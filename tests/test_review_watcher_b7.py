# SPDX-License-Identifier: Apache-2.0
"""The watcher's half of B.7: gate-aware scheduling, pings, and liveness (H-1..H-4).

The pieces tested here are the ones whose failure is SILENT — a pass launched while the
operator is still marking the round up, a gate that opens and nobody hears, an environment
that dies and takes the error-reporting path with it. Each of those has a live incident
behind it, and each was previously the kind of logic that only a running service, a real
critic binary and a deliberately broken venv could exercise. They are pure functions here
for exactly that reason: a layer nobody calls looks like it works.
"""

import pytest

from assistant_memory.review import round_gate as rg
from assistant_memory.review import poll, watcher


def _m(seq, kind, payload=None, role="development", created_at=None):
    return {
        "seq": seq, "kind": kind, "role": role,
        "payload": payload or {}, "created_at": created_at,
    }


B7 = {"protocol": "B.7"}


def _round_open(extra=()):
    """A channel with a parked round gate: one security finding, proposed, unmarked."""
    return [
        _m(1, "artifact", {"mode": "spec", "bundle": {"spec_markdown": "# s"}}),
        _m(2, "findings", {
            "artifact_seq": 1,
            "items": [{"id": "s1", "finding_type": "security_mechanism",
                       "adversary": "a second contributor", "false_positive_cost": "high"}],
        }, role="critic"),
        _m(3, "proposals", {"entries": [{"finding_id": "s1", "proposed_outcome": "waive"}]}),
        *extra,
    ]


# --- H-1: the round gate owns the schedule -------------------------------


def test_no_pass_while_the_gate_is_open():
    """A critic pass launched while the operator is still marking the round up would spend
    an invocation on a version that is about to change — and would defeat the gate's whole
    purpose, which is that the veto costs zero spent work."""
    assert watcher.plan(_round_open(), B7)["action"] != "review"


@pytest.mark.parametrize("tail,expected_state", [
    ([], rg.PROPOSALS_OWED),
    ([_m(3, "proposals", {"entries": [{"finding_id": "f1", "proposed_outcome": "fix"}]})],
     rg.UNPARKED),
])
def test_no_pass_anywhere_inside_the_round_cycle(tail, expected_state):
    messages = [
        _m(1, "artifact", {"mode": "spec"}),
        _m(2, "findings", {"artifact_seq": 1,
                           "items": [{"id": "f1", "finding_type": "correctness"}]}, role="critic"),
        *tail,
    ]
    assert rg.compute(messages).state == expected_state
    assert watcher.plan(messages, B7)["action"] == "wait"


def test_round_closing_holds_the_next_pass_until_the_ledger_is_settled():
    """H-1's barrier: without it, artifact-before-disposition ordering makes a version
    eligible for a pass while its round's dispositions are still owed, and the critic
    reviews a version whose ledger is formally open."""
    messages = [
        _m(1, "artifact", {"mode": "spec"}),
        _m(2, "findings", {"artifact_seq": 1,
                           "items": [{"id": "f1", "finding_type": "correctness"}]}, role="critic"),
        _m(3, "proposals", {"entries": [{"finding_id": "f1", "proposed_outcome": "fix"}]}),
        _m(4, "artifact", {"mode": "spec"}),
    ]
    assert rg.compute(messages).state == rg.ROUND_CLOSING
    assert watcher.plan(messages, B7)["action"] == "wait"
    settled = [*messages, _m(5, "disposition", {"finding_id": "f1", "outcome": "fixed"})]
    assert watcher.plan(settled, B7)["action"] == "review"


