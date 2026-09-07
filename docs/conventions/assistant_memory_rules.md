# Assistant Memory Rules

> An operating spec for one or more agents (Claude, Codex, …) collaborating over a **shared** long-lived
> assistant memory (the knowledge graph + the file-based memory). Written to be **project-agnostic**.
> Structure:
> - **Part I — Core**: always on, applies to any long-lived project.
> - **Part II — Plugins**: composable, opt-in capability bundles (a project attaches only what it has).
> - **Part III** is not shipped: it held the author's own project instance and working backlog (marked NOT canon in the source).
>
> `[home: …]` tags mark where each block eventually lands: `file-feedback` = behavioral rules loaded
> every session (must be *followed*); `graph-Document` = the canonical self-contained reference living
> in the graph. **v2.1 (2026-07-09)** — dogfooded on a commercial ML project, hardened over critic rounds; it
> now evolves through the maintenance pass (§14), not ad-hoc edits. **v2.2 (2026-07-12)** — the
> Core entry-points rule (§9/§16) + four stateless-client clarifications (scheduled/delegated
> runtimes, client-vs-agent distinction, platform product terminology, transient-block diagnosis),
> each born from a documented incident (P9 batch). **v2.3 (2026-07-15)** — new Plugin
> `operator-profile` (cross-agent operator interaction preferences: collection with an epistemic
> gate, controlled `domain` vocabulary, two storage axes `status`×`standing`, the guarded
> self-installing repo bootstrap, sensitivity gate); server capability deployed to prod before this
> section published (rollout-ordering invariant). **v2.4 (2026-07-15)** — the human-readable rendering
> genre unified across the semantic map and the Intent Summary (§7: one genre in three tenses, a delta
> section on the intents, the map standing BESIDE the post-review intent rather than superseding it, a delta
> section that opens every re-read, completeness-by-decisions / freedom-from-mechanics, cold grounding
> from the code); the private-graph / public-git surface split (§2 + §9: process artifacts may be
> gitignored, graph-sync stays the sole durable carrier of the promise); a new §11a (a rule carries its
> reason, tagged **technical** or **principled**); and a sharpened Plugin `review` critic model (the
> critic sees and reads the **whole** repository in any mode — reach is a focus of attention, not a
> read-fence; provenance ≠ authority; an external defect is an **operator escalation**, not a finding
> class). *(The design intended this as "v2.3", but v2.3 was taken by `operator-profile` first — so
> this batch lands as v2.4.)* **v2.5 (2026-08-24)** — §7 rules 4-6 reworked and one v2.4
> claim REVERSED: the semantic map is written **blind** (given no account of the work at all,
> not merely "cold"), it does **not** `supersede` the post-review intent but stands **beside**
> it — the pair is what localises whether a hole is in the implementation, in the
> self-description, in the design, or in how the spec was summarised to the operator — it takes
> no delta against documents it has never read, it raises no findings and cannot reopen a
> converged review, and a **spec cycle produces no map at all**. The comparison against the
> intents moved to a separate pass whose reader is allowed to see them. Operator rulings of
> 2026-08-23/24, implemented in review-loop slice B.11. **v2.6 (2026-08-26)** — the
> **development cycle** becomes a thing the memory knows: a `DevelopmentCycle` anchor owning both
> of the slice's reviews, its four Intent Summaries, its spec and the LITERAL transcript of its
> two free-form phases, each document written at its own finalisation carrying its type and its
> review; §9's "the graph is the sole durable carrier" corrected to owe that sync to the intents
> and the transcript and not to decisions alone; a comparison reads its document set FROM THE
> ANCHOR (filename derivation forbidden); every item the loop puts to a human carries a
> four-part **projection** recorded beside the machine item verbatim; and two process lessons
> promoted from unread graph nodes into the canon — adjacency is not connection, and "reuses the
> existing mechanism" without a written contract is a second implementation in ambush. Operator
> rulings of 2026-08-25/26, implemented in review-loop slice B.12. **v2.7 (2026-09-02)** — Plugin
> `review` REBUILT after the B.15 post-mortem (a 17-round run with 54% descendant findings): the
> review loop now runs on instruction **pair v1** (source of record: `docs/review/critic.md` +
> `docs/review/development.md`, RU) carried by the **`review_harness`** package — full semantic
> coverage each pass, the WHAT/HOW boundary tested by the wrong-result scenario, a trend line
> (new territory vs descendants) on every pass, three stop paths with the **operator alone**
> declaring finalization, finding levels (level 1 belongs to the operator and is never closed
> inside the pair), three trust levels of decisions (operator depth rulings close whole finding
> classes), and the run summary folding into the graph as a Decision on the cycle anchor. The old
> service-side review flow is **legacy** (read-only) until removed after the first live harness
> run. Pair v1 converged on itself in 3 rounds, on its spec in 3, on the harness code in 22
> (operator finalization 2026-09-02).
>
> An agent **recalls before it acts** (§17); a project is **bootstrapped once** against the shared
> backend, by any agent (§18).

---

# PART I — CORE (project-agnostic, always on)

## 1. Two stores and the boundary  `[home: file-feedback]`

Two stores; confusing them is the primary error.

- **The graph** — granular **domain knowledge**: how things work, what was decided and why, incidents,
  hypotheses, experiments, requests. Goal: **self-contained** so "how did we solve a problem like X"
  works **without repo/source access**, from another project.
- **The files** (`MEMORY.md` + `*.md` notes) — (a) `feedback` = how I should work; (b) `user` = who
  the operator is; (c) `project`/`reference` = live operational status + external pointers.

**Boundary:** "how the domain / a decision works" → graph. "how I work / who / what's happening right
now" → files. Live status is *also* mirrored in the graph (§9-status). A client with **no persistent
local store at all** cannot hold this file layer — see Plugin `stateless-client` (Part II).

## 2. Self-containment  `[home: file-feedback]`

A link from the graph into the source repo/docs leads nowhere from outside, and an external consumer
never sees the linked artifact. So **the graph carries the SUBSTANCE**, not a pointer. `provenance` /
links are an *addition* for those who have the source, never a replacement. Applies to every artifact,
including semantic maps (which live in the repo — the graph cannot "reference and forget").

**Two memory surfaces — private and public.** A project may keep memory on **two** surfaces: the
**graph is private** (the whole process — including what was rejected, the deltas, the rationale) and
the **repository/git is public** (the result that ships). A fact's absence from the public surface is
often **deliberate**, not an omission: process artifacts live only in the private one. This *strengthens*
self-containment rather than weakening it — the graph does not duplicate the repo, it holds what the
public surface intentionally leaves out. (Consequence for timing / gitignored artifacts: §9.)

## 3. Granularity  `[home: file-feedback]`

- **One node = one idea.** Tests: would someone pull this out on its own, in another project? Does it
  have its own "why"? **Tie-breaker:** if two facts are always used together and neither has
  independent value, they are ONE node. It stays a heuristic — do not over-formalize.
- **Thin hub + atomic children** is the general organizing pattern: a hub node holds orientation, the
  real content lives in atomic children. A big document = a thin anchor node + atomic children. (The
  concrete "hub = a subsystem Component" is a plugin specialization — see Part II.)

## 4. Core node taxonomy  `[home: graph-Document; summary → file-feedback]`

Project-agnostic types (plugins add more — Part II):

- **Note** — a fact / rule / contract / observation. Its standing is carried by `status` (§6): an
  established fact vs a raw observation vs a provisional claim.
- **Decision** — one ruling. Fields: `decision` + `why` + `lesson` (where portable) +, **where a real
  fork existed**, `alternatives` / `rejected` / `tradeoffs` / `consequences` (the "ADR content"; no
  separate `ADR` type).
- **Hypothesis** — a conjecture (§6). First-class, with its own lifecycle and `status`.
- **Experiment** — an empirical investigation that chose/rejected by measurement (§6).
- **OpenQuestion** — an explicitly unresolved, parked design question.
- **ExternalRequest** — an incoming ask/directive from outside (colleague, stakeholder, operator) that
  drives work. Not knowledge — a stimulus.
