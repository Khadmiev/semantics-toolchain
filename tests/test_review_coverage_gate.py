# SPDX-License-Identifier: Apache-2.0
"""The server-side coverage gate and the two coverage message kinds (B.6 T1-4 / F-2 / G-3).

The division under test is the load-bearing one: the critic REPORTS unreached rows and
declares its clean pass honestly; the SERVER accounts. A coverage obligation phrased as a
critic-side stopping rule would rebuild exactly the deadlock that stalled past reviews —
the critic re-deriving the ledger from its lossy replay and withholding a clean pass over
it. So an unreached row must never produce a 409; it must be RECORDED and routed.

Also covered: the non-deadlock exit (an unbounded coverage gate is a deadlock with extra
steps), and the conditional payload requirements that are each a row's only evidence.
"""

import pytest

from assistant_memory.models.review import Review
from assistant_memory.review.errors import (
    ConvergenceRefusedError,
    InvalidMessagePayloadError,
)
from assistant_memory.review import coverage
from assistant_memory.review.repository import (
    _convergence_state,
    advance_state,
    append_message,
    create_review,
    get_messages,
)


def _manifest(artifact_seq, rows, manifest_id=None, mode="spec", base="aaa", commit="bbb",
              inputs=None):
    """A manifest payload with REAL identities.

    Row ids and the manifest id are content-addressed, and the server verifies both — so a
    test that invented them would only ever be testing the invention. `manifest_id` is
    ignored when given; two manifests differ because their CONTENT differs, which is also
    the only way they differ in production.
    """
    payload = {
        "artifact_seq": artifact_seq,
        "manifest_id": "",
        "tool_version": coverage.TOOL_VERSION,
        "mode": mode,
        "base": base,
        "commit": commit,
        "granularity": "symbol",
        "inputs": inputs if inputs is not None else {
            "spec_markdown": "a1b2c3d4e5f60718" if mode == "spec" else None,
            "declared_scope": None,
            # Always a digest, never null: the list is DECLARED even when empty, so that a
            # forgotten `--high-stakes-file` and a considered "none" are different payloads.
            "high_stakes": "0f1e2d3c4b5a6978" if any(
                isinstance(r, dict) and r.get("high_stakes") for r in rows
            ) else "e3b0c44298fc1c14",
        },
        "rows": rows,
    }
    payload["manifest_id"] = coverage.manifest_id_for(payload)
    return payload


def _locator(name):
    return f"src/{name}.py"


def _rid(name, kind="code"):
    """The content-addressed row id a test must reference in its report rows."""
    return coverage.row_id_for(kind, _locator(name))


def _row(name, kind="code", locator=None, **extra):
    loc = locator or _locator(name)
    return {"row_id": coverage.row_id_for(kind, loc), "kind": kind, "locator": loc, **extra}


_BLIND = {"row_id": "hunt-by-name", "kind": "blind_edge", "locator": "hunt-by-name"}
_BLIND_CLEAN = {
    "row_id": "hunt-by-name",
    "verdict": "reviewed-clean",
    "searches_performed": ["grep -r coverage_report"],
}


async def _in_force(session, rid, seq):
    """The manifest id currently in force for an artifact — read, never guessed."""
    manifests = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "coverage_manifest" and (m.payload or {}).get("artifact_seq") == seq
    ]
    return manifests[-1].payload["manifest_id"] if manifests else "0000000000000000"


async def _pass_reporting(session, rid, seq, rows):
    """One complete critic pass: its coverage report, then its pass-ending status.

    The non-deadlock exit counts PASSES, so a test meaning "two passes could not reach this
    row" has to end each of them — two reports posted back to back are one pass reporting
    twice, which is exactly the case the exit must not fire on.
    """
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": rows},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "artifact_seq": seq},
    )


#: The coverage ledger these tests exercise is orthogonal to B.7's round gate and
#: unchanged by it; the reviews here are pinned to the pre-B.7 round structure so the
#: fixtures keep posting findings and dispositions in the sequence this suite describes.
_LEGACY = {"protocol": "B.6"}


async def _review_with_artifact(session, mode="spec"):
    issued = await create_review(session, slug="cov", mode=mode, config=_LEGACY)
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-1 — x\n"},
                 "artifact_ref": {"base": "aaa", "commit": "bbb"}},
    )
    return rid, artifact.seq


# --- manifest payload ------------------------------------------------------


async def test_manifest_requires_exactly_one_blind_edge_row(session):
    """The blind edge declares what the manifest CANNOT see. Omit it and the manifest
    presents itself as complete — more dangerous than no manifest at all."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [_row("r1")]),
        )
    assert "EXACTLY ONE" in str(exc.value)


async def test_manifest_refuses_two_blind_edges(session):
    rid, seq = await _review_with_artifact(session)
    second = {"row_id": "hunt-by-name-2", "kind": "blind_edge", "locator": "x"}
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [_BLIND, second]),
        )


async def test_manifest_blind_edge_must_carry_the_fixed_id(session):
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [{"row_id": "blind", "kind": "blind_edge", "locator": "x"}]),
        )
    assert "fixed id" in str(exc.value)


async def test_manifest_refuses_duplicate_row_ids(session):
    """Row ids are set keys in the gate; a duplicate would silently collapse two rows."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [_row("r1"), _row("r1"), _BLIND]),
        )
    assert "duplicate" in str(exc.value)


# --- report payload: the three carve-outs ----------------------------------


def _code_manifest(artifact_seq, base, commit, block_ids=("b1",), high=()):
    """A well-formed B.14 code-mode manifest: two axis rows per block + the blind edge,
    the complete slicing on the payload, and the slicing fingerprinted in `inputs`."""
    blocks = [
        {"block_id": bid, "claim": f"one coherent claim of the change ({bid})",
         "refs": [{"path": "src/x.py", "side": "new", "start": 1, "end": 2}],
         "high_stakes": bid in high}
        for bid in block_ids
    ]
    rows = [
        {"row_id": f"{bid}::{axis}", "kind": "block", "locator": f"{bid}::{axis}",
         **({"high_stakes": True} if bid in high else {})}
        for bid in block_ids for axis in ("change", "seam")
    ] + [dict(_BLIND)]
    payload = {
        "artifact_seq": artifact_seq,
        "manifest_id": "",
        "tool_version": coverage.TOOL_VERSION,
        "mode": "code",
        "base": base,
        "commit": commit,
        "granularity": "symbol",
        "inputs": {
            "spec_markdown": None,
            "declared_scope": None,
            "high_stakes": "e3b0c44298fc1c14",
            # the REAL canonical fingerprint — the server verifies the carried slicing
            # hashes to it (round 9, b14-carried-slicing-not-bound-to-manifest-id)
            "blocks": coverage.slicing_digest(blocks),
        },
        "rows": rows,
        "blocks": blocks,
        # the declared list rides verbatim; e3b0c44298fc1c14 is the digest of an
        # EMPTY declaration, so the carried list is empty too
        "high_stakes_declared": [],
    }
    payload["manifest_id"] = coverage.manifest_id_for(payload)
    return payload


async def _post_manifest(session, rid, seq, rows=None, manifest_id="m1", commit="bbb"):
    """The manifest must be derived from the artifact's own ref pair, so a test posting one
    for a second artifact has to name that artifact's commit — the server checks it.

    A DIFFERENT table for a version that already has one is a replacement, and the server
    now requires the replacement record (`replaces_manifest_id` + `correction`; finding
    b8-manifest-input-replacement-unanchored). Filled in here so the supersession tests
    stay about supersession; the refusal paths are exercised by their own tests in
    test_review_b8_loop_hardening.py, which post replacements without the record on
    purpose."""
    rows = rows if rows is not None else [_row("r1"), _BLIND]
    payload = _manifest(seq, rows, manifest_id=manifest_id, commit=commit)
    prior = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "coverage_manifest" and (m.payload or {}).get("artifact_seq") == seq
    ]
    if prior and prior[-1].payload["manifest_id"] != payload["manifest_id"]:
        payload["replaces_manifest_id"] = prior[-1].payload["manifest_id"]
        payload["correction"] = "test replacement — corrected derivation"
    return await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=payload,
    )


