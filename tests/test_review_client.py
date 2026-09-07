# SPDX-License-Identifier: Apache-2.0
"""Review client-side primitives (spec §6, §9, §12): render, reserve log, long-poll.

All side-effect-free or file-only — no DB, no network. The long-poll core is exercised
with a scripted getter (empties, transient failures, the degradation threshold).
"""

import pytest

from assistant_memory.review.log import append_messages, reserve_log_path
from assistant_memory.review.poll import (
    ServiceUnreachable,
    TransportError,
    poll_until_message,
)
from assistant_memory.review.render import render_message, render_timeline


def _msg(seq, role, kind, payload):
    return {"seq": seq, "role": role, "kind": kind, "payload": payload, "created_at": "2026-07-08T11:00:00Z"}


# --- render (§9.1) -------------------------------------------------------


def test_render_findings_lists_each_item():
    m = _msg(3, "critic", "findings", {"artifact_seq": 1, "items": [
        {"id": "f1", "severity": "blocking", "class": "impl_error", "claim": "off-by-one", "location": "x.py:10"},
        {"id": "f2", "severity": "minor"},
    ]})
    out = render_message(m)
    assert "2 finding(s) over v1" in out
    assert "f1 [blocking/impl_error" in out
    assert "@ x.py:10" in out
    assert "f2 [minor]" in out


def test_render_finding_shows_lineage_and_reopen():
    m = _msg(3, "critic", "findings", {"artifact_seq": 2, "items": [
        {"id": "f3", "severity": "minor", "lineage_of": "f1"},
        {"id": "f4", "severity": "blocking", "reopens_finding_id": "f2"},
    ]})
    out = render_message(m)
    assert "(lineage of f1)" in out
    assert "(reopens f2)" in out


def test_render_empty_findings_reads_as_clean_pass():
    out = render_message(_msg(4, "critic", "findings", {"artifact_seq": 2, "items": []}))
    assert "clean pass — 0 findings over v2" in out


def test_render_status_and_header():
    m = _msg(5, "critic", "status", {"value": "converged", "artifact_seq": 2, "iteration": 3})
    out = render_message(m)
    assert "status=converged (v2, iter 3)" in out
    assert "(seq 5) critic/status" in out


def test_render_resolution_keys_current_schema():
    """Cycle-04 F4: renderer shows the current keys (element_id/finding_id/question_id),
    not the removed decision_ref, so the operator can match a contest to its answer."""
    esc = render_message(_msg(3, "critic", "escalation",
                              {"kind": "contested_disposition", "finding_id": "f1", "detail": "no"}))
    assert "escalation [contested_disposition] f1" in esc
    q = render_message(_msg(4, "critic", "human_question", {"id": "q1", "question": "which?"}))
    assert "human_question [q1]: which?" in q
    dr = render_message(_msg(5, "operator", "decision_response",
                             {"question_id": "q1", "held": True, "rationale": "A"}))
    assert "decision_response [q1]: held=True" in dr
    assert "?" not in dr.split("held")[0]  # the key resolved, not a bare [?]


def test_render_unknown_kind_is_not_dropped():
    out = render_message(_msg(9, "system", "mystery", {"a": 1}))
    assert "mystery" in out  # forward-compatible fallback, never a silent drop


def test_render_timeline_orders_and_headers():
    msgs = [_msg(1, "development", "artifact", {"mode": "spec"}),
            _msg(2, "critic", "notice", {"text": "hi"})]
    out = render_timeline(msgs, header="Review demo")
    assert out.startswith("# Review demo")
    assert out.index("seq 1") < out.index("seq 2")


# --- reserve log (§9.1, §12) ---------------------------------------------


def test_reserve_log_path_naming():
    p = reserve_log_path("abcd1234-5678-90ab-cdef-000000000000", "My Spec!", reviews_dir="/tmp/r")
    assert p.name == "review_My-Spec-_abcd1234.md"


def test_append_is_append_only_with_single_header(tmp_path):
    p = tmp_path / "log.md"
    append_messages(p, [_msg(1, "development", "artifact", {"mode": "spec"})])
    append_messages(p, [_msg(2, "critic", "notice", {"text": "hi"})])
    text = p.read_text(encoding="utf-8")
    assert text.count("# Review reserve log") == 1  # header written once
    assert "seq 1" in text and "seq 2" in text
    assert text.index("seq 1") < text.index("seq 2")  # earlier entry retained, not rewritten


# --- long-poll core (§6.1, §12) ------------------------------------------


def _scripted(script):
    it = iter(script)

    async def get(after, wait):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return get


async def test_poll_returns_first_message_after_empties():
    got = await poll_until_message(_scripted([[], [], [{"seq": 1}]]), after=0, per_wait=0.0)
    assert got == [{"seq": 1}]


async def test_poll_resets_failures_on_success():
    # fail, empty (resets), fail, message — with threshold 2 this must NOT trip.
    script = [TransportError("a"), [], TransportError("b"), [{"seq": 7}]]
    got = await poll_until_message(_scripted(script), after=0, per_wait=0.0, max_failures=2)
    assert got == [{"seq": 7}]


async def test_poll_raises_service_unreachable_after_threshold():
    seen = []
    with pytest.raises(ServiceUnreachable):
        await poll_until_message(
            _scripted([TransportError("x"), TransportError("y"), TransportError("z")]),
            after=0, per_wait=0.0, max_failures=3,
            on_failure=lambda n, exc: seen.append(n),
        )
    assert seen == [1, 2, 3]  # on_failure fired for each consecutive failure


# --- silence alarm (K.4.3) -------------------------------------------------