- **Incident** — a failure with a lifecycle: `symptom` / `root_cause` / `fix` / `lesson` / `status`.
- **Idea** — a project-agnostic canonical principle over multiple instances; used **sparingly** (only a
  principle recurring ≥2–3× that benefits from a canonical statement + an instance hub; when unsure,
  don't — a `lesson` + a `pattern:*` tag suffices).
- **Document** — a thin anchor for a semantic map or external doc (`path`, `scope`, `refresh_note`).
- **ProjectState** — the live operational state of an effort, mirroring a `project_*.md` file (§9).
- Roots: **Project**, **Person**, **Tag**.

## 5. Core edge taxonomy (direction src → dst)  `[home: graph-Document; summary → file-feedback]`

- **contained_in** — the tree, one parent (child → hub, hub → Project).
- **depends_on** — by dependency/flow (consumer → producer; a blocked OpenQuestion → its unblocker).
- **references** — "uses / rests on / cites" (hub → its Document; cross-cutting node; Idea → instance).
- **supersedes** — new → old (evolution / retraction).
- **caused_by** / **fixes** — incident semantics.
- **relates_to** — weak association / contrast.
- **tagged_with** — any node → a Tag (project tag or a `pattern:*` tag).
- **informs** — an Experiment → the Decision its evidence justified (investigation → ruling).
- **motivates** — a stimulus (`ExternalRequest` / `Hypothesis`) → the work it produced
  (`Experiment` / `Decision` / `Task`). Query: "what did this request/hypothesis lead to."

## 6. Epistemic model — kind × status  `[home: graph-Document; summary → file-feedback]`

Two **orthogonal** axes, so a year later you can still answer "is this an established fact, or do we
think so, or was it overturned?".

**Kind (the node type)** — captures the *nature* of the knowledge and its lifecycle:
`Note`(fact/observation) · `Decision` · `Hypothesis` · `Experiment` · `OpenQuestion` · `ExternalRequest`.

**`status` (a small field on the node)** — captures the *current standing*, default `current`:
`current` (in force) · `provisional` (tentative / a raw observation, not yet confirmed) ·
`superseded` (replaced — with an edge recording what replaced it: `supersedes` for evolution of the
same thing, or a plugin-declared displacement edge, e.g. the skills plugin's `displaces`) · `disputed` ·
`rejected` (with `rejected_reason`).

**Lifecycles:**
- `ExternalRequest` → `motivates` → Decision / Experiment / Task.
- `Hypothesis` → `motivates` an `Experiment` that tests it, OR opens an `OpenQuestion`, OR is
  **rejected**. Its `status` moves `open → testing → supported | rejected | superseded`.
- **A REJECTED hypothesis/proposal is RECORDED, not deleted** — with its `rejected_reason`, stage
  (in discussion / by experiment), and who/when. Recording the rejection is the whole point: it stops
  the idea being re-proposed and re-argued. (Mirrors Decision `rejected` fields + Experiment negative
  results.) See §8 for the discriminant.

**Status transitions.** Safe, evidence-backed changes are autonomous, with provenance: `→ superseded`
(mechanical — when the superseding node is created, with a `supersedes` edge) and `→ disputed` (a
reversible flag — surface it immediately). Confidence-raising or final changes — `provisional → current`
and `→ rejected` — need a **confirming event** (an Experiment result / verification against ground truth
/ operator confirmation). Default when unsure: **propose, don't flip.** Where whether a transition may be
done autonomously is itself unspecified, resolve it by the first-encounter rule (§11).

## 7. Human-readable rendering (semantic map & intent)  `[home: file-feedback]`

A **universal principle** (not domain-specific): a person reading top to bottom must be able to *see*
what a thing means and decides, without reading the code or the spec. This is **one genre**, appearing
in **three tenses** over the life of a change:

- the **pre-review intent** of a spec ("what we are about to build"),
- the **post-review intent** of a spec ("what we will finally build" + a delta),
- the **semantic map** after implementation ("what was built" + a delta).

They are the same kind of document — the readable projection at the level of *meaning* — differing only
in what they are grounded on and when they are read. Normative rules of the genre:

1. **Complete on decisions, free of mechanics.** The document is complete on every resolved fork and
   every rule that binds the system's behaviour; it is free to omit the machinery (tables, indexes,
   data structures, files). Test: *"in this situation the system does that"* → belongs in; *"field X of
   type Y with a partial index"* → does not.
2. **The one exception — a declared constraint.** Machinery enters the document when it is the *answer
   to a declared constraint* (any frame: disk, tokens, latency, cost, privacy, a size limit; declared
   by the operator in the moment OR by project rules as load-bearing). Then the machinery is itself a
   decision with its own "why", rendered at the level of meaning ("we stage into a temp table to avoid a
   second full pass"), as non-technically as the constraint allows.
3. **Style.** Plain continuous prose; **every abbreviation expanded on first use**; code/`§`/commit
   pointers collected in one closing "where this lives" section, never woven inline; bilingual where the
   operator works bilingually (a source-of-record copy + a canonical copy, kept consistent).
4. **Delta section (opens every re-read) — for the INTENTS.** A document of this genre that is read
   *again after a gate* — the post-review intent — **opens** with a "what changed in meaning since the
   last reading" section, at the front, not the tail. This is not a findings ledger (that is
   bookkeeping); it is a substantive account: "I meant it this way; it turned out wrong; now it is this
   way." **The map is exempt, and the exemption is the point:** it has never read the intents, so it has
   nothing to take a delta against (rule 5).
5. **Two grounds, and the map is BLIND, not merely cold.** The intent is decompiled from the **spec**;
   the map from the **code at the converged commit** (Core §10).

   "Cold" was the old word and it is too weak. The map is written by a party that is given **no account
   of the work at all** — no intents, no review traffic, no findings. Its independence is the product:
   a reader who has seen the review's own account cannot help reproducing it, and a reproduced account
   is exactly what the operator already read in the intent. The spec is available to it as an aid to
   *meaning* and may **never be the ground of an assertion**: every statement in a map rests on a code
   address.

   **The map does NOT `supersede` the post-review intent.** It stands **beside** it, and the pair is
   what makes a defect locatable — operator ruling of 2026-08-23: «Лучше пусть рядом постоит. Так можно
   восстановить, на каком этапе дыра появилась.» The intents agree and the map differs → the defect is
   in the implementation; the map matches the code as designed but the post-review intent misdescribes
   it → the defect is in the self-description; all three agree and the operator still objects → the
   defect is in the design; and the worst address of all — the map contradicts the post-review intent OF
   THE SPEC while the code faithfully implements the spec, meaning the spec was summarised to the
   operator as something it was not. Superseding erases the second document every one of those readings
   needs.

   The comparison itself is not lost: it is a **separate pass**, run by a reader that IS allowed to see
   the intents and can therefore quote them, and its output is a list for the operator rather than a
   delta inside the map.
6. **The map is the operator's instrument, not the loop's.** It raises no findings, enters no ledger, is
   owed no disposition, and cannot reopen a converged review. Operator ruling: «Карта ничего не
   переоткроет. Это мое и только мое решение.»

The rendered document is the readable long-form; the graph node is the compact index; both carry the
substance (self-containment, §2). *(Timing — when each tense is written — is §9.)*

## 8. When to write / When NOT to write  `[home: file-feedback]`

**Write when:** the substance is durable and has a future reader — a decision, a fact, an incident with
a lesson, an experiment (incl. negative results), a hypothesis with re-litigation risk, an external
request, an unresolved question.

**Do NOT write:**
- one-off discussions and intermediate thoughts with no future value;
- transient ideas that got **no substantive evaluation** (a passing thought nobody engaged);
- restatements of what a doc / repo / git already records (unless something non-obvious was learned —
  then store the non-obvious part);
- anything derivable on demand or purely ephemeral;
- anything with **no plausible future reader**.

**The discriminant for rejected ideas is RE-LITIGATION RISK, not "rejected".** If an idea was
substantively considered and turned down, the rejection + reason is high-value memory (§6). Skip only
ideas that never got real engagement. Long-lived memory degrades from low-value accumulation far more
than from bad structure — negative criteria matter as much as positive ones.

## 9. Timing, and durable-vs-live status  `[home: file-feedback]`

**Timing (graph):** (1) NOT inside an iterative-review cycle. (2) On completion of a review — record the
changes. (3) On any change to any doc/spec/map — reflect the substance into the graph (self-containment).

**Timing (human-readable rendering — map & intent):** (1) NOT inside a review cycle. (2) The
readable rendering follows the change through its tenses (§7): the **pre-review intent** before the
review, the **post-review intent** at convergence, the **semantic map** once an implementation exists.
Both intents obey the two genre rules of §7 (a delta section on re-read; grounded cold on the spec).
(3) On any code/spec/doc change. *(A review is a quiet zone for the rendering; the trigger is any change
to the artifact.)*

**"A pure spec gets no map" — clarified.** A spec with no code yet still gets its readable rendering:
the **post-review intent is that rendering** — de-facto the map at the "no code yet" stage. There is no
*separate* semantic map until code exists; when it does, the map (written blind from the code) stands
**beside** the post-review intent rather than replacing it (§7 rule 5), and the pair is what localises
where a hole appeared. "No code yet" is never a reason to withhold the readable rendering — only to call
it the intent rather than the map.

**And a spec cycle produces no map at all.** Operator ruling of 2026-08-23: «по окончанию спеки делать не
надо - после код ревью». A map of a spec would be a model reading a text against a model's summary of the
same text — one medium, weak independence, poorer yield. The map of the CODE checks the spec for free, by
way of the fourth reading in §7 rule 5.

**Graph private, git public — the durable carrier of a promise is the graph.** Because the graph is the
private surface and git the public one (§2), process artifacts — the intents — **may be gitignored** by
project rules. This does **not** cancel the finalize-time graph-sync of decisions; on the contrary it
makes the graph the **sole durable carrier** of what was promised. A gitignored intent that is never
graph-synced leaves no durable record at all — so for a change with gitignored process files the
finalize graph-sync is not optional, it is the only place the promise survives.

**And what is owed that sync is not only DECISIONS.** This paragraph declared the graph the sole durable
carrier while the sync it named covered decisions alone — so the two documents that carry the promise in
the operator's own words, the **Intent Summaries** and the **literal transcript of the cycle's free-form
phases**, fell outside it and were durable nowhere. They are inside it now: each is written to the
cycle's anchor, **in full text rather than as a path**, at its own finalisation (see Plugin `review`).
A path is unreadable to anyone without the repository, and the parties that must later read these
documents — a reconciliation pass, a future session, the server resolving a comparison — have none.

**Two clocks — do not conflate them.** Recording **decisions/facts** into the graph fires on
review-completion / any doc-spec change **regardless of whether code exists yet** — record them as
`provisional` (§6) until a confirming implementation/result. Only the readable **map** waits for an
implementation. "No implementation yet" is **never** a reason to withhold a decision from the graph —
that just offloads the catch onto the operator.

**Status split by change-speed:**
- **Durable / architectural** → the hub's `current_version` (current version, wiring, "reached
  milestone"). Dated. Updated on a rework / registration / milestone.
- **Live / operational** → a `project_*.md` file **AND** a mirroring `ProjectState` node, updated in the
  **same motion** (that is how drift is prevented — not by keeping status out of the graph).
- Rule of thumb: **changes on a tick → file + ProjectState; changes on a registration/rework → hub.**
- Incidents: transient run-incident → short file report (+ ProjectState); incident with a reusable
  lesson → an `Incident` node.

**Entry points move with the meaning (same-motion).** When a unit's **positioning / canonical
meaning / key framing** changes (not a routine status tick), update its **entry points in the same
motion** as the content: the hub's `summary` (`Project`/`Component`), and — for an agent with a local
file store — that agent's own session-start record. Recall reads the hub first (§17), so a canonical
shift recorded only as a child node **never reaches the next reader**: correct content parked where no
reader looks first is a silent-staleness bug, not a completed write. Test question before closing the
write: *"if the next session reads only the hub and its own start-up context, does it get the new
key?"* (Motivating instance, 2026-07-11/12: a confirmed canonical repositioning was recorded as a
child `Idea` node while the `Project` hub summary and the agent's local memory kept serving the old
framing — caught by the operator, not by the rules.)

