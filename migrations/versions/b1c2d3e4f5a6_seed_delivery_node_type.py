# SPDX-License-Identifier: Apache-2.0
"""seed Delivery node type (actionable-layer outbound records)

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-07-05 17:00:00.000000

A Delivery is a pending outbound record the runner writes when a job's plan has a `deliver` or
`elicit` action (docs/design/actionable-layer.md §7 / prompts §1). The bot reads pending
Deliveries, sends them, and marks them sent — so delivery is decoupled from the bot through the
graph (D3). Idempotent (ON CONFLICT DO NOTHING).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DESCRIPTION = (
    "A pending outbound record produced by the actionable-layer runner from a job plan's "
    "`deliver` or `elicit` action: the message/question, its delivery target, an occurrence-"
    "correlated idempotency key, and a status the bot flips when it sends. Decouples delivery "
    "from the bot through the graph (D3)."
)

_PROPS_SCHEMA = {
    "text": "str — the message (deliver) or question (elicit)",
    "kind": "message | question",
    "target": "delivery target (immutable, from the job; D22/D32)",
    "key": "{occurrence_id}:{index} — runner-owned idempotency key",
    "status": "pending | sent",
    "target_field": "(elicit) the structured field the reply fills",
    "default": "(elicit) value used if the operator stays silent",
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
            type="Delivery",
            default_sensitivity="normal",
            description=_DESCRIPTION,
            props_schema=_PROPS_SCHEMA,
        )
        .on_conflict_do_nothing(index_elements=["type"])
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM node_types WHERE type = 'Delivery'"))
