# SPDX-License-Identifier: Apache-2.0
from fastapi.testclient import TestClient

from assistant_memory.main import _warm_embedder, app


def test_health_liveness() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_embedder_warmup_runs() -> None:
    # Best-effort warmup should complete without raising (deterministic embedder in tests).
    await _warm_embedder()
