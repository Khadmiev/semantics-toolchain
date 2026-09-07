# SPDX-License-Identifier: Apache-2.0
"""operator-profile: membership marker, domain registry index, identity columns

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-14 19:10:00.000000

Cycle-2 implementation of the operator-profile plugin
(docs/design/2026-07-14_operator_profile_plugin_spec.md, review a6827c1a):

- profile_entries — the server-set profile membership marker (account-bound
  ownership, conflict key, profile STANDING member|candidate|parked, the
  candidate's recorded resolved-effective base, validated_version_id).
  Standing-split identity is enforced by two partial unique indexes.
- profile_domains — the fast index over the domain registry (the audited record
  is a versioned graph node per entry).
- accounts.person_node_id — the account's own Person node (B3 identity anchor).
- credentials.sensitive_capable — E38: may this credential receive
  above-normal-sensitivity preference content (operator-set, default false).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NIL = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.create_table(
        "profile_entries",
        sa.Column("node_id", sa.Uuid(), primary_key=True),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("project_space_id", sa.Uuid(), nullable=True),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("standing", sa.String(), nullable=False),
        sa.Column("base_scope", sa.String(), nullable=True),
        sa.Column("base_node_id", sa.Uuid(), nullable=True),
        sa.Column("base_version_id", sa.Uuid(), nullable=True),
        sa.Column("validated_version_id", sa.Uuid(), nullable=False),
        sa.Column("merge_provenance", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_space_id"], ["spaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["validated_version_id"], ["node_versions.id"], ondelete="CASCADE"),
        sa.CheckConstraint("scope IN ('global','project')", name="ck_profile_entries_scope"),
        sa.CheckConstraint(
            "standing IN ('member','candidate','parked')", name="ck_profile_entries_standing"
        ),
        sa.CheckConstraint(
            "(scope = 'project') = (project_space_id IS NOT NULL)",
            name="ck_profile_entries_project_key",
        ),
    )
    op.create_index(
        "uq_profile_member_per_key",
        "profile_entries",
        ["account_id", "scope", "domain", sa.text(f"COALESCE(project_space_id, '{_NIL}'::uuid)")],
        unique=True,
        postgresql_where=sa.text("standing = 'member'"),
    )
    op.create_index(
        "uq_profile_candidate_per_key",
        "profile_entries",
        ["account_id", "scope", "domain", sa.text(f"COALESCE(project_space_id, '{_NIL}'::uuid)")],
        unique=True,
        postgresql_where=sa.text("standing = 'candidate'"),
    )
    op.create_index("ix_profile_entries_account", "profile_entries", ["account_id"])
    op.create_index("ix_profile_entries_domain", "profile_entries", ["domain"])

    op.create_table(
        "profile_domains",
        sa.Column("domain", sa.String(), primary_key=True),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("canonical_of", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["canonical_of"], ["profile_domains.domain"]),
        sa.CheckConstraint(
            "state IN ('accepted','pending','rejected','alias')", name="ck_profile_domains_state"
        ),
        sa.CheckConstraint(
            "(state = 'alias') = (canonical_of IS NOT NULL)",
            name="ck_profile_domains_alias_target",
        ),
    )

    op.add_column("accounts", sa.Column("person_node_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_accounts_person_node", "accounts", "nodes", ["person_node_id"], ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "credentials",
        sa.Column("sensitive_capable", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("credentials", "sensitive_capable")
    op.drop_constraint("fk_accounts_person_node", "accounts", type_="foreignkey")
    op.drop_column("accounts", "person_node_id")
    op.drop_table("profile_domains")
    op.drop_index("ix_profile_entries_domain", table_name="profile_entries")
    op.drop_index("ix_profile_entries_account", table_name="profile_entries")
    op.drop_index("uq_profile_candidate_per_key", table_name="profile_entries")
    op.drop_index("uq_profile_member_per_key", table_name="profile_entries")
    op.drop_table("profile_entries")
