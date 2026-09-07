# SPDX-License-Identifier: Apache-2.0
"""Review-process counters (B.6 §6, element M-1).

The review process had no eval, which is why every question about it reduced to taste.
These five counters are what make the design's own claims checkable — and two of them are
safety counters that detect the economic layer suppressing what it should have surfaced:

1. **coverage-claim reliability** — the share of rows claimed ``reviewed-clean`` that a
   later pass turns into a finding. ``reviewed-clean`` is a cheap claim, exactly like a
   false ``fixed``; this is the number that says how cheap, and it is what settles whether
   an independent audit of clean claims is worth generalising beyond the deepest tier.
2. **class compression** — findings per distinct root class and the pass-span of each class.
   The baseline to beat is a real one: in a converged spec review, a single class surfaced
   across NINE separate passes, one instance at a time.
3. **operator touches** — how often the loop spent the operator's attention, split by
   whether their ruling agreed with the route development proposed. Disagreement is the
   signal worth having: agreement at scale means the escalation could have been handled
   below him.
4. **wall clock and pass count**, by depth tier — whether the throttle actually throttles.
5. **drop reversal rate** — how often a below-threshold waiver is contested and then
   overturned. A rising number means the triage threshold is set where it swallows real
   findings.

Everything is computed READ-TIME from the append-only message log, which is what lets the
baseline for counters 2-5 be drawn retroactively from reviews that closed long before this
code existed. Counter 1 cannot: it reads coverage reports, which did not exist before B.6.
One honest gap comes with that: class compression is only measurable where findings carry a
class attribution, so historical reviews contribute passes and touches but not compression.

Pure functions over plain message dicts — no session, no I/O, unit-testable in isolation.
"""

from datetime import datetime

#: Routes stamped on a disposition by the triage (T2-4). `drop` is the one the safety
#: counters watch: it is the route that closes a finding by judging it below threshold.
TRIAGE_ROUTES = ("auto_fix", "auto_dispose", "escalate", "drop")


def _payload(m: dict) -> dict:
    return m.get("payload") or {}


def _triage(m: dict) -> dict:
    """The triage marker on a disposition, defensively.

    Deliberately NOT server-validated (a determined mis-tag survives any schema, and the
    real check on a wrong tag is the critic's contest), so a malformed marker must degrade
    the metric rather than crash the endpoint.
    """
    triage = _payload(m).get("triage")
    return triage if isinstance(triage, dict) else {}


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_operator_answer(m: dict) -> bool:
    """Was this decision_response the OPERATOR's, or one development settled itself?

    Both markers are honoured: the authored role, and an explicit ``answered_by`` for
    bindings that relay everything under one role. Shared by every counter that claims to
    measure operator attention — a guard applied in one place and forgotten in another is
    how two counters end up disagreeing about the same event.
    """
    return (
        m.get("role") != "development"
        and _payload(m).get("answered_by") != "development"
    )


def coverage_claim_reliability(messages: list[dict]) -> dict:
    """Rows claimed clean that a LATER report turned into a finding (counter 1).

    Row ids are content-addressed, so "the same row" is well defined across artifact
    versions and this comparison does not depend on any manifest still being expanded in
    the replay.
    """
    ever_clean: dict[str, int] = {}  # row_id -> artifact version of the first clean claim
    same_version: dict[str, int] = {}
    cross_version: dict[str, int] = {}
    for m in sorted(
        (m for m in messages if m.get("kind") == "coverage_report"),
        key=lambda m: m.get("seq") or 0,
    ):
        version = _payload(m).get("artifact_seq")
        for row in _payload(m).get("rows") or []:
            if not isinstance(row, dict) or not row.get("row_id"):
                continue
            row_id, verdict = row["row_id"], row.get("verdict")
            if verdict == "reviewed-clean":
                # The LATEST clean claim, not the first. Keeping the first meant a row
                # claimed clean on v1 and again on v2, then found on v2, was filed as a
                # cross-version flip — undercounting exactly the same-version signal the
                # split exists to preserve.
                ever_clean[row_id] = version
            elif verdict == "finding" and row_id in ever_clean:
                # A row already filed as a CROSS-version flip can still earn the stronger
                # same-version verdict later (clean v1 -> finding v2 -> clean v2 -> finding
                # v2). Freezing the first classification undercounted precisely the evidence
                # this split exists to preserve. Same-version is terminal; cross-version is
                # provisional and upgrades.
                if row_id in same_version:
                    continue
                # A row id is derived from kind + locator, so it survives an artifact
                # version bump BY DESIGN — that is what makes the late-surfacing diagnostic
                # well defined. But it also means the surface behind the row may have
                # CHANGED in between, and a finding on genuinely new content is not evidence
                # that the earlier clean claim was hollow. The two cases are reported
                # separately rather than folded into one number that flatters neither:
                # same-version flips are the strong signal, cross-version ones are ambiguous
                # by construction and the lineage-vs-reopen rule is what tells them apart.
                if ever_clean[row_id] == version:
                    same_version[row_id] = version
                    cross_version.pop(row_id, None)  # upgraded out of the weaker bucket
                else:
                    cross_version[row_id] = version
    total = len(ever_clean)
    return {
        "rows_ever_claimed_clean": total,
        "rows_later_carrying_a_finding": len(same_version) + len(cross_version),
        "same_version_flips": sorted(same_version),
        "cross_version_flips": sorted(cross_version),
        # The headline share counts only same-version flips: the row did not change, and a
        # later pass still found something. None rather than 0 on an empty denominator —
        # "no data yet" and "perfectly reliable" are different answers, and only one of them
        # justifies acting on the number.
        "unreliable_share": (len(same_version) / total) if total else None,
    }


