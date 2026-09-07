# SPDX-License-Identifier: Apache-2.0
"""Headless critic watcher — pure-core unit tests (no service, no codex binary).

Covers: pass planning (new artifact / already reviewed / parked on operator),
rationale redaction for the cold pass, output parsing (fenced / bare / retry-worthy
garbage), verdict -> channel-message translation (memo included), and the
two-phase run_pass flow in spec vs code mode with injected invoke/post.
"""

import json

import pytest

from assistant_memory.review import watcher


def _msg(seq, role, kind, payload=None):
    return {"seq": seq, "role": role, "kind": kind, "payload": payload or {}}


# --- plan ------------------------------------------------------------------


def test_plan_reviews_new_artifact():
    msgs = [_msg(1, "development", "artifact", {"mode": "code"})]
    decision = watcher.plan(msgs)
    assert decision == {
        "action": "review",
        "artifact_seq": 1,
        "mode": "code",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }


def test_plan_waits_when_already_reviewed_and_reviews_new_version():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}),
        _msg(3, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}),
    ]
    assert watcher.plan(msgs)["action"] == "wait"
    msgs.append(_msg(4, "development", "disposition", {"finding_id": "f1", "outcome": "fixed"}))
    msgs.append(_msg(5, "development", "artifact", {"mode": "spec"}))
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 5,
        "mode": "spec",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }


def test_a_pass_that_dies_before_its_status_is_not_coverage_for_any_trigger():
    """THE MIGRATION TEST — the axis is "did the pass END", and every watcher trigger that
    asks a question of that shape must answer it the same way.

    Each of these used to derive the boundary privately from whichever raw message kind was
    in front of it, and they disagreed. A pass that posts its evidence and then dies is not
    hypothetical: a transport failure killed one between its report and its status on
    2026-07-22, and the watcher could not even post its own error. Left unswept, each
    trigger produces the same stall in its own way, one round apart.
    """
    # 1. findings without a status -> the version is NOT reviewed, so the pass is re-planned
    dead = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
    ]
    assert watcher.last_reviewed_seq(dead) == 0
    assert watcher.plan(dead)["action"] == "review"
    ended = dead + [_msg(3, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1})]
    assert watcher.last_reviewed_seq(ended) == 1
    assert watcher.plan(ended)["action"] == "wait"

    # 2. a report without a status does not answer the denominator...
    owed = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, [], "m1")),
        _msg(3, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(4, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1", "rows": []}),
    ]
    assert watcher.unanswered_manifest(owed) is True
    assert watcher.unanswered_manifest(
        owed + [_msg(5, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1})]
    ) is False

    # 3. ...and does not spend the sweep the server routed
    swept = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1", "rows": []}),
        _msg(4, "critic", "status", {"value": "converged", "artifact_seq": 1}),
        _msg(5, "system", "notice", {"phase": "convergence_blocked", "artifact_seq": 1,
                                     "unreached_rows": ["r1"]}),
        _msg(6, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1", "rows": []}),
    ]
    assert watcher.coverage_repass_due(swept) is True
    assert watcher.coverage_repass_due(
        swept + [_msg(7, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1})]
    ) is False


