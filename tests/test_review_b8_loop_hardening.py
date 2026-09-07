# SPDX-License-Identifier: Apache-2.0
"""B.8 loop hardening — the server half (spec docs/design/2026-08-12_loop_hardening_spec.md).

Part A: a completed critic pass must survive a POST-time refusal — row/item-level defects
are accepted in part (the valid content recorded, the invalid returned with reasons), a
unique-prefix row id is normalised instead of refused, and 422 stays reserved for
payload-level malformation and all-content-invalid messages. Part C: the declared scope is
a FOCUS (out-of-scope rows stay in the denominator and are closed by ONE collective
operator exemption; high-stakes rows never), and unreached rows escalate as one fork per
pass. Part F (server side): the operator's `grounds_override` is validated at POST against
the review kind's declared grounds, and a high-stakes `reviewed-clean` without well-formed
typed evidence is DOWNGRADED to not-reached with `instrument_failure` — recorded, never
invented.

Every scenario here reproduces a measured incident named in the spec; none is speculative.
"""

import pytest

from assistant_memory.review import coverage
from assistant_memory.review.errors import InvalidMessagePayloadError
from assistant_memory.review.repository import (
    _coverage_blockers,
    advance_state,
    append_message,
    create_review,
    get_messages,
)

from tests.test_review_coverage_gate import (  # the fixtures are the suite's own
    _BLIND,
    _BLIND_CLEAN,
    _in_force,
    _manifest,
    _rid,
    _row,
    _review_with_artifact,
)

EVIDENCE = {"ground": "spec-artifact", "read_ref": "r1", "element_id": "A-1"}


async def _manifest_in_force(session, rid, seq, rows):
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_manifest(seq, rows),
    )
    return await _in_force(session, rid, seq)


# --- A-1: a coverage report is accepted in part --------------------------------------


async def test_one_bad_row_no_longer_voids_the_report(session):
    """DoD 1, the 2026-08-10 shape: 292 rows, one missing `observation` — the other 291
    verdicts land, the rejected row is named with its reason, and it stays UNREPORTED
    (no verdict), resurfacing through the ordinary not-reached machinery."""
    rid, seq = await _review_with_artifact(session)
    names = [f"r{i}" for i in range(1, 292)]
    manifest_rows = [_row(n) for n in names[:-1]]
    manifest_rows.append(_row(names[-1], high_stakes=True))
    manifest_rows.append(_BLIND)
    mid = await _manifest_in_force(session, rid, seq, manifest_rows)
    report_rows = [{"row_id": _rid(n), "verdict": "reviewed-clean"} for n in names[:-1]]
    # the one defective row: high-stakes reviewed-clean with NO observation
    report_rows.append({"row_id": _rid(names[-1]), "verdict": "reviewed-clean"})
    report_rows.append(_BLIND_CLEAN)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid, "rows": report_rows},
    )
    partial = msg.partial_acceptance
    assert partial and [r["row_id"] for r in partial["rejected_rows"]] == [_rid(names[-1])]
    assert "observation" in partial["rejected_rows"][0]["reason"]
    stored = {r["row_id"] for r in msg.payload["rows"]}
    assert _rid(names[-1]) not in stored and len(stored) == 291  # 290 clean + blind edge
    # unreported = a blocker, exactly as if the report had never mentioned it
    log = await get_messages(session, rid, after=0)
    assert _rid(names[-1]) in _coverage_blockers(log, seq)


async def test_a_report_whose_rows_all_fail_is_still_a_refusal(session):
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    with pytest.raises(InvalidMessagePayloadError, match="every row failed"):
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": mid,
                     "rows": [{"row_id": "ghost-a", "verdict": "reviewed-clean"},
                              {"row_id": "ghost-b", "verdict": "reviewed-clean"}]},
        )


async def test_manifest_id_mismatch_stays_a_whole_refusal(session):
    """A report answering a different denominator is not a per-row defect (A-1)."""
    rid, seq = await _review_with_artifact(session)
    await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    with pytest.raises(InvalidMessagePayloadError, match="must answer the current"):
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": "feedfacecafe0000",
                     "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"},
                              _BLIND_CLEAN]},
        )


# --- A-2: a unique-prefix row id is normalised, not refused --------------------------


