# SPDX-License-Identifier: Apache-2.0
"""Scheduler slice 1: ScheduledJob node type, due-jobs query, claim/finish run-record."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from assistant_memory.models.graph import NodeType
from assistant_memory.repository import graph
from assistant_memory.scheduler import predicate
from assistant_memory.scheduler.job import (
    KEY_ENABLED,
    KEY_INSTRUCTION,
    KEY_NEXT_RUN,
    KEY_TRIGGER,
    ScheduledJob,
    iso,
)
from assistant_memory.scheduler.repository import due_jobs, next_due
from assistant_memory.scheduler.runner import SchedulerRunner


@pytest_asyncio.fixture
async def sj_type(session):
    """Register the ScheduledJob node type inside the test transaction (self-contained,
    independent of whether the seed migration has been applied to the test DB)."""
    if await session.get(NodeType, "ScheduledJob") is None:  # may already be seeded in the DB
        session.add(NodeType(type="ScheduledJob", default_sensitivity="normal"))
        await session.flush()


async def _make_job(
    session, account, space, *, next_run, enabled=True, kind="interval", spec=None, predicate=None
):
    trigger = {"kind": kind, "spec": spec if spec is not None else {"seconds": 3600}}
    if predicate is not None:
        trigger["predicate"] = predicate
    return await graph.create_node(
        session,
        type="ScheduledJob",
        space_id=space.id,
        account_id=account.id,
        label="test job",
        properties={
            KEY_ENABLED: enabled,
            KEY_INSTRUCTION: "do the thing",
            KEY_TRIGGER: trigger,
            KEY_NEXT_RUN: iso(next_run),
        },
    )


async def test_due_jobs_filters(session, account, space, sj_type):
    now = datetime.now(UTC)
    past = now - timedelta(minutes=5)
    future = now + timedelta(minutes=5)
    due = await _make_job(session, account, space, next_run=past)
    not_due = await _make_job(session, account, space, next_run=future)
    disabled = await _make_job(session, account, space, next_run=past, enabled=False)

    got = {n.id for n in await due_jobs(session, now=now, lease_seconds=300)}
    assert due.id in got
    assert not_due.id not in got
    assert disabled.id not in got


async def test_tick_fires_interval_and_advances(session, account, space, sj_type):
    now = datetime.now(UTC)
    job = await _make_job(
        session,
        account,
        space,
        next_run=now - timedelta(minutes=1),
        kind="interval",
        spec={"seconds": 3600},
    )

    runner = SchedulerRunner(session_factory=None, lease_seconds=300)
    assert await runner.tick(session) == 1

    props = (await graph.get_node(session, job.id)).properties
    assert props["finished_at"] is not None
    assert props["claimed_at"] is None
    assert props["enabled"] is True
    assert datetime.fromisoformat(props["next_run"]) > now
    # no longer due
    assert job.id not in {n.id for n in await due_jobs(session, now=now, lease_seconds=300)}


async def test_tick_one_shot_disables(session, account, space, sj_type):
    now = datetime.now(UTC)
    job = await _make_job(
        session, account, space, next_run=now - timedelta(minutes=1), kind="one_shot", spec={}
    )
    runner = SchedulerRunner(session_factory=None)
    assert await runner.tick(session) == 1

    props = (await graph.get_node(session, job.id)).properties
    assert props["enabled"] is False
    assert props["next_run"] is None
    assert props["finished_at"] is not None


async def test_tick_journals_malformed_predicate(session, account, space, sj_type):
    """Superseded expectation (F21, review dc694faf): a malformed predicate used to fail
    closed but INVISIBLY (due-but-unjournaled loop); it now finishes the occurrence with a
    durable {stage: predicate} record and disables the job."""
    now = datetime.now(UTC)
    job = await _make_job(
        session, account, space, next_run=now - timedelta(minutes=1), predicate={"field": "x"}
    )
    runner = SchedulerRunner(session_factory=None)
    assert await runner.tick(session) == 0  # still does not fire
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "predicate"
    assert props["enabled"] is False
    assert props.get("claimed_at") is None


# --- slice 2: cron, deterministic predicate, next_due ------------------------


def test_cron_advance():
    now = datetime(2026, 1, 1, 0, 2, tzinfo=UTC)
    job = ScheduledJob(
        uuid.uuid4(), None, {KEY_TRIGGER: {"kind": "cron", "spec": {"expr": "*/5 * * * *"}}}
    )
    nxt, disable = job.advance(now)
    assert disable is False
    assert nxt == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    assert nxt.tzinfo is not None


def test_interval_advance():
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    job = ScheduledJob(
        uuid.uuid4(), None, {KEY_TRIGGER: {"kind": "interval", "spec": {"seconds": 90}}}
    )
    nxt, disable = job.advance(now)
    assert disable is False
    assert nxt == now + timedelta(seconds=90)


async def _flag(session, account, space, **props):
    return await graph.create_node(
        session,
        type="Note",
        space_id=space.id,
        account_id=account.id,
        label="flag",
        properties=props,
    )



def _access(session, account, space):
    """Predicate reads are access-scoped (F31): tests evaluate under the job's effective
    account+space, with membership granted like a real operator account."""
    from assistant_memory.access import Access

    return Access(session, account.id, scopes={space.id})


async def _grant(session, account, space):
    from assistant_memory.models.identity import Membership

    session.add(Membership(account_id=account.id, space_id=space.id, permission="admin"))
    await session.flush()

async def test_predicate_leaves_and_composition(session, account, space):
    await _grant(session, account, space)
    acc = _access(session, account, space)
    n = await _flag(session, account, space, a=1, nested={"b": "x"})
    nid = str(n.id)

    eq = {"node": nid, "path": "a", "op": "eq", "value": 1}
    assert await predicate.evaluate(session, eq, acc)
    assert not await predicate.evaluate(session, {**eq, "op": "ne"}, acc)
    assert await predicate.evaluate(session, {**eq, "op": "ge"}, acc)
    assert await predicate.evaluate(session, {"node": nid, "path": "a", "op": "exists"}, acc)
    assert await predicate.evaluate(session, {"node": nid, "path": "missing", "op": "absent"}, acc)
    assert await predicate.evaluate(
        session, {"node": nid, "path": "nested.b", "op": "eq", "value": "x"}, acc
    )

    leaf_true = {"node": nid, "path": "a", "op": "eq", "value": 1}
    leaf_false = {"node": nid, "path": "a", "op": "eq", "value": 2}
    assert await predicate.evaluate(session, {"all": [leaf_true, leaf_true]}, acc)
    assert not await predicate.evaluate(session, {"all": [leaf_true, leaf_false]}, acc)
    assert await predicate.evaluate(session, {"any": [leaf_false, leaf_true]}, acc)
    assert not await predicate.evaluate(session, {"not": leaf_true}, acc)

    # empty / absent predicate = no gate
    assert await predicate.evaluate(session, {}, acc)
    assert await predicate.evaluate(session, None, acc)


async def test_predicate_missing_node(session, account, space):
    await _grant(session, account, space)
    acc = _access(session, account, space)
    ghost = str(uuid.uuid4())
    assert not await predicate.evaluate(
        session, {"node": ghost, "path": "a", "op": "eq", "value": 1}, acc
    )
    assert await predicate.evaluate(session, {"node": ghost, "path": "a", "op": "absent"}, acc)


async def test_predicate_out_of_scope_node_reads_absent(session, account, space, space2):
    """F31: a node OUTSIDE the job's effective scope reads as ABSENT — same isolation as
    GATHER (D18/D19) — even though it exists in the graph."""
    await _grant(session, account, space)
    foreign = await graph.create_node(
        session, type="Note", space_id=space2.id, account_id=account.id,
        properties={"a": 1},
    )
    acc = _access(session, account, space)  # scoped to `space`, not space2
    leaf = {"node": str(foreign.id), "path": "a", "op": "exists"}
    assert not await predicate.evaluate(session, leaf, acc)
    assert await predicate.evaluate(
        session, {"node": str(foreign.id), "path": "a", "op": "absent"}, acc
    )


async def test_predicate_malformed_raises(session, account, space):
    acc = _access(session, account, space)
    with pytest.raises(ValueError):
        await predicate.evaluate(session, {"path": "a", "op": "eq", "value": 1}, acc)  # no node
    with pytest.raises(ValueError):
        await predicate.evaluate(
            session, {"node": str(uuid.uuid4()), "path": "a", "op": "??"}, acc
        )  # bad op


async def test_tick_predicate_true_executes(session, account, space, sj_type):
    await _grant(session, account, space)  # predicate reads are scoped (F31)
    now = datetime.now(UTC)
    flag = await _flag(session, account, space, go=True)
    job = await _make_job(
        session,
        account,
        space,
        next_run=now - timedelta(minutes=1),
        kind="interval",
        spec={"seconds": 3600},
        predicate={"node": str(flag.id), "path": "go", "op": "eq", "value": True},
    )
    runner = SchedulerRunner(session_factory=None)
    assert await runner.tick(session) == 1
    props = (await graph.get_node(session, job.id)).properties
    assert props["finished_at"] is not None
    assert datetime.fromisoformat(props["next_run"]) > now


async def test_tick_predicate_false_reschedules_without_executing(session, account, space, sj_type):
    await _grant(session, account, space)  # predicate reads are scoped (F31)
    now = datetime.now(UTC)
    flag = await _flag(session, account, space, go=False)
    job = await _make_job(
        session,
        account,
        space,
        next_run=now - timedelta(minutes=1),
        kind="interval",
        spec={"seconds": 3600},
        predicate={"node": str(flag.id), "path": "go", "op": "eq", "value": True},
    )
    runner = SchedulerRunner(session_factory=None)
    assert await runner.tick(session) == 0  # predicate false → not executed
    props = (await graph.get_node(session, job.id)).properties
    assert props["finished_at"] is not None  # occurrence handled
    assert datetime.fromisoformat(props["next_run"]) > now  # but rescheduled


async def test_tick_cron_advances(session, account, space, sj_type):
    now = datetime.now(UTC)
    job = await _make_job(
        session,
        account,
        space,
        next_run=now - timedelta(minutes=1),
        kind="cron",
        spec={"expr": "*/5 * * * *"},
    )
    runner = SchedulerRunner(session_factory=None)
    assert await runner.tick(session) == 1
    props = (await graph.get_node(session, job.id)).properties
    assert datetime.fromisoformat(props["next_run"]) > now
    assert props["enabled"] is True


async def test_next_due_returns_earliest_enabled(session, account, space, sj_type):
    now = datetime.now(UTC)
    await _make_job(session, account, space, next_run=now + timedelta(hours=2))
    await _make_job(session, account, space, next_run=now + timedelta(minutes=10))
    await _make_job(session, account, space, next_run=now + timedelta(minutes=1), enabled=False)
    nd = await next_due(session)
    assert nd is not None
    assert abs((nd - (now + timedelta(minutes=10))).total_seconds()) < 1