def test_the_watcher_and_the_server_agree_on_who_is_gated():
    """An audience review is stamped B.7 at creation like any other and excluded from the
    gate by GENRE. When only the server knew that, the watcher folded the genre's untyped
    findings into `proposals_owed` and waited forever for proposals the server never wanted
    (critic finding `watcher-gates-audience-reviews`) — two copies of one predicate,
    disagreeing within an hour of being written."""
    audience = {"protocol": "B.7", "genre": "audience", "cold_verdict_first": False, "strategic_reader": False, "machine_comb": False}
    messages = [
        _m(1, "artifact", {"mode": "spec"}),
        _m(2, "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}, role="critic"),
        _m(3, "disposition", {"finding_id": "f1", "outcome": "fixed"}),
        _m(4, "artifact", {"mode": "spec"}),
    ]
    assert watcher.round_gate_in_force(audience) is False
    assert watcher.plan(messages, audience)["action"] == "review"
    # ...and the ordinary B.7 review is still gated.
    assert watcher.round_gate_in_force(B7) is True


def test_a_pre_b7_review_schedules_exactly_as_before():
    messages = [
        _m(1, "artifact", {"mode": "spec"}),
        _m(2, "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}, role="critic"),
        _m(3, "proposals", {"entries": [{"finding_id": "f1", "proposed_outcome": "fix"}]}),
    ]
    # No protocol marker: the gate is not in force and the old triggers decide alone.
    assert watcher.plan(messages, {})["action"] == "review"


def test_the_audit_pass_still_runs_after_an_operator_final():
    """F-4: an operator final stops DEFECT rounds only — the faithfulness audit of the
    post-review summary runs fully automatically afterwards, exactly as after a converged
    review. The fold sits in the terminal state forever once the operator stopped the
    review, and a scheduler that only launches from the two outside-a-round states leaves
    every operator-finalized review's audit silently unlaunched — the server accepts the
    summary and its audit findings, and the critic never comes."""
    messages = [
        _m(1, "artifact", {"mode": "spec"}),
        _m(2, "findings", {"artifact_seq": 1,
                           "items": [{"id": "f1", "finding_type": "correctness"}]},
           role="critic"),
        _m(3, "proposals", {"entries": [{"finding_id": "f1", "proposed_outcome": "waive"}]}),
        _m(4, "operator_finalize", {"mode": "now", "operator_quote": "стоп"}),
    ]
    # Right after the final, the latest artifact is an ORDINARY version the operator
    # deliberately left unreviewed: no pass may launch over it — that would be the defect
    # round the operator just ended.
    assert watcher.plan(messages, B7)["action"] == "wait"
    summary = _m(5, "artifact", {"intent_summary": True, "converged_artifact_seq": 1})
    plan = watcher.plan([*messages, summary], B7)
    assert plan["action"] == "review"
    assert plan["artifact_seq"] == 5


# --- H-2: the gate is never silent ---------------------------------------


def test_gate_ping_fires_once_per_round():
    """Per-round parking multiplies the known parked-invisibility failure by the round
    count; without a ping the design dies quietly. But a gate the operator has partially
    marked and left parked stays parked WITHOUT re-alarm."""
    messages = _round_open()
    decision = watcher.plan(messages, B7)
    assert decision["action"] == "gate_ping" and decision["round_seq"] == 3
    pinged = [*messages, _m(4, "notice", {"phase": "gate_ping", "round_seq": 3}, role="critic")]
    assert watcher.plan(pinged, B7)["action"] == "wait"


def test_an_operator_owed_review_is_surfaced_once_and_then_goes_quiet():
    """The server's OTHER door to the operator had no bell. A parked review is
    indistinguishable from patience: the stall alarm stays quiet because waiting on a human
    is by design, and the scheduler holds off because it must not race the operator.
    Measured 2026-08-10: 231 escalations landed at once and nothing made a sound."""
    messages = [
        _m(1, "artifact", {"mode": "spec"}),
        _m(2, "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}, role="critic"),
        _m(3, "escalation", {"kind": "contested_fork", "element_id": "coverage-row:a"},
           role="system"),
        _m(4, "escalation", {"kind": "contested_fork", "element_id": "coverage-row:b"},
           role="system"),
    ]
    decision = watcher.plan(messages, {})
    assert decision["action"] == "operator_ping" and decision["items_seq"] == 4
    pinged = [*messages, _m(5, "notice", {"phase": "operator_items_ping", "items_seq": 4},
                            role="critic")]
    assert watcher.plan(pinged, {})["action"] == "wait", "the same batch never re-alarms"
    # ...a LATER batch is a new arrival and rings again.
    more = [*pinged, _m(6, "escalation", {"kind": "contested_fork", "element_id": "x"},
                        role="system")]
    assert watcher.plan(more, {})["action"] == "operator_ping"


def test_the_gate_fallback_survives_a_watcher_restart():
    """Channel-5 round-1 finding `gate-fallback-lost-on-watcher-restart`: the fallback
    timer lived only in the watch process, so a recorded opening ping plus one restart
    read as 'obligation met' and the configured fallback was permanently skipped while
    the review sat parked. Everything needed to resume is in the journal."""
    from datetime import UTC, datetime

    ping = _m(4, "notice",
              {"phase": "gate_ping", "round_seq": 3, "role": "primary", "delivered": False},
              role="critic", created_at="2026-08-10T12:00:00+00:00")
    messages = _round_open(extra=[ping])
    now = datetime(2026, 8, 10, 12, 10, tzinfo=UTC)
    resumed = watcher.gate_fallback_resume(messages, rg.compute(messages), now=now)
    assert resumed == (3, 600.0), "a fresh process re-arms the timer with the waited time"
    # Once-only survives the restart in BOTH directions: a recorded fallback outcome —
    # fired or suppressed — ends the obligation...
    for outcome in (True, "suppressed"):
        resolved = [*messages, _m(5, "notice",
                                  {"phase": "gate_ping", "round_seq": 3, "fallback": outcome},
                                  role="critic")]
        assert watcher.gate_fallback_resume(resolved, rg.compute(resolved), now=now) is None
    # ...and so does the operator's authoritative response (W-2).
    answered = [*messages, _m(5, "gate_directive",
                              {"finding_id": "s1", "directive": "accept",
                               "operator_quote": "ага"})]
    assert watcher.gate_fallback_resume(answered, rg.compute(answered), now=now) is None
    # A notice with no readable timestamp arms from zero — one late ping, never none.
    undated = _round_open(extra=[_m(4, "notice", {"phase": "gate_ping", "round_seq": 3},
                                    role="critic")])
    assert watcher.gate_fallback_resume(undated, rg.compute(undated), now=now) == (3, 0.0)


@pytest.mark.parametrize("kind,expected", [
    ("gate_directive", "cancelled"),
    ("operator_finalize", "cancelled"),
    ("notice", "fire"),          # unrelated traffic must NOT cancel the fallback
    ("artifact", "fire"),
])
def test_only_the_authoritative_response_cancels_the_fallback(kind, expected):
    messages = [*_round_open(), _m(4, kind, {})]
    assert watcher.fallback_step(
        messages, round_seq=3, waited=999, delay=300, distinct_fallback=True
    ) == expected


def test_the_watcher_does_not_claim_a_push_it_cannot_send():
    """The operator's primary channel is a desktop+phone notification, which only the agent
    session can send. A sidecar that quietly rerouted it to the bot would report a delivery
    it never made — and the operator would stop trusting the journal, not the sidecar."""
    sent: list[tuple[str, str]] = []
    notify = watcher.dispatching_notifier(
        service=lambda channel, text: sent.append((channel, text)) or True,
        command=None,
    )
    assert notify("push", "hello") is False, "the watcher has no transport for push"
    assert sent == []
    assert notify("prod_bot", "hello") is True and sent[0][0] == "prod_bot"


def test_machinery_pings_take_a_channel_the_watcher_can_reach():
    """An environmental fault and a stall fire exactly when the development side may be
    gone — or may BE the thing that stalled — so they must not be addressed to a channel
    only that side can deliver."""
    cfg = {"primary": "push", "fallback": "prod_bot"}
    assert watcher.machine_ping_role(cfg, ("push",)) == "fallback"
    # With a push transport bound at launch, the watcher speaks for itself again.
    assert watcher.machine_ping_role(cfg, ()) == "primary"


def test_fallback_waits_for_its_delay_and_suppresses_a_duplicate_transport():
    messages = _round_open()
    assert watcher.fallback_step(
        messages, round_seq=3, waited=10, delay=300, distinct_fallback=True
    ) == "wait"
    # With no second transport bound, escalating into the same channel is noise, not
    # insurance — and saying so beats firing a duplicate.
    assert watcher.fallback_step(
        messages, round_seq=3, waited=999, delay=300, distinct_fallback=False
    ) == "suppressed"


# --- H-3: an environmental fault costs the review nothing ----------------


def test_fault_stretch_retries_then_drops_to_a_quiet_cadence():
    stretch = watcher.FaultStretch(open=True)
    first = watcher.fault_step(stretch, budget=3)
    assert first["action"] == "retry" and first["pause"] > 0
    stretch.attempts = first["attempts"]
    second = watcher.fault_step(stretch, budget=3)
    assert second["action"] == "retry" and second["pause"] >= first["pause"]
    stretch.attempts = second["attempts"]
    spent = watcher.fault_step(stretch, budget=3)
    assert spent["action"] == "slow_probe"
    # ...and there is NO permanent exhausted state: the slow probe keeps probing.
    stretch.attempts = spent["attempts"]
    assert watcher.fault_step(stretch, budget=3)["action"] == "slow_probe"


async def test_one_ping_per_stretch_even_when_the_probe_is_blind():
    """The storm regression. A probe-blind fault (the over-cap prompt is the measured one)
    used to reopen a fresh stretch — with a fresh ping — every cycle, because probe
    SUCCESS closed the stretch just before the invocation env-failed: a ping each ~30s,
    the budget never spent, the quiet cadence never reached, and the stretch's own notices
    growing the very prompt that caused the fault. Containment means: one ping at the
    stretch's start, each spent attempt noticed once, the drop to the quiet cadence
    noticed once, and later quiet cycles silent in the journal."""
    pings: list[str] = []
    notices: list[dict] = []
    sleeps: list[float] = []

    async def ping_machine(kind, detail, **extra):
        pings.append(kind)

    async def notice(phase, **payload):
        notices.append(payload)

    async def sleep(s):
        sleeps.append(s)

    stretch = watcher.FaultStretch()
    for _ in range(6):
        await watcher.contain_environmental_fault(
            watcher.EnvironmentFault("prompt over cap"), stretch, budget=3,
            ping_machine=ping_machine, notice=notice, sleep=sleep,
        )
    assert pings == ["environment"], "ONE ping per stretch, at its start"
    assert [n["stretch"] for n in notices] == ["retry", "retry", "slow_probe"], (
        "each spent attempt is an event; the quiet cadence is noticed once and then quiet"
    )
    assert sleeps[2:] == [watcher.SLOW_PROBE_SECONDS] * 4, "the cadence actually slows"


async def test_recovery_closes_the_stretch_once_and_the_next_fault_pings_again():
    notices: list[dict] = []

    async def notice(phase, **payload):
        notices.append(payload)

    async def ping_machine(kind, detail, **extra):
        notices.append({"ping": kind})

    async def sleep(s):
        pass

    stretch = watcher.FaultStretch(open=True, attempts=5, pinged=True, slow_probed=True)
    await watcher.close_fault_stretch(stretch, "pass completed", notice=notice)
    assert not stretch.open and stretch.attempts == 0 and not stretch.slow_probed
    assert [n.get("stretch") for n in notices] == ["recovered"]
    # closing a stretch that is not open says nothing — recovery is an event, not a state
    await watcher.close_fault_stretch(stretch, "pass completed", notice=notice)
    assert [n.get("stretch") for n in notices] == ["recovered"]
    # ...and a genuinely NEW fault after recovery is a new stretch, pinged again.
    await watcher.contain_environmental_fault(
        watcher.EnvironmentFault("venv torn again"), stretch, budget=3,
        ping_machine=ping_machine, notice=notice, sleep=sleep,
    )
    assert {"ping": "environment"} in notices


def test_probe_failure_is_environmental():
    probe = watcher.codex_probe("definitely-not-a-real-binary-9z exec -s read-only -", None, 5.0)
    with pytest.raises(watcher.EnvironmentFault):
        probe()


def test_the_default_probe_does_not_run_the_invocation(tmp_path):
    """Learned from a live run, minutes after the first version was written: the reference
    binding's first token is an operator-authored WRAPPER, which ignores a version flag and
    runs the real critic command with no prompt — so a `--version` probe failed on a healthy
    environment, every attempt, and blocked the review permanently. A pre-flight that cannot
    tell a broken environment from its own wrong assumption is worse than none."""
    marker = tmp_path / "ran"
    wrapper = tmp_path / "wrapper.cmd"
    wrapper.write_text(f"@echo off\r\necho x > {marker}\r\nexit /b 1\r\n", encoding="utf-8")
    probe = watcher.codex_probe(str(wrapper).replace("\\", "/") + " exec -", None, 5.0)
    assert "resolved" in probe(), "an existing wrapper resolves — nothing is spawned"
    assert not marker.exists(), "the default probe must not RUN the critic invocation"


def test_an_opt_in_probe_command_is_still_honoured(tmp_path):
    failing = tmp_path / "check.cmd"
    failing.write_text("@echo off\r\nexit /b 3\r\n", encoding="utf-8")
    probe = watcher.codex_probe(
        "cmd exec -", None, 10.0, probe_cmd=str(failing).replace("\\", "/")
    )
    with pytest.raises(watcher.EnvironmentFault) as e:
        probe()
    assert "rc=3" in str(e.value)


def test_oversized_prompt_is_refused_before_the_invocation():
    """The live incident: the prompt crossed the critic's 1.05M-character cap on version 42
    and the call was rejected after the whole prompt had been built and sent."""
    with pytest.raises(watcher.EnvironmentFault) as e:
        watcher._check_prompt_size("x" * 101, 100, "full pass")
    assert "over the configured critic input cap" in str(e.value)


async def test_environmental_fault_posts_no_needs_human():
    """0eb844fa: a critic that never started consumed an operator gate. The needs_human
    status is what did that, so the environmental path must not post one."""
    posted: list[dict] = []

    async def post(route, body):
        posted.append({"route": route, **body})
        return {}

    def invoke(_prompt):
        raise watcher.EnvironmentFault("critic binary vanished mid-review")

    messages = [_m(1, "artifact", {"mode": "code", "diff": "x"})]
    with pytest.raises(watcher.EnvironmentFault):
        await watcher.run_pass(messages, "prompt", invoke, post, mode="code", artifact_seq=1)
    kinds = [p.get("kind") for p in posted]
    assert "status" not in kinds, "an environmental fault must not end the pass"
    assert not any((p.get("payload") or {}).get("phase") == "watcher_error" for p in posted)


# --- H-4: the stall alarm is mechanics, not habit ------------------------


def test_stall_threshold_bootstraps_then_follows_the_review():
    assert poll.stall_threshold([], bootstrap=900) == 900
    # Three completed passes of ~100s each -> 2x the median.
    messages = []
    seq = 0
    for i in range(3):
        seq += 1
        messages.append(_m(seq, "artifact", {"mode": "spec"},
                           created_at=f"2026-08-09T10:{i * 10:02d}:00+00:00"))
        seq += 1
        messages.append(_m(seq, "status", {"value": "needs_iteration", "artifact_seq": seq - 1},
                           role="critic", created_at=f"2026-08-09T10:{i * 10 + 1:02d}:40+00:00"))
    assert poll.stall_threshold(messages, bootstrap=900) == pytest.approx(200, abs=1)


async def test_poll_idle_hook_runs_while_nothing_arrives():
    """The fallback ping exists for the case where the operator does NOT answer — a client
    that only woke on messages could never fire it."""
    idles: list[float] = []
    calls = {"n": 0}

    async def get(_after, _wait):
        calls["n"] += 1
        return [{"seq": 1}] if calls["n"] > 2 else []

    messages = await poll.poll_until_message(
        get, after=0, per_wait=0, on_idle=lambda elapsed: idles.append(elapsed)
    )
    assert messages and len(idles) == 2


# --- compaction of the new kinds (feedback 42a059ef) ---------------------


def test_superseded_round_traffic_is_compacted_from_birth():
    """An uncompacted new message kind is how the context overflow happens again — this is
    not a precaution: the rationale notices alone crossed the cap during B.7's own review."""
    long_plan = "p" * 5000
    messages = [
        _m(1, "artifact", {"mode": "spec", "bundle": {"spec_markdown": "# s"}}),
        _m(2, "proposals", {"entries": [
            {"finding_id": "f1", "proposed_outcome": "fix", "plan": long_plan,
             "reason": "r" * 5000, "context": "c" * 5000},
        ]}),
        _m(3, "class_report", {"directive_ref": "f1", "root": "old", "candidates": [],
                               "closure_kind": "lexical_only", "false_negative_mode": "n",
                               "evidence": "e" * 5000}),
        _m(4, "proposals", {"entries": [
            {"finding_id": "f2", "proposed_outcome": "fix", "plan": long_plan},
        ]}),
    ]
    out = watcher.compact_replay(messages)
    old = next(m for m in out if m["seq"] == 2)
    assert "plan" not in old["payload"]["entries"][0], "the superseded plan is elided"
    assert old["payload"]["entries"][0]["finding_id"] == "f1", "the DECISION survives"
    assert "superseded" in old["payload"]
    old_report = next(m for m in out if m["seq"] == 3)
    assert "evidence" not in old_report["payload"]
    assert old_report["payload"]["closure_kind"] == "lexical_only", "the claim survives"
    current = next(m for m in out if m["seq"] == 4)
    assert current["payload"]["entries"][0]["plan"] == long_plan, "the current round is whole"


# --- B.8 B-1: the compaction registry ------------------------------------------------


def test_every_wire_kind_declares_a_compaction_policy():
    """B-1: any message kind present in the channel schema but named in neither
    ``VERBATIM_KINDS`` nor ``COLLAPSE_TABLE`` MUST fail — an uncompacted-by-default new
    kind is how the replay overflow happens again (three overflows, one mechanism)."""
    classified = watcher.VERBATIM_KINDS | set(watcher.COLLAPSE_TABLE)
    unclassified = rg.WIRE_KINDS - classified
    assert not unclassified, (
        f"wire kinds with NO declared compaction policy: {sorted(unclassified)} — add each "
        "to watcher.VERBATIM_KINDS (kept verbatim) or watcher.COLLAPSE_TABLE (collapsed)"
    )
    stale = classified - rg.WIRE_KINDS
    assert not stale, (
        f"compaction registry names kinds absent from the wire contract: {sorted(stale)}"
    )
    both = watcher.VERBATIM_KINDS & set(watcher.COLLAPSE_TABLE)
    assert not both, f"kinds classified BOTH verbatim and collapsed: {sorted(both)}"


def test_unregistered_kind_is_collapsed_by_default_at_runtime():
    """B-1's runtime floor: a kind this watcher predates (a newer server) is collapsed to
    a stub, never carried verbatim into the prompt."""
    big = "x" * 50_000
    replay = [
        _m(1, "artifact", {"mode": "spec", "bundle": {"spec_markdown": "# s"}}),
        _m(2, "brand_new_kind", {"body": big}, role="critic"),
    ]
    out = watcher.compact_replay(replay)
    novel = next(m for m in out if m["kind"] == "brand_new_kind")
    assert "compacted" in novel["payload"]
    assert big not in str(novel["payload"])
    assert len(str(novel["payload"])) < 2000


# --- B.8 A-4/A-5/A-6: the bounded post repair and the refusal envelope ---------------


def _refusal(detail: str) -> RuntimeError:
    import json as _json
    return RuntimeError(
        "post messages rejected: 422 " + _json.dumps({"detail": detail})
    )


def test_repair_refiles_occupied_finding_id_without_closure_dispute():
    """A-4 repair 1: fresh id, occupied id named, `reopens_finding_id` NOT set."""
    body = {"role": "critic", "kind": "findings",
            "payload": {"artifact_seq": 1,
                        "items": [{"id": "f1", "finding_type": "correctness"},
                                  {"id": "f2", "finding_type": "drift"}]}}
    detail = ("finding id 'f1' already has a terminal disposition — a re-raise must use "
              "a fresh item `id` with `reopens_finding_id`, not the disposed id")
    repaired, changes, park = watcher._repair_post(body, detail)
    assert park is None
    ids = [f["id"] for f in repaired["payload"]["items"]]
    assert ids == ["f1-refiled", "f2"]
    refiled = repaired["payload"]["items"][0]
    assert "reopens_finding_id" not in refiled
    assert any("'f1'" in c for c in changes)
    # the original body is untouched (the watcher may still need it for the journal)
    assert body["payload"]["items"][0]["id"] == "f1"


def test_repair_drops_unmatched_rows_and_keeps_the_rest():
    """A-4 repair 2: named rows dropped, the rest re-posted; nothing invented."""
    body = {"role": "critic", "kind": "coverage_report",
            "payload": {"artifact_seq": 1, "manifest_id": "m",
                        "rows": [{"row_id": "aaa", "verdict": "reviewed-clean"},
                                 {"row_id": "bbb", "verdict": "reviewed-clean"}]}}
    detail = "coverage_report: row 'aaa' is not in manifest 'm'"
    repaired, changes, park = watcher._repair_post(body, detail)
    assert park is None
    assert [r["row_id"] for r in repaired["payload"]["rows"]] == ["bbb"]
    assert any("aaa" in c and "unreported" in c for c in changes)


def test_repair_quotes_own_text_into_observation_or_drops():
    """A-4 repair 3: quote the critic's own text if the pass output carries one; else
    drop — no verdict is invented."""
    quoted = {"row_id": "hs1", "verdict": "reviewed-clean", "note": "checked the guard"}
    bare = {"row_id": "hs2", "verdict": "reviewed-clean"}
    body = {"role": "critic", "kind": "coverage_report",
            "payload": {"artifact_seq": 1, "manifest_id": "m", "rows": [quoted, bare]}}
    detail = ("coverage_report: row 'hs1' is high-stakes: a `reviewed-clean` verdict must "
              "cite a specific checked `observation` (a bare clean claim is the cheapest "
              "claim there is); row 'hs2' is high-stakes: a `reviewed-clean` verdict must "
              "cite a specific checked `observation` (a bare clean claim is the cheapest "
              "claim there is)")
    repaired, changes, park = watcher._repair_post(body, detail)
    assert park is None
    rows = repaired["payload"]["rows"]
    assert [r["row_id"] for r in rows] == ["hs1"]
    assert rows[0]["observation"] == "checked the guard"
    assert any("hs2" in c and "dropped" in c for c in changes)


def test_repair_leaving_zero_rows_refuses_to_repost():
    """A-4: an empty report would claim 'coverage reported' with no verdict — park."""
    body = {"role": "critic", "kind": "coverage_report",
            "payload": {"artifact_seq": 1, "manifest_id": "m",
                        "rows": [{"row_id": "aaa", "verdict": "reviewed-clean"}]}}
    repaired, changes, park = watcher._repair_post(
        body, "coverage_report: row 'aaa' is not in manifest 'm'"
    )
    assert repaired is None
    assert park and "ZERO rows" in park


def test_undocumented_refusal_gets_no_repair():
    body = {"role": "critic", "kind": "coverage_report",
            "payload": {"artifact_seq": 1, "manifest_id": "m",
                        "rows": [{"row_id": "aaa", "verdict": "reviewed-clean"}]}}
    repaired, changes, park = watcher._repair_post(
        body, "coverage_report is anchored to artifact_seq 1 but the latest artifact is 2"
    )
    assert repaired is None
    assert "no documented repair shape" in park


async def test_post_results_repairs_once_and_journals(monkeypatch):
    """The refused findings batch is repaired, journalled (phase post_repair), re-posted."""
    posted = []
    refused = {"done": False}

    async def post(route, body):
        if (body.get("kind") == "findings" and not refused["done"]):
            refused["done"] = True
            raise _refusal(
                "finding id 'f1' already has a terminal disposition — a re-raise must "
                "use a fresh item `id` with `reopens_finding_id`, not the disposed id"
            )
        posted.append(body)
        return {}

    bodies = [{"role": "critic", "kind": "findings",
               "payload": {"artifact_seq": 1, "items": [{"id": "f1"}]}},
              {"role": "critic", "kind": "status",
               "payload": {"value": "needs_iteration", "artifact_seq": 1}}]
    await watcher._post_results(bodies, post, 1)
    kinds = [b["kind"] for b in posted]
    assert kinds == ["notice", "findings", "status"]
    assert posted[0]["payload"]["phase"] == watcher.POST_REPAIR_PHASE
    assert posted[1]["payload"]["items"][0]["id"] == "f1-refiled"


async def test_post_results_partial_acceptance_is_not_a_repair_trigger(capsys):
    """A 200 with rejected_rows re-posts NOTHING (the accepted content is recorded)."""
    posted = []

    async def post(route, body):
        posted.append(body)
        return {"rejected_rows": [{"row_id": "bad", "reason": "not in manifest"}]}

    bodies = [{"role": "critic", "kind": "coverage_report",
               "payload": {"artifact_seq": 1, "manifest_id": "m",
                           "rows": [{"row_id": "good", "verdict": "reviewed-clean"}]}}]
    await watcher._post_results(bodies, post, 1)
    assert [b["kind"] for b in posted] == ["coverage_report"]
    assert "not a repair trigger" in capsys.readouterr().err


async def test_post_results_refusal_parks_alive_in_the_h3_envelope():
    """A-5: an undocumented refusal raises PostRefusalFault (an EnvironmentFault) — the
    watcher parks alive with the pass owed instead of dying through needs_human."""
    async def post(route, body):
        raise _refusal("some entirely novel refusal the watcher has no repair for")

    bodies = [{"role": "critic", "kind": "findings",
               "payload": {"artifact_seq": 1, "items": [{"id": "f1"}]}}]
    with pytest.raises(watcher.PostRefusalFault) as exc:
        await watcher._post_results(bodies, post, 1)
    assert isinstance(exc.value, watcher.EnvironmentFault)


async def test_transport_fault_after_terminal_status_is_notice_only():
    """DoD 5 / incident a84e2f35: a fault in the tail of a CONCLUDED pass produces a
    notice and no needs_human — and does not raise."""
    posted = []

    async def post(route, body):
        if body.get("kind") == "notice" and (body["payload"].get("phase")
                                             == watcher.CRITIC_MEMO_PHASE):
            raise RuntimeError("server disconnected mid-tail")
        posted.append(body)
        return {}

    bodies = [{"role": "critic", "kind": "status",
               "payload": {"value": "needs_iteration", "artifact_seq": 1}},
              {"role": "critic", "kind": "notice",
               "payload": {"phase": watcher.CRITIC_MEMO_PHASE, "memo": "m"}}]
    await watcher._post_results(bodies, post, 1)  # must NOT raise
    kinds = [(b["kind"], (b.get("payload") or {}).get("phase")) for b in posted]
    assert ("status", None) in kinds
    assert ("notice", watcher.WATCHER_ERROR_PHASE) in kinds
    assert not any(b["kind"] == "status"
                   and b["payload"].get("value") == "needs_human" for b in posted)


async def test_error_handler_skips_needs_human_when_pass_concluded(tmp_path, monkeypatch):
    """A-6: a fresh channel read showing an ACCEPTED terminal status suppresses the
    error handler's needs_human — the fault becomes a notice only."""
    monkeypatch.chdir(tmp_path)
    posted = []

    async def post(route, body):
        posted.append((route, body))
        return {}

    def invoke(prompt):
        return "utter garbage"  # unparseable twice -> the generic error path

    msgs = [_m(1, "artifact", {"mode": "code"})]

    async def fetch_new():
        return [_m(2, "status", {"value": "needs_iteration", "artifact_seq": 1},
                   role="critic")]

    with pytest.raises(ValueError):
        await watcher.run_pass(msgs, "CRITIC", invoke, post, mode="code",
                               artifact_seq=1, fetch_new=fetch_new)
    kinds = [(b.get("kind"), (b.get("payload") or {}).get("value"))
             for _r, b in posted if _r == "messages"]
    assert ("status", "needs_human") not in kinds
    assert any(b.get("kind") == "notice"
               and (b.get("payload") or {}).get("phase") == watcher.WATCHER_ERROR_PHASE
               for _r, b in posted)


def test_kill_critic_children_kills_a_live_child_and_clears_the_registry():
    """B.8 C-3(c): no orphaned critic outlives the watcher (review f2f39623: a killed
    watcher left its codex child as a zombie holding a pass-slot appearance)."""
    import subprocess
    import sys as _sys
    import time

    group_kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if _sys.platform == "win32"
        else {"start_new_session": True}
    )
    child = subprocess.Popen(
        [_sys.executable, "-c", "import time; time.sleep(60)"], **group_kwargs
    )
    watcher._live_critic_children.add(child)
    watcher.kill_critic_children()
    deadline = time.time() + 10
    while child.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    assert child.poll() is not None, "the child must be dead after kill_critic_children"
    assert child not in watcher._live_critic_children


# --- B.8 Part F: declared grounds, the probe, typed evidence --------------------------


def test_declared_grounds_registry():
    assert rg.declared_grounds("spec", None) == ("repo", "graph", "docs")
    assert rg.declared_grounds("code", None) == ("repo", "spec-artifact")
    assert rg.declared_grounds("spec", "audience") == ("repo", "graph")
    # the visual genre names no grounds today -> refuses to run (fail-closed)
    assert rg.declared_grounds("spec", "visual") is None


def test_probe_grounds_fails_hard_without_an_override():
    def boom():
        raise RuntimeError("sandbox dead")

    degraded, hard = watcher.probe_grounds(
        ("repo", "graph"), {"repo": boom, "graph": lambda: "ok"}, set()
    )
    assert degraded == []
    assert [g for g, _ in hard] == ["repo"]


def test_probe_grounds_goes_degraded_under_a_live_override():
    def boom():
        raise RuntimeError("sandbox dead")

    degraded, hard = watcher.probe_grounds(
        ("repo", "graph"), {"repo": boom, "graph": lambda: "ok"}, {"repo"}
    )
    assert degraded == ["repo"] and hard == []


def test_probe_grounds_missing_probe_is_fail_closed():
    degraded, hard = watcher.probe_grounds(("graph",), {}, set())
    assert degraded == [] and [g for g, _ in hard] == ["graph"]
    assert "no probe is wired" in hard[0][1]


def test_live_ground_overrides_reads_only_operator_notices():
    replay = [
        _m(1, "notice", {"phase": "grounds_override", "ground": "repo", "reason": "r"},
           role="operator"),
        _m(2, "notice", {"phase": "grounds_override", "ground": "graph", "reason": "r"},
           role="development"),  # not the operator's -> does not bind
    ]
    assert watcher.live_ground_overrides(replay) == {"repo"}


def _verdict_with_evidence(evidence, read_log):
    return {
        "findings": {"items": [], "artifact_seq": 1},
        "status": {"value": "needs_iteration", "artifact_seq": 1},
        "read_log": read_log,
        "coverage_report": {
            "artifact_seq": 1, "manifest_id": "m",
            "rows": [{"row_id": "hs1", "verdict": "reviewed-clean",
                      "observation": "checked", "evidence": evidence}],
        },
    }


def test_evidence_quote_must_match_what_the_read_served():
    log = [{"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc",
            "range": [1, 2]}]
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "return 42"}, log
    )
    downgrades = watcher.verify_report_evidence(
        verdict, lambda entry: "def f():\n    return 41\n", ()
    )
    assert [d["row_id"] for d in downgrades] == ["hs1"]
    row = verdict["coverage_report"]["rows"][0]
    assert row["verdict"] == "not-reached"
    assert row["reason"].startswith("instrument_failure")