async def test_not_reached_row_requires_its_reason(session):
    """The reason is not commentary: it is what the gate routes on and what the operator
    reads when the row survives two passes. B.8 A-1: the defect costs the ROW, not the
    pass — the reasonless row is rejected (stays unreported) and the rest lands."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "not-reached"}, _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == _rid("r1")
    assert "reason" in partial["rejected_rows"][0]["reason"]
    assert {r["row_id"] for r in msg.payload["rows"]} == {"hunt-by-name"}


async def test_blind_edge_row_requires_searches_under_every_verdict(session):
    """Obeying the no-prose storage rule would leave the one row whose entire value is the
    evidence asserting coverage with none. B.8 A-1: the searchless blind-edge row is
    rejected per row; the rest of the report lands."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"},
                          {"row_id": "hunt-by-name", "verdict": "reviewed-clean"}]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == "hunt-by-name"
    assert "searches_performed" in partial["rejected_rows"][0]["reason"]
    assert {r["row_id"] for r in msg.payload["rows"]} == {_rid("r1")}


async def test_finding_verdict_requires_the_finding_id(session):
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "finding"}, _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == _rid("r1")
    assert "finding_id" in partial["rejected_rows"][0]["reason"]


async def test_high_stakes_clean_claim_must_cite_an_observation(session):
    """A bare `reviewed-clean` on a load-bearing row is the cheapest claim there is — and
    only the MANIFEST knows which rows are load-bearing, so the check needs the log.
    B.8 A-1: the bare row is rejected (stays unreported); the report survives."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1", high_stakes=True), _BLIND])
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == _rid("r1")
    assert "observation" in partial["rejected_rows"][0]["reason"]


# --- report vs manifest ----------------------------------------------------


async def test_report_without_a_posted_manifest_is_refused(session):
    """A report free to answer no denominator could certify coverage of a table nobody
    ever saw — which is the one thing an external denominator exists to prevent."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq), "rows": [_BLIND_CLEAN]},
        )
    assert "no posted coverage_manifest" in str(exc.value)


async def test_report_must_answer_the_current_manifest(session):
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, manifest_id="m1")
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": "0000000000000000", "rows": [_BLIND_CLEAN]},
        )
    assert "current denominator" in str(exc.value)


async def test_report_cannot_invent_a_row(session):
    """B.8 A-1: the invented row is rejected per row (it stays unreported and nothing
    records its verdict); the rest of the report lands."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": "ghost", "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == "ghost"
    assert "not in manifest" in partial["rejected_rows"][0]["reason"]
    assert {r["row_id"] for r in msg.payload["rows"]} == {"hunt-by-name"}


# --- the gate --------------------------------------------------------------


async def _declare_converged(session, rid, seq, report_rows=None):
    """A clean pass, shaped as a real one is: findings, then THIS pass's coverage report,
    then the status. A pass may not lean on a report written before its own findings
    existed, so the report belongs inside the pass. ``report_rows`` states what this pass
    verdicted; by default the previous pass's verdicts are re-posted unchanged."""
    await advance_state(session, rid, "critic_reviewing")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    if report_rows is not None:
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq,
                     "manifest_id": await _in_force(session, rid, seq),
                     "rows": report_rows},
        )
    else:
        prior = [
            m for m in await get_messages(session, rid, after=0)
            if m.kind == "coverage_report" and (m.payload or {}).get("artifact_seq") == seq
        ]
        if prior:
            await append_message(
                session, review_id=rid, role="critic", kind="coverage_report",
                payload=dict(prior[-1].payload),
            )
    return await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "converged", "iteration": 1, "artifact_seq": seq},
    )


async def test_unreached_rows_block_convergence_but_never_409(session):
    """The critic declared honestly. What is owed is another sweep — so the declaration is
    RECORDED and routed, and the blocker notice carries the worklist."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await _declare_converged(  # no exception
        session, rid, seq,
        report_rows=[{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "out of budget"},
                     _BLIND_CLEAN],
    )

    review = await session.get(Review, rid)
    assert review.state != "converged"
    # coverage is the CRITIC's turn — not development's, not the operator's
    assert review.state == "critic_reviewing"
    assert review.parked is False
    notices = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "notice" and (m.payload or {}).get("phase") == "convergence_blocked"
    ]
    assert notices and notices[-1].payload["unreached_rows"] == [_rid("r1")]


async def test_a_row_with_no_verdict_at_all_is_a_blocker(session):
    """The absence of a verdict is exactly the 'never looked' case the manifest exists to
    make visible — silence on a row is not a pass."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq), "rows": [_BLIND_CLEAN]},
    )
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state != "converged"


async def test_a_filled_report_self_completes_the_blocked_convergence(session):
    """The critic already declared honestly; making it re-declare would be the hand-worked
    re-post the server's self-completion exists to remove."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await _declare_converged(
        session, rid, seq,
        report_rows=[{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"},
                     _BLIND_CLEAN],
    )
    assert (await session.get(Review, rid)).state != "converged"

    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    # The sweep ENDS, as every pass does — and it ends WITHOUT re-declaring convergence, which
    # is the whole point: the server completes the declaration the critic already made.
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "artifact_seq": seq},
    )
    assert (await session.get(Review, rid)).state == "converged"


async def test_operator_granted_allowance_tolerates_unreached_rows(session):
    """The allowance defaults to 0 at every tier; raising it is an operator act, recorded
    like any waiver — here as the depth-tier notice the gate posts."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "tier": "light", "granted_by": "operator",
                 "unreached_allowance": 1},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "generated file"},
                          _BLIND_CLEAN]},
    )
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state == "converged"


async def test_review_without_a_manifest_still_converges(session):
    """The gate is vacuous where there is no denominator — a review predating the coverage
    machinery, or an intent-summary artifact, is audited rather than reached."""
    rid, seq = await _review_with_artifact(session)
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state == "converged"


async def test_converged_still_409s_on_a_genuinely_malformed_declaration(session):
    """Coverage joins the RECORDED-and-routed blockers; it must not turn the critic's own
    errors (stale version, no clean pass) into routed ones."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await advance_state(session, rid, "critic_reviewing")
    with pytest.raises(ConvergenceRefusedError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "converged", "artifact_seq": seq},  # no clean pass
        )


# --- the non-deadlock exit -------------------------------------------------


async def test_row_unreached_twice_goes_to_the_operator_and_stops_blocking(session):
    """An unbounded coverage gate is a deadlock with extra steps. Two consecutive passes
    that could not reach a row mean either the row is unreviewable or the scope is wrong —
    both operator calls, and neither settled by more passes."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "generated code"}
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])

    messages = await get_messages(session, rid, after=0)
    escalations = [
        m for m in messages
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert len(escalations) == 1, "raised once, not once per later report"
    assert (await session.get(Review, rid)).parked is True

    # It has STOPPED being a loop condition: the open escalation holds the review, not the
    # coverage gate. Counting it in both places would be the deadlock again.
    await _declare_converged(session, rid, seq)
    review = await session.get(Review, rid)
    assert review.state != "converged"
    assert review.parked is True

    await append_message(
        session, review_id=rid, role="operator", kind="decision_response",
        payload={"element_id": f"coverage-row:{_rid('r1')}", "decision": "accept as unreviewable",
                 # B.11 B-2: the answer says which of two things it means. Silence used to
                 # mean this one, which is how ~128 rows once left the denominator.
                 "coverage_disposition": "accepted_unreviewable"},
    )
    assert (await session.get(Review, rid)).state == "converged"


# --- matrix cells the gate must not get wrong ------------------------------


async def test_a_coverage_sweep_that_fills_the_rows_can_still_post_its_status(session):
    """The sweep's report used to self-complete the blocked convergence on the spot, which
    took the review out of `critic_reviewing` — and the pass-ending status the critic's output
    contract ALWAYS posts next was then refused. The reference binding failed immediately
    after doing exactly the work it was scheduled to do.

    Same rule as the two-pass exit: nothing that ends a pass is decided mid-pass."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"}

    # pass 1 declares converged, and coverage is the ONLY blocker
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [unreached, _BLIND_CLEAN]},
    )
    await advance_state(session, rid, "critic_reviewing")
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "converged", "artifact_seq": seq},
    )
    assert (await session.get(Review, rid)).state != "converged", "blocked on the row"

    # the sweep the server asked for: report fills the row, THEN the pass ends
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "converged", "artifact_seq": seq},
    )
    assert (await session.get(Review, rid)).state == "converged"


async def test_a_reposted_manifest_supersedes_the_old_one(session):
    """A manifest may legitimately be re-posted for the same version — a granularity
    downgrade after a row-cap escalation, or a corrected derivation. The report is always
    judged against the denominator in force, and a report answering the OLD one is refused
    rather than silently accepted against a table nobody is using."""
    rid, seq = await _review_with_artifact(session)
    stale = await _post_manifest(session, rid, seq, rows=[_row("r1"), _BLIND])
    stale_id = stale.payload["manifest_id"]
    await _post_manifest(session, rid, seq, rows=[_row("r2"), _BLIND])
    assert await _in_force(session, rid, seq) != stale_id

    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": stale_id,
                     "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
        )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r2"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state == "converged"


async def test_a_report_answering_a_superseded_manifest_does_not_satisfy_the_gate(session):
    """Coverage claimed against a replaced denominator is not coverage of the current one —
    otherwise re-deriving the manifest would be a way to keep old verdicts alive."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _BLIND], manifest_id="m1")
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _row("r9"), _BLIND],
                         manifest_id="m2")
    # The pass cannot even END now: its status is refused until a report answers the
    # denominator in force, which is a better place to catch it than the gate.
    await advance_state(session, rid, "critic_reviewing")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "converged", "artifact_seq": seq},
        )
    assert (await session.get(Review, rid)).state != "converged"


