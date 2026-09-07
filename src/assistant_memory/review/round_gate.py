# SPDX-License-Identifier: Apache-2.0
"""The B.7 round gate: one mechanical operator stop per round, BEFORE anything is built.

Spec: ``docs/design/2026-08-08_round_gate_spec.md``. The loop this module changes ran
critic pass -> development fixes -> next pass, with the operator seeing the work only
after convergence. B.7 inserts a stop: the critic's findings are answered by a
``proposals`` message (one entry per finding, **nothing implemented**), the server parks
the review while any entry the operator owns is unmarked, and the operator's per-finding
directives are what sanction the work.

WHY THIS IS A SERVER MODULE AND NOT PROMPT PROSE (the design constraint inherited from
B.6 and measured again in B.7's own dry run): a rule that lives only in a prompt does not
bind at the moment of decision — two profile rules were read, quoted, recorded in the gate
document and violated for the whole session (feedback ``b35514c7``), and the class gate
was simply not executed while looking executed from the inside (``cc1b2478``). So the
gate is computed here, from the message log, and refusal happens at POST.

THE STATE IS DERIVED, NEVER STORED. Everything below is a fold over the append-only
channel — the same discipline the convergence guard and the coverage ledger already run
on. A stored round-state column would be a second copy of a fact the log already carries,
and two copies of one fact drift.

ONE IMPLEMENTATION, TWO CONSUMERS. The server parks on these predicates and the watcher
schedules passes from them (H-1). The watcher's own ``open_operator_items`` is a hand copy
of the server's and the two have already disagreed; this module exists so the round gate
never acquires that second meaning. Everything here therefore works on plain message
DICTS (``{seq, role, kind, payload}``) — the shape the watcher already replays and the
shape the server converts to in one comprehension.
"""

from dataclasses import dataclass, field

from .errors import InvalidMessagePayloadError
from .genres import AUDIENCE_GENRE, genre_of

#: The protocol marker a review created under B.7 carries in its config (R-1). One
#: constant, read by the server (which stamps it) and the watcher (which schedules from
#: it) — the rollout rule is not a thing to spell twice.
PROTOCOL = "B.7"

#: Notice phases carrying the review's two initialization-window settings (G-3, G-7).
GATE_MODE_PHASE = "gate_mode"
REVIEW_CONFIG_PHASE = "review_config"

#: B.9 G-3: development's incidental discovery, recorded into the channel instead of
#: silently edited. Findings stay critic-owned: the critic's next pass either adopts an
#: observation as a finding of its own or dismisses it with a reason, and the server
#: refuses a pass-ending status that leaves one unanswered (the ledger, not a courtesy).
DEV_OBSERVATION_PHASE = "dev_observation"
OBSERVATION_ACTIONS = ("adopted", "dismissed")

#: Critic-status SHAPES exempt from the observation-ledger burden — ENUMERATED (phase,
#: value) pairs, never "any phase" and never phase alone (findings
#: b9-observation-ledger-escape-paths and b9-observation-ledger-reserved-phase-still-
#: bypasses: a bare key-presence exemption let an invented phase bypass the ledger, and
#: a phase-only allowlist let the model CLAIM a reserved phase on an ordinary verdict).
#: Only administrative shapes the loop itself produces belong here — the genre-gate
#: refusal and the watcher's minimal always-valid error status, both strictly
#: `needs_human`, neither a pass-ending verdict. The summary-audit handoff
#: (`gate_handoff`) needs no entry — its pass anchors to an `intent_summary` artifact,
#: which the validator already exempts by anchor. The phases themselves are the
#: HARNESS's vocabulary: the watcher strips a model-claimed reserved phase before
#: posting (with a recorded notice). Residual, already-recorded boundary: a direct post
#: with the critic token can still fake a full shape — the server cannot tell the
#: watcher from a direct post (single-user trust boundary, option X).
LEDGER_EXEMPT_STATUS_SHAPES = frozenset(
    {("genre_gate", "needs_human"), ("watcher_error", "needs_human")}
)
#: The reserved phase names, derived — what the watcher refuses from model output.
RESERVED_STATUS_PHASES = frozenset(p for p, _ in LEDGER_EXEMPT_STATUS_SHAPES)

#: B-5, the POSITIVE half of the standing frame (finding
#: b9-standing-threat-frame-omits-positive-model): the operator-confirmed threat model
#: and operating scale travel as a validated notice — {threat_model, operating_scale,
#: granted_by}. LATEST BINDS (operator ruling, review 03982239 round 3: the frame is
#: live — boundaries already grow mid-review, and the positive half is no deader):
#: a re-statement supersedes in the critic's frame block, the append-only channel keeps
#: the history. Development posts the first record right after the pre-review gate.
THREAT_CONTEXT_PHASE = "threat_context"

#: Gate modes. `auto` — judgment entries wait, mechanical ones ride on their accepted
#: proposal; `all_wait` — every entry of every round waits (the operator's switch for
#: complex subjects: «на сложных задачах его отключать, а на простых — использовать
#: автоматику»).
GATE_MODES = ("auto", "all_wait")
DEFAULT_GATE_MODE = "auto"

#: Finding types (G-5). The first two are where the operator's judgement — and the
#: measured cost — concentrates, which is why the digest sorts by this field.
FINDING_TYPES = (
    "security_mechanism",
    "proportionality",
    "correctness",
    "completeness",
    "drift",
)
#: The two types whose entries always wait for the operator, whatever the proposal says.
JUDGMENT_TYPES = ("security_mechanism", "proportionality")

#: Proposal outcomes (W-1), a discriminated union: `fix` carries a plan, `waive` carries
#: its grounds in `reason`, `escalate` carries a recommendation and no plan.
PROPOSAL_OUTCOMES = ("fix", "waive", "escalate")
#: Gate directives (W-2). Only `accept` / `amend` SETTLE an entry; the two report
#: directives are valid answers that leave their entry pending until the report lands and
#: the operator rules.
DIRECTIVES = ("accept", "amend", "detail", "class_analysis")
SETTLING_DIRECTIVES = ("accept", "amend")
REPORT_DIRECTIVES = ("detail", "class_analysis")
#: directive -> the report kind that answers it (C-1, C-3).
REPORT_KIND_FOR = {"detail": "detail_report", "class_analysis": "class_report"}

#: Operator finals (W-3) and the disposition outcomes only the SERVER may emit (W-4).
FINALIZE_MODES = ("now", "after_fixes")
OPERATOR_RISK_ACCEPTED = "operator_risk_accepted"
FIXED_UNVERIFIED = "fixed_unverified"
SERVER_ONLY_OUTCOMES = (OPERATOR_RISK_ACCEPTED, FIXED_UNVERIFIED)
TERMINAL_OUTCOMES = ("fixed", "waived", *SERVER_ONLY_OUTCOMES)

#: Round-gate states. These are NOT review FSM states: they are the round's own position,
#: derived per message, and the legality table below is keyed on them.
IDLE = "idle"
PASS_RUNNING = "pass_running"
PROPOSALS_OWED = "proposals_owed"
PARKED = "parked"
UNPARKED = "unparked"
ROUND_CLOSING = "round_closing"
OPERATOR_FINALIZING = "operator_finalizing"
TERMINAL = "terminal"

#: W-3's legality table — the SOLE normative surface for where a development-postable
#: message kind is legal. A violation is visible by inspection of this table rather than by
#: sweeping the code, which is exactly why the spec put it in one place: an undefined
#: state x kind cell used to be a silent behaviour fork (round-2 finding
#: `final-after-fixes-conflicts-with-artifact-refusal`).
#:
#: ``IDLE`` is the state OUTSIDE any round — before the review's first version, and between
#: a closed round and the next pass. The spec's table describes the round cycle and says
#: nothing about it; reading its absence as "everything refused" would refuse the first
#: artifact of every review. Outside a round the ledger's own pre-B.7 rules stand, so the
#: gate keeps its hands off ``artifact`` and ``disposition`` here and refuses only the
#: round-gate kinds, which have no round to answer.
LEGAL_KINDS: dict[str, frozenset[str]] = {
    IDLE: frozenset({"artifact", "disposition"}),
    PASS_RUNNING: frozenset(),
    PROPOSALS_OWED: frozenset({"proposals"}),
    PARKED: frozenset({"gate_directive", "detail_report", "class_report", "operator_finalize"}),
    UNPARKED: frozenset({"artifact", "gate_directive", "operator_finalize"}),
    ROUND_CLOSING: frozenset({"operator_finalize", "disposition"}),
    OPERATOR_FINALIZING: frozenset({"artifact"}),
    # THE REVIEW IS OVER, AND ITS POST-REVIEW PHASE IS NOT ROUND TRAFFIC. W-3's table
    # refuses both of these in a terminal state, and taken literally that would make the
    # operator's final unreachable in practice: the summary is exactly what they read at
    # their gate afterwards, its audit cycle re-posts it, and that cycle disposes of its own
    # findings (measured: the previous review's audit closed two). So both cells are open
    # here — but SCOPED to the audit path in `refusal`, not to the kind: only the summary
    # artifact, and only a disposition for a finding raised against it. An unscoped opening
    # let an ordinary post-final disposition through (critic finding
    # `terminal-allows-generic-disposition`).
    TERMINAL: frozenset({"artifact", "disposition"}),
}

# B.11 E-8: `map`, `map_disposition` and `reconciliation` are DELIBERATELY ABSENT from the
# table above, and their absence is the mechanism rather than an omission. The table only
# governs `GATED_KINDS`; a kind outside that set passes through in every gate state
# untouched, which is exactly what the operator's ruling requires — «Карта ничего не
# переоткроет» — and what E-8 means by "legal in `converged` and changing no state". Adding
# them to the table would give the round gate an opinion about traffic that is not the
# round's, and giving the state machine a second exception is what E-8 declined.

#: The kinds this table governs. Everything else on the channel — notices, findings,
#: coverage, escalations, human questions, the critic's statuses — is NOT round-gate
#: traffic and must pass through untouched: the gate constrains the round's decision
#: sequence, not the channel's ability to record what happened.
GATED_KINDS = frozenset(
    {
        "artifact",
        "proposals",
        "gate_directive",
        "detail_report",
        "class_report",
        "operator_finalize",
        "disposition",
    }
)

