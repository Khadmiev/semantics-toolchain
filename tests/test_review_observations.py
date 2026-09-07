# SPDX-License-Identifier: Apache-2.0
"""B.9 G-3: the dev_observation ledger — an answer is a ledger entry, not a courtesy.

Development's incidental discoveries land on the channel as `dev_observation` notices;
the critic's next pass-ending status must answer every observation posted before the
pass began — adopted (naming a finding emitted in the SAME pass) or dismissed (with a
reason) — and the server refuses a status that leaves the ledger open. Same shape as
the proposals-per-finding validation: every item covered, no extras, an omission
mechanically visible — with referential integrity, not just counting.

The suite drives a pre-B.7 review (`protocol: B.6`): the ledger is gate-independent by
design, and the old protocol lets the flow post artifacts freely.
"""

import pytest

from assistant_memory.review.errors import InvalidMessagePayloadError
from assistant_memory.review.repository import append_message, create_review


async def _review(session):
    return await create_review(session, slug="obs", mode="spec", config={"protocol": "B.6"})


async def _post(session, rid, role, kind, payload):
    return await append_message(session, review_id=rid, role=role, kind=kind, payload=payload)


def _observation(oid, text="a stray inconsistency noticed while combing"):
    return {"phase": "dev_observation", "observation_id": oid, "text": text}


async def _artifact(session, rid):
    return await _post(session, rid, "development", "artifact", {
        "mode": "spec", "bundle": {"spec_markdown": "# s\n"},
    })


async def _pass_findings(session, rid, artifact_seq, *items):
    return await _post(session, rid, "critic", "findings", {
        "artifact_seq": artifact_seq,
        "items": [
            {"id": i, "finding_type": "correctness", "title": i} for i in items
        ],
    })


# --- posting an observation ------------------------------------------------


async def test_observation_requires_id_and_text(session):
    issued = await _review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "development", "notice",
                    {"phase": "dev_observation", "text": "no id"})
    assert "observation_id" in e.value.reason
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "development", "notice",
                    {"phase": "dev_observation", "observation_id": "obs-1"})
    assert "text" in e.value.reason


async def test_observation_is_developments_own_record(session):
    issued = await _review(session)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "critic", "notice", _observation("obs-1"))
    assert "critic" in e.value.reason


async def test_observation_id_is_unique_on_the_channel(session):
    issued = await _review(session)
    await _post(session, issued.review.id, "development", "notice", _observation("obs-1"))
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, issued.review.id, "development", "notice", _observation("obs-1"))
    assert "distinct stable id" in e.value.reason


# --- the pass-ending status answers the ledger -----------------------------


async def test_status_leaving_an_owed_observation_is_refused(session):
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)                          # seq 1
    await _post(session, rid, "development", "notice", _observation("obs-1"))  # seq 2
    v2 = await _artifact(session, rid)                     # seq 3
    await _pass_findings(session, rid, v2.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status",
                    {"value": "needs_iteration", "artifact_seq": v2.seq})
    assert "obs-1" in e.value.reason and "no disposition" in e.value.reason


async def test_dismissal_with_reason_closes_the_ledger(session):
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v2.seq,
        "observation_dispositions": [
            {"observation_id": "obs-1", "action": "dismissed", "reason": "not a defect"},
        ],
    })
    # B.11 A-5: A LATER PASS MAY RE-ANSWER IT, AND THE RE-ANSWER IS A NO-OP. This used to
    # refuse the whole concluding status, and it destroyed a complete pass of B.11's own
    # spec review (e3c796f7, artifact_seq 25): findings and a coverage report had already
    # landed, and the status was refused solely for re-answering a closed observation. A
    # re-answer asserts nothing false — it confirms what is already closed — so it is
    # accepted and ignored.
    v3 = await _artifact(session, rid)
    await _pass_findings(session, rid, v3.seq)
    accepted = await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v3.seq,
        "observation_dispositions": [
            {"observation_id": "obs-1", "action": "dismissed", "reason": "again"},
        ],
    })
    assert accepted.seq > 0


