# SPDX-License-Identifier: Apache-2.0
"""The five §6 review-process counters (B.6 M-1) — pure functions over a message log.

Two of these are safety counters: they exist to detect the economic layer suppressing what
it should have surfaced. So the properties under test are mostly about not flattering the
number — an empty denominator reports "no data" rather than a perfect score, unattributed
findings are counted separately instead of averaged in, and a contest only counts when it
actually came after the thing it contests.
"""

from datetime import UTC, datetime, timedelta

from assistant_memory.review import metrics


def _m(seq, kind, payload=None, role="critic", created_at=None):
    return {
        "seq": seq,
        "role": role,
        "kind": kind,
        "payload": payload or {},
        "created_at": created_at,
    }


# --- counter 1: coverage-claim reliability ---------------------------------


def test_clean_claim_that_later_carries_a_finding_is_counted():
    """`reviewed-clean` is a cheap claim, exactly like a false `fixed`. This is the number
    that says how cheap."""
    log = [
        _m(1, "coverage_report", {"artifact_seq": 9, "rows": [
            {"row_id": "a", "verdict": "reviewed-clean"},
            {"row_id": "b", "verdict": "reviewed-clean"},
        ]}),
        _m(2, "coverage_report", {"artifact_seq": 9, "rows": [
            {"row_id": "a", "verdict": "finding", "finding_id": "f1"},
            {"row_id": "b", "verdict": "reviewed-clean"},
        ]}),
    ]
    out = metrics.coverage_claim_reliability(log)
    assert out["rows_ever_claimed_clean"] == 2
    assert out["rows_later_carrying_a_finding"] == 1
    assert out["unreliable_share"] == 0.5
    assert out["same_version_flips"] == ["a"]


def test_a_flip_across_artifact_versions_is_reported_separately():
    """A row id survives a version bump BY DESIGN — that is what makes the late-surfacing
    diagnostic well defined. But the surface behind it may have CHANGED, and a finding on
    genuinely new content is not evidence that the earlier clean claim was hollow. Folding
    both into one share would misreport the counter that settles whether a clean-claim audit
    is worth generalising."""
    log = [
        _m(1, "coverage_report", {"artifact_seq": 5,
                                  "rows": [{"row_id": "a", "verdict": "reviewed-clean"}]}),
        _m(2, "coverage_report", {"artifact_seq": 9,
                                  "rows": [{"row_id": "a", "verdict": "finding",
                                            "finding_id": "f1"}]}),
    ]
    out = metrics.coverage_claim_reliability(log)
    assert out["cross_version_flips"] == ["a"]
    assert out["same_version_flips"] == []
    assert out["unreliable_share"] == 0.0  # the headline counts only same-version flips


def test_a_finding_before_any_clean_claim_is_not_a_flip():
    log = [
        _m(1, "coverage_report", {"rows": [{"row_id": "a", "verdict": "finding", "finding_id": "f1"}]}),
        _m(2, "coverage_report", {"rows": [{"row_id": "a", "verdict": "reviewed-clean"}]}),
    ]
    out = metrics.coverage_claim_reliability(log)
    assert out["rows_later_carrying_a_finding"] == 0


def test_empty_denominator_reports_no_data_not_perfect_reliability():
    """"No data yet" and "perfectly reliable" are different answers, and only one of them
    justifies acting on the number."""
    assert metrics.coverage_claim_reliability([])["unreliable_share"] is None


# --- counter 2: class compression ------------------------------------------


def test_class_span_measures_the_drip_not_the_count():
    """The pathology is not "many findings" but ONE defect discovered one instance at a
    time across many passes."""
    log = [
        _m(1, "findings", {"items": [{"id": "f1", "root_class": "callable-identity"}]}),
        _m(2, "findings", {"items": [{"id": "f2", "root_class": "key-privacy"}]}),
        _m(3, "findings", {"items": [{"id": "f3", "root_class": "callable-identity"}]}),
    ]
    out = metrics.class_compression(log)
    assert out["distinct_classes"] == 2
    assert out["classes"]["callable-identity"]["instances"] == 2
    assert out["classes"]["callable-identity"]["pass_span"] == 3
    assert out["classes"]["key-privacy"]["pass_span"] == 1
    assert out["widest_pass_span"] == 3


def test_unattributed_findings_are_reported_separately_not_averaged_in():
    """An unattributed finding is missing data; folding it into a synthetic class would
    flatter the compression number."""
    log = [_m(1, "findings", {"items": [{"id": "f1"}, {"id": "f2", "root_class": "c"}]})]
    out = metrics.class_compression(log)
    assert out["unattributed_findings"] == 1
    assert out["attributed_findings"] == 1
    assert out["distinct_classes"] == 1


