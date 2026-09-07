# SPDX-License-Identifier: Apache-2.0
"""GATHER slice (D33-D35): declared read-steps → context, effective principal, budget,
fail-closed; plus the runner integration (context reaches REASON, errors stop the run)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from assistant_memory.auth.resolver import Principal
from assistant_memory.models.graph import Node, NodeType
from assistant_memory.models.identity import Membership
from assistant_memory.repository import graph
from assistant_memory.scheduler import gather
from assistant_memory.scheduler.job import iso
from assistant_memory.scheduler.reasoning import StubReasoner
from assistant_memory.scheduler.runner import SchedulerRunner
from assistant_memory.search import get_embedder, index_node


@pytest_asyncio.fixture
async def types(session):
    for t in ("ScheduledJob", "Delivery"):
        if await session.get(NodeType, t) is None:  # may already be seeded in the DB
            session.add(NodeType(type=t, default_sensitivity="normal"))
    await session.flush()


@pytest_asyncio.fixture
async def member(session, account, space):
    """The job's creating account must be a member of the origin space to read from it."""
    session.add(Membership(account_id=account.id, space_id=space.id, permission="admin"))
    await session.flush()


def _principal(account, space) -> Principal:
    return Principal(
        account_id=account.id,
        credential_id=uuid.uuid4(),
        trust="untrusted",
        scopes=[str(space.id)],
        allowed_tools=None,
    )


async def _note(session, account, space, text, *, index=False):
    node = await graph.create_node(
        session, type="Note", space_id=space.id, account_id=account.id,
        label=text[:40], properties={"text": text},
    )
    if index:
        await index_node(session, node, get_embedder())
    return node


# --- declaration validation (the D36 gate's building block) ---------------


def test_validate_rejects_non_list():
    with pytest.raises(gather.GatherError):
        gather.validate_gather_steps({"tool": "get"})


def test_validate_rejects_unknown_tool_args_and_keys():
    with pytest.raises(gather.GatherError):
        gather.validate_gather_steps([{"tool": "delete_node", "args": {}}])
    with pytest.raises(gather.GatherError):
        gather.validate_gather_steps([{"tool": "search", "args": {"sql": "drop"}}])
    with pytest.raises(gather.GatherError):
        gather.validate_gather_steps([{"tool": "search", "args": {}, "foreach": "x"}])


def test_validate_rejects_bad_arg_values():
    """F17: value-level gating — reject at authoring what the handler would reject at
    every occurrence."""
    bad = [
        [{"tool": "get", "args": {"node_id": "not-a-uuid"}}],
        [{"tool": "explain", "args": {"node_id": 42}}],
        [{"tool": "traverse", "args": {"start": "nope"}}],
        [{"tool": "traverse", "args": {"start": str(uuid.uuid4()), "direction": "sideways"}}],
        [{"tool": "traverse", "args": {"start": str(uuid.uuid4()), "depth": "deep"}}],
        [{"tool": "timeline", "args": {"limit": "bad"}}],
        [{"tool": "timeline", "args": {"since": "yesterdayish"}}],
        [{"tool": "search", "args": {"query": 5}}],
        [{"tool": "recent_nodes_context", "args": {"hours": "bad"}}],
        [{"tool": "recent_nodes_context", "args": {"hours": 0}}],
        [{"tool": "recent_nodes_context", "args": {"include_properties": "yes"}}],
    ]
    for steps in bad:
        with pytest.raises(gather.GatherError):
            gather.validate_gather_steps(steps)


def test_validate_accepts_well_formed_steps():
    gather.validate_gather_steps(
        [
            {"tool": "timeline", "args": {"limit": 10}},
            {"tool": "search", "args": {"query": "x", "limit": 5}},
            {"tool": "recent_nodes_context", "args": {"hours": 24, "neighbors_k": 3}},
        ]
    )


# --- execution ------------------------------------------------------------