## 10. Source of truth  `[home: file-feedback]`

The truth is the **authoritative ground artifact of the domain**; verify against it before asserting,
and trust neither a stale map nor old memory. The ground artifact varies:

- engineering → the code on HEAD (+ the live schema/warehouse);
- research → the data / experiment results;
- product / design → the current design doc / decision of record;
- legal / knowledge base → the primary sources.

When there is no single ground artifact, say so and record the claim as `provisional` (§6).

## 11. Extending the taxonomy & resolving forks  `[home: file-feedback]`

**Prefer an existing type/edge.** A new node/edge type is justified only when it is a genuinely distinct
*kind* that (a) recurs, (b) existing types force you to misuse, and (c) enables a query you otherwise
cannot make. Adding one is a deliberate act, documented here. Same discipline as the `pattern:*`
vocabulary (§15). Abuse mode to avoid: overloading `Note`/`Decision` for everything, OR uncontrolled
type sprawl.

**Resolving an unspecified policy fork.** The first time a real fork appears that this ruleset does not
pre-decide — a plugin-vs-plugin collision, an autonomy question (e.g. a §6 status transition), an
ambiguous cleanup call — **ask the operator, record the choice as a project-scoped policy, then apply it
automatically thereafter.** The operator may change the decision for later cases at any time. Core
invariants are not subject to this — they always win. **Record every such policy in one project-scoped
policy register** (clearly marked project-local, never Core), each with its date and the fork that
created it — so "architectural invariant" vs "historical local choice" stays answerable; the maintenance
pass (§14) reviews them.

## 11a. Rule provenance — a rule carries its reason  `[home: file-feedback]`

When you record a rule / invariant, record its **reason**, and tag the *kind* of reason:

- **technical** — it follows from a limitation of the current tool / implementation. When that
  limitation goes away, the rule is **revisited whole** (not propped up with exceptions).
- **principled** — it follows from the nature of the thing. Do not touch it.

A rule without a reason will be **completed by the next reader with an invention** — which they will
then defend as if it were the original ground. (This is not hypothetical: a fabricated *epistemic*
justification for the review plugin's "no graph writes" invariant was caught in review; the true reasons
were two *technical* ones.) Meeting a rule with no reason, an agent does **not** invent one — it **asks
the operator** and records the answer.

## 12. Writing a node  `[home: file-feedback]`

- **Label specific, searchable by meaning** ("Silver-driven refund: no REFUND webhook", not "decision 3").
- **Properties = a dense but SELF-CONTAINED retrieval unit** (abbreviations OK for search/traversal;
  dense ≠ empty pointer — understandable without the source). This is the difference from the
  semantic map (readable long-form).
- **`provenance` always** (`file:line` / commit / `§` / source ref).
- **`lesson` where portable** (a takeaway with no project jargon).
- **`status`** where it isn't the default `current` (§6).

## 13. Write discipline  `[home: file-feedback]`

- **Search before writing** (dedup) → `update`, not a duplicate.
- **Supersede, don't delete** (keep history); delete only a fact that turned out to be wrong.
- Retry a transport-failed write (e.g. gateway flake) with the exact same call — a failed call left no
  partial write.

## 14. Maintenance & decay  `[home: file-feedback]`

Long-lived memory needs upkeep, not just intake:

- periodically **consolidate** duplicates and near-duplicates; **prune** stale/low-value nodes;
- **collapse** `supersedes` chains (keep the head + the lineage, drop noise);
- **revisit `status`**: promote a `provisional` observation to `current` when confirmed, flip to
  `superseded`/`rejected`/`disputed` when overtaken; resolve stale `OpenQuestion`s;
- a `hypothesis` left `open` for a long time is a review trigger (test it, park it explicitly, or reject
  it with a reason).
- **review project-scoped policies** (§11) — promote one to Core if it has proven general across
  projects, retire it if obsolete, else keep it local; this stops first-encounter choices silently
  accreting into a "shadow Core".

**Cleanup policy — conservative, default-to-keep.** `prune` (delete) only the **demonstrably wrong** or
an **exact duplicate**. Low-value / stale content is **demoted** (via `status`) or **consolidated**, not
deleted; consolidation merges duplicates while preserving provenance. Deleting non-duplicate content
needs **operator confirmation** (writes are audited / undoable). When in doubt, keep + flag. The precise
signals for "low-value / stale" are deliberately left to accumulate from practice — the invariant is the
**keep-bias**, not a fixed checklist. As the system grows, this maintenance policy may graduate into its
own spec.

## 15. Pattern layer — cross-project findability  `[home: graph-Document; summary → file-feedback]`

- **`pattern:*` tags = a controlled vocabulary** (a small fixed set), **project-agnostic** (NOT under any
  project tag — that is the point), tagging any node whose `lesson` instantiates the pattern. Adding a
  tag is deliberate (sprawl kills findability). This is the primary cross-project index.
- **`Idea` nodes** carry the canonical statement of a recurring principle, `references` its instances —
  used sparingly (§4).
- `lesson` strings stay on nodes; tags + Ideas are the index layer on top.

