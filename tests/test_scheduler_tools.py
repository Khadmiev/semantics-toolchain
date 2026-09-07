# SPDX-License-Identifier: Apache-2.0
"""MCP authoring tools for ScheduledJob: create / list / pause + the D36 executability gate
and dry_run (actionable layer)."""

import uuid

import pytest
import pytest_asyncio

from assistant_memory.auth.resolver import Principal
from assistant_memory.config import settings
from assistant_memory.mcp import tools
from assistant_memory.mcp.errors import InvalidArgument
from assistant_memory.models.graph import NodeType
from assistant_memory.models.identity import Membership


@pytest.fixture(autouse=True)
def _executable_runtime(monkeypatch):
    """Authoring now validates against the executing runtime (D36); tests author against a
    runtime that could actually run the job unless a test overrides these."""
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(settings, "scheduler_reasoner", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")


@pytest_asyncio.fixture
async def sj_type(session):
    if await session.get(NodeType, "ScheduledJob") is None:  # may already be seeded in the DB
        session.add(NodeType(type="ScheduledJob", default_sensitivity="normal"))
        await session.flush()


@pytest_asyncio.fixture
async def principal(session, account, space):
    session.add(Membership(account_id=account.id, space_id=space.id, permission="admin"))
    await session.flush()
    return Principal(
        account_id=account.id,
        credential_id=uuid.uuid4(),
        trust="trusted",
        scopes=None,
        allowed_tools=None,
    )


async def test_create_list_pause(session, principal, sj_type):
    res = await tools.create_scheduled_job(
        session,
        principal,
        instruction="morning brief",
        trigger={"kind": "interval", "spec": {"seconds": 3600}},
    )
    assert res["status"] == "ok"
    node = res["node"]
    assert node["type"] == "ScheduledJob"
    assert node["properties"]["enabled"] is True
    assert node["properties"]["next_run"]
    jid = node["id"]

    listed = await tools.list_scheduled_jobs(session, principal)
    assert any(j["id"] == jid and j["kind"] == "interval" for j in listed["jobs"])

    await tools.set_scheduled_job_enabled(session, principal, node_id=jid, enabled=False)
    j = next(
        j for j in (await tools.list_scheduled_jobs(session, principal))["jobs"] if j["id"] == jid
    )
    assert j["enabled"] is False

    active = await tools.list_scheduled_jobs(session, principal, include_disabled=False)
    assert not any(j["id"] == jid for j in active["jobs"])


async def test_create_cron_and_one_shot(session, principal, sj_type):
    cron = await tools.create_scheduled_job(
        session,
        principal,
        instruction="c",
        trigger={"kind": "cron", "spec": {"expr": "*/5 * * * *"}},
    )
    assert cron["node"]["properties"]["next_run"]

    one = await tools.create_scheduled_job(
        session,
        principal,
        instruction="o",
        trigger={"kind": "one_shot", "spec": {"at": "2030-01-01T09:00:00+00:00"}},
    )
    assert one["node"]["properties"]["next_run"].startswith("2030-01-01T09:00:00")


async def test_create_bad_trigger(session, principal, sj_type):
    with pytest.raises(InvalidArgument):
        await tools.create_scheduled_job(
            session, principal, instruction="x", trigger={"kind": "nope"}
        )
    with pytest.raises(InvalidArgument):
        await tools.create_scheduled_job(
            session, principal, instruction="x", trigger={"kind": "one_shot", "spec": {}}
        )


async def test_create_rejects_disabled_scheduler(session, principal, sj_type, monkeypatch):
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    with pytest.raises(InvalidArgument, match="DISABLED"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
        )


async def test_create_rejects_stub_reasoner(session, principal, sj_type, monkeypatch):
    monkeypatch.setattr(settings, "scheduler_reasoner", "stub")
    with pytest.raises(InvalidArgument, match="stub"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
        )


async def test_create_rejects_unconstructible_reasoner(session, principal, sj_type, monkeypatch):
    monkeypatch.setattr(settings, "scheduler_reasoner", "strong")
    monkeypatch.setattr(settings, "strong_cli_token", "")
    with pytest.raises(InvalidArgument, match="misconfigured"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
        )


async def test_create_rejects_autonomous_agency(session, principal, sj_type):
    with pytest.raises(InvalidArgument, match="autonomous"):
        await tools.create_scheduled_job(
            session, principal, instruction="x", agency="autonomous",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
        )


async def test_create_rejects_bad_gather_step(session, principal, sj_type):
    with pytest.raises(InvalidArgument, match="unknown tool"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            tools=[{"tool": "delete_node", "args": {}}],
        )


async def test_create_rejects_delivery_without_bot(session, principal, sj_type, monkeypatch):
    monkeypatch.setattr(settings, "bot_enabled", False)
    with pytest.raises(InvalidArgument, match="bot channel"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            delivery={"channel": "bot", "target": "op"},
        )


async def test_create_accepts_valid_gather_steps(session, principal, sj_type):
    res = await tools.create_scheduled_job(
        session, principal, instruction="survey",
        trigger={"kind": "cron", "spec": {"expr": "30 7 * * *"}},
        tools=[{"tool": "recent_nodes_context", "args": {"hours": 24, "neighbors_k": 3}}],
    )
    assert res["status"] == "ok"
    assert res["node"]["properties"]["tools"][0]["tool"] == "recent_nodes_context"


async def test_dry_run_probes_without_creating(session, principal, sj_type, monkeypatch):
    # dry_run skips the enablement checks (it runs NOW, not via the resident runner) and
    # must work even where the runner is off — that is its purpose as a probe.
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "scheduler_reasoner", "stub")
    before = (await tools.list_scheduled_jobs(session, principal))["jobs"]
    res = await tools.create_scheduled_job(
        session, principal, instruction="probe",
        trigger={"kind": "interval", "spec": {"seconds": 60}},
        tools=[{"tool": "timeline", "args": {"limit": 3}}],
        dry_run=True,
    )
    assert res["status"] == "dry_run"
    assert res["plan"] == {"type": "final_plan"}  # the stub's no-op plan, validated
    assert res["plan_error"] is None
    assert res["gather"]["steps"] == 1
    after = (await tools.list_scheduled_jobs(session, principal))["jobs"]
    assert len(after) == len(before)  # nothing was created


