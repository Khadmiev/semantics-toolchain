# SPDX-License-Identifier: Apache-2.0
"""Re-embed every current node version — run after switching embedding models.

Embeddings live on the current version (B6). When the model changes (e.g. the dev
deterministic stub -> bge-m3), stored vectors are stale and must be recomputed, or
query vectors won't match. This walks all visible (non-deleted) nodes and re-indexes
each through the configured embedder.

Usage (with the real model):
    AM_EMBEDDING_PROVIDER=sentence_transformers \
        uv run --extra embeddings python -m assistant_memory.search.reindex
"""

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import Node
from .embedder import Embedder, get_embedder
from .indexer import index_node


async def reindex_all(session: AsyncSession, embedder: Embedder | None = None) -> int:
    """Re-embed + re-tsvector the current version of every non-deleted node.

    Flushes per node; the caller commits. Returns the number of nodes reindexed.
    """
    embedder = embedder or get_embedder()
    nodes = list(
        await session.scalars(
            select(Node).where(Node.deleted_at.is_(None), Node.current_version_id.isnot(None))
        )
    )
    for node in nodes:
        await index_node(session, node, embedder)
    return len(nodes)


async def _main() -> None:
    from ..db import SessionLocal

    embedder = get_embedder()
    print(f"reindexing with {type(embedder).__name__} ...")
    async with SessionLocal() as session:
        count = await reindex_all(session, embedder)
        await session.commit()
    print(f"done: reindexed {count} nodes")


if __name__ == "__main__":
    asyncio.run(_main())
