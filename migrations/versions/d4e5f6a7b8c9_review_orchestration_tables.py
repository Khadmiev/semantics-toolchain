# SPDX-License-Identifier: Apache-2.0
"""review-orchestration tables (schema review_orchestration)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-08 09:30:00.000000

Foundation for the automated dev<->critic review loop (spec
docs/design/2026-07-07_review_orchestration_spec.md §3.1): an ISOLATED Postgres schema
holding reviews, their append-only message log, and per-review auth tokens. Kept out of
the graph tables (INV-7) so transient review state never mixes with durable memory.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "review",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default=sa.text("'created'")),
        sa.Column("parked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("iteration", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("artifact_ref", postgresql.JSONB(), nullable=True),
        sa.Column("config", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("last_material_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode IN ('spec','code')", name="ck_review_mode"),
        sa.CheckConstraint(
            "state IN ('created','artifact_ready','critic_reviewing','dev_disposing',"
            "'converged','operator_gate','finalized','returned','abandoned')",
            name="ck_review_state",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_review_state", "review", ["state"], schema=SCHEMA)

    op.create_table(
        "review_message",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["review_id"], [f"{SCHEMA}.review.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "role IN ('development','critic','operator','system')", name="ck_review_message_role"
        ),
        sa.CheckConstraint(
            "kind IN ('artifact','findings','disposition','status','human_question',"
            "'waiver','dissent','escalation','decision_response','notice')",
            name="ck_review_message_kind",
        ),
        sa.UniqueConstraint("review_id", "seq", name="uq_review_message_seq"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_review_message_review_seq", "review_message", ["review_id", "seq"], schema=SCHEMA
    )

    op.create_table(
        "review_token",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["review_id"], [f"{SCHEMA}.review.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("role IN ('development','critic')", name="ck_review_token_role"),
        sa.UniqueConstraint("token_hash", name="uq_review_token_hash"),
        schema=SCHEMA,
    )
    op.create_index("ix_review_token_review", "review_token", ["review_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("review_token", schema=SCHEMA)
    op.drop_table("review_message", schema=SCHEMA)
    op.drop_table("review", schema=SCHEMA)
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
