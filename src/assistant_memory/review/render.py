# SPDX-License-Identifier: Apache-2.0
"""Render review messages to human-readable markdown (spec §9.1).

Pure functions over the message dict shape the API returns (``routes._msg``:
``seq``/``role``/``kind``/``payload``/``created_at``). One consumer is the reserve-mode
local log (§12); the other is the on-demand readable timeline rendered from the store
(§9.1). Kept side-effect-free so it is trivially testable and reusable in both.
"""

from typing import Any

from . import round_gate

Message = dict[str, Any]


def _p(payload: Any) -> dict:
    return payload if isinstance(payload, dict) else {}


def _finding_line(f: dict) -> str:
    """One finding item, tolerant of missing optional fields (spec §4 Finding)."""
    tags = "/".join(str(f[k]) for k in ("severity", "class") if f.get(k) is not None)
    tier = f.get("tier")
    head = f"{f.get('id', '?')}"
    if tags:
        head += f" [{tags}{', tier=' + str(tier) if tier else ''}]"
    parts = [head]
    if f.get("claim"):
        parts.append(str(f["claim"]))
    if f.get("location"):
        parts.append(f"@ {f['location']}")
    if f.get("suggestion"):
        parts.append(f"→ {f['suggestion']}")
    if f.get("lineage_of"):
        parts.append(f"(lineage of {f['lineage_of']})")
    if f.get("reopens_finding_id"):
        parts.append(f"(reopens {f['reopens_finding_id']})")
    return " ".join(parts)


