# SPDX-License-Identifier: Apache-2.0
"""review: B.9 B-5 — the threat-boundary registry

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-19 00:00:00.000000

B.9 slice 2 (spec docs/design/2026-08-16_review_loop_next_spec.md, Part B, B-5): the
durable source of the standing threat frame is a service-side registry with exactly two
authorized write paths — the management endpoint (deployment scope, an operator act with
their quote) and the `records_boundary` marker on a relayed in-review operator message
(review scope, minted by the server). A development waive claiming the mechanical
round-gate route must carry a `boundary_ref` that resolves to an entry eligible for the
current review (deployment-scoped, or scoped to it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"


def upgrade() -> None:
    op.create_table(
        "threat_boundary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(["review_id"], [f"{SCHEMA}.review.id"], ondelete="CASCADE"),
        sa.CheckConstraint("scope IN ('deployment','review')", name="ck_threat_boundary_scope"),
        sa.CheckConstraint(
            "source IN ('management','review_ruling')", name="ck_threat_boundary_source"
        ),
        sa.CheckConstraint(
            "(scope = 'review') = (review_id IS NOT NULL)",
            name="ck_threat_boundary_scope_review",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_threat_boundary_review", "threat_boundary", ["review_id"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("threat_boundary", schema=SCHEMA)