#: EVERY message kind of the review wire contract — the channel schema in one place
#: (B.8 B-1). Lives here rather than in the repository because both sides need it and
#: this module is the one they both already import: the server validates these kinds,
#: and the watcher's compaction registry must classify every one of them
#: (`test_every_wire_kind_declares_a_compaction_policy` asserts the two sets agree).
#: ADDING A KIND? It must simultaneously enter the watcher's `VERBATIM_KINDS` or its
#: `COLLAPSE_TABLE`, or the suite fails — an uncompacted-by-default new kind is how the
#: replay overflow happens again (three overflows, one mechanism).
WIRE_KINDS = frozenset(
    {
        "artifact",
        "findings",
        "disposition",
        "escalation",
        "human_question",
        "external_defect_graph_result",
        "coverage_manifest",
        "coverage_report",
        "decision_response",
        "status",
        "notice",
        "waiver",
        "dissent",
        "proposals",
        "gate_directive",
        "detail_report",
        "class_report",
        "operator_finalize",
        # B.11 Part E/F. They ride the wire like any other kind and, like notices and
        # coverage traffic, they are not round-gate traffic (see the note under
        # LEGAL_KINDS). Registered HERE because every wire kind owes a compaction policy —
        # a kind that is uncompacted by default is how the replay overflowed three times.
        "map",
        "map_disposition",
        "reconciliation",
    }
)

#: B.8 F-1 — the DECLARED GROUNDS of every review kind: the external surfaces its seeing
#: verdicts rest on (the artifact under review itself needs no probe). One technical
#: layer, per the operator's taxonomy (2026-08-12): the seeing function exists in EVERY
#: review and differs only in what it checks against — an audience seeing pass checks
#: the artifact against reality, a spec review checks it against the code, docs and
#: graph, a code review checks the code against its spec. ``None`` means the kind names
#: NO grounds today and therefore REFUSES to run a seeing pass until it declares them —
#: fail-closed, no default is inherited (today that is the visual genre). Lives here
#: because both sides need it: the server validates a `grounds_override` against it, the
#: watcher probes it before every pass.
REVIEW_GROUNDS: dict[str, tuple[str, ...] | None] = {
    "spec": ("repo", "graph", "docs"),
    "code": ("repo", "spec-artifact"),
    "audience-seeing": ("repo", "graph"),
    "visual": None,
}

#: The operator's carrier for F-3's degraded-mode choice — validated at POST against the
#: review kind's declared grounds, binding until the review ends, recorded like a waiver.
GROUNDS_OVERRIDE_PHASE = "grounds_override"


def declared_grounds(mode: str | None, genre: str | None) -> tuple[str, ...] | None:
    """The grounds a pass over this review kind must probe (F-1). None = refuse to run.

    The genre axis wins where it exists: an audience review's seeing pass has its own
    grounds regardless of the artifact's carrier mode, and the visual genre names none.
    """
    if genre == "audience":
        return REVIEW_GROUNDS["audience-seeing"]
    if genre == "visual":
        return REVIEW_GROUNDS["visual"]
    return REVIEW_GROUNDS.get(mode or "")


#: B.8 F-2 — the grounds an evidence block may cite, and the carrier each requires.
EVIDENCE_GROUNDS = ("repo", "docs", "graph", "spec-artifact")


def evidence_shape_problems(evidence) -> list[str]:
    """Ways one F-2 `evidence` block is not well-formed typed proof of reading.

    ONE implementation for every carrier of the requirement — the coverage-report row
    and the claim-map confirmation both cite it, and two copies of a schema drift.
    Shape only: the CONTENT check (does the quote match what that read served) belongs
    to the tract that holds the run's read log.
    """

    def _filled(v) -> bool:
        return isinstance(v, str) and bool(v.strip())

    if not isinstance(evidence, dict):
        return [
            "`evidence` must be an object {ground, read_ref, quote | node_id+version | "
            "element_id}"
        ]
    problems = []
    ground = evidence.get("ground")
    if ground not in EVIDENCE_GROUNDS:
        problems.append(
            f"evidence `ground` must be one of {EVIDENCE_GROUNDS}, got {ground!r}"
        )
    if not _filled(evidence.get("read_ref")):
        problems.append(
            "evidence requires a non-empty `read_ref` naming an entry of THIS run's "
            "read log — a quote alone is reproducible from prompt material without "
            "opening anything"
        )
    if ground in ("repo", "docs") and not _filled(evidence.get("quote")):
        problems.append(
            f"evidence for ground {ground!r} requires an exact `quote` from the read's "
            "named line range"
        )
    if ground == "graph" and not (
        _filled(evidence.get("node_id")) and evidence.get("version") is not None
    ):
        problems.append(
            "evidence for ground 'graph' requires `node_id` and `version` of the node "
            "the read served"
        )
    if ground == "spec-artifact" and not _filled(evidence.get("element_id")):
        problems.append(
            "evidence for ground 'spec-artifact' requires the `element_id` the code was "
            "checked against"
        )
    return problems


def read_log_entry_problems(entry, ground) -> list[str]:
    """Ways one run-scoped read-log ENTRY is not a well-formed record of a read (F-2).

    ``evidence_shape_problems`` validates the evidence block; this validates the entry
    its ``read_ref`` points at — critic-authored text that was previously trusted as-is
    (finding b8-evidence-read-log-unbound): a fabricated entry passed for a read, and a
    malformed line range silently widened the repo quote check from the named range to
    the whole file. Lives here beside the evidence schema because it is the same
    requirement's other half, one home for every carrier. Fail-closed: a verdict citing
    a malformed entry is downgraded by the caller; nothing is invented.
    """

    def _filled(v) -> bool:
        return isinstance(v, str) and bool(v.strip())

    if not isinstance(entry, dict):
        return ["the referenced read-log entry is not an object"]
    problems = []
    if not _filled(entry.get("locator")):
        problems.append("the referenced read-log entry names no locator — what was read?")
    version = entry.get("version")
    if version is None or (isinstance(version, str) and not version.strip()):
        problems.append("the referenced read-log entry names no version — read as of what?")
    if ground in ("repo", "docs"):
        rng = entry.get("range")
        valid = (
            isinstance(rng, (list, tuple))
            and len(rng) == 2
            and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in rng)
            and rng[0] <= rng[1]
        )
        if not valid:
            problems.append(
                f"a {ground} read-log entry requires `range` = [start, end], two positive "
                "integers, start <= end — without it the quote check would silently widen "
                "from the named range to the whole file"
            )
    return problems


@dataclass
class Entry:
    """One finding's position inside its round.

    ``settled_outcome`` is the SANCTIONED action (``fix`` / ``waive``) once the operator's
    directive settles the entry — it is what F-3's after-fixes bookkeeping keys on, and
    the reason an ``escalate`` proposal carries a typed ``recommended_outcome``: an
    accepted recommendation has to name an action, or "the operator accepted it" would not
    say what was accepted.
    """

    finding_id: str
    finding_type: str | None = None
    proposed_outcome: str | None = None
    proposed_reason: str | None = None
    recommended_outcome: str | None = None  # escalate proposals only
    forced_judgment: bool = False  # an open server contest over this finding
    #: B.14 D-3: this finding contests a declared temporary state (`contests_declaration`
    #: on the item, validated at POST against the frozen register). The dissent route
    #: stays open, and it is always the operator's to rule on.
    contests_declaration: bool = False
    #: B.9 B-5: the SERVER stamped this waive's `boundary_ref` as resolving to a recorded
    #: threat boundary eligible for this review. Set only from the stored payload's
    #: server-minted `boundary_ref_resolved` marker, never from a client claim.
    boundary_mechanical: bool = False
    directive: str | None = None  # the last directive verb seen
    settled_outcome: str | None = None
    settled_reason: str | None = None  # the operator's / the proposal's recorded grounds
    report_pending: str | None = None  # the outstanding report directive, if any
    needs_ruling: bool = False  # a report directive reopened this entry (see _apply_directive)
    disposed_outcome: str | None = None

    @property
    def settled(self) -> bool:
        return self.settled_outcome is not None

    def judgment(self, mode: str) -> bool:
        """Is this a JUDGMENT entry — one where the operator's ruling is the point?

        `security_mechanism` / `proportionality` by type; any proposal to waive; any
        escalate; any finding under a server-raised contest. In `all_wait` every entry of
        the round is one, by the operator's own switch.

        B.9 B-5 carves the one mechanical route out of the waive class: a waive whose
        `boundary_ref` the SERVER validated against the threat-boundary registry (scope-
        eligible for this review) rides on its accepted proposal — the boundary is already
        the operator's recorded answer, so re-asking is the measured waste the registry
        exists to remove. The route never outranks the operator's own switches: `all_wait`
        and an open server contest still wait, and a waive proposing a boundary NOT yet
        recorded (no resolving ref) waits exactly as before — a new boundary is genuinely
        the operator's to grant. The gate validates resolution, not aptness (operator
        ruling, spec round 9): whether the boundary covers the waived threat is the
        critic's existing contest right, which lands as `forced_judgment` here.
        """
        if mode == "all_wait":
            return True
        if self.forced_judgment:
            return True
        # B.14 D-3: a contest of a declared temporary state is a judgment entry — the
        # declaration was the operator's settlement, so only the operator unsettles it.
        if self.contests_declaration:
            return True
        if self.proposed_outcome == "waive" and self.boundary_mechanical:
            return False
        return (
            self.finding_type in JUDGMENT_TYPES
            or self.proposed_outcome in ("waive", "escalate")
        )