async def test_a_redundant_disposition_does_not_mask_an_owed_one(session):
    """A-5 relaxed ONE check and only one: an observation this pass owes and left undisposed
    is still refused, even when the same status truthfully re-answers a closed one.

    The relaxation's whole safety argument is that it cannot mask a MISSING disposition,
    which is checked separately — so the two checks are exercised in one status here.
    """
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v2.seq,
        "observation_dispositions": [
            {"observation_id": "obs-1", "action": "dismissed", "reason": "not a defect"},
        ],
    })
    await _post(session, rid, "development", "notice", _observation("obs-2"))
    v3 = await _artifact(session, rid)
    await _pass_findings(session, rid, v3.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v3.seq,
            "observation_dispositions": [
                {"observation_id": "obs-1", "action": "dismissed", "reason": "again"},
            ],
        })
    assert "obs-2" in e.value.reason and "no disposition" in e.value.reason


async def test_a_disposition_for_an_unknown_observation_is_still_refused(session):
    """The other neighbour A-5 deliberately left alone: an entry naming an observation that
    does not exist on the channel asserts something false, and stays refused."""
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v2.seq,
            "observation_dispositions": [
                {"observation_id": "ghost", "action": "dismissed", "reason": "never posted"},
            ],
        })
    assert "no such observation" in e.value.reason


async def test_dismissal_requires_a_reason(session):
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v2.seq,
            "observation_dispositions": [
                {"observation_id": "obs-1", "action": "dismissed"},
            ],
        })
    assert "reason" in e.value.reason


async def test_adoption_names_a_finding_of_the_same_pass(session):
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq, "f-adopted")
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v2.seq,
        "observation_dispositions": [
            {"observation_id": "obs-1", "action": "adopted", "finding_id": "f-adopted"},
        ],
    })


async def test_adoption_of_an_unknown_finding_is_refused(session):
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq, "f-real")
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v2.seq,
            "observation_dispositions": [
                {"observation_id": "obs-1", "action": "adopted", "finding_id": "f-ghost"},
            ],
        })
    assert "f-ghost" in e.value.reason and "same pass" in e.value.reason


async def test_observation_posted_after_the_pass_began_belongs_to_the_next(session):
    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)                     # seq 1
    await _pass_findings(session, rid, v1.seq)
    # Posted AFTER the pass's artifact: not owed by this pass...
    await _post(session, rid, "development", "notice", _observation("obs-late"))
    await _post(session, rid, "critic", "status",
                {"value": "needs_iteration", "artifact_seq": v1.seq})
    # ...and disposing it early is refused.
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v2.seq,
        "observation_dispositions": [
            {"observation_id": "obs-late", "action": "dismissed", "reason": "fine"},
        ],
    })


async def test_late_observation_may_be_answered_by_a_same_version_pass(session):
    """A pass MAY answer any posted, unanswered observation — late ones included
    (finding b9-late-observation-no-legal-cleanup-path: the old 'belongs to the NEXT
    pass' refusal plus the no-empty-repost rule left a late observation with no legal
    answering move). The OWED set stays anchored to the artifact — nothing is demanded
    blind — but a same-version repass can settle the ledger."""
    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)
    await _pass_findings(session, rid, v1.seq)
    await _post(session, rid, "development", "notice", _observation("obs-late"))
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v1.seq,
        "observation_dispositions": [
            {"observation_id": "obs-late", "action": "dismissed", "reason": "checked"},
        ],
    })
    # ...and answering it twice is a NO-OP (B.11 A-5), not a killed pass: the second
    # answer re-states a closed truth, and refusing it used to throw away a whole pass's
    # real findings and coverage report along with it.
    await _pass_findings(session, rid, v1.seq)
    accepted = await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v1.seq,
        "observation_dispositions": [
            {"observation_id": "obs-late", "action": "dismissed", "reason": "again"},
        ],
    })
    assert accepted.seq > 0


async def test_phased_statuses_do_not_carry_the_burden(session):
    """Administrative and refusal statuses carry a `phase`; the ledger binds only the
    pass-ending verdict — a watcher error report must never be refused over it."""
    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    await _post(session, rid, "critic", "status", {
        "value": "needs_human", "artifact_seq": v2.seq, "phase": "watcher_error",
    })
    assert v1.seq < v2.seq  # the observation stays owed for the NEXT pass-ending status
    with pytest.raises(InvalidMessagePayloadError):
        await _post(session, rid, "critic", "status",
                    {"value": "needs_iteration", "artifact_seq": v2.seq})


