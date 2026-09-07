# SPDX-License-Identifier: Apache-2.0
"""B.7 round gate: the loop stops every round, BEFORE anything is implemented.

Spec: docs/design/2026-08-08_round_gate_spec.md. Two halves, matching the code:

- the PURE core (`round_gate.compute`) — the fold that says where a round stands, what
  parks it, and which message kinds are legal there. Tested without a database, because
  the watcher reads the same answers and both consumers must be able to.
- the SERVER — the refusals at POST and the two operator finals' bookkeeping. Tested
  against the real repository, because "the server, not discipline, holds the loop closed"
  is the whole claim of this change (a rule that lives in a prompt was measured not to
  bind: two profile rules read, quoted, recorded and violated in one session).
"""

import pytest

from assistant_memory.review import round_gate as rg
from assistant_memory.review.errors import InvalidMessagePayloadError
from tests.instrument_helpers import seed_instruments
from assistant_memory.review.repository import (
    append_message,
    create_review,
    get_messages,
)

# --- replay builders (pure-core half) ------------------------------------


class Log:
    """A tiny channel builder: append messages, get the dict replay the code reads."""

    def __init__(self):
        self.messages: list[dict] = []

    def add(self, kind, payload=None, role="development") -> int:
        seq = len(self.messages) + 1
        self.messages.append(
            {"seq": seq, "role": role, "kind": kind, "payload": payload or {}}
        )
        return seq

    def artifact(self, **payload) -> int:
        return self.add("artifact", {"mode": "spec", **payload})

    def findings(self, *items, artifact_seq=1) -> int:
        return self.add(
            "findings",
            {"artifact_seq": artifact_seq, "items": list(items)},
            role="critic",
        )

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


def _directive(fid, directive="accept", **extra):
    return {"finding_id": fid, "directive": directive, "operator_quote": "ага", **extra}


# --- the fold: what opens a round, what parks it -------------------------


def test_clean_pass_opens_no_round():
    """G-1: a pass that raises zero findings creates no round gate at all — no proposals
    owed, no parking, straight into the existing convergence machinery."""
    log = Log()
    log.artifact()
    log.findings(artifact_seq=1)
    state = log.state()
    assert state.round is None
    assert state.state == rg.IDLE
    assert "artifact" in rg.LEGAL_KINDS[state.state]


def test_findings_park_nothing_until_proposals_land():
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    state = log.state()
    assert state.state == rg.PROPOSALS_OWED
    # Nothing may be implemented, and nothing but the proposals may be posted.
    assert rg.LEGAL_KINDS[state.state] == frozenset({"proposals"})


def test_mechanical_only_round_does_not_park_in_auto():
    """G-3: in `auto` a round of correctness/completeness/drift FIX proposals rides on the
    accepted proposal — the digest is still rendered, but development proceeds."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), _finding("f2", "drift"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1"), _entry("f2")]})
    state = log.state()
    assert state.state == rg.UNPARKED
    assert not state.gate_open
    # ...and their sanction is the proposal itself, which is what F-3 later keys on.
    assert {e.settled_outcome for e in state.round.entries.values()} == {"fix"}


def test_security_finding_parks_the_round():
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), _finding("s1", "security_mechanism"), artifact_seq=1)
    log.add(
        "proposals",
        {"entries": [
            _entry("f1"),
            _entry("s1", adversary="a second contributor", false_positive_cost="kills the feature"),
        ]},
    )
    state = log.state()
    assert state.gate_open
    assert state.round.waiting(state.mode) == ["s1"]


@pytest.mark.parametrize("outcome", ["waive", "escalate"])
def test_waive_and_escalate_proposals_wait_whatever_the_type(outcome):
    """A proposal to waive is top-of-digest and waits; so is an escalation. The measured
    failure both guard is accepting a defence because declining feels defensive."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1", outcome)]})
    assert log.state().gate_open


def test_all_wait_mode_holds_every_entry():
    """The operator's switch for complex subjects: every entry of the round waits."""
    log = Log()
    log.add("notice", {"phase": "gate_mode", "mode": "all_wait", "granted_by": "operator"})
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=2)
    log.add("proposals", {"entries": [_entry("f1")]})
    state = log.state()
    assert state.mode == "all_wait"
    assert state.gate_open and state.round.waiting("all_wait") == ["f1"]


def test_contested_finding_is_a_judgment_entry():
    """A finding the operator is already being asked about must not ride through on a
    mechanical proposal."""
    log = Log()
    log.artifact()
    log.add(
        "escalation",
        {"kind": "contested_fork", "finding_id": "f1", "detail": "oscillation"},
        role="system",
    )
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1")]})
    assert log.state().gate_open


def test_directives_unpark_and_the_version_opens_round_closing():
    log = Log()
    log.artifact()
    log.findings(_finding("s1", "security_mechanism"), artifact_seq=1)
    log.add("proposals", {"entries": [
        _entry("s1", adversary="nobody in the model", false_positive_cost="high"),
    ]})
    assert log.state().gate_open
    log.add("gate_directive", _directive("s1"))
    assert log.state().state == rg.UNPARKED
    log.artifact()  # the next version — its acceptance opens round_closing
    state = log.state()
    assert state.state == rg.ROUND_CLOSING
    assert state.round.owed_dispositions() == ["s1"]
    log.add("disposition", {"finding_id": "s1", "outcome": "fixed"})
    # Round closed: the ledger is settled and the next pass may run (H-1's barrier).
    assert log.state().state == rg.PASS_RUNNING