@dataclass
class Round:
    """One round: the findings of a critic pass, their proposals, and the operator's marks."""

    findings_seq: int
    artifact_seq: int | None
    entries: dict[str, Entry] = field(default_factory=dict)
    proposals_seq: int | None = None
    version_seq: int | None = None  # the artifact accepted after unparking (round_closing)
    state: str = PROPOSALS_OWED

    def waiting(self, mode: str) -> list[str]:
        """Entries that still owe an operator ruling — the gate's worklist.

        Two sources. The waiting CLASS (judgment entries, per the gate mode) — and any entry
        the operator REOPENED with a report directive, whatever its class: asking for a
        report is not a ruling, and the entry stays theirs until they rule (W-2). Without the
        second source a `detail` override of an auto-sanctioned mechanical entry unparked the
        round the moment the report landed, with no `accept`/`amend` ever given (critic
        finding `mechanical-override-skips-reruling`).
        """
        return [
            fid
            for fid, e in self.entries.items()
            if (e.judgment(mode) or e.needs_ruling) and not e.settled
        ]

    def outstanding_reports(self) -> list[str]:
        """Entries with a report request the report has not answered yet (G-3, W-2)."""
        return [fid for fid, e in self.entries.items() if e.report_pending]

    def should_park(self, mode: str) -> bool:
        """THE parking predicate, stated once (G-3).

        Park while any waiting-class entry lacks a settled directive OR any report request
        is outstanding — and unpark only when neither holds. The re-park a `detail` /
        `class_analysis` override causes in the unparked state (W-2) is a CONSEQUENCE of
        this predicate, not an exception to it.
        """
        return bool(self.waiting(mode) or self.outstanding_reports())

    def owed_dispositions(self) -> list[str]:
        """Findings of this round whose terminal disposition is still owed (H-1's barrier).

        Every finding of the round owes one: a sanctioned fix closes as `fixed`, a
        sanctioned waive as `waived`. The barrier exists because artifact-before-
        disposition ordering would otherwise make a version eligible for the next critic
        pass while its round's ledger is formally open (round-4 finding
        `artifact-advance-before-round-closure`).
        """
        return [fid for fid, e in self.entries.items() if e.disposed_outcome is None]


@dataclass
class GateState:
    """The round gate as the channel currently stands."""

    mode: str = DEFAULT_GATE_MODE
    state: str = IDLE
    round: Round | None = None
    finalize_mode: str | None = None  # "now" / "after_fixes" once the operator stopped
    initialized: bool = False  # a first artifact version exists (the config window closed)
    config: dict = field(default_factory=dict)  # resolved review_config (G-7)
    #: Findings raised against a post-review summary — the faithfulness audit's own. They
    #: are the ONE ledger still open once a review is terminal, and the only dispositions
    #: its channel accepts there.
    audit_findings: set = field(default_factory=set)

    @property
    def gate_open(self) -> bool:
        return self.state == PARKED


# --- resolved per-review settings (G-7) ----------------------------------

#: Defaults for every `review_config` field. Absence of the notice, or of any single
#: field, falls to these — silence falls to the default, never to an error. The ping
#: roles are the operator's round-1 choice; the 5 minutes and the 15-minute stall
#: threshold are the operator's own numbers (round-1 and round-9 gates, 2026-08-08).
DEFAULT_REVIEW_CONFIG: dict = {
    "ping": {"primary": "push", "fallback": "prod_bot", "fallback_delay_s": 300},
    "fault_stretch": {"attempts": 3},
    "stall": {"bootstrap_threshold_s": 900},
}


def resolved_config(messages: list[dict]) -> dict:
    """The review's resolved ping / fault-stretch / stall settings (G-7).

    ONE notice carries all three, posted in the initialization window; every consumer —
    the watcher, both poll clients, and the supervisor's generated launcher — reads THIS
    function's answer, so the supervisor and the live watcher notify identical channels by
    construction rather than by two hands agreeing.
    """
    out = {section: dict(values) for section, values in DEFAULT_REVIEW_CONFIG.items()}
    notice = _first_config_notice(messages, REVIEW_CONFIG_PHASE)
    if notice is None:
        return out
    for section in out:
        supplied = notice.get(section)
        if isinstance(supplied, dict):
            out[section].update({k: v for k, v in supplied.items() if v is not None})
    return out


def _first_config_notice(messages: list[dict], phase: str) -> dict | None:
    """The FIRST validated notice of this phase — inside the initialization window it binds
    for the review's lifetime, and anything later was already refused at POST."""
    for m in messages:
        if m.get("kind") == "notice" and (m.get("payload") or {}).get("phase") == phase:
            return m.get("payload") or {}
    return None


def in_force(config: dict | None) -> bool:
    """Does the B.7 round gate govern a review with this config? THE ONE predicate.

    Read by the server (which refuses traffic on it) and by the watcher (which schedules
    passes on it). It lives here because the two had a copy each for about an hour, and
    they disagreed within it: the watcher's copy kept only the protocol check, so an
    AUDIENCE review — stamped B.7 by default at creation, but excluded from the gate by the
    server on its genre — had its untyped findings folded into `proposals_owed` by the
    watcher, which then waited forever for proposals the server never wanted (critic finding
    `watcher-gates-audience-reviews`). That is the same two-copies-of-one-fact defect this
    module exists to prevent, committed while writing the module that prevents it.

    Two conditions, both necessary: the review was stamped with the protocol at creation
    (R-1 — an older review keeps the old round structure for its whole life), and it is not
    an audience review (S-2 — that genre has its own gates, and a second one over them was
    never designed).
    """
    config = config or {}
    if genre_of(config) == AUDIENCE_GENRE:
        return False
    return config.get("protocol") == PROTOCOL


def gate_mode(messages: list[dict]) -> str:
    """The review's gate mode (G-3). Absence of the notice means `auto` — the default."""
    notice = _first_config_notice(messages, GATE_MODE_PHASE)
    mode = (notice or {}).get("mode")
    return mode if mode in GATE_MODES else DEFAULT_GATE_MODE


def initialization_window_open(messages: list[dict]) -> bool:
    """Is the review still before its first artifact version?

    Both per-review settings are accepted only here. The window is the honest boundary: a
    setting that could change mid-review would let the party the gate constrains widen it
    after seeing what the gate caught.
    """
    return not any(m.get("kind") == "artifact" for m in messages)


# --- what still awaits the operator --------------------------------------


def open_operator_item_keys(messages: list[dict]) -> list[str]:
    """Escalations + human_questions still awaiting the operator — ORDER-AWARE.

    THE ONE IMPLEMENTATION, and this time actually one: the server's DB-side authority
    (`repository._open_operator_items`) delegates here over the same plain-dict shape the
    watcher replays. The first cut of this module kept a hand copy instead, and the two
    disagreed in exactly the two places the DB side had paid to learn: a keyless
    human_question became an unclosable key (the DB pools it, closed by ANY later operator
    answer), and one operator answer settled only ONE of several escalations sharing a key
    (the DB settles the whole fork — feedback `e9cf8a67`, the operator relaying one
    identical ruling twice). Either divergence leaves the server saying "unparked" while
    the watcher waits on a human forever — and the stall alarm holds its tongue precisely
    because the silence looks human-owed. The two-copies defect, committed inside the
    module whose docstring forbids it.

    Semantics (the DB side's, verbatim): an ``escalation`` opens its element/finding key
    (keyless → a unique unclosable key — fail-safe, an ill-formed contest can only hold a
    review open); a ``human_question`` opens its ``id`` (a keyless question joins a pool
    closed by ANY later operator answer — a question is milder than a contest, so its
    fail-safe is softer); ``needs_human`` joins the same keyless pool; a
    ``decision_response`` closes only what is ALREADY OPEN at that point (never a future
    contest), decrements question / external-defect keys by one, and settles EVERY open
    escalation under an element/finding key at once — the unit of the operator decision is
    the fork, not the escalation message.
    """
    open_count: dict[str, int] = {}
    keyless_waits: list[str] = []  # keyless questions + needs_human — closed by ANY answer
    for m in messages:
        payload = m.get("payload") or {}
        kind = m.get("kind")
        seq = m.get("seq")
        if kind == "escalation":
            # The KEYING is `operator_item_key`'s, not a second copy of it (B.12 H-6): the
            # audit that joins each item to its projection has to mean the same key this
            # fold means, or it measures its own spelling.
            key = operator_item_key(kind, payload, seq)
            open_count[key] = open_count.get(key, 0) + 1
        elif kind == "human_question":
            key = operator_item_key(kind, payload, seq)
            if payload.get("id") is not None:
                open_count[key] = open_count.get(key, 0) + 1
            else:
                keyless_waits.append(key)
        elif kind == "status" and payload.get("value") == "needs_human":
            keyless_waits.append(operator_item_key(kind, payload, seq))
        elif kind == "decision_response":
            qid = payload.get("question_id")
            if qid is not None:
                key = f"q:{qid}"
                if open_count.get(key, 0) > 0:
                    open_count[key] -= 1
            elif payload.get("escalation_id") is not None:
                key = f"ext:{payload['escalation_id']}"
                if open_count.get(key, 0) > 0:
                    open_count[key] -= 1
            else:
                if payload.get("element_id") is not None:
                    key = f"element:{payload['element_id']}"
                elif payload.get("finding_id") is not None:
                    key = f"finding:{payload['finding_id']}"
                else:
                    key = None
                # One answer settles EVERY escalation open under the key at this point —
                # still order-aware: it closes only what is already open.
                if key is not None and open_count.get(key, 0) > 0:
                    open_count[key] = 0
            # any operator answer settles the keyless waits (questions + needs_human)
            keyless_waits.clear()
    return [k for k, n in open_count.items() for _ in range(n)] + keyless_waits


# --- B.12 H-6: every operator item joined to its projection ---------------------------


def operator_item_key(kind: str, payload: dict, seq) -> str | None:
    """The ONE key of one operator item, or None if this message raises no item.

    THE ONE IMPLEMENTATION of the keying, extracted here because it now has two consumers
    with different questions. `open_operator_item_keys` asks "what is still OPEN" and does
    open/close accounting; `projection_audit` asks "what was ever RAISED" and joins each to
    its projection. Two hand-rolled copies of the namespacing would eventually disagree
    about which item a projection explains, which is precisely the failure the audit
    exists to catch — the audit would then be measuring its own copy.

    The namespacing is the server's, unchanged: an escalation on its element or its
    finding, an external defect on its own id, a human question on its question id, and
    the RAISING MESSAGE'S SEQUENCE NUMBER for an item that carries no key of its own. That
    fallback is what makes the field total: every item has a key, because an item with no
    key of its own has its seq.
    """
    if kind == "escalation":
        if payload.get("kind") == "external_defect":
            return f"ext:{payload.get('id') or f'seq{seq}'}"
        if payload.get("element_id") is not None:
            return f"element:{payload['element_id']}"
        if payload.get("finding_id") is not None:
            return f"finding:{payload['finding_id']}"
        return f"seq{seq}"
    if kind == "human_question":
        qid = payload.get("id")
        return f"q:{qid}" if qid is not None else f"question-seq{seq}"
    if kind == "status" and payload.get("value") == "needs_human":
        return f"needs_human-seq{seq}"
    if kind == "proposals":
        # THE ROUND GATE IS AN OPERATOR ITEM and it is on this list deliberately. It has no
        # key of its own — nothing about a proposals message identifies "the gate it
        # opened" — so it takes the fallback, its own seq. It is also the reason the audit
        # may NOT be written as "every key in `open_operator_item_keys`": the gate is not
        # in that fold at all, so keying the audit off it would refuse exactly the
        # projection the operator most needs.
        return f"seq{seq}"
    return None


