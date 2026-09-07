# SPDX-License-Identifier: Apache-2.0
"""Write-side indexing: fill a node's current version with embedding + tsvector.

Called by the write tools after a mutation. We only ever index the *current*
version, so a new version supersedes the old in search automatically (the stale
version row keeps its vector but is never queried). FTS is RU+EN: two configs
concatenated, which stems both languages without picking one.

Full-text (`search_tsv`) is deliberately **decoupled** from embedding generation:
``index_fts`` populates tsvectors with **no model**, so the startup bootstrap
(IR-1) can make seeded content searchable + visible without loading bge-m3;
``backfill_embeddings`` fills the (slow, model-bound) vectors afterwards. The
combined ``index_node`` remains the one-shot path used by the write tools.

Flushes but does not commit — the caller owns the transaction.
"""

import asyncio
import json
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import Node, NodeVersion
from .embedder import Embedder


def _node_text(node: Node) -> str:
    return _compose_text(node.label, node.properties)


def _compose_text(label: str | None, properties: dict | None) -> str:
    parts = [label or ""]
    if properties:
        parts.append(json.dumps(properties, ensure_ascii=False, sort_keys=True))
    return " ".join(p for p in parts if p).strip()


def _tsvector(text: str):
    """RU+EN tsvector expression (stems both without picking a language)."""
    return func.to_tsvector("russian", text).op("||")(func.to_tsvector("english", text))


async def index_fts(session: AsyncSession, node: Node) -> None:
    """Populate the current version's `search_tsv` only — NO model, no embedding.

    Makes a node findable by full-text (and, with membership, visible) immediately.
    The embedding is filled later by ``backfill_embeddings``.
    """
    if node.current_version_id is None:
        return
    tsv = _tsvector(_node_text(node))
    await session.execute(
        update(NodeVersion).where(NodeVersion.id == node.current_version_id).values(search_tsv=tsv)
    )
    await session.flush()


async def index_node(session: AsyncSession, node: Node, embedder: Embedder) -> None:
    """Embed + tsvector the node's current version. No-op if it has no version."""
    if node.current_version_id is None:
        return
    text = _node_text(node)
    # model inference is CPU-bound and blocking — off the event loop, or every
    # concurrent request stalls for the inference duration (prod: bge-m3)
    vector = (await asyncio.to_thread(embedder.embed, [text]))[0]
    await session.execute(
        update(NodeVersion)
        .where(NodeVersion.id == node.current_version_id)
        .values(embedding=vector, search_tsv=_tsvector(text))
    )
    await session.flush()


async def pending_embeddings(
    session: AsyncSession, *, limit: int = 256
) -> list[tuple[uuid.UUID, str]]:
    """(current_version_id, text) for non-deleted nodes whose current version lacks an embedding.

    The read half of a non-blocking backfill: the caller embeds the texts off the
    event loop, then hands the vectors to ``write_embeddings``.
    """
    rows = await session.execute(
        select(Node.current_version_id, Node.label, NodeVersion.properties)
        .join(NodeVersion, NodeVersion.id == Node.current_version_id)
        .where(NodeVersion.embedding.is_(None), Node.deleted_at.is_(None))
        .limit(limit)
    )
    return [(vid, _compose_text(label, props)) for vid, label, props in rows]


async def write_embeddings(
    session: AsyncSession, items: list[tuple[uuid.UUID, list[float]]]
) -> None:
    """Write pre-computed vectors onto their version rows (the write half of backfill)."""
    for version_id, vector in items:
        await session.execute(
            update(NodeVersion).where(NodeVersion.id == version_id).values(embedding=vector)
        )
    await session.flush()
