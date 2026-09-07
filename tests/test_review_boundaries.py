# SPDX-License-Identifier: Apache-2.0
"""B.9 B-5: the threat-boundary registry and the mechanical waive route, plus E-1 —
the owner-credential read path for closed reviews.

Spec docs/design/2026-08-16_review_loop_next_spec.md, Part B (B-5) and Part E (E-1).
Two halves, matching the round-gate suite's split: the PURE core (a waive whose
server-stamped `boundary_ref_resolved` marker rides instead of parking) and the SERVER
(the registry's two write paths, the resolution stamp at POST, and the read-path
authorization change).
"""

import httpx
import pytest
import pytest_asyncio

from assistant_memory.auth import issue_credential
from assistant_memory.db import get_session
from assistant_memory.main import app
from assistant_memory.models.identity import Account, User
from assistant_memory.review import round_gate as rg
from assistant_memory.review.errors import InvalidMessagePayloadError
from assistant_memory.review.repository import (
    add_deployment_boundary,
    append_message,
    create_review,
    get_messages,
    list_boundaries,
    revoke_review_tokens,
)
from tests.instrument_helpers import seed_instruments

# --- shared builders (mirroring test_review_round_gate) -------------------


class Log:
    def __init__(self):
        self.messages: list[dict] = []

    def add(self, kind, payload=None, role="development") -> int:
        seq = len(self.messages) + 1
        self.messages.append(
            {"seq": seq, "role": role, "kind": kind, "payload": payload or {}}
        )
        return seq

    def state(self) -> rg.GateState:
        return rg.compute(self.messages)


def _finding(fid, ftype="correctness"):
    return {"id": fid, "finding_type": ftype, "title": fid}


def _entry(fid, outcome="fix", **extra):
    entry = {
        "finding_id": fid,
        "context": f"the mechanism {fid} concerns, in one sentence",
        "proposed_outcome": outcome,
        "class_closure_claim": {"kind": "cell", "scope": "this one call site"},
        "reason": "because",
    }
    if outcome == "fix":
        entry["plan"] = "do the thing"
    if outcome == "escalate":
        entry["recommendation"] = "operator picks"
        entry["recommended_outcome"] = "fix"
    return {**entry, **extra}


async def _b9_review(session, mode="spec", slug="b5"):
    """B.11 A-1: these reviews carry no coverage denominator, and now say so.

    The setting exists precisely so that "no manifest anywhere" stops being read as "this
    review never had coverage" — a premise that is false for the first version of EVERY
    coverage-bearing review. These tests are about the threat-boundary registry and post
    no manifest at all, so the absence is declared rather than inferred.
    """
    return await create_review(
        session, slug=slug, mode=mode, instrument=await seed_instruments(session),
        config={"coverage": {"in_play": False}},
    )


async def _post(session, review_id, role, kind, payload):
    return await append_message(
        session, review_id=review_id, role=role, kind=kind, payload=payload
    )


async def _open_round(session, rid, *findings):
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# s\n\n### E-1 — x\n"},
    })
    await _post(session, rid, "critic", "findings", {"artifact_seq": 1, "items": list(findings)})


# --- the pure core: the mechanical route ----------------------------------


def test_stamped_boundary_waive_rides_instead_of_parking():
    """B-5 rule 3: a waive whose ref the SERVER resolved is a mechanical entry — the
    boundary is already the operator's recorded answer, so the round does not park on
    re-asking it. This holds even for the security_mechanism type, which is exactly the
    class the four measured forks came from."""
    log = Log()
    log.add("artifact", {"mode": "spec"})
    log.add(
        "findings",
        {"artifact_seq": 1, "items": [_finding("s1", "security_mechanism")]},
        role="critic",
    )
    log.add(
        "proposals",
        {"entries": [_entry(
            "s1", "waive",
            adversary="none in the declared model",
            false_positive_cost="n/a",
            boundary_ref="00000000-0000-4000-8000-000000000001",
            boundary_ref_resolved=True,
        )]},
    )
    state = log.state()
    assert not state.gate_open
    assert state.round.entries["s1"].settled_outcome == "waive"