def test_no_findings_reports_no_ratio():
    assert metrics.class_compression([])["findings_per_class"] is None


# --- counter 3: operator touches -------------------------------------------


def test_operator_touches_split_by_whether_the_ruling_agreed():
    """Disagreement is the signal worth having: agreement at scale means the escalation
    could have been handled below the operator."""
    log = [
        _m(1, "escalation", {"finding_id": "f1"}),
        _m(2, "human_question", {"id": "q1"}),
        _m(3, "decision_response", {"finding_id": "f1", "agreed_with_proposal": True}, role="operator"),
        _m(4, "decision_response", {"question_id": "q1", "agreed_with_proposal": False}, role="operator"),
    ]
    out = metrics.operator_touches(log)
    assert out["items_raised"] == 2
    assert out["ruled_with_development"] == 1
    assert out["ruled_against_development"] == 1
    assert out["unreported_agreement"] == 0


def test_answers_without_the_agreement_flag_are_reported_as_unreported():
    log = [_m(1, "decision_response", {"question_id": "q1"}, role="operator")]
    assert metrics.operator_touches(log)["unreported_agreement"] == 1


# --- counter 4: pace -------------------------------------------------------


def test_pace_reads_the_tier_and_measures_the_span():
    start = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)
    log = [
        _m(1, "notice", {"phase": "review_depth", "tier": "standard", "granted_by": "operator"},
           created_at=start.isoformat()),
        _m(2, "status", {"value": "needs_iteration"}, created_at=(start + timedelta(minutes=30)).isoformat()),
        _m(3, "status", {"value": "converged"}, created_at=(start + timedelta(minutes=45)).isoformat()),
    ]
    out = metrics.pace(log)
    assert out["depth_tier"] == "standard"
    assert out["depth_tier_granted_by"] == "operator"
    assert out["critic_passes"] == 2
    assert out["wall_clock_seconds"] == 45 * 60


def test_pace_survives_a_log_with_no_timestamps():
    """Counters 2-5 are drawn retroactively from past channels — a historical log that
    serialises without timestamps must degrade, not raise."""
    out = metrics.pace([_m(1, "status", {"value": "converged"})])
    assert out["wall_clock_seconds"] is None
    assert out["critic_passes"] == 1


# --- counter 5: drop reversals ---------------------------------------------


def test_drop_contested_and_overturned_is_counted():
    log = [
        _m(1, "disposition", {"finding_id": "f1", "outcome": "waived",
                              "triage": {"route": "drop"}}, role="development"),
        _m(2, "escalation", {"kind": "contested_disposition", "finding_id": "f1"}),
        _m(3, "decision_response", {"finding_id": "f1", "requires_new_artifact": True},
           role="operator"),
    ]
    out = metrics.drop_reversals(log)
    assert out["drops"] == 1
    assert out["drops_contested"] == 1
    assert out["drops_overturned"] == 1
    assert out["reversal_share"] == 1.0


def test_an_answer_predating_the_drop_settles_nothing():
    """Order matters: a response can never pre-answer a contest that has not happened."""
    log = [
        _m(1, "decision_response", {"finding_id": "f1", "requires_new_artifact": True},
           role="operator"),
        _m(2, "disposition", {"finding_id": "f1", "outcome": "waived",
                              "triage": {"route": "drop"}}, role="development"),
    ]
    out = metrics.drop_reversals(log)
    assert out["drops"] == 1
    assert out["drops_overturned"] == 0


def test_a_waiver_without_a_drop_route_is_not_a_drop():
    """`waived` is reused for drops precisely so a drop stays answerable — but only the
    route marker makes it a drop, and the counter must not conflate the two."""
    log = [
        _m(1, "disposition", {"finding_id": "f1", "outcome": "waived",
                              "triage": {"route": "auto_dispose"}}, role="development"),
    ]
    assert metrics.drop_reversals(log)["drops"] == 0


# --- the routed ledger -----------------------------------------------------


def test_ledger_enumerates_by_route_and_names_the_unrouted():
    """A disposition carrying no route is a defect in the ledger, not an item to skip —
    the post-review summary is where that has to become visible."""
    log = [
        _m(1, "disposition", {"finding_id": "f1", "outcome": "fixed",
                              "triage": {"route": "auto_fix"}}, role="development"),
        _m(2, "disposition", {"finding_id": "f2", "outcome": "waived",
                              "triage": {"route": "drop"}}, role="development"),
        _m(3, "disposition", {"finding_id": "f3", "outcome": "fixed"}, role="development"),
    ]
    out = metrics.triage_ledger(log)
    assert out["by_route"]["auto_fix"] == ["f1"]
    assert out["by_route"]["drop"] == ["f2"]
    assert out["unrouted"] == ["f3"]


