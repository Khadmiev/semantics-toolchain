# SPDX-License-Identifier: Apache-2.0
"""seed starter node and edge types

Revision ID: 5101366eff46
Revises: d8522fca4e80
Create Date: 2026-06-29 22:06:02.861474

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5101366eff46"
down_revision: str | Sequence[str] | None = "d8522fca4e80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (type, default_sensitivity)
NODE_TYPES = [
    ("Tag", "low"),
    ("Ingredient", "low"),
    ("List", "low"),
    ("ListItem", "low"),
    ("Note", "normal"),
    ("Idea", "normal"),
    ("Recipe", "normal"),
    ("Document", "normal"),
    ("DocumentChunk", "normal"),
    ("Person", "normal"),
    ("Task", "normal"),
    ("Project", "high"),
    ("Decision", "high"),
    ("ProjectState", "high"),
]

# (type, category)
EDGE_TYPES = [
    ("contained_in", "containment"),
    ("references", "associative"),
    ("relates_to", "associative"),
    ("mentions", "associative"),
    ("supersedes", "associative"),
    ("uses", "associative"),
    ("tagged_with", "associative"),
    ("has_friend", "associative"),
]


def upgrade() -> None:
    for type_, sensitivity in NODE_TYPES:
        op.execute(
            "INSERT INTO node_types (type, default_sensitivity) "
            f"VALUES ('{type_}', '{sensitivity}') ON CONFLICT (type) DO NOTHING"
        )
    for type_, category in EDGE_TYPES:
        op.execute(
            "INSERT INTO edge_types (type, category, sensitive) "
            f"VALUES ('{type_}', '{category}', false) ON CONFLICT (type) DO NOTHING"
        )


def downgrade() -> None:
    types = "', '".join(t for t, _ in EDGE_TYPES)
    op.execute(f"DELETE FROM edge_types WHERE type IN ('{types}')")
    types = "', '".join(t for t, _ in NODE_TYPES)
    op.execute(f"DELETE FROM node_types WHERE type IN ('{types}')")
