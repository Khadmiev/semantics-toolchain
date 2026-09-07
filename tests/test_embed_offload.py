# SPDX-License-Identifier: Apache-2.0
"""Regression: blocking embedding inference must run OFF the event loop.

In production the embedder is SentenceTransformer bge-m3 — a CPU-bound blocking
call. The write path (index_node) and the query path (search) run inside async
request handlers; a synchronous embed() there freezes the entire uvicorn loop
for the inference duration. Invisible with the fast DeterministicEmbedder the
other tests pin, so these tests use a deliberately SLOW embedder and assert the
loop keeps making progress while it grinds.
"""

import asyncio
import time
import uuid

from assistant_memory.access import Access
from assistant_memory.auth import issue_credential, resolve_credential
from assistant_memory.mcp import tools
from assistant_memory.models.graph import Node
from assistant_memory.models.identity import Membership
from assistant_memory.search.embedder import DeterministicEmbedder
from assistant_memory.search.indexer import index_node
from assistant_memory.search.query import search

_EMBED_SECONDS = 0.3
_TICK = 0.01


class BlockingEmbedder:
    """Simulates production model inference: correct vectors, but blocking."""

    def __init__(self) -> None:
        self._inner = DeterministicEmbedder(1024)
        self.dim = self._inner.dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        time.sleep(_EMBED_SECONDS)  # stand-in for CPU-bound inference
        return self._inner.embed(texts)


async def _ticker(counter: dict) -> None:
    while True:
        counter["ticks"] += 1
        await asyncio.sleep(_TICK)


async def _loop_progress_during(coro) -> int:
    """Run ``coro`` with a concurrent ticker; return ticks made while it ran."""
    counter = {"ticks": 0}
    ticker = asyncio.ensure_future(_ticker(counter))
    try:
        await coro
    finally:
        ticker.cancel()
    return counter["ticks"]


async def _seeded_node(session, account, space) -> tuple[Node, object]:
    session.add(Membership(account_id=account.id, space_id=space.id, permission="write"))
    await session.flush()
    issued = await issue_credential(session, account_id=account.id)
    principal = await resolve_credential(session, issued.token)
    created = await tools.create_node(
        session, principal, type="Note", space=space.id, label="loop responsiveness probe"
    )
    node = await session.get(Node, uuid.UUID(created["node"]["id"]))
    return node, principal


async def test_index_node_does_not_block_event_loop(session, account, space):
    node, _ = await _seeded_node(session, account, space)
    ticks = await _loop_progress_during(index_node(session, node, BlockingEmbedder()))
    # a blocked loop yields ~0 ticks; an offloaded embed leaves room for ~30
    assert ticks >= 10


async def test_search_does_not_block_event_loop(session, account, space):
    _, principal = await _seeded_node(session, account, space)
    access = Access(session, principal.account_id, scopes=None)
    ticks = await _loop_progress_during(
        search(session, access, "loop responsiveness probe", embedder=BlockingEmbedder())
    )
    assert ticks >= 10