def test_a_malformed_triage_marker_degrades_instead_of_crashing():
    """The marker is deliberately NOT server-validated, so the metric must survive one."""
    log = [_m(1, "disposition", {"finding_id": "f1", "outcome": "fixed", "triage": "drop"},
              role="development")]
    assert metrics.triage_ledger(log)["unrouted"] == ["f1"]
    assert metrics.drop_reversals(log)["drops"] == 0


def test_review_metrics_returns_every_counter():
    out = metrics.review_metrics([])
    assert set(out) == {
        "coverage_claim_reliability",
        "class_compression",
        "operator_touches",
        "pace",
        "drop_reversals",
        "triage_ledger",
        "gate_directives",
    }


def test_class_analysis_directives_are_counted():
    """C-2: every `class_analysis` directive is a recorded event of "development did not
    run the class gate itself, the operator had to demand it"."""
    log = [
        _m(1, "proposals", {"entries": [{"finding_id": "f1"}]}, role="development"),
        _m(2, "gate_directive", {"finding_id": "f1", "directive": "class_analysis"},
           role="development"),
        _m(3, "gate_directive", {"finding_id": "f1", "directive": "accept"}, role="development"),
    ]
    out = metrics.gate_directives(log)
    assert out["rounds_gated"] == 1
    assert out["by_directive"]["class_analysis"] == 1
    assert out["by_directive"]["accept"] == 1


# --- from the B.6 implementation review ------------------------------------


def test_a_development_settled_response_is_not_operator_attention():
    """This counter measures the OPERATOR'S attention. A response development settled
    mechanically — a transport fault, say — is not the operator having been spent, and
    counting it would report the exact opposite of what happened."""
    log = [
        _m(1, "human_question", {"id": "q1"}),
        _m(2, "decision_response", {"question_id": "q1", "answered_by": "development"},
           role="development"),
    ]
    out = metrics.operator_touches(log)
    assert out["operator_answers"] == 0
    assert out["settled_by_development"] == 1


def test_a_pass_ending_in_needs_human_still_counts_as_a_pass():
    """It spent its invocation and its wall clock, and it is often the pass that parked the
    review. Counting only the productive endings would flatter the number this counter
    exists to expose."""
    log = [
        _m(1, "status", {"value": "needs_human"}),
        _m(2, "status", {"value": "needs_iteration"}),
        _m(3, "status", {"value": "converged"}, role="system"),  # server self-completion
    ]
    assert metrics.pace(log)["critic_passes"] == 2


def test_only_a_contested_disposition_counts_as_contesting_a_drop():
    """Another escalation naming the same finding — an oscillation fork, say — is about
    something else, and counting it would inflate a SAFETY counter in the reassuring
    direction."""
    log = [
        _m(1, "disposition", {"finding_id": "f1", "outcome": "waived",
                              "triage": {"route": "drop"}}, role="development"),
        _m(2, "escalation", {"kind": "contested_fork", "finding_id": "f1"}),
        _m(3, "decision_response", {"finding_id": "f1", "requires_new_artifact": True},
           role="operator"),
    ]
    out = metrics.drop_reversals(log)
    assert out["drops"] == 1
    assert out["drops_contested"] == 0
    assert out["drops_overturned"] == 0


def test_a_development_settled_response_never_overturns_a_drop():
    log = [
        _m(1, "disposition", {"finding_id": "f1", "outcome": "waived",
                              "triage": {"route": "drop"}}, role="development"),
        _m(2, "escalation", {"kind": "contested_disposition", "finding_id": "f1"}),
        _m(3, "decision_response", {"finding_id": "f1", "requires_new_artifact": True},
           role="development"),
    ]
    assert metrics.drop_reversals(log)["drops_overturned"] == 0


def test_the_latest_clean_claim_decides_whether_a_flip_is_same_version():
    """Keeping the FIRST clean version filed a row claimed clean on v1 and again on v2, then
    found on v2, as a cross-version flip — undercounting the same-version signal the split
    exists to preserve."""
    log = [
        _m(1, "coverage_report", {"artifact_seq": 5,
                                  "rows": [{"row_id": "a", "verdict": "reviewed-clean"}]}),
        _m(2, "coverage_report", {"artifact_seq": 9,
                                  "rows": [{"row_id": "a", "verdict": "reviewed-clean"}]}),
        _m(3, "coverage_report", {"artifact_seq": 9,
                                  "rows": [{"row_id": "a", "verdict": "finding",
                                            "finding_id": "f1"}]}),
    ]
    out = metrics.coverage_claim_reliability(log)
    assert out["same_version_flips"] == ["a"]
    assert out["cross_version_flips"] == []
