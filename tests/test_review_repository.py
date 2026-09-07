# SPDX-License-Identifier: Apache-2.0
"""Review-orchestration core protocol tests (spec docs/design/2026-07-07_review_orchestration_spec.md).

Focus: per-review token isolation (§8), monotonic `seq` + the long-poll cursor
(§3.2), the message-driven FSM (§5), and — the load-bearing part — the server-side
convergence guard (§10, INV-2/INV-4): its four refusal paths and the one accept path.
"""

import uuid

import pytest

from assistant_memory.models.review import Review
from assistant_memory.review.errors import (
    ConvergenceRefusedError,
    InvalidMessagePayloadError,
    InvalidReviewTokenError,
    InvalidStateTransitionError,
    ReviewNotFoundError,
    RevokedReviewTokenError,
)
from assistant_memory.review.repository import (
    advance_state,
    append_message,
    create_review,
    get_messages,
    resolve_review_token,
    revoke_review_tokens,
)
from tests.instrument_helpers import seed_instruments


async def _new_review(session, mode="spec"):
    """A review on the PRE-B.7 protocol — the round structure these tests describe.

    B.7's round gate is the default for reviews created from now on, and it changes the
    legal message sequence (a finding is proposed on before it is fixed; dispositions live
    in `round_closing`). The machinery below — the convergence guard, the oscillation
    detector, the coverage ledger — is unchanged by that and still runs for every review
    created before the deploy, so these tests keep exercising it on a review pinned to the
    old protocol. The round gate has its own suite (`test_review_round_gate.py`).
    """
    issued = await create_review(session, slug="demo", mode=mode, config={"protocol": "B.6"})
    return issued


# --- creation, tokens, isolation (§8) ------------------------------------


async def test_create_review_mints_two_distinct_scoped_tokens(session):
    issued = await _new_review(session)
    assert issued.dev_token != issued.critic_token
    assert issued.review.state == "created"

    dev = await resolve_review_token(session, issued.dev_token)
    critic = await resolve_review_token(session, issued.critic_token)
    assert dev.review_id == critic.review_id == issued.review.id
    # role is a label only (option X) — but the mint records it correctly.
    assert dev.role == "development"
    assert critic.role == "critic"


async def test_unknown_review_mode_rejected_early(session):
    with pytest.raises(ValueError):
        await create_review(session, slug="x", mode="prose")


async def test_resolve_bad_token_raises(session):
    with pytest.raises(InvalidReviewTokenError):
        await resolve_review_token(session, "not-a-real-token")


async def test_two_reviews_do_not_cross(session):
    instrument = await seed_instruments(session)
    a = await create_review(session, slug="a", mode="spec", instrument=instrument)
    b = await create_review(session, slug="b", mode="code", instrument=instrument)
    resolved = await resolve_review_token(session, a.dev_token)
    assert resolved.review_id == a.review.id
    assert resolved.review_id != b.review.id


async def test_revoke_tokens_blocks_resolution(session):
    issued = await _new_review(session)
    await revoke_review_tokens(session, issued.review.id)
    with pytest.raises(RevokedReviewTokenError):
        await resolve_review_token(session, issued.dev_token)


# --- messages: monotonic seq + long-poll cursor (§3.2, §6) ---------------


async def test_seq_is_monotonic_per_review(session):
    issued = await _new_review(session)
    rid = issued.review.id
    m1 = await append_message(session, review_id=rid, role="development", kind="notice", payload={"text": "a"})
    m2 = await append_message(session, review_id=rid, role="development", kind="notice", payload={"text": "b"})
    m3 = await append_message(session, review_id=rid, role="development", kind="notice", payload={"text": "c"})
    assert [m1.seq, m2.seq, m3.seq] == [1, 2, 3]


async def test_get_messages_after_cursor(session):
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="notice", payload={"text": "a"})
    m2 = await append_message(session, review_id=rid, role="development", kind="notice", payload={"text": "b"})
    fresh = await get_messages(session, rid, after=m2.seq - 1)
    assert [m.seq for m in fresh] == [m2.seq]
    assert await get_messages(session, rid, after=m2.seq) == []


async def test_append_to_missing_review_raises(session):
    with pytest.raises(ReviewNotFoundError):
        await append_message(
            session, review_id=uuid.uuid4(), role="development", kind="notice", payload={}
        )


# --- message-driven FSM (§5) ---------------------------------------------


async def test_artifact_moves_to_ready_and_marks_material_change(session):
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    review = await session.get(Review, rid)
    assert review.state == "artifact_ready"
    assert review.last_material_change_at is not None


async def test_needs_iteration_increments_and_moves_to_disposing(session):
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": 1})
    review = await session.get(Review, rid)
    assert review.state == "dev_disposing"
    assert review.iteration == 1


async def test_escalation_parks_and_decision_unparks(session):
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "operator_decision_challenge", "element_id": "d1", "detail": "x"})
    assert (await session.get(Review, rid)).parked is True
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"element_id": "d1", "held": True})
    assert (await session.get(Review, rid)).parked is False


# --- lifecycle transitions (§5) ------------------------------------------


async def test_illegal_transition_refused(session):
    issued = await _new_review(session)  # state=created
    with pytest.raises(InvalidStateTransitionError):
        await advance_state(session, issued.review.id, "finalized")


async def test_finalize_revokes_tokens_and_stamps_closed(session):
    issued = await _new_review(session)
    rid = issued.review.id
    # created -> ... -> converged -> operator_gate -> finalized (walk the legal path)
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await _drive_to_converged(session, rid)
    await advance_state(session, rid, "operator_gate")
    review = await advance_state(session, rid, "finalized")
    assert review.state == "finalized"
    assert review.closed_at is not None
    with pytest.raises(RevokedReviewTokenError):
        await resolve_review_token(session, issued.dev_token)