def class_compression(messages: list[dict]) -> dict:
    """Findings per distinct root class, and each class's pass span (counter 2).

    The pass span is what the drip pathology actually looks like: not "many findings" but
    ONE defect discovered one instance at a time across many passes. Findings without a
    ``root_class`` are counted separately rather than lumped into a synthetic class — an
    unattributed finding is missing data, and averaging it in would flatter the number.
    """
    classes: dict[str, dict] = {}
    unattributed = 0
    pass_index = 0
    for m in sorted(
        (m for m in messages if m.get("kind") == "findings"),
        key=lambda m: m.get("seq") or 0,
    ):
        pass_index += 1
        for item in _payload(m).get("items") or []:
            if not isinstance(item, dict):
                continue
            root = item.get("root_class")
            if not isinstance(root, str) or not root.strip():
                unattributed += 1
                continue
            entry = classes.setdefault(
                root.strip(), {"instances": 0, "first_pass": pass_index, "last_pass": pass_index}
            )
            entry["instances"] += 1
            entry["last_pass"] = pass_index
    for entry in classes.values():
        entry["pass_span"] = entry["last_pass"] - entry["first_pass"] + 1
    attributed = sum(e["instances"] for e in classes.values())
    return {
        "distinct_classes": len(classes),
        "attributed_findings": attributed,
        "unattributed_findings": unattributed,
        "findings_per_class": (attributed / len(classes)) if classes else None,
        "widest_pass_span": max((e["pass_span"] for e in classes.values()), default=0),
        "classes": dict(sorted(classes.items())),
    }


def operator_touches(messages: list[dict]) -> dict:
    """Escalations and questions that reached the operator, and how they landed (counter 3).

    The agreement split is development's own report of whether the operator's ruling matched
    the route it proposed. That is self-reported by the interested party — which is fine for
    a metric and would not be fine for a gate; read it as a trend, not as evidence in a
    dispute.
    """
    raised = sum(1 for m in messages if m.get("kind") in ("escalation", "human_question"))
    responses = [m for m in messages if m.get("kind") == "decision_response"]
    # This counter measures the OPERATOR'S attention, so a response development settled
    # mechanically must not be counted as the operator having been spent. Both markers are
    # honoured: the authored role, and an explicit `answered_by` for bindings that relay
    # everything under one role.
    answers = [m for m in responses if _is_operator_answer(m)]
    settled_by_development = len(responses) - len(answers)
    agreed = sum(1 for m in answers if _payload(m).get("agreed_with_proposal") is True)
    disagreed = sum(1 for m in answers if _payload(m).get("agreed_with_proposal") is False)
    return {
        "items_raised": raised,
        "operator_answers": len(answers),
        "settled_by_development": settled_by_development,
        "ruled_with_development": agreed,
        "ruled_against_development": disagreed,
        "unreported_agreement": len(answers) - agreed - disagreed,
    }


def pace(messages: list[dict]) -> dict:
    """Wall clock, pass count and the depth tier they were spent at (counter 4)."""
    tier = None
    granted_by = None
    for m in messages:
        payload = _payload(m)
        if m.get("kind") == "notice" and payload.get("phase") == "review_depth":
            tier = payload.get("tier") or tier
            granted_by = payload.get("granted_by") or granted_by
    stamps = [ts for ts in (_parse_ts(m.get("created_at")) for m in messages) if ts]
    # Every pass the critic ENDED counts, `needs_human` included: a pass that stopped on a
    # blocker still spent its invocation and its wall clock, and it is often the one that
    # parked the review. Counting only the two "productive" endings would flatter exactly
    # the number this counter exists to expose. Server-emitted statuses are still excluded —
    # the self-completion path posts its own `converged`, and that is a pass nobody ran.
    passes = sum(
        1
        for m in messages
        if m.get("kind") == "status"
        and m.get("role") == "critic"
        and _payload(m).get("value") in ("needs_iteration", "converged", "needs_human")
    )
    artifact_versions = sum(1 for m in messages if m.get("kind") == "artifact")
    wall_clock = None
    if len(stamps) >= 2:
        wall_clock = (max(stamps) - min(stamps)).total_seconds()
    return {
        "depth_tier": tier,
        "depth_tier_granted_by": granted_by,
        "critic_passes": passes,
        "artifact_versions": artifact_versions,
        "wall_clock_seconds": wall_clock,
    }


