# SPDX-License-Identifier: Apache-2.0
"""Text embedders. The interface is tiny; the model behind it is swappable.

Privacy (plan.md): the production embedder is an *offline* multilingual model
(bge-m3) run on the VPS, so personal text never leaves the box. Tests and dev use
``DeterministicEmbedder`` — a hashing bag-of-words vector that needs no heavy deps
yet still places texts sharing tokens closer, so ranking is meaningful.
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from ..config import settings

_TOKEN = re.compile(r"\w+", re.UNICODE)


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Map each text to a unit-norm vector of length ``dim``."""
        ...


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class DeterministicEmbedder:
    """Hashing embedder: stable, fast, dependency-free. Shared tokens -> closer.

    Each token is hashed to a dimension with a stable sign; the vector is then
    L2-normalized. Cosine similarity therefore grows with token overlap — enough
    for tests and local dev without a real model.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokenize(text):
            digest = hashlib.sha1(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]


class SentenceTransformerEmbedder:
    """The real offline model (bge-m3 by default). Heavy deps are imported lazily.

    Requires ``sentence-transformers`` (and torch), installed only where the model
    actually runs (the VPS) — not a core dependency.
    """

    def __init__(self, model_name: str, dim: int) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised only on the VPS
            raise RuntimeError(
                "embedding_provider='sentence_transformers' needs the optional "
                "'sentence-transformers' package installed"
            ) from exc
        self._model = SentenceTransformer(model_name)
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - VPS only
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """The process-wide embedder selected by config."""
    if settings.embedding_provider == "sentence_transformers":
        return SentenceTransformerEmbedder(settings.embedding_model, settings.embedding_dim)
    return DeterministicEmbedder(settings.embedding_dim)