# --- the load-bearing convergence guard (§10, INV-2, INV-4) --------------


async def _drive_to_converged(session, rid):
    """Walk a review to a legitimately-convergeable state: one finding raised on v1,
    fixed, a new artifact v2 posted, and a clean verifying pass over v2."""
    # v1 already posted by the caller (seq 1). Critic raises f1 against v1.
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": 1, "items": [{"id": "f1", "severity": "minor"}]})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": 1})
    # Dev disposes f1 and posts v2.
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": 1, "outcome": "fixed", "reason": "done"})
    v2 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    # Critic's clean verifying pass over v2.
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v2.seq, "items": []})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "iteration": 2, "artifact_seq": v2.seq})
    return v2.seq


async def test_converged_accepted_on_consistent_pass(session):
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await _drive_to_converged(session, rid)
    assert (await session.get(Review, rid)).state == "converged"


async def test_converged_refused_without_pickup(session):
    """Converged only from critic_reviewing (operator ruling 2026-07-08): a critic that
    skipped the pickup step gets a self-explanatory refusal, not a silent state jump."""
    issued = await _new_review(session)
    with pytest.raises(ConvergenceRefusedError, match="critic_reviewing"):
        await append_message(session, review_id=issued.review.id, role="critic", kind="status",
                             payload={"value": "converged", "artifact_seq": 1})


async def test_converged_refused_on_stale_artifact_seq(session):
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    v2 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    assert v2.seq > v1.seq
    await advance_state(session, rid, "critic_reviewing")
    # A clean pass exists for v1 but v2 is now latest -> stale.
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    with pytest.raises(ConvergenceRefusedError, match="stale version"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "converged", "artifact_seq": v1.seq})


async def test_converged_refused_without_materialized_clean_pass(session):
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    # A non-empty findings for v1 — not a clean pass.
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f1", "severity": "minor"}]})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": v1.seq, "outcome": "waived", "reason": "ok"})
    with pytest.raises(ConvergenceRefusedError, match="clean verifying pass"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "converged", "artifact_seq": v1.seq})


async def test_converged_with_undisposed_routes_to_dev_then_self_completes(session):
    """K.2/K.4.2: the critic declares converged on its own clean pass without policing
    the ledger. With a finding still undisposed the server does NOT refuse — it records a
    `convergence_blocked` notice and ROUTES the review to dev_disposing. When dev disposes,
    the server SELF-COMPLETES convergence (K.4.1) — no re-posted converged, no re-sent
    artifact."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    # f1 raised, never disposed; a clean pass exists.
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f1", "severity": "blocking"}]})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # converged is NOT refused — it is routed to dev_disposing with a blocker notice.
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    review = await session.get(Review, rid)
    assert review.state == "dev_disposing"
    msgs = await get_messages(session, rid)
    blocked = [m for m in msgs if m.role == "system" and m.kind == "notice"
               and (m.payload or {}).get("phase") == "convergence_blocked"]
    assert len(blocked) == 1
    assert blocked[0].payload["undisposed_findings"] == ["f1"]
    # dev disposes f1 -> the server self-completes convergence.
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": v1.seq, "outcome": "fixed", "reason": "done"})
    review = await session.get(Review, rid)
    assert review.state == "converged"
    msgs = await get_messages(session, rid)
    auto = [m for m in msgs if m.role == "system" and m.kind == "status"
            and (m.payload or {}).get("value") == "converged"]
    assert len(auto) == 1


async def test_converged_with_open_escalation_routes_then_self_completes(session):
    """A `waived` finding satisfies 'all disposed', but an open `contested_disposition`
    is an operator-owed blocker: converged is not refused — it is routed (the review stays
    parked/pending_human), and the server self-completes when the operator resolves it
    (2026-07-08 hardening preserved, delivered via K.4 routing not a 409)."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f1", "severity": "blocking"}]})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": v1.seq, "outcome": "waived", "reason": "later"})
    # critic contests the waiver — open escalation
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "contested_disposition", "finding_id": "f1", "detail": "waiver does not resolve it"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # every finding is disposed + a clean pass exists, yet the open contest blocks it:
    # routed (parked), not converged.
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    review = await session.get(Review, rid)
    assert review.state != "converged"
    assert review.parked is True
    # operator resolves the contested disposition -> escalation closed -> self-completes.
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "f1", "held": True, "rationale": "waiver stands"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_oscillation_autoescalates_and_blocks_convergence(session):
    """A findings item reopening a DISPOSED finding triggers a server-side system
    contested_fork escalation (operator ruling 2026-07-08): parked + convergence
    blocked until the operator settles it."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": 1, "items": [{"id": "f1", "severity": "minor"}]})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": 1, "outcome": "waived", "reason": "ok"})
    v2 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    # critic reopens f1 -> the SERVER must emit the contested_fork, not the critic
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v2.seq,
                                  "items": [{"id": "f2", "severity": "minor", "reopens_finding_id": "f1"}]})
    msgs = await get_messages(session, rid)
    auto = [m for m in msgs if m.role == "system" and m.kind == "escalation"]
    assert len(auto) == 1
    assert auto[0].payload["kind"] == "contested_fork"
    assert auto[0].payload["finding_id"] == "f1"
    review = await session.get(Review, rid)
    assert review.parked is True
    # blocked until the operator settles; f2 also needs a disposition (it is a finding)
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f2", "artifact_seq": v2.seq, "outcome": "fixed", "reason": "done"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v2.seq, "items": []})
    # the open contested_fork routes converged (parked), it is not converged yet
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v2.seq})
    assert (await session.get(Review, rid)).state != "converged"
    # operator settles the fork -> server self-completes convergence (K.4.1)
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "f1", "held": True, "rationale": "fork settled"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_unpark_only_when_all_escalations_resolved(session):
    """`parked` reflects the REMAINING open set: one answer with a second contest still
    open keeps the review parked (map flag M1, fixed 2026-07-08)."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "contested_disposition", "finding_id": "f1", "detail": "a"})
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "operator_decision_challenge", "element_id": "d1", "detail": "b"})
    assert (await session.get(Review, rid)).parked is True
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "f1", "held": True})
    assert (await session.get(Review, rid)).parked is True  # d1 still open
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"element_id": "d1", "held": True})
    assert (await session.get(Review, rid)).parked is False