async def test_create_rejects_non_executable_interval(session, principal, sj_type):
    # F13: authoring must reject an interval the runtime cannot advance with
    for bad in ("bad", 0, -5):
        for dry in (False, True):
            with pytest.raises(InvalidArgument, match="seconds"):
                await tools.create_scheduled_job(
                    session, principal, instruction="x",
                    trigger={"kind": "interval", "spec": {"seconds": bad}},
                    dry_run=dry,
                )


async def test_create_rejects_missing_required_gather_args(session, principal, sj_type):
    # F14: required args + non-dict falsy args are gated at authoring
    for bad_tools in (
        [{"tool": "get", "args": {}}],
        [{"tool": "traverse", "args": {}}],
        [{"tool": "explain", "args": []}],
    ):
        for dry in (False, True):
            with pytest.raises(InvalidArgument, match="args"):
                await tools.create_scheduled_job(
                    session, principal, instruction="x",
                    trigger={"kind": "cron", "spec": {"expr": "0 8 * * *"}},
                    tools=bad_tools,
                    dry_run=dry,
                )


async def test_dry_run_on_stub_reasoner_is_marked(session, principal, sj_type, monkeypatch):
    # F16: a stub probe must not be mistakable for the D36 proving occurrence
    monkeypatch.setattr(settings, "scheduler_reasoner", "stub")
    res = await tools.create_scheduled_job(
        session, principal, instruction="probe",
        trigger={"kind": "interval", "spec": {"seconds": 60}},
        dry_run=True,
    )
    assert res["reasoner"] == "stub"
    assert "does NOT prove REASON capability" in res["warning"]


async def test_create_rejects_malformed_gather_budget(session, principal, sj_type):
    # F10: a bad budget would make every occurrence fail AFTER acceptance — reject at the gate
    for dry in (False, True):
        with pytest.raises(InvalidArgument, match="gather_chars"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "interval", "spec": {"seconds": 60}},
                budget={"gather_chars": "not-an-int"},
                dry_run=dry,
            )
        # F20: a truthy non-object budget is a structured rejection, not an AttributeError
        with pytest.raises(InvalidArgument, match="budget must be an object"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "interval", "spec": {"seconds": 60}},
                budget="generous",
                dry_run=dry,
            )


async def test_create_rejects_malformed_predicate_and_one_shot_combo(
    session, principal, sj_type
):
    # F21: the predicate grammar is gated at authoring; one_shot+predicate is rejected
    # until the runner supports it
    for dry in (False, True):
        with pytest.raises(InvalidArgument, match="predicate"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "cron", "spec": {"expr": "0 8 * * *"},
                         "predicate": {"field": "x"}},
                dry_run=dry,
            )
        with pytest.raises(InvalidArgument, match="one_shot"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "one_shot", "spec": {"at": "2030-01-01T09:00:00+00:00"},
                         "predicate": {"node": str(uuid.uuid4()), "op": "exists"}},
                dry_run=dry,
            )