async def test_run_steps_executes_and_labels_blocks(session, account, space, member):
    node = await _note(session, account, space, "gather-target text")
    text, stats = await gather.run_steps(
        session,
        _principal(account, space),
        [
            {"tool": "get", "args": {"node_id": str(node.id)}},
            {"tool": "timeline", "args": {"limit": 5}},
        ],
    )
    assert "## gather step 0: get" in text
    assert "gather-target text" in text
    assert "## gather step 1: timeline" in text
    assert stats["steps"] == 2
    assert stats["dropped_blocks"] == 0


async def test_run_steps_budget_drops_whole_blocks(session, account, space, member):
    node = await _note(session, account, space, "budget " * 100)
    steps = [{"tool": "get", "args": {"node_id": str(node.id)}}] * 3
    text, stats = await gather.run_steps(
        session, _principal(account, space), steps, budget_chars=900
    )
    assert stats["steps"] == 3
    assert stats["dropped_blocks"] >= 1
    assert "[gather truncated:" in text
    assert len(text) <= 900 + 100  # the marker may exceed the cap, blocks may not


async def test_run_steps_fails_closed_on_a_failing_read(session, account, space, member):
    with pytest.raises(gather.GatherError):
        await gather.run_steps(
            session,
            _principal(account, space),
            [{"tool": "get", "args": {"node_id": str(uuid.uuid4())}}],
        )


async def test_recent_nodes_context_reports_changes_and_sizes(session, account, space, member):
    await _note(session, account, space, "fresh node alpha", index=True)
    await _note(session, account, space, "fresh node beta", index=True)
    text, stats = await gather.run_steps(
        session,
        _principal(account, space),
        [{"tool": "recent_nodes_context", "args": {"hours": 1, "neighbors_k": 2}}],
    )
    assert "fresh node alpha" in text
    assert "fresh node beta" in text
    assert "properties_chars" in text
    assert stats["steps"] == 1


# --- hygiene acks (D38) ---------------------------------------------------
#
# The ack is EXTERNAL to the node it approves and pins that node's version; gather resolves
# it in code, so the job prompt only reads a boolean. Default-deny: no ack = not approved;
# a pin that no longer matches the current version = not approved.


async def _ack(
    session, account, space, targets, *, kind, pins, status="current", judged_by="operator",
    scribe="агент (Claude Code), записал со слов оператора", node_type="Note",
):
    props = {gather.ACK_KIND_KEY: kind, **pins}
    if judged_by is not None:
        props[gather.ACK_JUDGED_BY_KEY] = judged_by
    if scribe is not None:
        props[gather.ACK_SCRIBE_KEY] = scribe
    ack = await graph.create_node(
        session, type=node_type, space_id=space.id, account_id=account.id,
        label="одобрено оператором", properties=props, status=status,
    )
    for target in targets:
        await graph.link(
            session, type=gather.ACK_EDGE, src_node=ack.id, dst_node=target.id,
            account_id=account.id,
        )
    return ack


async def _oversized_ack(session, account, space, target, *, pin=None, **kw):
    pin = str(pin or target.current_version_id)
    return await _ack(
        session, account, space, [target],
        kind=gather.ACK_OVERSIZED, pins={gather.ACK_PIN_KEY: pin}, **kw,
    )


async def _touch(session, account, node):
    return await graph.update_node(
        session, node_id=node.id, account_id=account.id,
        expected_version=node.current_version_id, properties_patch={"text": "edited"},
    )


async def _entries(session, account, space) -> dict:
    result = await gather._recent_nodes_context(
        session, _principal(account, space), hours=1, neighbors_k=0
    )
    return {entry["id"]: entry for entry in result["changed"]}


async def test_recent_nodes_context_defaults_to_no_approval(session, account, space, member):
    node = await _note(session, account, space, "unacked node")
    entry = (await _entries(session, account, space))[str(node.id)]
    assert entry["current_version_id"] == str(node.current_version_id)
    assert entry["oversized_ack"] is False
    assert entry["not_duplicate_of"] == []


async def test_oversized_ack_holds_while_the_pin_matches(session, account, space, member):
    node = await _note(session, account, space, "big node")
    await _oversized_ack(session, account, space, node)
    entry = (await _entries(session, account, space))[str(node.id)]
    assert entry["oversized_ack"] is True


