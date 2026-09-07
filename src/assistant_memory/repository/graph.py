# SPDX-License-Identifier: Apache-2.0
"""Core graph mutations: versioned nodes (CAS), idempotent edges and references.

Functions flush but do not commit — the caller owns the transaction.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import (
    Edge,
    EdgeSpace,
    EdgeType,
    Node,
    NodeSpace,
    NodeType,
    NodeVersion,
)
from .errors import ConflictError, ContainerConflictError, NotFoundError

_UNSET: Any = object()
_HIGH = ("high", "critical")


async def _effective_sensitivity(session: AsyncSession, node: Node) -> str:
    if node.sensitivity:
        return node.sensitivity
    node_type = await session.get(NodeType, node.type)
    return node_type.default_sensitivity if node_type else "normal"


# --- nodes ---------------------------------------------------------------


async def create_node(
    session: AsyncSession,
    *,
    type: str,
    space_id: uuid.UUID,
    account_id: uuid.UUID,
    label: str | None = None,
    properties: dict | None = None,
    sensitivity: str | None = None,
    source_ref: str | None = None,
    status: str = "current",
) -> Node:
    """Create a node with its first version and a reference in `space_id`."""
    properties = properties or {}
    node = Node(
        type=type,
        label=label,
        properties=properties,
        status=status,
        sensitivity=sensitivity,
        origin_space=space_id,
        created_by=account_id,
    )
    session.add(node)
    await session.flush()  # assigns node.id

    version = NodeVersion(
        node_id=node.id,
        label=label,
        properties=properties,
        status=status,
        source_ref=source_ref,
        author=account_id,
    )
    session.add(version)
    await session.flush()  # assigns version.id

    node.current_version_id = version.id
    session.add(NodeSpace(node_id=node.id, space_id=space_id, added_by=account_id))
    await session.flush()
    return node


async def get_node(session: AsyncSession, node_id: uuid.UUID) -> Node | None:
    node = await session.get(Node, node_id)
    if node is None or node.deleted_at is not None:
        return None
    return node


async def update_node(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    account_id: uuid.UUID,
    expected_version: uuid.UUID,
    label: Any = _UNSET,
    properties_patch: dict | None = None,
    source_ref: str | None = None,
    status: Any = _UNSET,
) -> Node:
    """Append a new version (CAS on `expected_version`). properties_patch is a shallow merge."""
    node = await session.get(Node, node_id)
    if node is None or node.deleted_at is not None:
        raise NotFoundError(f"node {node_id} not found")
    if node.current_version_id != expected_version:
        raise ConflictError(
            f"stale version: expected {expected_version}, current {node.current_version_id}"
        )

    new_label = node.label if label is _UNSET else label
    new_status = node.status if status is _UNSET else status
    new_props = node.properties
    if properties_patch is not None:
        new_props = {**(node.properties or {}), **properties_patch}

    version = NodeVersion(
        node_id=node.id,
        label=new_label,
        properties=new_props,
        status=new_status,
        source_ref=source_ref,
        author=account_id,
    )
    session.add(version)
    await session.flush()

    node.label = new_label
    node.properties = new_props
    node.status = new_status
    node.current_version_id = version.id
    await session.flush()
    return node


async def delete_node(
    session: AsyncSession,
    *,
    node_id: uuid.UUID,
    expected_version: uuid.UUID,
) -> Node:
    """Soft-delete (CAS). The node and its versions stay for history."""
    node = await session.get(Node, node_id)
    if node is None or node.deleted_at is not None:
        raise NotFoundError(f"node {node_id} not found")
    if node.current_version_id != expected_version:
        raise ConflictError(
            f"stale version: expected {expected_version}, current {node.current_version_id}"
        )
    node.deleted_at = datetime.now(UTC)
    await session.flush()
    return node


# --- edges ---------------------------------------------------------------


async def _derive_sensitive(
    session: AsyncSession, type_: str, src: uuid.UUID, dst: uuid.UUID
) -> bool:
    edge_type = await session.get(EdgeType, type_)
    if edge_type is not None and edge_type.sensitive:
        return True
    for node_id in (src, dst):
        node = await session.get(Node, node_id)
        if node is not None and await _effective_sensitivity(session, node) in _HIGH:
            return True
    return False


async def _active_edge(
    session: AsyncSession, type_: str, src: uuid.UUID, dst: uuid.UUID
) -> Edge | None:
    return await session.scalar(
        select(Edge).where(
            Edge.type == type_,
            Edge.src_node == src,
            Edge.dst_node == dst,
            Edge.valid_to.is_(None),
        )
    )


async def link(
    session: AsyncSession,
    *,
    type: str,
    src_node: uuid.UUID,
    dst_node: uuid.UUID,
    account_id: uuid.UUID,
    properties: dict | None = None,
    sensitive: bool | None = None,
) -> Edge:
    """Create an edge; idempotent on the active (type, src, dst). Enforces single container."""
    existing = await _active_edge(session, type, src_node, dst_node)
    if existing is not None:
        return existing

    if sensitive is None:
        sensitive = await _derive_sensitive(session, type, src_node, dst_node)

    edge = Edge(
        type=type,
        src_node=src_node,
        dst_node=dst_node,
        properties=properties or {},
        sensitive=sensitive,
        created_by=account_id,
    )
    try:
        async with session.begin_nested():
            session.add(edge)
            await session.flush()
    except IntegrityError as exc:
        detail = str(getattr(exc, "orig", exc))
        if "uq_one_container" in detail:
            raise ContainerConflictError(
                f"node {src_node} already has an active container"
            ) from exc
        # uq_active_edge race: another tx inserted the same active edge first.
        racer = await _active_edge(session, type, src_node, dst_node)
        if racer is not None:
            return racer
        raise
    return edge


async def unlink(session: AsyncSession, *, edge_id: uuid.UUID) -> bool:
    """Retract an edge (soft). Idempotent: returns False if already inactive/absent."""
    edge = await session.get(Edge, edge_id)
    if edge is None or edge.valid_to is not None:
        return False
    edge.valid_to = datetime.now(UTC)
    await session.flush()
    return True


# --- references (sharing) ------------------------------------------------


async def share_node(
    session: AsyncSession, *, node_id: uuid.UUID, space_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    """Add a node reference to a space. Idempotent; returns True if newly added."""
    stmt = (
        pg_insert(NodeSpace)
        .values(node_id=node_id, space_id=space_id, added_by=account_id)
        .on_conflict_do_nothing(index_elements=["node_id", "space_id"])
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def unshare_node(session: AsyncSession, *, node_id: uuid.UUID, space_id: uuid.UUID) -> bool:
    """Remove a node reference. Idempotent; returns True if a row was removed."""
    result = await session.execute(
        delete(NodeSpace).where(NodeSpace.node_id == node_id, NodeSpace.space_id == space_id)
    )
    return result.rowcount > 0


async def share_edge(
    session: AsyncSession, *, edge_id: uuid.UUID, space_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    """Explicitly include a (sensitive) edge in a space. Idempotent."""
    stmt = (
        pg_insert(EdgeSpace)
        .values(edge_id=edge_id, space_id=space_id, added_by=account_id)
        .on_conflict_do_nothing(index_elements=["edge_id", "space_id"])
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def unshare_edge(session: AsyncSession, *, edge_id: uuid.UUID, space_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(EdgeSpace).where(EdgeSpace.edge_id == edge_id, EdgeSpace.space_id == space_id)
    )
    return result.rowcount > 0