async def test_the_intent_summary_audit_is_not_gated_on_coverage(session):
    """After convergence the summary artifact becomes the latest version — and it is
    AUDITED, not reached. A coverage gate applied to it would block the faithfulness audit
    the operator gate depends on."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state == "converged"

    summary = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"intent_summary": True, "converged_artifact_seq": seq,
                 "summary_markdown": "what this change does"},
    )
    # the summary does NOT reopen the loop, and no manifest is owed for it
    assert (await session.get(Review, rid)).state == "converged"
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": summary.seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_human", "artifact_seq": summary.seq,
                 "phase": "gate_handoff", "detail": "audited clean"},
    )
    assert (await session.get(Review, rid)).state == "converged"


async def test_report_for_a_superseded_artifact_is_refused(session):
    """A report for a version the review has moved past has no consumer: it describes a
    denominator nobody is asking about, while every reader of coverage state — the gate,
    the persistence escalation, the watcher's re-pass signal — is asking about the current
    one. Refusing at the root beats teaching each reader to distrust what it reads."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    await _post_manifest(session, rid, newer.seq, manifest_id="m2", commit="ccc")

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                     "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
        )
    assert "superseded version" in str(exc.value)


async def test_a_stale_report_cannot_drive_the_persistence_escalation(session):
    """The consequence the refusal above buys: a row cannot be escalated to the operator on
    the strength of a report about a denominator the review has left behind."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"}
    await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    await _post_manifest(session, rid, newer.seq, rows=[_row("r1"), _BLIND],
                         manifest_id="m2", commit="ccc")
    # the second not-reached arrives on the CURRENT version — two consecutive passes, and
    # the escalation is correct here (a resubmission must not reset the counter)
    await _pass_reporting(session, rid, newer.seq, [unreached, _BLIND_CLEAN])
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert len(escalations) == 1


async def test_two_unanswerable_reports_do_not_escalate_the_whole_manifest(session):
    """An `unanswerable` report claims coverage of NOTHING by its own contract — it is the
    escape hatch for a pass that cannot produce rows at all. Reading it as "looked at every
    row, reached none" turns an honest "I could not measure" into one operator question per
    row.

    MEASURED LIVE, 2026-08-09 (review 5e57eca1): two consecutive passes answered
    `unanswerable` — the first because the critic's interpreter could not import the
    coverage tool, the second because it stopped early on a blocking defect — and the server
    raised 244 contested-fork escalations, one per manifest row. The review could not
    converge until a human answered 244 questions nobody had asked; the channel was
    abandoned and the review restarted.
    """
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _row("r2"), _BLIND])
    manifest_id = await _in_force(session, rid, seq)
    for _ in range(2):
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": manifest_id,
                     "unanswerable": "the coverage tool could not be run in this binding"},
        )
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "artifact_seq": seq},
        )
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and str((m.payload or {}).get("element_id") or "").startswith("coverage-row:")
    ]
    assert escalations == [], "an unmeasured denominator is not 244 operator questions"
    # ...and convergence is still blocked, which is the correct half: nothing was measured.
    assert (await session.get(Review, rid)).parked is False


async def test_manifest_mode_must_match_the_artifact_mode(session):
    """A manifest is mode-specific by construction — element rows in spec mode, symbol reach
    in code mode. Nothing downstream catches a mismatch: the critic's no-tooling fallback
    compares base and commit, and those match."""
    rid, seq = await _review_with_artifact(session)  # a SPEC artifact
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_code_manifest(seq, base="aaa", commit="bbb"),
        )
    assert "wrong denominator" in str(exc.value)


async def test_a_row_omitted_from_two_reports_reaches_the_operator_exit(session):
    """The gate reads an omitted row as unreached; the persistence check must read it the
    same way. Otherwise a row silently dropped from every report blocks convergence forever
    and never becomes the operator's call — the deadlock the two-pass exit exists to end."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _BLIND])
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [_BLIND_CLEAN])
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert len(escalations) == 1
    assert (await session.get(Review, rid)).parked is True


async def test_a_pass_that_reaches_the_row_on_its_second_report_does_not_escalate(session):
    """Within a pass the LAST report speaks for it, so the exit is evaluated when the pass
    ENDS. A pass that reports `not-reached` and then reaches the row before its status has
    reached it; firing on the first report spends the operator's attention on a verdict the
    pass itself withdrew — and the whole point of the exit is to spend that attention only
    where more passes cannot help."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"}
    await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])

    in_force = await _in_force(session, rid, seq)
    for rows in ([unreached, _BLIND_CLEAN],
                 [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]):
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": in_force, "rows": rows},
        )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "artifact_seq": seq},
    )

    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert escalations == [], "the pass reached the row before it ended"
    assert (await session.get(Review, rid)).parked is False


async def test_findings_over_a_manifest_owed_version_are_refused_whole(session):
    """Refusing only the pass-ending status let a PARTIAL pass land — findings on the log,
    status rejected — and the reference watcher then read those orphaned findings as "this
    version is reviewed" and waited forever, while the refusal had gone to the one role that
    cannot post a denominator. The evidence itself is refused, so nothing lands."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await _pass_reporting(
        session, rid, seq,
        [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN],
    )
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    before = len(await get_messages(session, rid, after=0))

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="findings",
            payload={"artifact_seq": newer.seq, "items": []},
        )
    assert "owes a coverage_manifest" in str(exc.value)
    assert len(await get_messages(session, rid, after=0)) == before, "no partial pass landed"


async def test_an_escalation_over_a_manifest_owed_version_is_refused_too(session):
    """Guarding `findings` alone left the MORE expensive half open: an escalation routes the
    operator's attention, and it would have done so over a version whose denominator nobody
    had posted yet. The guard keys on the same evidence set the pass model calls `reviewed`,
    so the two cannot drift apart again."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await _pass_reporting(
        session, rid, seq,
        [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN],
    )
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    before = len(await get_messages(session, rid, after=0))

    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="escalation",
            payload={"kind": "operator_decision_challenge", "element_id": "e1",
                     "artifact_seq": newer.seq, "detail": "a defect in the untouched base"},
        )
    assert "owes a coverage_manifest" in str(exc.value)
    assert len(await get_messages(session, rid, after=0)) == before

    # ...but ASKING the operator something is not evidence the subject was read, and gating
    # it would block the pre-review question path.
    await append_message(
        session, review_id=rid, role="critic", kind="human_question",
        payload={"id": "q1", "artifact_seq": newer.seq, "question": "which base?"},
    )


async def test_an_intent_summary_never_owes_a_manifest(session):
    """The summary is audited against the converged version, not reached. Gating it would put
    the operator's handoff behind machinery that must not apply to it."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await _pass_reporting(
        session, rid, seq,
        [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN],
    )
    summary = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"intent_summary": True, "converged_artifact_seq": seq,
                 "summary_markdown": "what this does"},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": summary.seq, "items": []},
    )


async def test_an_escalated_row_stays_exempt_when_the_operator_accepts_it(session):
    """The operator ruled the row genuinely unreviewable — it must not come back."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _BLIND])
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "generated"}
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    await append_message(
        session, review_id=rid, role="operator", kind="decision_response",
        payload={"element_id": f"coverage-row:{_rid('r1')}", "decision": "accept as unreviewable",
                 # B.11 B-2: the answer says which of two things it means. Silence used to
                 # mean this one, which is how ~128 rows once left the denominator.
                 "coverage_disposition": "accepted_unreviewable"},
    )
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state == "converged"


async def test_the_exemption_lapses_when_the_operator_redirects_instead(session):
    """F12: a permanent exemption meant that once a row hit the two-pass exit, every later
    version silently stopped being gated on it — the economic layer suppressing exactly what
    it had been told to fix. An answer demanding a change ends the exemption."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _BLIND])
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "generated"}
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    await append_message(
        session, review_id=rid, role="operator", kind="decision_response",
        payload={"element_id": f"coverage-row:{_rid('r1')}", "decision": "narrow the scope instead",
                 "requires_new_artifact": True,
                 "coverage_disposition": "keep_gating"},
    )
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    await _post_manifest(session, rid, newer.seq, rows=[_row("r1"), _BLIND],
                         manifest_id="m2", commit="ccc")
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": newer.seq, "manifest_id": await _in_force(session, rid, newer.seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "still"},
                          _BLIND_CLEAN]},
    )
    await _declare_converged(session, rid, newer.seq)
    review = await session.get(Review, rid)
    assert review.state != "converged", "the row is gated again after a redirect"


