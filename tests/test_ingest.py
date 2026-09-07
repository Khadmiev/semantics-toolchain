# SPDX-License-Identifier: Apache-2.0
from sqlalchemy import select

from assistant_memory.access import Access
from assistant_memory.ingest import chunk_text, ingest_document
from assistant_memory.models.graph import Edge, Node
from assistant_memory.models.identity import Membership
from assistant_memory.search import search


async def _grant(session, account, space, permission="write"):
    session.add(Membership(account_id=account.id, space_id=space.id, permission=permission))
    await session.flush()


# --- chunking ------------------------------------------------------------


def test_chunk_packs_small_paragraphs_together():
    text = "para one." + "\n\n" + "para two."
    assert chunk_text(text, max_chars=100) == ["para one.\n\npara two."]


def test_chunk_splits_when_over_limit():
    text = "\n\n".join("x" * 30 for _ in range(3))  # three 30-char paragraphs
    chunks = chunk_text(text, max_chars=50)
    assert len(chunks) == 3
    assert all(len(c) <= 50 for c in chunks)


def test_chunk_hard_splits_long_paragraph():
    chunks = chunk_text("y" * 120, max_chars=50)
    assert len(chunks) == 3  # 50 + 50 + 20
    assert all(len(c) <= 50 for c in chunks)


# --- ingestion -----------------------------------------------------------


async def test_ingest_creates_document_and_chunks(session, account, space):
    text = "\n\n".join(f"Paragraph {i} discussing topic {i} in detail." for i in range(6))
    result = await ingest_document(
        session, space_id=space.id, account_id=account.id, source="doc.md", text=text, max_chars=60
    )

    document = await session.get(Node, result["document_id"])
    assert document.type == "Document"
    assert document.properties["chunks"] == result["chunks"] > 1

    # scope to THIS document's chunks via its containment edges (a shared dev DB may
    # hold other documents — never select DocumentChunk unfiltered).
    edges = list(
        await session.scalars(
            select(Edge).where(Edge.type == "contained_in", Edge.dst_node == document.id)
        )
    )
    assert len(edges) == result["chunks"]
    chunks = list(
        await session.scalars(select(Node).where(Node.id.in_([e.src_node for e in edges])))
    )
    assert all(c.type == "DocumentChunk" for c in chunks)
    assert all(c.properties.get("source") == "doc.md" for c in chunks)


async def test_replace_supersedes_prior_document(session, account, space):
    src = "same.md"
    first = await ingest_document(
        session, space_id=space.id, account_id=account.id, source=src,
        text="one\n\ntwo\n\nthree", max_chars=20,
    )
    second = await ingest_document(
        session, space_id=space.id, account_id=account.id, source=src,
        text="brand new content", max_chars=20, replace=True,
    )

    # the old document is soft-deleted; only the new one is active for this source
    active = list(
        await session.scalars(
            select(Node).where(
                Node.type == "Document",
                Node.deleted_at.is_(None),
                Node.properties["source"].astext == src,
            )
        )
    )
    assert [d.id for d in active] == [second["document_id"]]
    old = await session.get(Node, first["document_id"])
    assert old.deleted_at is not None


async def test_ingested_chunks_are_searchable(session, account, space):
    await _grant(session, account, space)
    text = "Alpha intro.\n\nThe quick brown fox jumps over.\n\nOmega outro."
    await ingest_document(
        session, space_id=space.id, account_id=account.id, source="doc.md", text=text, max_chars=44
    )

    results = await search(session, Access(session, account.id), "quick brown fox")
    assert any("quick brown fox" in (n.properties.get("text") or "") for n in results)
