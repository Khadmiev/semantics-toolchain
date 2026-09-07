# SPDX-License-Identifier: Apache-2.0
"""seed Feedback node type (conventions feedback intake)

Revision ID: d3c4b5a6f7e8
Revises: c2b3a4d5e6f7
Create Date: 2026-07-03 18:30:00.000000

The node type behind the `feedback` tool: a report about the conventions /
memory-behavior, landed in the conventions owner's zone. First registered at
runtime; this seeds it so a fresh DB matches. Idempotent (ON CONFLICT DO NOTHING).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert as pg_insert

# revision identifiers, used by Alembic.
revision: str = "d3c4b5a6f7e8"
down_revision: str | Sequence[str] | None = "c2b3a4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DESCRIPTION = (
    "Feedback about the assistant-memory conventions or the agent's memory-behavior — a "
    "mis-application, ambiguity, gap, or suggestion — reported (often from ANOTHER project) via "
    "the `feedback` tool, which lands it in the conventions owner's zone so no foreign agent "
    "hand-writes into another project's subtree. Carries the report text, kind, optional section, "
    "and who reported it. Starts status=provisional; the §14 maintenance pass triages it."
)

_node_types = sa.table(
    "node_types",
    sa.column("type", sa.String),
    sa.column("default_sensitivity", sa.String),
    sa.column("description", sa.String),
)


def upgrade() -> None:
    op.get_bind().execute(
        pg_insert(_node_types)
        .values(type="Feedback", default_sensitivity="normal", description=_DESCRIPTION)
        .on_conflict_do_nothing(index_elements=["type"])
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM node_types WHERE type = 'Feedback'"))