async def test_response_before_escalation_does_not_preclose_it(session):
    """Cycle-04 F1 regression: a decision_response must close only an ALREADY-OPEN
    item — an earlier same-key response cannot pre-answer a later escalation."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f1", "severity": "minor"}]})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": v1.seq, "outcome": "waived", "reason": "ok"})
    # response FIRST (stray/early), escalation with the same key AFTER
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "f1", "held": True})
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "contested_disposition", "finding_id": "f1", "detail": "waiver inadequate"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # the pre-escalation response must NOT preclose the later contest — converged routes
    # (parked), it does not self-complete.
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    # a LATER response does close it -> server self-completes convergence
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "f1", "held": True})
    assert (await session.get(Review, rid)).state == "converged"


async def test_unanswered_question_blocks_convergence(session):
    """Cycle-04 F2 (B1): an unanswered human_question is operator-owed — converged is
    refused until the relayed answer references its id."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="human_question",
                         payload={"id": "q1", "question": "which variant?", "context": "..."})
    assert (await session.get(Review, rid)).parked is True
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # an unanswered question routes converged (parked), it does not self-complete
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    # the relayed answer references q1 -> question closed -> server self-completes
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"question_id": "q1", "held": True, "rationale": "variant A"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_unrelated_answer_keeps_question_parked(session):
    """Cycle-04 F2 companion bug: a decision_response for an unrelated escalation must
    NOT clear the park held by an open question."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="human_question",
                         payload={"id": "q1", "question": "?"})
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "operator_decision_challenge", "element_id": "d1", "detail": "x"})
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"element_id": "d1", "held": True})
    assert (await session.get(Review, rid)).parked is True  # q1 still open
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"question_id": "q1", "held": True})
    assert (await session.get(Review, rid)).parked is False


async def test_keyless_question_closed_by_any_later_answer(session):
    """A legacy keyless question blocks until ANY later operator answer (softer
    fail-safe than a keyless escalation, which blocks forever)."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="human_question",
                         payload={"question": "no id here"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # a legacy keyless question routes converged (parked) until ANY later answer
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"held": True, "rationale": "answered in chat"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_needs_human_blocks_convergence(session):
    """Cycle-04 F3 regression: a `needs_human` status is operator-owed — converged is
    refused until a later operator answer, even with no escalation/question open."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "needs_human", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).parked is True
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # needs_human is operator-owed -> converged routes (parked), does not self-complete
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"held": True, "rationale": "unblocked in chat"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_abandon_closes_and_revokes(session):
    """abandon is reachable from any non-terminal state and revokes tokens (ruling 2026-07-08)."""
    issued = await _new_review(session)
    rid = issued.review.id
    review = await advance_state(session, rid, "abandoned")  # straight from `created`
    assert review.state == "abandoned"
    assert review.closed_at is not None
    with pytest.raises(RevokedReviewTokenError):
        await resolve_review_token(session, issued.dev_token)
    # terminal: nothing advances out of abandoned
    with pytest.raises(InvalidStateTransitionError):
        await advance_state(session, rid, "artifact_ready")


# --- disposition wire contract, validated at POST time (feedback 4a83064b) ----


async def test_malformed_disposition_refused_at_post(session):
    """The live hpo-spec incident shape: key `disposition` instead of `outcome` — must be
    refused immediately at the author, not surface iterations later at convergence."""
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="not `disposition`"):
        await append_message(session, review_id=issued.review.id, role="development",
                             kind="disposition",
                             payload={"finding_id": "f1", "disposition": "fixed"})


async def test_disposition_without_finding_id_refused(session):
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="finding_id"):
        await append_message(session, review_id=issued.review.id, role="development",
                             kind="disposition", payload={"outcome": "fixed"})


async def test_disposition_invalid_outcome_refused(session):
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="outcome"):
        await append_message(session, review_id=issued.review.id, role="development",
                             kind="disposition",
                             payload={"finding_id": "f1", "outcome": "resolved"})


# --- external_defect escalation (spec J.1–J.3, J.8) ----------------------


async def test_external_defect_blocks_convergence_until_grafted_and_answered(session):
    """An external_defect escalation blocks convergence on TWO axes (J.8.2): it is an
    open operator-owed escalation AND it needs a development graph-result record. Both must
    clear; the server then self-completes (K.4.1)."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    # critic raises a base defect (external_defect) + a clean pass on the artifact
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "artifact_seq": v1.seq,
                                  "base_location": "x.py:3", "claim": "bug", "evidence": "obs",
                                  "depends_change": True})
    assert (await session.get(Review, rid)).parked is True
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # converged is routed (blocked): open escalation + missing graph-result
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    blocked = [m for m in await get_messages(session, rid)
               if m.kind == "notice" and (m.payload or {}).get("phase") == "convergence_blocked"]
    assert blocked and blocked[0].payload["ungrafted_external_defects"] == ["e1"]
    # dev posts the graph-result — still blocked on the operator's answer
    await append_message(session, review_id=rid, role="development",
                         kind="external_defect_graph_result",
                         payload={"escalation_ref": "e1", "dedup_outcome": "new", "node_id": "n1",
                                  "write_status": "written", "actor": "development"})
    assert (await session.get(Review, rid)).state != "converged"
    # operator DEFERs -> escalation resolved, everything clears -> server self-completes
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"escalation_id": "e1", "action": "defer"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_external_defect_without_id_refused_at_post(session):
    """F5 regression: an external_defect escalation MUST carry a stable `id` (J.8.1). A
    keyless one is refused at POST time — otherwise it would be excluded from the graph-
    result convergence gate (ungrafted_external_defects counts BY id) and could converge
    with no external_defect_graph_result at all."""
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="external_defect"):
        await append_message(session, review_id=issued.review.id, role="critic",
                             kind="escalation",
                             payload={"kind": "external_defect", "base_location": "x.py:3",
                                      "claim": "bug", "evidence": "obs"})