def drop_reversals(messages: list[dict]) -> dict:
    """Below-threshold waivers, and how often the operator put them back (counter 5).

    Order matters and is respected: a contest counts only when it comes AFTER the drop it
    disputes, and an overturn only when it comes after the contest. An answer that predates
    the disposition it appears to resolve settles nothing.
    """
    drops: dict[str, int] = {}  # finding_id -> seq of the drop
    contested: dict[str, int] = {}
    overturned: dict[str, int] = {}
    for m in sorted(messages, key=lambda m: m.get("seq") or 0):
        payload, seq, kind = _payload(m), m.get("seq") or 0, m.get("kind")
        if kind == "disposition" and _triage(m).get("route") == "drop":
            fid = payload.get("finding_id")
            if fid:
                drops[fid] = seq
        elif kind == "escalation" and payload.get("kind") == "contested_disposition":
            # Only a CONTESTED DISPOSITION is a contest of the drop. Another escalation that
            # happens to name the same finding — an oscillation fork, say — is about
            # something else, and counting it would inflate a safety counter in the
            # reassuring direction.
            fid = payload.get("finding_id")
            if fid in drops and drops[fid] < seq and fid not in contested:
                contested[fid] = seq
        elif kind == "decision_response" and _is_operator_answer(m):
            # Same attribution guard as the operator-touches counter: a response development
            # settled mechanically is not the operator overturning anything.
            fid = payload.get("finding_id")
            if fid in contested and contested[fid] < seq and _overturns(payload):
                overturned[fid] = seq
    return {
        "drops": len(drops),
        "drops_contested": len(contested),
        "drops_overturned": len(overturned),
        "reversal_share": (len(overturned) / len(drops)) if drops else None,
        "overturned_findings": sorted(overturned),
    }


def _overturns(payload: dict) -> bool:
    """Did the operator's answer put a dropped finding back into play?

    Mirrors the server's return-signal set (the ``requires_new_artifact`` family) and adds
    the explicit form, so an operator ruling that overturns a drop without demanding a new
    artifact version still counts.
    """
    return bool(
        payload.get("overturns_disposition")
        or payload.get("requires_new_artifact")
        or payload.get("returns")
        or payload.get("requires_artifact_change")
        or payload.get("reopen")
    )


def triage_ledger(messages: list[dict]) -> dict:
    """Dispositions grouped by triage route — the enumerable ledger T2-6 discloses.

    ``unrouted`` is reported rather than hidden: a disposition carrying no route is a defect
    in the ledger, not an item to skip, and the post-review summary is where that has to
    become visible.
    """
    by_route: dict[str, list[str]] = {route: [] for route in TRIAGE_ROUTES}
    unrouted: list[str] = []
    for m in messages:
        if m.get("kind") != "disposition":
            continue
        fid = _payload(m).get("finding_id") or ""
        route = _triage(m).get("route")
        if route in by_route:
            by_route[route].append(fid)
        else:
            unrouted.append(fid)
    return {"by_route": by_route, "unrouted": unrouted}


def gate_directives(messages: list[dict]) -> dict:
    """The round gate's own counters (B.7 C-2) — no new machinery, just a count.

    ``class_analysis`` is the load-bearing one: every such directive is, BY CONSTRUCTION, a
    journal-recorded event of "development did not run the class gate itself, the operator
    had to demand it". It is the first measurable signal for a failure that used to be
    visible only as operator frustration, and a down payment on the standing "the process
    has no eval" gap.

    ``detail`` counts the findings whose digest line was not enough to rule on — a read on
    the digest's own quality, from the one party that can judge it.
    """
    by_directive: dict[str, int] = {d: 0 for d in ("accept", "amend", "detail", "class_analysis")}
    for m in messages:
        if m.get("kind") != "gate_directive":
            continue
        directive = _payload(m).get("directive")
        if directive in by_directive:
            by_directive[directive] += 1
    rounds = sum(
        1
        for m in messages
        if m.get("kind") == "proposals"
    )
    return {"rounds_gated": rounds, "by_directive": by_directive}


def review_metrics(messages: list[dict]) -> dict:
    """All five counters plus the routed ledger and the round gate's, for one review."""
    return {
        "coverage_claim_reliability": coverage_claim_reliability(messages),
        "class_compression": class_compression(messages),
        "operator_touches": operator_touches(messages),
        "pace": pace(messages),
        "drop_reversals": drop_reversals(messages),
        "triage_ledger": triage_ledger(messages),
        "gate_directives": gate_directives(messages),
    }
