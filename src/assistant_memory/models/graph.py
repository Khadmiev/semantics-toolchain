# SPDX-License-Identifier: Apache-2.0
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .base import EDGE_CATEGORIES, NODE_STATUSES, SENSITIVITIES, Base, _check_values

EMBEDDING_DIM = 1024  # bge-m3 / e5-large dense dim (B6); migration locks the column


class NodeType(Base):
    """Soft type registry for nodes (new type = a row, no migration)."""

    __tablename__ = "node_types"

    type: Mapped[str] = mapped_column(String, primary_key=True)
    default_sensitivity: Mapped[str] = mapped_column(String, server_default="normal")
    description: Mapped[str | None] = mapped_column(String)
    props_schema: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            _check_values("default_sensitivity", SENSITIVITIES),
            name="ck_node_types_sensitivity",
        ),
    )


class EdgeType(Base):
    """Soft type registry for edges, with containment/associative + sensitive flag."""

    __tablename__ = "edge_types"

    type: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str | None] = mapped_column(String)
    category: Mapped[str] = mapped_column(String, server_default="associative")
    sensitive: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    src_types: Mapped[list | None] = mapped_column(JSONB)
    dst_types: Mapped[list | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(_check_values("category", EDGE_CATEGORIES), name="ck_edge_types_category"),
    )


class Node(Base):
    """Graph node. Current projection; history in node_versions."""

    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(ForeignKey("node_types.type"))
    label: Mapped[str | None] = mapped_column(String)
    properties: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Epistemic validity axis (§6), orthogonal to `type`. Current projection; the
    # per-version value lives on node_versions for history.
    status: Mapped[str] = mapped_column(String, server_default="current")
    sensitivity: Mapped[str | None] = mapped_column(String)
    origin_space: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("spaces.id"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Cyclic FK with node_versions -> created via ALTER (use_alter) to break the cycle.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("node_versions.id", use_alter=True, name="fk_nodes_current_version")
    )

    __table_args__ = (
        CheckConstraint(
            "sensitivity IS NULL OR sensitivity IN ('low', 'normal', 'high', 'critical')",
            name="ck_nodes_sensitivity",
        ),
        CheckConstraint(_check_values("status", NODE_STATUSES), name="ck_nodes_status"),
        Index("ix_nodes_status", "status"),
    )


class NodeVersion(Base):
    """Immutable, append-only version of a node.

    Search lives on the version (not the node) so embeddings purge naturally: a
    new version supersedes the old, and search only ever ranks current versions
    (decisions §6 #7). ``embedding`` / ``search_tsv`` are filled by the B6 indexer.
    """

    __tablename__ = "node_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    label: Mapped[str | None] = mapped_column(String)
    properties: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # The node's epistemic status at this version (§6) — the status audit trail.
    status: Mapped[str] = mapped_column(String, server_default="current")
    source_ref: Mapped[str | None] = mapped_column(String)
    author: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    search_tsv: Mapped[str | None] = mapped_column(TSVECTOR)

    __table_args__ = (
        CheckConstraint(_check_values("status", NODE_STATUSES), name="ck_node_versions_status"),
    )


class Edge(Base):
    """Typed, directed, time-versioned edge between two nodes."""

    __tablename__ = "edges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(ForeignKey("edge_types.type"))
    src_node: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    dst_node: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    properties: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    sensitive: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Idempotency: at most one active edge of a given (type, src, dst).
        Index(
            "uq_active_edge",
            "type",
            "src_node",
            "dst_node",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        # Containment single-parent: a node has at most one active container.
        Index(
            "uq_one_container",
            "src_node",
            unique=True,
            postgresql_where=text("type = 'contained_in' AND valid_to IS NULL"),
        ),
        Index("ix_edges_src_active", "src_node", postgresql_where=text("valid_to IS NULL")),
        Index("ix_edges_dst_active", "dst_node", postgresql_where=text("valid_to IS NULL")),
    )


class NodeSpace(Base):
    """Reference: a node is included in a space (many-to-many)."""

    __tablename__ = "node_spaces"

    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    added_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EdgeSpace(Base):
    """Explicit inclusion of a sensitive edge in a space (induced edges need none)."""

    __tablename__ = "edge_spaces"

    edge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("edges.id", ondelete="CASCADE"), primary_key=True
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True
    )
    added_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