async def test_manifest_must_be_anchored_to_a_real_artifact(session):
    """An orphan manifest could be posted ahead of any artifact and later become the
    denominator for whatever took that seq — with none of the anchor checks having run."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq + 99, [_row("r1"), _BLIND]),
        )
    assert "not an artifact on this channel" in str(exc.value)


async def test_manifest_ref_pair_must_match_the_artifact(session):
    """The check the critic performs by hand when it cannot re-run the tool — made
    mechanical, so it holds in bindings that cannot run anything at all."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [_row("r1"), _BLIND], commit="zzz"),
        )
    assert "different change than the one under review" in str(exc.value)


async def test_a_finding_verdict_cannot_name_a_ghost_finding(session):
    """A `finding` verdict is the one way a row counts as reached without a clean claim, so
    a clean findings message plus a row pointing at an id nobody raised would satisfy
    coverage with nothing in the ledger at all."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "finding", "finding_id": "ghost"},
                          _BLIND_CLEAN]},
    )
    # B.8 A-1: the ghost-pointing row is rejected per row (it stays unreported); the
    # invariant is unchanged — nothing records the row as reached.
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == _rid("r1")
    assert "not in the ledger" in partial["rejected_rows"][0]["reason"]
    assert {r["row_id"] for r in msg.payload["rows"]} == {"hunt-by-name"}


async def test_a_finding_verdict_naming_a_real_finding_is_accepted(session):
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": [{"id": "f1", "severity": "minor"}]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "finding", "finding_id": "f1"},
                          _BLIND_CLEAN]},
    )


async def test_only_an_operator_notice_can_raise_the_unreached_allowance(session):
    """Raising the allowance is an operator act. A development-authored notice moving it
    would let the party the gate constrains switch the gate off."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="development", kind="notice",
        payload={"phase": "review_depth", "tier": "light", "unreached_allowance": 5},
    )
    await _declare_converged(
        session, rid, seq,
        report_rows=[{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"},
                     _BLIND_CLEAN],
    )
    assert (await session.get(Review, rid)).state != "converged"

    # the same value, authored by the operator, does move it
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "tier": "light", "granted_by": "operator",
                 "unreached_allowance": 5},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"},
                          _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "artifact_seq": seq},
    )
    assert (await session.get(Review, rid)).state == "converged"


async def test_a_non_operator_may_lower_a_raised_allowance_but_never_widen_it(session):
    """The rule is "a non-operator notice may never WIDEN the gate", and only the running
    value can express it. Comparing against the DEFAULT made a lowering from an
    operator-raised value look like a raise: after the operator allowed 5, a later notice
    asking for 3 was dropped and the review kept running on the more permissive 5."""
    rid, seq = await _review_with_artifact(session)
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "tier": "deep", "unreached_allowance": 5},
    )
    await append_message(
        session, review_id=rid, role="development", kind="notice",
        payload={"phase": "review_depth", "tier": "deep", "unreached_allowance": 3},
    )
    assert (await _convergence_state(session, rid)).unreached_allowance == 3, "lowering holds"

    # ...and widening again from the non-operator side is still ignored
    await append_message(
        session, review_id=rid, role="development", kind="notice",
        payload={"phase": "review_depth", "tier": "deep", "unreached_allowance": 9},
    )
    assert (await _convergence_state(session, rid)).unreached_allowance == 3


async def test_spec_manifest_must_carry_its_input_fingerprints(session):
    """An optional fingerprint is decoration: the manifest could omit it and still pass
    every mechanical check a no-tooling binding can perform, which is exactly the binding
    the fingerprints exist for."""
    issued = await create_review(session, slug="spec-cov", mode="spec", config=_LEGACY)
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-1 — x\n"}},
    )
    bare = _manifest(artifact.seq, [_row("S-1", kind="element"), _BLIND], mode="spec")
    bare.pop("inputs")
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=bare,
        )
    assert "inputs" in str(exc.value)


async def test_input_fingerprints_must_carry_substance_not_just_shape(session):
    """Shape is not substance: a null spec digest in spec mode is a missing fingerprint for
    the one input that always exists, and a non-string value is a fingerprint present in
    form while carrying nothing."""
    issued = await create_review(session, slug="spec-cov2", mode="spec", config=_LEGACY)
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-1 — x\n"}},
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(
                artifact.seq, [_row("S-1", kind="element"), _BLIND], mode="spec",
                inputs={"spec_markdown": None, "declared_scope": None, "high_stakes": None},
            ),
        )
    assert "must carry its digest" in str(exc.value)


async def test_high_stakes_flags_require_their_own_fingerprint_in_code_mode(session):
    """`high_stakes` is a non-ref input in CODE mode too — it decides which rows demand a
    cited observation behind a clean claim, so the flags must be attributable."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(
                seq, [_row("r1", high_stakes=True), _BLIND],
                inputs={"spec_markdown": None, "declared_scope": None, "high_stakes": None},
            ),
        )
    assert "inputs.high_stakes" in str(exc.value)


async def test_a_manifest_for_an_intent_summary_is_refused(session):
    """The summary is audited against the converged version, not reached. An erroneous
    manifest here would make the binding demand a coverage report from the audit pass and
    put the operator gate's handoff behind machinery that must not apply to it."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await _declare_converged(session, rid, seq)
    summary = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"intent_summary": True, "converged_artifact_seq": seq,
                 "summary_markdown": "what this does"},
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(summary.seq, [_row("r1"), _BLIND]),
        )
    assert "intent summary" in str(exc.value)


async def test_a_missing_manifest_is_development_owed_not_a_critic_sweep(session):
    """Representing it as an unreached coverage row asked the CRITIC for another sweep it
    had no way to satisfy — only development posts a manifest — and let a raised allowance
    tolerate the absence of the table itself."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "tier": "light", "granted_by": "operator",
                 "unreached_allowance": 99},
    )
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    # A pass over the manifest-less version cannot even BEGIN — its first evidence message is
    # refused, covered separately. What remains here is the convergence-gate half, for the
    # case where the version was never reviewed at all: it must route to DEVELOPMENT rather
    # than ask the critic for a sweep it cannot satisfy, and must not be tolerable under a
    # raised allowance meant for rows rather than for the absence of the table.
    await advance_state(session, rid, "critic_reviewing")

    review = await session.get(Review, rid)
    assert review.state != "converged"
    check = await _convergence_state(session, rid)
    assert check.missing_manifest is True
    assert check.unreached_rows == []
    assert check.convergeable is False


async def test_no_persistence_escalation_while_the_allowance_already_tolerates_the_row(session):
    """The two-pass exit exists to break a DEADLOCK. Where the operator has already raised
    the allowance far enough that coverage is not blocking, there is none to break — and
    escalating anyway spends exactly the attention the raised allowance was granted to
    save."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "tier": "light", "granted_by": "operator",
                 "unreached_allowance": 5},
    )
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "generated"}
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert escalations == []
    assert (await session.get(Review, rid)).parked is False


async def test_once_coverage_is_in_play_a_later_artifact_must_carry_a_manifest(session):
    """The obligation lived in prose while the enforcing layer shrugged: development could
    drop the denominator mid-review and the gate would go quiet exactly when it stopped
    being fed."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    newer = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "### S-2 — y\n"},
                 "artifact_ref": {"base": "aaa", "commit": "ccc"}},
    )
    # The refusal lands on the FIRST evidence message, not at the convergence gate and not
    # at the pass-ending status: refusing the status alone left the findings on the log and
    # the pass half-recorded, which every version-based trigger then read as "reviewed".
    await advance_state(session, rid, "critic_reviewing")
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="findings",
            payload={"artifact_seq": newer.seq, "items": []},
        )
    assert "owes a coverage_manifest" in str(exc.value)

    # With the denominator in place the same pass is legal — evidence first, then its report.
    await _post_manifest(session, rid, newer.seq, manifest_id="m2", commit="ccc")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": newer.seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": newer.seq, "manifest_id": await _in_force(session, rid, newer.seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "converged", "artifact_seq": newer.seq},
    )
    assert (await session.get(Review, rid)).state == "converged"


async def test_a_review_that_never_used_coverage_is_not_retroactively_gated(session):
    """A review started before this machinery existed must finish under the process it
    started with — the open code review the rollout explicitly leaves under B.5."""
    rid, seq = await _review_with_artifact(session)
    await _declare_converged(session, rid, seq)
    assert (await session.get(Review, rid)).state == "converged"


async def test_input_digests_must_look_like_digests(session):
    """The validator's own message promises 'a digest string or null'. Accepting any
    non-empty string made that message false — a placeholder passed while the attributable
    evidence was gone."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(
                seq, [_row("r1"), _BLIND],
                inputs={"spec_markdown": None, "declared_scope": "not-a-digest",
                        "high_stakes": None},
            ),
        )
    assert "inputs.declared_scope" in str(exc.value)