async def test_oversized_ack_lapses_when_the_node_changes(session, account, space, member):
    node = await _note(session, account, space, "big node")
    await _oversized_ack(session, account, space, node)
    await _touch(session, account, node)
    entry = (await _entries(session, account, space))[str(node.id)]
    assert entry["oversized_ack"] is False  # touched since approval — ask the operator again


async def test_ack_is_not_honoured_unless_current_and_well_formed(
    session, account, space, member
):
    retired = await _note(session, account, space, "node with a retired ack")
    await _oversized_ack(session, account, space, retired, status="superseded")

    mismatched = await _note(session, account, space, "node with a stale pin")
    await _oversized_ack(session, account, space, mismatched, pin=uuid.uuid4())

    unpinned = await _note(session, account, space, "node with an unpinned ack")
    await _ack(session, account, space, [unpinned], kind=gather.ACK_OVERSIZED, pins={})

    entries = await _entries(session, account, space)
    for node in (retired, mismatched, unpinned):
        assert entries[str(node.id)]["oversized_ack"] is False, node.label


async def test_ack_nodes_are_not_themselves_hygiene_candidates(session, account, space, member):
    node = await _note(session, account, space, "big node")
    ack = await _oversized_ack(session, account, space, node)
    entries = await _entries(session, account, space)
    assert str(node.id) in entries
    assert str(ack.id) not in entries  # bookkeeping about the graph, not content in it


async def test_pair_ack_marks_both_sides_not_duplicates(session, account, space, member):
    left = await _note(session, account, space, "клиника отзывы — часть A")
    right = await _note(session, account, space, "клиника отзывы — часть B")
    await _ack(
        session, account, space, [left, right],
        kind=gather.ACK_NOT_DUPLICATES,
        pins={gather.ACK_PINS_KEY: {
            str(left.id): str(left.current_version_id),
            str(right.id): str(right.current_version_id),
        }},
    )
    entries = await _entries(session, account, space)
    assert entries[str(left.id)]["not_duplicate_of"] == [str(right.id)]
    assert entries[str(right.id)]["not_duplicate_of"] == [str(left.id)]


async def test_one_sided_pair_ack_grants_nothing(session, account, space, member):
    """F1 (critic, review 7f4cf857): an ack that PINS a pair but only REFERENCES one side must
    not suppress the pair — a referenced set that disagrees with the pinned set is a broken
    record, not an approval."""
    left = await _note(session, account, space, "односторонняя отметка — левый")
    right = await _note(session, account, space, "односторонняя отметка — правый")
    await _ack(
        session, account, space, [left],  # references LEFT only...
        kind=gather.ACK_NOT_DUPLICATES,
        pins={gather.ACK_PINS_KEY: {  # ...but pins BOTH
            str(left.id): str(left.current_version_id),
            str(right.id): str(right.current_version_id),
        }},
    )
    entries = await _entries(session, account, space)
    assert entries[str(left.id)]["not_duplicate_of"] == []
    assert entries[str(right.id)]["not_duplicate_of"] == []


async def test_oversized_ack_pointing_at_two_nodes_grants_nothing(
    session, account, space, member
):
    """F1 sibling: `oversized_ok` pins one version and names no node, so it may reference
    exactly ONE target — otherwise the pin is ambiguous and grants nothing."""
    one = await _note(session, account, space, "цель один")
    two = await _note(session, account, space, "цель два")
    await _ack(
        session, account, space, [one, two],
        kind=gather.ACK_OVERSIZED,
        pins={gather.ACK_PIN_KEY: str(one.current_version_id)},
    )
    entries = await _entries(session, account, space)
    assert entries[str(one.id)]["oversized_ack"] is False
    assert entries[str(two.id)]["oversized_ack"] is False


async def test_malformed_ack_like_node_stays_visible_to_the_survey(
    session, account, space, member
):
    """F2 (critic, review 7f4cf857): a marker property alone must NOT hide a node. A node that
    merely CLAIMS to be an ack (no references, no parsable pin) stays in the survey — silent
    suppression is exactly what default-deny forbids."""
    impostor = await graph.create_node(
        session, type="Note", space_id=space.id, account_id=account.id,
        label="узел с меткой, но без структуры",
        properties={gather.ACK_KIND_KEY: gather.ACK_OVERSIZED, "text": "не отметка"},
    )
    unlinked = await _note(session, account, space, "цель без ссылки")
    entries = await _entries(session, account, space)
    assert str(impostor.id) in entries  # visible: it is not a valid ack record
    assert entries[str(impostor.id)]["oversized_ack"] is False
    assert entries[str(unlinked.id)]["oversized_ack"] is False