def test_evidence_matching_quote_passes():
    log = [{"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc",
            "range": [1, 2]}]
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "return 42"}, log
    )
    downgrades = watcher.verify_report_evidence(
        verdict, lambda entry: "def f():\n    return 42\n", ()
    )
    assert downgrades == []
    assert verdict["coverage_report"]["rows"][0]["verdict"] == "reviewed-clean"


def test_evidence_ref_must_name_a_read_of_this_run():
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r9", "quote": "x"}, []
    )
    downgrades = watcher.verify_report_evidence(verdict, lambda e: "x", ())
    assert downgrades and "no entry of this run's read log" in downgrades[0]["reason"]


def test_confirmations_against_a_degraded_ground_are_barred():
    log = [{"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc",
            "range": [1, 1]}]
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "x"}, log
    )
    downgrades = watcher.verify_report_evidence(verdict, lambda e: "x", ("repo",))
    assert downgrades and "grounds_inline" in downgrades[0]["reason"]


# --- b8-evidence-read-log-unbound: the ENTRY the evidence cites is validated too ------


def test_a_fabricated_read_log_entry_is_downgraded():
    """An entry with no coordinates is a claim of a read, not a record of one — it was
    previously trusted as-is, so a fabricated `{"id": "r1", "ground": "graph"}` passed a
    graph confirmation without any read having happened."""
    verdict = _verdict_with_evidence(
        {"ground": "graph", "read_ref": "r1", "node_id": "n1", "version": 3},
        [{"id": "r1", "ground": "graph"}],
    )
    downgrades = watcher.verify_report_evidence(verdict, None, ())
    assert downgrades and "no locator" in downgrades[0]["reason"]
    assert verdict["coverage_report"]["rows"][0]["verdict"] == "not-reached"