async def test_external_defect_nonstring_identity_fields_refused(session):
    """F18 regression: identity fields used as set/key members must be non-empty STRINGS, not
    merely truthy — a non-scalar id/ref would crash the convergence gate (500) instead of
    failing validation (422). Covers external_defect.id, graph_result.escalation_ref,
    decision_response.escalation_id."""
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="non-empty string"):
        await append_message(session, review_id=rid, role="critic", kind="escalation",
                             payload={"kind": "external_defect", "id": ["not", "scalar"],
                                      "base_location": "x.py:3", "claim": "b", "evidence": "o"})
    with pytest.raises(InvalidMessagePayloadError, match="escalation_ref"):
        await append_message(session, review_id=rid, role="development",
                             kind="external_defect_graph_result",
                             payload={"escalation_ref": {"x": 1}, "dedup_outcome": "new",
                                      "node_id": "n1", "write_status": "written", "actor": "dev"})
    with pytest.raises(InvalidMessagePayloadError, match="escalation_id"):
        await append_message(session, review_id=rid, role="operator", kind="decision_response",
                             payload={"escalation_id": ["e1"], "action": "defer"})


async def test_generic_operator_identity_types_refused(session):
    """F19 class closure: generic operator identities used as open-item keys must be non-empty
    strings WHEN PRESENT — a list/dict/empty-string key would otherwise crash
    _open_operator_items during convergence."""
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="element_id"):
        await append_message(session, review_id=rid, role="critic", kind="escalation",
                             payload={"kind": "operator_decision_challenge", "element_id": ["d1"]})
    with pytest.raises(InvalidMessagePayloadError, match="human_question"):
        await append_message(session, review_id=rid, role="critic", kind="human_question",
                             payload={"id": {"x": 1}, "question": "?"})
    with pytest.raises(InvalidMessagePayloadError, match="finding_id"):
        await append_message(session, review_id=rid, role="operator", kind="decision_response",
                             payload={"finding_id": ["f1"], "held": True})
    # escalation_id = [] (falsey non-string) must NOT bypass the non-empty-string check
    with pytest.raises(InvalidMessagePayloadError, match="escalation_id"):
        await append_message(session, review_id=rid, role="operator", kind="decision_response",
                             payload={"escalation_id": [], "action": "defer"})


async def test_element_and_finding_ids_do_not_close_each_other(session):
    """F20: the server namespaces generic operator keys by field type (like the watcher), so a
    same-string `finding_id` answer does not close an `element_id` challenge."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "operator_decision_challenge", "element_id": "x",
                                  "detail": "?"})
    assert (await session.get(Review, rid)).parked is True
    # a finding answer with the SAME string must NOT close the element challenge
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "x", "held": True})
    assert (await session.get(Review, rid)).parked is True  # still open (wrong namespace)
    # the matching element answer closes it
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"element_id": "x", "held": True})
    assert (await session.get(Review, rid)).parked is False


async def test_findings_items_container_validated(session):
    """F21: findings.items must be a list of objects — a non-list or a non-object entry is
    refused with a controlled 422, not an AttributeError/500."""
    issued = await _new_review(session)
    rid = issued.review.id
    for bad in ("abc", {"id": "f1"}, [42]):
        with pytest.raises(InvalidMessagePayloadError):
            await append_message(session, review_id=rid, role="critic", kind="findings",
                                 payload={"artifact_seq": 1, "items": bad})


async def test_reused_disposed_finding_id_refused(session):
    """F22: a findings item may not reuse an already-disposed id as its OWN id (it would be
    silently counted as disposed). A re-raise uses a fresh id + `reopens_finding_id`, which is
    the accepted path (and triggers the contested_fork oscillation escalation)."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f1", "severity": "minor"}]})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": v1.seq, "outcome": "fixed", "reason": "done"})
    # reusing the disposed id f1 as a new item id is refused
    with pytest.raises(InvalidMessagePayloadError, match="already has a terminal disposition"):
        await append_message(session, review_id=rid, role="critic", kind="findings",
                             payload={"artifact_seq": v1.seq, "items": [{"id": "f1", "severity": "minor"}]})
    # the correct re-raise: a FRESH id that reopens f1 (accepted -> contested_fork)
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq,
                                  "items": [{"id": "f2", "severity": "minor", "reopens_finding_id": "f1"}]})
    msgs = await get_messages(session, rid)
    assert any(m.role == "system" and m.kind == "escalation"
               and (m.payload or {}).get("kind") == "contested_fork" for m in msgs)


