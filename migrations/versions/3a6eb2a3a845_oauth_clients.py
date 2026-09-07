# SPDX-License-Identifier: Apache-2.0
"""oauth_clients

Revision ID: 3a6eb2a3a845
Revises: 415645e80676
Create Date: 2026-07-01 09:13:23.432405

Persist dynamically-registered OAuth clients (DCR, RFC 7591) so a client (e.g.
ChatGPT) that registered once keeps its client_id across server restarts. The
full client metadata is stored as JSONB.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3a6eb2a3a845"
down_revision: str | Sequence[str] | None = "415645e80676"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )


def downgrade() -> None:
    op.drop_table("oauth_clients")
