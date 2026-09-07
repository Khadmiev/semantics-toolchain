# SPDX-License-Identifier: Apache-2.0
"""Review-orchestration HTTP API tests (spec §3.2, §8, §16).

Drives the real app in-loop over httpx. The repository logic is unit-tested in
test_review_repository.py; here we verify the HTTP wiring: the two auth surfaces
(bootstrap credential vs per-review token), review isolation, status-code mapping,
and one full end-to-end convergence through the endpoints.
"""

import httpx
import pytest_asyncio

from assistant_memory.auth import issue_credential
from assistant_memory.db import get_session
from assistant_memory.main import app
from assistant_memory.models.identity import Account, User


@pytest_asyncio.fixture
async def api(session):
    """(client, bootstrap_token). ``get_session`` is overridden to the rolled-back
    test session; a real account credential is issued to act as the bootstrap cred."""
    user = User(label="op", is_owner=True)
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label="personal")
    session.add(acc)
    await session.flush()
    bootstrap = (await issue_credential(session, account_id=acc.id, label="bootstrap")).token

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, bootstrap
    finally:
        app.dependency_overrides.pop(get_session, None)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _create(client, bootstrap, slug="demo", mode="spec", config=None):
    """Create a review over the API; pinned PRE-B.7 by default.

    The end-to-end path these tests walk is the old round structure (findings straight to
    dispositions). B.7's round gate changes that sequence and is covered by its own suite;
    pinning here keeps this file testing the transport and the convergence machinery it was
    written for, both of which still serve every pre-deploy review.
    """
    r = await client.post(
        "/reviews",
        headers=_auth(bootstrap),
        json={"slug": slug, "mode": mode, "config": config or {"protocol": "B.6"}},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _post_msg(client, rid, token, role, kind, payload):
    r = await client.post(
        f"/reviews/{rid}/messages", headers=_auth(token),
        json={"role": role, "kind": kind, "payload": payload},
    )
    assert r.status_code == 200, r.text
    return r.json()


# --- auth surfaces (§8) --------------------------------------------------


async def test_create_requires_bootstrap_credential(api):
    client, _ = api
    assert (await client.post("/reviews", json={"slug": "x", "mode": "spec"})).status_code == 401
    r = await client.post("/reviews", headers=_auth("garbage"), json={"slug": "x", "mode": "spec"})
    assert r.status_code == 401


async def test_bad_mode_is_422(api):
    client, bootstrap = api
    r = await client.post("/reviews", headers=_auth(bootstrap), json={"slug": "x", "mode": "prose"})
    assert r.status_code == 422


async def test_per_review_token_isolation(api):
    client, bootstrap = api
    a = await _create(client, bootstrap, slug="a")
    b = await _create(client, bootstrap, slug="b")
    # A's token reaches A ...
    assert (await client.get(f"/reviews/{a['review_id']}", headers=_auth(a["dev_token"]))).status_code == 200
    # ... but not B (403) ...
    assert (await client.get(f"/reviews/{b['review_id']}", headers=_auth(a["dev_token"]))).status_code == 403
    # ... and a missing token is 401.
    assert (await client.get(f"/reviews/{a['review_id']}")).status_code == 401


async def test_console_requires_bootstrap_not_review_token(api):
    client, bootstrap = api
    created = await _create(client, bootstrap, slug="visible")
    listed = await client.get("/reviews", headers=_auth(bootstrap))
    assert listed.status_code == 200
    slugs = [r["slug"] for r in listed.json()["reviews"]]
    assert "visible" in slugs
    # a per-review token is not an account credential -> 401 on the console
    assert (await client.get("/reviews", headers=_auth(created["dev_token"]))).status_code == 401


# --- messages + long-poll cursor (§3.2, §6) ------------------------------


async def test_append_and_poll_cursor(api):
    client, bootstrap = api
    r = await _create(client, bootstrap)
    rid, dev, critic = r["review_id"], r["dev_token"], r["critic_token"]
    m = await _post_msg(client, rid, dev, "development", "artifact", {"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    assert m["seq"] == 1
    # critic polls from 0 -> sees the artifact; from seq 1 with no wait -> empty
    got = await client.get(f"/reviews/{rid}/messages?after=0&wait=0", headers=_auth(critic))
    assert [x["seq"] for x in got.json()["messages"]] == [1]
    empty = await client.get(f"/reviews/{rid}/messages?after=1&wait=0", headers=_auth(critic))
    assert empty.json()["messages"] == []


# --- the server-enforced convergence guard over HTTP (§10, §16) ----------


async def test_converged_without_pickup_is_409(api):
    client, bootstrap = api
    r = await _create(client, bootstrap)
    rid, critic = r["review_id"], r["critic_token"]
    resp = await client.post(
        f"/reviews/{rid}/messages", headers=_auth(critic),
        json={"role": "critic", "kind": "status", "payload": {"value": "converged", "artifact_seq": 1}},
    )
    assert resp.status_code == 409
    assert "critic_reviewing" in resp.json()["detail"]


async def test_illegal_state_transition_is_409(api):
    client, bootstrap = api
    r = await _create(client, bootstrap)
    rid, dev = r["review_id"], r["dev_token"]
    resp = await client.post(f"/reviews/{rid}/state", headers=_auth(dev), json={"target": "finalized"})
    assert resp.status_code == 409


async def test_full_convergence_end_to_end(api):
    """create -> artifact -> finding -> fix -> new artifact -> clean pass -> converged (§16)."""
    client, bootstrap = api
    r = await _create(client, bootstrap)
    rid, dev, critic = r["review_id"], r["dev_token"], r["critic_token"]

    await _post_msg(client, rid, dev, "development", "artifact", {"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    assert (await client.post(f"/reviews/{rid}/state", headers=_auth(critic),
                              json={"target": "critic_reviewing"})).status_code == 200
    await _post_msg(client, rid, critic, "critic", "findings",
                    {"artifact_seq": 1, "items": [{"id": "f1", "severity": "minor"}]})
    await _post_msg(client, rid, critic, "critic", "status",
                    {"value": "needs_iteration", "artifact_seq": 1})
    await _post_msg(client, rid, dev, "development", "disposition",
                    {"finding_id": "f1", "artifact_seq": 1, "outcome": "fixed", "reason": "done"})
    v2 = await _post_msg(client, rid, dev, "development", "artifact", {"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await client.post(f"/reviews/{rid}/state", headers=_auth(critic), json={"target": "critic_reviewing"})
    await _post_msg(client, rid, critic, "critic", "findings", {"artifact_seq": v2["seq"], "items": []})
    conv = await _post_msg(client, rid, critic, "critic", "status",
                           {"value": "converged", "artifact_seq": v2["seq"]})
    assert conv["kind"] == "status"

    snap = await client.get(f"/reviews/{rid}", headers=_auth(dev))
    assert snap.json()["state"] == "converged"

    # on-demand readable timeline rendered from the store (§9.1) — no parallel log
    tl = await client.get(f"/reviews/{rid}/timeline", headers=_auth(dev))
    assert tl.status_code == 200
    assert "clean pass — 0 findings" in tl.text
    assert "status=converged" in tl.text


async def test_malformed_disposition_is_422(api):
    """Wire-contract guard (feedback 4a83064b): the hpo-incident payload shape
    (`disposition:` key instead of `outcome`) is refused at POST time to its author."""
    client, bootstrap = api
    created = await _create(client, bootstrap)
    rid, dev = created["review_id"], created["dev_token"]
    r = await client.post(
        f"/reviews/{rid}/messages", headers=_auth(dev),
        json={"role": "development", "kind": "disposition",
              "payload": {"finding_id": "f1", "disposition": "fixed"}},
    )
    assert r.status_code == 422
    assert "outcome" in r.json()["detail"]