async def test_create_accepts_valid_predicate(session, principal, space, sj_type):
    from assistant_memory.repository import graph as repo_graph

    flag = await repo_graph.create_node(
        session, type="Note", space_id=space.id, account_id=principal.account_id,
        properties={"flag": True},
    )
    res = await tools.create_scheduled_job(
        session, principal, instruction="gated",
        trigger={"kind": "cron", "spec": {"expr": "0 8 * * *"},
                 "predicate": {"all": [{"node": str(flag.id), "op": "exists",
                                        "path": "flag"}]}},
    )
    assert res["status"] == "ok"


async def test_create_rejects_predicate_referencing_invisible_node(
    session, principal, sj_type
):
    # F31 authoring half: a predicate gating on a node outside the job's scope is rejected
    with pytest.raises(InvalidArgument, match="not visible"):
        await tools.create_scheduled_job(
            session, principal, instruction="gated",
            trigger={"kind": "cron", "spec": {"expr": "0 8 * * *"},
                     "predicate": {"node": str(uuid.uuid4()), "op": "exists"}},
        )


async def test_runner_falsy_malformed_predicates_fail_durably(session, principal, sj_type):
    # F30 is runtime behavior but the shapes come from authoring history; the authoring
    # gate also rejects them now
    for bad in ([], "", 0):
        with pytest.raises(InvalidArgument, match="predicate"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "cron", "spec": {"expr": "0 8 * * *"}, "predicate": bad},
            )


async def test_create_rejects_nonstring_cron_expr(session, principal, sj_type):
    # F27: exotic cron expr types are named rejections, not raw AttributeError escapes
    for bad in (["0", "8"], 5, True, {"expr": "x"}):
        for dry in (False, True):
            with pytest.raises(InvalidArgument, match="spec.expr"):
                await tools.create_scheduled_job(
                    session, principal, instruction="x",
                    trigger={"kind": "cron", "spec": {"expr": bad}},
                    dry_run=dry,
                )


async def test_create_resolves_node_refs_in_gather_steps(session, principal, space, sj_type):
    # F28: a gather step pointed at an invisible node is rejected at normal create
    from assistant_memory.repository import graph as repo_graph

    with pytest.raises(InvalidArgument, match="not visible"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            tools=[{"tool": "get", "args": {"node_id": str(uuid.uuid4())}}],
        )
    visible = await repo_graph.create_node(
        session, type="Note", space_id=space.id, account_id=principal.account_id,
        properties={"text": "ref target"},
    )
    res = await tools.create_scheduled_job(
        session, principal, instruction="x",
        trigger={"kind": "interval", "spec": {"seconds": 60}},
        tools=[{"tool": "get", "args": {"node_id": str(visible.id)}}],
    )
    assert res["status"] == "ok"


async def test_create_rejects_malformed_iso_timestamps(session, principal, sj_type):
    # F24: a bad ISO datetime is a named InvalidArgument, not a raw ValueError escape
    for trigger in (
        {"kind": "one_shot", "spec": {"at": "tomorrow-ish"}},
        {"kind": "one_shot", "spec": {"at": 12345}},
        {"kind": "interval", "spec": {"seconds": 60, "start": "not-a-date"}},
    ):
        for dry in (False, True):
            with pytest.raises(InvalidArgument, match="ISO datetime"):
                await tools.create_scheduled_job(
                    session, principal, instruction="x", trigger=trigger, dry_run=dry
                )


def test_trigger_schema_documents_variants_and_predicate():
    # F32: the public schema exposes the enforced trigger/predicate contract
    from assistant_memory.mcp.server import SCHEMAS

    trigger_schema = SCHEMAS["create_scheduled_job"]["properties"]["trigger"]
    assert trigger_schema["properties"]["kind"]["enum"] == ["one_shot", "interval", "cron"]
    spec_props = trigger_schema["properties"]["spec"]["properties"]
    assert {"at", "seconds", "start", "expr"} <= set(spec_props)
    assert spec_props["seconds"]["minimum"] == 1
    assert "predicate" in trigger_schema["properties"]
    assert "one_shot+predicate is rejected" in trigger_schema["description"]


def test_delivery_schema_documents_shape():
    # F29: the public schema exposes the enforced delivery shape, not a generic object
    from assistant_memory.mcp.server import SCHEMAS

    delivery_schema = SCHEMAS["create_scheduled_job"]["properties"]["delivery"]
    assert set(delivery_schema["properties"]) == {"channel", "target"}
    assert delivery_schema["additionalProperties"] is False