def _body(kind: str, payload: dict) -> list[str]:
    """The readable body line(s) for a message kind (spec §4). Returns 1+ lines."""
    if kind == "artifact":
        ref = payload.get("artifact_ref")
        mode = payload.get("mode")
        bits = [b for b in (f"mode={mode}" if mode else None, f"ref={ref}" if ref else None) if b]
        return [f"artifact posted{' (' + ', '.join(bits) + ')' if bits else ''}"]

    if kind == "findings":
        items = payload.get("items") or []
        anchor = payload.get("artifact_seq")
        if not items:
            return [f"clean pass — 0 findings over v{anchor}"]
        lines = [f"{len(items)} finding(s) over v{anchor}:"]
        lines += [f"    - {_finding_line(f)}" for f in items]
        return lines

    if kind == "disposition":
        waiver = payload.get("waiver_id")
        tail = f" [waiver {waiver}]" if waiver else ""
        return [
            f"{payload.get('finding_id', '?')} → {payload.get('outcome', '?')}"
            f" over v{payload.get('artifact_seq')}: {payload.get('reason', '')}{tail}"
        ]

    if kind == "status":
        it = payload.get("iteration")
        return [
            f"status={payload.get('value', '?')} (v{payload.get('artifact_seq')}"
            f"{', iter ' + str(it) if it is not None else ''})"
        ]

    if kind == "human_question":
        opts = payload.get("options")
        qid = payload.get("id")
        return [
            f"human_question{' [' + str(qid) + ']' if qid else ''}:"
            f" {payload.get('question', '')}"
            f"{' | options: ' + ', '.join(map(str, opts)) if opts else ''}"
        ]

    if kind == "waiver":
        return [
            f"waiver [{payload.get('gate', '?')}]: {payload.get('reason', '')}"
            f" (granted={payload.get('granted')})"
        ]

    if kind == "dissent":
        return [
            f"dissent on \"{payload.get('decision', '')}\": {payload.get('objection', '')}"
            f" (recorded={payload.get('recorded')})"
        ]

    if kind == "escalation":
        ref = payload.get("element_id") or payload.get("finding_id")
        return [
            f"escalation [{payload.get('kind', '?')}]{' ' + str(ref) if ref else ''}:"
            f" {payload.get('detail', '')}"
        ]

    if kind == "decision_response":
        # the resolution key, in the current schema (element_id / finding_id / question_id)
        ref = payload.get("element_id") or payload.get("finding_id") or payload.get("question_id")
        rat = payload.get("rationale")
        return [
            f"decision_response [{ref or '?'}]: held={payload.get('held')}"
            f"{' — ' + str(rat) if rat else ''}"
        ]

    if kind == "coverage_manifest":
        rows = payload.get("rows")
        count = len(rows) if isinstance(rows, list) else payload.get("row_count", "?")
        high = (
            sum(1 for r in rows if isinstance(r, dict) and r.get("high_stakes"))
            if isinstance(rows, list)
            else 0
        )
        return [
            f"coverage manifest {payload.get('manifest_id', '?')} over "
            f"v{payload.get('artifact_seq')}: {count} row(s), "
            f"granularity={payload.get('granularity', '?')}"
            f"{f', {high} high-stakes' if high else ''}"
        ]

    if kind == "coverage_report":
        # A summary, never the rows. This timeline is what the operator reads when a
        # chat-surfaced item looks suspicious — per-row verdicts scale with the diff, so
        # dumping them here would spend exactly the attention the whole design protects.
        if payload.get("unanswerable"):
            # Named, never rendered as an empty-but-fine report: a declaration of NO coverage
            # is the one the operator most needs to see in the timeline.
            return [
                f"coverage report over v{payload.get('artifact_seq')}: UNANSWERABLE — no row "
                f"was verdicted. Reason: {payload['unanswerable']}"
            ]
        rows = payload.get("rows")
        if not isinstance(rows, list):
            counts = payload.get("counts") or {}
            not_reached = payload.get("not_reached") or []
        else:
            counts = {}
            for r in rows:
                if isinstance(r, dict):
                    verdict = str(r.get("verdict") or "unknown")
                    counts[verdict] = counts.get(verdict, 0) + 1
            not_reached = [
                r
                for r in rows
                if isinstance(r, dict)
                and r.get("verdict")
                in ("not-reached", "cannot_reach", "instrument_failure")
            ]
        summary = ", ".join(f"{v}={n}" for v, n in sorted(counts.items())) or "no rows"
        # B.12 C-4: the granularity travels BESIDE the count, wherever the count is shown
        # to a human. A light tier produces a flattering coverage number over a shallow
        # reading — "5 of 5" and "13 of 113" can be the same reading — and the number is
        # what a human remembers. The manifest line has always printed it; the REPORT line
        # is where it was missing, which is the line that carries the numbers.
        lines = [
            f"coverage report over v{payload.get('artifact_seq')}: {summary}"
            f" (granularity={payload.get('granularity', '?')})"
        ]
        # Unreached rows ARE named: they are the worklist that routes another sweep, and
        # the thing the operator reads if one survives two passes.
        # Round 13, b14-typed-coverage-failure-reasons-hidden: the typed B.14
        # outcomes carry their reason to the operator exactly as not-reached does —
        # the escalation text promises the reason is on the report row.
        lines += [
            # collapsed payloads carry `not_reached` entries with no verdict key —
            # they default to the legacy label
            f"    - {(r.get('verdict') or 'not-reached') if (r.get('verdict') or 'not-reached') != 'not-reached' else 'not reached'}: "
            f"{r.get('row_id', '?')} — {r.get('reason', '')}"
            for r in not_reached
        ]
        return lines

    if kind == "proposals":
        entries = payload.get("entries") or []
        lines = [f"{len(entries)} proposal(s) — nothing implemented yet:"]
        lines += [
            f"    - {e.get('finding_id', '?')} → {e.get('proposed_outcome', '?')}"
            f" [{(e.get('class_closure_claim') or {}).get('kind', '?')}]: {e.get('reason', '')}"
            for e in entries
            if isinstance(e, dict)
        ]
        return lines

    if kind == "gate_directive":
        amendment = payload.get("amendment") or {}
        tail = f" → {amendment.get('outcome')}" if amendment.get("outcome") else ""
        return [
            f"operator directive [{payload.get('finding_id', '?')}]:"
            f" {payload.get('directive', '?')}{tail} — «{payload.get('operator_quote', '')}»"
        ]

    if kind == "detail_report":
        return [f"detail report [{payload.get('directive_ref', '?')}]: {payload.get('context', '')}"]

    if kind == "class_report":
        candidates = payload.get("candidates") or []
        return [
            f"class report [{payload.get('directive_ref', '?')}]:"
            f" {payload.get('root', '')} — {len(candidates)} candidate(s),"
            f" closure={payload.get('closure_kind', '?')};"
            f" cannot see: {payload.get('false_negative_mode', '')}"
        ]

    if kind == "operator_finalize":
        return [
            f"OPERATOR FINALIZE [{payload.get('mode', '?')}]:"
            f" «{payload.get('operator_quote', '')}»"
        ]

    if kind == "operator_projection":
        # B.12 C-1. ONE LINE, and the halves are named rather than printed: a projection
        # carries the machine item verbatim beside a four-part translation, and dumping
        # both into a timeline would spend exactly the attention this record exists to
        # protect. What the line has to answer is «which operator item is this, and does it
        # carry both halves» — which is also what the H-6 join asks of it.
        parts = [p for p in ("what_happened", "concerns", "proposal", "answers") if payload.get(p)]
        head = " ".join(str(payload.get("what_happened") or "").split())[:160]
        return [
            f"operator projection [{payload.get('item_key', '?')}]:"
            f" {len(parts)}/4 parts, original "
            f"{'carried' if payload.get('original') else 'MISSING'}"
            + (f" — {head}" if head else "")
        ]

    if kind == "map":
        # THE BODY IS NEVER PRINTED HERE. A map is a whole document written for the
        # operator's gate; the timeline is what they scan when something looks wrong, and a
        # 100 KB body in it makes the timeline unreadable exactly when it is being used.
        # (B.11 added three kinds without rendering, so all three fell through to the
        # payload dump below. Found while adding the projection's line, and closed for the
        # class rather than for the one kind.)
        body = payload.get("body_markdown") or ""
        return [
            f"semantic map over {payload.get('base', '?')[:12]}..{payload.get('commit', '?')[:12]}:"
            f" {len(body)} chars, {len(payload.get('modules') or [])} module(s)"
        ]

    if kind == "map_disposition":
        return [
            f"map disposition [map seq {payload.get('map_seq', '?')}] →"
            f" {payload.get('outcome', '?')}: «{payload.get('operator_quote', '')}»"
        ]

    if kind == "reconciliation":
        if payload.get("outcome") != "produced":
            return [
                f"reconciliation over map seq {payload.get('map_seq', '?')}: FAILED —"
                f" {payload.get('reason', '')}"
            ]
        entries = [e for e in (payload.get("entries") or []) if isinstance(e, dict)]
        # THE INVENTORY IS RENDERED FIRST, and it is rendered even when nothing diverged
        # (finding `b12-acting-constraints-not-bound`, sol round 1). "No discrepancy found"
        # over a set the pass could not fully read is the single most misleading line this
        # report can print, and the pass gets one attempt — so the reader has to be able to
        # see the difference without going back to the journal.
        inventory = [d for d in (payload.get("documents_read") or []) if isinstance(d, dict)]
        unread = [d for d in inventory if d.get("read") is not True]
        opening = []
        if inventory:
            opening.append(
                f"    documents: {len(inventory) - len(unread)} read"
                + (f", {len(unread)} NOT read" if unread else ", none unread")
            )
            opening += [
                f"      - UNREAD {d.get('node_id', '?')}: "
                + " ".join(str(d.get("problem") or "").split())[:160]
                for d in unread
            ]
        else:
            opening.append(
                "    documents: NO inventory carried — what this pass read is unknown"
            )
        if not entries:
            head = (
                f"reconciliation over map seq {payload.get('map_seq', '?')}: produced,"
                " no discrepancy found"
            )
            # "Fully read" is grounded by the POST-time join, not by this renderer: the
            # server refuses a `produced` inventory that is not exactly one row per document
            # of the cycle's set (finding `b12-inventory-not-joined-to-cycle-set`), so a
            # journal message reaching this line with no unread rows really did cover the
            # set. This renderer sees only the payload and could not perform that join.
            head += (
                " — but the set was NOT fully read (see below)"
                if unread or not inventory
                else " over a fully read set (a real result, and the commonest good one)"
            )
            return [head] + opening
        lines = [
            f"reconciliation over map seq {payload.get('map_seq', '?')}:"
            f" {len(entries)} entry(ies), ordered by cost of missing:"
        ] + opening
        lines += [
            f"    - [{e.get('cost_of_missing', '?')}] {e.get('label', '?')}"
            f" @ {e.get('address', '?')}: "
            + " ".join(str(e.get("note") or "").split())[:200]
            for e in entries
        ]
        return lines

    if kind == "notice":
        # Notices are free-form by design, and the B.6 ones are STRUCTURED records with no
        # `text` at all — rendering only `text` showed the operator a blank line exactly
        # where the applied depth tier or the unreached coverage worklist should have been.
        phase = payload.get("phase")
        if phase == "review_depth":
            allowance = payload.get("unreached_allowance")
            bits = [f"depth tier = {payload.get('tier', '?')}"]
            if payload.get("granted_by"):
                bits.append(f"granted by {payload['granted_by']}")
            if allowance is not None:
                bits.append(f"unreached allowance {allowance}")
            return ["notice [review_depth]: " + ", ".join(bits)]
        if phase == "convergence_blocked":
            blockers = [
                (f"{len(payload.get(key) or [])} {label}")
                for key, label in (
                    ("undisposed_findings", "undisposed finding(s)"),
                    ("open_operator_items", "open operator item(s)"),
                    ("ungrafted_external_defects", "ungrafted external defect(s)"),
                    ("unreached_rows", "unreached coverage row(s)"),
                )
                if payload.get(key)
            ]
            # A boolean blocker among lists: without it, a review blocked ONLY by the
            # development-owed missing manifest rendered as "no blockers listed", hiding the
            # actual reason it is sitting in dev_disposing.
            if payload.get("missing_coverage_manifest"):
                blockers.append("no coverage manifest for this artifact version")
            lines = [
                "notice [convergence_blocked]: clean pass recorded; "
                + (", ".join(blockers) if blockers else "no blockers listed")
            ]
            if payload.get("unreached_rows"):
                lines.append(f"    - unreached: {', '.join(payload['unreached_rows'])}")
            return lines
        if payload.get("text"):
            return [f"notice: {payload['text']}"]
        # Any other structured notice (cold verdicts, a critic memo, a watcher error):
        # name its phase and its keys rather than printing an empty line.
        keys = ", ".join(k for k in payload if k != "phase")
        return [f"notice{f' [{phase}]' if phase else ''}: {keys or '(empty)'}"]

    return [f"{kind}: {payload}"]  # forward-compatible fallback — never drop an event