def test_report_request_reparks_and_only_a_ruling_settles():
    """W-2: `detail` / `class_analysis` are valid answers that leave their item PENDING —
    settlement is only `accept` / `amend`."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1", "waive")]})
    log.add("gate_directive", _directive("f1", "detail"))
    assert log.state().gate_open
    log.add("detail_report", {
        "directive_ref": "f1", "context": "c", "evidence": "e",
        "location": "l", "consequences": "q",
    })
    state = log.state()
    assert state.gate_open, "the report answers the request; the operator still has to rule"
    assert state.round.outstanding_reports() == []
    log.add("gate_directive", _directive("f1", "amend", amendment={"outcome": "waive", "reason": "no"}))
    assert log.state().state == rg.UNPARKED


def test_a_later_ruling_supersedes_an_outstanding_report_request():
    """The operator owns the gate and is never forced to wait for a report they asked for."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1", "waive")]})
    log.add("gate_directive", _directive("f1", "class_analysis"))
    assert log.state().gate_open
    log.add("gate_directive", _directive("f1"))
    assert log.state().state == rg.UNPARKED


def test_override_of_a_mechanical_entry_and_its_re_park():
    """G-3's override right, with W-2's re-park: the operator may reach into a round that
    never parked, and asking for a report there reopens the gate rather than dead-ending."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1")]})
    assert log.state().state == rg.UNPARKED
    log.add("gate_directive", _directive("f1", "detail"))
    state = log.state()
    assert state.gate_open
    assert "detail_report" in rg.LEGAL_KINDS[state.state]


def test_a_report_override_voids_the_auto_sanction_and_waits_for_a_ruling():
    """A report request is not a ruling. Asking for `detail` on an entry that rode on its
    accepted proposal takes that sanction back: the report answers the request, the operator
    answers the entry. Without this the round unparked the moment the report landed, with no
    `accept`/`amend` ever given (critic finding `mechanical-override-skips-reruling`)."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1")]})
    assert log.state().state == rg.UNPARKED  # mechanical, auto-sanctioned
    log.add("gate_directive", _directive("f1", "detail"))
    state = log.state()
    assert state.gate_open and state.round.entries["f1"].settled_outcome is None
    log.add("detail_report", {
        "directive_ref": "f1", "context": "c", "evidence": "e",
        "location": "l", "consequences": "q",
    })
    state = log.state()
    assert state.gate_open, "the report closes the request, not the entry"
    assert state.round.waiting(state.mode) == ["f1"]
    log.add("gate_directive", _directive("f1"))
    assert log.state().state == rg.UNPARKED