async def test_a_reviewing_pass_cannot_end_without_its_coverage_report(session):
    """Enforced in the SERVER, not just in one binding: a report omitted on a findings pass
    could never be supplied afterwards, because once a newer artifact lands a report for the
    older one is refused. The ledger gap would be permanent and invisible."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": [{"id": "f1", "severity": "minor"}]},
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": seq},
        )
    assert "must post a coverage_report answering THAT manifest" in str(exc.value)


async def test_a_pass_that_posted_no_findings_owes_no_coverage_report(session):
    """A pass that could not reach the subject at all reviewed nothing and owes nothing —
    the transport failure earlier in this very review is the case. The trigger is having
    posted FINDINGS, not the status value."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_human", "artifact_seq": seq,
                 "detail": "could not read the subject"},
    )


async def test_a_needs_human_pass_that_DID_review_still_owes_its_report(session):
    """Exempting `needs_human` by its VALUE was inconsistent with the reference binding and
    wrong in substance: a pass that reviewed the artifact and then stopped on an operator
    item owes its verdicts like any other. The honest report for a blocked pass is every row
    `not-reached` with the blockage as the reason — which puts the blockage IN the ledger
    instead of leaving a silent hole where a pass used to be."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_human", "artifact_seq": seq, "detail": "operator item"},
        )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "not-reached",
                           "reason": "pass stopped on an operator item"},
                          _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_human", "artifact_seq": seq, "detail": "operator item"},
    )


async def test_the_report_obligation_is_keyed_to_the_manifest_in_force(session):
    """A manifest may legitimately be re-posted for the same artifact — a corrected
    derivation, a granularity downgrade after a row-cap escalation. An earlier report then
    answers a denominator the review has replaced, and accepting it would let the ledger
    permanently miss verdicts for the table actually being used."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, manifest_id="m1")
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await _post_manifest(session, rid, seq, rows=[_row("r9"), _BLIND], manifest_id="m2")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "iteration": 2, "artifact_seq": seq},
        )
    assert await _in_force(session, rid, seq) in str(exc.value)


async def test_a_re_pass_must_produce_its_own_coverage_report(session):
    """PER PASS, not per (artifact, manifest). A re-pass over the same version — the sweep
    the coverage gate itself routes — must produce its OWN verdicts, or the sweep ends with
    nothing new on the log while the gate reads the previous report and blocks again."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": [{"id": "f1", "severity": "minor"}]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"},
                          _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": seq},
    )
    # a SECOND pass over the same artifact and the same manifest cannot lean on the first
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "iteration": 2, "artifact_seq": seq},
        )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "iteration": 2, "artifact_seq": seq},
    )


async def test_a_report_predating_this_pass_findings_does_not_answer_it(session):
    """A report written before the findings existed cannot carry `finding` verdicts for
    them, so rows would read clean or unreached while the same pass raised findings on
    them."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": [{"id": "f1", "severity": "minor"}]},
    )
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": seq},
        )


async def test_only_an_operator_can_accept_a_coverage_row_as_unreviewable(session):
    """Otherwise a development-authored answer closing the escalation reads exactly like
    'the operator accepted this row', letting the party the gate constrains grant itself a
    permanent exemption."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq, rows=[_row("r1"), _BLIND])
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "generated"}
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    await append_message(
        session, review_id=rid, role="development", kind="decision_response",
        payload={"element_id": f"coverage-row:{_rid('r1')}", "decision": "I say it is unreviewable",
                 "coverage_disposition": "accepted_unreviewable"},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq), "rows": [unreached, _BLIND_CLEAN]},
    )
    await advance_state(session, rid, "critic_reviewing")
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "converged", "artifact_seq": seq},
    )
    assert (await session.get(Review, rid)).state != "converged"


async def test_a_numeric_fingerprint_is_not_a_digest_string(session):
    """Coercing with str() let a JSON number whose text happens to be sixteen digits
    satisfy a rule that says 'a digest STRING or null'."""
    rid, seq = await _review_with_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(
                seq, [_row("r1"), _BLIND],
                inputs={"spec_markdown": None, "declared_scope": 1234567890123456,
                        "high_stakes": None},
            ),
        )
    assert "inputs.declared_scope" in str(exc.value)


async def test_row_ids_and_manifest_id_must_be_the_real_content_addressed_ones(session):
    """Everything downstream keys on these: the report answers a manifest_id, the gate and
    the watcher decide 'is this denominator answered' by comparing it, and the
    late-surfacing diagnostic matches rows across versions by row_id. Left as opaque
    strings, a re-posted manifest could change every row while reusing the old id and each
    of those checks would go on agreeing about a table that had been replaced."""
    rid, seq = await _review_with_artifact(session)
    payload = _manifest(seq, [_row("r1"), _BLIND])
    payload["rows"][0]["row_id"] = "chosen-by-hand"
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=payload,
        )
    assert "never chosen" in str(exc.value)

    reused = _manifest(seq, [_row("r1"), _BLIND])
    reused["manifest_id"] = "0000000000000000"
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=reused,
        )
    assert "does not match this manifest's content" in str(exc.value)


async def test_a_blocked_pass_after_a_real_one_is_not_charged_the_earlier_findings(session):
    """The trigger is PASS-LOCAL. Asking whether findings ever existed made every later
    status inherit the obligation from an earlier pass, so a `needs_human` raised before
    reading anything would be refused for a report it had no verdicts to fill."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": seq},
    )
    # a LATER pass that reads nothing owes nothing
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_human", "artifact_seq": seq, "detail": "blocked"},
    )


async def test_a_finding_verdict_cannot_cite_an_earlier_passs_finding(session):
    """Validating against every id ever raised let a later clean pass mark a row `finding`
    by pointing at a finding an EARLIER pass raised and development already disposed — the
    row counts as reached while this pass found nothing there, which is the same hollow
    claim as a bare `reviewed-clean`, only harder to see."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": [{"id": "f1", "severity": "minor"}]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "finding", "finding_id": "f1"},
                          _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": seq},
    )
    # the NEXT pass finds nothing, and may not reuse the earlier pass's finding as a verdict
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "finding", "finding_id": "f1"},
                          _BLIND_CLEAN]},
    )
    # B.8 A-1: the stale-citation row is rejected per row — the invariant holds (the row
    # never counts as reached), and the pass's other verdicts land.
    partial = msg.partial_acceptance
    assert partial and partial["rejected_rows"][0]["row_id"] == _rid("r1")
    assert "not in the ledger" in partial["rejected_rows"][0]["reason"]
    assert {r["row_id"] for r in msg.payload["rows"]} == {"hunt-by-name"}


async def test_a_manifest_from_another_tool_version_is_refused(session):
    """A manifest built by an older derivation hashes correctly against its own content, so
    self-consistency proves nothing about whether it was derived the way this review is
    supposed to derive it."""
    rid, seq = await _review_with_artifact(session)
    payload = _manifest(seq, [_row("r1"), _BLIND])
    payload["tool_version"] = "0"
    payload["manifest_id"] = coverage.manifest_id_for(payload)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=payload,
        )
    assert "another derivation" in str(exc.value)


async def test_granularity_must_meet_the_depth_tier(session):
    """A manifest derived coarser than the tier allows silently shrinks the denominator to a
    fraction of the rows the review was supposed to verdict, while passing every other
    check."""
    rid, seq = await _review_with_artifact(session)
    coarse = _manifest(seq, [_row("r1"), _BLIND])
    coarse["granularity"] = "file"
    coarse["manifest_id"] = coverage.manifest_id_for(coarse)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=coarse,
        )
    assert "depth tier requires symbol-level" in str(exc.value)

    # ...and `light`, once the operator has granted it, admits the coarser derivation
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "tier": "light", "granted_by": "operator"},
    )
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest", payload=coarse,
    )


async def test_only_an_operator_may_relax_the_tier_to_file_granularity(session):
    """The third reader of an operator-owned setting in this module, and the first two
    learned the same lesson: a development-authored `tier: light` would let the party the
    coverage gate constrains grant itself a coarser denominator."""
    rid, seq = await _review_with_artifact(session)
    coarse = _manifest(seq, [_row("r1"), _BLIND])
    coarse["granularity"] = "file"
    coarse["manifest_id"] = coverage.manifest_id_for(coarse)
    await append_message(
        session, review_id=rid, role="development", kind="notice",
        payload={"phase": "review_depth", "tier": "light"},
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=coarse,
        )
    assert "depth tier requires symbol-level" in str(exc.value)


async def test_two_reports_inside_one_pass_do_not_trip_the_operator_exit(session):
    """The exit is defined on consecutive PASSES. A binding free to post more than one
    report before its pass-ending status would otherwise escalate a row to the operator on
    the strength of a single pass reporting twice."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"}
    mid = await _in_force(session, rid, seq)
    for _ in range(2):
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": mid, "rows": [unreached, _BLIND_CLEAN]},
        )
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert escalations == [], "one pass, however many reports, is one pass"


