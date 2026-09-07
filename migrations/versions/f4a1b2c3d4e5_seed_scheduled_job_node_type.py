# SPDX-License-Identifier: Apache-2.0
"""seed ScheduledJob node type (scheduler plugin — actionable layer)

Revision ID: f4a1b2c3d4e5
Revises: d3c4b5a6f7e8
Create Date: 2026-07-05 15:00:00.000000

The node type behind the actionable-layer scheduler (docs/design/actionable-layer.md
D3/D4): a ScheduledJob is a declarative, autonomously-executed job whose definition
lives in the graph, not in box config. Node content is schemaless; `props_schema`
documents the shape (not enforced — validation lives in scheduler/job.py). Idempotent
(ON CONFLICT DO NOTHING).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

# revision identifiers, used by Alembic.
revision: str = "f4a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "d3c4b5a6f7e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DESCRIPTION = (
    "A scheduled/proactive job for the actionable layer: a declarative spec the resident "
    "runner executes without the operator in the chat (briefings, watchers, reminders, "
    "self-maintenance). Distinct from Task (a human todo): actor is the runner, and a "
    "recurring job never 'completes' — completion is per-occurrence. Definition lives in the "
    "graph so any client can author it and it is audited/versioned. See docs/design/"
    "actionable-layer.md D3/D4/D5."
)

# Documentation only (schemaless in the DB). Field names decided at implementation
# (OQ2, resolved 2026-07-05). Slice 1 supports trigger kinds one_shot | interval.
_PROPS_SCHEMA = {
    "enabled": "bool — paused = false",
    "trigger": "{kind: one_shot|interval|cron|event, spec, predicate?}",
    "instruction": "str — natural-language goal for the REASON step",
    "agency": "gather_then_judge | autonomous",
    "tools": "list[str] — capability allowlist (fence)",
    "writes": "declared write-scope (fence)",
    "budget": "tool-call / token cap (fence)",
    "delivery": "{channel, target} — target is immutable (D22/D32)",
    "next_run": "ISO-8601 UTC — when the runner should fire next",
    "last_run": "ISO-8601 UTC — last fire",
    "claimed_at": "ISO-8601 UTC — D23 lease; set on claim, cleared on finish",
    "finished_at": "ISO-8601 UTC — last occurrence completion",
}

_node_types = sa.table(
    "node_types",
    sa.column("type", sa.String),
    sa.column("default_sensitivity", sa.String),
    sa.column("description", sa.String),
    sa.column("props_schema", JSONB),
)


def upgrade() -> None:
    op.get_bind().execute(
        pg_insert(_node_types)
        .values(
            type="ScheduledJob",
            default_sensitivity="normal",
            description=_DESCRIPTION,
            props_schema=_PROPS_SCHEMA,
        )
        .on_conflict_do_nothing(index_elements=["type"])
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM node_types WHERE type = 'ScheduledJob'"))