def test_a_terminal_review_takes_the_summary_and_nothing_else():
    """The terminal row's artifact cell is the summary exception, not an open door: an
    ordinary version would reopen a review the operator deliberately closed."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1")]})
    log.add("operator_finalize", {"mode": "now", "operator_quote": "стоп"})
    state = log.state()
    assert state.state == rg.TERMINAL
    assert rg.refusal(state, "artifact", {"mode": "code", "diff": "x"}, "development")
    assert rg.refusal(state, "artifact", {"intent_summary": True}, "development") is None


def test_a_terminal_review_disposes_only_its_audit_findings():
    """The audit cycle owes dispositions for its own findings and the review's ledger is
    otherwise closed. Opening the cell by KIND let an ordinary post-final disposition
    through (critic finding `terminal-allows-generic-disposition`); the opening is scoped to
    the audit path instead."""
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    log.add("proposals", {"entries": [_entry("f1")]})
    log.add("operator_finalize", {"mode": "now", "operator_quote": "стоп"})
    summary = log.artifact(intent_summary=True, summary_markdown="# итог")
    log.findings(_finding("a1", "completeness"), artifact_seq=summary)
    state = log.state()
    assert state.state == rg.TERMINAL and state.audit_findings == {"a1"}
    assert rg.refusal(state, "disposition", {"finding_id": "a1", "outcome": "fixed"},
                      "development") is None
    problem = rg.refusal(state, "disposition", {"finding_id": "f1", "outcome": "waived"},
                         "development")
    assert problem and "ledger" in problem


def test_summary_audit_findings_open_no_round():
    """Q-3: the round gate governs defect rounds only. The faithfulness-audit cycle over
    the post-review summary runs fully automatically — gate fatigue is a declared risk of
    B.7 and the audit is the backstop for it, so it must not be gated by it."""
    log = Log()
    log.artifact()
    log.findings(artifact_seq=1)
    summary = log.artifact(intent_summary=True, summary_markdown="...")
    log.findings(_finding("a1"), artifact_seq=summary)
    state = log.state()
    assert state.round is None
    assert "artifact" in rg.LEGAL_KINDS[state.state]


def test_pass_running_stands_aside_for_an_operator_owed_review():
    """`pass_running` refuses a new version while the critic works — and must not do so
    when the review is waiting on the OPERATOR, or a returned review could never post the
    version the operator asked for."""
    log = Log()
    log.artifact()
    assert log.state().state == rg.PASS_RUNNING
    log.add("human_question", {"id": "q1", "question": "which one?"}, role="critic")
    assert log.state().state == rg.IDLE
    log.add("decision_response", {"question_id": "q1", "answer": "that one"})
    assert log.state().state == rg.PASS_RUNNING


# --- open operator items: ONE implementation, both consumers --------------


def test_a_keyless_question_is_closed_by_any_later_operator_answer():
    """The DB side pools a keyless human_question, closed by ANY later operator answer (a
    question is milder than a contest, so its fail-safe is softer). The first replay copy
    turned it into an unclosable key instead — after which the server said "unparked"
    while the watcher waited on a human forever, and the stall alarm held its tongue
    because the silence looked human-owed."""
    log = Log()
    log.add("human_question", {"question": "which one?"}, role="critic")  # no id — legal
    assert rg.open_operator_items(log.messages)
    log.add("decision_response", {"finding_id": "unrelated", "held": True})
    assert not rg.open_operator_items(log.messages)


def test_one_answer_settles_every_escalation_under_its_key():
    """A contested element/finding is ONE fork however many escalations share its key
    (feedback e9cf8a67: the server auto-raises a contested_fork AND the critic posts its
    own for the same finding — the operator had to relay one identical ruling twice).
    The unit of the operator decision is the fork, and the replay side must agree with
    the DB side on that, or the watcher keeps waiting after the server unparked."""
    log = Log()
    log.add("escalation", {"finding_id": "f1", "kind": "contested_fork"}, role="system")
    log.add("escalation", {"finding_id": "f1", "kind": "operator_decision_challenge"},
            role="critic")
    assert rg.open_operator_items(log.messages)
    log.add("decision_response", {"finding_id": "f1", "held": True})
    assert not rg.open_operator_items(log.messages)
    # ...and order-awareness survives: an answer never pre-closes a future contest.
    log.add("escalation", {"finding_id": "f1", "kind": "contested_fork"}, role="system")
    assert rg.open_operator_items(log.messages)


# --- the legality table ---------------------------------------------------


def test_legality_table_refuses_by_state():
    log = Log()
    log.artifact()
    log.findings(_finding("f1"), artifact_seq=1)
    state = log.state()  # proposals owed
    assert rg.refusal(state, "artifact", {}, "development") is not None
    assert rg.refusal(state, "disposition", {}, "development") is not None
    assert rg.refusal(state, "proposals", {}, "development") is None
    # Non-round traffic is never gated: the gate constrains decisions, not records.
    for kind in ("findings", "notice", "escalation", "coverage_report", "status"):
        assert rg.refusal(state, kind, {}, "critic") is None
    # The server's own bookkeeping is not subject to the table it implements.
    assert rg.refusal(state, "disposition", {}, "system") is None


def test_refusal_says_what_is_owed():
    log = Log()
    log.artifact()
    log.findings(_finding("s1", "security_mechanism"), artifact_seq=1)
    log.add("proposals", {"entries": [
        _entry("s1", adversary="x", false_positive_cost="y"),
    ]})
    problem = rg.refusal(log.state(), "artifact", {}, "development")
    assert "s1" in problem and "gate is open" in problem


# --- resolved settings ----------------------------------------------------


def test_review_config_defaults_and_overrides():
    assert rg.resolved_config([])["ping"]["fallback_delay_s"] == 300
    assert rg.resolved_config([])["stall"]["bootstrap_threshold_s"] == 900
    log = Log()
    log.add("notice", {"phase": "review_config", "ping": {"fallback_delay_s": 60}})
    resolved = rg.resolved_config(log.messages)
    assert resolved["ping"]["fallback_delay_s"] == 60
    # ...and the roles that were not named keep their defaults rather than vanishing.
    assert resolved["ping"]["primary"] == "push"
    assert resolved["fault_stretch"]["attempts"] == 3


# --- the server: refusals at POST ----------------------------------------


async def _b7_review(session, mode="spec"):
    """A current-protocol review with NO coverage denominator, declared rather than assumed.

    B.11 A-1 gave "is coverage in play" a name, a default (true) and a home in the frozen
    config, because the old answer was inferred from an empty channel and was therefore
    wrong for the first version of every review. These tests are about the round gate and
    post nothing resembling a manifest, so they say so explicitly — which is the whole
    point of the setting: the absence of a denominator is now a stated decision.
    """
    issued = await create_review(
        session, slug="b7", mode=mode, instrument=await seed_instruments(session),
        config={"coverage": {"in_play": False}},
    )
    assert (issued.review.config or {}).get("protocol") == "B.7"
    assert (issued.review.config or {})["coverage"]["in_play"] is False
    return issued


async def _post(session, review_id, role, kind, payload):
    return await append_message(
        session, review_id=review_id, role=role, kind=kind, payload=payload
    )


async def _open_round(session, rid, *findings, mode="spec"):
    await _post(session, rid, "development", "artifact", {
        "mode": mode, "bundle": {"spec_markdown": "# s\n\n### E-1 — x\n"},
    })
    await _post(session, rid, "critic", "findings", {"artifact_seq": 1, "items": list(findings)})


async def test_findings_must_be_typed(session):
    issued = await _b7_review(session)
    rid = issued.review.id
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# s\n"},
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "findings", {
            "artifact_seq": 1, "items": [{"id": "f1", "title": "x"}],
        })
    assert "finding_type" in e.value.reason


async def test_findings_must_name_a_version_that_exists(session):
    """A findings message opens a ROUND, and a round is what the operator is asked to mark
    up — an anchor pointing at a sequence that is not an artifact would open a round over
    nothing (critic finding `findings-anchor-not-bound-to-current-artifact`)."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# s\n"},
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "findings", {
            "artifact_seq": 999,
            "items": [{"id": "f1", "finding_type": "correctness", "title": "x"}],
        })
    assert "not an artifact on this channel" in e.value.reason