def test_graph_evidence_must_cite_a_read_of_the_named_node():
    """The graph's content cannot be re-read from the watcher (the recorded boundary),
    but the cited read must at least be a read OF the node and version the evidence
    claims — a mismatch means the reference proves nothing about the confirmation."""
    verdict = _verdict_with_evidence(
        {"ground": "graph", "read_ref": "r1", "node_id": "n1", "version": 3},
        [{"id": "r1", "ground": "graph", "locator": "n-OTHER", "version": 3}],
    )
    downgrades = watcher.verify_report_evidence(verdict, None, ())
    assert downgrades and "n-OTHER" in downgrades[0]["reason"]


def test_graph_evidence_with_agreeing_coordinates_passes_marked():
    """Accepted — and SAYING what was checked (operator ruling, round 2 of this code's
    own review): the tract cannot re-read graph content, so the row carries an explicit
    form-and-coordinates-only mark instead of looking verified to repo strength."""
    verdict = _verdict_with_evidence(
        {"ground": "graph", "read_ref": "r1", "node_id": "n1", "version": 3},
        [{"id": "r1", "ground": "graph", "locator": "n1", "version": 3}],
    )
    assert watcher.verify_report_evidence(verdict, None, ()) == []
    row = verdict["coverage_report"]["rows"][0]
    assert row["verdict"] == "reviewed-clean"
    assert "form_and_coordinates_only" in row["evidence_verification"]