def test_plan_announces_an_owed_manifest_instead_of_burning_a_pass():
    """Once coverage is in play, a version with no manifest is DEVELOPMENT's ball. Scheduling
    a critic pass here burned an invocation the server then refused — and the refusal reached
    the critic's watcher, the one role that cannot post a denominator. Announce once, then
    wait: silence is what stranded the review, because no version-based trigger can see it."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "development", "coverage_manifest", {"artifact_seq": 1, "manifest_id": "m1"}),
        _msg(3, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1"}),
        _msg(4, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(5, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}),
        _msg(6, "development", "artifact", {"mode": "code"}),  # no manifest of its own
    ]
    assert watcher.plan(msgs) == {"action": "await_manifest", "artifact_seq": 6}

    # announced -> wait, not re-announce: a poll loop that repeated itself would bury the
    # channel it is trying to route
    msgs.append(_msg(7, "critic", "notice",
                     {"phase": watcher.MANIFEST_OWED_PHASE, "artifact_seq": 6}))
    assert watcher.plan(msgs)["action"] == "wait"

    # and the moment development supplies it, the ordinary pass is scheduled again
    msgs.append(_msg(8, "development", "coverage_manifest",
                     {"artifact_seq": 6, "manifest_id": "m2"}))
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 6,
        "mode": "code",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }


def test_plan_never_demands_a_manifest_of_an_intent_summary():
    """The summary is audited against the converged version, not reached — gating it would put
    the operator's handoff behind machinery that must not apply to it."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "development", "coverage_manifest", {"artifact_seq": 1, "manifest_id": "m1"}),
        _msg(3, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1"}),
        _msg(4, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(5, "critic", "status", {"value": "converged", "artifact_seq": 1}),
        _msg(6, "development", "artifact",
             {"intent_summary": True, "converged_artifact_seq": 1}),
    ]
    assert watcher.plan(msgs)["action"] == "review"


def test_plan_parks_on_open_operator_items():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "critic", "escalation",
             {"element_id": "op2", "kind": "operator_decision_challenge"}),
        _msg(3, "development", "artifact", {"mode": "spec"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass  # escalation unanswered
    msgs.append(_msg(4, "operator", "decision_response", {"element_id": "op2", "held": True}))
    assert watcher.plan(msgs)["action"] == "review"


def test_plan_parks_on_external_defect_until_escalation_id_answer():
    """The watcher must mirror the server's external_defect keying (J.8): an
    external_defect escalation is closed only by a decision_response carrying
    `escalation_id` — a generic answer (element_id/finding_id) must NOT clear it, or the
    critic would never review the next artifact after the operator resolves the defect."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "critic", "escalation",
             {"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
              "claim": "bug", "evidence": "obs"}),
        _msg(3, "development", "artifact", {"mode": "code"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass  # external_defect unanswered
    # a same-string answer under the wrong field must not close it
    msgs.append(_msg(4, "operator", "decision_response", {"finding_id": "e1", "held": True}))
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass
    # the correct escalation_id answer closes it -> the newer artifact is reviewed
    msgs.append(_msg(5, "operator", "decision_response", {"escalation_id": "e1", "action": "defer"}))
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 3,
        "mode": "code",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }


def test_plan_needs_human_cleared_by_any_later_response():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "critic", "status", {"value": "needs_human", "artifact_seq": 1}),
        _msg(3, "development", "artifact", {"mode": "spec"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass
    msgs.append(_msg(4, "operator", "decision_response", {"element_id": "x"}))
    assert watcher.plan(msgs)["action"] == "review"


def test_plan_schedules_audit_pass_after_convergence():
    # the planner is STATE-BLIND (message-driven): an intent_summary artifact posted
    # while the review sits in `converged` is scheduled like any newer artifact
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "critic", "status", {"value": "converged", "artifact_seq": 1}),
        _msg(4, "development", "artifact",
             {"mode": "spec", "intent_summary": True, "converged_artifact_seq": 1}),
    ]
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 4,
        "mode": "spec",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        # B.8 E-1: the plan SAYS the target is a summary, so the pass layer never
        # spends a cold pass on it in any mode
        "intent_summary": True,
    }


def test_operator_keys_namespaced_by_type():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "development", "human_question", {"id": "x1", "question": "?"}),
        _msg(3, "development", "artifact", {"mode": "spec"}),
        # a FINDING answer with the same string must not close the QUESTION
        _msg(4, "operator", "decision_response", {"finding_id": "x1"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass
    msgs.append(_msg(5, "operator", "decision_response", {"question_id": "x1"}))
    assert watcher.plan(msgs)["action"] == "review"


def test_human_question_closed_only_by_matching_answer():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "development", "human_question", {"id": "q1", "question": "which way?"}),
        _msg(3, "development", "artifact", {"mode": "spec"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass
    # an answer to something ELSE does not clear the keyed question
    msgs.append(_msg(4, "operator", "decision_response", {"element_id": "unrelated"}))
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass
    msgs.append(_msg(5, "operator", "decision_response", {"question_id": "q1"}))
    assert watcher.plan(msgs)["action"] == "review"


# --- redaction / prompt ------------------------------------------------------


def test_projection_visible_drops_only_rationale_notices():
    """B-1's one-line rule: development explanations never render; everything else —
    operator messages, memos, dev observations — always does."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "development", "notice", {"phase": "rationale", "rationales": {"j1": "..."}}),
        _msg(3, "critic", "notice", {"phase": "critic_memo", "memo": "keep"}),
        _msg(4, "operator", "notice", {"phase": "operator_ruling", "text": "frame"}),
        _msg(5, "development", "notice",
             {"phase": "dev_observation", "observation_id": "obs-1", "text": "stray"}),
    ]
    visible = watcher.projection_visible(msgs)
    assert [m["seq"] for m in visible] == [1, 3, 4, 5]


def test_render_projection_is_deterministic_and_marked_generated():
    """A-1: same channel -> same bytes (no timestamps), and the file says what it is."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "development", "notice", {"phase": "rationale", "rationales": {"j1": "X"}}),
    ]
    text = watcher.render_projection(msgs)
    assert text == watcher.render_projection(list(msgs))
    assert "DERIVED" in text
    assert '"rationales"' not in text  # the B-1 exclusion holds in the file itself


# --- compaction (Incident 8ba4172b: long-review replay must stay bounded) -----


def _bundle_artifact(seq, body, elements=None):
    return _msg(seq, "development", "artifact",
                {"mode": "spec",
                 "bundle": {"spec_markdown": body,
                            "elements": elements or [{"id": "e1", "text": body}],
                            "focus": ["e1"]}})


def test_compact_replay_stubs_superseded_bundle_bodies_keeps_newest():
    msgs = [
        _bundle_artifact(1, "BODY-V1"),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}),
        _bundle_artifact(3, "BODY-V2"),
        _bundle_artifact(5, "BODY-V3"),
    ]
    out = watcher.compact_replay(msgs)
    assert [m["seq"] for m in out] == [1, 2, 3, 5]
    stub = "[superseded — см. seq 5]"
    assert out[0]["payload"]["bundle"]["spec_markdown"] == stub
    assert out[0]["payload"]["bundle"]["elements"] == stub
    assert out[0]["payload"]["bundle"]["focus"] == ["e1"]  # non-body keys survive
    assert out[2]["payload"]["bundle"]["spec_markdown"] == stub
    # the version under review is complete
    assert out[3]["payload"]["bundle"]["spec_markdown"] == "BODY-V3"
    assert out[3]["payload"]["bundle"]["elements"][0]["text"] == "BODY-V3"


def test_compact_replay_keeps_decision_history_verbatim():
    history = [
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1", "claim": "x"}]}),
        _msg(3, "development", "disposition",
             {"finding_id": "f1", "outcome": "waived", "reason": "long reason"}),
        _msg(4, "critic", "escalation", {"kind": "contested_disposition", "finding_id": "f1"}),
        _msg(5, "operator", "decision_response", {"finding_id": "f1", "held": True}),
        _msg(6, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}),
        _msg(7, "critic", "notice", {"phase": "critic_memo", "memo": "revisit f1"}),
        _msg(8, "development", "notice", {"phase": "rationale", "rationales": {"j1": "r"}}),
    ]
    msgs = [_bundle_artifact(1, "BODY-V1"), *history, _bundle_artifact(9, "BODY-V2")]
    out = watcher.compact_replay(msgs)
    assert out[1:-1] == history  # every non-artifact message passes through untouched


def test_compact_replay_stubs_older_intent_summary_only():
    msgs = [
        _bundle_artifact(1, "BODY-V1"),
        _msg(2, "development", "artifact",
             {"mode": "spec", "intent_summary": True, "converged_artifact_seq": 1,
              "summary_markdown": "SUMMARY-OLD"}),
        _msg(3, "development", "artifact",
             {"mode": "spec", "intent_summary": True, "converged_artifact_seq": 1,
              "summary_markdown": "SUMMARY-NEW"}),
    ]
    out = watcher.compact_replay(msgs)
    assert out[1]["payload"]["summary_markdown"] == "[superseded — см. seq 3]"
    assert out[2]["payload"]["summary_markdown"] == "SUMMARY-NEW"
    # the converged bundle stays complete — the audit compares the summary against it
    assert out[0]["payload"]["bundle"]["spec_markdown"] == "BODY-V1"
    # non-body intent_summary keys survive
    assert out[1]["payload"]["converged_artifact_seq"] == 1


def test_compact_replay_keeps_historical_cold_verdict_notices():
    """B-4 retired cold-verdict PRODUCTION; pre-B.9 channels still carry the notices,
    and a reserved phase must never brick a replay — they pass through untouched."""
    def cold(seq):
        return _msg(seq, "critic", "notice",
                    {"phase": watcher.COLD_VERDICTS_PHASE,
                     "cold_verdicts": [{"element_id": f"j{seq}", "verdict": "holds",
                                        "reason": "r"}]})

    msgs = [_bundle_artifact(1, "BODY"), cold(2), cold(3), cold(4), cold(5)]
    out = watcher.compact_replay(msgs)
    assert [m["seq"] for m in out] == [1, 2, 3, 4, 5]


def test_compact_replay_never_mutates_input():
    import copy

    msgs = [
        _bundle_artifact(1, "BODY-V1"),
        _msg(2, "critic", "notice", {"phase": watcher.COLD_VERDICTS_PHASE,
                                     "cold_verdicts": []}),
        _bundle_artifact(3, "BODY-V2"),
    ]
    snapshot = copy.deepcopy(msgs)
    watcher.compact_replay(msgs)
    assert msgs == snapshot


def test_compact_replay_code_mode_refs_untouched():
    msgs = [
        _msg(1, "development", "artifact",
             {"mode": "code", "artifact_ref": {"repo": ".", "commit": "abc"}}),
        _msg(2, "development", "artifact",
             {"mode": "code", "artifact_ref": {"repo": ".", "commit": "def"}}),
    ]
    assert watcher.compact_replay(msgs) == msgs


def _code_artifact(seq, commit, diff):
    return _msg(seq, "development", "artifact",
                {"mode": "code", "artifact_ref": {"repo": ".", "commit": commit},
                 "description": f"change at {commit}", "diff": diff})


def test_compact_replay_stubs_superseded_code_diffs():
    """wrc1: code mode ships ref + inline diff — old diffs bloat the replay exactly
    like old spec bodies. Only the newest diff survives; ref/mode/metadata stay."""
    msgs = [
        _code_artifact(1, "abc", "DIFF-V1"),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}),
        _code_artifact(3, "def", "DIFF-V2"),
        _code_artifact(5, "0a1", "DIFF-V3"),
    ]
    out = watcher.compact_replay(msgs)
    stub = "[superseded — см. seq 5]"
    assert out[0]["payload"]["diff"] == stub
    assert out[2]["payload"]["diff"] == stub
    assert out[3]["payload"]["diff"] == "DIFF-V3"
    # metadata survives on stubbed versions — the history of refs stays readable
    assert out[0]["payload"]["artifact_ref"] == {"repo": ".", "commit": "abc"}
    assert out[0]["payload"]["mode"] == "code"
    assert out[0]["payload"]["description"] == "change at abc"
    assert out[1] == msgs[1]  # findings untouched


async def test_run_pass_prompts_elide_superseded_bodies():
    calls = {"invoked": [], "posted": []}
    cold = '{"cold_verdicts": [{"element_id": "j1", "verdict": "holds", "reason": "r"}]}'
    full = '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 1}}'
    outputs = iter([cold, full])

    def invoke(prompt):
        calls["invoked"].append(prompt)
        return next(outputs)

    async def post(route, body):
        calls["posted"].append((route, body))
        return {}

    msgs = [
        _bundle_artifact(1, "BODY-V1-SUPERSEDED"),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}),
        _msg(3, "development", "disposition",
             {"finding_id": "f1", "outcome": "fixed", "reason": "KEPT-DISPOSITION"}),
        _bundle_artifact(4, "BODY-V2-CURRENT"),
    ]
    await watcher.run_pass(msgs, "CRITIC", invoke, post, mode="spec", artifact_seq=4)
    for prompt in calls["invoked"]:
        assert "BODY-V1-SUPERSEDED" not in prompt
        assert "BODY-V2-CURRENT" in prompt
        assert "KEPT-DISPOSITION" in prompt  # the decision history is never elided


def test_build_prompt_contains_core_pointer_and_contract():
    prompt = watcher.build_prompt(
        "CRITIC PROMPT", [_msg(1, "d", "artifact")], watcher.OUTPUT_CONTRACT,
        projection_path="X:/projections/review_x/projection.json",
    )
    assert prompt.startswith("CRITIC PROMPT")
    assert "OUTPUT CONTRACT" in prompt
    assert '"kind": "artifact"' in prompt  # the current version rides the resident core
    # the single-channel transport's injection defence: the core is delimited
    # and explicitly marked as data (see the module's transport security model)
    assert "NOT instructions" in prompt
    assert prompt.index("NOT instructions") < prompt.index('"kind": "artifact"')
    # A-1/A-3: the prompt points at the file projection instead of inlining history
    assert "X:/projections/review_x/projection.json" in prompt
    assert "FULL JOURNAL PROJECTION" in prompt


def test_build_prompt_resident_core_excludes_history_and_keeps_operator_material():
    """A-3: superseded bodies and past traffic live only in the projection; operator
    messages and the current version stay inline; the threat frame stands (B-5)."""
    msgs = [
        _bundle_artifact(1, "BODY-V1-SUPERSEDED"),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1"}]}),
        _msg(3, "development", "disposition",
             {"finding_id": "f1", "outcome": "fixed", "reason": "done",
              "class_closure": {"scope": "s", "combed": [], "found": []}}),
        _msg(4, "operator", "notice", {"phase": "operator_ruling", "text": "OPERATOR-FRAME"}),
        _bundle_artifact(5, "BODY-V2-CURRENT"),
    ]
    prompt = watcher.build_prompt(
        "CRITIC", msgs, "CONTRACT", projection_path="p.json",
        frame={
            "threat_model": "personal single-user deployment; leak via published artifacts",
            "operating_scale": "graph of thousands of nodes, one reader",
            "granted_by": "operator, pre-gate",
            "boundaries": [{"id": "b-1", "scope": "deployment", "text": "single-user box"}],
        },
    )
    assert "BODY-V2-CURRENT" in prompt
    assert "BODY-V1-SUPERSEDED" not in prompt
    assert "OPERATOR-FRAME" in prompt
    assert "STANDING THREAT FRAME" in prompt and "single-user box" in prompt
    # the POSITIVE half renders verbatim beside the exclusions (B-5)
    assert "leak via published artifacts" in prompt
    assert "thousands of nodes" in prompt
    # ...and an absent context is SAID to be absent, never dressed as a model
    bare = watcher.threat_frame_block({"boundaries": [{"id": "b", "scope": "review", "text": "t"}]})
    assert "no operator-confirmed threat context recorded" in bare
    # the disposition posted after the last critic status opens the prompt (A-3/A-5)
    assert "DISPOSITIONS AWAITING VERIFICATION" in prompt


def test_build_prompt_names_pending_observations():
    """G-3: unanswered dev observations are named inline — the server will refuse a
    pass-ending status that skips them, so the prompt must not hide them in the file."""
    msgs = [
        _msg(1, "d", "artifact"),
        _msg(2, "development", "notice",
             {"phase": "dev_observation", "observation_id": "obs-7", "text": "stray"}),
    ]
    prompt = watcher.build_prompt("CRITIC", msgs, "CONTRACT", projection_path="p.json")
    assert "OBSERVATIONS AWAITING YOUR VERDICT" in prompt and "obs-7" in prompt
    answered = msgs + [
        _msg(3, "critic", "status",
             {"value": "needs_iteration", "artifact_seq": 1,
              "observation_dispositions": [
                  {"observation_id": "obs-7", "action": "dismissed", "reason": "fine"}]}),
    ]
    prompt = watcher.build_prompt("CRITIC", answered, "CONTRACT", projection_path="p.json")
    assert "OBSERVATIONS AWAITING YOUR VERDICT" not in prompt


# --- parsing -----------------------------------------------------------------


def test_parse_fenced_and_bare_output():
    fenced = 'prose\n```json\n{"findings": {"items": []}}\n```\nmore prose'
    assert watcher.parse_critic_output(fenced) == {"findings": {"items": []}}
    bare = 'verdict follows {"status": {"value": "converged"}} thanks'
    assert watcher.parse_critic_output(bare) == {"status": {"value": "converged"}}


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        watcher.parse_critic_output("no json here at all")


# --- verdict -> messages -----------------------------------------------------


def test_result_messages_order_and_memo():
    verdict = {
        "findings": {"items": [{"id": "f1"}]},
        "escalations": [{"kind": "contested_disposition", "finding_id": "f1"}],
        "status": {"value": "needs_iteration", "iteration": 1},
        "memo": "look at error handling next pass",
    }
    out = watcher.result_messages(verdict, artifact_seq=5)
    assert [m["kind"] for m in out] == ["findings", "escalation", "status", "notice"]
    assert out[0]["payload"]["artifact_seq"] == 5  # defaulted onto findings
    assert out[2]["payload"]["artifact_seq"] == 5  # ... and status
    assert out[3]["payload"]["phase"] == watcher.CRITIC_MEMO_PHASE


def test_result_messages_rejects_nonlist_or_nonobject_items():
    """F21: findings.items must be a list of objects — a string / dict / non-object entry is a
    controlled ValueError (retry/operator path), never an AttributeError downstream."""
    for bad in ("abc", {"id": "f1"}, [42]):
        with pytest.raises(ValueError, match="list of objects"):
            watcher.result_messages(
                {"findings": {"items": bad}, "status": {"value": "needs_iteration"}}, 1
            )


def test_result_messages_requires_findings_and_status():
    with pytest.raises(ValueError):
        watcher.result_messages({"status": {"value": "converged"}}, artifact_seq=1)
    with pytest.raises(ValueError):
        watcher.result_messages({"findings": {"items": []}}, artifact_seq=1)


def test_result_messages_injects_artifact_seq_into_escalations():
    verdict = {
        "findings": {"items": []},
        "escalations": [{"kind": "contested_disposition", "finding_id": "f1"}],
        "status": {"value": "needs_iteration", "iteration": 1},
    }
    out = watcher.result_messages(verdict, artifact_seq=7)
    esc = [m for m in out if m["kind"] == "escalation"]
    assert esc and esc[0]["payload"]["artifact_seq"] == 7


def test_result_messages_strips_model_claimed_reserved_phase():
    """Reserved phases are the harness's vocabulary (finding
    b9-observation-ledger-reserved-phase-still-bypasses): a model verdict claiming
    `watcher_error` on its status loses the phase, and the strip is recorded as a
    replay-visible notice. An ordinary qualifier phase still passes through verbatim."""
    verdict = {
        "findings": {"items": []},
        "status": {"value": "needs_iteration", "iteration": 3, "phase": "watcher_error"},
    }
    out = watcher.result_messages(verdict, artifact_seq=9)
    status = [m for m in out if m["kind"] == "status"][0]
    assert "phase" not in status["payload"]
    strips = [m for m in out if m["kind"] == "notice"
              and m["payload"].get("phase") == "reserved_phase_stripped"]
    assert len(strips) == 1 and strips[0]["payload"]["stripped_phase"] == "watcher_error"
    # An ordinary qualifier survives untouched.
    out2 = watcher.result_messages(
        {"findings": {"items": []},
         "status": {"value": "needs_iteration", "phase": "second_sweep"}},
        artifact_seq=9,
    )
    status2 = [m for m in out2 if m["kind"] == "status"][0]
    assert status2["payload"]["phase"] == "second_sweep"


def test_observation_repass_due_schedules_and_spends():
    """The late-observation twin of coverage_repass_due (finding
    b9-late-observation-no-legal-cleanup-path): a convergence_blocked notice naming
    unanswered observations for the CURRENT artifact schedules a same-version pass; the
    signal is spent once the observations are answered, and a blocker for a superseded
    version never schedules."""
    base = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "development", "notice",
             {"phase": "dev_observation", "observation_id": "obs-late", "text": "t"}),
        _msg(3, "system", "notice",
             {"phase": "convergence_blocked", "artifact_seq": 1,
              "unanswered_observations": ["obs-late"]}),
    ]
    assert watcher.observation_repass_due(base) is True
    answered = base + [
        _msg(4, "critic", "status",
             {"value": "converged", "artifact_seq": 1,
              "observation_dispositions": [
                  {"observation_id": "obs-late", "action": "dismissed", "reason": "ok"}]}),
    ]
    assert watcher.observation_repass_due(answered) is False
    superseded = base + [_msg(4, "development", "artifact", {"mode": "code"})]
    assert watcher.observation_repass_due(superseded) is False


def test_result_messages_emits_human_questions():
    verdict = {
        "findings": {"items": []},
        "human_questions": [{"id": "q1", "question": "which convention wins here?"}],
        "status": {"value": "needs_human", "iteration": 1},
    }
    out = watcher.result_messages(verdict, artifact_seq=4)
    assert [m["kind"] for m in out] == ["findings", "human_question", "status"]
    hq = out[1]["payload"]
    assert hq["id"] == "q1" and hq["artifact_seq"] == 4


def test_result_messages_rejects_malformed_human_question():
    base = {"findings": {"items": []}, "status": {"value": "needs_human"}}
    for bad in ([{"id": "q1"}], [{"question": "?"}], [1]):
        with pytest.raises(ValueError, match="human question"):
            watcher.result_messages({**base, "human_questions": bad}, artifact_seq=1)


def test_result_messages_preserves_status_qualifiers():
    # the gate_handoff discriminator (and any extra status field) must pass through
    verdict = {
        "findings": {"items": []},
        "status": {"value": "needs_human", "phase": "gate_handoff",
                   "detail": "intent summary audited clean"},
    }
    out = watcher.result_messages(verdict, artifact_seq=9)
    status = out[-1]["payload"]
    assert status["phase"] == "gate_handoff"
    assert status["detail"] == "intent summary audited clean"
    assert status["artifact_seq"] == 9


def test_result_messages_rejects_mismatched_artifact_seq():
    base = {"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}
    for bad in (
        {**base, "findings": {"items": [], "artifact_seq": 3}},
        {**base, "status": {"value": "converged", "artifact_seq": 3}},
        {**base, "escalations": [{"kind": "contested_disposition", "artifact_seq": 3}]},
    ):
        with pytest.raises(ValueError, match="artifact_seq"):
            watcher.result_messages(bad, artifact_seq=5)


# --- run_pass ----------------------------------------------------------------


async def _run(mode, invoke_outputs):
    calls = {"invoked": [], "posted": []}

    def invoke(prompt):
        calls["invoked"].append(prompt)
        return invoke_outputs[len(calls["invoked"]) - 1]

    async def post(route, body):
        calls["posted"].append((route, body))
        return {}

    msgs = [
        _msg(1, "development", "artifact", {"mode": mode}),
        _msg(2, "development", "notice", {"phase": "rationale", "rationales": {}}),
    ]
    await watcher.run_pass(msgs, "CRITIC", invoke, post, mode=mode, artifact_seq=1)
    return calls


async def test_run_pass_code_mode_single_invocation():
    clean = '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}'
    calls = await _run("code", [clean])
    assert len(calls["invoked"]) == 1
    kinds = [(route, body.get("kind")) for route, body in calls["posted"]]
    assert kinds == [("state", None), ("messages", "findings"), ("messages", "status")]


async def test_run_pass_spec_mode_single_fresh_invocation():
    """A-5/B-4: spec mode runs ONE fresh-session invocation like every other pass — the
    separate cold pass is retired, and development's rationale reaches neither the
    prompt nor the projection (B-1)."""
    full = ('{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}, '
            '"memo": "nothing pending"}')
    calls = await _run("spec", [full])
    assert len(calls["invoked"]) == 1
    assert '"phase": "rationale"' not in calls["invoked"][0]
    posted_kinds = [body.get("kind") for route, body in calls["posted"] if route == "messages"]
    assert posted_kinds == ["findings", "status", "notice"]  # memo last, no cold notice


def test_answered_needs_human_pass_does_not_count_as_coverage():
    """A pass that DEFERRED to the operator must not read as a pass that judged.

    Live dead-lock (review 4ffd8b0e, artifact_seq 195): a clean pass ended with a question
    to the operator instead of a verdict. The version then read as reviewed, so `plan()`
    scheduled nothing — while the channel still waited on a critic verdict that could never
    be produced. Both sides waited on each other with nothing due.
    """
    base = [
        _msg(1, "development", "artifact", {"mode": "spec", "bundle": {"spec_markdown": "s"}}),
        _msg(2, "development", "coverage_manifest", {"artifact_seq": 1}),
        _msg(3, "critic", "findings", {"items": [], "artifact_seq": 1}),
        _msg(4, "critic", "human_question", {"id": "q1", "artifact_seq": 1}),
        _msg(5, "critic", "status", {"value": "needs_human", "artifact_seq": 1}),
    ]
    # while the question is open the operator owns the turn either way
    assert watcher.plan(base)["action"] != "review"  # a ping is also not a pass
    assert watcher.last_reviewed_seq(base) == 0

    answered = base + [_msg(6, "operator", "decision_response", {"question_id": "q1"})]
    # answered: the verdict is owed again, so a pass is due — not silence
    assert watcher.plan(answered)["action"] == "review"

    # a pass that CONCLUDED still counts, exactly as before (no regression on the
    # 2026-07-22 lesson: findings alone are not coverage, an ended pass is)
    concluded = base[:3] + [_msg(4, "critic", "status", {"value": "needs_iteration",
                                                         "artifact_seq": 1})]
    assert watcher.last_reviewed_seq(concluded) == 1
    assert watcher.plan(concluded)["action"] != "review"  # a ping is also not a pass


async def test_run_pass_retries_unparseable_output_once(tmp_path, monkeypatch):
    # chdir: an unparseable reply is now preserved on disk relative to cwd, and a test
    # must not drop that debris into the repo it runs from
    monkeypatch.chdir(tmp_path)
    full = '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}'
    calls = await _run("code", ["utter garbage", full])
    assert len(calls["invoked"]) == 2
    assert "UNPARSEABLE" in calls["invoked"][1]


# --- failure surfacing / currency (never silent) ------------------------------


def _recording_post(calls):
    async def post(route, body):
        calls["posted"].append((route, body))
        return {}

    return post


async def test_run_pass_unparseable_after_retry_surfaces_needs_human(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # see the note on the retry test above
    calls = {"posted": []}

    def invoke(prompt):
        return "utter garbage"

    with pytest.raises(ValueError):
        await watcher.run_pass(
            [_msg(1, "development", "artifact", {"mode": "code"})],
            "CRITIC", invoke, _recording_post(calls), mode="code", artifact_seq=1,
        )
    tail = [(b.get("kind"), (b.get("payload") or {})) for r, b in calls["posted"]
            if r == "messages"]
    assert tail[-2][0] == "notice"
    assert tail[-2][1]["phase"] == watcher.WATCHER_ERROR_PHASE
    # The fallback status carries the SAME ledger-exempt phase (B.9 G-3 fix): an owed
    # observation must never 422 the very report announcing a fault.
    assert tail[-1] == ("status", {"value": "needs_human", "artifact_seq": 1,
                                   "phase": watcher.WATCHER_ERROR_PHASE})


async def test_unparseable_reply_reaches_the_channel_and_the_disk(tmp_path, monkeypatch):
    """A spent pass must leave evidence.

    Live gap (review 4ffd8b0e over artifact_seq 179): both attempts replied unparseably,
    the watcher surfaced a bare "no JSON object found" and exited, and the replies existed
    nowhere — so the only artefact that could explain the failure was the one the failure
    path discarded. Both replies now travel in the surfaced error (the channel outlives
    this machine) AND land on disk.
    """
    monkeypatch.chdir(tmp_path)
    calls = {"posted": []}
    replies = iter(["FIRST prose reply, no json here", "SECOND prose reply either"])

    def invoke(prompt):
        return next(replies)

    with pytest.raises(ValueError) as excinfo:
        await watcher.run_pass(
            [_msg(1, "development", "artifact", {"mode": "code"})],
            "CRITIC", invoke, _recording_post(calls), mode="code", artifact_seq=1,
        )

    surfaced = [(b.get("payload") or {}).get("error") for r, b in calls["posted"]
                if r == "messages" and b.get("kind") == "notice"
                and (b.get("payload") or {}).get("phase")
                == watcher.WATCHER_ERROR_PHASE]
    assert len(surfaced) == 1
    # both attempts are distinguishable: "prose twice" and "nothing the second time" are
    # different failures with different fixes
    assert "SECOND prose reply" in surfaced[0]
    assert "FIRST prose reply" in surfaced[0]
    assert "SECOND prose reply" in str(excinfo.value)

    dumped = sorted(p.name for p in (tmp_path / watcher.RAW_DUMP_DIR).iterdir())
    assert dumped == ["unparseable_seq1_first.txt", "unparseable_seq1_retry.txt"]
    assert (tmp_path / watcher.RAW_DUMP_DIR / "unparseable_seq1_retry.txt").read_text(
        encoding="utf-8") == "SECOND prose reply either"


async def test_unparseable_dump_failure_does_not_mask_the_original_error(monkeypatch):
    """Preserving evidence is best-effort: it may never replace the real error."""
    monkeypatch.setattr(watcher, "save_unparseable", lambda *a, **k: None)
    detail = watcher.unparseable_detail(7, "retry", "prose", ValueError("no JSON object"))
    assert "no JSON object" in detail
    assert "could not be written" in detail
    assert "prose" in detail


async def test_run_pass_invocation_failure_surfaces_needs_human():
    calls = {"posted": []}

    def invoke(prompt):
        raise RuntimeError("critic invocation failed rc=1: boom")

    with pytest.raises(RuntimeError):
        await watcher.run_pass(
            [_msg(1, "development", "artifact", {"mode": "code"})],
            "CRITIC", invoke, _recording_post(calls), mode="code", artifact_seq=1,
        )
    kinds = [b.get("kind") for r, b in calls["posted"] if r == "messages"]
    assert kinds == ["notice", "status"]  # watcher_error + needs_human, nothing else


async def test_run_pass_abandons_when_newer_artifact_appears():
    calls = {"posted": []}
    clean = '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}'

    async def fetch_new():
        return [_msg(9, "development", "artifact", {"mode": "code"})]

    posted = await watcher.run_pass(
        [_msg(1, "development", "artifact", {"mode": "code"})],
        "CRITIC", lambda p: clean, _recording_post(calls),
        mode="code", artifact_seq=1, fetch_new=fetch_new,
    )
    assert posted is False
    # only the pickup went out — no stale findings/status for seq 1
    assert [r for r, b in calls["posted"]] == ["state"]


async def test_run_pass_abandons_when_threat_context_restated():
    """B-5 freshness seam 2 (b9-threat-context-not-a-freshness-input): a mid-pass
    threat_context record means the verdict was formed under the OLD frame — the pass
    is abandoned unspent, exactly like a newer artifact."""
    calls = {"posted": []}
    clean = '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}'

    async def fetch_new():
        return [_msg(9, "operator", "notice",
                     {"phase": "threat_context", "threat_model": "corrected",
                      "operating_scale": "s", "granted_by": "op"})]

    posted = await watcher.run_pass(
        [_msg(1, "development", "artifact", {"mode": "code"})],
        "CRITIC", lambda p: clean, _recording_post(calls),
        mode="code", artifact_seq=1, fetch_new=fetch_new,
    )
    assert posted is False
    assert [r for r, b in calls["posted"]] == ["state"]


def test_frame_repass_due_schedules_and_spends():
    """B-5 freshness seam 3: a context restated after the last pass-ending status
    schedules one fresh same-version pass; a later status spends the signal; no pass
    yet -> the ordinary unreviewed-artifact trigger owns it."""
    base = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}),
        _msg(3, "operator", "notice",
             {"phase": "threat_context", "threat_model": "m2",
              "operating_scale": "s", "granted_by": "op"}),
    ]
    assert watcher.frame_repass_due(base) is True
    # an administrative ledger-exempt status contains no pass and spends nothing
    faulted = base + [_msg(4, "critic", "status",
                           {"value": "needs_human", "artifact_seq": 1,
                            "phase": "watcher_error"})]
    assert watcher.frame_repass_due(faulted) is True
    spent = base + [_msg(4, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1})]
    assert watcher.frame_repass_due(spent) is False
    no_pass_yet = [
        _msg(1, "development", "artifact", {"mode": "code"}),
        _msg(2, "operator", "notice",
             {"phase": "threat_context", "threat_model": "m", "operating_scale": "s",
              "granted_by": "op"}),
    ]
    assert watcher.frame_repass_due(no_pass_yet) is False


async def test_run_pass_writes_the_projection_before_invoking(tmp_path):
    """A-1: the projection file is rendered from THIS pass's replay before the
    invocation, is marked generated, and carries no development rationale (B-1)."""
    projection = tmp_path / "proj" / "projection.json"
    seen = {}

    def invoke(prompt):
        # the file must already exist AT invocation time, not after the pass
        seen["existed"] = projection.exists()
        seen["content"] = projection.read_text(encoding="utf-8")
        return '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}'

    calls = {"posted": []}
    await watcher.run_pass(
        [
            _msg(1, "development", "artifact", {"mode": "code"}),
            _msg(2, "development", "notice",
                 {"phase": "rationale", "rationales": {"j1": "SECRET-RATIONALE"}}),
        ],
        "CRITIC", invoke, _recording_post(calls),
        mode="code", artifact_seq=1, projection_path=projection,
    )
    assert seen["existed"] is True
    assert "SECRET-RATIONALE" not in seen["content"]
    assert "DERIVED" in seen["content"]


async def test_run_pass_projection_probe_failure_routes_environmentally(tmp_path):
    """A-4: an unreadable projection is a named environmental refusal BEFORE the
    invocation — nothing spent, no watcher_error, no needs_human."""
    calls = {"posted": []}

    def probe(path):
        raise watcher.EnvironmentFault("projection unreadable in the sandbox")

    with pytest.raises(watcher.EnvironmentFault):
        await watcher.run_pass(
            [_msg(1, "development", "artifact", {"mode": "code"})],
            "CRITIC", lambda p: "never invoked", _recording_post(calls),
            mode="code", artifact_seq=1,
            projection_path=tmp_path / "projection.json", projection_probe=probe,
        )
    # only the pickup went out — the fault stretch owns what happens next
    assert [r for r, b in calls["posted"]] == ["state"]


async def test_run_pass_pickup_failure_surfaces_needs_human():
    calls = {"posted": []}

    async def post(route, body):
        if route == "state":
            raise RuntimeError("post state rejected: 500 boom")
        calls["posted"].append((route, body))
        return {}

    with pytest.raises(RuntimeError, match="post state rejected"):
        await watcher.run_pass(
            [_msg(1, "development", "artifact", {"mode": "code"})],
            "CRITIC", lambda p: "never invoked", post, mode="code", artifact_seq=1,
        )
    kinds = [b.get("kind") for r, b in calls["posted"]]
    assert kinds == ["notice", "status"]  # watcher_error + needs_human


async def test_run_pass_posts_when_channel_quiet():
    calls = {"posted": []}
    clean = '{"findings": {"items": []}, "status": {"value": "converged", "iteration": 0}}'

    async def fetch_new():
        return []

    posted = await watcher.run_pass(
        [_msg(1, "development", "artifact", {"mode": "code"})],
        "CRITIC", lambda p: clean, _recording_post(calls),
        mode="code", artifact_seq=1, fetch_new=fetch_new,
    )
    assert posted is True
    kinds = [(r, b.get("kind")) for r, b in calls["posted"]]
    assert kinds == [("state", None), ("messages", "findings"), ("messages", "status")]


# --- codex_invoker temp-file hygiene ------------------------------------------


def test_codex_invoker_prompt_file_created_and_removed(tmp_path):
    import sys as _sys
    from pathlib import Path

    capture = tmp_path / "seen.txt"
    pathfile = tmp_path / "prompt_path.txt"
    # copy the prompt file's content + record its path, so we can verify it existed
    # during the run and was deleted afterwards
    script = (
        "import sys,shutil,pathlib;"
        f"shutil.copy(sys.argv[1], r'{capture}');"
        f"pathlib.Path(r'{pathfile}').write_text(sys.argv[1])"
    )
    cmd = f'"{_sys.executable}" -c "{script}" {{prompt_file}}'
    invoke = watcher.codex_invoker(cmd, cwd=None, timeout=60)
    invoke("PROMPT BODY")
    assert capture.read_text(encoding="utf-8") == "PROMPT BODY"
    temp_path = Path(pathfile.read_text(encoding="utf-8").strip())
    assert not temp_path.exists()  # the replay-bearing temp file never lingers


def test_codex_invoker_timeout_propagates_cleanly():
    # regression evidence for finding w10: an exception raised by subprocess.run
    # propagates after the finally-cleanup — it is NOT masked by an UnboundLocalError.
    # Since B.9 round 7 (finding critic-invocation-timeout-is-terminal) the timeout
    # propagates AS EnvironmentFault: a hung provider routes to the fault stretch
    # instead of killing the watcher; the no-masking property is unchanged.
    import sys as _sys

    cmd = f'"{_sys.executable}" -c "import time; time.sleep(30)"'
    invoke = watcher.codex_invoker(cmd, cwd=None, timeout=0.5)
    with pytest.raises(watcher.EnvironmentFault, match="timeout"):
        invoke("PROMPT")


def test_codex_invoker_stdin_mode_reads_prompt():
    import sys as _sys

    cmd = f'"{_sys.executable}" -c "import sys;print(sys.stdin.read())"'
    invoke = watcher.codex_invoker(cmd, cwd=None, timeout=60)
    assert invoke("VIA STDIN").strip() == "VIA STDIN"


# --- transport hardening: oversize current diff + settled register ---------


def test_compact_replay_replaces_oversize_current_diff_with_git_pointer():
    big = "x" * 1000
    msgs = [
        _msg(1, "development", "artifact",
             {"mode": "code", "diff": "old", "artifact_ref": {"base": "b1", "commit": "c1"}}),
        _msg(2, "development", "artifact",
             {"mode": "code", "diff": big,
              "artifact_ref": {"base": "b1", "commit": "c2", "branch": "review/x"}}),
    ]
    out = watcher.compact_replay(msgs, max_current_diff_chars=100)
    assert out[0]["payload"]["diff"].startswith("[superseded")
    cur = out[1]["payload"]["diff"]
    assert "git diff b1..c2" in cur
    assert "1000 chars" in cur
    # metadata survives; input list is never mutated
    assert out[1]["payload"]["artifact_ref"]["commit"] == "c2"
    assert msgs[1]["payload"]["diff"] == big


def test_compact_replay_keeps_small_current_diff_inline():
    msgs = [
        _msg(1, "development", "artifact",
             {"mode": "code", "diff": "tiny", "artifact_ref": {"base": "b", "commit": "c"}}),
    ]
    out = watcher.compact_replay(msgs, max_current_diff_chars=100)
    assert out[0]["payload"]["diff"] == "tiny"


def test_compact_replay_never_cuts_spec_bundles_even_when_huge():
    msgs = [
        _msg(1, "development", "artifact",
             {"mode": "spec", "bundle": {"spec_markdown": "s" * 500, "elements": []}}),
    ]
    out = watcher.compact_replay(msgs, max_current_diff_chars=100)
    assert out[0]["payload"]["bundle"]["spec_markdown"] == "s" * 500


def _settled_msgs():
    return [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [
            {"id": "f1", "claim": "wrong X"},
            {"id": "f2", "claim": "risky Y"},
        ]}),
        _msg(3, "development", "disposition",
             {"finding_id": "f1", "outcome": "fixed", "reason": "did the thing"}),
        _msg(4, "development", "disposition",
             {"finding_id": "f2", "outcome": "waived", "reason": "accepted trade-off Z"}),
    ]


def test_settled_register_lists_fixed_and_waived_with_reason():
    reg = watcher.settled_register(_settled_msgs())
    assert "[f1] fixed" in reg and "wrong X" in reg
    assert "[f2] WAIVED" in reg and "accepted trade-off Z" in reg
    # both dispute routes stay advertised, split by closure type — no gagged dissent
    assert "reopens_finding_id" in reg
    assert "contested_disposition" in reg


def test_settled_register_empty_without_terminal_dispositions():
    assert watcher.settled_register(_settled_msgs()[:2]) == ""


def test_build_prompt_carries_the_register():
    prompt = watcher.build_prompt(
        "CRITIC-PROMPT", _settled_msgs(), "CONTRACT", projection_path="p.json"
    )
    assert "SETTLED FINDINGS REGISTER" in prompt
    assert prompt.index("SETTLED FINDINGS REGISTER") < prompt.index("RESIDENT CORE")


def test_build_prompt_without_dispositions_has_no_register_section():
    prompt = watcher.build_prompt(
        "CRITIC-PROMPT", _settled_msgs()[:2], "CONTRACT", projection_path="p.json"
    )
    assert "SETTLED FINDINGS REGISTER" not in prompt


def test_settled_register_reflects_effective_ledger_state():
    """Latest disposition governs; a closure disputed AFTER its disposition leaves the
    register until the operator answers (finding settled-register-not-terminal)."""
    base = _settled_msgs()
    # f2's waiver is contested after the disposition -> f2 drops out
    contested = base + [
        _msg(5, "critic", "escalation",
             {"kind": "contested_disposition", "finding_id": "f2", "detail": "inadequate"}),
    ]
    reg = watcher.settled_register(contested)
    assert "[f1] fixed" in reg and "f2" not in reg
    # the operator settles the contest -> f2 returns as settled
    answered = contested + [
        _msg(6, "operator", "decision_response", {"finding_id": "f2", "decision": "waiver stands"}),
    ]
    reg = watcher.settled_register(answered)
    assert "[f2] WAIVED" in reg
    # a reopen targeting f1 drops f1 until answered
    reopened = answered + [
        _msg(7, "critic", "findings", {"artifact_seq": 1, "items": [
            {"id": "f3", "claim": "fix does not work", "reopens_finding_id": "f1"}]}),
    ]
    reg = watcher.settled_register(reopened)
    assert "[f1]" not in reg and "[f2] WAIVED" in reg


def test_settled_register_uses_latest_disposition_and_full_waiver_reason():
    long_reason = "accepted trade-off " + "z" * 600
    msgs = [
        _msg(1, "development", "artifact", {"mode": "spec"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": [{"id": "f1", "claim": "c"}]}),
        _msg(3, "development", "disposition",
             {"finding_id": "f1", "outcome": "fixed", "reason": "first try"}),
        _msg(4, "development", "disposition",
             {"finding_id": "f1", "outcome": "waived", "reason": long_reason}),
    ]
    reg = watcher.settled_register(msgs)
    assert "[f1] WAIVED" in reg and "fixed" not in reg
    assert "z" * 600 in reg  # the waiver reason is carried in full, never truncated


def test_oversize_diff_stays_inline_without_usable_git_ref():
    """Elision must never trade the review's subject for a dangling pointer (finding
    elided-diff-can-lose-review-subject-without-ref)."""
    big = "x" * 1000
    for bad_ref in ({}, {"base": "b1"}, {"commit": "c1"}, {"base": "", "commit": "c1"}):
        msgs = [_msg(1, "development", "artifact",
                     {"mode": "code", "diff": big, "artifact_ref": bad_ref})]
        out = watcher.compact_replay(msgs, max_current_diff_chars=100)
        assert out[0]["payload"]["diff"] == big


def test_settled_register_dispute_not_masked_by_later_disposition():
    """After a dispute, only an OPERATOR answer re-settles the finding — development
    re-disposing its own contested closure must not (finding
    settled-register-dispute-can-be-masked-by-later-disposition)."""
    msgs = _settled_msgs() + [
        _msg(5, "critic", "escalation",
             {"kind": "contested_disposition", "finding_id": "f2", "detail": "inadequate"}),
        _msg(6, "development", "disposition",
             {"finding_id": "f2", "outcome": "waived", "reason": "still waived, better words"}),
    ]
    reg = watcher.settled_register(msgs)
    assert "f2" not in reg  # dev's own re-disposition does not clear the dispute
    reg = watcher.settled_register(msgs + [
        _msg(7, "operator", "decision_response", {"finding_id": "f2", "decision": "stands"}),
    ])
    assert "[f2] WAIVED" in reg and "better words" in reg


def test_ref_only_current_code_artifact_stubs_older_diffs_and_instructs_git():
    """A ref-only code artifact supersedes earlier inline diffs (finding
    artifact-ref-only-current-artifact-leaves-stale-diff-inline): the old diff is
    stubbed and the current artifact gains an explicit git-read instruction."""
    old = "x" * 50
    msgs = [
        _msg(1, "development", "artifact",
             {"mode": "code", "diff": old, "artifact_ref": {"base": "b", "commit": "c1"}}),
        _msg(2, "development", "artifact",
             {"mode": "code", "artifact_ref": {"base": "b", "commit": "c2"}}),
    ]
    out = watcher.compact_replay(msgs, max_current_diff_chars=1000)
    assert out[0]["payload"]["diff"].startswith("[superseded")
    assert "git diff b..c2" in out[1]["payload"]["diff"]
    # input untouched: the ref-only artifact still has no diff key in the original
    assert "diff" not in msgs[1]["payload"]


def test_whitespace_git_ref_never_allows_elision():
    big = "x" * 1000
    msgs = [_msg(1, "development", "artifact",
                 {"mode": "code", "diff": big,
                  "artifact_ref": {"base": "  ", "commit": "c1"}})]
    out = watcher.compact_replay(msgs, max_current_diff_chars=100)
    assert out[0]["payload"]["diff"] == big


def test_plan_inherits_mode_from_converged_artifact_for_modeless_summary():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "development", "artifact",
             {"intent_summary": True, "converged_artifact_seq": 1,
              "summary_markdown": "# s"}),
    ]
    decision = watcher.plan(msgs)
    assert decision["action"] == "review" and decision["mode"] == "code"


def test_settled_register_operator_rejection_voids_closure_until_redisposed():
    """An operator answer that returns/voids a disputed closure must not re-settle it
    (finding settled-register-operator-rejection-still-registers-closure); only a
    disposition posted AFTER the rejection re-enters the register."""
    msgs = _settled_msgs() + [
        _msg(5, "critic", "escalation",
             {"kind": "contested_disposition", "finding_id": "f2", "detail": "inadequate"}),
        _msg(6, "operator", "decision_response",
             {"finding_id": "f2", "decision": "waiver rejected, fix it",
              "requires_new_artifact": True}),
    ]
    reg = watcher.settled_register(msgs)
    assert "f2" not in reg  # rejected closure is void, not settled
    reg = watcher.settled_register(msgs + [
        _msg(7, "development", "disposition",
             {"finding_id": "f2", "outcome": "fixed", "reason": "actually fixed now"}),
    ])
    assert "[f2] fixed" in reg  # the post-rejection disposition re-enters


# --- read-only sandbox guard (Incident db5b243d) ---------------------------


@pytest.mark.parametrize("cmd", [
    "codex exec -s read-only -",              # the default
    "codex exec --sandbox read-only -",       # long form
    "codex exec resume {id} -c sandbox_mode=\"read-only\"",  # resume config form (no -s)
    "CODEX EXEC -S READ-ONLY -",              # case-insensitive
    "codex exec -s read-only -s read-only -",  # duplicate but ALL read-only -> still safe
    "codex exec --sandbox=read-only -",       # = form
    "codex exec --config sandbox_mode=read-only -",  # long config form, safe
    "codex exec -c=sandbox_mode=read-only -",  # = joined config, safe
])
def test_codex_cmd_is_read_only_accepts_pinned_safe(cmd):
    assert watcher._codex_cmd_is_read_only(cmd) is True


@pytest.mark.parametrize("cmd", [
    "codex exec -",                           # no sandbox flag -> inherits config (unsafe)
    "codex exec -s workspace-write -",        # writable sandbox
    "codex exec -s danger-full-access -",     # the incident mode
    "codex exec --dangerously-bypass-approvals-and-sandbox -",
    "codex exec -s read-only --dangerously-bypass-approvals-and-sandbox -",  # read-only + escape
    "my-wrapper --prompt {prompt_file}",      # unrecognized wrapper errs closed
    # substring-bypass regressions (finding critic-readonly-substring-wrapper-bypass):
    "my-wrapper -s read-only",                # contains the token but is NOT codex exec
    'codex exec --note "-s read-only" -',     # stray token inside a quoted argument, not a pin
    "codex review -s read-only -",            # codex, but not the `exec` subcommand
    'wrapper "codex exec -s read-only"',      # whole codex cmd buried in a wrapper's quoted arg
    "codex exec -s read-only extra unbalanced\"quote",  # unparseable -> err closed
    # conflicting-override regressions (finding critic-readonly-conflicting-sandbox-override):
    "codex exec -s read-only -s workspace-write -",           # later -s overrides the safe pin
    "codex exec -s workspace-write -s read-only -",           # unsafe pin present (order-independent)
    "codex exec -s read-only -c sandbox_mode=workspace-write -",  # conflicting -c sandbox_mode
    "codex exec --sandbox=read-only --sandbox=workspace-write -",  # conflicting = forms
    # shell-metachar regressions (finding critic-readonly-shell-metachar-bypass): a valid
    # read-only invocation with an appended/chained/redirected command via shell=True:
    "codex exec -s read-only - ; python write_graph.py",     # appended command
    "codex exec -s read-only - && rm -rf x",                 # && chain
    "codex exec -s read-only - | tee out.txt",               # pipe
    "codex exec -s read-only - > out.txt",                   # redirection
    "codex exec -s read-only - $(python evil.py)",           # command substitution
    "codex exec -s read-only - `whoami`",                    # backtick substitution
    # config long-form conflict (finding critic-readonly-config-long-form-override):
    "codex exec -s read-only --config sandbox_mode=workspace-write -",   # long form override
    "codex exec -s read-only --config=sandbox_mode=workspace-write -",   # = joined long form
    "codex exec --config sandbox_mode=workspace-write -",     # unsafe config-only, no -s
])
def test_codex_cmd_is_read_only_rejects_unsafe(cmd):
    assert watcher._codex_cmd_is_read_only(cmd) is False


def test_main_refuses_unsafe_codex_cmd_without_break_glass(tmp_path):
    """The watcher will not even start the critic under an unsafe sandbox unless the
    explicit break-glass flag is passed (Incident db5b243d)."""
    tok = tmp_path / "critic.tok"
    tok.write_text("x", encoding="utf-8")
    prompt = tmp_path / "critic.md"
    prompt.write_text("prompt", encoding="utf-8")
    argv = [
        "--base-url", "http://x", "--review-id", "r", "--token-file", str(tok),
        "--critic-prompt", str(prompt), "--codex-cmd", "codex exec -s danger-full-access -",
    ]
    with pytest.raises(SystemExit):  # argparse .error() exits
        watcher.main(argv)


class _FakeProc:
    returncode = 0
    stdout = "CRITIC-OUT"
    stderr = ""


def _fake_popen(captured):
    """A Popen stand-in for the invoker (B.8 C-3 moved it off subprocess.run so the child
    can be tracked and killed with its process group)."""

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["shell"] = kw.get("shell")
            captured["kw"] = kw
            self.returncode = 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            hook = captured.get("on_communicate")
            if hook is not None:
                hook(captured["argv"])
            return "CRITIC-OUT", ""

        def poll(self):
            return self.returncode

    return _FakePopen


def test_codex_invoker_runs_validated_argv_no_shell(monkeypatch):
    """codex_invoker executes a shlex-split argv with shell=False, so a shell never expands
    or chains anything: `$VAR` / `;` reach codex as literal args (finding
    critic-readonly-shell-expansion-bypass). The argv run == what the guard tokenizes."""
    captured = {}
    monkeypatch.setattr(watcher.subprocess, "Popen", _fake_popen(captured))
    invoke = watcher.codex_invoker("codex exec -s read-only -", cwd=None, timeout=10)
    assert invoke("PROMPT-BODY") == "CRITIC-OUT"
    assert captured["shell"] is False
    assert captured["argv"] == ["codex", "exec", "-s", "read-only", "-"]
    assert captured["input"] == "PROMPT-BODY"  # prompt on stdin when no {prompt_file}
    assert captured["argv"] == watcher._tokenize_codex_cmd("codex exec -s read-only -")


def test_codex_invoker_substitutes_prompt_file_token(monkeypatch):
    """`{prompt_file}` is replaced per token (space-safe) with the temp path; stdin unused."""
    captured = {}
    monkeypatch.setattr(watcher.subprocess, "Popen", _fake_popen(captured))
    invoke = watcher.codex_invoker("codex exec -s read-only {prompt_file}", cwd=None, timeout=10)
    invoke("BODY")
    assert captured["argv"][:4] == ["codex", "exec", "-s", "read-only"]
    assert captured["argv"][4] != "{prompt_file}" and captured["argv"][4].endswith(".md")
    assert captured["input"] is None


def test_codex_invoker_substitutes_embedded_prompt_file(monkeypatch):
    """An embedded placeholder (--input={prompt_file}) is substituted WITHIN the token, so the
    critic still receives the artifact prompt (regression
    critic-prompt-file-embedded-placeholder-regression)."""
    captured = {}
    monkeypatch.setattr(watcher.subprocess, "Popen", _fake_popen(captured))
    invoke = watcher.codex_invoker(
        "codex exec -s read-only --input={prompt_file}", cwd=None, timeout=10
    )
    invoke("BODY")
    embedded = captured["argv"][4]
    assert embedded.startswith("--input=") and embedded.endswith(".md")
    assert "{prompt_file}" not in embedded
    assert captured["input"] is None


def test_codex_invoker_prompt_file_alive_and_populated_at_invocation(monkeypatch):
    """The {prompt_file} temp file is created delete=False, so leaving the with-block CLOSES
    and flushes it but does NOT delete it; unlink runs in the finally, AFTER the child has
    run. So at the moment codex runs, the path exists and holds the prompt. This refutes
    finding critic-prompt-file-tempfile-deleted-before-use and guards a future delete=True."""
    import os

    seen = {}
    captured = {}

    def on_communicate(argv):
        path = argv[-1]  # standalone {prompt_file} token -> substituted path is the last arg
        seen["exists"] = os.path.exists(path)
        seen["content"] = (
            open(path, encoding="utf-8").read() if os.path.exists(path) else None
        )

    captured["on_communicate"] = on_communicate
    monkeypatch.setattr(watcher.subprocess, "Popen", _fake_popen(captured))
    invoke = watcher.codex_invoker("codex exec -s read-only {prompt_file}", cwd=None, timeout=10)
    invoke("PROMPT-CONTENT")
    assert seen["exists"] is True  # file present when codex runs
    assert seen["content"] == "PROMPT-CONTENT"  # and it holds the prompt


def test_codex_invoker_rejects_unparseable_template():
    with pytest.raises(ValueError):
        watcher.codex_invoker('codex exec -s read-only "unbalanced', cwd=None, timeout=10)


# --- B.6: the coverage binding (T1-4 / F-2 / G-3) --------------------------


def _cov_manifest(seq, rows, manifest_id="m1"):
    return {"artifact_seq": seq, "manifest_id": manifest_id, "tool_version": "1",
            "base": "aaa", "commit": "bbb", "granularity": "symbol", "rows": rows}


def test_result_messages_carries_the_coverage_report():
    """The report rides the SAME pass as the findings it accompanies. Without this the
    prompt would instruct the critic to emit a report its own binding silently drops —
    a prompt claiming a capability the enforcing layer does not have."""
    verdict = {
        "findings": {"artifact_seq": 4, "items": []},
        "coverage_report": {"manifest_id": "m1", "rows": [
            {"row_id": "hunt-by-name", "verdict": "reviewed-clean",
             "searches_performed": ["grep coverage"]}]},
        "status": {"value": "converged", "artifact_seq": 4},
    }
    posts = watcher.result_messages(verdict, 4)
    kinds = [p["kind"] for p in posts]
    assert kinds == ["findings", "coverage_report", "status"]
    # anchored to the reviewed version, and posted BEFORE the status so the report is on
    # the log when the server evaluates the declaration in the same batch
    assert posts[1]["payload"]["artifact_seq"] == 4
    assert posts[1]["role"] == "critic"


def test_result_messages_rejects_a_coverage_report_for_another_version():
    verdict = {
        "findings": {"artifact_seq": 4, "items": []},
        "coverage_report": {"artifact_seq": 3, "manifest_id": "m1", "rows": []},
        "status": {"value": "converged", "artifact_seq": 4},
    }
    with pytest.raises(ValueError):
        watcher.result_messages(verdict, 4)


def test_result_messages_without_a_coverage_report_is_unchanged():
    """Reviews with no manifest (and the intent-summary audit) post no report."""
    verdict = {"findings": {"artifact_seq": 1, "items": []},
               "status": {"value": "converged", "artifact_seq": 1}}
    assert [p["kind"] for p in watcher.result_messages(verdict, 1)] == ["findings", "status"]


def test_plan_reruns_a_pass_when_the_server_routes_a_coverage_sweep():
    """A declaration blocked ONLY by unreached rows leaves no new artifact, so the ordinary
    'newer artifact than I last reviewed' trigger cannot see it — and the sweep the server
    asked for would simply never happen."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1", "rows": []}),
        _msg(4, "critic", "status", {"value": "converged", "artifact_seq": 1}),
    ]
    assert watcher.plan(msgs)["action"] == "wait"
    msgs.append(_msg(5, "system", "notice",
                     {"phase": "convergence_blocked", "artifact_seq": 1,
                      "unreached_rows": ["r1"]}))
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 1,
        "mode": "code",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }
    # A report alone does NOT spend it: the pass that posted it may never end, which is not
    # hypothetical — a transport failure killed a pass between its report and its status on
    # 2026-07-22. Spending the signal there left an in-flight pass and a watcher waiting
    # forever for a version it already considered reviewed.
    msgs.append(_msg(6, "critic", "coverage_report",
                     {"artifact_seq": 1, "manifest_id": "m1", "rows": []}))
    assert watcher.plan(msgs)["action"] == "review", "the sweep is still owed"

    # ...it is spent once the pass that answered it ENDS
    msgs.append(_msg(7, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}))
    assert watcher.plan(msgs)["action"] == "wait"


