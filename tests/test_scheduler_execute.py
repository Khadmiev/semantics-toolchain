# SPDX-License-Identifier: Apache-2.0
"""Execute pipeline (slice 3): GATHER → REASON (stub) → validate plan → ACT within the fence."""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from assistant_memory.models.graph import Node, NodeType, NodeVersion
from assistant_memory.repository import graph
from assistant_memory.scheduler.job import iso
from assistant_memory.scheduler.plan import PlanError, canonical_actions, validate_final_plan
from assistant_memory.scheduler.reasoning import StubReasoner
from assistant_memory.scheduler.runner import SchedulerRunner


@pytest_asyncio.fixture
async def types(session):
    for t in ("ScheduledJob", "Delivery"):
        if await session.get(NodeType, t) is None:  # may already be seeded in the DB
            session.add(NodeType(type=t, default_sensitivity="normal"))
    await session.flush()


async def _job(session, account, space, *, writes=None, delivery=None, agency=None):
    props = {
        "enabled": True,
        "trigger": {"kind": "interval", "spec": {"seconds": 3600}},
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    if writes is not None:
        props["writes"] = writes
    if delivery is not None:
        props["delivery"] = delivery
    if agency is not None:
        props["agency"] = agency
    return await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )


async def _job_deliveries(session, job_id):
    """Delivery nodes for one job's occurrences (key = '{job_id}:{iso}:{index}'), so assertions
    are scoped to the test's own job rather than the whole (possibly polluted) Delivery table."""
    return list(
        await session.scalars(
            select(Node).where(
                Node.type == "Delivery", Node.properties["key"].astext.like(f"{job_id}:%")
            )
        )
    )


def test_validate_and_canonical_order():
    with pytest.raises(PlanError):
        validate_final_plan({"type": "final_plan", "act": True})
    with pytest.raises(PlanError):
        validate_final_plan({"type": "read_request", "reads": []})
    with pytest.raises(PlanError):
        validate_final_plan({"type": "final_plan", "writes": [{"op": "share", "type": "Note"}]})
    with pytest.raises(PlanError):
        validate_final_plan({"type": "final_plan", "bogus": 1})

    plan = validate_final_plan(
        {
            "type": "final_plan",
            "deliver": {"text": "a"},
            "writes": [{"op": "create", "type": "Note"}],
            "elicit": {"target_field": "x", "question": "q"},
        }
    )
    actions = canonical_actions(plan, "occ")
    assert [a.kind for a in actions] == ["deliver", "write", "elicit"]
    assert actions[0].key == "occ:0"
    assert actions[2].key == "occ:2"


