# Intents prompt — Review Orchestration (skill)

> Source of record for the **intents** skill: it produces the two operator gates that
> bracket a review — the **pre-review** and **post-review Intent Summaries** — as **two
> standalone files**, each with a one-line chat pointer. Run by development (reference:
> Claude Code). The Intent Summary is **not a report about the review** — it is the
> **human-readable decompilation of the artifact**: what the thing is, what it does, and
> by what rules, so the operator (who sits on the semantic gap and does not read the
> spec) can *see* what will be built. It is one genre with the semantic map, in an
> earlier tense (see the conventions' rule on human-readable rendering).

> **B.12** (`docs/design/2026-08-25_development_cycle_and_reconciliation_form_spec.md`) adds
> the cycle these two summaries belong to: the operator DECLARES a development cycle, its
> anchor carries the timestamp and identifier of the cycle's first message, and each intent
> is written to that anchor **in full text at its own finalisation** with its type label,
> its subject (pre-review: which artifact it translates) or its review affinity
> (post-review: which review it reports) — not in a batch at the end, because the moment of
> writing is itself data. See "Before the pre-review gate" and the Rules.

These summaries are **human gates** and must survive automation (INV: human gates are
never removed). Automation compresses the loop *between* them; it never replaces them.
Each is written to a **file**; only a **one-line pointer** goes to chat (channel
discipline — never dump the full summary into chat unless the operator asks). The
post-review summary consumes the review's state **from the store** (the authoritative
timeline: artifacts, findings, dispositions, waivers, dissents, escalations, the
operator's decision responses, and status) for its service tail — but its **body** is a
description of the subject, not a rendering of that timeline.

## The genre (both summaries share it)

A person reads it top to bottom and understands the thing without the spec:

- **Complete on decisions, free of mechanics.** Say every resolved fork and every rule
  that binds the system's behaviour; omit the machinery (tables, indexes, data
  structures, file layout). "In this situation the system does that" belongs in; "a
  field of this type with a partial index" does not — unless it is the answer to a
  **declared constraint** (disk, tokens, latency, cost, privacy, a size limit), in which
  case it is itself a decision and is rendered at the level of meaning, as
  non-technically as the constraint allows.
- **Self-contained.** No references to spec section numbers or decision ids; every
  abbreviation expanded on first use. The operator does not open the spec — the summary
  stands alone. Pointers to code/§/commit, if any, go in one closing "where this lives"
  line, never woven through the prose.
- **Plain prose**, bilingual where the operator works bilingually.

## Before the pre-review gate: the cycle is DECLARED (B.12 A-2, A-9)

A development cycle — one slice of work, from the operator's first free-form sentence to
acceptance — begins **when the operator says so. You never infer one.** The declaration may
arrive at any time: at the first sentence, or well into the discussion, which costs nothing
because the honest start time does not come from when anyone remembered the process.

At the declaration, create the cycle's `DevelopmentCycle` anchor and write **its two start
fields**: the **timestamp of the cycle's first message** and that message's **identifier**,
both taken from the session record. The recorded start is *not* the moment of declaring —
**you name the message you believe the cycle starts at, and the operator confirms or moves
it**; the confirmed answer is what is stored. Sessions interleave: the session that produced
one recent spec opened by finishing the previous slice and slid into a new cycle with no
marker, which is exactly why this is confirmed rather than derived.

They are stored fields rather than something recomputed later. The transcript is prose, and a
boundary that has to be parsed out of prose stops existing the moment the session record
stops being the live source.

**Past cycles are never backfilled.** A cycle declared and then abandoned keeps its anchor
with whatever it accumulated, marked abandoned rather than deleted — "we discussed this and
decided not to" is precisely what otherwise gets re-proposed six months later.

The full write contract for the anchor and its documents is in the conduct-review leaf
(§11a); what belongs here is the declaration and the intents' own writes (see Rules).

## Pre-review Intent Summary (before the loop runs)