## 16. Recipe (per unit)  `[home: file-feedback]`

1. Read the unit's map; verify freshness against the ground artifact (§10).
2. Fresh → write directly. Status header stale → targeted patch. Diverged in substance → full rewrite.
   Touched an implemented unit with no/dense map → write it readable (§7).
3. Sync the `Document` `refresh_note`, then the nodes. If the unit's **canonical meaning shifted**,
   update its entry points in the same motion (§9): hub `summary` + the agent's session-start record.
4. Create: thin hub → `Document` → atomic children (Decision / Note / Incident / Experiment /
   OpenQuestion / Hypothesis / ExternalRequest).
5. Link per §5 (+ plugin edges).
6. Report to the operator with the tree + cross-locks.

*(Timing gates in §9; discipline in §13 — not repeated here.)*

## 17. Reading / recall  `[home: file-feedback]`

Memory is useless unless consulted at the right moments. **Reading precedes acting** — recall before you
derive, decide, or claim.

**Recall is not once-per-session — re-run it on every TOPIC SHIFT.** When the conversation crosses into
an adjacent subsystem (especially cross-cutting shared layers: scheduling / watermark / demand /
registry), search the graph anew under the **new** topic *before* proposing "let's build X" — it usually
already exists. Narrow, one-time recall at session start is the classic miss.

**When to recall:**
- **Starting on a unit** — pull its slice (current state, decisions, open questions, known incidents +
  experiments) *before* acting; don't re-derive or re-litigate what is already settled.
- **Before a decision** — is there an existing `Decision`, a `rejected` alternative, or an `Experiment`?
  ("have we decided / tried this?"). The read-side twin of §8's anti-re-litigation.
- **Before a proposal** — check `OpenQuestion`s and **rejected `Hypothesis`es**; don't re-propose what
  was already turned down.
- **Before investigating a failure** — check `Incident`s ("has this happened?") + `Experiment`s.
- **Before asserting a fact** — check its `status` (current / superseded / disputed); re-verify against
  ground truth (§10) if the claim is load-bearing.
- **When the user references prior work** ("like we did in X", "the thing we decided") — recall it.
- **Cross-project** — search `pattern:*` for "how was a problem like this solved elsewhere."

**How to recall:**
- **Search first** (semantic + full-text) for entry points; then **traverse** — `contained_in` for the
  unit's subtree, `depends_on` for lineage, `informs` / `motivates` for rationale, `supersedes` for
  currency. Read the hub first (orientation), then the specific children. Fetch by relevance — do not
  load everything.
- Prefer the graph for a decision / rationale / state; go to the **ground artifact** (§10) for current
  implementation detail.

**Trust on read** — a recalled node reflects what was true **when written**:
- check `status` + date; for a load-bearing claim, **re-verify against ground truth** (§10);
- if a node names a file / function / flag, confirm it still exists before acting on it;
- **synthesize the conclusion for the user — do not dump raw nodes.** The graph is the working store,
  not the user's reading material.

## 18. Onboarding a project (agent-agnostic)  `[home: file-feedback]`

The backend is **shared across agents** (Claude, Codex, …) — one graph. So a project is bootstrapped
**once**; every agent then reads/writes the same store under this ruleset (the ruleset is the shared
contract that keeps writes consistent). Write each step at the level of a **capability**
(create-typed-node · link · search · file-note) so any agent maps it to its own tools.

**Procedure (once per project):**
1. **Discovery** — the project root + its ground artifact (§10); confirm the shared backend is reachable
   and detect what the agent can do (typed graph vs file-only — degrade types to frontmatter/links if
   needed).
2. **Roots** — ensure a `Project` node, a `Person` (operator), a project `Tag`.
3. **Attach plugins** — scan the project and attach what applies: `database` (a data store?),
   `components` (large subsystems?), `versioned-pipeline` (a versioned execution substrate?). Register a
   plugin's types/edges once (shared thereafter).
4. **Seed structure** — for each substantial unit: a thin hub → its `Document` (a readable semantic map,
   if implemented, §7) → atomic `Decision` / `Incident` / `Experiment` / `OpenQuestion` mined from docs +
   code + history (recipe §16). Delegate large extraction to a subagent if available.
5. **Wire** — `contained_in` tree under `Project`; `depends_on` cascade; `produces` / `consumes` lineage
   (database plugin); cross-locks.
6. **Backfill the "why" from history** — decisions / incidents / experiments not visible in code (git
   history, superseded docs), each verified against the ground artifact on HEAD.
7. **Operational state + working agreement** — `ProjectState` mirror(s) for active efforts; record the
   behavioral subset of this ruleset so every agent and session follows it. *(A stateless client — no
   local store, no session hook — cannot do this; see Plugin `stateless-client`, which shifts the
   enforcement onto the platform's own customization layer.)*

**Idempotency & shared use:** the backend is shared, so onboarding is idempotent — **search before
creating** (§13); a second agent that finds the project already onboarded just uses it; registering a
type/edge that already exists is a no-op.

As it grows, this onboarding procedure may graduate into its own spec/skill (mirroring §14).

## 19. Project zones & cross-project writes  `[home: file-feedback]`

Each project owns its **zone** (its `Project` subtree). **Do NOT write into ANOTHER project's zone
without explicit operator permission** — work in the project of the current task. A foreign project
hand-writing nodes into another's subtree is the anti-pattern (a service writing to another service's
tables instead of calling its API).

The **one sanctioned cross-project write** is **feedback about these conventions / memory-behavior**: use
the `feedback` capability (a mis-application, ambiguity, gap, or suggestion). It lands a `provisional`
`Feedback` report in the conventions owner's zone for the §14 triage — never hand-write an `Incident`
into the memory project yourself. Same spirit for other projects: contribute through a **published
capability**, not by reaching into the subtree.

---

# PART II — PLUGINS (composable, opt-in)

A plugin is a bundle of {extra node types, extra edge types, extra rules} for one capability. The Core
is always on; a project attaches only the plugins it has. Adding a plugin follows §11.

A plugin is **structural** (adds node/edge types + rules) or **behavioral** (modifies write / update /
visibility rules — privacy, audit, multi-operator, sync — possibly with no new types). **The Core is
inviolable**: a behavioral plugin may not override a Core rule. Plugins are **additive** and must not
silently alter each other; a genuine collision is resolved by the first-encounter rule (§11).

## Plugin `database`  `[home: graph-Document]`
*For any project with a data store (present in almost all).*
- **`Table`** node — a physical/logical table/view, self-contained to browse: `fqn` · `kind`
  (main | audit | view | pointer | catalog) · `purpose` · `grain` · `key_columns` · `notable_columns` ·
  `partitioning` · `notes`. Points to a data-dictionary page, does not depend on it.
- **`produces`** (owner → Table) + **`consumes`** (reader → Table) = table-grain lineage.
- Tables are NOT in the containment tree — the owner is `produces`; they float, reached by lineage + tag.

## Plugin `components`  `[home: graph-Document]`
*For any project with large subsystems (more than half of them).*
- **`Component`** node — a thin hub for a subsystem/layer/module (role, code_location, `current_version`
  status, key facts + limitations). Children `contained_in` it.
- Cross-component dependency via `depends_on`; a subsystem-level "one semantic map per Component"
  specialization of §7.
- `current_version` holds the durable architectural status (§9).

## Plugin `versioned-pipeline`  `[home: graph-Document]`
*For projects with a versioned execution/orchestration substrate.*
- Concepts: algorithm-version vs pipeline-version (code vs bindings), watermark cursor, registration,
  rollback, orchestrator run states, producer/consumer pv dependency.
- `current_version` records the current av/pv + lineage; registration/rework is the milestone that
  updates the hub (§9).

## Plugin `stateless-client`  `[home: graph-Document]`  — **behavioral**
*For any agent/client that lacks a persistent local file store and/or a session-start hook — e.g. a bare
MCP/OAuth connector (ChatGPT-style) rather than an agent with its own CLAUDE.md/MEMORY.md + hooks
(Claude Code-style).*
- **"Memory-connected client" ≠ "memory-backed agent".** A client counts as a *memory-backed agent*
  only if its runtime provides all three: (a) durable storage for the operating contract, (b)
  **guaranteed** reachability of the memory tools in every execution context where a **memory-dependent
  obligation** runs or is delegated — a recall, a write, acting on memory-derived state (a context with
  no such obligation is out of scope of this test), and (c) an enforceable recall-before-acting hook in
  each such context. Anything less — including a bare
  chat client with opportunistic MCP access — is a *memory-connected client*: for it the external
  memory is advisory, not a dependable cognitive substrate, and nothing that MUST happen (a guaranteed
  recall, a memory-dependent scheduled job, a required write) may be entrusted to it alone.
