# SPDX-License-Identifier: Apache-2.0
"""node_versions search: embedding + fts

Revision ID: 415645e80676
Revises: 01e41739e763
Create Date: 2026-06-30 09:31:46.822964

Adds the B6 search columns to node_versions: a 1024-dim pgvector ``embedding``
(bge-m3 / e5-large dense dim) with an hnsw cosine index, and a ``search_tsv``
tsvector with a GIN index for RU+EN full-text. Both are filled by the indexer
on write; pre-existing rows stay NULL until reindexed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
revision: str = "415645e80676"
down_revision: str | Sequence[str] | None = "01e41739e763"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("node_versions", sa.Column("embedding", Vector(1024), nullable=True))
    op.add_column("node_versions", sa.Column("search_tsv", TSVECTOR(), nullable=True))
    op.execute(
        "CREATE INDEX ix_node_versions_embedding ON node_versions "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX ix_node_versions_search_tsv ON node_versions USING gin (search_tsv)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_node_versions_search_tsv")
    op.execute("DROP INDEX IF EXISTS ix_node_versions_embedding")
    op.drop_column("node_versions", "search_tsv")
    op.drop_column("node_versions", "embedding")