def test_a_report_for_another_version_does_not_spend_the_coverage_signal():
    """Otherwise a report for a different artifact version stands in for the sweep that was
    actually asked for, and the watcher goes quiet with the rows still open."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "system", "notice",
             {"phase": "convergence_blocked", "artifact_seq": 1, "unreached_rows": ["r1"]}),
        _msg(4, "critic", "coverage_report",
             {"artifact_seq": 99, "manifest_id": "mX", "rows": []}),
    ]
    assert watcher.plan(msgs)["action"] == "review"


def test_coverage_repass_never_races_an_open_operator_item():
    """The stale-pass guard discards a pass completed while the operator is owed something,
    so scheduling one there burns an invocation for nothing."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "system", "notice", {"phase": "convergence_blocked", "unreached_rows": ["r1"]}),
        _msg(4, "critic", "human_question", {"id": "q1", "question": "?"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass


def test_compaction_keeps_the_current_coverage_messages_whole():
    rows = [{"row_id": "r1", "kind": "code", "locator": "src/a.py::f"},
            {"row_id": "hunt-by-name", "kind": "blind_edge", "locator": "hunt-by-name"}]
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d1"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, rows)),
        _msg(3, "critic", "coverage_report",
             {"artifact_seq": 1, "manifest_id": "m1",
              "rows": [{"row_id": "r1", "verdict": "reviewed-clean"}]}),
    ]
    out = watcher.compact_replay(msgs)
    assert out[1]["payload"]["rows"] == rows
    assert out[2]["payload"]["rows"][0]["verdict"] == "reviewed-clean"


def test_compaction_collapses_superseded_coverage_and_keeps_the_diagnostic():
    """Per-row verdicts scale with the diff — an uncompacted new message kind is how the
    context-overflow incident happens again. What must SURVIVE the collapse: every
    not-reached row with its reason, and a rolling union of rows ever claimed clean,
    each with its LOCATOR (a bare id is not self-describing once its manifest is gone)."""
    rows_v1 = [{"row_id": "r1", "kind": "code", "locator": "src/a.py::f"},
               {"row_id": "r2", "kind": "code", "locator": "src/b.py::g"},
               {"row_id": "hunt-by-name", "kind": "blind_edge", "locator": "hunt-by-name"}]
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d1"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, rows_v1)),
        _msg(3, "critic", "coverage_report",
             {"artifact_seq": 1, "manifest_id": "m1", "rows": [
                 {"row_id": "r1", "verdict": "reviewed-clean"},
                 {"row_id": "r2", "verdict": "not-reached", "reason": "generated file"},
                 {"row_id": "hunt-by-name", "verdict": "reviewed-clean",
                  "searches_performed": ["grep -r dispatch"]}]}),
        _msg(4, "development", "artifact", {"mode": "code", "diff": "d2"}),
        _msg(5, "development", "coverage_manifest", _cov_manifest(4, rows_v1, "m2")),
    ]
    out = watcher.compact_replay(msgs)
    old_manifest, old_report, new_manifest = out[1]["payload"], out[2]["payload"], out[4]["payload"]

    assert old_manifest["row_count"] == 3 and "rows" in old_manifest
    assert not isinstance(old_manifest["rows"], list)  # stubbed, not carried
    assert new_manifest["rows"] == rows_v1  # the CURRENT version stays whole

    assert old_report["counts"] == {"reviewed-clean": 2, "not-reached": 1}
    assert old_report["not_reached"][0]["reason"] == "generated file"
    union = {e["row_id"]: e for e in old_report["clean_union"]}
    assert union["r1"]["locator"] == "src/a.py::f"
    # the blind edge is kept WHOLE and outside the union — its evidence must survive under
    # every verdict, not only the one that happens to have a home in the union
    assert old_report["blind_edge"]["searches_performed"] == ["grep -r dispatch"]
    assert "hunt-by-name" not in union