def render_message(msg: Message) -> str:
    """One timestamped entry (spec §9.1): header line + any body lines, indented."""
    payload = _p(msg.get("payload"))
    ts = msg.get("created_at") or "?"
    header = f"- [{ts}] (seq {msg.get('seq')}) {msg.get('role')}/{msg.get('kind')}:"
    body = _body(str(msg.get("kind")), payload)
    return "\n".join([f"{header} {body[0]}", *body[1:]])


def render_timeline(messages: list[Message], *, header: str | None = None) -> str:
    """The full readable timeline — a rendered entry per message, in order."""
    lines = [f"# {header}", ""] if header else []
    lines += [render_message(m) for m in messages]
    return "\n".join(lines)


# --- the computed service tail (B.7 F-4) ---------------------------------


def _findings_index(messages: list[Message]) -> dict[str, dict]:
    """Every finding ever raised, by id, with the claim text as first seen."""
    out: dict[str, dict] = {}
    for m in messages:
        if m.get("kind") != "findings":
            continue
        for f in _p(m.get("payload")).get("items") or []:
            if isinstance(f, dict) and f.get("id") and f["id"] not in out:
                out[str(f["id"])] = f
    return out


def _terminal_dispositions(messages: list[Message]) -> dict[str, dict]:
    """The LAST terminal disposition per finding — the ledger's effective state."""
    out: dict[str, dict] = {}
    for m in messages:
        payload = _p(m.get("payload"))
        if m.get("kind") == "disposition" and payload.get("outcome") in (
            "fixed", "waived", "operator_risk_accepted", "fixed_unverified",
        ):
            out[str(payload.get("finding_id"))] = {**payload, "_role": m.get("role")}
    return out