#: The four translated parts of a projection (C-1), mirrored from the server's validator so
#: the audit can see an incomplete record on a channel the validator did not screen.
PROJECTION_PARTS = ("what_happened", "concerns", "proposal", "answers")


def projection_audit(messages: list[dict]) -> dict:
    """Join every operator item that EXISTS AS A MESSAGE to its projection (B.12 H-6).

    Returns ``{"items": [...], "failures": [...], "clean": bool}``. Each item carries its
    seq, the message kind that raised it, its key, and the seqs of the projections found
    for that key.

    FOUR FAILURES, named rather than counted: an item with **no** projection; a projection
    **missing either half** of the pair (the verbatim original, or any of the four
    translated parts); a projection that names **no raising** or a raising that put nothing
    to the operator; and a projection whose named raising has a **different key** than the
    one it declares.

    WHAT THE SERVER ACTUALLY REFUSES AT POST, stated exactly (finding
    `b12-projection-reference-post-claim-drift`, sol round 1): the SHAPE of the record — a
    positive integer `item_seq`, a non-empty `item_key`, a non-empty original and four
    non-empty parts. It does NOT resolve the reference against the journal. RESOLVING IT IS
    THIS AUDIT'S JOB AND NOTHING ELSE'S, so the last two failures above are reachable on any
    channel and are not a re-ask of something already refused. An earlier revision of this
    docstring claimed the server refused them too; it was written in the same commit that
    added the field, it was never true, and a claim stronger than its mechanism is how a
    later reader concludes something is guaranteed and stops checking.

    THE JOIN IS ON THE RAISING'S SEQ, not on the key alone, since round 2 of this slice's
    own implementation review. The key is shared with the settle-per-key fold by design, so
    two distinct raisings about one finding carry one key; joining on it credited a single
    projection to both and reported the pair as complete.

    A THIRD FAILURE WAS HERE AND IS WITHDRAWN — "an item with two projections" — by the
    operator's amendment of 2026-08-27, made while reading a second projection of the very
    item this audit was auditing: «я могу его запросить как сейчас, когда перевод меня не
    устроил с 1 раза. И ты мне только что дал второй перевод. Так что переводов может быть
    более одного». The rule had read a second record as "the operator was shown two accounts
    of one question". It is the opposite: a second translation appears exactly when the first
    did not land, and the operator asking for it is the loop working, not a defect in it. All
    projections of an item are listed and the LAST one is the account that stood — the same
    latest-binds reading every other operator-owned record on this channel already has. Do
    not restore this rule from the shape of the spec text: the spec element that says "exactly
    one projection record" is superseded by the same amendment, recorded there.

    WHY THIS IS CHECKED AT ALL, given that the server deliberately does not hold a parked
    item until a projection exists (it has no channel to the operator, so such a hold would
    guarantee a *message* and never an *explanation*): a decision not to check on one side
    is only defensible if something checks on the other. Otherwise "the server deliberately
    does not check" and "nobody checks" are the same sentence.

    WHAT IS DELIBERATELY NOT ENUMERATED: naming the operator the two exits from a review.
    That is a conversational duty of development — it raises no message, has no key, and
    cannot be joined to anything. An enumeration that includes a member with no key is a
    check that always fails, and a check that always fails is turned off.

    It does NOT assert an order against the notification: the notification is a chat message
    and the channel does not see chat, so one of the two events being compared has no
    record. The pair — original beside translation, in one record — is what makes the
    ordering unnecessary rather than unverifiable. Faithfulness is not checked either, and
    that is a recorded trust boundary rather than an omission: what is mechanical is that a
    projection exists, carries both halves, and has no empty part.
    """
    # JOINED ON THE RAISING MESSAGE'S SEQ, NOT ON THE KEY ALONE (finding
    # `b12-projection-key-aliases-distinct-items`). The key is shared with the settle-per-key
    # fold, so two distinct raisings about one finding carry one key; joining on it credited
    # a single projection to both, and the tail then reported every item as carrying its
    # verbatim original while that original belonged to only one of them. The seq is what
    # attributes; the key still travels, and is checked against the seq below, because a
    # record that names one message and claims another's key is telling two stories.
    by_item_seq: dict[int, list[dict]] = {}
    mismatches: list[str] = []
    key_of_seq = {
        m.get("seq"): operator_item_key(
            str(m.get("kind")), m.get("payload") or {}, m.get("seq")
        )
        for m in messages
    }
    for m in messages:
        if m.get("kind") != "operator_projection":
            continue
        payload = m.get("payload") or {}
        item_seq = payload.get("item_seq")
        key = payload.get("item_key")
        if type(item_seq) is not int:
            # Pre-amendment records carry no seq. They are not silently dropped and not
            # silently credited either: the projection exists, so it is reported as
            # unattributable rather than counted for an item it may not have explained.
            mismatches.append(
                f"projection at seq {m.get('seq')} names no `item_seq` — it cannot be "
                "attributed to a raising, so it counts for none of them"
            )
            continue
        expected = key_of_seq.get(item_seq)
        if expected is None:
            mismatches.append(
                f"projection at seq {m.get('seq')} names `item_seq` {item_seq}, which is "
                "not a message that put anything to the operator"
            )
            continue
        if key is not None and str(key) != expected:
            mismatches.append(
                f"projection at seq {m.get('seq')} names `item_seq` {item_seq} (key "
                f"`{expected}`) but declares `item_key` `{key}` — one record, two stories"
            )
            continue
        by_item_seq.setdefault(item_seq, []).append(
            {"seq": m.get("seq"), "payload": payload}
        )

    items: list[dict] = []
    failures: list[str] = list(mismatches)
    for m in messages:
        payload = m.get("payload") or {}
        key = operator_item_key(str(m.get("kind")), payload, m.get("seq"))
        if key is None:
            continue
        found = by_item_seq.get(m.get("seq"), [])
        items.append(
            {
                "seq": m.get("seq"),
                "kind": m.get("kind"),
                "key": key,
                "projection_seqs": [p["seq"] for p in found],
                # Named rather than left to be inferred from the list's order: with more
                # than one projection legal, "which account stood" is the thing a reader
                # actually wants, and a reader who has to know that the list is ordered is
                # a reader who will one day read it when it is not.
                "account_that_stood": found[-1]["seq"] if found else None,
            }
        )
        if not found:
            failures.append(
                f"operator item `{key}` (raised at seq {m.get('seq')} as "
                f"{m.get('kind')}) has NO projection"
            )
        for p in found:
            missing = [
                part
                for part in ("original", *PROJECTION_PARTS)
                if not str((p["payload"].get(part) or "")).strip()
            ]
            if missing:
                failures.append(
                    f"projection at seq {p['seq']} for `{key}` is missing {missing} — a "
                    "projection carries the machine item verbatim AND all four translated "
                    "parts; an empty part is the failure this record exists to prevent"
                )
    return {"items": items, "failures": failures, "clean": not failures}


def open_operator_items(messages: list[dict]) -> bool:
    """Order-aware check: does anything still await the operator? (Bool view of
    `open_operator_item_keys` — the watcher and the fold ask yes/no; the server's
    convergence guard reads the keys.)"""
    return bool(open_operator_item_keys(messages))


# --- the fold ------------------------------------------------------------


def _open_contested_findings(messages: list[dict], upto_seq: int) -> set[str]:
    """Finding ids under a contest still awaiting the operator at ``upto_seq``.

    A contested finding is a judgment entry however its proposal reads (G-3): the server
    itself raises `contested_fork` on an oscillation, and a finding the operator is already
    being asked about must not ride through on a mechanical proposal.
    """
    open_ids: set[str] = set()
    for m in messages:
        if m.get("seq", 0) > upto_seq:
            break
        payload = m.get("payload") or {}
        fid = payload.get("finding_id")
        if not fid:
            continue
        if m.get("kind") == "escalation":
            open_ids.add(fid)
        elif m.get("kind") == "decision_response":
            open_ids.discard(fid)
    return open_ids


def summary_artifact_seqs(messages: list[dict]) -> set[int]:
    """Seqs of post-review intent-summary artifacts.

    THE ROUND GATE DOES NOT GOVERN THE FAITHFULNESS-AUDIT CYCLE (Q-3, F-4): audit passes
    over the post-review summary are not defect rounds, and that cycle runs fully
    automatically — no round gate inside it, the existing clean-pass terminator unchanged.
    A finding raised against a summary version therefore opens no round, and a summary
    artifact is never round traffic.

    Public because the fold and the watcher's scheduler are BOTH consumers: the fold uses
    it to keep summaries out of the round cycle, and the scheduler uses it to let the
    audit pass run in the terminal state (F-4's "the audit cycle runs fully automatically"
    holds after an operator final too — the state the first cut forgot, leaving every
    operator-finalized review's audit silently unlaunched).
    """
    return {
        m["seq"]
        for m in messages
        if m.get("kind") == "artifact" and (m.get("payload") or {}).get("intent_summary")
    }