def test_compaction_keeps_blind_edge_evidence_when_its_verdict_is_a_finding():
    """A blind-edge row whose verdict is `finding` sits in neither the clean union nor the
    not-reached list, so a verdict-shaped hole would delete the only evidence that the blind
    edge was searched — evidence the summary audit is required to check a disclosure
    against."""
    rows = [{"row_id": "hunt-by-name", "kind": "blind_edge", "locator": "hunt-by-name"}]
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d1"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, rows)),
        _msg(3, "critic", "coverage_report",
             {"artifact_seq": 1, "manifest_id": "m1", "rows": [
                 {"row_id": "hunt-by-name", "verdict": "finding", "finding_id": "f9",
                  "searches_performed": ["grep -r getattr(", "grep -r importlib"]}]}),
        _msg(4, "development", "artifact", {"mode": "code", "diff": "d2"}),
    ]
    out = watcher.compact_replay(msgs)
    blind = out[2]["payload"]["blind_edge"]
    assert blind["verdict"] == "finding"
    assert blind["searches_performed"] == ["grep -r getattr(", "grep -r importlib"]


def test_compaction_never_mutates_the_input():
    """The watcher reuses its replay list across passes."""
    rows = [{"row_id": "hunt-by-name", "kind": "blind_edge", "locator": "hunt-by-name"}]
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d1"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, rows)),
        _msg(3, "development", "artifact", {"mode": "code", "diff": "d2"}),
    ]
    before = json.loads(json.dumps(msgs))
    watcher.compact_replay(msgs)
    assert msgs == before