def _escalation_ledger(messages: list[Message]) -> list[str]:
    """Every escalation with how it was settled — or an explicit OPEN."""
    opened: list[tuple[str, dict]] = []
    answers: list[dict] = []
    for m in messages:
        payload = _p(m.get("payload"))
        if m.get("kind") == "escalation":
            key = (
                f"ext:{payload.get('id')}"
                if payload.get("kind") == "external_defect"
                else f"element:{payload['element_id']}"
                if payload.get("element_id")
                else f"finding:{payload['finding_id']}"
                if payload.get("finding_id")
                else f"seq:{m.get('seq')}"
            )
            opened.append((key, payload))
        elif m.get("kind") == "decision_response":
            answers.append(payload)
    lines = []
    for key, payload in opened:
        answer = next(
            (
                a
                for a in answers
                if key.endswith(str(a.get("escalation_id") or a.get("element_id")
                                    or a.get("finding_id") or "\0"))
            ),
            None,
        )
        settled = (
            f"settled: {answer.get('action') or ('held' if answer.get('held') else 'answered')}"
            f" — {answer.get('rationale') or answer.get('answer') or ''}"
            if answer
            else "**OPEN — never settled**"
        )
        lines.append(
            f"- [{payload.get('kind', '?')}] {key}: {payload.get('detail', '')} → {settled}"
        )
    return lines


