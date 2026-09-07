# SPDX-License-Identifier: Apache-2.0
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import MODES, PROPOSAL_STATUSES, SENSITIVITIES, Base, _check_values


class SensitivityMode(Base):
    """Write-policy base table: (sensitivity x operation) -> mode."""

    __tablename__ = "sensitivity_mode"

    sensitivity: Mapped[str] = mapped_column(String, primary_key=True)
    operation: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            _check_values("sensitivity", SENSITIVITIES), name="ck_sensmode_sensitivity"
        ),
        CheckConstraint(_check_values("mode", MODES), name="ck_sensmode_mode"),
    )


class TemplateFloor(Base):
    """Per space-template minimum mode (floor)."""

    __tablename__ = "template_floor"

    space_template: Mapped[str] = mapped_column(String, primary_key=True)
    min_mode: Mapped[str] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(_check_values("min_mode", MODES), name="ck_templatefloor_mode"),
    )


class TrustFloor(Base):
    """Per client-trust minimum mode (floor)."""

    __tablename__ = "trust_floor"

    client_trust: Mapped[str] = mapped_column(String, primary_key=True)
    min_mode: Mapped[str] = mapped_column(String)

    __table_args__ = (CheckConstraint(_check_values("min_mode", MODES), name="ck_trustfloor_mode"),)


class Proposal(Base):
    """Staged mutation awaiting server-side confirm (low-trust clients)."""

    __tablename__ = "proposals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    proposer_client: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credentials.id"))
    op: Mapped[str] = mapped_column(String)
    target_ref: Mapped[dict | None] = mapped_column(JSONB)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    sensitivity: Mapped[str | None] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The node a create_node proposal actually created, recorded at apply time. This is what lets
    # a SIBLING proposal reference a node that did not exist when it was staged: a staged create
    # returns no node id (the node is only proposed), so "link this future child to that future
    # parent" is otherwise inexpressible — the defect that shipped a structurally empty shopping
    # list (Incident 4b087d59). Parents apply first, so a child's parent ref resolves through here.
    created_node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"))

    __table_args__ = (
        CheckConstraint(_check_values("status", PROPOSAL_STATUSES), name="ck_proposals_status"),
        CheckConstraint(_check_values("mode", MODES), name="ck_proposals_mode"),
    )
