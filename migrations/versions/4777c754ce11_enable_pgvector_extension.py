# SPDX-License-Identifier: Apache-2.0
"""enable pgvector extension

Revision ID: 4777c754ce11
Revises:
Create Date: 2026-06-29 21:54:22.797513

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4777c754ce11"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Enable the pgvector extension (needed for embeddings in B6)."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