def compute(messages: list[dict]) -> GateState:
    """Fold the channel into the current round-gate state.

    The fold is deliberately literal — one branch per message kind, in seq order — because
    the alternative (each consumer re-deriving "is the gate open" from whichever field is
    in front of it) is the exact shape that gave the coverage ledger three disagreeing
    definitions of "a pass ended".
    """
    state = GateState(mode=gate_mode(messages), config=resolved_config(messages))
    current: Round | None = None
    summary_seqs = summary_artifact_seqs(messages)

    for m in messages:
        kind = m.get("kind")
        role = m.get("role")
        payload = m.get("payload") or {}
        seq = m.get("seq") or 0

        if kind == "artifact":
            state.initialized = True
            if state.state == OPERATOR_FINALIZING:
                # W-3: the ONE artifact `operator_finalizing` accepts is the final version;
                # its arrival closes the review (the server does the bookkeeping).
                state.state = TERMINAL
                current = None
                continue
            if current is not None and current.state == UNPARKED:
                current.version_seq = seq
                current.state = ROUND_CLOSING
                state.state = ROUND_CLOSING
            elif current is None and seq not in summary_seqs:
                # A new version with no round open: the critic's pass over it is what comes
                # next, and until its findings land the round gate holds development still
                # (the table's "critic pass running" row).
                state.state = PASS_RUNNING
            continue

        if kind == "findings" and role == "critic":
            items = [f for f in (payload.get("items") or []) if isinstance(f, dict)]
            if payload.get("artifact_seq") in summary_seqs:
                # Audit cycle — not a defect round (Q-3). Its findings open no round, but
                # they DO owe dispositions, and those stay legal after the review closes.
                state.audit_findings.update(
                    str(f.get("id")) for f in items if f.get("id")
                )
                continue
            if not items:
                # A pass that raised nothing creates no round: the version is answered and
                # the review is back outside the round cycle.
                if current is None:
                    state.state = IDLE
                # A pass that raised nothing creates NO round gate (G-1): no proposals are
                # owed, no parking occurs, and the pass routes into the existing
                # convergence machinery untouched.
                continue
            contested = _open_contested_findings(messages, seq)
            current = Round(
                findings_seq=seq,
                artifact_seq=payload.get("artifact_seq"),
                entries={
                    str(f.get("id")): Entry(
                        finding_id=str(f.get("id")),
                        finding_type=f.get("finding_type"),
                        forced_judgment=str(f.get("id")) in contested,
                        contests_declaration=bool(f.get("contests_declaration")),
                    )
                    for f in items
                    if f.get("id")
                },
            )
            state.round = current
            state.state = PROPOSALS_OWED
            continue

        if kind == "proposals" and current is not None:
            current.proposals_seq = seq
            for entry in payload.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                e = current.entries.get(str(entry.get("finding_id")))
                if e is None:
                    continue
                e.proposed_outcome = entry.get("proposed_outcome")
                e.recommended_outcome = entry.get("recommended_outcome")
                e.proposed_reason = entry.get("reason")
                # B.9 B-5: the marker is server-minted at append time (a client-supplied
                # one is refused by validate_proposals), so replaying it from the stored
                # payload is replaying the server's own resolution verdict.
                e.boundary_mechanical = (
                    entry.get("proposed_outcome") == "waive"
                    and entry.get("boundary_ref_resolved") is True
                )
            # A round with no waiting-class entry never parks: the digest is still
            # rendered (visibility is unconditional) but development proceeds, and the
            # accepted proposal itself is the mechanical entries' sanction.
            for e in current.entries.values():
                if not e.judgment(state.mode):
                    e.settled_outcome = _sanctioned_outcome(e)
                    e.settled_reason = e.proposed_reason
            current.state = PARKED if current.should_park(state.mode) else UNPARKED
            state.state = current.state
            continue

        if kind == "gate_directive" and current is not None:
            e = current.entries.get(str(payload.get("finding_id")))
            if e is not None:
                _apply_directive(e, payload)
                current.state = PARKED if current.should_park(state.mode) else UNPARKED
                state.state = current.state
            continue

        if kind in ("detail_report", "class_report") and current is not None:
            ref = str(payload.get("directive_ref"))
            e = current.entries.get(ref)
            if e is not None and e.report_pending == _directive_for_report(kind):
                # The report closes its REQUEST; the entry stays pending until the
                # operator rules on it (W-2: settlement is only accept / amend).
                e.report_pending = None
                current.state = PARKED if current.should_park(state.mode) else UNPARKED
                state.state = current.state
            continue

        if kind == "disposition" and current is not None:
            e = current.entries.get(str(payload.get("finding_id")))
            if e is not None:
                e.disposed_outcome = payload.get("outcome")
            if current.state == ROUND_CLOSING and not current.owed_dispositions():
                # The round is complete: the ledger is settled and the next pass may run.
                current.state = "closed"
                state.round = None
                state.state = PASS_RUNNING
                current = None
            continue

        if kind == "operator_finalize":
            mode = payload.get("mode")
            state.finalize_mode = mode
            if mode == "after_fixes" and state.state in (PARKED, UNPARKED):
                state.state = OPERATOR_FINALIZING
            else:
                # `now` from anywhere, and `after_fixes` issued in `round_closing` (where
                # the round's version already IS the final): no further artifact is
                # expected, the server closes the ledger and the review is over.
                state.state = TERMINAL
                state.round = None
                current = None
            continue

    # THE ONE ESCAPE FROM `pass_running`. That state refuses a new artifact version, which
    # is right while the critic is genuinely working — and a deadlock the moment the review
    # is waiting on the OPERATOR instead: a returned review, an answered escalation and a
    # `needs_human` stop all need a new version, and the pass that would have cleared the
    # state is never coming. A review parked on a human is not a review with a pass
    # running, so the gate stands aside (W-5's posture: a refusal may never strand
    # completed work or the loop).
    if state.state == PASS_RUNNING and open_operator_items(messages):
        state.state = IDLE
    return state


def _sanctioned_outcome(entry: Entry) -> str | None:
    """The action an ACCEPTED proposal sanctions — `fix` or `waive`.

    An `escalate` proposal's accepted action is its `recommended_outcome`: the operator
    accepting "escalate" is accepting the recommendation, and a recommendation that does
    not name an action would leave F-3's after-fixes bookkeeping with a settled directive
    it cannot classify. (Implementation decision beyond the spec's wire text, made here
    rather than left to a later surprise: W-1 requires the recommendation, and typing its
    outcome is what makes the accept path total.)
    """
    if entry.proposed_outcome in ("fix", "waive"):
        return entry.proposed_outcome
    if entry.proposed_outcome == "escalate":
        return entry.recommended_outcome
    return None


def _apply_directive(entry: Entry, payload: dict) -> None:
    """Apply one relayed operator directive to its entry (W-2)."""
    directive = payload.get("directive")
    entry.directive = directive
    if directive == "accept":
        entry.settled_outcome = _sanctioned_outcome(entry)
        entry.settled_reason = entry.proposed_reason
        entry.report_pending = None  # a later accept SUPERSEDES an outstanding request
    elif directive == "amend":
        amendment = payload.get("amendment") or {}
        entry.settled_outcome = amendment.get("outcome")
        entry.settled_reason = amendment.get("reason") or payload.get("operator_quote")
        entry.report_pending = None
    elif directive in REPORT_DIRECTIVES:
        # A REPORT REQUEST UNSETTLES THE ENTRY. The report answers the request; the OPERATOR
        # answers the entry. Any sanction the entry was carrying — an auto-sanctioned
        # mechanical proposal, or an earlier ruling the operator is now reconsidering — is
        # void until they rule again, and `needs_ruling` is what keeps the round parked in
        # the meantime even for an entry whose class would otherwise ride.
        entry.report_pending = directive
        entry.needs_ruling = True
        entry.settled_outcome = None
        entry.settled_reason = None


def _directive_for_report(kind: str) -> str:
    return "detail" if kind == "detail_report" else "class_analysis"


# --- the legality question ------------------------------------------------


def refusal(state: GateState, kind: str, payload: dict, role: str) -> str | None:
    """Why this message is not legal here, or None.

    Refusals are per-message and actionable (W-5): none of them may invalidate a completed
    critic pass, poison the channel, or consume an operator gate. Completed work destroyed
    by a one-character refusal is the most expensive recorded failure class of this
    transport, and B.7 must not add members to it.
    """
    if kind not in GATED_KINDS:
        return None  # not round-gate traffic — the gate constrains decisions, not records
    # The server's own bookkeeping dispositions (F-2/F-3) are emitted inside a transition
    # and are not subject to the table they implement.
    if role == "system":
        return None
    # The terminal row's artifact cell is the SUMMARY EXCEPTION, not an open door: an
    # ordinary artifact after an operator final would reopen a review the operator closed,
    # and the table permits only the post-review summary there (critic finding
    # `terminal-accepts-nonsummary-artifact`).
    if state.state == TERMINAL and kind == "artifact" and not payload.get("intent_summary"):
        return (
            "this review is over (the operator finalized it): the only artifact its channel "
            "still accepts is the post-review intent summary — an ordinary version would "
            "reopen a review that was deliberately closed"
        )
    # F-3'S FINAL VERSION CARRIES ITS ATTESTATION, OR THE LABEL IT EARNS IS A LIE. After
    # this artifact the server closes the sanctioned findings as `fixed_unverified` — fixed,
    # with the critic's verifying pass skipped by operator decision. That label is only
    # honest if the work behind it was self-audited and its tests were green, which is
    # exactly what F-3 asks the final version to attest. Accepting it unattested made the
    # strongest bookkeeping label in the system rest on nothing (critic finding
    # `operator-final-artifact-attestation-unenforced`).
    if state.state == OPERATOR_FINALIZING and kind == "artifact":
        missing = [
            f for f in ("self_audit", "tests_green")
            if not (payload.get(f) if f != "tests_green" else payload.get(f) is True)
        ]
        if missing:
            return (
                f"the after-fixes final version must carry {missing} — the server closes "
                "this round's sanctioned findings as `fixed_unverified` on its arrival, and "
                "that label says the critic's verifying pass was SKIPPED, not that there was "
                "nothing to verify. `tests_green` must be literally true"
            )
    if (
        state.state == TERMINAL
        and kind == "disposition"
        and str(payload.get("finding_id")) not in state.audit_findings
    ):
        return (
            "this review is over: the only dispositions its channel still accepts are the "
            "faithfulness audit's own — findings raised against the post-review summary. "
            f"{payload.get('finding_id')!r} was not one of them, and the review's ledger "
            "was closed when the operator finalized it"
        )
    legal = LEGAL_KINDS.get(state.state, frozenset())
    if kind in legal:
        # A DISPOSITION MUST MATCH THE SANCTION IT CLOSES. Inside a round the operator's
        # ruling is what licenses the work, so a `waived` posted over a sanctioned fix (or a
        # `fixed` over a sanctioned waive) would quietly substitute development's judgement
        # for theirs at the last step — after the gate, where nobody is looking any more
        # (critic finding `disposition-outcome-not-bound-to-sanction`). Outside a round the
        # ledger's own rules stand and this says nothing.
        if kind == "disposition" and state.state == ROUND_CLOSING and state.round is not None:
            entry = state.round.entries.get(str(payload.get("finding_id")))
            expected = {"fix": "fixed", "waive": "waived"}.get(
                entry.settled_outcome if entry else None
            )
            if expected is not None and payload.get("outcome") != expected:
                return (
                    f"finding {payload.get('finding_id')!r} was settled as "
                    f"{entry.settled_outcome!r} at the round gate, so its disposition must "
                    f"be {expected!r}, not {payload.get('outcome')!r}. To close it "
                    "differently, the operator has to say so — the sanction is theirs"
                )
        return None
    return (
        f"a {kind!r} message is not legal while the round gate is in {state.state!r} "
        f"(legal here: {sorted(legal) or 'nothing — the round is between stages'}). "
        + _hint(state, kind)
    )