async def test_silence_alarm_fires_once_then_polling_continues():
    calls = {"n": 0}

    async def get(after, wait):
        calls["n"] += 1
        return [] if calls["n"] < 4 else [{"seq": 1}]

    fired = []
    msgs = await poll_until_message(
        get, after=0, per_wait=0.0, alarm_after=0.0, on_alarm=fired.append
    )
    assert msgs == [{"seq": 1}]
    assert len(fired) == 1  # one shot across three empty holds


async def test_no_alarm_when_disabled_or_message_arrives_first():
    async def get(after, wait):
        return [{"seq": 1}]

    fired = []
    await poll_until_message(get, after=0, per_wait=0.0, alarm_after=None,
                             on_alarm=fired.append)
    await poll_until_message(get, after=0, per_wait=0.0, alarm_after=0.0,
                             on_alarm=fired.append)
    assert fired == []


# --- B.6 coverage rendering ------------------------------------------------


def test_coverage_report_renders_a_summary_not_the_rows():
    """This timeline is what the operator reads when something looks suspicious. Per-row
    verdicts scale with the diff, so dumping them here would spend exactly the attention
    the design exists to protect — but UNREACHED rows are named, because they are the
    worklist and the thing the operator rules on if one survives two passes."""
    out = render_message({
        "seq": 6, "role": "critic", "kind": "coverage_report", "created_at": "t",
        "payload": {"artifact_seq": 1, "manifest_id": "m1", "rows": [
            {"row_id": "a", "verdict": "reviewed-clean"},
            {"row_id": "b", "verdict": "reviewed-clean"},
            {"row_id": "c", "verdict": "not-reached", "reason": "generated file"},
        ]},
    })
    assert "reviewed-clean=2" in out
    assert "not reached: c — generated file" in out
    assert "row_id" not in out  # the raw payload never lands in the readable view


def test_coverage_report_renders_a_compacted_payload_too():
    """The replay hands superseded reports over in their collapsed form."""
    out = render_message({
        "seq": 7, "role": "critic", "kind": "coverage_report", "created_at": "t",
        "payload": {"artifact_seq": 1, "counts": {"reviewed-clean": 3},
                    "not_reached": [{"row_id": "z", "reason": "unreviewable"}]},
    })
    assert "reviewed-clean=3" in out
    assert "not reached: z — unreviewable" in out


def test_coverage_manifest_renders_its_size_and_high_stakes_count():
    out = render_message({
        "seq": 5, "role": "development", "kind": "coverage_manifest", "created_at": "t",
        "payload": {"artifact_seq": 1, "manifest_id": "m1", "granularity": "symbol",
                    "rows": [{"row_id": "a", "high_stakes": True},
                             {"row_id": "hunt-by-name"}]},
    })
    assert "m1" in out and "2 row(s)" in out and "1 high-stakes" in out


def test_structured_b6_notices_render_their_content_not_a_blank_line():
    """The B.6 notices carry no `text` at all, so rendering only `text` showed the operator
    an empty line exactly where the applied depth tier or the unreached worklist belongs."""
    tier = render_message({
        "seq": 3, "role": "operator", "kind": "notice", "created_at": "t",
        "payload": {"phase": "review_depth", "tier": "standard", "granted_by": "operator",
                    "unreached_allowance": 0},
    })
    assert "depth tier = standard" in tier and "granted by operator" in tier

    blocked = render_message({
        "seq": 4, "role": "system", "kind": "notice", "created_at": "t",
        "payload": {"phase": "convergence_blocked", "artifact_seq": 9,
                    "unreached_rows": ["r1", "r2"], "undisposed_findings": []},
    })
    assert "2 unreached coverage row(s)" in blocked
    assert "unreached: r1, r2" in blocked


def test_an_unknown_structured_notice_names_its_phase_rather_than_rendering_empty():
    out = render_message({
        "seq": 5, "role": "critic", "kind": "notice", "created_at": "t",
        "payload": {"phase": "cold_verdicts", "cold_verdicts": [{"element_id": "P-1"}]},
    })
    assert "cold_verdicts" in out
    assert out.rstrip().endswith("cold_verdicts")


def test_a_missing_manifest_blocker_is_named_not_rendered_as_none():
    """A boolean blocker among lists: without it a review blocked ONLY by the
    development-owed missing manifest rendered as 'no blockers listed'."""
    out = render_message({
        "seq": 9, "role": "system", "kind": "notice", "created_at": "t",
        "payload": {"phase": "convergence_blocked", "artifact_seq": 9,
                    "unreached_rows": [], "missing_coverage_manifest": True},
    })
    assert "no coverage manifest for this artifact version" in out
    assert "no blockers listed" not in out


def test_coverage_report_renders_typed_failure_reasons():
    """Round 13, finding b14-typed-coverage-failure-reasons-hidden: the typed B.14
    outcomes carry their reason to the operator exactly as not-reached does — the
    escalation text promises the reason is on the report row."""
    out = render_message({
        "seq": 8, "role": "critic", "kind": "coverage_report", "created_at": "t",
        "payload": {"artifact_seq": 1, "manifest_id": "m1", "rows": [
            {"row_id": "b1::change", "verdict": "reviewed-clean"},
            {"row_id": "b1::seam", "verdict": "cannot_reach", "reason": "dead branch"},
            {"row_id": "b2::seam", "verdict": "instrument_failure", "reason": "sandbox died"},
        ]},
    })
    assert "cannot_reach: b1::seam — dead branch" in out
    assert "instrument_failure: b2::seam — sandbox died" in out
