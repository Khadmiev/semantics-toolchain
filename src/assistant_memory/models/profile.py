# SPDX-License-Identifier: Apache-2.0
"""Operator-profile plugin tables (spec docs/design/2026-07-14_operator_profile_plugin_spec.md).

Two server-side control tables sit BESIDE the graph:

- ``profile_entries`` — the PROFILE MEMBERSHIP MARKER. A node is a profile
  preference iff it has a row here; the row carries the owning account (ownership
  is account-equality, never zone writability), the conflict key (scope, domain
  [, project]), the profile ``standing`` (member | candidate | parked | rejected —
  the axis that drives compilation; Core node ``status`` stays purely epistemic;
  ``rejected`` is B.13 A-9's TERMINAL tombstone: the slot vacates for a corrected
  re-proposal while the row is retained, every other disposition is refused against
  it, and a repeat reject answers "already retired" from the retained row), the
  candidate's recorded resolved-effective base, and ``validated_version_id`` —
  the node version produced by the validated profile path. Compilation trusts a
  row only while ``validated_version_id == nodes.current_version_id`` (defense
  in depth: a version minted outside the validated path can never bind).

- ``profile_domains`` — the fast lookup index over the domain REGISTRY. The
  audited record of each registry entry is an ordinary versioned graph node
  (``node_id``); this table is the index the write path consults (alias
  normalization, pending/rejected gating) and the protection check keys off.

Both kinds of rows mark their nodes CONTROL-PLANE PROTECTED: generic
update_node/delete_node refuse them entirely; mutations go through the profile
capabilities only.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

PROFILE_SCOPES = ("global", "project")
# "rejected" is the terminal TOMBSTONE standing (B.13 A-9): the row is retained so
# the node stays control-plane protected and the idempotent repeated reject has a
# durable lookup, but it occupies no slot (the uniqueness indexes key only
# member/candidate), compiles into nothing, and no disposition leaves it.
PROFILE_STANDINGS = ("member", "candidate", "parked", "rejected")
DOMAIN_STATES = ("accepted", "pending", "rejected", "alias")

# COALESCE target for the nullable project key inside unique indexes (global scope).
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


class ProfileEntry(Base):
    """Server-set profile-membership marker for one preference node."""

    __tablename__ = "profile_entries"

    node_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    scope: Mapped[str] = mapped_column(String)
    # Set iff scope == 'project' (enforced by ck_profile_entries_project_key).
    project_space_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE")
    )
    domain: Mapped[str] = mapped_column(String)
    standing: Mapped[str] = mapped_column(String)
    # A candidate's recorded RESOLVED-EFFECTIVE base: the rule resolution returned
    # for the domain at inference time (project member, else global member, else
    # null — all three columns NULL). Never rewritten (the freshness guard's whole
    # point); checked by confirm_preference Mode 1.
    base_scope: Mapped[str | None] = mapped_column(String)
    base_node_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # The node version the validated profile path produced. Compilation requires
    # it to equal nodes.current_version_id.
    validated_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("node_versions.id", ondelete="CASCADE")
    )
    # Parking provenance (which alias merge parked it, when) — audit for PARKED rows.
    merge_provenance: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "scope IN ('global','project')", name="ck_profile_entries_scope"
        ),
        CheckConstraint(
            "standing IN ('member','candidate','parked','rejected')",
            name="ck_profile_entries_standing",
        ),
        CheckConstraint(
            "(scope = 'project') = (project_space_id IS NOT NULL)",
            name="ck_profile_entries_project_key",
        ),
        # One MEMBER and one CANDIDATE per (account, scope, domain [, project]) —
        # the standing-split identity invariant. PARKED rows are not unique.
        Index(
            "uq_profile_member_per_key",
            "account_id",
            "scope",
            "domain",
            text(f"COALESCE(project_space_id, '{_NIL_UUID}'::uuid)"),
            unique=True,
            postgresql_where=text("standing = 'member'"),
        ),
        Index(
            "uq_profile_candidate_per_key",
            "account_id",
            "scope",
            "domain",
            text(f"COALESCE(project_space_id, '{_NIL_UUID}'::uuid)"),
            unique=True,
            postgresql_where=text("standing = 'candidate'"),
        ),
        Index("ix_profile_entries_account", "account_id"),
        Index("ix_profile_entries_domain", "domain"),
    )


class ProfileDomain(Base):
    """Registry index row for one domain key (canonical or alias)."""

    __tablename__ = "profile_domains"

    domain: Mapped[str] = mapped_column(String, primary_key=True)
    # The audited registry record — an ordinary versioned graph node.
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String)
    # Set iff state == 'alias': the accepted canonical key this one folds into.
    canonical_of: Mapped[str | None] = mapped_column(ForeignKey("profile_domains.domain"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('accepted','pending','rejected','alias')",
            name="ck_profile_domains_state",
        ),
        CheckConstraint(
            "(state = 'alias') = (canonical_of IS NOT NULL)",
            name="ck_profile_domains_alias_target",
        ),
    )