def test_a_blocker_for_a_superseded_artifact_never_reruns_forever():
    """The server refuses coverage reports for old versions, so a blocker naming one can
    never be spent — left live it would schedule passes over the current artifact forever,
    an unbounded invocation loop."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d1"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "critic", "status", {"value": "converged", "artifact_seq": 1}),
        _msg(4, "system", "notice",
             {"phase": "convergence_blocked", "artifact_seq": 1, "unreached_rows": ["r1"]}),
        _msg(5, "development", "artifact", {"mode": "code", "diff": "d2"}),
        _msg(6, "critic", "findings", {"artifact_seq": 5, "items": []}),
        _msg(7, "critic", "status", {"value": "needs_iteration", "artifact_seq": 5}),
    ]
    assert watcher.coverage_repass_due(msgs) is False
    assert watcher.plan(msgs)["action"] == "wait"


def test_a_missing_coverage_report_fails_the_pass_when_a_manifest_exists():
    """Without this the obligation lived only in the prompt: a pass that quietly dropped the
    report would post findings and a status, and the gate would block later with nobody able
    to say which pass had skipped it. Failing here routes into the existing retry."""
    verdict = {"findings": {"artifact_seq": 4, "items": []},
               "status": {"value": "converged", "artifact_seq": 4}}
    with pytest.raises(ValueError, match="coverage_report"):
        watcher.result_messages(verdict, 4, require_coverage=True)
    # ...and stays optional where no manifest was posted
    assert [p["kind"] for p in watcher.result_messages(verdict, 4)] == ["findings", "status"]


def test_manifest_presence_is_read_per_artifact_version():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, [])),
        _msg(3, "development", "artifact", {"mode": "code", "diff": "d2"}),
    ]
    assert watcher.manifest_posted_for(msgs, 1) is True
    assert watcher.manifest_posted_for(msgs, 3) is False


def test_a_manifest_posted_late_schedules_the_sweep_it_owes():
    """The recovery path no version-based trigger can see: development posts a manifest it
    had omitted, no new artifact arrives and no new blocker notice is emitted, so without
    this the review strands with a denominator nobody was scheduled to answer."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}),
    ]
    assert watcher.plan(msgs)["action"] == "wait"
    msgs.append(_msg(4, "development", "coverage_manifest", _cov_manifest(1, [])))
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 1,
        "mode": "code",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }
    # answered by a COMPLETED pass — the report alone is provisional until its status lands
    msgs.append(_msg(5, "critic", "coverage_report",
                     {"artifact_seq": 1, "manifest_id": "m1", "rows": []}))
    msgs.append(_msg(6, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}))
    assert watcher.plan(msgs)["action"] == "wait"