async def test_findings_and_disposition_nonstring_ids_refused(session):
    """F18 class closure: finding.id and disposition.finding_id are also hashable set members
    in the convergence gate — non-scalar ids are refused before they can crash it."""
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="non-empty string `id`"):
        await append_message(session, review_id=rid, role="critic", kind="findings",
                             payload={"artifact_seq": 1,
                                      "items": [{"id": {"nope": 1}, "severity": "minor"}]})
    with pytest.raises(InvalidMessagePayloadError, match="finding_id"):
        await append_message(session, review_id=rid, role="development", kind="disposition",
                             payload={"finding_id": ["f1"], "outcome": "fixed"})


async def test_malformed_graph_result_refused_at_post(session):
    """F6 regression: a graph_result CLEARS the J.8.2 gate, so a garbage payload (just an
    escalation_ref, no dedup_outcome/write_status/actor) must be refused."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    with pytest.raises(InvalidMessagePayloadError, match="external_defect_graph_result"):
        await append_message(session, review_id=rid, role="development",
                             kind="external_defect_graph_result",
                             payload={"escalation_ref": "e1"})


async def test_graph_result_matched_without_reference_refused(session):
    """F7 regression: dedup_outcome=matched must carry the reference_id of the matched node —
    a matched result with no reference cannot clear the gate."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    with pytest.raises(InvalidMessagePayloadError, match="reference_id"):
        await append_message(session, review_id=rid, role="development",
                             kind="external_defect_graph_result",
                             payload={"escalation_ref": "e1", "dedup_outcome": "matched",
                                      "write_status": "deferred_no_access", "actor": "development"})


async def test_graph_result_new_written_without_node_refused(session):
    """F7 regression: dedup_outcome=new + write_status=written must carry the node_id it
    created."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    with pytest.raises(InvalidMessagePayloadError, match="node_id"):
        await append_message(session, review_id=rid, role="development",
                             kind="external_defect_graph_result",
                             payload={"escalation_ref": "e1", "dedup_outcome": "new",
                                      "write_status": "written", "actor": "development"})


async def test_graph_result_skipped_written_refused(session):
    """F9 regression: dedup_outcome=skipped with write_status=written is contradictory — a
    skipped write names no durable target yet claims success; it must not clear the gate."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    with pytest.raises(InvalidMessagePayloadError, match="skipped"):
        await append_message(session, review_id=rid, role="development",
                             kind="external_defect_graph_result",
                             payload={"escalation_ref": "e1", "dedup_outcome": "skipped",
                                      "write_status": "written", "actor": "development"})


async def test_graph_result_matched_with_reference_accepted(session):
    """A legal matched result (reference_id present) is accepted, even when the link write
    itself was deferred — the matched node is named."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    msg = await append_message(session, review_id=rid, role="development",
                               kind="external_defect_graph_result",
                               payload={"escalation_ref": "e1", "dedup_outcome": "matched",
                                        "reference_id": "n_existing", "write_status": "written",
                                        "actor": "development"})
    assert msg.seq  # accepted


async def test_external_defect_decision_response_requires_valid_action(session):
    """F10 regression: an external_defect answer (escalation_id) must carry a real action —
    a bare `{escalation_id}` or an invalid action is refused, so it cannot close the
    open-operator axis (or freeze the review) without an actual operator disposition."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    with pytest.raises(InvalidMessagePayloadError, match="action"):
        await append_message(session, review_id=rid, role="operator", kind="decision_response",
                             payload={"escalation_id": "e1"})
    with pytest.raises(InvalidMessagePayloadError, match="action"):
        await append_message(session, review_id=rid, role="operator", kind="decision_response",
                             payload={"escalation_id": "e1", "action": "postpone"})


async def test_decision_response_for_nonexistent_external_defect_refused(session):
    """F10 regression: freezing/closing an external_defect id that was never raised is
    refused (no phantom frozen state, no phantom gate-clear)."""
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="no external_defect"):
        await append_message(session, review_id=issued.review.id, role="operator",
                             kind="decision_response",
                             payload={"escalation_id": "ghost", "action": "freeze"})


async def test_requires_new_artifact_must_be_boolean(session):
    """F11 regression: the return signal is a typed part of the contract."""
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="requires_new_artifact"):
        await append_message(session, review_id=issued.review.id, role="operator",
                             kind="decision_response",
                             payload={"finding_id": "f1", "requires_new_artifact": "yes"})


async def test_requires_new_artifact_suppresses_self_completion(session):
    """F11 regression: when the operator's answer requires a new artifact, the server does
    NOT self-complete a prior blocked converged over it — development must post a new
    version first."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    await append_message(session, review_id=rid, role="development",
                         kind="external_defect_graph_result",
                         payload={"escalation_ref": "e1", "dedup_outcome": "new", "node_id": "n1",
                                  "write_status": "written", "actor": "development"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    # the operator resolves the defect BUT flags that a new artifact is required
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"escalation_id": "e1", "action": "defer",
                                  "requires_new_artifact": True})
    review = await session.get(Review, rid)
    # not converged (self-completion suppressed) AND routed to a dev-owned state so the
    # required new artifact is not stranded (F12)
    assert review.state == "dev_disposing"


async def test_stale_external_defect_answer_refused(session):
    """F13 regression: an external_defect answer resolves only an OPEN defect. A second
    (stale/duplicate) answer after it was already resolved is refused — it cannot re-close
    the item or re-`freeze` the review."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    # first answer resolves it
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"escalation_id": "e1", "action": "defer"})
    # a stale/duplicate answer is refused (not open anymore)
    with pytest.raises(InvalidMessagePayloadError, match="not open"):
        await append_message(session, review_id=rid, role="operator", kind="decision_response",
                             payload={"escalation_id": "e1", "action": "freeze"})