Purpose: let the operator confirm *what is about to be built* — and, secondarily, how it
will be reviewed — before any critic time is spent. Pre-review intent = **"what I am
about to write into the spec."** Write a file and post a one-line chat pointer.

**First and main block — the subject.** A description of the thing you are about to
create, by the genre above: what it is, what it does, how it behaves from the point of
view of the user / the agents, what changes in the world once it exists. This is the
body; it is what the gate is *for*. The gate is meaningful only if the operator
understands **what** is being built.

**Second block — "what the operator needs to decide" (process).** After the subject,
and clearly subordinate to it:

- **Scope & mode** — spec or code; what is in and out of this review.
- **Epistemic tiers** — the load-bearing elements and their tiers (fact / constraint /
  operator decision / recorded decision / llm judgment), so the operator sees where the
  critic will aim and what is being treated as settled.
- **Proposed depth tier** — light / standard / deep, **with the reasoning for the
  proposal**: how much machinery this artifact is worth. This is the main lever on how long
  the review takes, so it is a required slot, not an optional one. Say plainly what the
  chosen depth stops doing. **Silence falls back** to the profile's default, and with no
  default, to the full pipeline.
- **The build-horizon fork** — *"is this built for how you use it today, or for other
  people running it themselves?"* Required, and **operator-only**: you may recommend, you
  may not settle it. Everything about proportionality is judged against the answer, so a
  review that never asks it silently defends the widest imaginable deployment — which is
  exactly how a spec ends up hardened for a cluster, a remote store and a third-party
  implementation while the operator runs it on one laptop.
- **The context this review will run on, restated for correction** — the threat model, the
  operating scale, and the criticality thresholds as read from the profile, in plain words.
  Restating rather than silently applying is the point: an absorbed-but-never-decided
  context is what lets both roles defend it at maximum with nobody chartered to object. The
  operator corrects it here, and the corrected value governs **this** review even where it
  differs from the profile.
- **Proposed procedural waivers** — each with rationale (trigger size, reversibility, whether it
  touches a recorded fact/decision — flag that prominently). The operator grants or
  declines; **silence = not granted** (full pipeline runs).
- **The round gate is in force, and its mode is chosen here.** Every round of this review
  stops before anything is implemented: development proposes per finding, you see the round
  in a digest, and the review stays parked until you have marked up what is yours to rule
  on. The mode is the one setting: **`auto`** (the default) holds the judgement findings —
  defence, proportionality, anything proposed for a waiver or an escalation — and lets the
  mechanical ones ride on their accepted proposal; **`all_wait`** holds every finding of
  every round. Silence means `auto`. Both this and the ping configuration bind for the
  review's lifetime and are set before its first version, so this gate is the only place to
  choose them.
- **The frozen instrument and its independence step (B.9 D-1/D-3).** Name what will
  actually run this review: the critic's engine / model / effort as frozen in the
  review snapshot, the profile version it launches from, and the development
  instrument declared beside it. Then name **the independence step this review runs
  at** — different provider > same family > same model (a marked self-check needs the
  operator's typed waiver at creation) — computed from the two snapshot records'
  provider/family facts; an ambiguous or unfilled family shows the weaker step marked
  "conservative: ambiguous lineage". Check the entered facts against the provider's
  official online documentation (fall back to model knowledge only when the lookup is
  unreachable — then the absence of a warning proves nothing) and voice any suspicion
  as an **advisory — never a block, never an edit**. "Clean" from a same-family critic
  must never impersonate "clean" from a foreign engine.
- **Known risks / open decisions** — anything you expect to need the operator, and any
  dissent you are already holding.
- **Audience genre only — the reader artifact, shown.** The pre-review summary of an
  audience review MUST carry the **path to the reader-artifact file** and its **reader
  fingerprint** (the hash of what the blind reader will actually see); the full text goes
  to chat on request. The operator will be ruling on dispositions of blind findings —
  edits to this text — and a gate over edits to a text the operator has never seen is a
  gate in form only.

### The acquisition path — when the profile does not yet hold what the gate needs

The gate is already asking the operator about threat model, scale, criticality, depth and
horizon. So the gate is also where a **missing** profile domain gets filled: no separate
onboarding surface exists, and building one would only add a second place where the same
questions are worded and a second surface to keep in step with this one.

A new operator therefore acquires their profile by **running their first review**, at the
moment each value is actually needed and in the context that makes the question meaningful.
This is walked by every adopter, not once by the current operator — every deployment starts
with an empty profile.

Two cases, deliberately different:

- **Domain absent** → propose it, ask for the value **in the operator's own terms** (not by
  its registry key), record the answer with `remember_preference`, then surface its
  acceptance as an operator action and **relay** that acceptance.