def test_an_unanswered_manifest_still_never_races_the_operator():
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(3, "development", "coverage_manifest", _cov_manifest(1, [])),
        _msg(4, "critic", "human_question", {"id": "q1", "question": "?"}),
    ]
    assert watcher.plan(msgs)["action"] != "review"  # a ping is also not a pass


def test_a_stale_report_does_not_answer_a_re_posted_manifest():
    """Keying on the artifact version alone would treat a report for the REPLACED
    denominator as an answer, leaving the current table unverdicted with nothing scheduled
    to fill it."""
    msgs = [
        _msg(1, "development", "artifact", {"mode": "code", "diff": "d"}),
        _msg(2, "development", "coverage_manifest", _cov_manifest(1, [], "m1")),
        _msg(3, "critic", "findings", {"artifact_seq": 1, "items": []}),
        _msg(4, "critic", "coverage_report", {"artifact_seq": 1, "manifest_id": "m1", "rows": []}),
        _msg(5, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}),
    ]
    assert watcher.plan(msgs)["action"] == "wait"
    msgs.append(_msg(6, "development", "coverage_manifest", _cov_manifest(1, [], "m2")))
    assert watcher.plan(msgs) == {
        "action": "review",
        "artifact_seq": 1,
        "mode": "code",
        "genre": None,
        "genre_roles": {"strategic_reader": None, "machine_comb": None},
        "intent_summary": False,
    }
    msgs.append(_msg(7, "critic", "coverage_report",
                     {"artifact_seq": 1, "manifest_id": "m2", "rows": []}))
    msgs.append(_msg(8, "critic", "status", {"value": "needs_iteration", "artifact_seq": 1}))
    assert watcher.plan(msgs)["action"] == "wait"