def test_unstamped_boundary_waive_waits_exactly_as_before():
    """A waive proposing a boundary NOT yet recorded (no resolving ref, hence no server
    stamp) always waits — a new boundary is genuinely the operator's to grant."""
    log = Log()
    log.add("artifact", {"mode": "spec"})
    log.add("findings", {"artifact_seq": 1, "items": [_finding("f1")]}, role="critic")
    log.add(
        "proposals",
        {"entries": [_entry("f1", "waive", boundary_ref="not-recorded-anywhere")]},
    )
    assert log.state().gate_open


def test_all_wait_outranks_the_mechanical_route():
    """The operator's switch stays supreme: in `all_wait` every entry waits, stamped
    boundary or not."""
    log = Log()
    log.add("notice", {"phase": "gate_mode", "mode": "all_wait", "granted_by": "operator"})
    log.add("artifact", {"mode": "spec"})
    log.add("findings", {"artifact_seq": 2, "items": [_finding("f1")]}, role="critic")
    log.add(
        "proposals",
        {"entries": [_entry(
            "f1", "waive",
            boundary_ref="00000000-0000-4000-8000-000000000001",
            boundary_ref_resolved=True,
        )]},
    )
    assert log.state().gate_open


# --- the server: registry write paths + the resolution stamp --------------


async def test_deployment_boundary_makes_the_waive_mechanical(session):
    """The full B-5 loop: a recorded deployment boundary + a waive citing it → the
    server stamps resolution into the STORED payload and the gate does not park."""
    boundary = await add_deployment_boundary(
        session, text="the machine is single-user; a second OS user is out of model"
    )
    issued = await _b9_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {
        "entries": [_entry("f1", "waive", boundary_ref=str(boundary.id))],
    })
    stored = (await get_messages(session, rid, after=0))[-1]
    assert stored.payload["entries"][0]["boundary_ref_resolved"] is True
    state = rg.compute(
        [{"seq": m.seq, "role": m.role, "kind": m.kind, "payload": m.payload or {}}
         for m in await get_messages(session, rid, after=0)]
    )
    assert not state.gate_open


async def test_unresolved_ref_gets_no_stamp_and_waits(session):
    issued = await _b9_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {
        "entries": [_entry("f1", "waive", boundary_ref="11111111-2222-4333-8444-555555555555")],
    })
    stored = (await get_messages(session, rid, after=0))[-1]
    assert "boundary_ref_resolved" not in stored.payload["entries"][0]


async def test_foreign_review_boundary_is_not_eligible(session):
    """A review-scoped entry minted in review A is NOT eligible in review B — the
    standing frame is deployment entries plus THIS review's own (B-5 scope rule)."""
    a = await _b9_review(session, slug="a")
    b = await create_review(
        session, slug="b", mode="spec", instrument=await seed_instruments(session),
        config={"coverage": {"in_play": False}},
    )
    minted = await _post(session, a.review.id, "operator", "notice", {
        "phase": "operator_ruling",
        "records_boundary": {"text": "локальная среда — противник вне модели"},
    })
    boundary_id = minted.payload["records_boundary"]["boundary_id"]
    await _open_round(session, b.review.id, _finding("f1"))
    await _post(session, b.review.id, "development", "proposals", {
        "entries": [_entry("f1", "waive", boundary_ref=boundary_id)],
    })
    stored = (await get_messages(session, b.review.id, after=0))[-1]
    assert "boundary_ref_resolved" not in stored.payload["entries"][0]
    # ...while in its OWN review the same ref is eligible.
    eligible = await list_boundaries(session, review_id=a.review.id)
    assert boundary_id in {str(x.id) for x in eligible}