- **Domain pending** → the value may already be recorded; what is missing is the
  **acceptance**. Surface *that* instead of re-asking a question the operator already
  answered.

**Recording the value is NOT the end of the step — acceptance is, and it is the operator's.**
A proposed domain is created pending and non-binding; it governs nothing until accepted, and
you can only **relay** that acceptance (`resolve_domain` is an operator decision relayed).
A gate that captured values and stopped would leave the domain pending forever, which every
consumer reads as **absent** — the failure would be silent and indistinguishable from a thin
profile.

**Completion is a verifiable end state, not a sequence of calls:** the step is done when
`get_operator_profile` shows the domain **compiled into the profile**. Anything short of
that is an incomplete acquisition, however many calls were made along the way.

Gate: the operator approves, adjusts, or waives. Do not start the loop until the
pre-review intent is settled (or explicitly waived — some reviews waive it when an
interactive intent consolidation already served its role; record that waiver).

## Post-review Intent Summary (after convergence, before finalize)

Purpose: the **human backstop before finalize** — the last point where the operator sees
the whole thing before it locks. Post-review intent = **"what the spec now says, and
therefore what will go into the code."** What is in this document goes into the code;
what is not in it does not. Write a file, post a one-line chat pointer.

**Opens with a delta section (at the front, not the tail).** "What changed in meaning
since the pre-review reading" — not a findings ledger (that is bookkeeping, below), but a
substantive account: *"I meant it this way; the review showed it was wrong; now it is
this way."* If nothing of substance changed, say that in one line.

**Body — the subject, final form.** The same genre as the pre-review intent, now
describing what will finally be built: every decision as it now stands, by the rules of
the genre. This is the contract on implementation.

**Service tail (bookkeeping, after the body — not the body).** Drawn from the store — and
under B.7 **the server computes it for you**: `GET /reviews/{id}/service_tail` renders the
ledger, the waivers, the dissents, the escalations with their resolutions, the routed
triage ledger, the round-gate directive counts and the coverage summary from the journal.
**Copy it verbatim.** A computed ledger needs no faithfulness audit — which shortens the
audit cycle to the description of the subject — and the ledger is the proof that no finding
was dropped, which is exactly the claim least safe to leave to the recollection of the party
the summary gates. What you still write yourself is the delta and the body.

**When the OPERATOR stopped the review** (`operator_finalized`, either mode), the body is
written **from a walk of the artifact as it actually stands at stoppage** — per finding,
what was implemented and what was not, including findings the server closed as
`operator_risk_accepted` whose fixes the walk finds already present, stated plainly. The
delta and the body describe the found state, not the planned one. The faithfulness audit
still runs, fully automatically, and checks them against that final state: an operator final
stops **defect rounds** only; it never turns the audit off. Gate fatigue is a declared risk
of the round gate, and this audit is the backstop for exactly that — "I may get tired and
miss something in one of the cycles, which then has to surface in the post-review intent"
(the operator, translated from Russian).

- **Outcome** — converged / operator-finalized (which mode, and their words) / returned;
  the final artifact version.