async def test_security_finding_names_its_adversary(session):
    """G-6, paid for live: five findings deepened a package-origin check whose adversary
    requires a second contributor that does not exist, and the check's false positive
    killed the feature on the target platform."""
    issued = await _b7_review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _open_round(session, issued.review.id, {
            "id": "s1", "finding_type": "security_mechanism", "title": "no signature check",
        })
    assert "adversary" in e.value.reason


async def test_proposals_must_cover_the_pass_exactly(session):
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"), _finding("f2"))
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "proposals", {"entries": [_entry("f1")]})
    assert "f2" in e.value.reason
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "proposals", {
            "entries": [_entry("f1"), _entry("f2"), _entry("ghost")],
        })
    assert "ghost" in e.value.reason


async def test_proposal_entry_needs_its_context_sentence(session):
    """W-1: `context` is the sentence the digest opens the entry with, verbatim — so the
    self-containment of the operator's surface is a wire field, not a style request."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    entry = _entry("f1")
    entry.pop("context")
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "proposals", {"entries": [entry]})
    assert "context" in e.value.reason


async def test_escalate_proposal_types_its_recommendation(session):
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    entry = _entry("f1", "escalate")
    entry.pop("recommended_outcome")
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "proposals", {"entries": [entry]})
    assert "recommended_outcome" in e.value.reason


async def test_artifact_refused_while_the_gate_is_open(session):
    """G-2: the gate is mechanical. A gate development could skip under deadline gradient
    would fail unnoticed — the exact shape of the class-gate non-execution already
    measured."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, {
        "id": "s1", "finding_type": "security_mechanism", "title": "x",
        "adversary": "a second contributor", "false_positive_cost": "kills the feature",
    })
    await _post(session, rid, "development", "proposals", {"entries": [
        _entry("s1", adversary="none in the model", false_positive_cost="high"),
    ]})
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "artifact", {
            "mode": "spec", "bundle": {"spec_markdown": "# v2\n"},
        })
    assert "gate is open" in e.value.reason
    review = await session.get(type(issued.review), rid)
    assert review.parked is True


async def test_gate_directive_needs_a_real_finding_and_a_quote(session):
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1", "waive")]})
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "gate_directive", _directive("nope"))
    assert "not an entry of the open round" in e.value.reason
    bare = _directive("f1")
    bare.pop("operator_quote")
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "gate_directive", bare)
    assert "operator_quote" in e.value.reason


async def test_operator_initiated_amendment_carries_the_confirmed_paraphrase(session):
    """G-8: the symmetric half of the gate — development's proposals bind only after the
    operator's word, and the operator's instructions bind only after development's
    understanding of them is confirmed."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1", "waive")]})
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "gate_directive", _directive(
            "f1", "amend",
            amendment={"outcome": "fix", "plan": "do it my way", "operator_initiated": True},
        ))
    assert "paraphrase_quote" in e.value.reason
    await _post(session, rid, "development", "gate_directive", _directive(
        "f1", "amend",
        amendment={"outcome": "fix", "plan": "do it my way", "operator_initiated": True},
        paraphrase_quote="да, все верно",
    ))


async def test_class_report_refuses_a_bare_closure_claim(session):
    """C-1: in spec mode `lexical_only` is the only alternative to `structural`. Measured:
    four letter-perfect lexical sweeps, four surviving class members."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1", "waive")]})
    await _post(session, rid, "development", "gate_directive", _directive("f1", "class_analysis"))
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "class_report", {
            "directive_ref": "f1", "root": "the class", "enumeration_command": "grep -rn x",
            "candidates": [], "closure_kind": "closed", "false_negative_mode": "n/a",
        })
    assert "closure_kind" in e.value.reason
    await _post(session, rid, "development", "class_report", {
        "directive_ref": "f1", "root": "the class", "enumeration_command": "grep -rn x",
        "candidates": [{"locator": "a.py::f", "state": "proposed_fix", "reason": "same shape"}],
        "closure_kind": "lexical_only",
        "false_negative_mode": "this grep cannot see statements phrased differently",
    })


async def test_report_for_a_closed_request_is_refused_per_message(session):
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1", "waive")]})
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "detail_report", {
            "directive_ref": "f1", "context": "c", "evidence": "e",
            "location": "l", "consequences": "q",
        })
    assert "no OPEN 'detail' directive" in e.value.reason