async def test_a_pass_that_only_escalated_still_owes_its_coverage_report(session):
    """The server side of the same rule the binding enforces: an escalation is proof the
    pass read something, so it cannot end as if it had reviewed nothing."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="escalation",
        payload={"kind": "external_defect", "id": "e1", "artifact_seq": seq,
                 "base_location": "x.py:3", "claim": "bug", "evidence": "obs"},
    )
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_human", "artifact_seq": seq},
        )


async def test_a_report_predating_this_passs_escalation_does_not_answer_it(session):
    """The evidence boundary includes escalations, and now it is actually ASKED. A report
    written before the pass raised its escalation cannot carry verdicts about what that
    escalation found — the property existed before and nothing called it."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "reviewed-clean"}, _BLIND_CLEAN]},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="escalation",
        payload={"kind": "external_defect", "id": "e1", "artifact_seq": seq,
                 "base_location": "x.py:3", "claim": "bug", "evidence": "obs"},
    )
    with pytest.raises(InvalidMessagePayloadError):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_human", "artifact_seq": seq},
        )


async def test_a_pre_review_pass_breaks_the_two_pass_chain(session):
    """A legal pre-review `needs_human` pass — no findings, no escalation, no report — sits
    between two report-bearing passes and BREAKS the run. Filtering such passes out made the
    two look consecutive again, which is the erasure the rule exists to prevent."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "budget"}
    await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    # a pass that could not read the subject at all: no findings, no escalation, no report
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_human", "artifact_seq": seq, "detail": "no repo access"},
    )
    await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert escalations == [], "a pass that never looked is not a pass that failed to reach"


# --- the non-stranding exit ------------------------------------------------
#
# A pass whose report the server refuses could previously neither end nor say why: ending is
# itself gated on the report. Live (review b0ef1b30) the critic mistyped one 12-character row
# id out of 278, the report was refused, the watcher's attempt to surface the error was
# refused by the same gate, and the review sat in `critic_reviewing` with no live critic and
# two delivered blocking findings hanging unterminated.


async def test_an_unanswerable_report_lets_the_pass_end(session):
    """The exit exists so a pass that cannot produce rows still terminates and still says
    why — the blockage lands IN the ledger instead of leaving a hole where a pass used to
    be."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await advance_state(session, rid, "critic_reviewing")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "unanswerable": "row ids could not be transcribed without error"},
    )
    await append_message(  # no exception: the pass ends
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "iteration": 1, "artifact_seq": seq},
    )


async def test_an_unanswerable_report_cannot_converge(session):
    """The hatch ends a pass; it never converges a review. Converging over it would declare
    the artifact clean against a denominator nobody measured."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    await advance_state(session, rid, "critic_reviewing")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": seq, "items": []},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "unanswerable": "row ids could not be transcribed without error"},
    )
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "converged", "iteration": 1, "artifact_seq": seq},
        )
    assert "UNANSWERABLE" in str(exc.value)
    assert (await session.get(Review, rid)).state != "converged"


async def test_an_unanswerable_report_requires_its_reason(session):
    """An unexplained one is a silent coverage hole — the thing the mechanism exists to
    prevent, reachable through the mechanism's own escape."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                     "unanswerable": "   "},
        )
    assert "unanswerable" in str(exc.value)


async def test_an_unanswerable_report_carries_no_rows(session):
    """Verdicts or a declaration, never both: a report claiming coverage of nothing while
    also carrying verdicts is two different claims wearing one message."""
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="critic", kind="coverage_report",
            payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                     "unanswerable": "could not transcribe", "rows": [_BLIND_CLEAN]},
        )
    assert "claims coverage of" in str(exc.value)


# --- B.10 A-1: the strict ref form travels with the contract gate to manifests --------

from tests.instrument_helpers import seed_instruments  # noqa: E402

_FULL_BASE = "a" * 40
_FULL_COMMIT = "b" * 40


async def _b10_review_with_ref_artifact(session):
    """A CURRENT-contract review with its ref-only code artifact (the post-B.10 shape)."""
    issued = await create_review(
        session, slug="b10cov", mode="code", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code",
                 "artifact_ref": {"base": _FULL_BASE, "commit": _FULL_COMMIT}},
    )
    return rid, artifact.seq


async def test_b10_manifest_abbreviated_ref_refused_on_current_contract(session):
    """finding b10-legacy-manifest-ref-validation-gate, the strict half: on a review
    stamped with the current generation the manifest's base/commit identity fields obey
    the same full-40-hex form rule as the artifact ref — ONE helper, every ref field."""
    rid, seq = await _b10_review_with_ref_artifact(session)
    with pytest.raises(InvalidMessagePayloadError) as exc:
        await append_message(
            session, review_id=rid, role="development", kind="coverage_manifest",
            payload=_manifest(seq, [_BLIND], base="90fd9c0", commit=_FULL_COMMIT),
        )
    assert "40-hex" in str(exc.value)


async def test_b10_manifest_full_hex_refs_accepted_on_current_contract(session):
    rid, seq = await _b10_review_with_ref_artifact(session)
    msg = await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_code_manifest(seq, base=_FULL_BASE, commit=_FULL_COMMIT),
    )
    assert msg.payload["base"] == _FULL_BASE


async def test_b10_legacy_manifest_form_still_accepted_on_an_unmarked_review(session):
    """The compatibility half, the spec's named acceptance case: an open pre-B.10
    review is still OWED its next manifest in the form it has always posted — the
    bd787de7 journal itself carries a legacy manifest with an abbreviated base. A
    globally strict helper would refuse it; the gate travels with the helper instead."""
    rid, seq = await _review_with_artifact(session)  # B.6-pinned -> legacy contract
    msg = await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_manifest(seq, [_BLIND], base="aaa", commit="bbb"),
    )
    assert msg.payload["base"] == "aaa"
    # and the same holds for a review with NO stamp at all (created by pre-B.10 code)
    issued = await create_review(
        session, slug="unmarked", mode="code",
        instrument=await seed_instruments(session),
    )
    review = issued.review
    review.config = {k: v for k, v in review.config.items() if k != "artifact_contract"}
    await session.flush()
    artifact = await append_message(
        session, review_id=review.id, role="development", kind="artifact",
        payload={"mode": "code", "diff": "--- a\n+++ b\n",
                 "artifact_ref": {"base": "aaa", "commit": "bbb"}},
    )
    msg = await append_message(
        session, review_id=review.id, role="development", kind="coverage_manifest",
        payload=_code_manifest(artifact.seq, base="aaa", commit="bbb"),
    )
    assert msg.payload["commit"] == "bbb"


# --- B.14 Part B: the question denominator on the server -------------------
#
# Code mode counts questions: two axis rows per development-sliced block, the typed
# verdict domain (not-reached retired), within-version accumulation with latest-binds,
# escalation ONLY on the critic's explicit typed statement (never "not yet"), and
# convergence totality with the allowance retired.

from tests.instrument_helpers import seed_instruments  # noqa: E402


async def _b14_code_review(session, block_ids=("b1", "b2"), high=()):
    issued = await create_review(
        session, slug="b14cov", mode="code", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code",
                 "artifact_ref": {"base": _FULL_BASE, "commit": _FULL_COMMIT}},
    )
    await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_code_manifest(
            artifact.seq, base=_FULL_BASE, commit=_FULL_COMMIT,
            block_ids=block_ids, high=high,
        ),
    )
    return rid, artifact.seq