def test_writes_schema_documents_canonical_forms():
    # F25: the public schema must expose the writes-scope shape, not a generic object
    from assistant_memory.mcp.server import SCHEMAS

    writes_schema = SCHEMAS["create_scheduled_job"]["properties"]["writes"]
    assert "oneOf" in writes_schema
    forms = writes_schema["oneOf"]
    assert any(f.get("type") == "array" for f in forms)
    assert any(f.get("type") == "object" and "types" in f.get("properties", {}) for f in forms)


async def test_create_gates_writes_fence_with_the_act_parser(session, principal, sj_type):
    # F23: the accepted declaration and the runtime fence cannot diverge
    for bad in ("Note", {"types": "Note"}, [""], [5], {"types": ["Note"], "foo": 1}, 42):
        for dry in (False, True):
            with pytest.raises(InvalidArgument, match="writes"):
                await tools.create_scheduled_job(
                    session, principal, instruction="x",
                    trigger={"kind": "interval", "spec": {"seconds": 60}},
                    writes=bad,
                    dry_run=dry,
                )
    # unregistered node type in the fence could never write successfully
    with pytest.raises(InvalidArgument, match="unregistered"):
        await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            writes=["NoSuchTypeEver"],
        )
    # both canonical forms pass ("Note" is seeded)
    for good in (["Note"], {"types": ["Note"]}):
        res = await tools.create_scheduled_job(
            session, principal, instruction="x",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            writes=good,
        )
        assert res["status"] == "ok"


async def test_create_rejects_malformed_delivery(session, principal, sj_type):
    # F22: delivery shape is gated (the runner reads .get('target') on it pre-ACT)
    for bad in ("op", {"target": 5}, {"chanel": "bot"}):
        with pytest.raises(InvalidArgument, match="delivery"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "interval", "spec": {"seconds": 60}},
                delivery=bad,
            )


async def test_create_rejects_nonobject_trigger_spec(session, principal, sj_type):
    # F19 authoring side: a truthy non-dict spec is a TriggerError, not a TypeError escape
    for dry in (False, True):
        with pytest.raises(InvalidArgument, match="spec must be an object"):
            await tools.create_scheduled_job(
                session, principal, instruction="x",
                trigger={"kind": "interval", "spec": "sixty"},
                dry_run=dry,
            )


async def test_dry_run_reasoner_transport_failure_is_structured(
    session, principal, sj_type, monkeypatch
):
    # F11: a backend/transport failure surfaces as a structured probe result, not an escape
    class _Broken:
        async def reason(self, context):
            raise RuntimeError("claude CLI not found on PATH")

    from assistant_memory.scheduler import reasoning

    monkeypatch.setattr(reasoning, "get_reasoner", lambda: _Broken())
    res = await tools.create_scheduled_job(
        session, principal, instruction="probe",
        trigger={"kind": "interval", "spec": {"seconds": 60}},
        dry_run=True,
    )
    assert res["status"] == "dry_run"
    assert res["plan"] is None
    assert "reason_transport" in res["plan_error"]
    assert "CLI not found" in res["plan_error"]


async def test_dry_run_misconfigured_reasoner_is_rejected(
    session, principal, sj_type, monkeypatch
):
    # F11: an unconstructible reasoner is a missing capability — named, not escaped
    monkeypatch.setattr(settings, "scheduler_reasoner", "strong")
    monkeypatch.setattr(settings, "strong_cli_token", "")
    with pytest.raises(InvalidArgument, match="misconfigured"):
        await tools.create_scheduled_job(
            session, principal, instruction="probe",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            dry_run=True,
        )


async def test_dry_run_still_validates_gather_steps(session, principal, sj_type):
    with pytest.raises(InvalidArgument, match="unknown tool"):
        await tools.create_scheduled_job(
            session, principal, instruction="probe",
            trigger={"kind": "interval", "spec": {"seconds": 60}},
            tools=[{"tool": "drop_table", "args": {}}],
            dry_run=True,
        )


async def test_set_enabled_rejects_non_job(session, principal, space, sj_type):
    from assistant_memory.mcp.errors import NotFound
    from assistant_memory.repository import graph

    note = await graph.create_node(
        session,
        type="Note",
        space_id=space.id,
        account_id=principal.account_id,
        properties={"text": "hi"},
    )
    with pytest.raises(InvalidArgument):
        await tools.set_scheduled_job_enabled(
            session, principal, node_id=str(note.id), enabled=False
        )
    with pytest.raises(NotFound):
        await tools.set_scheduled_job_enabled(
            session, principal, node_id=str(uuid.uuid4()), enabled=False
        )