async def test_execute_deliver_and_write(session, account, space, types):
    plan = {
        "type": "final_plan",
        "deliver": {"text": "morning!"},
        "writes": [{"op": "create", "type": "Note", "properties": {"text": "slice3-note-marker"}}],
        "rationale_summary": "ok",
    }
    job = await _job(
        session, account, space, writes=["Note"], delivery={"channel": "bot", "target": "op"}
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    assert await runner.tick(session) == 1

    # scope to THIS job's occurrence (the DB may hold other committed Delivery nodes)
    deliveries = list(
        await session.scalars(
            select(Node).where(
                Node.type == "Delivery", Node.properties["key"].astext.like(f"{job.id}:%")
            )
        )
    )
    assert len(deliveries) == 1
    d = deliveries[0].properties
    assert d["text"] == "morning!"
    assert d["status"] == "pending"
    assert d["target"] == "op"
    assert d["key"].endswith(":0")

    notes = list(
        await session.scalars(
            select(Node).where(Node.properties["text"].astext == "slice3-note-marker")
        )
    )
    assert len(notes) == 1

    journal = (await graph.get_node(session, job.id)).properties["last_plan"]
    assert [a["kind"] for a in journal["actions"]] == ["deliver", "write"]


async def test_execute_fence_rejects_out_of_scope_write(session, account, space, types):
    plan = {
        "type": "final_plan",
        "writes": [{"op": "create", "type": "Decision", "properties": {"_fence_probe": "p"}}],
    }
    job = await _job(session, account, space, writes=["Note"])  # Decision not in the fence
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    await runner.tick(session)
    # fence violation → nothing created (no partial), but the job still advanced,
    # and the failed occurrence is durably journaled (F2, review dc694faf)
    probes = await session.scalars(
        select(Node).where(Node.properties["_fence_probe"].astext == "p")
    )
    assert list(probes) == []
    journal = (await graph.get_node(session, job.id)).properties["last_plan"]
    assert journal["error"]["stage"] == "fence"


def test_validate_update_write_requires_node_id_and_patch():
    with pytest.raises(PlanError):  # no node_id
        validate_final_plan({"type": "final_plan", "writes": [{"op": "update", "type": "Note"}]})
    with pytest.raises(PlanError):  # no patch
        validate_final_plan(
            {"type": "final_plan", "writes": [{"op": "update", "type": "Note", "node_id": "x"}]}
        )
    ok = validate_final_plan(
        {
            "type": "final_plan",
            "writes": [{"op": "update", "type": "Note", "node_id": "x", "patch": {"a": 1}}],
        }
    )
    assert ok["writes"][0]["op"] == "update"


async def test_execute_update_within_fence(session, account, space, types):
    target = await graph.create_node(
        session, type="Note", space_id=space.id, account_id=account.id,
        properties={"text": "before", "marker": "upd-marker"},
    )
    plan = {
        "type": "final_plan",
        "writes": [
            {"op": "update", "type": "Note", "node_id": str(target.id), "patch": {"text": "after"}}
        ],
    }
    await _job(session, account, space, writes=["Note"])
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    assert await runner.tick(session) == 1
    assert (await graph.get_node(session, target.id)).properties["text"] == "after"


async def test_execute_update_out_of_space_is_fenced(session, account, space, space2, types):
    foreign = await graph.create_node(
        session, type="Note", space_id=space2.id, account_id=account.id,
        properties={"text": "x", "marker": "foreign-upd"},
    )
    plan = {
        "type": "final_plan",
        "writes": [
            {"op": "update", "type": "Note", "node_id": str(foreign.id), "patch": {"text": "hax"}}
        ],
    }
    # the job writes into `space`, not space2 where the foreign node lives
    job = await _job(session, account, space, writes=["Note"])
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    await runner.tick(session)
    # fenced: the foreign node is untouched; the failure is journaled durably
    assert (await graph.get_node(session, foreign.id)).properties["text"] == "x"
    journal = (await graph.get_node(session, job.id)).properties["last_plan"]
    assert journal["error"]["stage"] == "fence"


def test_validate_writes_fail_closed_on_malformed_payloads():
    # F18: falsy non-list writes must not become a successful no-op
    for bad_writes in ({}, "", 0):
        with pytest.raises(PlanError, match="writes must be a list"):
            validate_final_plan({"type": "final_plan", "writes": bad_writes})
    with pytest.raises(PlanError, match="non-empty string"):
        validate_final_plan({"type": "final_plan", "writes": [{"op": "create", "type": 123}]})
    with pytest.raises(PlanError, match="properties"):
        validate_final_plan(
            {"type": "final_plan",
             "writes": [{"op": "create", "type": "Note", "properties": "oops"}]}
        )
    with pytest.raises(PlanError, match="label"):
        validate_final_plan(
            {"type": "final_plan", "writes": [{"op": "create", "type": "Note", "label": 7}]}
        )
    with pytest.raises(PlanError, match="node_id"):
        validate_final_plan(
            {"type": "final_plan",
             "writes": [{"op": "update", "type": "Note", "node_id": 9, "patch": {}}]}
        )


def test_validate_write_keys_default_and_rationale_types():
    # OQ10 F5-F7: write-object key whitelists; elicit.default + rationale_summary typed
    with pytest.raises(PlanError, match="unknown keys"):
        validate_final_plan(
            {"type": "final_plan",
             "writes": [{"op": "create", "type": "Note", "share_with": "world"}]}
        )
    with pytest.raises(PlanError, match="unknown keys"):
        validate_final_plan(
            {"type": "final_plan",
             "writes": [{"op": "update", "type": "Note", "node_id": "x", "patch": {},
                         "properties": {}}]}
        )
    with pytest.raises(PlanError, match="default"):
        validate_final_plan(
            {"type": "final_plan", "elicit": {"target_field": "f", "default": 5}}
        )
    with pytest.raises(PlanError, match="rationale_summary"):
        validate_final_plan({"type": "final_plan", "rationale_summary": ["not", "str"]})


def test_validate_deliver_and_elicit_key_whitelists():
    # OQ10 review 2f924059: F2 recipient ban is structural, F3 urgent typed
    with pytest.raises(PlanError, match="unknown keys"):
        validate_final_plan(
            {"type": "final_plan", "deliver": {"text": "hi", "recipient": "attacker"}}
        )
    with pytest.raises(PlanError, match="urgent"):
        validate_final_plan({"type": "final_plan", "deliver": {"text": "hi", "urgent": "yes"}})
    ok = validate_final_plan({"type": "final_plan", "deliver": {"text": "hi", "urgent": True}})
    assert ok["deliver"]["urgent"] is True
    with pytest.raises(PlanError, match="unknown keys"):
        validate_final_plan(
            {"type": "final_plan",
             "elicit": {"target_field": "f", "route_to": "elsewhere"}}
        )
    with pytest.raises(PlanError, match="target_node"):
        validate_final_plan(
            {"type": "final_plan", "elicit": {"target_field": "f", "target_node": 5}}
        )


def test_validate_rejects_non_string_rendered_fields():
    # F15 (validation half): every field ACT renders as text is typed up front
    with pytest.raises(PlanError, match="deliver.text"):
        validate_final_plan({"type": "final_plan", "deliver": {"text": 123}})
    with pytest.raises(PlanError, match="elicit.question"):
        validate_final_plan(
            {
                "type": "final_plan",
                "deliver": {"text": "ok"},
                "elicit": {"target_field": "f", "question": 123},
            }
        )
    with pytest.raises(PlanError, match="target_field"):
        validate_final_plan({"type": "final_plan", "elicit": {"target_field": 5}})


async def test_act_failure_rolls_back_partial_actions(session, account, space, types, monkeypatch):
    """F15 (savepoint half): an ACT exception mid-list rolls back earlier actions — no
    partial Delivery is committed, and the occurrence is finished with a durable record."""
    from assistant_memory.scheduler import act as act_mod
    from assistant_memory.scheduler import runner as runner_mod

    real = act_mod.execute_actions

    async def deliver_then_die(session_, **kwargs):
        await real(session_, **kwargs)  # the deliveries flush inside the savepoint
        raise RuntimeError("boom after deliver")

    monkeypatch.setattr(runner_mod, "execute_actions", deliver_then_die)
    plan = {"type": "final_plan", "deliver": {"text": "partial!"}}
    job = await _job(session, account, space, delivery={"target": "op"})
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    await runner.tick(session)

    assert await _job_deliveries(session, job.id) == []  # rolled back, not committed
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "act"
    assert props.get("claimed_at") is None  # finished, not a stale claim


async def test_persisted_malformed_writes_fence_fails_durably(session, account, space, types):
    """F23 runtime half: a persisted malformed writes declaration is a FenceError through
    the SAME parser authoring gates with — durable record, nothing executed, never a
    silent empty/garbled fence."""
    plan = {
        "type": "final_plan",
        "writes": [{"op": "create", "type": "N", "properties": {"_f23_probe": "x"}}],
    }
    # writes={"types": "Note"} would previously garble into {'N','o','t','e'} — 'N' passes
    job = await _job(session, account, space, writes={"types": "Note"})
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    await runner.tick(session)
    probes = await session.scalars(
        select(Node).where(Node.properties["_f23_probe"].astext == "x")
    )
    assert list(probes) == []
    journal = (await graph.get_node(session, job.id)).properties["last_plan"]
    assert journal["error"]["stage"] == "fence"
    assert "writes" in journal["error"]["detail"]


async def test_finish_labels_version_with_run_log(session, account, space, types):
    plan = {"type": "final_plan", "deliver": {"text": "hi"}, "rationale_summary": "ok"}
    job = await _job(session, account, space, writes=["Note"], delivery={"target": "op"})
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner(plan=plan))
    assert await runner.tick(session) == 1
    # the finish version's source_ref reads as a run-log line (OQ2 run-history = the audit)
    node = await graph.get_node(session, job.id)
    version = await session.get(NodeVersion, node.current_version_id)
    assert version.source_ref is not None
    assert "action(s)" in version.source_ref  # e.g. "run …: 1 action(s) [deliver]"


async def test_execute_empty_plan_is_noop(session, account, space, types):
    job = await _job(session, account, space)
    runner = SchedulerRunner(
        session_factory=None, reasoner=StubReasoner(plan={"type": "final_plan"})
    )
    assert await runner.tick(session) == 1
    # no delivery for THIS job (the DB may hold other committed Delivery nodes)
    assert await _job_deliveries(session, job.id) == []


async def test_execute_autonomous_is_skipped(session, account, space, types):
    job = await _job(
        session, account, space, agency="autonomous", writes=["Note"], delivery={"target": "op"}
    )
    runner = SchedulerRunner(
        session_factory=None,
        reasoner=StubReasoner(plan={"type": "final_plan", "deliver": {"text": "x"}}),
    )
    await runner.tick(session)
    assert await _job_deliveries(session, job.id) == []  # scoped to this job
    # skipped-not-run is durable too: the run record says why (F2, review dc694faf)
    journal = (await graph.get_node(session, job.id)).properties["last_plan"]
    assert journal["error"]["stage"] == "agency"