async def test_three_way_not_duplicates_ack_grants_nothing(session, account, space, member):
    """F4 (critic, review 7f4cf857): `not_duplicates` approves a PAIR. A three-way ack would
    suppress several duplicate pairs off one approval the operator never gave in that shape."""
    a = await _note(session, account, space, "тройка — A")
    b = await _note(session, account, space, "тройка — B")
    c = await _note(session, account, space, "тройка — C")
    await _ack(
        session, account, space, [a, b, c],
        kind=gather.ACK_NOT_DUPLICATES,
        pins={gather.ACK_PINS_KEY: {
            str(n.id): str(n.current_version_id) for n in (a, b, c)
        }},
    )
    entries = await _entries(session, account, space)
    for node in (a, b, c):
        assert entries[str(node.id)]["not_duplicate_of"] == [], node.label


async def test_ack_without_operator_judgement_grants_nothing_and_stays_visible(
    session, account, space, member
):
    """F5 (critic, review 7f4cf857): the judgement is the OPERATOR's and the agent is only the
    scribe — the schema says every ack records that, so gather enforces it. A well-formed ack
    that does not claim operator judgement is not an approval, and cannot hide anything."""
    node = await _note(session, account, space, "узел с неподтверждённой отметкой")
    ack = await _oversized_ack(session, account, space, node, judged_by=None)
    entries = await _entries(session, account, space)
    assert entries[str(node.id)]["oversized_ack"] is False
    assert str(ack.id) in entries  # not a valid ack ⇒ not suppressed from the survey

    self_granted = await _oversized_ack(
        session, account, space,
        await _note(session, account, space, "узел, одобренный сам себе"),
        judged_by="agent",
    )
    assert str(self_granted.id) in (await _entries(session, account, space))


async def test_non_note_node_shaped_like_an_ack_grants_nothing(session, account, space, member):
    """F7 (critic, review 7f4cf857): the operator's schema says an ack is an ordinary Note. A
    content node of another type that merely matches the shape is not the approved contract —
    it grants nothing and stays visible."""
    target = await _note(session, account, space, "цель отметки не-того-типа")
    impostor = await _ack(
        session, account, space, [target],
        kind=gather.ACK_OVERSIZED,
        pins={gather.ACK_PIN_KEY: str(target.current_version_id)},
        node_type="Idea",
    )
    entries = await _entries(session, account, space)
    assert entries[str(target.id)]["oversized_ack"] is False
    assert str(impostor.id) in entries


async def test_ack_without_a_scribe_grants_nothing(session, account, space, member):
    """F8: D38 promises both `judged_by` and the `scribe` who wrote the judgement down. Both
    are enforced — a promised provenance field that nothing checks is decoration."""
    node = await _note(session, account, space, "узел с отметкой без писаря")
    ack = await _oversized_ack(session, account, space, node, scribe=None)
    entries = await _entries(session, account, space)
    assert entries[str(node.id)]["oversized_ack"] is False
    assert str(ack.id) in entries


async def test_pair_ack_lapses_for_both_sides_when_one_changes(session, account, space, member):
    left = await _note(session, account, space, "пара — левый")
    right = await _note(session, account, space, "пара — правый")
    await _ack(
        session, account, space, [left, right],
        kind=gather.ACK_NOT_DUPLICATES,
        pins={gather.ACK_PINS_KEY: {
            str(left.id): str(left.current_version_id),
            str(right.id): str(right.current_version_id),
        }},
    )
    await _touch(session, account, left)
    entries = await _entries(session, account, space)
    assert entries[str(left.id)]["not_duplicate_of"] == []  # its own pin lapsed
    assert entries[str(right.id)]["not_duplicate_of"] == []  # the other side moved