- **§1/§18 override.** Such a client has no "files" store to hold the behavioral subset (§1) or the
  working agreement (§18 step 7). Whatever the host platform's own memory/personalization feature holds
  is **NOT** this ruleset's file store — treat it as ordinary user data, never as compliance with §1 or
  §18, unless freshly re-verified against the graph.
- **Enforcement lives at the layer where the client decides to reach for a tool — which may be a
  *global* layer, not the project/context one.** Empirically (ChatGPT, 2026-07-04) a detailed
  context/Project-level prompt did **not** make the client attempt tool discovery at all; the decision
  "reach for a tool?" was governed by the platform's **global** custom-instructions layer. So the
  discovery-first / tool-first rule must be installed **globally** as a short, general rule ("if a
  request may involve a tool/connector/MCP, try discovery before answering; never claim an integration
  is unavailable until discovery or a real call has failed this turn"), while the detailed memory
  contract lives in the project/context layer. Ship **both**: the global rule makes the client *try the
  tool*, the context prompt makes it *work with the graph correctly* — neither alone sufficed.
- **§17 enforcement shifts onto the platform's customization layer, not the server.** No hook can inject
  a reminder before a stateless client acts, so the operator must add a standing instruction at the
  platform's own customization surface (e.g. ChatGPT custom instructions) that: (a) calls `conventions`
  at the start of every conversation **and on every turn that touches memory**, treating it as binding
  for that turn — there is nowhere to cache it, and a version loaded a few messages ago must not be
  relied on; (b) calls `search`/`traverse` before answering **any** question that could be project/domain-specific,
  not only when explicitly told to — a list of example triggers (terminology, decisions, incidents,
  status, "how did we do this before") is illustrative, **never exhaustive**; (c) never concludes a tool
  is absent from a partial/self-reported tool list, **unconditionally** — regardless of whether the
  operator asserted the tool exists — and matches a tool **by meaning/purpose**, not exact bare name,
  since the same capability may appear under a different namespace/prefix per client (observed:
  `feedback` surfaced as `assistant_memory.feedback` for ChatGPT vs `mcp__assistant-memory__feedback`
  for Claude). The same guard covers claims that the **connector / MCP transport itself** is
  unavailable or "disconnected" — never assert that from cached/stale connection state; re-run discovery
  (reopen the connector / `list_resources`) and attempt a real call **in the same turn** before refusing,
  and if it genuinely fails, name the **concrete error** rather than a blanket "unavailable". (This is a
  distinct failure mode from tool-in-list absence: a stateless client refused a write claiming the whole
  MCP was unavailable — from stale cached state — and only re-discovered after an operator challenge.)
- **Discovery-first / no reasoning before discovery (lazy-loading clients).** Where capabilities appear
  only after a discovery call, **absence of a capability from the current turn's visible list means
  nothing** — (re)run discovery before any memory access, on every such turn and again whenever a needed
  capability isn't currently loaded, never carrying availability over from a prior turn. Do **no**
  reasoning about MCP/tool/graph state or write-ability *before* that discovery (no explaining,
  hypothesizing, or proposing workarounds first). Forbidden until a fresh discovery: "the tool was here
  and vanished", "the connector was lost", "MCP is off", "no memory this turn", "I don't see it so it's
  absent". An operator asserting the connector should work is a **strong signal to re-discover, not to
  argue**.
- **Scheduled / delegated execution runs in a DIFFERENT runtime — never assume it inherits the
  connectors.** Empirically (ChatGPT Scheduled Tasks, controlled A/B experiment 2026-07-04): tasks
  created from a Project chat and from a normal chat both execute in a separate runtime with **no
  access to the user's MCP/connectors at all**; project instructions, the global tool-first rule and
  explicit discovery-first task prompts cannot compensate — the connector surface simply is not there
  (this sits *below* prompt/tool-choice enforcement). Rule: a memory-dependent scheduled or delegated
  workflow needs a runtime that **actually has** connector access (an external scheduler/agent runtime,
  e.g. the backend's own actionable layer), or an explicit capability check that proves connector access
  **from within that execution runtime itself** (e.g. a dry-run task that must reach the memory before
  the real one is scheduled) — connector discovery in the *originating* session proves **nothing** about
  the runtime that will execute the task. A task that silently runs memory-blind is worse than one that
  refuses to be scheduled.
- **Speak the platform's product language in operator-facing instructions.** ChatGPT's user surface
  calls an MCP-backed integration an **"app"** (Apps), not "MCP": onboarding docs, custom-instructions
  text and troubleshooting steps aimed at that platform's users must say "connect/use the app" and must
  not assume the surface exposes or recognizes the term MCP. Keep "MCP" as the protocol/transport term
  in agent-facing prose.
- **Write-gating (on initiative, not capability).** A stateless client **is a full read/write client
  and *should* write when asked** — maintaining the operator's content (personal-domain especially:
  shopping lists, task lists, notes) is a first-class use case, not something to be reluctant about.
  **The graph is the source of truth** for anything the operator asks to remember: the client must NOT
  use the host platform's built-in memory (e.g. ChatGPT `bio`) as a substitute — write to the graph
  first, and to the platform store only if the operator explicitly asks for *that* store or the graph is
  genuinely unavailable after all rules above. Before a create (or an update whose target id is unknown),
  **search first** to update an existing node instead of duplicating (Core §13); `feedback`/`get`/update-
  by-known-id need no search. What is gated is **initiative, not capability**: it does **NOT** create/
  update/delete/link/unlink/retype graph content *on its own initiative* — only on the operator's direct,
  in-turn instruction to write (e.g. "save this", "add to the list", "log an incident"). Recall
  (search/traverse/conventions) stays proactive with no exception (previous bullet). The one proactive-write exemption: the
  `feedback` capability (§19) **may** be called on the client's own initiative — it is the sanctioned,
  low-stakes reporting channel and gating it would defeat its purpose. This is the intended design for a
  write-enabled stateless client (server-side write-denial is deliberately *not* used — it would break
  the personal-domain use case); it is plugin-scoped and stricter than Core §13 only on the WHETHER-to-
  write-autonomously axis (§13 governs HOW). Promote to Core via §11/§14 only if it proves to generalize
  beyond stateless clients.
- **Degradation transparency (don't silently weaken the graph).** When a layer *outside* this backend —
  typically the host platform's own safety/content filter — blocks or rejects a memory call, the client
  must **not silently degrade graph semantics** to route around it: dropping edge properties, swapping in
  vaguer labels, or omitting relationships just to get a call through lands **quietly-wrong** memory,
  which is worse than a visible failure. Instead: preserve intent — tell the operator the **exact class
  of blocked operation**, keep the precise metadata (in a follow-up note or via `feedback`), and treat a
  block on benign personal-domain data (kinship labels, a family member's birth/residence) as a **false
  positive to surface, not to quietly comply with** — and do **not** retry a platform-blocked call with
  altered/weakened data without explicit operator permission. Motivating instance: a stateless client hit
  platform-safety blocks on ordinary family writes (`{'relationship':'mother'}`, `{'kinship':'couple'}`)
  and worked around them with empty properties / weaker labels, landing incomplete relationship metadata.
  **Transient vs deterministic blocks are distinguished by retrying the EXACT query.** If the identical
  call succeeds on a later attempt **under materially equivalent tool/auth/connector/runtime
  conditions**, the block was context-sensitive/transient (a false positive to note), not a
  deterministic payload block — if those conditions changed between attempts, record the outcome as
  *inconclusive / environment-sensitive*, not transient. Diagnose before concluding the *content* is
  blocked, and never diagnose by retrying a **weakened** variant: that corrupts the diagnosis *and*
  violates the no-silent-weakening rule above. (Observed 2026-07-04: a benign recall query was blocked
  once, then succeeded verbatim on multiple retries in the same session — transient, not
  content-determined.)
- **Motivating instance:** Incident recording ChatGPT needing three escalating operator nudges to recall
  from the graph and to discover the already-registered `feedback` tool — the gap was entirely
  client-side (verified against `HANDLERS` in `mcp/tools.py`), not a server registration bug.

## Plugin `review`  `[home: graph-Document]`  — **behavioral**
*For any project doing LLM-assisted iterative review of an artifact (spec, code, prompt) with an
independent critic. Adds no node types — transient review state lives in the project's own store, never
the graph; only the durable outcome folds in via Core types.*

**REBUILT (v2.7, 2026-09-02).** The loop below replaces the earlier service-driven flow after the B.15
post-mortem (17 rounds, 54% of findings were descendants of the loop's own fixes). **Source of record
for the loop mechanics: the instruction pair v1 — `docs/review/critic.md` (critic) and
`docs/review/development.md` (development), in Russian** — this section is the canon summary, the pair
files are the letter. The old service-side flow (`create_review` transitions, waiver machinery,
epistemic tiers, one-shot challenge) is **legacy: read-only for old runs, drives nothing new**, and is
removed after the first live harness run (operator's word). Proven before adoption: the pair reviewed
itself (3 rounds), its harness spec (3 rounds), and the harness code (22 rounds), each finalized by the
operator.

- **Abstract roles.** *development* (produces the artifact, disposes findings) and *critic* (reviews) are
  roles, not models; the model↔role binding is machine-profile config.
- **Independence by model (invariant).** The critic runs on a *different model* from development.
  Self-review is a **degraded fallback, labelled as such**, followed by independent review or an owner
  waiver before finalization.
- **Full semantic coverage, every pass.** The critic checks the **whole** artifact against the **whole**
  description each round — every thesis, in both directions (artifact violates description; description
  no longer matches artifact) — never diff-only. A pass that skimmed is not a pass.
- **The WHAT/HOW boundary, tested by the wrong-result scenario.** The description owns WHAT; development
  owns HOW — but a HOW can be an unstated part of a WHAT. The test: would the operator, seeing the
  result, call it wrong? Then it was WHAT. Boundary disputes are legal and go to the **operator as a
  fork**; the critic honors settled boundaries.
- **Trend line, every pass (invariant).** Each pass reports findings split into **new territory** vs
  **descendants of prior fixes**, honestly. The trend is the operator's primary convergence instrument;
  development surfaces it without spin, and continuing vs stopping is the operator's call.
- **Three stop paths; the operator alone finalizes.** (1) A clean pass — zero findings on the current
  version; (2) the operator's judgment call on a visible trend plateau; (3) the operator's word,
  unconditional. The critic **recommends** stopping or continuing; development **never** declares
  convergence; only the operator declares finalization.
- **Finding levels.** A **level-1 finding** — one that touches an operator decision, privacy, or the
  threat model — is **never closed inside the pair**: it escalates to the operator and blocks
  finalization until answered. Everything else is the **working layer**: development disposes each
  finding terminally (fixed with evidence, or contested back to the critic), and every finding reaches
  a written per-finding outcome — no silent drop.
- **Three trust levels of decisions.** *Operator-understood* (the operator grasped and ruled — highest;
  the critic honors it), *delegated* (the operator delegated the call to development — the critic may
  probe the delegation's edges), *working* (development's own call — fully challengeable). **Operator
  depth rulings close whole finding classes** (e.g. "protection against corruption of the run's own
  files is deep enough; further scenarios are beyond v1 — risk accepted"), and the critic judges against
  them instead of re-raising the class.
- **Development duties (each learned by paying for it).** **Self-review before the critic** — development
  runs the critic's checklist on its own change first. **Sweep the class along ALL its axes** — closing
  the visible half of a finding's class and declaring the whole class closed is a recorded failure mode;
  the cure is naming the class's axes explicitly and mutation-testing the closure. **One investigated
  fix** — development proposes a single fix it has verified end-to-end, never an "(A)/(B) your choice"
  menu. **Ready-made first** — before inventing a mechanism, check the project and common practice for
  an existing solution; use-vs-invent is the operator's call when it matters.
- **Description deltas carry authorship marks.** The task description accretes a per-round delta section
  naming what changed and **who authored it** (operator word / development). A **semantic** change to
  the description requires explicit operator fixation — batched as a numbered list closed by one
  operator word; silence fixes nothing.
- **The harness is the standard vehicle** (`review_harness` package). It owns mechanics, never verdicts:
  a durable run catalog restored **from files only** (git-ignored under the reader criterion — hygiene,
  not defense); operator gates whose questions and answers are journaled **verbatim**; the critic
  launched as **codex exec pinned read-only** — a structural whitelist on the command template plus
  attestation from the tool's own header, silence ≠ read-only; both roles watched by a **detection
  window** grounded in *observed* gaps of the machine (machine profile), with the operator notified
  within his profiled `notify-within`; completed rounds are incombustible, the current round is
  replayable after a crash. Falling back to operator hand-relay is announced, never silent.
- **The critic sees and reads the WHOLE repository; its focus is the artifact under review.** Access and
  reading are unbounded; the critic concentrates its critique on what is under review, reads the rest as
  ground, and must read the places the artifact touches (reach = focus of attention, not a read-fence).
  The critic is sighted with graph access in this genre; as critic it writes **no** graph nodes — graph
  sync is development's job.
- **Provenance = origin, not authority.** The base is the source of truth about what the system *does*,
  not an assertion about what it *should* do; the critic doubts the base as obligation and leans on it as
  fact. A finding must **land on the artifact under review**; a defect found in the base is an
  **escalation to the operator**, not a finding of this review — dedup against the graph before raising.
- **Durable outcome on completion.** At run completion development writes the **run summary into the
  graph** — a Decision on the cycle anchor: operator decisions (incl. depth rulings), finalization, the
  trend, the commit — and the operator-exchange pairs flow to the graph through the profile buffer
  (raw material of the operator profile). Transient round state never enters the graph.
- **The development cycle has an anchor in the graph, and both of its reviews belong to it.** A cycle is
  one slice of work — from the operator's first free-form sentence to acceptance — recorded as a
  `DevelopmentCycle` node. Everything the slice produced hangs off it: **both reviews** (each review's
  Decision is linked to the anchor at finalisation), **all four Intent Summaries**, the **spec**, and the
  **literal transcript of the two free-form phases**. The cycle's commits are a property of the anchor:
  an ordered list of full 40-character shas and nothing else — a commit already has an unforgeable
  timestamp and an authoritative store, so a node per commit would be a second implementation of history
  on top of git. **The operator declares the cycle; development never infers one.** The declaration may
  be late; what is recorded as the START is the timestamp and identifier of the cycle's FIRST message,
  written onto the anchor at declaration, not the moment anyone remembered the process. **Past cycles are
  never backfilled.**
- **Each cycle document is written at ITS OWN finalisation, in full text, with its type and — per kind — its review or its subject.**
  Not in a batch at the end: the moment of writing IS data. "Which documents did this decision stand on"
  is answered by comparing timestamps — one version per document, the latest created before the decision
  — and four documents written a minute apart at the end make that comparison meaningless. The mechanism
  does not break visibly when this is violated; it starts lying, and **nothing can check it** (no server
  observes when a document was really written), so it holds because it is followed. Each document carries
  a **type label** from a closed vocabulary — `intent_pre_review`, `intent_post_review`, `spec`,
  `free_phase_transcript` — and, for the POST-review intent alone, the **id of the review it belongs
  to**; for the spec, the transcript AND the pre-review intent that affinity is written out as
  explicitly empty, because they belong to the CYCLE rather than to either review. The pre-review
  intent instead names **which artifact it translates** (`spec` or `implementation`), since a cycle has
  several of them and a label is not an identity. Amended by the operator 2026-08-27: the earlier rule
  demanded that document be written before its review existed while carrying that review's id, which is
  not merely awkward but impossible. Without the label a reading pass cannot
  tell a recorded conversation from a promise, and the two do not have the same standing.
- **The free-form phases are stored as a LITERAL transcript.** The prose turns of both sides, in order,
  extracted mechanically from the session record: not a summary, not a template, no sections. A summary
  written by the party whose renderings the comparison exists to check inherits that party's blind spots;
  and any structure imposed on the record formalises, through the back door, exactly the two phases that
  are unformalised by design. The agent's own reasoning between turns is not included — what was SAID is
  recorded, not what was thought.
- **A comparison reads its document set FROM THE ANCHOR, and deriving it from filenames is forbidden.**
  Not from a declaration on the channel either: a declaration is unverifiable by construction when most
  of the documents are files and the server has no repository. Filename derivation is not rigour but a
  guaranteed error — intent filenames are not paired, and several have no twin at all.
- **Every item the loop puts to a human carries a human projection, and the projection is a RECORD.**
  Four parts, in the operator's language: what happened in ordinary words; what it concerns (how much, of
  what, where); what is proposed and why; what each available answer will actually do, said as outcomes
  rather than as the names of fields. One record carries the machine item **verbatim beside** its
  translation, under the key of the item it explains, and the human is called from that record. **Tidied
  machine material is not a projection**: printing a table more legibly, grouping its rows, or typing its
  reasons still leaves the human reading a machine table. The default is that the operator does not read
  what is written for machines (see Plugin `operator-profile`), so a machine artifact either has a
  projection or never becomes the basis of a question.
- **Two rules for reading and writing specs, learned by paying for them:**
  - **A constraint written in the element that FORBIDS, but absent from the element that ACTS, is not a
    constraint.** The test: for every acting rule and every rule that constrains it, ask whether the
    acting one carries the constraint **in its own condition** or merely sits beside it in the same
    document. **Adjacency is not connection.** Found twice in a single review by the critic and never
    once by development.
  - **"This surface reuses the existing mechanism", with no written contract of the operation, is a
    second implementation in ambush.** Write the bridge as the contract of ONE shared operation, with
    explicit captures and idempotency, and list every consumer in one edit. Roughly 7 of 30 findings of
    one spec review were this class.

## Plugin `skills`  `[home: graph-Document]`  — **structural + behavioral**
*For any project that publishes reusable agent skills through the shared graph, so any skill-capable
agent on any project self-installs them without knowing the source repo.*
- **`Skill`** node — a self-contained installable agent skill: `name` · `description` (when-to-use /
  triggers) · `body` (the full skill/prompt content) · `target_role` (development | critic | any) ·
  `target_harness` (claude-skill | codex-prompt | …) · `version` · `status`. Self-contained (Core §2):
  a consumer installs with no repo access. `Skill` nodes float (query/tag), not in the containment tree.
- **Skill-sync (behavioral).** A skill-capable agent, as part of reading the conventions at session
  start, fetches the **installable** `Skill` nodes for it, compares each `version` with its
  locally-installed copy, and installs/updates any missing/stale one into its own skills location
  (e.g. `~/.claude/skills/<name>/`) using its own file tools. **Installable** = `status: current`
  (published) + authored by a **trusted credential** + matching `target_role`/`target_harness`; a
  draft/`provisional`/`superseded`/`rejected` or untrusted-authored node is **data, not an
  installable**. Package key = `name` + `target_harness` (+ namespace/publisher); two `current` nodes
  sharing a key → **park/escalate**, never a silent pick. **No installer software exists — the agent is
  the installer.** Next-session activation latency is acceptable.
- **Skill-sync starts with an inventory.** Before installing anything, the agent lists what is already
  installed on **every skill surface it can reach** — its native store *and* any account-/platform-level
  store its tools can manage (e.g. a web skills panel via browser automation). A surface it cannot
  write is **reported** to the operator with the change it needs — never silently skipped. Each
  installable `Skill` node is then checked not only for version staleness but for **collision**: an
  already-installed skill — any name, any surface, any author — whose triggers/purpose overlap enough
  that both could fire on the same request. **Install target vs inventory scope:** the agent
  installs/updates **only into its native store**; non-native surfaces are inventoried for collision
  detection and are changed only through the collision gate below or an explicit operator
  instruction — never auto-synced. Parks and reports are **per surface**.
- **Collision → operator gate; archive-then-replace.** A collision is never resolved silently: raise it
  to the operator ("the conventions require skill X vN; it collides with installed skill Y on surface S
  — replace?"). **Silence = not granted** — the install for that surface parks, and the park is
  reported. On approval, the displaced skill's **full body is archived to the graph first**: a `Skill`
  node, `status: superseded`, a **`displaces`** edge from the incoming skill to the archived one (a
  plugin edge type: "took this skill's place on an install surface" — **no package lineage implied**;
  `supersedes` stays reserved for a new version of the same package key), provenance naming the
  surface, the date, and the approval — self-contained enough to reinstall from the node alone
  (Core §2; that is the rollback path). The archived node's `superseded` status pairs with `displaces`
  here — the displacement pairing Core §6 names explicitly (operator decision, 2026-07-09 review).
  Only then is it removed or replaced. **The archive record is its own node — never a relabelled
  distribution copy.** If the displaced skill also exists in the graph as a published (`current`)
  distribution `Skill` node, that node's publication state is untouched (it changes only through the
  publication gate); the archive record `references` it for lineage. Dedup (Core §13) applies only to
  an existing **archive record** of the same body + surface event — reuse it, don't mint a twin.
- **Same package key, new version → notify, not gate.** When an installed skill and an installable
  `Skill` node share the **full package key as defined above** (`name` + `target_harness` +
  namespace/publisher where present) and only `version` differs, the publication gate has already
  vetted the body: install/update and tell the operator in **one chat line** ("skill X updated
  vA→vB"). No second gate — and no silent update. A differing namespace/publisher/author is **never**
  a plain version bump: it is a collision (or a package-key conflict) and takes the gates above.
- **Harness-universal by construction.** The steps above are written at capability level (Core §18) and
  bind **any skill-capable agent** — Claude Code (file skills), Codex (prompt/config surface), Cursor
  (rules surface), future harnesses. `target_harness` names the surface; a **new harness value is data,
  not a spec change**. Each agent maps the steps to its own tools; what it cannot do itself it hands to
  the operator explicitly.
- **Trust — the gate is publication.** A skill `body` is a **behavioral control plane**, not ordinary
  memory: once installed it *governs* the agent. A new/changed body enters as a **draft**
  (`provisional`, not installable); the operator **publishes** by promoting it to `current` — a Core §6
  status transition needing the operator's confirming event. Auto-install stays frictionless for
  published skills; **every new/changed body passes a human gate** before it governs any agent.
- **Published versions are immutable (invariant).** Install-relevant fields of a `current` version
  (`body`, `description`, targeting) are **frozen**: any change is a **new version** re-entering as a
  draft through the publication gate — never an in-place edit. One `version` = one body; an agent
  finding a `current` body differing from its installed copy at the **same** version treats it as
  invalid and **parks/escalates**.
- **Source of record vs distribution.** The editable source — and its review cycle — lives in the
  authoring project's repo; the graph `Skill` node is the **published distribution copy**, re-seeded on
  a `version` bump (the conventions' own `refresh_note` pattern). Consumers read **only the graph**.

## Plugin `operator-profile`  `[home: graph-Document]`  — **structural + behavioral**
*For any project whose operator wants their interaction preferences (language, explanation style,
format habits) known to **every** agent on every machine, not just the one that learned them.*
Preferences are a **behavioral control plane** (like skills): once in force they govern how agents
address the operator, so entry into force is gated.

- **Collection (behavioral).** Any agent, in any project, watches for **preference signals**: direct
  instructions about interaction style ("answer in Russian", "no section-number references", "no
  buttons"), repeated clarification requests over the same kind of output, and the operator's own
  language choice. It records each as a **portable rule with its why** — not a chat quote. **Epistemic
  gate:** a preference the operator **stated directly** enters as `current` immediately (the statement
  is the confirming event); a preference the agent **inferred** from behavior enters as `provisional`
  and is proposed to the operator in one chat line — it becomes binding only through the explicit
  confirmation path (`confirm_preference`), which records the confirming event (Core §6 transition;
  same principle as the skills publication gate). One preference = one node (Core §3): a `Note`
  auto-tagged `preference` (the existing `remember_preference` substrate) carrying `scope` (`global` |
  `project`), a `domain` key, the rule text, and a **portable why** (safe outside the graph:
  interaction style, no personal facts — it is compiled into local files).
- **Preference identity & conflicts (structural).** A profile-participating preference carries a
  `domain`: a short kebab-case key naming the aspect of interaction it governs. Domain keys are a
  **controlled vocabulary**, exactly like the `pattern:*` tag layer (Core §15): the plugin ships an
  initial registry (`language`, `answer-format`, `doc-references`, `ui-buttons`, `verbosity`, `tone`,
  `explanation-level`) plus an alias map, both living in the graph as a registry the §14 pass
  maintains. The server **normalizes on write**: a known alias resolves to its canonical key; an
  unknown key is a controlled error listing the registry, unless the caller passes `new_domain: true`
  — then the key is created **pending**: the preference is stored, but a pending domain **never
  compiles** and never participates in conflict resolution until it is **accepted** through the
  registry gate (`resolve_domain` — trusted-credential, operator-relayed, audited; the agent proposes
  the new domain in one chat line). So a freeform key can never affect anyone's compiled behavior
  before a human accepted it — deliberate growth, no silent fragmentation into
  `answer-format`/`response-format`/`format` synonyms. Domain keys are **non-sensitive by
  construction** (they name an aspect of interaction, never its content).
- **The domain is the conflict key.** Different domains are additive; same domain = the same rule at
  different strengths of authority. Within one `(account, scope, domain[, project])` key the identity
  is **split by standing**: at most **one `member`** (a binding rule) **and** at most **one
  `candidate`** (an unconfirmed inference) coexist. A **stated** write creates/updates the member (the
  statement is the confirming event) and in the same transaction **supersedes** any pending candidate
  for the key (a direct statement outranks a pending inference). An **inferred** write
  (`inferred: true`) **never** touches the member: it creates/updates the key's single candidate,
  recording as its **base** the resolved effective rule it was inferred against (project member, else
  global member, else null — scope, node id, version), and stays out of every compiled profile.
  **Confirmation is what replaces:** promoting the candidate flips it to member and supersedes the
  previous member — **but only if that member still matches the candidate's recorded base**; a base
  mismatch means the rule changed after the inference, and the promotion is **refused** pending
  explicit re-confirmation.
- **Two storage axes** — the plugin never touches the Core status vocabulary. Core `status` stays the
  **epistemic** axis (current/provisional/superseded/rejected, Core §6 transitions as usual); the
  profile marker carries a separate profile-specific **`standing`** field — `member` (binding) |
  `candidate` (unconfirmed inference) | `parked` (an alias-collision loser awaiting disposition).
  **Compilation and resolution read `standing` only:** members compile, resolution considers members,
  candidates and parked nodes are invisible to both. The capabilities update the two axes in defined
  pairs: stated write → `member` + `current`; inferred write → `candidate` + `provisional`;
  promotion → candidate becomes `member`/`current` and the displaced member becomes `superseded`
  (standing removed); retirement → `superseded`, standing removed; parking (alias) → `parked`, **Core
  status unchanged** (epistemic history preserved); detach → marker + standing removed, Core status
  untouched.
- **Cross-scope resolution** is deterministic and happens **before delivery filtering**: for each
  domain, the project-scoped member wins if present, else the global one; if the **winning** node is
  above-normal sensitivity it **masks** the domain — the domain is withheld entirely rather than
  falling back to a lower-precedence deliverable rule (precedence is never silently inverted by
  filtering). The server returns only the **resolved effective set** — a client never merges.
- **Storage (structural) — two scopes.** **Project**-scoped preferences live in the project's own zone
  (ordinary write). **Global** preferences attach to the operator's `Person` node in their personal
  space — a cross-project write, sanctioned the same way `feedback` is (**the second exception to the
  zone rule, §19**): `remember_preference` routes `scope: global` writes to the calling credential's
  operator zone regardless of the project it was called from. No `Person` node yet → the write
  **creates** it in the account's personal space; no personal space → the write **fails with an
  explicit error** naming the missing prerequisite (never a silent fallback into another zone).
- **Delivery (behavioral) — the guarded bootstrap.** Repo-based harnesses (Claude Code, Codex, Cursor,
  future) read agent-instruction files from the repo; those files are a contract with **every**
  contributor, so the committed bootstrap block is **impersonal and degrades silently**:
  - At session start the agent makes **one honest availability attempt** (a real discovery/capability
    call, not a cached belief). Failure is handled **by class**: an **absence** failure (no connector
    configured, graph unreachable, graph up but the profile capability missing) → **silently skip** the
    block (no error, no mention, no retry this session; normal work proceeds). An **authentication**
    failure (401/403 from a *configured* connector) is **not** silent-skipped: only a user who
    configured credentials can receive it, so the agent tells **its** user once, briefly, and proceeds
    without hydration. (This **narrows** the stateless-client discovery-first rule: one real attempt
    before concluding absence; concluding absence with **no** attempt stays forbidden. Silence protects
    colleagues who don't have the system; a 401 by construction never reaches them.)
  - **Stale-cache policy:** if hydration fails and a previously written local profile file exists, the
    agent **may** follow it as an explicitly stale cache — profile content is interaction style, not
    authorization, so the worst case is outdated style, and deleting the cache would punish transient
    outages. Refreshed on the next successful hydration; no re-fetch within the failed session. The
    header carries `version` + `fetched_at` so staleness is visible; an agent following a stale cache
    after an auth failure has already told its user.
  - On success the agent calls the profile capability. The **server** resolves who the operator is from
    the calling credential's **account** (the account, not the credential, is the operator unit — one
    person, many clients), compiles the resolved effective set (per-domain, project overrides global),
    and returns it with a single **version**.
  - **Before writing** the agent verifies the target path is actually ignored and untracked
    (`git check-ignore` + not in the index). If not — the `.gitignore` line is missing or the file is
    somehow tracked — it does **not** write profile content; it proposes the `.gitignore` fix through
    the self-install gate (or silently skips if no operator is present). Personal content is never
    written to a path that could reach git.
  - The local file is **two parts** with separate rewrite rules: a **client-maintained** cache-metadata
    header (`fetched_at`, updated on **every** successful hydration) followed by the **server-produced**
    `profile_markdown` verbatim (deterministic: version header + rules; contains **no** timestamps). On
    success the agent always refreshes `fetched_at`; the server part is rewritten only when the returned
    `version` differs. Thus the compiled content stays byte-identical across harnesses for the same
    version, while staleness stays visible. Different credential → different operator → that user's own
    profile in their own working copy.
- **Self-install (behavioral) — the bootstrap installs itself.** The committed bootstrap block carries
  a **version marker** (`operator-profile bootstrap vN`); the conventions state the current N. **File
  ownership** mirrors the skill-sync surface rule: each harness owns **its own** agent-instruction file
  (Claude Code → `CLAUDE.md`, Codex → `AGENTS.md`, Cursor → its rules file, …), and an agent
  checks/maintains the block **only** in the file its own harness reads — per-file states resolve
  independently:
  - Own file, block **absent** → **propose** the install to the operator: one commit adding the block
    (and the shared `.gitignore` line if missing), both impersonal; one operator "yes" per repository
    per file.
  - Own file, block **present at current version** → nothing to do; go straight to hydration.
  - Own file, block **present but stale** (older N) → propose an operator-gated update commit, exactly
    like an install; never silently rewrite a committed file.
  - **Other** harnesses' files (absent/stale blocks) → **inventory, not action**: report the
    observation to the operator; never edit a file another harness owns. Hydration follows the agent's
    **own** file's block only, so a mixed-version repo cannot make one agent follow another harness's
    stale semantics.
  - Idempotent by construction: one block per harness file serves **all** users of a shared repo — the
    first user's commit is enough; later users' agents find the block and fetch their own per-credential
    profiles at runtime.
- **Invariants.**
  - The committed bootstrap text contains **no personal data** and **no hard dependency** on private
    infrastructure — a silent no-op for any contributor without the graph.
  - Operator-profile content is **never** written to git-tracked files, and the writer **verifies**
    ignore/untracked status before writing. Canon lives in the graph; the local file is a gitignored
    compiled copy; harness user-level stores may cache but repo-tracked files may not.
  - Only operator-**confirmed** preferences are binding: `provisional` (inferred, unconfirmed)
    preferences are excluded from the compiled profile.
  - **Sensitivity gate for local files:** a preference above normal sensitivity is **never** compiled
    into `profile_markdown` — such preferences are non-deliverable through the repo bootstrap by
    design, served only through surfaces that keep them off disk (the platform layer under
    `stateless-client`, or direct graph reads). The omission is **never silent**: compilation returns a
    withheld-count and the compiled profile carries a one-line note ("N preference(s) withheld:
    sensitivity"). The `why` of any preference is written portable by rule; the server compiles only
    `text` + that portable why.
  - **Rollout ordering:** the server capability is **live before** the canon section publishes
    (re-seed). Defense in depth: the bootstrap's silent-skip already covers "graph up, capability
    missing", so even a wrong order degrades gracefully.
- **Boundary with Plugin `stateless-client`.** operator-profile owns **what** the preference layer is
  (collection, identity/conflict semantics, storage, the compiled profile) and its **repo-based**
  delivery. Platform clients without a repo (ChatGPT-style) keep their existing delivery — the
  platform's customization layer under `stateless-client` — and may read the **same** preference nodes
  through it; no rule is duplicated, each plugin owns its own delivery surface.

---