async def test_unknown_phase_does_not_escape_the_ledger(session):
    """The exemption is an ENUMERATED allowlist, never key presence (finding
    b9-observation-ledger-escape-paths): a status with an invented phase carries the
    full ledger burden like a phase-less verdict."""
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status",
                    {"value": "needs_iteration", "artifact_seq": v2.seq,
                     "phase": "totally_made_up"})
    assert "obs-1" in e.value.reason


async def test_late_observation_blocks_convergence_until_a_repass_answers(session):
    """An observation posted AFTER the latest artifact is owed by no pass anchored to
    it; the convergence gate keeps it from dying unanswered (finding
    b9-observation-ledger-escape-paths, second path) — and the cleanup route is a
    SAME-VERSION critic repass, the critic's turn, never a dev turn with no legal move
    (finding b9-late-observation-no-legal-cleanup-path)."""
    from assistant_memory.models.review import Review
    from assistant_memory.review.repository import advance_state, get_messages

    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)
    await advance_state(session, rid, "critic_reviewing")
    await _pass_findings(session, rid, v1.seq)  # clean pass — no items
    await _post(session, rid, "development", "notice", _observation("obs-late"))
    await _post(session, rid, "critic", "status",
                {"value": "converged", "artifact_seq": v1.seq})
    review = await session.get(Review, rid)
    # Observation-only blocker: the CRITIC's turn (a same-version repass), not dev's.
    assert review.state == "critic_reviewing"
    msgs = await get_messages(session, rid)
    blocked = [m for m in msgs if m.kind == "notice"
               and (m.payload or {}).get("phase") == "convergence_blocked"]
    assert len(blocked) == 1
    assert blocked[0].payload["unanswered_observations"] == ["obs-late"]
    # The repass answers the late observation ON its declaring status — the discount
    # lets the pass that cleared the ledger converge rather than re-block on itself.
    await _pass_findings(session, rid, v1.seq)  # fresh clean pass, same version
    await _post(session, rid, "critic", "status", {
        "value": "converged", "artifact_seq": v1.seq,
        "observation_dispositions": [
            {"observation_id": "obs-late", "action": "dismissed",
             "reason": "checked — not a defect"},
        ],
    })
    review = await session.get(Review, rid)
    assert review.state == "converged"


async def test_stamped_pass_identity_owes_and_adopts_per_pass(session):
    """b9-observation-ledger-lacks-pass-identity: with the watcher's
    `projection_through_seq` stamp, the OWED set is everything the pass's projection
    provably contained — a late observation becomes MANDATORY in the repass scheduled
    for it — and adoption resolves only against the findings message carrying the SAME
    stamp, never a finding from an earlier pass over the same version."""
    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)                              # seq 1
    await _post(session, rid, "critic", "findings", {
        "artifact_seq": v1.seq, "projection_through_seq": v1.seq,
        "items": [{"id": "f1", "finding_type": "correctness", "title": "f1"}],
    })                                                              # pass 1
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v1.seq,
        "projection_through_seq": v1.seq,
    })
    late = await _post(session, rid, "development", "notice", _observation("obs-late"))
    await _post(session, rid, "critic", "findings", {
        "artifact_seq": v1.seq, "projection_through_seq": late.seq,
        "items": [{"id": "f2", "finding_type": "correctness", "title": "f2"}],
    })                                                              # pass 2, same version
    # the repass saw the late observation — omitting it is refused
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v1.seq,
            "projection_through_seq": late.seq,
        })
    assert "obs-late" in e.value.reason
    # adoption may not cite a finding of the EARLIER pass over the same version
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v1.seq,
            "projection_through_seq": late.seq,
            "observation_dispositions": [
                {"observation_id": "obs-late", "action": "adopted", "finding_id": "f1"},
            ],
        })
    assert "THIS pass" in e.value.reason
    # citing the same-stamp finding settles the ledger
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v1.seq,
        "projection_through_seq": late.seq,
        "observation_dispositions": [
            {"observation_id": "obs-late", "action": "adopted", "finding_id": "f2"},
        ],
    })