async def test_records_boundary_mints_and_returns_the_id(session):
    issued = await _b9_review(session)
    msg = await _post(session, issued.review.id, "operator", "notice", {
        "phase": "operator_ruling",
        "records_boundary": {"text": "option X: the direct-post door is recorded"},
    })
    boundary_id = msg.payload["records_boundary"]["boundary_id"]
    rows = await list_boundaries(session, review_id=issued.review.id)
    match = [b for b in rows if str(b.id) == boundary_id]
    assert match and match[0].scope == "review" and match[0].source == "review_ruling"


async def test_records_boundary_is_the_operators_marker(session):
    issued = await _b9_review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "development", "notice", {
            "records_boundary": {"text": "self-granted boundary"},
        })
    assert "OPERATOR" in e.value.reason


async def test_records_boundary_refuses_a_preset_id(session):
    issued = await _b9_review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "operator", "notice", {
            "records_boundary": {"text": "x", "boundary_id": "chosen-by-client"},
        })
    assert "receipt" in e.value.reason


async def test_client_set_resolution_marker_is_refused(session):
    issued = await _b9_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "proposals", {
            "entries": [_entry(
                "f1", "waive",
                boundary_ref="00000000-0000-4000-8000-000000000001",
                boundary_ref_resolved=True,
            )],
        })
    assert "minted by the server" in e.value.reason


async def test_boundary_ref_belongs_to_a_waive(session):
    issued = await _b9_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "proposals", {
            "entries": [_entry("f1", "fix", boundary_ref="whatever")],
        })
    assert "waive" in e.value.reason


# --- E-1: the owner-credential read path ----------------------------------


@pytest_asyncio.fixture
async def api(session):
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


async def test_closed_review_stays_readable_with_bootstrap(api, session):
    """E-1: finalization revokes every per-review token (correct — a closed review has
    no live participants), and the journal stays REACHABLE through the deployment
    owner's ordinary credential. Durable-but-unreachable was the disease."""
    client, bootstrap = api
    issued = await create_review(
        session, slug="closing", mode="spec", instrument=await seed_instruments(session),
        config={"coverage": {"in_play": False}},
    )
    rid = str(issued.review.id)
    await append_message(
        session, review_id=issued.review.id, role="development", kind="notice",
        payload={"text": "the one message"},
    )
    await revoke_review_tokens(session, issued.review.id)

    # The revoked token is dead on every surface...
    assert (await client.get(f"/reviews/{rid}/messages", headers=_auth(issued.dev_token))).status_code == 401
    # ...while the bootstrap credential reads everything E-1 names:
    for path in (f"/reviews/{rid}/messages", f"/reviews/{rid}/timeline",
                 f"/reviews/{rid}/service_tail", f"/reviews/{rid}/metrics",
                 f"/reviews/{rid}"):
        r = await client.get(path, headers=_auth(bootstrap))
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"


async def test_write_endpoints_do_not_take_the_bootstrap_path(api, session):
    """E-1 changes READS only: a closed review accepts no writes, and even on a live one
    the message POST authorizes by per-review token, never by account credential."""
    client, bootstrap = api
    issued = await create_review(
        session, slug="writes", mode="spec", instrument=await seed_instruments(session)
    )
    r = await client.post(
        f"/reviews/{issued.review.id}/messages", headers=_auth(bootstrap),
        json={"role": "development", "kind": "notice", "payload": {"text": "x"}},
    )
    assert r.status_code == 401


async def test_garbage_credential_is_still_refused_on_reads(api, session):
    client, _ = api
    issued = await create_review(
        session, slug="garbage", mode="spec", instrument=await seed_instruments(session)
    )
    r = await client.get(f"/reviews/{issued.review.id}/messages", headers=_auth("nonsense"))
    assert r.status_code == 401