def _hint(state: GateState, kind: str) -> str:
    """One sentence saying what IS owed — a refusal that does not say what to do next is
    how a loop goes quiet."""
    if state.state == PASS_RUNNING:
        return "No round is open: the next critic pass is what moves the review."
    if state.state == PROPOSALS_OWED:
        return (
            "The round owes its `proposals` message — one entry per finding of the pass, "
            "with nothing implemented yet (G-1)."
        )
    if state.state == PARKED:
        rnd = state.round
        waiting = rnd.waiting(state.mode) if rnd else []
        reports = rnd.outstanding_reports() if rnd else []
        return (
            "The gate is open: the operator's directives are owed for "
            f"{waiting or 'nothing'}"
            + (f", and reports are owed for {reports}" if reports else "")
            + ". Nothing may be implemented before they arrive."
        )
    if state.state == UNPARKED:
        return (
            "Directives are complete: post the next artifact version (its acceptance opens "
            "`round_closing`), then the owed terminal dispositions."
        )
    if state.state == ROUND_CLOSING:
        rnd = state.round
        return (
            "The round's version is accepted; what is owed are its terminal dispositions "
            f"for {rnd.owed_dispositions() if rnd else []}."
        )
    if state.state == OPERATOR_FINALIZING:
        return (
            "The operator finalized after fixes: exactly one artifact — the final version, "
            "with its self-audit and tests attested — is accepted, and nothing else."
        )
    return (
        "The review is over. Its channel still carries the post-review summary and the "
        "audit cycle over it; what it does not carry is round traffic, because there is no "
        "round to answer."
    )


# --- payload validation (W-1, W-2, C-1, C-3, W-3, G-5, G-6, K-1, FF-1) ----
#
# Every refusal below hits THE SINGLE MESSAGE with an actionable error and never touches a
# completed pass or an open gate (W-5). The messages say what is owed, not merely what was
# wrong: a validator that only says "no" is how an automated loop goes quiet.


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _refuse(what: str, problems: list[str], expected: str) -> None:
    if problems:
        raise InvalidMessagePayloadError(
            f"malformed {what}: " + "; ".join(problems) + f" — expected {expected}"
        )


def validate_proposals(payload: dict, rnd: Round | None) -> None:
    """W-1: one entry per finding of the pass, and nothing implemented yet.

    The proposal is where narrowing becomes visible ("cell" is a legal claim — but it is
    now a CLAIM, on the record, in front of the operator) and where defence-acceptance-by-
    default becomes visible (a waive proposal is top-of-digest). The coverage check is what
    makes the digest's entry count verifiable against the journal.
    """
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise InvalidMessagePayloadError(
            "malformed proposals: `entries` must be a non-empty list — one entry per "
            "finding of the pass (a pass that raised nothing owes no proposals at all)"
        )
    problems: list[str] = []
    seen: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append("each entry must be an object")
            continue
        fid = entry.get("finding_id")
        if not _nonempty(fid):
            problems.append("each entry requires a non-empty `finding_id`")
            continue
        seen.append(fid)
        where = f"entry {fid!r}"
        if not _nonempty(entry.get("context")):
            problems.append(
                f"{where}: `context` must be one plain-language sentence naming the "
                "mechanism this finding concerns — the digest opens the entry with it "
                "verbatim, so an operator surface that stands on its own is a wire field, "
                "not a style request"
            )
        if not _nonempty(entry.get("reason")):
            problems.append(f"{where}: `reason` is required")
        outcome = entry.get("proposed_outcome")
        if outcome not in PROPOSAL_OUTCOMES:
            problems.append(
                f"{where}: `proposed_outcome` must be one of {list(PROPOSAL_OUTCOMES)}, "
                f"got {outcome!r}"
            )
        elif outcome == "fix" and not _nonempty(entry.get("plan")):
            problems.append(f"{where}: a `fix` proposal requires a `plan`")
        elif outcome == "escalate":
            if not _nonempty(entry.get("recommendation")):
                problems.append(f"{where}: an `escalate` proposal requires a `recommendation`")
            if entry.get("recommended_outcome") not in ("fix", "waive"):
                problems.append(
                    f"{where}: an `escalate` proposal requires `recommended_outcome` "
                    "('fix' or 'waive') — accepting an escalation accepts the "
                    "recommendation, and the bookkeeping has to know what was accepted"
                )
        elif outcome == "waive" and entry.get("plan") is not None:
            problems.append(f"{where}: a `waive` proposal carries no plan (its grounds are `reason`)")
        # B.9 B-5: a boundary-citing waive rides on a VALIDATED reference, not on prose.
        # `boundary_ref` is legal only on a waive; the resolution marker is the SERVER's
        # to mint at append time — a client-set one would be a self-granted mechanical
        # route, the exact class SERVER_ONLY_OUTCOMES already refuses elsewhere.
        if entry.get("boundary_ref") is not None:
            if outcome != "waive":
                problems.append(
                    f"{where}: `boundary_ref` belongs to a `waive` proposal — the "
                    "mechanical route it claims exists only for boundary-citing waives"
                )
            elif not _nonempty(entry.get("boundary_ref")):
                problems.append(f"{where}: `boundary_ref` must be a non-empty string when present")
        if "boundary_ref_resolved" in entry:
            problems.append(
                f"{where}: `boundary_ref_resolved` is minted by the server after "
                "resolving the ref against the threat-boundary registry — a client "
                "never sets it"
            )
        claim = entry.get("class_closure_claim")
        if not isinstance(claim, dict) or claim.get("kind") not in ("class", "cell"):
            problems.append(
                f"{where}: `class_closure_claim` must be "
                "{kind: 'class'|'cell', scope} — the narrowing has to be a claim on the "
                "record, in front of the operator"
            )
        elif not _nonempty(claim.get("scope")):
            problems.append(f"{where}: `class_closure_claim.scope` is required")
        # G-6: a security finding's entry answers both fields, or its digest line cannot
        # say who the adversary is and what the defence costs when it misfires.
        finding_type = (rnd.entries[fid].finding_type if rnd and fid in rnd.entries else None)
        if finding_type == "security_mechanism":
            for f in ("adversary", "false_positive_cost"):
                if not _nonempty(entry.get(f)):
                    problems.append(
                        f"{where}: a `security_mechanism` finding's entry must answer "
                        f"`{f}` — the cost asymmetry is predictable on paper before "
                        "implementation, and this field is what makes writing it down "
                        "unavoidable"
                    )
    if rnd is not None:
        expected = set(rnd.entries)
        got = set(seen)
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        duplicates = sorted({f for f in seen if seen.count(f) > 1})
        if missing:
            problems.append(f"findings of this pass with no entry: {missing}")
        if extra:
            problems.append(f"entries for findings this pass did not raise: {extra}")
        if duplicates:
            problems.append(f"duplicate entries for {duplicates}")
    _refuse(
        "proposals",
        problems,
        "{entries: [{finding_id, context, proposed_outcome: fix|waive|escalate, "
        "class_closure_claim: {kind, scope}, reason, + plan (fix) / recommendation + "
        "recommended_outcome (escalate) / adversary + false_positive_cost (security)}]}",
    )


def validate_gate_directive(payload: dict, rnd: Round | None) -> None:
    """W-2: one relayed operator ruling, per finding, carrying the operator's words.

    The relay is prompt-maintained authority, not authenticated — the same recorded
    concession every `decision_response` already carries. What the schema CAN hold is the
    shape: which finding, which verb, and a typed amendment rather than free text.
    """
    problems: list[str] = []
    fid = payload.get("finding_id")
    if not _nonempty(fid):
        problems.append("`finding_id` must be a non-empty string")
    elif rnd is not None and fid not in rnd.entries:
        problems.append(
            f"`finding_id` {fid!r} is not an entry of the open round "
            f"(this round's findings: {sorted(rnd.entries)})"
        )
    directive = payload.get("directive")
    if directive not in DIRECTIVES:
        problems.append(f"`directive` must be one of {list(DIRECTIVES)}, got {directive!r}")
    if not _nonempty(payload.get("operator_quote")):
        problems.append(
            "`operator_quote` is required — the quote is what keeps the relay auditable "
            "against chat"
        )
    if directive == "amend":
        amendment = payload.get("amendment")
        if not isinstance(amendment, dict):
            problems.append(
                "`amend` requires a typed `amendment`: {outcome: 'fix', plan} or "
                "{outcome: 'waive', reason}"
            )
        else:
            outcome = amendment.get("outcome")
            if outcome == "fix" and not _nonempty(amendment.get("plan")):
                problems.append("an amended fix requires `amendment.plan`")
            elif outcome == "waive" and not _nonempty(amendment.get("reason")):
                problems.append("an operator waiver requires `amendment.reason`")
            elif outcome not in ("fix", "waive"):
                problems.append(
                    f"`amendment.outcome` must be 'fix' or 'waive', got {outcome!r} — no "
                    "other amended outcome exists"
                )
            # G-8: an amendment that implements an OPERATOR-INITIATED change carries the
            # operator's verbatim confirmation of development's paraphrase. Whether the
            # change was operator-initiated is development's own declaration (the server
            # cannot observe chat); what the server can enforce is that a declared one
            # never arrives without its confirmation.
            if amendment.get("operator_initiated") and not _nonempty(
                payload.get("paraphrase_quote")
            ):
                problems.append(
                    "an amendment implementing an operator-initiated change requires "
                    "`paraphrase_quote` — the operator's verbatim confirmation of your "
                    "paraphrase (G-8), auditable against chat like every operator quote here"
                )
    elif payload.get("amendment") is not None:
        problems.append(f"`amendment` belongs to `amend`, not to {directive!r}")
    _refuse(
        "gate_directive",
        problems,
        "{finding_id, directive: accept|amend|detail|class_analysis, amendment?, "
        "operator_quote}",
    )


