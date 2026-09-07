# SPDX-License-Identifier: Apache-2.0
"""Search (B6): offline embeddings + RU/EN full-text, fused and access-filtered.

- ``embedder`` — the pluggable text->vector interface plus a dependency-free
  deterministic stand-in (so tests need no PyTorch) and the real offline model.
- ``indexer`` — fills a node's *current* version row with embedding + tsvector.
- ``query`` — hybrid retrieval (vector + FTS via reciprocal-rank fusion), scoped
  to what the principal can see.
"""

from .embedder import Embedder, get_embedder
from .indexer import index_fts, index_node, pending_embeddings, write_embeddings
from .query import search

__all__ = [
    "Embedder",
    "get_embedder",
    "index_fts",
    "index_node",
    "pending_embeddings",
    "search",
    "write_embeddings",
]