_STAMP = {"model": "gpt-5.2-codex", "effort": "high"}


async def _code_pass(session, rid, seq, rows):
    await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": rows},
    )
    await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "artifact_seq": seq, **_STAMP},
    )


async def test_code_mode_verdict_domain_is_typed(session):
    """B-3/B-7: `not-reached` is retired in code mode; the typed failures require their
    reasons; an unanswered axis is simply left out of the report."""
    rid, seq = await _b14_code_review(session)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [
                     {"row_id": "b1::change", "verdict": "reviewed-clean"},
                     {"row_id": "b1::seam", "verdict": "not-reached", "reason": "later"},
                     {"row_id": "b2::change", "verdict": "cannot_reach"},
                     {"row_id": "b2::seam", "verdict": "instrument_failure",
                      "reason": "the read tool died"},
                     _BLIND_CLEAN,
                 ]},
    )
    kept = {r["row_id"]: r["verdict"] for r in msg.payload["rows"]}
    assert kept == {
        "b1::change": "reviewed-clean",
        "b2::seam": "instrument_failure",
        "hunt-by-name": "reviewed-clean",
    }
    rejected = {r["row_id"]: r["reason"] for r in (msg.partial_acceptance or {})["rejected_rows"]}
    assert "retired in code mode" in rejected["b1::seam"]
    assert "reason" in rejected["b2::change"]


async def test_typed_failures_are_spec_mode_refused(session):
    rid, seq = await _review_with_artifact(session)
    await _post_manifest(session, rid, seq)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": _rid("r1"), "verdict": "cannot_reach",
                           "reason": "why"}, _BLIND_CLEAN]},
    )
    rejected = (msg.partial_acceptance or {})["rejected_rows"]
    assert rejected and "question" in rejected[0]["reason"]


async def test_answers_accumulate_within_a_version_latest_binds(session):
    """B-3: an axis answered by any pass over the same version stands; a later row
    supersedes the earlier one; the blockers are what is still unanswered."""
    rid, seq = await _b14_code_review(session)
    await _code_pass(session, rid, seq, [
        {"row_id": "b1::change", "verdict": "reviewed-clean"},
        {"row_id": "b1::seam", "verdict": "instrument_failure", "reason": "flaky read"},
        _BLIND_CLEAN,
    ])
    # the failed axis is already held by its remedy escalation (exempt); the two
    # never-answered axes are the accumulated remainder
    state = await _convergence_state(session, rid)
    assert state.unreached_rows == ["b2::change", "b2::seam"]
    # the second pass answers the remainder and SUPERSEDES the failed axis
    await _code_pass(session, rid, seq, [
        {"row_id": "b1::seam", "verdict": "reviewed-clean"},
        {"row_id": "b2::change", "verdict": "reviewed-clean"},
        {"row_id": "b2::seam", "verdict": "reviewed-clean"},
    ])
    state = await _convergence_state(session, rid)
    assert state.unreached_rows == []
    assert not state.coverage_blocked


async def test_the_allowance_is_retired_in_code_mode(session):
    """B-7: convergence demands totality — an operator-raised allowance moves nothing."""
    rid, seq = await _b14_code_review(session)
    await append_message(
        session, review_id=rid, role="operator", kind="notice",
        payload={"phase": "review_depth", "unreached_allowance": 50},
    )
    await _code_pass(session, rid, seq, [
        {"row_id": "b1::change", "verdict": "reviewed-clean"}, _BLIND_CLEAN,
    ])
    state = await _convergence_state(session, rid)
    assert state.unreached_allowance == 0
    assert state.coverage_blocked
    # ...and no escalation was raised for the merely-unanswered axes: a block-axis with
    # no outcome escalates NEVER (B-6) — it only blocks convergence.
    escalations = [
        m for m in await get_messages(session, rid, after=0) if m.kind == "escalation"
    ]
    assert escalations == []


async def test_cannot_reach_escalates_immediately(session):
    """B-6: the row-question escalation is raised at the pass end that leaves the axis
    on the critic's explicit statement — no two-pass wait — and the ruling settles it."""
    rid, seq = await _b14_code_review(session)
    await _code_pass(session, rid, seq, [
        {"row_id": "b1::change", "verdict": "cannot_reach",
         "reason": "generated file, unreadable in this binding"},
        _BLIND_CLEAN,
    ])
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == "coverage-row:b1::change"
    ]
    assert len(escalations) == 1
    assert "cannot_reach" in escalations[0].payload["detail"]
    assert (await session.get(Review, rid)).parked is True
    # the operator's ruling settles the axis: accepted as unreviewable -> exempt
    await append_message(
        session, review_id=rid, role="operator", kind="decision_response",
        payload={"element_id": "coverage-row:b1::change",
                 "decision": "accept as unreviewable",
                 "coverage_disposition": "accepted_unreviewable"},
    )
    state = await _convergence_state(session, rid)
    assert "b1::change" not in state.unreached_rows


async def test_instrument_failure_raises_the_remedy_escalation_at_pass_end(session):
    """B-6: the B.11 third fork retargeted — accumulated instrument_failure axes ask for
    an instrument remedy at pass end; the text never invites a waiver; a superseding
    REAL outcome after remedy clears the axis."""
    rid, seq = await _b14_code_review(session, block_ids=("b1",))
    await _code_pass(session, rid, seq, [
        {"row_id": "b1::change", "verdict": "instrument_failure",
         "reason": "evidence re-read tool crashed"},
        {"row_id": "b1::seam", "verdict": "instrument_failure",
         "reason": "evidence re-read tool crashed"},
        _BLIND_CLEAN,
    ])
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("cause") == coverage.INSTRUMENT_FAILURE_REASON
    ]
    assert len(escalations) == 1
    payload = escalations[0].payload
    assert set(payload["row_ids"]) == {"b1::change", "b1::seam"}
    assert "NOT be waived" in payload["detail"]
    assert "remedy" in payload["requested_operator_action"]
    # the remedy route: operator answers keep_gating, a later pass supersedes
    await append_message(
        session, review_id=rid, role="operator", kind="decision_response",
        payload={"element_id": payload["element_id"],
                 "decision": "instrument fixed, sweep again",
                 "coverage_disposition": "keep_gating"},
    )
    await _code_pass(session, rid, seq, [
        {"row_id": "b1::change", "verdict": "reviewed-clean"},
        {"row_id": "b1::seam", "verdict": "reviewed-clean"},
    ])
    state = await _convergence_state(session, rid)
    assert state.unreached_rows == []


async def test_high_stakes_axes_keep_the_evidence_discipline(session):
    """B-3: BOTH axis rows of a high_stakes block demand the cited observation behind a
    `reviewed-clean` — the F-2 mechanism unchanged, now per block-axis."""
    rid, seq = await _b14_code_review(session, block_ids=("b1",), high=("b1",))
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": "b1::seam", "verdict": "reviewed-clean"},
                          _BLIND_CLEAN]},
    )
    rejected = (msg.partial_acceptance or {})["rejected_rows"]
    assert rejected and "high-stakes" in rejected[0]["reason"]


# --- B.14 spec constraint 3: an in-flight legacy code review keeps its contract ----
#
# The question-denominator semantics are keyed on the manifest CARRYING A SLICING,
# never on `mode == "code"` alone (finding b14-legacy-code-coverage-contract-
# overwritten): a code review created before B.14 holds a v1 place manifest, and its
# critic contract knows not-reached, per-pass reports and the two-pass exit.


def _legacy_code_manifest(artifact_seq, rows=None):
    """A pre-B.14 code manifest: place rows, no blocks, tool_version '1'. Postable
    through the validator on a LEGACY-contract review (round 10, finding
    b14-legacy-contract-not-carried-through-critic-surfaces); refused on a current
    one."""
    payload = _manifest(artifact_seq, rows if rows is not None else [_row("r1"), _BLIND],
                        mode="code")
    payload["tool_version"] = "1"
    payload["inputs"] = {"spec_markdown": None, "declared_scope": None,
                         "high_stakes": "e3b0c44298fc1c14"}
    payload["manifest_id"] = coverage.manifest_id_for(payload)
    return payload


def test_legacy_code_manifest_keeps_the_old_verdict_domain():
    from assistant_memory.review.repository import (
        _validate_coverage_report_against_manifest,
    )

    manifest = _legacy_code_manifest(1)
    report = {
        "artifact_seq": 1, "manifest_id": manifest["manifest_id"],
        "rows": [
            {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "legacy sweep"},
            {"row_id": "hunt-by-name", "verdict": "cannot_reach", "reason": "nope"},
        ],
    }
    rejected, _, _ = _validate_coverage_report_against_manifest(report, manifest)
    kept = {r["row_id"]: r["verdict"] for r in report["rows"]}
    # not-reached stays legal; the typed failure is the one refused
    assert kept == {_rid("r1"): "not-reached"}
    assert rejected and "question" in rejected[0]["reason"]