async def test_fixed_disposition_carries_its_class_closure(session):
    """K-1: the schema binds the MAKING of the class claim; its truth stays the critic's
    target, not the validator's."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1")]})
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# v2\n"},
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "disposition", {
            "finding_id": "f1", "outcome": "fixed", "artifact_seq": 3, "reason": "done",
        })
    assert "class_closure" in e.value.reason
    await _post(session, rid, "development", "disposition", {
        "finding_id": "f1", "outcome": "fixed", "artifact_seq": 3, "reason": "done",
        "class_closure": {
            "kind": "class", "enumeration_command": "grep -rn 'x' src/",
            "closure_kind": "structural",
            "false_negative_mode": "the checker reads the spec, not the files it cites",
        },
    })


async def test_development_escalation_records_the_third_option_search(session):
    """FF-1: four of five operator-escalated forks closed by reformulation, not choice."""
    issued = await _b7_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "escalation", {
            "kind": "new_decision", "element_id": "E-1", "detail": "A or B?",
        })
    assert "third_option_search" in e.value.reason
    # The critic's own escalations are not what the measured waste came from.
    await _post(session, rid, "critic", "escalation", {
        "kind": "contested_disposition", "finding_id": "f9", "detail": "the waiver is thin",
    })


async def test_server_only_outcomes_are_not_postable(session):
    issued = await _b7_review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "development", "disposition", {
            "finding_id": "f1", "outcome": "fixed_unverified",
        })
    assert "emitted by the server" in e.value.reason


# --- the two operator finals ---------------------------------------------


async def _outcomes(session, rid) -> dict:
    return {
        (m.payload or {}).get("finding_id"): (m.payload or {}).get("outcome")
        for m in await get_messages(session, rid, after=0)
        if m.kind == "disposition"
    }


async def test_finalize_now_closes_everything_as_operator_risk_accepted(session):
    """F-2: one uniform rule, in every accepting state, with no state-dependent branching.
    Nothing is dropped silently; the closing hand is the operator's."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"), _finding("f2", "proportionality"))
    await _post(session, rid, "development", "proposals", {
        "entries": [_entry("f1"), _entry("f2", "waive")],
    })
    await _post(session, rid, "development", "operator_finalize", {
        "mode": "now", "operator_quote": "хватит, останавливаем",
    })
    assert await _outcomes(session, rid) == {
        "f1": rg.OPERATOR_RISK_ACCEPTED, "f2": rg.OPERATOR_RISK_ACCEPTED,
    }
    review = await session.get(type(issued.review), rid)
    assert review.state == "operator_finalized" and review.parked is False


async def test_finalize_after_fixes_labels_by_the_settled_directive(session):
    """F-3: total, keyed on the settled directive — a settled operator ruling is never
    relabelled as unaddressed risk, and `fixed_unverified` never labels unimplemented work."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(
        session, rid,
        _finding("f1"), _finding("f2", "proportionality"), _finding("f3", "proportionality"),
    )
    await _post(session, rid, "development", "proposals", {
        "entries": [_entry("f1"), _entry("f2", "waive"), _entry("f3")],
    })
    await _post(session, rid, "development", "gate_directive", _directive("f2"))  # waive stands
    await _post(session, rid, "development", "gate_directive", _directive("f3", "detail"))
    await _post(session, rid, "development", "operator_finalize", {
        "mode": "after_fixes", "operator_quote": "доделай принятое и стоп",
    })
    review = await session.get(type(issued.review), rid)
    assert review.state == "operator_finalizing"
    assert await _outcomes(session, rid) == {}, "nothing closes until the final version lands"
    # Exactly one further message is accepted here: the final artifact.
    with pytest.raises(InvalidMessagePayloadError):
        await _post(session, rid, "development", "gate_directive", _directive("f3"))
    # The final version attests what its label will claim — see the test below.
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# final\n"}, "tests_green": True,
        "self_audit": "suite green, directives implemented as sanctioned",
    })
    assert await _outcomes(session, rid) == {
        "f1": rg.FIXED_UNVERIFIED,          # mechanical, sanctioned by the accepted proposal
        "f2": "waived",                      # a settled operator waive keeps its meaning
        "f3": rg.OPERATOR_RISK_ACCEPTED,     # pending a report — never directed
    }
    review = await session.get(type(issued.review), rid)
    assert review.state == "operator_finalized"


async def test_the_after_fixes_final_must_attest_what_its_label_claims(session):
    """On this artifact's arrival the server closes the sanctioned findings as
    `fixed_unverified` — fixed, with the critic's verifying pass skipped by operator
    decision. That is the strongest label in the ledger, and it was resting on nothing
    (critic finding `operator-final-artifact-attestation-unenforced`)."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1", "waive")]})
    await _post(session, rid, "development", "operator_finalize", {
        "mode": "after_fixes", "operator_quote": "доделай принятое и стоп",
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "artifact", {
            "mode": "spec", "bundle": {"spec_markdown": "# final"},
        })
    assert "self_audit" in e.value.reason and "tests_green" in e.value.reason
    with pytest.raises(InvalidMessagePayloadError):
        await _post(session, rid, "development", "artifact", {
            "mode": "spec", "bundle": {"spec_markdown": "# final"},
            "self_audit": "done", "tests_green": "yes",  # a string is not an attestation
        })
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# final"},
        "self_audit": "suite green, directives implemented as sanctioned",
        "tests_green": True,
    })