def _validate_report_ref(payload: dict, rnd: Round | None, directive: str, what: str) -> list[str]:
    """A report answers ONE open directive — refusing a dangling ref per-message (W-5)."""
    ref = payload.get("directive_ref")
    if not _nonempty(ref):
        return ["`directive_ref` must name the finding_id of the open directive"]
    if rnd is None:
        return [f"no round is open, so no {directive!r} directive can be outstanding"]
    entry = rnd.entries.get(ref)
    if entry is None or entry.report_pending != directive:
        return [
            f"`directive_ref` {ref!r} matches no OPEN {directive!r} directive in this "
            "round (a superseded request closes when the operator rules directly, and a "
            f"report for a closed request is refused — post the {what} only while the "
            "request stands)"
        ]
    return []


def validate_detail_report(payload: dict, rnd: Round | None) -> None:
    """C-3: the expanded picture of one finding, typed rather than prose."""
    problems = _validate_report_ref(payload, rnd, "detail", "detail_report")
    for f in ("context", "evidence", "location", "consequences"):
        if not _nonempty(payload.get(f)):
            problems.append(f"`{f}` is required and non-empty")
    _refuse(
        "detail_report",
        problems,
        "{directive_ref, context, evidence, location, consequences}",
    )


def validate_class_report(payload: dict, rnd: Round | None) -> None:
    """C-1: the class-closure loop's answer, with its closure strength stated honestly.

    `structural` means a future violation is visible WITHOUT a sweep; `lexical_only` is the
    honest weak claim. Four letter-perfect lexical sweeps left four surviving class members
    in one measured review — so a bare "class closed" resting on a sweep is invalid here,
    and the weak claim has to be said out loud where the operator can decline it.
    """
    problems = _validate_report_ref(payload, rnd, "class_analysis", "class_report")
    if not _nonempty(payload.get("root")):
        problems.append("`root` must name the class in one phrase")
    if not _nonempty(payload.get("enumeration_command")):
        problems.append("`enumeration_command` must be the command you actually ran, verbatim")
    if payload.get("closure_kind") not in ("structural", "lexical_only"):
        problems.append(
            "`closure_kind` must be 'structural' (a future violation is visible without a "
            "sweep) or 'lexical_only' (swept lexically, class NOT structurally closed)"
        )
    if not _nonempty(payload.get("false_negative_mode")):
        problems.append(
            "`false_negative_mode` must state in one line what this enumeration CANNOT see "
            "('this grep cannot see statements phrased differently')"
        )
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        problems.append("`candidates` must be a list (empty is a legal answer)")
    else:
        for c in candidates:
            if not isinstance(c, dict):
                problems.append("each candidate must be an object")
                continue
            if not _nonempty(c.get("locator")):
                problems.append("each candidate requires a `locator`")
            if c.get("state") not in ("proposed_fix", "out_of_scope"):
                problems.append(
                    "candidate `state` must be 'proposed_fix' or 'out_of_scope' — states "
                    "here are PRE-sanction: nothing is implemented before the operator's "
                    "directive, so `fixed` exists only in post-directive dispositions"
                )
            if not _nonempty(c.get("reason")):
                problems.append("each candidate requires a `reason`")
    _refuse(
        "class_report",
        problems,
        "{directive_ref, root, enumeration_command, candidates: [{locator, state, reason}], "
        "closure_kind, false_negative_mode}",
    )


def validate_operator_finalize(payload: dict) -> None:
    """W-3: the operator's own stop, in one of two modes."""
    problems: list[str] = []
    if payload.get("mode") not in FINALIZE_MODES:
        problems.append(f"`mode` must be one of {list(FINALIZE_MODES)}")
    if not _nonempty(payload.get("operator_quote")):
        problems.append("`operator_quote` is required")
    _refuse("operator_finalize", problems, "{mode: now|after_fixes, operator_quote}")


def validate_finding_types(payload: dict) -> None:
    """G-5/G-6 on the critic's side: every finding is typed, and a defence finding names
    its adversary and the cost of a false positive.

    Typing is what lets the digest sort by "where the operator matters" — the operator's
    judgement load, and the graph's measured cost, concentrate in the first two types.
    """
    problems: list[str] = []
    for f in payload.get("items") or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        ftype = f.get("finding_type")
        if ftype not in FINDING_TYPES:
            problems.append(
                f"finding {fid!r}: `finding_type` must be one of {list(FINDING_TYPES)}, "
                f"got {ftype!r}"
            )
            continue
        if ftype == "security_mechanism":
            for field_name, what in (
                ("adversary", "who exercises the threat WITHIN this review's declared "
                              "threat model"),
                ("false_positive_cost", "what the proposed defence costs when it fires on "
                                        "a legitimate case"),
            ):
                if not _nonempty(f.get(field_name)):
                    problems.append(
                        f"finding {fid!r}: a `security_mechanism` finding must carry "
                        f"`{field_name}` — {what}. A missing-defence finding whose "
                        "adversary names nobody inside the declared model is born with "
                        "its terminal answer visible"
                    )
    _refuse(
        "findings",
        problems,
        "each item to carry `finding_type` (security_mechanism|proportionality|"
        "correctness|completeness|drift), plus `adversary` and `false_positive_cost` on "
        "security findings",
    )


def validate_class_closure(payload: dict, mode: str) -> None:
    """K-1: a `fixed` disposition carries its class-closure claim on the wire.

    The schema binds the MAKING of the claim; its truth stays the critic's target, not the
    validator's. In spec mode a bare "class closed" resting on a lexical sweep is invalid:
    in prose the class is defined by meaning, and a grep enumerates only the wordings its
    author already thought of.
    """
    if payload.get("outcome") != "fixed":
        return
    block = payload.get("class_closure")
    problems: list[str] = []
    if not isinstance(block, dict):
        problems.append(
            "a `fixed` disposition requires a `class_closure` block: "
            "{kind: 'class'|'cell', enumeration_command (when kind='class'), "
            "closure_kind (spec mode), false_negative_mode}"
        )
    else:
        kind = block.get("kind")
        if kind not in ("class", "cell"):
            problems.append("`class_closure.kind` must be 'class' or 'cell'")
        if kind == "class" and not _nonempty(block.get("enumeration_command")):
            problems.append(
                "a `class` closure requires `class_closure.enumeration_command` — the "
                "command you actually ran"
            )
        if not _nonempty(block.get("false_negative_mode")):
            problems.append(
                "`class_closure.false_negative_mode` must state what the enumeration "
                "cannot see, in one line — one honest line makes the claim's strength "
                "part of the claim"
            )
        if mode == "spec" and kind == "class" and block.get("closure_kind") not in (
            "structural",
            "lexical_only",
        ):
            problems.append(
                "in spec mode `class_closure.closure_kind` must be 'structural' or "
                "'lexical_only' — a lexical sweep is explicitly insufficient to support "
                "'class closed' (measured: 4/4 letter-perfect sweeps, 4 surviving members)"
            )
    _refuse("disposition", problems, "a `fixed` disposition to carry its `class_closure` block")


def validate_config_notice(payload: dict, messages: list[dict]) -> None:
    """G-3 / G-7: the two per-review settings, persisted on the wire and bound early.

    Both ride a validated notice accepted ONLY in the initialization window — before the
    review's first artifact version — and inside that window the FIRST one binds for the
    review's lifetime. A setting that could change mid-review would let the party the gate
    constrains widen it after seeing what the gate caught; a setting implied from chat
    would not be persisted at all. Absence of the notice means the defaults, never an
    error: silence falls to `auto` and to the operator's own numbers.
    """
    phase = payload.get("phase")
    if phase not in (GATE_MODE_PHASE, REVIEW_CONFIG_PHASE):
        return
    if not initialization_window_open(messages):
        raise InvalidMessagePayloadError(
            f"a {phase!r} notice is accepted only in the initialization window — before "
            "this review's first artifact version. The setting in force was fixed then "
            "(or defaulted); it does not change mid-review"
        )
    if _first_config_notice(messages, phase) is not None:
        raise InvalidMessagePayloadError(
            f"this review already carries a {phase!r} notice — the first one binds for the "
            "review's lifetime"
        )
    problems: list[str] = []
    if phase == GATE_MODE_PHASE:
        if payload.get("mode") not in GATE_MODES:
            problems.append(f"`mode` must be one of {list(GATE_MODES)}")
        if not _nonempty(payload.get("granted_by")):
            problems.append("`granted_by` must record who granted the mode")
        _refuse(
            "gate_mode notice", problems, "{phase: 'gate_mode', mode: auto|all_wait, granted_by}"
        )
        return
    for section, fields in (
        ("ping", ("primary", "fallback")),
        ("fault_stretch", ()),
        ("stall", ()),
    ):
        supplied = payload.get(section)
        if supplied is None:
            continue
        if not isinstance(supplied, dict):
            problems.append(f"`{section}` must be an object")
            continue
        for f in fields:
            if f in supplied and not _nonempty(supplied.get(f)):
                problems.append(f"`{section}.{f}` must name a channel role, as a string")
    for section, f in (
        ("ping", "fallback_delay_s"),
        ("fault_stretch", "attempts"),
        ("stall", "bootstrap_threshold_s"),
    ):
        supplied = payload.get(section)
        if isinstance(supplied, dict) and f in supplied:
            value = supplied[f]
            # A bool is an int in Python and would sail through a numeric check while
            # meaning nothing as a delay; a zero or negative one would disable a mechanism
            # the spec states cannot be disabled.
            if type(value) not in (int, float) or value <= 0:
                problems.append(f"`{section}.{f}` must be a positive number, got {value!r}")
    _refuse(
        "review_config notice",
        problems,
        "{phase: 'review_config', ping: {primary, fallback, fallback_delay_s}, "
        "fault_stretch: {attempts}, stall: {bootstrap_threshold_s}} — any field may be "
        "omitted and falls to its default",
    )