def test_repo_evidence_without_a_repo_to_read_is_marked_not_silently_trusted():
    """Same criterion, not same ground-name: a spec-mode binding with no repo root skips
    the quote re-read, and that unverifiable acceptance must say so too."""
    log = [{"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc",
            "range": [1, 2]}]
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "return 42"}, log
    )
    assert watcher.verify_report_evidence(verdict, None, ()) == []
    assert "form_and_coordinates_only" in (
        verdict["coverage_report"]["rows"][0]["evidence_verification"]
    )


def test_a_repo_row_verified_against_a_real_read_is_marked_as_verified():
    """The server validates form only (it cannot re-read the repo under review), so the
    watcher says on the record which rows it actually held against a real read — a
    verified row and a merely well-shaped one must not look alike on the channel
    (finding b8-direct-coverage-evidence-bypasses-watcher-verification)."""
    log = [{"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc",
            "range": [1, 2]}]
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "return 42"}, log
    )
    assert watcher.verify_report_evidence(
        verdict, lambda e: "def f():\n    return 42\n", ()
    ) == []
    mark = verdict["coverage_report"]["rows"][0]["evidence_verification"]
    assert mark.startswith("content_reread_by_watcher") and "src/x.py" in mark


@pytest.mark.parametrize("rng", [None, [1], ["1", "2"], [0, 5], [7, 3], [True, 2]])
def test_repo_read_without_a_valid_range_is_downgraded_not_widened(rng):
    """A missing or malformed range used to make the re-read serve the WHOLE file, so a
    quote from anywhere in it passed as a quote from the named read. Fail closed: the
    verdict is downgraded with the range named as the problem, and the (whole-file)
    read_source is never consulted."""
    entry = {"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc"}
    if rng is not None:
        entry["range"] = rng
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "somewhere in the file"}, [entry]
    )
    downgrades = watcher.verify_report_evidence(
        verdict, lambda e: "somewhere in the file", ()
    )
    assert downgrades and "range" in downgrades[0]["reason"]