async def test_stampless_pass_boundary_is_server_derived(session):
    """b9-pass-identity-fix-excludes-supported-bindings: without the watcher's stamp
    the boundary is derived from the channel itself — a pass's findings message is the
    latest critic findings after the previous pass-ending status. Every advertised
    binding gets per-pass semantics; the stamp stays a precision override."""
    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)                              # seq 1
    await _pass_findings(session, rid, v1.seq, "f1")                # seq 2 (no stamp)
    await _post(session, rid, "critic", "status",
                {"value": "needs_iteration", "artifact_seq": v1.seq})  # seq 3, pass end
    late = await _post(session, rid, "development", "notice", _observation("obs-late"))
    await _pass_findings(session, rid, v1.seq, "f2")                # repass findings
    assert late.seq  # posted before the repass findings
    # omitting the late observation is refused — it precedes this pass's findings
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status",
                    {"value": "needs_iteration", "artifact_seq": v1.seq})
    assert "obs-late" in e.value.reason
    # adoption may not cite the EARLIER pass's finding over the same version
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v1.seq,
            "observation_dispositions": [
                {"observation_id": "obs-late", "action": "adopted", "finding_id": "f1"},
            ],
        })
    assert "THIS pass" in e.value.reason
    await _post(session, rid, "critic", "status", {
        "value": "needs_iteration", "artifact_seq": v1.seq,
        "observation_dispositions": [
            {"observation_id": "obs-late", "action": "adopted", "finding_id": "f2"},
        ],
    })


async def test_context_restatement_invalidates_the_clean_pass(session):
    """b9-threat-context-not-a-freshness-input, seam 3: a clean pass counts for
    convergence only if it POSTDATES the latest threat_context record — a restated
    frame always buys one fresh critic look before convergence."""
    from assistant_memory.models.review import Review
    from assistant_memory.review.repository import advance_state

    issued = await _review(session)
    rid = issued.review.id
    v1 = await _artifact(session, rid)
    await advance_state(session, rid, "critic_reviewing")
    await _pass_findings(session, rid, v1.seq)  # clean pass
    ctx = await _post(session, rid, "operator", "notice", {
        "phase": "threat_context", "threat_model": "CORRECTED mid-review",
        "operating_scale": "s", "granted_by": "operator",
    })
    # PASS-BOUND freshness (b9-threat-context-freshness-not-pass-bound): once the
    # context identity is non-zero, unstamped or stale pass evidence cannot post at
    # all — the abandonment for every binding, server-managed.
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status",
                    {"value": "converged", "artifact_seq": v1.seq})
    assert "context_seq" in e.value.reason
    with pytest.raises(InvalidMessagePayloadError):
        await _post(session, rid, "critic", "findings",
                    {"artifact_seq": v1.seq, "items": []})
    # a fresh clean pass STAMPED with the newest identity converges
    await _post(session, rid, "critic", "findings",
                {"artifact_seq": v1.seq, "items": [], "context_seq": ctx.seq})
    await _post(session, rid, "critic", "status",
                {"value": "converged", "artifact_seq": v1.seq, "context_seq": ctx.seq})
    review = await session.get(Review, rid)
    assert review.state == "converged"
    # ...and a restatement AFTER convergence is refused with the gate-return route
    # (b9-frame-repass-not-a-real-pass-boundary): no state transition could carry the
    # promised fresh pass, so the record is refused, never silently accepted.
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "operator", "notice", {
            "phase": "threat_context", "threat_model": "too late",
            "operating_scale": "s", "granted_by": "operator",
        })
    assert "RETURN" in e.value.reason


async def test_reserved_phase_on_an_ordinary_verdict_does_not_escape(session):
    """The exemption is a (phase, value) SHAPE: a model-claimed reserved phase on
    needs_iteration — a shape the harness never produces — carries the full ledger
    burden (finding b9-observation-ledger-reserved-phase-still-bypasses)."""
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status",
                    {"value": "needs_iteration", "artifact_seq": v2.seq,
                     "phase": "watcher_error"})
    assert "obs-1" in e.value.reason


async def test_duplicate_dispositions_are_refused(session):
    issued = await _review(session)
    rid = issued.review.id
    await _artifact(session, rid)
    await _post(session, rid, "development", "notice", _observation("obs-1"))
    v2 = await _artifact(session, rid)
    await _pass_findings(session, rid, v2.seq)
    with pytest.raises(InvalidMessagePayloadError) as e:
        await _post(session, rid, "critic", "status", {
            "value": "needs_iteration", "artifact_seq": v2.seq,
            "observation_dispositions": [
                {"observation_id": "obs-1", "action": "dismissed", "reason": "a"},
                {"observation_id": "obs-1", "action": "dismissed", "reason": "b"},
            ],
        })
    assert "duplicate" in e.value.reason