def test_effective_principal_uses_creator_and_origin_space(account, space):
    class _N:
        id = uuid.uuid4()
        created_by = account.id
        origin_space = space.id

    p = gather.effective_principal(_N())
    assert p.account_id == account.id
    assert p.scopes == [str(space.id)]
    assert p.credential_id == gather.GATHER_CREDENTIAL_ID


def test_effective_principal_fails_closed_without_origin_space(account):
    """F9: origin_space=None must NOT become scopes=None (= the creator's full account
    membership) — a malformed job gets no reads at all."""

    class _N:
        id = uuid.uuid4()
        created_by = account.id
        origin_space = None

    with pytest.raises(gather.GatherError, match="origin_space"):
        gather.effective_principal(_N())


async def test_runner_missing_origin_space_no_reads_no_reason(
    session, account, space, member, types
):
    """F9 end-to-end: a tools-declaring job with no origin_space fails in GATHER — the
    reasoner is never invoked and the failure is journaled durably."""
    job = await _job(session, account, space, tools=[{"tool": "timeline", "args": {"limit": 2}}])
    job.origin_space = None
    await session.flush()
    reasoner = _RecordingReasoner(plan={"type": "final_plan"})
    runner = SchedulerRunner(session_factory=None, reasoner=reasoner)
    await runner.tick(session)
    assert reasoner.contexts == []  # REASON never saw a context
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "gather"
    assert "origin_space" in props["last_plan"]["error"]["detail"]


# --- runner integration ---------------------------------------------------


class _RecordingReasoner(StubReasoner):
    def __init__(self, plan=None):
        super().__init__(plan)
        self.contexts: list[dict] = []

    async def reason(self, context: dict) -> dict:
        self.contexts.append(context)
        return await super().reason(context)


async def _job(session, account, space, *, tools=None, delivery=None):
    props = {
        "enabled": True,
        "trigger": {"kind": "interval", "spec": {"seconds": 3600}},
        "instruction": "survey",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    if tools is not None:
        props["tools"] = tools
    if delivery is not None:
        props["delivery"] = delivery
    return await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )


async def test_runner_feeds_gathered_context_to_reason(session, account, space, member, types):
    await _note(session, account, space, "context-marker-xyz")
    job = await _job(session, account, space, tools=[{"tool": "timeline", "args": {"limit": 5}}])
    reasoner = _RecordingReasoner(plan={"type": "final_plan"})
    runner = SchedulerRunner(session_factory=None, reasoner=reasoner)
    assert await runner.tick(session) == 1

    assert len(reasoner.contexts) == 1
    assert "## gather step 0: timeline" in reasoner.contexts[0]["context"]
    journal = (await graph.get_node(session, job.id)).properties["last_plan"]
    assert journal["gather"]["steps"] == 1


async def test_runner_gather_failure_stops_the_run(session, account, space, member, types):
    job = await _job(
        session,
        account,
        space,
        tools=[{"tool": "get", "args": {"node_id": str(uuid.uuid4())}}],
        delivery={"target": "op"},
    )
    runner = SchedulerRunner(
        session_factory=None,
        reasoner=StubReasoner(plan={"type": "final_plan", "deliver": {"text": "should not go"}}),
    )
    await runner.tick(session)
    deliveries = list(
        await session.scalars(
            select(Node).where(
                Node.type == "Delivery", Node.properties["key"].astext.like(f"{job.id}:%")
            )
        )
    )
    assert deliveries == []  # fail closed: nothing delivered
    props = (await graph.get_node(session, job.id)).properties
    # the failure is DURABLE (F2): journaled in the run record, not process-log-only
    assert props["last_plan"]["error"]["stage"] == "gather"
    assert props["next_run"] > iso(datetime.now(UTC) - timedelta(minutes=1))  # still advanced


class _RaisingReasoner:
    async def reason(self, context: dict) -> dict:
        from assistant_memory.scheduler.reasoning import ReasonerError

        raise ReasonerError("boom: not json")


