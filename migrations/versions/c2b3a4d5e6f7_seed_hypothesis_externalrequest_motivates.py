# SPDX-License-Identifier: Apache-2.0
"""seed Hypothesis / ExternalRequest node types + motivates edge (§4-§6)

Revision ID: c2b3a4d5e6f7
Revises: b1f2c3d4e5a6
Create Date: 2026-07-02 17:25:00.000000

The remaining Core types the ruleset assumes (handoff §4.1). They were first
registered at runtime in the live graph; this migration makes them part of the
schema so a fresh database (a new machine, a clean checkout, another agent's
onboarding) matches prod. Idempotent via ON CONFLICT DO NOTHING — a no-op where
the type already exists, so it is safe to run against the live server.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

# revision identifiers, used by Alembic.
revision: str = "c2b3a4d5e6f7"
down_revision: str | Sequence[str] | None = "b1f2c3d4e5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (type, default_sensitivity, description)
NODE_TYPES = [
    (
        "Hypothesis",
        "normal",
        "A conjecture with its own lifecycle, distinct from a Decision (a ruling) and an "
        "Experiment (an investigation). Carries the claim, its rationale/prior, and what would "
        "confirm or refute it. status moves open -> testing -> supported | rejected | superseded "
        "(the validity axis, §6). Use `motivates` to link to the Experiment that tests it or the "
        "OpenQuestion it opens; on rejection it is RECORDED not deleted, with rejected_reason + "
        "stage (in discussion / by experiment) + who/when, so it is not re-proposed and "
        "re-argued. Makes 'what did we think, and did it hold?' queryable.",
    ),
    (
        "ExternalRequest",
        "normal",
        "An incoming ask/directive from outside (colleague, stakeholder, operator) that drives "
        "work. NOT knowledge — a stimulus. Carries who asked, what was asked, when, and any "
        "constraints/context. Use `motivates` to link to the work it produced (Experiment / "
        "Decision / Task), so 'what did this request lead to' is answerable. Distinct from a Task "
        "(the actionable item) and from a Decision (the ruling).",
    ),
]

# (type, category, description)
EDGE_TYPES = [
    (
        "motivates",
        "associative",
        "src (a stimulus — ExternalRequest or Hypothesis) drove dst (the work it produced — "
        "Experiment / Decision / Task). Directional: stimulus -> work. Query: 'what did this "
        "request/hypothesis lead to' = motivates out-edges of the stimulus; 'what motivated this "
        "work' = motivates in-edges. Distinct from `informs` (evidence -> ruling) and "
        "`depends_on` (flow/dependency).",
    ),
]

_node_types = sa.table(
    "node_types",
    sa.column("type", sa.String),
    sa.column("default_sensitivity", sa.String),
    sa.column("description", sa.String),
)
_edge_types = sa.table(
    "edge_types",
    sa.column("type", sa.String),
    sa.column("category", sa.String),
    sa.column("sensitive", sa.Boolean),
    sa.column("description", sa.String),
)


def upgrade() -> None:
    bind = op.get_bind()
    for type_, sensitivity, description in NODE_TYPES:
        bind.execute(
            pg_insert(_node_types)
            .values(type=type_, default_sensitivity=sensitivity, description=description)
            .on_conflict_do_nothing(index_elements=["type"])
        )
    for type_, category, description in EDGE_TYPES:
        bind.execute(
            pg_insert(_edge_types)
            .values(type=type_, category=category, sensitive=False, description=description)
            .on_conflict_do_nothing(index_elements=["type"])
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM edge_types WHERE type = ANY(:types)"),
        {"types": [t for t, _, _ in EDGE_TYPES]},
    )
    bind.execute(
        sa.text("DELETE FROM node_types WHERE type = ANY(:types)"),
        {"types": [t for t, _, _ in NODE_TYPES]},
    )