async def test_boundary_management_endpoints(api, session):
    """The management write path over HTTP: POST records the operator's words at
    deployment scope; GET lists the registry; a per-review token reaches neither."""
    client, bootstrap = api
    r = await client.post(
        "/reviews/boundaries", headers=_auth(bootstrap),
        json={"text": "single-user machine; foreign-credential adversary out of model"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "deployment"
    listed = await client.get("/reviews/boundaries", headers=_auth(bootstrap))
    assert listed.status_code == 200
    assert any(
        b["id"] == r.json()["id"] for b in listed.json()["boundaries"]
    )
    issued = await create_review(
        session, slug="tok", mode="spec", instrument=await seed_instruments(session)
    )
    assert (
        await client.get("/reviews/boundaries", headers=_auth(issued.dev_token))
    ).status_code == 401


async def test_threat_frame_usage_read(api, session):
    """The watcher-facing usage read: deployment entries plus THIS review's own, under
    the review token it already holds."""
    client, bootstrap = api
    deployment = await add_deployment_boundary(session, text="deployment-wide boundary")
    issued = await create_review(
        session, slug="frame", mode="spec", instrument=await seed_instruments(session)
    )
    minted = await append_message(
        session, review_id=issued.review.id, role="operator", kind="notice",
        payload={"phase": "operator_ruling", "records_boundary": {"text": "in-review ruling"}},
    )
    r = await client.get(
        f"/reviews/{issued.review.id}/threat-frame", headers=_auth(issued.dev_token)
    )
    assert r.status_code == 200
    ids = {b["id"] for b in r.json()["boundaries"]}
    assert str(deployment.id) in ids
    assert minted.payload["records_boundary"]["boundary_id"] in ids
    # No restatement notice yet: the positive half falls back to the CREATION snapshot
    # (b9-threat-context-not-a-freshness-input: the context is a creation input).
    assert r.json()["threat_model"] == "test deployment: single user, no foreign input"
    assert r.json()["operating_scale"] == "test scale: one channel, one reader"


async def test_creation_without_threat_context_is_refused(session):
    """b9-threat-context-not-a-freshness-input, seam 1: the operator-confirmed context
    is a CREATION input — a B.9 review cannot exist without a declared frame."""
    from assistant_memory.review.errors import ReviewCreationRefusedError

    block = await seed_instruments(session)
    block.pop("threat_context")
    with pytest.raises(ReviewCreationRefusedError) as e:
        await create_review(session, slug="noctx", mode="spec", instrument=block)
    assert "threat_context" in str(e.value)


async def test_threat_frame_carries_the_positive_context_latest_binds(api, session):
    """B-5 positive half: the operator-confirmed threat model and scale ride validated
    `threat_context` notices; the LATEST record binds (the operator may re-state
    mid-review), and the critic may not author its own frame."""
    from assistant_memory.review.errors import InvalidMessagePayloadError

    client, _ = api
    issued = await create_review(
        session, slug="frame2", mode="spec", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    ctx = {"phase": "threat_context", "threat_model": "personal box, leak via artifacts",
           "operating_scale": "thousands of nodes", "granted_by": "operator, pre-gate"}
    await append_message(session, review_id=rid, role="development", kind="notice", payload=ctx)
    r = await client.get(f"/reviews/{rid}/threat-frame", headers=_auth(issued.dev_token))
    assert r.json()["threat_model"] == "personal box, leak via artifacts"
    # latest binds — a re-statement supersedes in the served frame
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={**ctx, "threat_model": "CORRECTED model", "granted_by": "operator, mid-review"},
    )
    r2 = await client.get(f"/reviews/{rid}/threat-frame", headers=_auth(issued.dev_token))
    assert r2.json()["threat_model"] == "CORRECTED model"
    assert r2.json()["granted_by"] == "operator, mid-review"
    # the server-issued context identity is the binding record's seq — pass evidence
    # stamps it, and 0 means "the creation snapshot binds"
    assert r2.json()["context_seq"] > r.json()["context_seq"] > 0
    # the critic may not author the frame; incomplete records are refused
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(session, review_id=rid, role="critic", kind="notice", payload=ctx)
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="development", kind="notice",
            payload={"phase": "threat_context", "threat_model": "m"},
        )
