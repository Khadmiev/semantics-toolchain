# SPDX-License-Identifier: Apache-2.0
"""node status: epistemic validity axis (kind x status, §6)

Revision ID: b1f2c3d4e5a6
Revises: 3a6eb2a3a845
Create Date: 2026-07-02 17:10:00.000000

Promotes the epistemic ``status`` from a properties-JSON convention to a
first-class, queryable column on both ``nodes`` (current projection) and
``node_versions`` (the per-version audit trail). Values are constrained to the
§6 enum (current | provisional | superseded | disputed | rejected); existing
rows backfill to ``current`` via the server default. A btree index on
``nodes.status`` supports filtering ("all provisional nodes", "rejected
hypotheses"). Transition rules stay an agent-level contract — NOT enforced here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f2c3d4e5a6"
down_revision: str | Sequence[str] | None = "3a6eb2a3a845"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CK = "status IN ('current', 'provisional', 'superseded', 'disputed', 'rejected')"


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("nodes", "node_versions"):
        op.add_column(
            table,
            sa.Column("status", sa.String(), nullable=False, server_default="current"),
        )
        op.create_check_constraint(f"ck_{table}_status", table, _STATUS_CK)
    op.create_index("ix_nodes_status", "nodes", ["status"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_nodes_status", table_name="nodes")
    for table in ("node_versions", "nodes"):
        op.drop_constraint(f"ck_{table}_status", table, type_="check")
        op.drop_column(table, "status")