async def test_graph_result_before_escalation_refused(session):
    """F6 regression: a graph_result must reference a REAL prior external_defect — a
    dangling/pre-emptive one cannot clear the gate for a defect never raised."""
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="no external_defect"):
        await append_message(session, review_id=issued.review.id, role="development",
                             kind="external_defect_graph_result",
                             payload={"escalation_ref": "ghost", "dedup_outcome": "new",
                                      "node_id": "n1", "write_status": "written",
                                      "actor": "development"})


async def test_duplicate_external_defect_id_refused(session):
    """F6 regression: two external_defect escalations with the same id would collapse under
    one graph-result in the set-based gate — the duplicate is refused."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs"})
    with pytest.raises(InvalidMessagePayloadError, match="duplicate external_defect id"):
        await append_message(session, review_id=rid, role="critic", kind="escalation",
                             payload={"kind": "external_defect", "id": "e1", "base_location": "y.py:9",
                                      "claim": "other", "evidence": "obs2"})


async def test_external_defect_freeze_parks_frozen_and_resumes(session):
    """FREEZE (J.3) pauses the review in `frozen` while development fixes the base defect;
    it is NOT terminal — development resumes out of it."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs", "depends_change": True})
    await append_message(session, review_id=rid, role="development",
                         kind="external_defect_graph_result",
                         payload={"escalation_ref": "e1", "dedup_outcome": "new", "node_id": "n1",
                                  "write_status": "written", "actor": "development"})
    # operator FREEZEs -> review paused in `frozen`
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"escalation_id": "e1", "action": "freeze"})
    assert (await session.get(Review, rid)).state == "frozen"
    # a lifecycle jump out of frozen is NOT allowed (it would strand the review) — resume is
    # by posting a new artifact (the base fix = a new version, J.8.5)
    with pytest.raises(InvalidStateTransitionError):
        await advance_state(session, rid, "dev_disposing")
    v2 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    review = await session.get(Review, rid)
    assert review.state == "artifact_ready"  # frozen -> artifact_ready on the new artifact
    assert v2.seq  # a fresh artifact_seq for the post-fix base


async def test_freeze_resume_does_not_strand_a_blocked_converged(session):
    """F8 regression: a blocked converged declaration + FREEZE must NOT leave the review
    stuck in dev_disposing reusing the stale clean pass. FREEZE parks it `frozen`, and the
    only resume is a NEW artifact (base shift), which re-enters the loop for a fresh pass."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs", "depends_change": True})
    await append_message(session, review_id=rid, role="development",
                         kind="external_defect_graph_result",
                         payload={"escalation_ref": "e1", "dedup_outcome": "new", "node_id": "n1",
                                  "write_status": "written", "actor": "development"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    # critic declares converged -> routed (the external_defect is still open)
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    # operator FREEZEs -> frozen, NOT self-completed, NOT stranded in dev_disposing
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"escalation_id": "e1", "action": "freeze"})
    assert (await session.get(Review, rid)).state == "frozen"
    # resume forces a new artifact version; the stale clean pass is not reused
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    assert (await session.get(Review, rid)).state == "artifact_ready"


async def test_external_defect_write_failure_keeps_escalation_open(session):
    """A failed graph write (J.8.2) records the attempt but does NOT resolve the escalation:
    the graph-result clears the ungrafted axis, yet the open escalation still blocks until
    the operator answers."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "external_defect", "id": "e1", "base_location": "x.py:3",
                                  "claim": "bug", "evidence": "obs", "depends_change": True})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    await append_message(session, review_id=rid, role="development",
                         kind="external_defect_graph_result",
                         payload={"escalation_ref": "e1", "dedup_outcome": "skipped",
                                  "write_status": "deferred_no_access", "actor": "development"})
    # ungrafted cleared, but the open escalation still blocks convergence
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state != "converged"
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"escalation_id": "e1", "action": "reject"})
    assert (await session.get(Review, rid)).state == "converged"