def test_git_read_source_refuses_a_malformed_range_rather_than_serving_the_file():
    """The belt behind the validator: even if an entry reaches the re-read with a broken
    range, the read serves nothing instead of the whole file."""
    read = watcher.git_read_source(".")
    assert read is not None
    served = read({"locator": "pyproject.toml", "version": "HEAD", "range": [0, 10]})
    assert served is None


# --- b8-ground-probe-bypass: the probe kill-switch flag is gone -----------------------


def test_the_no_ground_probes_flag_is_removed():
    """F-1/F-3's paths are exhaustive: probe passes, or the operator's RECORDED override
    degrades the pass, or the run refuses. A local CLI kill-switch was a second,
    unrecorded bypass — argparse must not know it anymore."""
    with pytest.raises(SystemExit):
        watcher.main([
            "--base-url", "http://x", "--review-id", "r", "--token-file", "t",
            "--codex-cmd", "codex exec -s read-only -", "--no-ground-probes",
        ])


# --- B.10 Part C: the projection rides the replay's diet (C-1/C-2/C-3) ----------------
#
# The defect this section guards: B.8 taught the PROMPT path to elide superseded bodies;
# B.9's file projection shipped uncompacted on the assumption "the file has no token
# cost" — measured false in review b5fd70de (1.4MB projection, the coverage
# re-verification pass stopped fitting). The projection now calls the SAME elision
# functions, parameterized: a CURRENT body is never replaced there.