async def test_runner_reasoner_protocol_failure_is_durable(session, account, space, member, types):
    from assistant_memory.models.graph import NodeVersion

    job = await _job(session, account, space, tools=[{"tool": "timeline", "args": {"limit": 2}}])
    runner = SchedulerRunner(session_factory=None, reasoner=_RaisingReasoner())
    await runner.tick(session)
    node = await graph.get_node(session, job.id)
    assert node.properties["last_plan"]["error"]["stage"] == "reason"
    version = await session.get(NodeVersion, node.current_version_id)
    assert "FAILED at reason" in version.source_ref  # readable in the version-chain run-log


async def test_runner_malformed_trigger_no_side_effects(session, account, space, member, types):
    """F12: a persisted trigger that cannot advance fails BEFORE any ACT side effect —
    no delivery is committed, the claim is finished (not left to lease-expiry retry),
    the job is disabled with a durable 'advance' failure record."""
    props = {
        "enabled": True,
        "trigger": {"kind": "interval", "spec": {"seconds": "bad"}},
        "instruction": "go",
        "delivery": {"target": "op"},
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(
        session_factory=None,
        reasoner=StubReasoner(plan={"type": "final_plan", "deliver": {"text": "must not go"}}),
    )
    await runner.tick(session)
    deliveries = list(
        await session.scalars(
            select(Node).where(
                Node.type == "Delivery", Node.properties["key"].astext.like(f"{job.id}:%")
            )
        )
    )
    assert deliveries == []  # ACT never ran
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "advance"
    assert props["enabled"] is False  # it can never schedule again — visible, not looping
    assert props.get("claimed_at") is None  # finished, not a stale claim


async def test_runner_nonobject_spec_fails_durably(session, account, space, member, types):
    """F19: a persisted truthy non-dict trigger.spec dies in advance() with a durable
    record and a cleared claim — never an AttributeError escaping past the claim."""
    props = {
        "enabled": True,
        "trigger": {"kind": "interval", "spec": "sixty seconds"},
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "advance"
    assert "must be an object" in props["last_plan"]["error"]["detail"]
    assert props["enabled"] is False
    assert props.get("claimed_at") is None


async def test_runner_nonobject_trigger_never_claims(session, account, space, member, types):
    """F19 sibling: a truthy non-dict trigger reads as unsupported kind — skipped before
    any claim, nothing raised."""
    props = {
        "enabled": True,
        "trigger": "every morning",
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    assert await runner.tick(session) == 0
    props = (await graph.get_node(session, job.id)).properties
    assert props.get("claimed_at") is None  # never claimed


async def test_runner_malformed_predicate_fails_durably(session, account, space, member, types):
    """F21 runtime half: a persisted malformed predicate is journaled durably and the job
    disabled — never a due-but-unjournaled retry loop."""
    props = {
        "enabled": True,
        "trigger": {"kind": "interval", "spec": {"seconds": 3600},
                    "predicate": {"field": "x"}},
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "predicate"
    assert props["enabled"] is False
    assert props.get("claimed_at") is None


async def test_runner_composite_predicate_garbage_fails_durably(
    session, account, space, member, types
):
    """F26: {"all": "bad"} would AttributeError deep in evaluation — static validate in
    the runtime path maps it to the durable predicate stage."""
    props = {
        "enabled": True,
        "trigger": {"kind": "interval", "spec": {"seconds": 3600},
                    "predicate": {"all": "bad"}},
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "predicate"
    assert props["enabled"] is False
    assert props.get("claimed_at") is None


async def test_runner_falsy_persisted_predicates_fail_durably(
    session, account, space, member, types
):
    """F30: a falsy malformed predicate ([], "", 0) is PRESENT — it must fail closed with
    a durable record, not read as 'no gate' and execute the job unconditionally."""
    for bad in ([], "", 0):
        props = {
            "enabled": True,
            "trigger": {"kind": "interval", "spec": {"seconds": 3600}, "predicate": bad},
            "instruction": "go",
            "delivery": {"target": "op"},
            "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
        }
        job = await graph.create_node(
            session, type="ScheduledJob", space_id=space.id, account_id=account.id,
            properties=props,
        )
        runner = SchedulerRunner(
            session_factory=None,
            reasoner=StubReasoner(plan={"type": "final_plan", "deliver": {"text": "no"}}),
        )
        await runner.tick(session)
        props = (await graph.get_node(session, job.id)).properties
        assert props["last_plan"]["error"]["stage"] == "predicate", f"predicate={bad!r}"
        assert props["enabled"] is False
        deliveries = list(
            await session.scalars(
                select(Node).where(
                    Node.type == "Delivery", Node.properties["key"].astext.like(f"{job.id}:%")
                )
            )
        )
        assert deliveries == []  # never executed


async def test_runner_persisted_one_shot_predicate_fails_durably(
    session, account, space, member, types
):
    """F26: a persisted one_shot+predicate lands in the durable finish+disable path
    instead of being skipped silently while due."""
    props = {
        "enabled": True,
        "trigger": {"kind": "one_shot", "spec": {"at": "2020-01-01T00:00:00+00:00"},
                    "predicate": {"node": str(uuid.uuid4()), "op": "exists"}},
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "predicate"
    assert "one_shot" in props["last_plan"]["error"]["detail"]
    assert props["enabled"] is False


async def test_runner_nonstring_cron_expr_fails_durably(session, account, space, member, types):
    """F27: a persisted non-string cron expr dies in advance() with a durable record,
    not an AttributeError from croniter escaping past the claim."""
    props = {
        "enabled": True,
        "trigger": {"kind": "cron", "spec": {"expr": ["0", "8"]}},
        "instruction": "go",
        "next_run": iso(datetime.now(UTC) - timedelta(minutes=1)),
    }
    job = await graph.create_node(
        session, type="ScheduledJob", space_id=space.id, account_id=account.id, properties=props
    )
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "advance"
    assert props["enabled"] is False
    assert props.get("claimed_at") is None


async def test_runner_nonobject_delivery_fails_durably(session, account, space, member, types):
    """F22 runtime half: a truthy non-object delivery fails before ACT with a durable
    record, not an AttributeError past the claim."""
    job = await _job(session, account, space, delivery="op")
    runner = SchedulerRunner(
        session_factory=None,
        reasoner=StubReasoner(plan={"type": "final_plan", "deliver": {"text": "hi"}}),
    )
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "delivery"
    assert props.get("claimed_at") is None


async def test_runner_nonobject_budget_fails_durably(session, account, space, member, types):
    """F20: a persisted truthy non-object budget on a tools job fails in GATHER with a
    durable record — not an AttributeError past the claim."""
    job = await _job(session, account, space, tools=[{"tool": "timeline", "args": {"limit": 2}}])
    node = await graph.get_node(session, job.id)
    node.properties = {**node.properties, "budget": "generous"}
    await session.flush()
    runner = SchedulerRunner(session_factory=None, reasoner=StubReasoner())
    await runner.tick(session)
    props = (await graph.get_node(session, job.id)).properties
    assert props["last_plan"]["error"]["stage"] == "gather"
    assert "budget must be an object" in props["last_plan"]["error"]["detail"]
    assert props.get("claimed_at") is None


class _TransportFailReasoner:
    """A backend/transport failure (timeout, missing CLI, API error) — NOT a ReasonerError."""

    async def reason(self, context: dict) -> dict:
        # Any exception that is not a ReasonerError stands for a backend failure here;
        # the strong tier's own StrongTierError is one of these (a RuntimeError subclass),
        # and naming it would tie this test to the optional bot layer for nothing.
        raise RuntimeError("strong tier timed out after 180.0s")


async def test_runner_reasoner_transport_failure_is_durable(
    session, account, space, member, types
):
    from assistant_memory.models.graph import NodeVersion

    job = await _job(session, account, space, tools=[{"tool": "timeline", "args": {"limit": 2}}])
    runner = SchedulerRunner(session_factory=None, reasoner=_TransportFailReasoner())
    await runner.tick(session)
    node = await graph.get_node(session, job.id)
    # F5: a backend failure lands in the run record like any other failure — it must not
    # escape to tick() leaving only a stale claim and a process log line
    assert node.properties["last_plan"]["error"]["stage"] == "reason_transport"
    assert "timed out" in node.properties["last_plan"]["error"]["detail"]
    version = await session.get(NodeVersion, node.current_version_id)
    assert "FAILED at reason_transport" in version.source_ref
