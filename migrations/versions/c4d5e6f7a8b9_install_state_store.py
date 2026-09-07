# SPDX-License-Identifier: Apache-2.0
"""install-state store: settings, completion records, regression events (B.13 Part A)

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
Create Date: 2026-08-28 15:00:00.000000

B.13 Part A (docs/design/2026-08-27_public_repo_install_process_spec.md):

- install_settings — the canonical store for installer-owned values the running
  process must read back (A-1 stage 3: the conventions-home routing id; A-4: the
  pending-target and active-installed release identities; A-4/A-5: the persisted
  monotonic installation-generation counter).
- install_completion_records — A-4's completion record, written by the server on
  observing the canonical probe; append-only history stamped with the generation,
  the configuration identity, and the release the loop was proven on.
- install_regression_events — A-5's durable invalidation event; recording one
  advances the generation, retiring every earlier completion record forever.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "install_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "install_completion_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Uuid(),
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observation", postgresql.JSONB(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("config_identity", sa.String(), nullable=False),
        sa.Column("release_tag", sa.String(), nullable=True),
        sa.Column("release_commit", sa.String(), nullable=False),
    )
    op.create_index(
        "ix_install_completion_generation", "install_completion_records", ["generation"]
    )
    op.create_table(
        "install_regression_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("failed_stage", sa.String(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("generation_before", sa.Integer(), nullable=False),
        sa.Column("generation_after", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("install_regression_events")
    op.drop_index("ix_install_completion_generation", table_name="install_completion_records")
    op.drop_table("install_completion_records")
    op.drop_table("install_settings")