import json as _json


def _projection_messages(msgs):
    return _json.loads(watcher.render_projection(msgs))["messages"]


def _spec_v(seq, body):
    return _m(seq, "artifact",
              {"mode": "spec",
               "bundle": {"spec_markdown": body,
                          "elements": [{"id": "e1", "text": body}]}})


def _spec_ref_v(seq, commit="c" * 40):
    """A referential spec artifact: subject = ref + path, NO inline bundle (B.10 B-1)."""
    return _m(seq, "artifact",
              {"mode": "spec", "artifact_ref": {"base": "b" * 40, "commit": commit},
               "path": "docs/design/spec.md"})


def test_projection_elides_superseded_bodies_and_collapses_coverage_like_the_replay():
    """C-1: same functions, same stubs — the projection and the prompt replay render
    ONE compaction by construction; the current version stays whole in both."""
    msgs = [
        _spec_v(1, "BODY-V1"),
        _m(2, "coverage_manifest",
           {"artifact_seq": 1, "manifest_id": "m1", "granularity": "element",
            "rows": [{"row_id": "r1", "locator": "spec:e1"}]}),
        _m(3, "coverage_report",
           {"artifact_seq": 1, "manifest_id": "m1",
            "rows": [{"row_id": "r1", "verdict": "reviewed-clean"}]}, role="critic"),
        _spec_v(4, "BODY-V2"),
        _spec_v(6, "BODY-V3"),
    ]
    proj = _projection_messages(msgs)
    replay = watcher.compact_replay(msgs)
    # parity: superseded spec bodies carry the identical stub text in both consumers
    stub = "[superseded — см. seq 6]"
    for out in (proj, replay):
        assert out[0]["payload"]["bundle"]["spec_markdown"] == stub
        assert out[3]["payload"]["bundle"]["spec_markdown"] == stub
        assert out[4]["payload"]["bundle"]["spec_markdown"] == "BODY-V3"
    # superseded coverage collapses in the projection exactly as in the replay
    assert proj[1]["payload"]["row_count"] == 1
    assert proj[1]["payload"]["rows"] != [{"row_id": "r1", "locator": "spec:e1"}]
    assert proj[2]["payload"]["counts"] == {"reviewed-clean": 1}