- **Findings ledger** — every finding and its terminal disposition: `fixed` (what
  changed) or `waived` (with the recorded reason — and, if the critic contested the
  waiver, the operator's resolution). Nothing omitted — the ledger is the proof that no
  finding was dropped.
- **Procedural waivers granted at gates** — each with its reason; recorded-decision
  touches flagged. **Name the sense, never the bare word.** "Waiver" has four referents in
  this system and they are counted in four different places: a *procedural waiver* is a
  channel message granted at a gate (this bullet); a *waived finding* is a terminal
  disposition, counted in the findings ledger above; a *below-threshold drop* is the
  economic layer's route, counted in the loop metrics; a *self-check waiver* is the
  operator's typed grant permitting a same-model review, frozen in the instrument
  snapshot. A heading that says only "Waivers" is true of all four and reports one — which
  is exactly how a service tail once printed "none" while ten findings sat waived three
  paragraphs above it. So: each sense is named by its own phrase, and the bare word is
  never a section heading. A fifth sense then arrives as a heading that breaks the rule,
  visible without anybody sweeping the text for the word.
- **Dissents recorded** — any operator decision held over development's or the critic's
  objection, with the objection.
- **Escalations & their resolution** — every escalation raised and how the operator
  settled it: an operator-decision challenge (held / overruled), a **contested
  disposition** (a waiver the critic judged inadequate — upheld or overruled?), a
  contested fork (oscillation), and any **external-defect** escalation (the base defect,
  the operator's freeze / defer / reject, and its **graph-result ledger** — dedup outcome,
  write status, and the node/reference id, **or an explicit no-node debt** when the write
  was deferred / failed / rejected, so the operator is never told a base defect was
  persisted when it was not). Surfacing a contested waiver and its resolution — and any
  un-persisted external-defect debt — is a core job of this backstop.
- **The routed triage ledger — everything handled without the operator, disclosed in
  full.** This is the accountability half of the economic layer; without it, routing
  findings away from the operator is indistinguishable from silently suppressing them. The
  gate is where a drop can still be contested at a cost of one line of reading, and that is
  what makes the automatic routes safe to grant in the first place.

  **Produced by ENUMERATING THE STORE, never by recollection.** Walk the `disposition`
  messages **by `triage.route`** and emit:
  - every **`drop`** with its `e_estimate`, its `basis`, and the **resolved values** in
    `profile_inputs` — each with its source and as-of marker, so the operator sees what the
    drop was measured against rather than merely which settings were consulted (the values
    that governed it are not recoverable later: a gate correction binds for one review, and
    a mid-review preference is written back as it happens);
  - every **`auto_fix`** and every **`auto_dispose`** — precisely the findings that never
    reached the operator at all.

  Disclosure is **per finding, not aggregate**. A disposition carrying **no** route is a
  **defect in the ledger**, not an item to skip — report it as such.
- **The depth tier actually applied, and who granted it.** Not the tier proposed — the one
  that ran.
- **Coverage map summary** — how many manifest rows, how many reviewed-clean, how many
  produced findings, how many were never reached and why; and the blind-edge row's
  `searches_performed`. State honestly that `reviewed-clean` is a **claimed** verdict: the
  map measures claimed coverage, not reading. **Never present a coverage map for a review
  that had none** — absent machinery is stated as absent, not omitted silently.
- **Audience genre only — the delivery audit line.** The summary carries the **path to the
  final version of the reader artifact**, and its own line in the faithfulness audit:
  **per artifact version and per counted reading run, what was handed to the operator** —
  the version diff digests and the report renders (blind; strategic and machine when
  enabled). The claim is checked **against the chat journal** at this gate. The render
  files themselves are machine-guaranteed by the crediting gate; what this line audits is
  the delivery gesture, which no server holds — that is why it is audited rather than
  assumed.
- **Durable outcome to fold into the graph** — the finalized spec/decision(s), the
  waivers-with-reason, the dissents, and any portable lesson (provisional until a
  confirming implementation/result). Nothing transient. **Plus exactly ONE pointer to
  the journal (B.9 E-2): the review id and where its channel lives** — the service,
  read with the deployment's ordinary bootstrap credential (E-1 keeps a closed review's
  read endpoints reachable after its tokens are revoked). The journal itself is **never
  copied into the graph**: the review tables live in the same Postgres under the same
  backup, so a copy adds no durability, while hundreds of protocol messages would flood
  semantic search with process noise. The pointer suffices only because an agent can
  actually retrieve the journal through it — that reachability is the recorded
  precondition, not an assumption.