async def test_finalize_after_fixes_in_round_closing_takes_the_accepted_version(session):
    """W-3: issued in `round_closing` the round's already-accepted version IS the final —
    no further artifact is expected, and the closure labels are the settled directives'."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1")]})
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# v2\n"},
    })
    await _post(session, rid, "development", "operator_finalize", {
        "mode": "after_fixes", "operator_quote": "стоп, версия уже принята",
    })
    assert await _outcomes(session, rid) == {"f1": rg.FIXED_UNVERIFIED}
    review = await session.get(type(issued.review), rid)
    assert review.state == "operator_finalized"


# --- the initialization window -------------------------------------------


async def test_gate_mode_binds_once_and_only_before_the_first_version(session):
    issued = await _b7_review(session)
    rid = issued.review.id
    await _post(session, rid, "development", "notice", {
        "phase": "gate_mode", "mode": "all_wait", "granted_by": "operator at the pre-review gate",
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "notice", {
            "phase": "gate_mode", "mode": "auto", "granted_by": "operator",
        })
    assert "already carries" in e.value.reason
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# s\n"},
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "notice", {
            "phase": "review_config", "ping": {"fallback_delay_s": 60},
        })
    assert "initialization window" in e.value.reason


async def test_review_config_refuses_a_non_positive_threshold(session):
    issued = await _b7_review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "development", "notice", {
            "phase": "review_config", "stall": {"bootstrap_threshold_s": 0},
        })
    assert "positive number" in e.value.reason


async def test_a_disposition_must_match_the_sanction_it_closes(session):
    """The operator's ruling is what licenses the work — so closing a sanctioned fix as
    `waived` would substitute development's judgement for theirs at the last step, after
    the gate, where nobody is looking any more (critic finding
    `disposition-outcome-not-bound-to-sanction`)."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1")]})
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# v2\n"},
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "disposition", {
            "finding_id": "f1", "outcome": "waived", "artifact_seq": 3,
            "reason": "on second thought, no",
        })
    assert "settled as 'fix'" in e.value.reason
    await _post(session, rid, "development", "disposition", {
        "finding_id": "f1", "outcome": "fixed", "artifact_seq": 3, "reason": "done",
        "class_closure": {"kind": "cell", "false_negative_mode": "one call site, read whole"},
    })


