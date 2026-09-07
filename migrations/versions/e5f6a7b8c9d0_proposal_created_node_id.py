# SPDX-License-Identifier: Apache-2.0
"""proposals.created_node_id — the node a create_node proposal actually created

Lets a staged proposal reference a SIBLING proposal's future node. A staged create returns no
node id (the node exists only after approval), so before this there was no way to express
"link this future child to that future parent" — every structure the bot created in one turn
arrived with no edges (Incident 4b087d59).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proposals",
        sa.Column("created_node_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_proposals_created_node_id_nodes",
        "proposals",
        "nodes",
        ["created_node_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_proposals_created_node_id_nodes", "proposals", type_="foreignkey")
    op.drop_column("proposals", "created_node_id")