async def test_no_self_completion_without_a_converged_declaration(session):
    """K.4.1 regression (F3): a clean findings pass followed by `needs_human`, then answered
    by the operator, must NOT self-complete convergence — the critic never DECLARED converged
    (INV: convergence is critic-declared; the server only self-completes a declaration that
    was blocked)."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    # a clean pass, but the critic then blocks on the operator — NOT a converged declaration
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "needs_human", "artifact_seq": v1.seq})
    # the operator answers -> the needs_human clears, ledger is otherwise settled...
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"held": True, "rationale": "unblocked in chat"})
    # ...but WITHOUT a converged declaration the server must not converge on its own
    assert (await session.get(Review, rid)).state != "converged"
    # once the critic actually declares it, convergence proceeds normally
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state == "converged"


async def test_later_findings_invalidate_clean_pass_for_self_completion(session):
    """F16 regression: a clean pass + a blocked converged, then a LATER non-empty findings
    pass on the SAME artifact, must NOT self-complete from the stale clean pass once the
    blocker clears — a FRESH clean pass + converged is required (order-aware freshness)."""
    issued = await _new_review(session)
    rid = issued.review.id
    v1 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await advance_state(session, rid, "critic_reviewing")
    # an open question is the blocker so the converged declaration is routed, not accepted
    await append_message(session, review_id=rid, role="critic", kind="human_question",
                         payload={"id": "q1", "question": "which?"})
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})  # clean pass
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})  # blocked by q1
    assert (await session.get(Review, rid)).state != "converged"
    # a LATER non-empty pass on the SAME artifact — the critic found a new issue
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f2", "severity": "minor"}]})
    # answer the question and dispose the new finding -> the blockers clear...
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"question_id": "q1", "held": True})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f2", "artifact_seq": v1.seq, "outcome": "fixed", "reason": "done"})
    # ...but the clean pass is STALE (the f2 pass came after it) -> no self-completion
    assert (await session.get(Review, rid)).state != "converged"
    # only a FRESH clean pass + declaration converges
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": []})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v1.seq})
    assert (await session.get(Review, rid)).state == "converged"


# --- transport hardening (review/transport-hardening change) ---------------


async def test_oversize_payload_refused_at_post(session, monkeypatch):
    """One oversized message must not enter the append-only channel (review 809987bd)."""
    from assistant_memory.config import settings as app_settings

    monkeypatch.setattr(app_settings, "review_max_message_bytes", 200)
    issued = await _new_review(session)
    with pytest.raises(InvalidMessagePayloadError, match="channel limit"):
        await append_message(
            session, review_id=issued.review.id, role="development", kind="artifact",
            payload={"artifact_ref": {}, "mode": "spec", "blob": "x" * 500},
        )
    # under the limit, the same kind passes
    await append_message(
        session, review_id=issued.review.id, role="development", kind="artifact",
        payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}},
    )


async def _converge_once(session, rid):
    """artifact -> pickup -> clean pass -> converged; returns the artifact message."""
    v = await append_message(session, review_id=rid, role="development", kind="artifact",
                             payload={"artifact_ref": {}, "mode": "code", "diff": "d"})
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v.seq, "items": []})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v.seq})
    return v


async def test_milestone_artifact_after_converged_reopens_the_loop(session):
    """The recurring converged-409 gotcha (review 12feddeb seqs 26/85): a milestone-style
    review posts its NEXT artifact after a convergence; the FSM must reopen the loop so the
    critic's next honest `converged` is not refused for wrong state."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    await _converge_once(session, rid)
    assert (await session.get(Review, rid)).state == "converged"

    # next milestone artifact reopens the loop...
    v2 = await append_message(session, review_id=rid, role="development", kind="artifact",
                              payload={"artifact_ref": {}, "mode": "code", "diff": "d"})
    assert (await session.get(Review, rid)).state == "artifact_ready"
    # ...and the second convergence goes through without a 409
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v2.seq, "items": []})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "converged", "artifact_seq": v2.seq})
    assert (await session.get(Review, rid)).state == "converged"


async def test_intent_summary_artifact_does_not_reopen_converged(session):
    """The faithfulness audit runs with the review sitting in `converged` — the
    intent_summary artifact must not flip it back to artifact_ready."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    v1 = await _converge_once(session, rid)
    await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code", "intent_summary": True,
                 "converged_artifact_seq": v1.seq, "summary_markdown": "# summary"},
    )
    assert (await session.get(Review, rid)).state == "converged"


async def test_one_decision_response_settles_every_escalation_of_the_fork(session):
    """One contested fork can carry TWO escalations (server auto-raise + the critic's own,
    feedback e9cf8a67); a single operator answer keyed by the finding settles them all."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    for who in ("system", "critic"):
        await append_message(
            session, review_id=rid, role=who, kind="escalation",
            payload={"kind": "contested_fork", "finding_id": "fX",
                     "detail": f"{who} contest"},
        )
    assert (await session.get(Review, rid)).parked is True
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "fX", "decision": "keep the fix"})
    review = await session.get(Review, rid)
    assert review.parked is False


async def test_decision_response_still_cannot_preanswer_future_escalation(session):
    """Fork-wide settling stays ORDER-AWARE: an answer settles only what is open at its
    point in the timeline — a later escalation on the same finding re-parks the review."""
    issued = await _new_review(session)
    rid = issued.review.id
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}})
    await append_message(session, review_id=rid, role="operator", kind="decision_response",
                         payload={"finding_id": "fY", "decision": "pre-answer"})
    await append_message(session, review_id=rid, role="critic", kind="escalation",
                         payload={"kind": "contested_fork", "finding_id": "fY"})
    assert (await session.get(Review, rid)).parked is True


async def test_malformed_artifact_refused(session):
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="mode"):
        await append_message(session, review_id=rid, role="development", kind="artifact",
                             payload={"artifact_ref": {}})
    with pytest.raises(InvalidMessagePayloadError, match="intent_summary"):
        await append_message(
            session, review_id=rid, role="development", kind="artifact",
            payload={"mode": "spec", "intent_summary": True},  # missing seq + markdown
        )


async def test_malformed_status_and_notice_refused(session):
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="value"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "convreged", "artifact_seq": 1})
    with pytest.raises(InvalidMessagePayloadError, match="artifact_seq"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "needs_human", "artifact_seq": "one"})
    with pytest.raises(InvalidMessagePayloadError, match="phase"):
        await append_message(session, review_id=rid, role="critic", kind="notice",
                             payload={"phase": ""})


async def test_empty_waiver_and_dissent_refused(session):
    """The full message-kind matrix is validated: a waiver/dissent with no recorded
    rationale defeats the ledger; a rationale in any accepted field passes."""
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="waiver"):
        await append_message(session, review_id=rid, role="development", kind="waiver",
                             payload={})
    with pytest.raises(InvalidMessagePayloadError, match="dissent"):
        await append_message(session, review_id=rid, role="development", kind="dissent",
                             payload={"reason": ""})
    await append_message(session, review_id=rid, role="development", kind="waiver",
                         payload={"scope": "wire robustness", "waiver": "no-fuzzing"})
    await append_message(session, review_id=rid, role="development", kind="dissent",
                         payload={"objection": "risk X remains"})