- **The projection directory is disposable (B.9 E-3)** — after finalize, delete the
  review's projection directory (or note it for hand cleanup): the file is derived
  (regenerated from the channel at any moment) and the journal it projects stays
  reachable per E-1. Deleting a derived file is safe by construction; leaving it is
  merely untidy.

**Faithfulness audit first — criterion: completeness of the subject description.** The
summary is authored by the party it gates, so before the chat pointer goes to the
operator, post it into the review channel as an artifact whose payload is marked
`intent_summary` and carries the summary text, the `artifact_seq` of the converged
version it summarizes, **and the review's `mode` (B.8 E-1: the summary inherits the
review's mode — a code-mode review's summary posted without one used to buy a spurious
spec cold pass; the watcher no longer defaults it, and a summary is never a cold-pass
target in any mode)** — e.g. `{"intent_summary": true, "converged_artifact_seq": <n>,
"mode": "<the review's mode>", "summary_markdown": "..."}`. The critic audits it against
the converged artifact and the record, asking **"what is in the spec that this document
omits, and that the operator would want to know?"** — completeness and faithfulness of
the *meaning of the subject*, not whether verification claims are overstated. Fold its
findings into the summary and re-post until the audit pass is clean. The operator reads
the **audited** version. The audit pass runs like every B.9 pass — a fresh session over
the projection, development explanations never delivered (the old two-delivery
sequencing died with the cold pass, B-4); the terminator is unchanged: a clean audit
pass — empty findings plus a
`needs_human` carrying `"phase": "gate_handoff"` (an explicit handoff: no question, no
`decision_response` owed, cleared by the gate action itself), never a second `converged`
— ends the cycle (the summary is never itself summarized).

**The summary-audit ledger has a TERMINATOR (B.8 E-2)** — the standing rule that ended a
nine-pass self-referential chase (review 4e4d23b6): (1) the summary carries **no numeric
totals of audit findings or audit rounds** — they go stale with every re-post; (2) any
per-pass list in it is a **frozen slice up to a named seq** and claims no completeness;
(3) later audit passes of the self-descriptive class are **disposed in the replay and
NOT appended** to the summary's list — the replay is the only complete count. Write the
summary to these rules from the first version; the critic audits against them and must
not demand what they exclude.

Gate: the operator **accepts** (→ finalize, then graph-sync) or **returns** with changes
(→ the loop reopens at a new artifact version; the no-progress window resets).

## The map stands beside this summary (code reviews, B.11)

On a code review that declared the semantic-map role, a second document reaches the
operator at the same gate: the critic's **semantic map**, written from the FINAL CODE by a
reader that has seen neither this summary nor the channel it came from.

**This summary is unchanged and stays mandatory.** The map does not replace it, does not
supersede it, and is not a later version of it — an earlier convention said it superseded
this document and that is reversed. They are two different things: the intent is the
**author's account** of what was decided, the map is an **independent reading of the code**.

The pair is the point, and it is the operator's own ruling (translated from Russian): "Better
let it stand beside. That way one can reconstruct at which stage the hole appeared." Neither
document localises a defect
alone:

- the intents agree and the map differs → the defect is in the **implementation**;
- the map matches the code as designed but this summary misdescribes it → the defect is in
  the **self-description**, in the very report the operator relies on;
- all three agree and the operator still objects → the defect is in the **design**;
- the map contradicts the post-review intent OF THE SPEC while the code faithfully
  implements the spec → the spec was **summarised to the operator as something it was
  not**, and consent was given to that. This is the worst address, and it is only reachable
  because the two documents both survive.

Note what this does NOT change: the faithfulness audit of this summary runs exactly as
before, and it checks the summary against the **channel** — did the summary lie about the
review. Faithfulness to the **subject** is what the map covers, and nobody checked it until
now. Three audits of one document would be two too many; these two check different things.