async def test_unique_prefix_row_id_is_accepted_and_echoed(session):
    """DoD 2: an 11-character prefix of a real 12-hex id — a whole pass used to die for
    this keystroke (incident 00d1d69d)."""
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    full = _rid("r1")
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid,
                 "rows": [{"row_id": full[:-1], "verdict": "reviewed-clean"},
                          _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["normalised_rows"] == [
        {"reported": full[:-1], "accepted": full}
    ]
    assert {r["row_id"] for r in msg.payload["rows"]} == {full, "hunt-by-name"}


async def test_ambiguous_prefix_is_refused_per_row(session):
    """Two manifest rows sharing the prefix = real ambiguity; the row is rejected and the
    rest of the report lands."""
    rid, seq = await _review_with_artifact(session)
    # find two locators whose row ids share a first hex character (brute force, cheap)
    ids = {}
    pair = None
    for i in range(200):
        name = f"amb{i}"
        head = coverage.row_id_for("code", f"src/{name}.py")[0]
        if head in ids:
            pair = (ids[head], name)
            break
        ids[head] = name
    assert pair is not None
    a, b = pair
    mid = await _manifest_in_force(session, rid, seq, [_row(a), _row(b), _BLIND])
    prefix = _rid(a)[0]  # matches both rows
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid,
                 "rows": [{"row_id": prefix, "verdict": "reviewed-clean"},
                          {"row_id": _rid(b), "verdict": "reviewed-clean"},
                          _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == prefix
    assert "ambiguous" in partial["rejected_rows"][0]["reason"]


# --- A-3: a finding batch is accepted in part ----------------------------------------


async def test_a_disposed_id_rejects_the_item_not_the_batch(session):
    """DoD 3 / incident 9ee91783: one malformed identifier destroyed a pass whose other
    findings never landed."""
    issued = await create_review(session, slug="b8-a3", mode="spec",
                                 config={"protocol": "B.6"})
    rid = issued.review.id
    v1 = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"artifact_ref": {}, "mode": "spec", "bundle": {"spec_markdown": "s"}},
    )
    await advance_state(session, rid, "critic_reviewing")
    await append_message(session, review_id=rid, role="critic", kind="findings",
                         payload={"artifact_seq": v1.seq, "items": [{"id": "f1"}]})
    await append_message(session, review_id=rid, role="development", kind="disposition",
                         payload={"finding_id": "f1", "artifact_seq": v1.seq,
                                  "outcome": "fixed", "reason": "done"})
    msg = await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": v1.seq,
                 "items": [{"id": "f1"}, {"id": "f2"}]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_items"][0]["id"] == "f1"
    assert "terminal disposition" in partial["rejected_items"][0]["reason"]
    assert [f["id"] for f in msg.payload["items"]] == ["f2"]


# --- C-1/C-2: the collective scope exemption and the grouped fork --------------------


def _scoped_manifest(artifact_seq, rows):
    payload = _manifest(artifact_seq, rows)
    payload["inputs"] = {**payload["inputs"], "declared_scope": "ab12cd34ef567890"}
    payload["manifest_id"] = coverage.manifest_id_for(payload)
    return payload


async def test_one_operator_exemption_closes_out_of_scope_rows_collectively(session):
    """DoD 7: the rows stay in the denominator, marked; ONE operator message closes them
    all; a high-stakes row refuses the exemption (review 59b5ec42: six forks for one
    already-made per-scope decision)."""
    rid, seq = await _review_with_artifact(session)
    rows = [
        _row("in1"),
        _row("out1", out_of_declared_scope=True),
        _row("out2", out_of_declared_scope=True),
        _row("ouths", out_of_declared_scope=True, high_stakes=True),
        _BLIND,
    ]
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_scoped_manifest(seq, rows),
    )
    mid = await _in_force(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid,
                 "rows": [{"row_id": _rid("in1"), "verdict": "reviewed-clean"},
                          _BLIND_CLEAN]},
    )
    log = await get_messages(session, rid, after=0)
    before = _coverage_blockers(log, seq)
    assert {_rid("out1"), _rid("out2"), _rid("ouths")} <= set(before)
    # ONE operator exemption, keyed on the declared-scope digest
    await append_message(
        session, review_id=rid, role="operator", kind="decision_response",
        payload={"scope_exemption": {"declared_scope": "ab12cd34ef567890"}},
    )
    log = await get_messages(session, rid, after=0)
    after = set(_coverage_blockers(log, seq))
    assert _rid("out1") not in after and _rid("out2") not in after
    # the high-stakes row REFUSES the exemption: load-bearing is load-bearing
    assert _rid("ouths") in after


async def test_a_development_scope_exemption_grants_nothing(session):
    """The party the gate constrains must not grant itself the exemption."""
    rid, seq = await _review_with_artifact(session)
    rows = [_row("out1", out_of_declared_scope=True), _BLIND]
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_scoped_manifest(seq, rows),
    )
    mid = await _in_force(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid, "rows": [_BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="development", kind="decision_response",
        payload={"scope_exemption": {"declared_scope": "ab12cd34ef567890"}},
    )
    log = await get_messages(session, rid, after=0)
    assert _rid("out1") in _coverage_blockers(log, seq)


async def test_multiple_unreached_rows_raise_one_grouped_fork(session):
    """DoD 7 (C-2): one contested_fork listing the rows, not one per row."""
    rid, seq = await _review_with_artifact(session)
    rows = [_row("u1"), _row("u2"), _row("u3"), _BLIND]
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_manifest(seq, rows),
    )
    mid = await _in_force(session, rid, seq)
    for _ in range(2):  # two consecutive passes leaving the same rows unreached
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": mid,
                     "rows": [{"row_id": _rid(n), "verdict": "not-reached",
                               "reason": "outside the surfaces this pass read"}
                              for n in ("u1", "u2", "u3")] + [_BLIND_CLEAN]},
        )
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "artifact_seq": seq},
        )
    forks = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation" and (m.payload or {}).get("kind") == "contested_fork"
    ]
    assert len(forks) == 1
    row_ids = forks[0].payload.get("row_ids")
    assert set(row_ids) == {_rid("u1"), _rid("u2"), _rid("u3")}
    assert str(forks[0].payload.get("element_id", "")).startswith("coverage-rows:")