def render_service_tail(messages: list[Message], *, state: str | None = None) -> str:
    """The post-review summary's bookkeeping half, COMPUTED from the journal (B.7 F-4).

    Development copies this verbatim into the summary instead of authoring it. Two reasons,
    both measured: a computed ledger needs no faithfulness audit, which shortens the audit
    cycle to the subject description alone; and the ledger is the proof that no finding was
    dropped, which is exactly the claim least safe to leave to recollection by the party
    the summary gates.

    Absent machinery is stated as absent, never omitted silently — a review with no
    coverage map says so, rather than leaving the operator to read a missing section as a
    clean one.
    """
    findings = _findings_index(messages)
    disposed = _terminal_dispositions(messages)
    artifacts = [m for m in messages if m.get("kind") == "artifact"]
    final = artifacts[-1]["seq"] if artifacts else None
    gate_directives = [m for m in messages if m.get("kind") == "gate_directive"]
    class_analysis = [
        m for m in gate_directives if _p(m.get("payload")).get("directive") == "class_analysis"
    ]
    finalize = next(
        (m for m in messages if m.get("kind") == "operator_finalize"), None
    )

    out: list[str] = ["## Service tail (computed from the review journal)", ""]
    outcome = state or "in flight"
    if finalize is not None:
        outcome += (
            f" — the OPERATOR stopped this review "
            f"({_p(finalize.get('payload')).get('mode')}): "
            f"«{_p(finalize.get('payload')).get('operator_quote', '')}»"
        )
    out += [f"**Outcome.** {outcome}; final artifact version: v{final}.", ""]

    out.append("**Findings ledger.** Every finding raised, with its terminal disposition:")
    if not findings:
        out.append("- no findings were raised in this review")
    for fid, f in findings.items():
        d = disposed.get(fid)
        claim = " ".join(str(f.get("title") or f.get("claim") or "").split())[:160]
        ftype = f.get("finding_type")
        head = f"- `{fid}`" + (f" [{ftype}]" if ftype else "") + (f" — {claim}" if claim else "")
        if d is None:
            out.append(f"{head} → **UNDISPOSED** (a hole in the ledger, reported as such)")
        else:
            by = " (server-emitted)" if d.get("_role") == "system" else ""
            out.append(f"{head} → **{d.get('outcome')}**{by}: {d.get('reason', '')}")
    out.append("")

    # B.11 D-1: THE WORD "WAIVER" HAS FOUR REFERENTS IN THIS SYSTEM, and this section used
    # to be headed with the bare word while listing only one of them. Twice measured: in
    # bb5f3494 the block printed "none" while the finding ledger three paragraphs above
    # carried a `waived` disposition, forcing a hand-written annotation under a tail the
    # operator is told to copy VERBATIM; in 690a60c7 the same heading, read honestly, said
    # "one waived thing" while ten findings were waived above under a different heading.
    #
    # The four senses, each with its own validation path and its own home:
    #   1. procedural waiver   — a channel `waiver` message granted at a gate
    #                            (repository._validate_waiver_payload);
    #   2. waived finding      — a terminal disposition outcome beside `fixed`
    #                            (repository, the disposition outcome vocabulary);
    #   3. below-threshold drop— the economic layer's `drop` route (metrics.py);
    #   4. self-check waiver   — the typed operator grant permitting a same-model review,
    #                            frozen into the instrument snapshot (instruments.py).
    #
    # THE NAMING RULE IS THE STRUCTURAL HALF, and it is what makes this a class closure
    # rather than one renamed heading: in operator-facing prose each sense is named by its
    # OWN phrase, and the bare word "waiver" is never a section heading anywhere. A fifth
    # sense then shows up as a heading that violates the rule, with no lexical sweep.
    #
    # Nothing about the DATA changes here: the two senses printed below are validated
    # separately and independently in the server already. Only the prose that reports them.
    waivers = [m for m in messages if m.get("kind") == "waiver"]
    waived_findings = [
        fid for fid, d in _terminal_dispositions(messages).items()
        if d.get("outcome") == "waived"
    ]
    out.append("**Procedural waivers granted at gates.**")
    out += (
        [
            f"- [{_p(m.get('payload')).get('gate', '?')}] "
            f"{_p(m.get('payload')).get('reason', '')} "
            f"(granted={_p(m.get('payload')).get('granted')})"
            for m in waivers
        ]
        or ["- none"]
    )
    out.append(
        f"- **waived findings** (a different thing, counted where they live — the finding "
        f"ledger above): {len(waived_findings)}"
        + (f" — {', '.join(f'`{fid}`' for fid in sorted(waived_findings))}" if waived_findings else "")
    )
    out.append("")

    dissents = [m for m in messages if m.get("kind") == "dissent"]
    out.append("**Dissents recorded.**")
    out += (
        [
            f"- on «{_p(m.get('payload')).get('decision', '')}»: "
            f"{_p(m.get('payload')).get('objection', '')}"
            for m in dissents
        ]
        or ["- none"]
    )
    out.append("")

    out.append("**Escalations & their resolution.**")
    out += _escalation_ledger(messages) or ["- none"]
    out.append("")

    routed = [
        (str(_p(m.get("payload")).get("finding_id")), _p(m.get("payload")))
        for m in messages
        if m.get("kind") == "disposition" and m.get("role") != "system"
    ]
    # B.11 D-2: THE HEADER ASSERTED THE OPPOSITE OF WHAT THE ROWS SAID. In bb5f3494 all
    # twenty findings were dispositioned by the operator — in person or by a standing
    # decision, visible in the `basis` of every row — under a fixed header claiming
    # "everything handled without the operator". A tail the operator is asked to copy
    # verbatim must not need a hand-written correction to be true, so the header is
    # derived from the rows rather than asserted over them.
    _operator_basis = any(
        "operator" in str((payload.get("triage") or {}).get("basis", "")).lower()
        for _fid, payload in routed
    )
    _all_wait = round_gate.gate_mode([dict(m) for m in messages]) == "all_wait"
    if _operator_basis or _all_wait:
        out.append(
            "**Triage ledger** (this round's gate mode"
            + (" was `all_wait`" if _all_wait else " routed some findings past the operator")
            + "; the operator's own rulings are visible in each row's basis):"
        )
    else:
        out.append("**Routed triage ledger** (everything handled without the operator):")
    if not routed:
        out.append("- no development-authored dispositions")
    for fid, payload in routed:
        route = (payload.get("triage") or {}).get("route")
        if route is None:
            out.append(
                f"- `{fid}`: **no `triage.route` recorded** — a defect in the ledger, not "
                "an item to skip"
            )
        else:
            out.append(f"- `{fid}`: route={route}, basis={(payload.get('triage') or {}).get('basis', '')}")
    out.append("")

    if gate_directives:
        out += [
            "**Round gate.** "
            f"{len(gate_directives)} operator directive(s) across the review; "
            f"{len(class_analysis)} of them demanded a class analysis "
            "(a journal-recorded count of the rounds where development did not run the "
            "class gate itself and the operator had to demand it — C-2's metric).",
            "",
        ]

    depth = [
        _p(m.get("payload"))
        for m in messages
        if m.get("kind") == "notice" and _p(m.get("payload")).get("phase") == "review_depth"
    ]
    out.append(
        "**Depth tier actually applied.** "
        + (
            f"{depth[-1].get('tier', '?')} (granted by {depth[-1].get('granted_by', '?')}, "
            f"unreached allowance {depth[-1].get('unreached_allowance', 0)})"
            if depth
            else "none recorded — the review ran on the default"
        )
    )
    out.append("")

    manifests = [
        _p(m.get("payload"))
        for m in messages
        if m.get("kind") == "coverage_manifest"
    ]
    reports = [
        _p(m.get("payload")) for m in messages if m.get("kind") == "coverage_report"
    ]
    out.append("**Coverage map summary.**")
    if not manifests:
        out.append(
            "- this review had NO coverage machinery — stated as absent, not omitted"
        )
    else:
        manifest = manifests[-1]
        rows = manifest.get("rows") or []
        out.append(
            f"- manifest `{manifest.get('manifest_id', '?')}`: {len(rows)} row(s), "
            f"granularity={manifest.get('granularity', '?')}"
        )
        if reports:
            last = reports[-1]
            counts: dict[str, int] = {}
            for r in last.get("rows") or []:
                if isinstance(r, dict):
                    counts[str(r.get("verdict"))] = counts.get(str(r.get("verdict")), 0) + 1
            out.append(
                "- last report: "
                + (", ".join(f"{v}={n}" for v, n in sorted(counts.items())) or "no rows")
                # B.12 C-4: beside the count, always. The service tail is copied verbatim
                # into the post-review summary, so a number that leaves this line without
                # its granularity is a number the operator reads a month later with no way
                # to know what a row was.
                + f" (granularity={last.get('granularity', manifest.get('granularity', '?'))})"
                + " — `reviewed-clean` is a CLAIMED verdict: the map measures claimed "
                "coverage, not reading"
            )
            for r in last.get("rows") or []:
                if isinstance(r, dict) and r.get("verdict") in (
                    "not-reached", "cannot_reach", "instrument_failure",
                ):
                    _v = r.get("verdict")
                    out.append(
                        f"    - {'not reached' if _v == 'not-reached' else _v}: "
                        f"{r.get('row_id')} — {r.get('reason', '')}"
                    )
                if isinstance(r, dict) and r.get("searches_performed"):
                    out.append(
                        f"    - blind edge `{r.get('row_id')}` searches: "
                        + ", ".join(map(str, r["searches_performed"]))
                    )
        else:
            out.append("- no coverage report was posted against it")
    out.append("")

    # B.12 H-6: the join, computed rather than attested. It sits here, beside the audience
    # genre's delivery-audit line, because it audits the same shape of duty: something the
    # server deliberately does not hold, so that a human gesture is checked afterwards
    # instead of being assumed. Computed for the same reason the findings ledger is — a
    # computed ledger needs no faithfulness audit, and this is exactly the claim least safe
    # to leave to the recollection of the party it audits.
    audit = round_gate.projection_audit(messages)
    out.append("**Operator items and their projections (B.12 C-1/H-6).**")
    if not audit["items"]:
        out.append(
            "- no item was routed to the operator in this review — stated as absent, not "
            "omitted"
        )
    else:
        projected = sum(1 for i in audit["items"] if i["projection_seqs"])
        # COUNTED AS "AT LEAST ONE", not "exactly one" (operator's amendment 2026-08-27).
        # A second projection is what happens when the first did not land and the operator
        # asked again — the loop working. The line names which account STOOD, because with
        # several legal that is the question the reader is actually asking.
        out.append(
            f"- {len(audit['items'])} operator item(s) exist on this channel; "
            f"{projected} carry at least one projection"
        )
        for item in audit["items"]:
            seqs = item["projection_seqs"]
            if not seqs:
                rendered = "**NONE**"
            elif len(seqs) == 1:
                rendered = f"projection seq {seqs[0]}"
            else:
                rendered = (
                    "projections " + ", ".join(f"seq {s}" for s in seqs)
                    + f" — the account that stood is seq {item['account_that_stood']}"
                )
            out.append(
                f"    - `{item['key']}` ({item['kind']} at seq {item['seq']}) → {rendered}"
            )
    if audit["failures"]:
        out.append("- **THE CHECK FAILS**, and the failures are named rather than counted:")
        out += [f"    - {f}" for f in audit["failures"]]
        if not any(m.get("kind") == "operator_projection" for m in messages):
            # A channel with NO projection at all is ambiguous in one direction that
            # matters to whoever reads this line: the duty may have been skipped, or the
            # review may simply predate it. The check reports what it sees either way, and
            # says which two readings are open rather than letting the harsher one stand
            # alone — the duty binds this review and every review after it.
            out.append(
                "    - (this channel carries NO projection at all: either none was made, "
                "or the review predates the duty)"
            )
    elif audit["items"]:
        out.append(
            "- the check passes: every operator item has at least one projection, and every "
            "projection carries the machine item verbatim and all four translated parts"
        )
    out.append(
        "- what this does NOT check, deliberately: that the carried original is faithful to "
        "the item, or the translation to the original. That is a recorded trust boundary, "
        "not an omission — and it is narrow: existence, both halves, and no empty part are "
        "all mechanical, and are what the lines above report. Naming the two exits from a "
        "review is outside the enumeration: it raises no message and has no key."
    )
    out.append("")
    return "\n".join(out)