def test_a_pass_blocked_before_reading_may_return_neither_findings_nor_report():
    """The server accepts it and the prompt tells the critic to do it, so the binding must
    too — a pass legal in one layer and refused by the other is the defect this pairing
    exists to prevent. Recognised by `needs_human` with findings OMITTED, never by empty
    findings, which is a clean pass and owes its verdicts."""
    blocked = {"status": {"value": "needs_human", "artifact_seq": 4,
                          "detail": "could not read the subject"}}
    posts = watcher.result_messages(blocked, 4, require_coverage=True)
    assert [p["kind"] for p in posts] == ["status"]

    # a CLEAN pass still owes its report
    clean = {"findings": {"artifact_seq": 4, "items": []},
             "status": {"value": "needs_human", "artifact_seq": 4}}
    with pytest.raises(ValueError, match="coverage_report"):
        watcher.result_messages(clean, 4, require_coverage=True)


def test_findings_are_still_required_on_an_ordinary_pass():
    """The carve-out must not become a way to end any pass without findings."""
    with pytest.raises(ValueError, match="findings"):
        watcher.result_messages({"status": {"value": "needs_iteration", "artifact_seq": 4}}, 4)


def test_an_escalation_disqualifies_the_pre_review_blocker_carveout():
    """"I could not reach the subject" and "here is a defect I found in the base" cannot
    both be true of the same pass. Without this the carve-out was a way to emit substantive
    output while skipping the verdicts that output implies."""
    verdict = {
        "escalations": [{"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                         "claim": "bug", "evidence": "obs"}],
        "status": {"value": "needs_human", "artifact_seq": 4},
    }
    with pytest.raises(ValueError, match="findings"):
        watcher.result_messages(verdict, 4, require_coverage=True)


def test_a_human_question_does_not_disqualify_it():
    """"I cannot read the repository, how should I proceed" is exactly what a blocked pass
    has to be able to ask."""
    verdict = {
        "human_questions": [{"id": "q1", "question": "no repo access — proceed how?"}],
        "status": {"value": "needs_human", "artifact_seq": 4},
    }
    kinds = [p["kind"] for p in watcher.result_messages(verdict, 4, require_coverage=True)]
    assert kinds == ["human_question", "status"]


def test_missing_report_diagnostic_is_denominator_aware():
    """Round 13, finding b14-missing-report-retry-states-legacy-contract: the error is
    appended verbatim to the retry prompt, so it must state the contract the server
    will hold the retry against."""
    verdict = {"findings": {"items": [{"id": "f1", "claim": "c", "severity": "minor",
                                      "finding_type": "correctness", "location": "x"}]},
               "status": {"value": "needs_iteration"}}
    with pytest.raises(ValueError, match="not-reached with a reason"):
        watcher.result_messages(dict(verdict), 1, require_coverage=True)
    with pytest.raises(ValueError, match="instrument_failure"):
        watcher.result_messages(dict(verdict), 1, require_coverage=True,
                                question_denominator=True)
