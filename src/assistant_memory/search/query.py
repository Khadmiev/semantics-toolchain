# SPDX-License-Identifier: Apache-2.0
"""Hybrid retrieval: vector + full-text, fused and access-filtered.

Two independent rankings — nearest embeddings and best FTS matches — are merged
with reciprocal-rank fusion (RRF), which needs no score calibration between the
two and is robust to scale differences. Everything is constrained to the nodes
the principal can see (``Access.space_ids()`` already honours credential scope).
"""

import asyncio
import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import Access
from ..models.graph import Node, NodeSpace, NodeVersion
from .embedder import Embedder, get_embedder

_RRF_K = 60  # standard RRF damping; larger flattens the rank contribution
_POOL_FACTOR = 5  # candidates pulled per ranking before fusion
_MIN_POOL = 50


def _filtered(stmt: Select, filters: dict | None) -> Select:
    if filters and filters.get("type"):
        stmt = stmt.where(Node.type == filters["type"])
    if filters and filters.get("status"):
        status = filters["status"]
        stmt = stmt.where(Node.status.in_(status if isinstance(status, list) else [status]))
    return stmt


async def search(
    session: AsyncSession,
    access: Access,
    query: str | None = None,
    *,
    limit: int = 20,
    filters: dict | None = None,
    embedder: Embedder | None = None,
) -> list[Node]:
    """Top nodes for ``query`` within the principal's visibility, best-first."""
    space_ids = await access.space_ids()
    if not space_ids:
        return []
    visible = (
        select(NodeSpace.node_id).where(NodeSpace.space_id.in_(space_ids)).distinct()
    )
    base_where = (Node.id.in_(visible), Node.deleted_at.is_(None))

    if not query:
        # No query text: most-recent visible nodes (still access-filtered).
        stmt = _filtered(
            select(Node)
            .join(NodeVersion, NodeVersion.id == Node.current_version_id)
            .where(*base_where),
            filters,
        ).order_by(Node.created_at.desc()).limit(limit)
        return list(await session.scalars(stmt))

    embedder = embedder or get_embedder()
    # blocking model inference off the event loop (same rule as indexer.index_node)
    qvec = (await asyncio.to_thread(embedder.embed, [query]))[0]
    pool = max(limit * _POOL_FACTOR, _MIN_POOL)

    vector_stmt = _filtered(
        select(Node.id)
        .join(NodeVersion, NodeVersion.id == Node.current_version_id)
        .where(*base_where, NodeVersion.embedding.isnot(None)),
        filters,
    ).order_by(NodeVersion.embedding.cosine_distance(qvec)).limit(pool)
    vector_ids = list(await session.scalars(vector_stmt))

    tsq = func.plainto_tsquery("russian", query).op("||")(
        func.plainto_tsquery("english", query)
    )
    fts_stmt = _filtered(
        select(Node.id)
        .join(NodeVersion, NodeVersion.id == Node.current_version_id)
        .where(*base_where, NodeVersion.search_tsv.op("@@")(tsq)),
        filters,
    ).order_by(func.ts_rank(NodeVersion.search_tsv, tsq).desc()).limit(pool)
    fts_ids = list(await session.scalars(fts_stmt))

    scores: dict[uuid.UUID, float] = {}
    for ranking in (vector_ids, fts_ids):
        for rank, node_id in enumerate(ranking):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    ordered = sorted(scores, key=lambda n: scores[n], reverse=True)[:limit]
    if not ordered:
        return []

    by_id = {n.id: n for n in await session.scalars(select(Node).where(Node.id.in_(ordered)))}
    return [by_id[nid] for nid in ordered if nid in by_id]