# --- F-3: the grounds_override carrier ----------------------------------------------


async def test_grounds_override_validated_against_declared_grounds(session):
    # a CODE review, deliberately: code mode declares repo + spec-artifact and NOT
    # graph, which is the refusal under test (the shared fixture went spec at B.14)
    issued = await create_review(session, slug="grounds", mode="code", config={"protocol": "B.6"})
    rid = issued.review.id
    await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code", "diff": "--- a" + chr(10) + "+++ b" + chr(10)},
    )
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "grounds_override", "ground": "repo", "reason": "песочница"},
    )
    with pytest.raises(InvalidMessagePayloadError, match="declared ground"):
        await append_message(
            session, review_id=rid, role="operator", kind="notice",
            payload={"phase": "grounds_override", "ground": "graph",
                     "reason": "code mode declares no graph ground"},
        )
    with pytest.raises(InvalidMessagePayloadError, match="OPERATOR"):
        await append_message(
            session, review_id=rid, role="development", kind="notice",
            payload={"phase": "grounds_override", "ground": "repo", "reason": "сам себе"},
        )
    with pytest.raises(InvalidMessagePayloadError, match="reason"):
        await append_message(
            session, review_id=rid, role="operator", kind="notice",
            payload={"phase": "grounds_override", "ground": "repo"},
        )


# --- F-2 (server side): evidence requiredness on high-stakes reviewed-clean ----------


async def test_high_stakes_clean_without_evidence_is_downgraded_not_invented(session):
    """An observation-bearing high-stakes reviewed-clean with no typed evidence is
    RECORDED as not-reached with `instrument_failure` — the three contract verdicts
    stand, and the downgrade is echoed."""
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(
        session, rid, seq, [_row("hs", high_stakes=True), _BLIND]
    )
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid,
                 "rows": [{"row_id": _rid("hs"), "verdict": "reviewed-clean",
                           "observation": "checked the guard"},
                          _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["downgraded_rows"][0]["row_id"] == _rid("hs")
    stored = {r["row_id"]: r for r in msg.payload["rows"]}
    assert stored[_rid("hs")]["verdict"] == "not-reached"
    assert stored[_rid("hs")]["reason"].startswith("instrument_failure")


async def test_high_stakes_clean_with_wellformed_evidence_is_recorded(session):
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(
        session, rid, seq, [_row("hs", high_stakes=True), _BLIND]
    )
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": mid,
                 "rows": [{"row_id": _rid("hs"), "verdict": "reviewed-clean",
                           "observation": "checked the guard",
                           "evidence": dict(EVIDENCE)},
                          _BLIND_CLEAN]},
    )
    assert msg.partial_acceptance is None
    stored = {r["row_id"]: r for r in msg.payload["rows"]}
    assert stored[_rid("hs")]["verdict"] == "reviewed-clean"


# --- round 2 of this code's own review: a manifest replacement says so, and why ------
# (finding b8-manifest-input-replacement-unanchored: two tables for one version with
# different high-stakes digests and no recorded cause — measured in this design's own
# review, instance authored by development itself.)


async def test_a_silent_manifest_replacement_is_refused(session):
    rid, seq = await _review_with_artifact(session)
    await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    with pytest.raises(InvalidMessagePayloadError, match="replaces_manifest_id"):
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [_row("r1", high_stakes=True), _BLIND]),
        )


async def test_a_replacement_naming_a_stale_manifest_is_refused(session):
    rid, seq = await _review_with_artifact(session)
    await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    replacement = _manifest(seq, [_row("r1", high_stakes=True), _BLIND])
    replacement["replaces_manifest_id"] = "feedfacecafe0000"
    replacement["correction"] = "row r1 is load-bearing"
    with pytest.raises(InvalidMessagePayloadError, match="replaces_manifest_id"):
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=replacement,
        )


async def test_a_replacement_without_a_correction_reason_is_refused(session):
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    replacement = _manifest(seq, [_row("r1", high_stakes=True), _BLIND])
    replacement["replaces_manifest_id"] = mid
    with pytest.raises(InvalidMessagePayloadError, match="correction"):
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=replacement,
        )


async def test_an_anchored_replacement_lands_and_takes_force(session):
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    replacement = _manifest(seq, [_row("r1", high_stakes=True), _BLIND])
    replacement["replaces_manifest_id"] = mid
    replacement["correction"] = "row r1 marked load-bearing after round-1 fixes"
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=replacement,
    )
    assert await _in_force(session, rid, seq) == replacement["manifest_id"]


async def test_an_identical_repost_needs_no_replacement_record(session):
    rid, seq = await _review_with_artifact(session)
    mid = await _manifest_in_force(session, rid, seq, [_row("r1"), _BLIND])
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_manifest(seq, [_row("r1"), _BLIND]),
    )
    assert await _in_force(session, rid, seq) == mid