def test_projection_never_replaces_the_current_body_even_an_oversized_legacy_diff():
    """C-1 (finding b10-projection-current-legacy-diff-conflict): the prompt path
    substitutes a CURRENT oversized inline diff with a git-read instruction; the
    projection invocation has that substitution DISABLED — a current body is never
    replaced in the file, whatever its size."""
    big = "x" * (watcher.CURRENT_DIFF_LIMIT + 1)
    msgs = [
        _m(1, "artifact", {"mode": "code", "diff": "old",
                           "artifact_ref": {"base": "b1", "commit": "c1"}}),
        _m(2, "artifact", {"mode": "code", "diff": big,
                           "artifact_ref": {"base": "b1", "commit": "c2"}}),
    ]
    # prompt path: substituted (the existing B.8 behavior, regression-pinned here)
    replay = watcher.compact_replay(msgs)
    assert "git diff b1..c2" in replay[1]["payload"]["diff"]
    # projection: the current diff is carried WHOLE; the superseded one is stubbed
    proj = _projection_messages(msgs)
    assert proj[1]["payload"]["diff"] == big
    assert proj[0]["payload"]["diff"].startswith("[superseded")


def test_projection_does_not_inject_a_git_read_instruction_into_a_ref_only_current():
    """The git-read instruction is prompt-path guidance; injecting it into the
    projection would fabricate a field the poster never sent (channel fidelity)."""
    msgs = [
        _m(1, "artifact", {"mode": "code", "diff": "old",
                           "artifact_ref": {"base": "b", "commit": "c1"}}),
        _m(2, "artifact", {"mode": "code",
                           "artifact_ref": {"base": "b", "commit": "c2"}}),
    ]
    replay = watcher.compact_replay(msgs)
    assert "git diff b..c2" in replay[1]["payload"]["diff"]  # prompt path: injected
    proj = _projection_messages(msgs)
    assert "diff" not in proj[1]["payload"]  # projection: fidelity, nothing invented
    assert proj[0]["payload"]["diff"].startswith("[superseded")  # elision still applies


def test_spec_supersession_form_transition_matrix():
    """C-1's REQUIRED matrix: supersession is defined by version recency, never by body
    presence (finding referential-spec-inline-predecessor-unelided) — all four form
    transitions of the shared elision path."""
    stub_of = watcher._superseded_stub

    # inline -> inline: the older body is stubbed toward the newest spec version
    out = watcher.compact_replay([_spec_v(1, "OLD"), _spec_v(3, "NEW")])
    assert out[0]["payload"]["bundle"]["spec_markdown"] == stub_of(3)
    assert out[1]["payload"]["bundle"]["spec_markdown"] == "NEW"

    # inline -> referential: the KEY case — a bundle-presence predicate would keep the
    # old inline body forever; version recency stubs it toward the referential version
    out = watcher.compact_replay([_spec_v(1, "OLD"), _spec_ref_v(3)])
    assert out[0]["payload"]["bundle"]["spec_markdown"] == stub_of(3)
    assert out[0]["payload"]["bundle"]["elements"] == stub_of(3)
    assert out[1]["payload"]["artifact_ref"]["commit"] == "c" * 40  # ref rides whole

    # referential -> inline: the ref version has no body to elide; metadata survives,
    # the newest inline body is whole
    out = watcher.compact_replay([_spec_ref_v(1, commit="d" * 40), _spec_v(3, "NEW")])
    assert out[0]["payload"]["artifact_ref"]["commit"] == "d" * 40
    assert "bundle" not in out[0]["payload"]
    assert out[1]["payload"]["bundle"]["spec_markdown"] == "NEW"

    # referential -> referential: nothing to elide anywhere
    msgs = [_spec_ref_v(1, commit="d" * 40), _spec_ref_v(3)]
    assert watcher.compact_replay(msgs) == msgs


def test_projection_stub_envelope_addresses_the_original():
    """C-2/C-3: the original's address is the stubbed message's OWN envelope — seq,
    role and kind survive in the projection, and fetching that seq from the channel
    returns the full original body; the stub TEXT names the current seq."""
    msgs = [_spec_v(1, "BODY-V1"), _spec_v(4, "BODY-V2"), _spec_v(7, "BODY-V3")]
    proj = _projection_messages(msgs)
    by_seq = {m["seq"]: m for m in msgs}
    for stubbed in proj[:2]:
        # envelope preserved: the address into the channel
        original = by_seq[stubbed["seq"]]
        assert (stubbed["role"], stubbed["kind"]) == (original["role"], original["kind"])
        # the channel still serves the full original at that address (append-only)
        assert original["payload"]["bundle"]["spec_markdown"].startswith("BODY-")
        # the stub text names the CURRENT seq (where the live subject is)
        assert stubbed["payload"]["bundle"]["spec_markdown"] == "[superseded — см. seq 7]"


def test_projection_with_three_versions_carries_no_duplicated_full_bodies():
    """E-1.2's mechanical check, as a unit: superseded bodies appear as stubs only —
    the projection's size is dominated by its current version, not by copies."""
    v1, v2, v3 = "UNIQUE-BODY-ONE", "UNIQUE-BODY-TWO", "UNIQUE-BODY-THREE"
    text = watcher.render_projection([_spec_v(1, v1), _spec_v(3, v2), _spec_v(5, v3)])
    assert text.count(v1) == 0 and text.count(v2) == 0
    assert text.count(v3) == 2  # spec_markdown + its element text — the current version


def test_code_mode_downgrade_lands_in_the_typed_verdict():
    """B.14 B-3/B-6: under a code-mode denominator the failed-evidence downgrade IS the
    typed `instrument_failure` outcome (reason bare) — `not-reached` is retired there
    and the server would refuse the spec-mode shape."""
    log = [{"id": "r1", "ground": "repo", "locator": "src/x.py", "version": "abc",
            "range": [1, 2]}]
    verdict = _verdict_with_evidence(
        {"ground": "repo", "read_ref": "r1", "quote": "return 42"}, log
    )
    downgrades = watcher.verify_report_evidence(
        verdict, lambda entry: "def f():\n    return 41\n", (), code_mode=True
    )
    assert [d["row_id"] for d in downgrades] == ["hs1"]
    row = verdict["coverage_report"]["rows"][0]
    assert row["verdict"] == "instrument_failure"
    assert "instrument_failure" not in row["reason"], "the class is the verdict, not a prefix"