def validate_fork_analysis(payload: dict) -> None:
    """FF-1: an escalated fork records what makes it a fork, and the third-option search.

    Measured 2026-07-21: four of five operator-escalated forks closed by REFORMULATION, not
    choice — a third option simpler than both existed. Operator attention is the scarcest
    resource in the loop; the check is one paragraph where the waste was one gate each.
    """
    block = payload.get("fork_analysis")
    problems: list[str] = []
    if not isinstance(block, dict):
        problems.append(
            "a development escalation requires a `fork_analysis` block: "
            "{blocking_constraints, third_option_search}"
        )
    else:
        if not _nonempty(block.get("blocking_constraints")):
            problems.append(
                "`fork_analysis.blocking_constraints` must say what exactly makes this a "
                "fork — which constraint blocks each side"
            )
        if not _nonempty(block.get("third_option_search")):
            problems.append(
                "`fork_analysis.third_option_search` must record the explicit search for a "
                "third option that dissolves the fork, and its result"
            )
    _refuse("escalation", problems, "{..., fork_analysis: {blocking_constraints, third_option_search}}")


# --- B.11 Part E/F: the map and its trailer, as predicates both sides share ------------
#
# THE A-1 LESSON, APPLIED BEFORE IT COSTS ANYTHING. The coverage predicate existed three
# times over and all three copies carried the same false premise, which is why B.11 has a
# Part A at all. So the once-only rule of the reconciliation attempt — asked by the SERVER
# when it validates a posted `reconciliation`, and by the WATCHER when it decides whether
# to schedule one — is written once, here, in the module both already import. It works on
# wire dicts; the server converts its rows.

#: The operator's carrier for a SECOND reconciliation attempt (E-11): a field on an
#: existing `decision_response`, never a fourth message kind.
RECONCILIATION_RETRY_KEY = "reconciliation_retry"


def map_target(messages: list[dict]) -> dict | None:
    """The base+commit pair the map is written against (B.11 E-1, E-4, E-9), or None.

    The target commit is the review's CONVERGED BRANCH COMMIT — the commit of the artifact
    version that converged, never a merge result, because a merge result is a tree nobody
    reviewed. The pair is what the changed-module scope is derived from: "what was actually
    built" is a statement about a CHANGE, and a change needs two points.

    An intent summary is skipped: it is a document about the version, not the version.
    """
    target = None
    for m in messages:
        payload = m.get("payload") or {}
        if m.get("kind") != "artifact" or payload.get("intent_summary"):
            continue
        ref = payload.get("artifact_ref") or {}
        if isinstance(ref, dict) and ref.get("base") and ref.get("commit"):
            target = {
                "base": ref["base"],
                "commit": ref["commit"],
                "artifact_seq": m.get("seq"),
            }
    return target


def map_seq_for(messages: list[dict], target: dict | None) -> int | None:
    """The seq of the map already written for this base+commit PAIR, if any (B.11 E-10).

    IDEMPOTENCE IS THIS AND NOTHING ELSE. No "already done" marker is introduced: an
    existing map for the same subject makes the pass unnecessary, and the channel is the
    only record consulted.

    THE SUBJECT IS THE PAIR, SO THE IDENTITY IS THE PAIR (critic finding
    `map-identity-drops-base`, round 2 of this slice's own review). Matching on the commit
    alone was this slice contradicting its own reasoning: the map's scope is derived
    mechanically from base+commit precisely because "what was actually built" is a
    statement about a CHANGE and a change needs two points — and then half of that pair
    was used to decide the map already existed. The same commit submitted against a
    different base is a different slice with a different derived scope, and an earlier map
    would have suppressed both the pass and the waiting-state surface for it, leaving the
    operator without a map of the actual change.

    Takes the target dict rather than loose strings so that every caller asks the question
    with the same shape; a predicate whose arguments are assembled differently at each
    site is the shape that let this drift in.
    """
    if not target:
        return None
    base, commit = target.get("base"), target.get("commit")
    if not base or not commit:
        return None
    for m in messages:
        payload = m.get("payload") or {}
        if (
            m.get("kind") == "map"
            and payload.get("base") == base
            and payload.get("commit") == commit
        ):
            return m.get("seq")
    return None


def intent_audit_clean(messages: list[dict]) -> bool:
    """Has the post-review Intent Summary's faithfulness audit come back clean? (F-1)

    Channel-observable, which is F-1's own requirement of its trigger: a summary artifact
    exists and a critic pass over it raised nothing. The audit checks the summary against
    the CHANNEL — did the summary lie about the review — and it is untouched by this slice
    (G-3); reconciliation only waits for it, because a reconciliation run against a summary
    that is itself under dispute would be comparing to a moving document.

    THE QUESTION IS ABOUT THE CURRENT SUMMARY, NOT ABOUT ANY SUMMARY EVER (critic finding
    `reconciliation-runs-on-superseded-summary`, round 3 of this slice's own review). The
    audit is iterative by design: findings are folded in and the summary is RE-POSTED as a
    new version until a pass comes back clean. So asking "does some clean pass exist over
    some summary" answers yes forever after the first one — including while a later summary
    sits with findings against it, which is precisely the moving document the docstring
    above says this predicate exists to avoid. The reconciliation gets exactly ONE attempt,
    so spending it against a superseded summary spends it for good.

    Order-aware, for the same reason the convergence declaration is: the LAST pass over the
    latest summary speaks for it, and a later non-empty pass takes the verdict back.
    """
    summaries = summary_artifact_seqs(messages)
    if not summaries:
        return False
    latest = max(summaries)
    clean: bool | None = None
    for m in messages:
        payload = m.get("payload") or {}
        if (
            m.get("kind") == "findings"
            and m.get("role") == "critic"
            and payload.get("artifact_seq") == latest
        ):
            clean = not (payload.get("items") or [])
    return clean is True


def reconciliation_standing(messages: list[dict], map_seq: int) -> dict:
    """The once-only state of the reconciliation attempt over one map (B.11 F-1, E-11).

    Returns ``{"spent": [seqs], "requests": {seq: consumed}, "claimable": seq|None}``.

    THE TWO CONDITIONS ARE ONE RULE: an attempt is not licensed when a `reconciliation`
    for this map already exists — WITH EITHER OUTCOME — and no unconsumed operator retry
    request stands for it. A spent attempt closes the door; an unconsumed request re-opens
    it exactly once. A `failed` attempt IS a spent attempt: treating it as unspent would
    turn the operator's «Попытка пусть будет одна» into an unbounded retry after every
    failure, which is precisely the false-promise shape Part A removes elsewhere.

    A request is CONSUMED when some reconciliation names its seq, and unconsumed
    otherwise — readable from the channel alone. No counter and no stored state: every
    reference in this slice points from a later message to an earlier one. Two requests
    therefore license two further attempts, each naming its own, and none is silently lost.
    """
    requests: dict[int, bool] = {}
    spent: list[int] = []
    for m in messages:
        payload = m.get("payload") or {}
        if m.get("kind") == "decision_response" and m.get("role") == "operator":
            request = payload.get(RECONCILIATION_RETRY_KEY)
            if isinstance(request, dict) and request.get("map_seq") == map_seq:
                requests[m.get("seq")] = False
        elif m.get("kind") == "reconciliation" and payload.get("map_seq") == map_seq:
            spent.append(m.get("seq"))
    for m in messages:
        if m.get("kind") != "reconciliation":
            continue
        claimed = (m.get("payload") or {}).get("retry_request_seq")
        if claimed in requests:
            requests[claimed] = True
    unconsumed = sorted(seq for seq, used in requests.items() if not used)
    return {
        "spent": spent,
        "requests": requests,
        # The FIRST attempt claims nothing (it needs no licence); a later one must name the
        # request it consumes, so the claim is readable from the channel alone.
        "claimable": (unconsumed[0] if unconsumed else None) if spent else None,
    }


def reconciliation_licensed(messages: list[dict], map_seq: int) -> bool:
    """May a reconciliation attempt over this map be made right now? (F-1's once-only)"""
    standing = reconciliation_standing(messages, map_seq)
    return not standing["spent"] or standing["claimable"] is not None


#: B.12 A-7 — where a review names the development cycle it belongs to: ONE key in the
#: config frozen at creation, beside the operator profile and for the same reason. The
#: cycle is a property of the review, decided when it was created, and a per-message copy
#: would be one more thing that can drift.
#:
#: It lives in THIS module, which is the module that exists so the server and the watcher
#: never hold two spellings of one wire field — the server resolves the anchor's documents
#: from the graph, the watcher decides whether the reconciliation is schedulable at all,
#: and both must mean the same key by construction rather than by two hands agreeing.
CYCLE_ANCHOR_KEY = "cycle_anchor"


def cycle_anchor_of(config: dict | None) -> str | None:
    """The development-cycle anchor this review belongs to, or None (B.12 A-7)."""
    ref = (config or {}).get(CYCLE_ANCHOR_KEY)
    return str(ref) if ref else None


# --- B.12 A-7: the declared intent set is GONE, and this is where it stood -------------
#
# B.11 had development post a `notice` with `phase: "intent_set"` naming the cycle's Intent
# Summaries, and the server validated every reconciliation citation against it. Both are
# removed here, and it is one removal rather than two: the declaration was unverifiable by
# construction. Three of the four documents were files, and the server has no repository —
# so the only one it could check was the one in which a mistake is impossible. The critic
# of review `36a80d12` peeled three layers off the same element in successive rounds
# (citations outside the set, then the absence of the declaration, then the declaration
# itself), which is the signature of a mechanism defending something that should not have
# been declared at all.
#
# WHAT REPLACES IT: the review names its development cycle in the config frozen at
# creation, and the cycle's documents hang off that anchor in the same database
# (`review.cycle.resolve_cycle_documents`). The set is now a fact the server reads rather
# than a claim it takes — which is also what lets the citation check keep standing, with a
# ground under it for the first time.
#
# DERIVING THE SET FROM FILENAME PATTERNS remains forbidden, and that has not softened: as
# of 2026-08-25 there were 43 intent files in `docs/implementation`, the names are not
# paired, and several pre-review intents have no post-review twin at all.
#
# Recorded here rather than deleted silently, because the next cycle that needs "the set of
# documents this review compares against" will otherwise reinvent the declaration as the
# obvious move.