async def test_status_requires_artifact_seq_anchor(session):
    """Every pass-ending status must carry its version anchor (finding
    status-anchor-not-required); `iteration` stays optional (needs_human omits it)."""
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="artifact_seq"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "needs_iteration", "iteration": 1})
    await append_message(session, review_id=rid, role="critic", kind="status",
                         payload={"value": "needs_human", "artifact_seq": 1})


async def test_intent_summary_flag_must_be_boolean(session):
    """`intent_summary` routes the FSM; a truthy string like "false" must be refused,
    and an explicit False behaves as an ordinary artifact (reopens a converged review)."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    v1 = await _converge_once(session, rid)
    with pytest.raises(InvalidMessagePayloadError, match="boolean"):
        await append_message(
            session, review_id=rid, role="development", kind="artifact",
            payload={"mode": "code", "intent_summary": "false",
                     "converged_artifact_seq": v1.seq, "summary_markdown": "x"},
        )
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"mode": "code", "intent_summary": False, "diff": "d",
                                  "artifact_ref": {}})
    assert (await session.get(Review, rid)).state == "artifact_ready"


async def test_boolean_never_passes_as_integer_anchor(session):
    """JSON true must not satisfy integer wire fields (True == 1 in Python) — finding
    version-anchor-bool-accepted."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    v1 = await _converge_once(session, rid)
    with pytest.raises(InvalidMessagePayloadError, match="artifact_seq"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "needs_human", "artifact_seq": True})
    with pytest.raises(InvalidMessagePayloadError, match="iteration"):
        await append_message(session, review_id=rid, role="critic", kind="status",
                             payload={"value": "needs_iteration", "artifact_seq": v1.seq,
                                      "iteration": True})
    with pytest.raises(InvalidMessagePayloadError, match="converged_artifact_seq"):
        await append_message(
            session, review_id=rid, role="development", kind="artifact",
            payload={"mode": "code", "intent_summary": True,
                     "converged_artifact_seq": True, "summary_markdown": "x"},
        )


async def test_scope_only_waiver_refused(session):
    """`scope` names what a waiver concerns, not why it was accepted — it must not
    satisfy the rationale requirement alone (finding waiver-scope-alone-accepted)."""
    issued = await _new_review(session)
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="scope"):
        await append_message(session, review_id=rid, role="development", kind="waiver",
                             payload={"scope": "wire validation"})
    await append_message(session, review_id=rid, role="development", kind="waiver",
                         payload={"reason": "cooperative agents, unchanged trust boundary"})


async def test_whitespace_rationales_and_identities_refused(session):
    """Whitespace-only strings satisfy nothing: rationale fields record no auditable
    reason and identities are garbage keys (finding rationale-whitespace-only-accepted
    + intent-summary-whitespace-only-accepted)."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    v1 = await _converge_once(session, rid)
    with pytest.raises(InvalidMessagePayloadError, match="waiver"):
        await append_message(session, review_id=rid, role="development", kind="waiver",
                             payload={"reason": "   "})
    with pytest.raises(InvalidMessagePayloadError, match="dissent"):
        await append_message(session, review_id=rid, role="development", kind="dissent",
                             payload={"objection": " \t "})
    with pytest.raises(InvalidMessagePayloadError, match="summary_markdown"):
        await append_message(
            session, review_id=rid, role="development", kind="artifact",
            payload={"mode": "code", "intent_summary": True,
                     "converged_artifact_seq": v1.seq, "summary_markdown": "  \n "},
        )


async def test_subjectless_artifact_refused(session):
    """A non-summary artifact must carry a review subject (finding
    subjectless-artifact-accepted): code needs a diff or usable base+commit; spec
    needs the bundle text."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    with pytest.raises(InvalidMessagePayloadError, match="review subject"):
        await append_message(session, review_id=rid, role="development", kind="artifact",
                             payload={"mode": "code"})
    with pytest.raises(InvalidMessagePayloadError, match="review subject"):
        await append_message(session, review_id=rid, role="development", kind="artifact",
                             payload={"mode": "code", "artifact_ref": {"base": "b", "commit": " "}})
    with pytest.raises(InvalidMessagePayloadError, match="review subject"):
        await append_message(session, review_id=rid, role="development", kind="artifact",
                             payload={"mode": "spec", "artifact_ref": {}})
    # usable ref alone is a subject for code; bundle text is the subject for spec
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"mode": "code",
                                  "artifact_ref": {"base": "b1", "commit": "c1"}})
    await append_message(session, review_id=rid, role="development", kind="artifact",
                         payload={"mode": "spec", "bundle": {"spec_markdown": "text"}})


async def test_intent_summary_needs_no_mode(session):
    """The intents protocol marks a summary by `intent_summary` alone — requiring `mode`
    would block the mandatory faithfulness audit (finding
    intent-summary-mode-required-without-protocol-basis); an invalid mode still refuses."""
    issued = await _new_review(session, mode="code")
    rid = issued.review.id
    v1 = await _converge_once(session, rid)
    await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"intent_summary": True, "converged_artifact_seq": v1.seq,
                 "summary_markdown": "# summary"},
    )
    assert (await session.get(Review, rid)).state == "converged"  # audit does not reopen
    with pytest.raises(InvalidMessagePayloadError, match="mode"):
        await append_message(
            session, review_id=rid, role="development", kind="artifact",
            payload={"intent_summary": True, "mode": "prose",
                     "converged_artifact_seq": v1.seq, "summary_markdown": "x"},
        )
