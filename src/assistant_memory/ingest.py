# SPDX-License-Identifier: Apache-2.0
"""Document ingestion: a text file/string -> a Document node + DocumentChunk nodes.

A server-side bulk job (not an MCP tool — the LLM adds memories one at a time; this
chunks and embeds whole documents). Each chunk becomes its own node so it gets an
embedding and is retrievable on its own; chunks are `contained_in` the Document so
the graph stitches them back together. Goes through the repository (versioned,
access-scoped) and the B6 indexer (vector + FTS), like any other write.

Flushes/commits are the caller's responsibility — functions here flush only.
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models.graph import Edge, Node
from .repository import graph as repo
from .search import get_embedder, index_node
from .search.embedder import Embedder

DEFAULT_MAX_CHARS = 1200


def chunk_text(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split text into chunks on blank-line paragraph boundaries.

    Paragraphs are greedily packed up to ``max_chars``; a single paragraph longer
    than that is hard-split. Keeps chunks aligned to natural breaks where possible.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = ""
        if len(para) > max_chars:
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


async def _supersede_existing(session: AsyncSession, source: str) -> int:
    """Soft-delete any active Document with this source (and its chunks). Reversible."""
    documents = list(
        await session.scalars(
            select(Node).where(
                Node.type == "Document",
                Node.deleted_at.is_(None),
                Node.properties["source"].astext == source,
            )
        )
    )
    now = datetime.now(UTC)
    for document in documents:
        chunk_ids = await session.scalars(
            select(Edge.src_node).where(
                Edge.type == "contained_in",
                Edge.dst_node == document.id,
                Edge.valid_to.is_(None),
            )
        )
        for chunk_id in chunk_ids:
            chunk = await session.get(Node, chunk_id)
            if chunk is not None and chunk.deleted_at is None:
                chunk.deleted_at = now
        document.deleted_at = now
    await session.flush()
    return len(documents)


async def ingest_document(
    session: AsyncSession,
    *,
    space_id: uuid.UUID,
    account_id: uuid.UUID,
    source: str,
    text: str,
    title: str | None = None,
    embedder: Embedder | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    replace: bool = False,
) -> dict:
    """Create a Document node and one DocumentChunk per chunk, embedded + linked.

    Returns ``{"document_id", "chunks"}``. ``source`` is recorded as the version
    ``source_ref`` (provenance) on every node it produces. With ``replace=True`` any
    prior Document with the same ``source`` is superseded (soft-deleted) first, so
    re-ingesting a file is idempotent rather than duplicating it.
    """
    embedder = embedder or get_embedder()
    title = title or source
    if replace:
        await _supersede_existing(session, source)
    chunks = chunk_text(text, max_chars=max_chars)

    document = await repo.create_node(
        session,
        type="Document",
        space_id=space_id,
        account_id=account_id,
        label=title,
        properties={"source": source, "title": title, "chunks": len(chunks)},
        source_ref=source,
    )
    await index_node(session, document, embedder)

    for index, body in enumerate(chunks):
        chunk = await repo.create_node(
            session,
            type="DocumentChunk",
            space_id=space_id,
            account_id=account_id,
            label=f"{title} #{index}",
            properties={"text": body, "index": index, "source": source},
            source_ref=source,
        )
        await index_node(session, chunk, embedder)
        await repo.link(
            session,
            type="contained_in",
            src_node=chunk.id,
            dst_node=document.id,
            account_id=account_id,
        )

    return {"document_id": document.id, "chunks": len(chunks)}