async def _legacy_inflight_code_review(session):
    """A pre-B.14 channel: code review, code artifact, v1 place manifest inserted
    directly — simulating a channel whose manifest predates the deploy (a legacy
    review also POSTs new v1 manifests now; the direct insert covers the replay
    path)."""
    from assistant_memory.models.review import ReviewMessage

    issued = await create_review(session, slug="legacy-code", mode="code", config=_LEGACY)
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code", "diff": "--- a\n+++ b\n",
                 "artifact_ref": {"base": "aaa", "commit": "bbb"}},
    )
    manifest = _legacy_code_manifest(artifact.seq)
    last = (await get_messages(session, rid, after=0))[-1].seq
    session.add(ReviewMessage(review_id=rid, seq=last + 1, role="development",
                              kind="coverage_manifest", payload=manifest))
    await session.flush()
    return rid, artifact.seq


async def test_legacy_inflight_code_review_finishes_under_its_own_contract(session):
    rid, seq = await _legacy_inflight_code_review(session)
    unreached = {"row_id": _rid("r1"), "verdict": "not-reached", "reason": "legacy"}
    # not-reached reports are ACCEPTED (the in-flight critic's contract), and the
    # allowance stays the tier's, never the question denominator's zero-totality
    for _ in range(2):
        await _pass_reporting(session, rid, seq, [unreached, _BLIND_CLEAN])
    state = await _convergence_state(session, rid)
    assert state.unreached_allowance == _DEFAULT_ALLOWANCE_PROBE
    # ...and the OLD two-pass exit still fires for it — one fork, the legacy route
    escalations = [
        m for m in await get_messages(session, rid, after=0)
        if m.kind == "escalation"
        and (m.payload or {}).get("element_id") == f"coverage-row:{_rid('r1')}"
    ]
    assert len(escalations) == 1
    assert "two" in escalations[0].payload["detail"] or "consecutive" in escalations[0].payload["detail"]


from assistant_memory.review.repository import (  # noqa: E402
    _DEFAULT_UNREACHED_ALLOWANCE as _DEFAULT_ALLOWANCE_PROBE,
)


async def test_code_manifest_must_carry_its_declared_stakes_list(session):
    """finding b14-code-high-stakes-input-not-reproducible: the carried list is
    required on a block manifest and must hash to the fingerprint it claims."""
    issued = await create_review(
        session, slug="hs-carry", mode="code", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code",
                 "artifact_ref": {"base": _FULL_BASE, "commit": _FULL_COMMIT}},
    )
    missing = _code_manifest(artifact.seq, base=_FULL_BASE, commit=_FULL_COMMIT)
    del missing["high_stakes_declared"]
    missing["manifest_id"] = coverage.manifest_id_for(missing)
    with pytest.raises(InvalidMessagePayloadError, match="high_stakes_declared"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=missing)
    mismatched = _code_manifest(artifact.seq, base=_FULL_BASE, commit=_FULL_COMMIT)
    mismatched["high_stakes_declared"] = ["src/x.py::sneaky"]
    mismatched["manifest_id"] = coverage.manifest_id_for(mismatched)
    with pytest.raises(InvalidMessagePayloadError, match="hash"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=mismatched)


# --- Round 10: the legacy coverage contract carried through the POST validator ------


async def _legacy_review_with_artifact(session, slug):
    issued = await create_review(session, slug=slug, mode="code", config=_LEGACY)
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code", "diff": "--- a\n+++ b\n",
                 "artifact_ref": {"base": "aaa", "commit": "bbb"}},
    )
    return rid, artifact.seq


async def test_legacy_review_posts_its_next_v1_manifest_through_the_validator(session):
    """Round 10, finding b14-legacy-contract-not-carried-through-critic-surfaces: the
    creation-time coverage contract licenses WRITES too — an in-flight pre-B.14 code
    review posts NEW v1 place manifests for its later artifact versions through the
    ordinary POST validator, not only via replay of stored ones."""
    rid, seq = await _legacy_review_with_artifact(session, "legacy-code-post")
    msg = await append_message(
        session, review_id=rid, role="development", kind="coverage_manifest",
        payload=_legacy_code_manifest(seq),
    )
    assert msg.payload["tool_version"] == "1"


async def test_legacy_review_refuses_a_current_contract_manifest(session):
    """The frozen contract cuts both ways: a legacy review posts neither the current
    tool_version nor a slicing — its denominator stays the place list it was created
    with."""
    rid, seq = await _legacy_review_with_artifact(session, "legacy-code-cur")
    current = _legacy_code_manifest(seq)
    current["tool_version"] = coverage.TOOL_VERSION
    current["manifest_id"] = coverage.manifest_id_for(current)
    with pytest.raises(InvalidMessagePayloadError, match="creation-time contract"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=current)
    sliced = _legacy_code_manifest(seq)
    sliced["blocks"] = [
        {"block_id": "b1", "claim": "smuggled slicing",
         "refs": [{"path": "src/x.py", "side": "new", "start": 1, "end": 2}],
         "high_stakes": False}
    ]
    sliced["manifest_id"] = coverage.manifest_id_for(sliced)
    with pytest.raises(InvalidMessagePayloadError, match="place denominator"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=sliced)


async def _current_code_review_with_artifact(session, slug):
    issued = await create_review(
        session, slug=slug, mode="code", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    artifact = await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "code",
                 "artifact_ref": {"base": _FULL_BASE, "commit": _FULL_COMMIT}},
    )
    return rid, artifact.seq


async def test_flipped_carried_block_flag_is_refused(session):
    """Round 10, finding b14-high-stakes-block-flag-not-bound-to-slicing: manifest_id
    covers the row flags and slicing_digest excludes flags by design, so a flipped
    carried-block flag survives both identities — equality with the axis rows is what
    binds it."""
    rid, seq = await _current_code_review_with_artifact(session, "b14-flag-bind")
    payload = _code_manifest(seq, base=_FULL_BASE, commit=_FULL_COMMIT)
    payload["blocks"][0]["high_stakes"] = True  # id and digest both survive the flip
    with pytest.raises(InvalidMessagePayloadError, match="one computed projection"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=payload)


async def test_non_boolean_block_flag_is_refused(session):
    rid, seq = await _current_code_review_with_artifact(session, "b14-flag-bool")
    payload = _code_manifest(seq, base=_FULL_BASE, commit=_FULL_COMMIT)
    payload["blocks"][0]["high_stakes"] = "yes"
    with pytest.raises(InvalidMessagePayloadError, match="boolean `high_stakes`"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=payload)


async def test_malformed_block_entry_refuses_instead_of_crashing(session):
    """Round 10, finding b14-malformed-block-manifest-crashes-validator: a non-object
    entry is the documented refusal, never an AttributeError out of the digest
    cross-check."""
    rid, seq = await _current_code_review_with_artifact(session, "b14-block-shape")
    payload = _code_manifest(seq, base=_FULL_BASE, commit=_FULL_COMMIT)
    payload["blocks"] = payload["blocks"] + ["not-an-object"]
    with pytest.raises(InvalidMessagePayloadError, match="must be an object"):
        await append_message(session, review_id=rid, role="development",
                             kind="coverage_manifest", payload=payload)


async def test_block_axis_row_ids_are_literal_no_prefix_recovery(session):
    """Round 11, finding b14-block-axis-prefix-normalization: prefix recovery exists
    for opaque 12-hex place ids; a literal `<block_id>::<axis>` identity that does not
    match is refused, never silently normalised (B.14 B-4)."""
    rid, seq = await _b14_code_review(session, block_ids=("b1",))
    msg = await append_message(
        session, review_id=rid, role="critic", kind="coverage_report",
        payload={"artifact_seq": seq, "manifest_id": await _in_force(session, rid, seq),
                 "rows": [{"row_id": "b1::c", "verdict": "reviewed-clean"},
                          {"row_id": "b1::seam", "verdict": "reviewed-clean"}]},
    )
    partial = msg.partial_acceptance or {}
    rejected = partial.get("rejected_rows") or []
    assert rejected and "no prefix recovery" in rejected[0]["reason"]
    assert not partial.get("normalised_rows")
    assert [r["row_id"] for r in msg.payload["rows"]] == ["b1::seam"]