async def test_the_post_review_summary_survives_an_operator_final(session):
    """The operator's final is followed by the summary they read at their gate, and by the
    critic's audit of it. Refusing that traffic would make the final unreachable in
    practice — an operator final stops DEFECT rounds only."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1")]})
    await _post(session, rid, "development", "operator_finalize", {
        "mode": "now", "operator_quote": "стоп",
    })
    summary = await _post(session, rid, "development", "artifact", {
        "intent_summary": True, "converged_artifact_seq": 1, "summary_markdown": "# итог",
    })  # anchor = the review's only ordinary version
    await _post(session, rid, "critic", "findings", {
        "artifact_seq": summary.seq,
        "items": [{"id": "a1", "finding_type": "completeness",
                   "title": "the summary omits the waiver"}],
    })
    # The audit cycle is not a defect round: no proposals are owed and its findings are
    # disposed of directly, exactly as before B.7.
    await _post(session, rid, "development", "disposition", {
        "finding_id": "a1", "outcome": "fixed", "artifact_seq": summary.seq,
        "reason": "folded in",
        "class_closure": {"kind": "cell", "false_negative_mode": "one document, read whole"},
    })


async def test_the_summary_must_name_the_version_it_summarizes(session):
    """The summary is what the operator reads at their gate, and it claims to describe a
    specific version. Accepting any integer let that claim point anywhere — at a version
    nobody reviewed, or at nothing (critic finding `intent-summary-anchor-is-unverified`)."""
    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"))
    await _post(session, rid, "development", "proposals", {"entries": [_entry("f1")]})
    await _post(session, rid, "development", "operator_finalize", {
        "mode": "now", "operator_quote": "стоп",
    })
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "development", "artifact", {
            "intent_summary": True, "converged_artifact_seq": 999,
            "summary_markdown": "# итог",
        })
    assert "latest ordinary version" in e.value.reason
    await _post(session, rid, "development", "artifact", {
        "intent_summary": True, "converged_artifact_seq": 1, "summary_markdown": "# итог",
    })


async def test_service_tail_is_computed_and_honest(session):
    """F-4: the tail is rendered by the SERVER from the journal — a computed artifact
    development copies verbatim, not prose it authors. Absent machinery is stated as
    absent; an undisposed finding is reported as a hole, not skipped."""
    from assistant_memory.review.render import render_service_tail

    issued = await _b7_review(session)
    rid = issued.review.id
    await _open_round(session, rid, _finding("f1"), _finding("f2", "proportionality"))
    await _post(session, rid, "development", "proposals", {
        "entries": [_entry("f1"), _entry("f2", "waive")],
    })
    await _post(session, rid, "development", "gate_directive", _directive("f2", "class_analysis"))
    await _post(session, rid, "development", "class_report", {
        "directive_ref": "f2", "root": "the class", "enumeration_command": "grep -rn x",
        "candidates": [], "closure_kind": "lexical_only",
        "false_negative_mode": "cannot see differently-phrased statements",
    })
    messages = [
        {"seq": m.seq, "role": m.role, "kind": m.kind, "payload": m.payload or {},
         "created_at": None}
        for m in await get_messages(session, rid, after=0)
    ]
    tail = render_service_tail(messages, state="in flight")
    assert "UNDISPOSED" in tail, "a finding with no terminal disposition is a hole, reported"
    assert "NO coverage machinery" in tail
    assert "1 of them demanded a class analysis" in tail
    assert "`f2`" in tail and "proportionality" in tail


async def test_the_audience_genre_is_out_of_scope(session):
    """S-2, an explicit non-goal: the audience genre has its own gates, its own pass plan
    and its own convergence conditions. Layering a second gate over them was never
    designed — and the exclusion is keyed on the genre rather than on somebody remembering
    to pin the protocol."""
    issued = await create_review(
        session, slug="deck", mode="spec",
        config={
            "genre": "audience", "cold_verdict_first": False,
            "coverage": {"in_play": False},
        },
        instrument=await seed_instruments(session),
    )
    rid = issued.review.id
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# deck\n"},
    })
    await _post(session, rid, "critic", "findings", {
        "artifact_seq": 1, "items": [{"id": "f1", "title": "untyped, as that genre posts them"}],
    })
    await _post(session, rid, "development", "disposition", {
        "finding_id": "f1", "outcome": "fixed", "artifact_seq": 1, "reason": "done",
    })


async def test_a_pre_b7_review_is_untouched(session):
    """R-1: the gate applies to reviews created after deploy. Retrofitting it onto a
    channel whose findings were never proposed against would refuse the messages it is
    owed."""
    issued = await create_review(session, slug="old", mode="spec", config={"protocol": "B.6"})
    rid = issued.review.id
    await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# s\n"},
    })
    await _post(session, rid, "critic", "findings", {
        "artifact_seq": 1, "items": [{"id": "f1", "title": "untyped, as before"}],
    })
    await _post(session, rid, "development", "disposition", {
        "finding_id": "f1", "outcome": "fixed", "artifact_seq": 1, "reason": "done",
    })


# --- B.10 A-1/A-4: the artifact contract — a sibling stamp, refusal keyed on it -------

from assistant_memory.models.review import Review  # noqa: E402
from assistant_memory.review.repository import (  # noqa: E402
    ARTIFACT_CONTRACT_B10,
    ARTIFACT_CONTRACT_LEGACY,
    artifact_contract_current,
)

_B10_BASE = "a" * 40
_B10_COMMIT = "b" * 40


async def _strip_contract_stamp(session, issued):
    """Simulate a review created by pre-B.10 code: the sibling stamp absent entirely.
    (An explicit legacy pin is the OTHER path; both must admit the old forms.)"""
    review = issued.review
    review.config = {
        k: v for k, v in (review.config or {}).items() if k != "artifact_contract"
    }
    await session.flush()
    return issued


async def test_b10_contract_pin_table(session):
    """A-4's full pin table (finding b10-artifact-contract-pin-alias): what creation
    stamps for every pin combination — the boundary is authoritative data on the
    review, never a clock."""
    instrument = await seed_instruments(session)
    # no pin -> the current generation
    no_pin = await create_review(session, slug="p1", mode="code", instrument=instrument)
    assert no_pin.review.config["artifact_contract"] == ARTIFACT_CONTRACT_B10
    # an explicit pin of the CURRENT protocol constant is not a reproduction -> current
    cur_pin = await create_review(
        session, slug="p2", mode="code", instrument=instrument,
        config={"protocol": rg.PROTOCOL},
    )
    assert cur_pin.review.config["artifact_contract"] == ARTIFACT_CONTRACT_B10
    # an explicit OLDER protocol pin is the existing reproduction predicate -> legacy
    old_pin = await create_review(
        session, slug="p3", mode="code", config={"protocol": "B.6"}
    )
    assert old_pin.review.config["artifact_contract"] == ARTIFACT_CONTRACT_LEGACY
    # the explicit legacy pin of the artifact contract ITSELF -> legacy; needed because
    # the protocol constant did not change across B.8/B.9, so a pre-B.10 review whose
    # protocol was already current is reproducible no other way
    hatch = await create_review(
        session, slug="p4", mode="code", instrument=instrument,
        config={"artifact_contract": "legacy"},
    )
    assert hatch.review.config["artifact_contract"] == ARTIFACT_CONTRACT_LEGACY


async def test_b10_contract_stamp_survives_a_restart(session):
    """A-4: the stamp is config data in the database — a service restart reads the
    same review row and the same contract; nothing is re-derived at runtime."""
    issued = await create_review(
        session, slug="stamp", mode="code", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    session.expire_all()  # drop every in-memory ORM state: the "restarted" service reads cold
    fresh = await session.get(Review, rid)
    assert fresh.config["artifact_contract"] == ARTIFACT_CONTRACT_B10
    assert artifact_contract_current(fresh.config)


async def test_b10_stamp_leaves_round_gate_and_instrument_untouched(session):
    """A-4's regression-tested-unchanged consumers (finding
    b10-protocol-stamp-preservation-undefined): `round_gate.in_force` is a literal
    equality on `config.protocol` and creation's reproduction predicate reads the same
    key — the sibling stamp must change neither, under any pin."""
    instrument = await seed_instruments(session)
    current = await create_review(session, slug="rg1", mode="code", instrument=instrument)
    assert current.review.config["protocol"] == rg.PROTOCOL
    assert rg.in_force(current.review.config)
    assert current.review.config.get("instrument")  # the freeze still ran
    # the legacy-contract hatch pins the CONTRACT only: the round gate and the
    # instrument freeze govern exactly as for any current-protocol review
    hatch = await create_review(
        session, slug="rg2", mode="code", instrument=instrument,
        config={"artifact_contract": "legacy"},
    )
    assert hatch.review.config["protocol"] == rg.PROTOCOL
    assert rg.in_force(hatch.review.config)
    assert hatch.review.config.get("instrument")
    # an older-protocol reproduction keeps its pre-B.7 behaviour, as before B.10
    old = await create_review(session, slug="rg3", mode="spec", config={"protocol": "B.6"})
    assert not rg.in_force(old.review.config)


async def test_b10_inline_diff_refused_on_a_current_contract_review(session):
    """A-1: for reviews created under B.10 a code-mode artifact carrying a `diff`
    field is refused at POST with an error naming the referential form."""
    issued = await _b7_review(session, mode="code")
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, issued.review.id, "development", "artifact", {
            "mode": "code", "diff": "--- a\n+++ b\n",
            "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
        })
    assert "NO inline `diff`" in str(exc.value)
    assert "artifact_ref" in str(exc.value)


async def test_b10_movable_names_and_prefixes_refused_by_form(session):
    """A-1 (finding b10-ref-must-be-immutable): a branch or tag name, or an
    abbreviated prefix, is not a subject — a movable name recorded in an append-only
    channel resolves to different bytes over time."""
    issued = await _b7_review(session, mode="code")
    for base, commit in (
        ("main", _B10_COMMIT),          # a movable branch name
        (_B10_BASE, "v1.2.0"),          # a tag name
        ("90fd9c0", _B10_COMMIT),       # an abbreviated prefix
        (_B10_BASE, _B10_COMMIT.upper()),  # not the canonical lowercase form
    ):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, issued.review.id, "development", "artifact", {
                "mode": "code", "artifact_ref": {"base": base, "commit": commit},
            })
        assert "40-hex" in str(exc.value)


async def test_b10_ref_only_code_artifact_accepted_on_current_contract(session):
    issued = await _b7_review(session, mode="code")
    msg = await _post(session, issued.review.id, "development", "artifact", {
        "mode": "code", "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
    })
    assert msg.seq == 1


async def test_b10_inline_diff_still_accepted_on_an_unmarked_review(session):
    """A-4: a review without the stamp — every pre-B.10 review — keeps its contract
    by construction."""
    issued = await _strip_contract_stamp(session, await _b7_review(session, mode="code"))
    msg = await _post(session, issued.review.id, "development", "artifact", {
        "mode": "code", "diff": "--- a\n+++ b\n",
        "artifact_ref": {"base": "aaa", "commit": "bbb"},
    })
    assert msg.seq == 1


async def test_b10_inline_diff_accepted_on_an_explicitly_legacy_pinned_review(session):
    """A-4: a legacy contract admits the inline diff — reproduction means
    reproduction."""
    issued = await create_review(
        session, slug="legacy-pin", mode="code",
        instrument=await seed_instruments(session),
        config={"artifact_contract": "legacy"},
    )
    msg = await _post(session, issued.review.id, "development", "artifact", {
        "mode": "code", "diff": "--- a\n+++ b\n",
        "artifact_ref": {"base": "short", "commit": "also-short"},
    })
    assert msg.seq == 1


# --- B.10 B-1: the referential spec subject on the wire -------------------------------


async def test_b10_referential_spec_subject_accepted(session):
    """B-1: an ALTERNATIVE subject form — ref + repository-relative path, no inline
    bundle. Every server-side check is form-level: the service resolves nothing."""
    issued = await _b7_review(session, mode="spec")
    msg = await _post(session, issued.review.id, "development", "artifact", {
        "mode": "spec",
        "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
        "path": "docs/design/spec.md",
    })
    assert msg.seq == 1


async def test_b10_both_spec_subject_forms_refused(session):
    """B-1: exactly one subject form per message — two subjects that can diverge
    silently is the defect class the coverage machinery exists to prevent."""
    issued = await _b7_review(session, mode="spec")
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, issued.review.id, "development", "artifact", {
            "mode": "spec",
            "bundle": {"spec_markdown": "# s\n"},
            "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
            "path": "docs/design/spec.md",
        })
    assert "EXACTLY ONE subject form" in str(exc.value)


async def test_b10_referential_spec_subject_form_is_strict(session):
    """The same strict helper as A-1 covers the spec ref; the path must be
    repository-relative."""
    issued = await _b7_review(session, mode="spec")
    rid = issued.review.id
    # an abbreviated ref is refused by form
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "development", "artifact", {
            "mode": "spec", "artifact_ref": {"base": "90fd9c0", "commit": _B10_COMMIT},
            "path": "docs/design/spec.md",
        })
    assert "40-hex" in str(exc.value)
    # a missing path is not a subject
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, rid, "development", "artifact", {
            "mode": "spec", "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
        })
    assert "`path`" in str(exc.value)
    # absolute paths and parent traversal are refused by form
    for path in ("/etc/passwd", "C:/x/spec.md", "../outside.md", "docs/../../x.md"):
        with pytest.raises(InvalidMessagePayloadError) as exc:
            await _post(session, rid, "development", "artifact", {
                "mode": "spec",
                "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
                "path": path,
            })
        assert "repository-relative" in str(exc.value)


async def test_b10_inline_spec_bundle_stays_fully_legal(session):
    """B-1/B-3: the referential form is an available alternative, not an obligation —
    subjects that do not live in git (audience decks) keep the inline bundle."""
    issued = await _b7_review(session, mode="spec")
    msg = await _post(session, issued.review.id, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# deck\n"},
    })
    assert msg.seq == 1


async def test_b10_referential_spec_subject_refused_on_legacy_reviews(session):
    """The referential form did not exist before B.10; an unmarked or legacy review
    keeps its prior accepted form — inline bundle only."""
    issued = await create_review(session, slug="old-spec", mode="spec",
                                 config={"protocol": "B.6"})
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await _post(session, issued.review.id, "development", "artifact", {
            "mode": "spec",
            "artifact_ref": {"base": _B10_BASE, "commit": _B10_COMMIT},
            "path": "docs/design/spec.md",
        })
    assert "bundle.spec_markdown" in str(exc.value)