**The map carries no references to any intent**, and that is deliberate rather than an
omission: it has never seen one, and a provenance list it could not verify would be
decoration. The machine-assisted comparison is a separate pass (`reconciliation`), and the
references live there, where a reader is actually allowed to read them.

## Rules

- **Two files, not one.** The pre- and post-review intents are **two standalone files
  side by side**, not one file rewritten in place. Each is substantial content → a file
  with a one-line chat pointer. Full content into chat only on explicit operator request
  (phone opt-in).
- **Files may be gitignored; the durable record is the graph.** By project rule the
  intents are process artifacts and **may live only on the private surface** (gitignored,
  not committed). That is deliberate, not an omission. The durable record of the promises
  is therefore the graph — a gitignored intent that is never graph-synced leaves no lasting
  trace at all.
- **Each intent is written to the cycle's anchor AT ITS OWN FINALISATION** (B.12 A-3),
  never in a batch at the end. Operator (translated from Russian): "Intents are written at
  the moment of THEIR OWN finalisation, not the review's finalisation".

  The write is a `Document` node `contained_in` the cycle's `DevelopmentCycle` anchor,
  carrying:
  - `text` — the **full text**, not a path. The graph carries substance; a path is
    unreadable to anyone without the repository, and the parties that read these documents
    later — a reconciliation pass, a future session, the server resolving a comparison —
    have none.
  - `kind` — `intent_pre_review` or `intent_post_review`;
  - `review_affinity` — **on the POST-review intent alone**: the id of the review whose
    outcome it reports, which exists by then. The PRE-review intent belongs to the CYCLE,
    not to a review — it is the human translation of the subject (the spec, or the
    implementation), and the subject exists before any review does — so its affinity is
    **empty by construction**, written out explicitly. (Amended by the operator 2026-08-27,
    settling `b12-review-affinity-before-review-id`: the old rule required this document to
    carry the id of a review that did not yet exist at its own finalisation.)
  - `translates` — **on the PRE-review intent alone**: `spec` or `implementation`, the
    subject this document is the translation of. A cycle has more than one pre-review
    intent and labels are not identity; the subject is what tells them apart.
  - `repo_path` as a convenience, never as the identity.

  **Why the timing is a rule and not a habit.** The moment of writing IS the datum: "which
  documents did this decision stand on" is answered by comparing timestamps, taking one
  version per document — the latest created before the decision. Four documents written a
  minute apart at the end make that comparison meaningless. When this rule is violated the
  mechanism does not break visibly; **it starts lying**. And nothing checks it — no server
  observes when a document was really written — so it holds because it is followed.

  A later edit is an ordinary node version. The repository file remains, and remains what
  the operator is handed a pointer to.
- **At finalisation, link the review's Decision to the cycle's anchor** (B.12 A-4). That one
  edge per review is what makes "which two reviews are this slice" answerable by walking the
  graph. Note the mechanical trap: a `contained_in` edge **from a Decision node is created
  sensitive** and stays invisible to traversal until it is shared into the space — without
  that step the decision is not reachable from the anchor at all, and the link reads as done
  while answering nothing.
- **Read from the store.** The post-review summary's service tail is rendered from the
  authoritative timeline — including the escalation history and the operator's decision
  responses (the contested-waiver and external-defect checks depend on them) — not from
  memory or a parallel log; if the service is in reserve mode, read the reserve log.
- **No OUTCOME graph writes here.** The intents skill *proposes* the durable outcome for
  the operator to accept; the actual graph-sync happens on finalize, after the operator
  accepts (no outcome sync inside the review). The acquisition path above is not an
  exception to this: `remember_preference` and the relayed `resolve_domain` write an
  operator preference, which outlives the review and is independent of its result — they
  are two of the three writes the development loop sanctions inside a review, and neither
  is a review outcome.
- **Translate to the operator's altitude.** Everything in these two documents is pitched in
  the operator's competence and register, read from the profile: consequences in their own
  domain, abbreviations expanded on first use, no infrastructure or security jargon. The
  operator does not read the spec — that is the entire premise of the genre.
