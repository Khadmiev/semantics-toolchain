---
name: semantic-map
description: >-
  Extract the SEMANTIC and LOGICAL layer of a system — the business/domain
  rules, decisions, state machines, taxonomies, boundary/identity rules, enum
  meanings, and invariants — from its code AND documentation, deliberately
  stripping away implementation/technical detail so a human can SEE the actual
  decision picture and catch divergence between stated intent and what the code
  really does. Use this whenever the user wants to understand "what does this
  system actually decide / mean / assume", review a pipeline or module at the
  business-logic altitude, audit whether the docs match the implementation,
  surface buried or undocumented design decisions, onboard onto unfamiliar
  domain logic, or sanity-check a spec against its code. Trigger on requests
  like "what are the actual rules here", "give me the logic/semantic picture of
  X", "what decisions are baked into this code", "does the design doc match the
  implementation", "map the state machine / event taxonomy / invariants",
  "I want to review the logic without the SQL noise", "semantic map of this module",
  "the logic without the technical details", "what does this code actually decide". Prefer
  this over a generic code walkthrough whenever the user cares about MEANING and
  DECISIONS rather than mechanics.
---

# Semantic Map

## Why this skill exists

Real systems hide their most important content — the **decisions** — inside
implementation. A single branch buried in a long source file (a status
assignment, a boundary condition, a default value, a classification rule) is
rarely "just a technical detail": it is usually a business ruling about what the
system treats as true. Such rulings routinely contradict the actual domain
intent, get encoded in code but never written in the spec (or vice versa), and
survive unnoticed until something breaks far downstream — precisely because they
look like plumbing.

The human reviewing a system needs to see **what it decides**, not how it's
wired. This skill produces a **Semantic Map**: a faithful, technical-noise-free
rendering of the system's domain logic, with active flagging of where intent
and implementation may have diverged. The reader should be able to be a domain
expert who does not read code, yet fully audit the system's rulings. The skill
is domain-agnostic — it works on any codebase (a billing pipeline, an auth
flow, a pricing engine, a state machine in a game) wherever meaning is buried
under mechanics.

The skill's value is judged by one question: **does the map let a human catch a
wrong or undocumented decision they would otherwise have missed?** Everything
below serves that.

## Step 0 — Settle mode, scope, maturity, and output language

Settle these before analysis. Infer them from the request and proceed; only ask
when genuinely ambiguous — an unnecessary round-trip is friction on what is
often a quick question.

1. **Delivery mode.** Decide which of two modes the request wants, from its
   phrasing:
   - **chat answer** — the user wants to *understand* something now ("what does
     this module actually decide?", "what are the real rules here?", "is this
     intentional?"). Answer inline. Do **not** create a file. Write in the
     user's own language (the language they are writing to you in) — silently,
     no need to ask or confirm; if they want another language they'll say so.
   - **document artifact** — the user wants a durable, shareable map ("write me
     a semantic map", "produce a doc", names an output path, or the scope is
     large enough that a file is the only sane container). Produce the markdown
     artifact per Step 4.
   When the request could be either, default to **chat answer** for a small/
   focused scope and confirm before producing a file for a large one — a
   surprise file artifact on a quick question is exactly the friction to avoid.
   The same semantic rigor (Steps 1-3) applies in both modes; only the output
   shape differs.

2. **Scope.** What is the subject? Accept either:
   - a **path** (a directory, a set of files), or
   - a **logical scope** ("the subscriptions pipeline", "the auth flow") that
     may span multiple directories — discover the relevant files via the Step 1
     inventory pass.
   Confirm the file set with the user **only when the scope is broad or the set
   is genuinely uncertain** (you had to guess which files belong). For a small,
   obvious scope, just proceed and report what you covered in the coverage note
   — don't stop to confirm the obvious. If the scope is large enough that a
   faithful single-pass read isn't possible, propose a decomposition (e.g. one
   map per subsystem) and let the user pick. This is the same proportionality
   principle as mode selection: match the ceremony to the size of the ask.

3. **Output language (document mode only).** A shared document may be for the
   user, their team, or external readers, and its audience language cannot be
   reliably inferred from the language of the current message — so for the
   document artifact, **ask** which language to write in if it isn't already
   clear, defaulting to the user's own preferred language. (Chat mode needs no
   such question — see mode 1.) Either way, the skill body and your reasoning
   stay in English; only the deliverable's prose, explanations, and section
   headers are translated. Identifiers, enum literals, table/column names,
   event-type names, and file paths are always kept verbatim in their canonical
   form (never translate `EXPIRATION`, `user_aliases`, `change_type`, etc.).

4. **Maturity of the system.** Infer (don't interrogate — same proportionality
   rule) whether the scope is an **MVP / move-fast** effort, **under active
   development**, or a **mature/production** system. This calibrates how loudly
   to report. On an MVP a great many "divergences" are deliberate corner-cutting
   — reporting each as a problem just produces a wall of "yeah, we know". So:
   - **Mature/production:** full scrutiny. A code-only ruling on a system that
     claims to be done is genuinely worth flagging.
   - **MVP / move-fast:** still report findings that would bite *even given* the
     accepted scope, but route the "this is rough but looks intentional for
     now" observations into a single short **MVP shortcuts / lower-priority
     semantic risks** list rather than the main flag register. Don't lecture a
     prototype about not being production.

   Two guards on this calibration, because misjudging maturity is itself a
   failure mode this skill is supposed to prevent:
   - **Naming honesty (the doc-is-not-intent rule, applied to shortcuts).** Do
     NOT label a shortcut "accepted" or "known" unless the artifact or the
     operator explicitly says so. Absent that, it is a *likely MVP shortcut /
     lower-priority risk*, not an accepted decision — the same skepticism you
     apply to "the doc says it's intentional".
   - **Hard floor — never demote these regardless of maturity.** Silent data
     loss, irreversible state/lifecycle transitions, security/privacy impact,
     and paid- or user-visible behavioral wrongness stay as correctness-breaking
     flags even on an MVP. "It's just a prototype" never excuses these.

   If you genuinely can't tell the maturity and it materially changes what you'd
   report, ask in one line. Otherwise **assume mature/production unless the user
   (or the artifact) frames it as an MVP/prototype** — when in doubt, scrutinize;
   a surplus flag is cheap to dismiss, a missed production bug is not.

## Step 1 — Inventory first, then ingest code AND docs together

### 1a. Inventory pass — proportional to scope

Large scopes are where this skill fails worst: drowning in context, or — worse
— emitting a confident "complete map" after only partially reading. Both hide
exactly the decisions the skill exists to surface. The inventory guards against
that — but scale it to the ask; a focused chat question about one module does
not need a full file census.

- **Small / obvious scope** (a single file or a clearly-bounded module): skip
  the formal inventory, read what's relevant, and note coverage in one line if
  anything was left out.
- **Broad / multi-file / uncertain scope:** before deep reading, build a
  lightweight inventory — list the files (code + docs) grouped by subsystem,
  triage each into **must-read** (carries domain logic: rule definitions, event
  taxonomies, state transitions, enums, invariants, design rationale), **skim**
  (supporting / likely-mechanical), or **out-of-scope**. If the must-read set is
  too large for one faithful pass, propose a **decomposition** and let the user
  pick. Never silently read a subset and present it as the whole.

When you do build the inventory it becomes the **coverage table** in document
output (Step 4), so the map never overclaims what it examined. Keep it cheap — a
triage list, not a full read.

### 1b. Ingest

Read both the implementation and the must-read documentation (design docs,
READMEs, DDL comments, inline comments, data-wiki entries, changelogs). The
whole point is the **comparison** between them, so neither source alone is
enough.

While reading, mentally tag every meaningful statement by **provenance**:
- **doc-only** — *claimed* in documentation, not (yet) verified in code.
- **code-only** — implemented in code, never stated in any doc.
- **both** — stated in docs and matched by code.
- **conflict** — docs say X, code does Y.

Critical stance on documentation: **a doc states a *claim* of intent, not
ground truth.** Docs go stale, describe aspirations never built, or encode a
decision the author later disagreed with. Treat "the design says X is
intentional" as a claim to be checked against the code AND, for consequential
rulings, against the operator — never as proof that X is correct or current.
The real intent may live only in the operator's head and may *contradict* the
doc; that contradiction is one of the most valuable things this skill can
surface (see Step 3).

This tagging is the backbone of the divergence section later. Keep a running
note of it as you read; don't reconstruct it from memory at the end.

For each decision you capture, record a **provenance pointer** (`file:line` or
`doc §section`) so the human can jump straight to the source and verify. The map
strips technical detail but never strips traceability.

## Step 2 — Apply the noise filter

This is the discipline that separates a Semantic Map from a generic code
summary. Aggressively **keep** the semantic layer and **drop** the mechanical
layer.

KEEP (semantic / logical):
- What things mean (domain definitions, grain — "what is one unit and what
  makes two things the same vs. different").
- Event/input taxonomies and what each triggers.
- State machines: states, transitions, what drives each transition.
- Boundary / segmentation / identity / merge rules ("what splits or joins a
  unit").
- Enum values and the real-world meaning of each.
- Classification rules ("X counts as Y when …").
- Invariants and what they protect.
- Decisions and their alternatives + rationale.
- Assumptions about the data and what happens when they don't hold.

DROP (technical / mechanical) — mention only if it changes a semantic outcome:
- SQL/Spark/dataframe mechanics, join strategies, window functions as such.
- Partitioning, clustering, file layout, performance tuning, range-join hints.
- Retry/idempotency/locking plumbing, checkpointing, temp-table churn.
- Serialization, surrogate-key generation, UUID schemes.
- Language/framework boilerplate.

The test for each fact: *"Would a domain expert who doesn't code need to know
this to judge whether the system is correct?"* If yes, keep it (translated into
plain domain language). If no, drop it — but if a mechanical choice silently
determines a semantic outcome (e.g. an ordering tie-break decides which event
wins and therefore whether a subscription splits), that IS semantic — surface
the *consequence*, not the mechanism.

## Step 3 — Surface divergence — on evidence, not suspicion

A map that catches a wrong decision is valuable. A map that *invents* wrong
decisions is worse than useless — it burns the reader's trust and their
attention, and it trains them to ignore the next flag. This skill's instruction
to look for hidden decisions creates a real failure mode: seeing divergence,
unstated assumptions, and "degradations" where there is only ordinary, correct
code. Resist it. The reader is far better served by five evidenced flags and an
honest "the rest looks sound" than by twenty speculative ones.

Two framing rules that govern this whole step:

- **A clean result is a real result.** "I examined the boundary logic and found
  no divergence between the code and the *documented* intent claims" is a
  complete, valuable finding — state it plainly. Do not manufacture a flag to
  look thorough; an empty divergence section on a sound module is the *correct*
  output, not a failure. Note the precise wording: clean means code matches what
  the docs *claim*, not that either matches real business intent — that is only
  confirmed once the operator answers the rulings in Step 3's confirmation list.
  Never report "matches intent" unqualified.
- **Every flag needs evidence.** A flag is a claim about the code, so it must
  point at the specific code or doc that demonstrates it (`file:line` / `doc
  §`). If you cannot point to where the behavior actually is, you do not have a
  flag — you have a hypothesis (see "Hypotheses" below), and it goes in a
  separate, clearly-weaker section. "Possibly there's a hidden assumption
  here…" with no locus is exactly the noise to suppress.

The failure modes worth surfacing — each only when the code/doc actually shows
it, never on a hunch:

- **Code-only decisions.** A ruling implemented in code that no doc states as
  intent. These are the highest-value flags: nobody decided this on purpose, or
  someone did and never wrote it down, so nobody reviews it.
- **Doc-vs-code conflicts.** The spec says one thing, the code does another.
- **Self-acknowledged degradations.** The code or doc admits it's doing a
  lesser/fallback behavior ("degrade to A1-like", "TODO: proper handling",
  "simplified because…"). These are decisions made under constraint that may no
  longer hold.
- **Data-availability-sensitive semantics.** Behavior that changes depending on
  whether some field/source is present. If a missing input silently flips a
  rule, the human must know.
- **Unstated assumptions.** Rules that only hold if some real-world condition is
  true, where that condition is assumed but never checked.
- **Stale intent.** A doc describes the old behavior; the code moved on (or the
  reverse).

For each flag, state plainly: what the code does (with its `file:line`), what
intent it appears to contradict (or that intent is absent), and why it matters
in domain terms. Be direct — when the evidence supports "this looks wrong", say
so; when it only supports "this is undocumented", say only that. Match the
strength of the claim to the strength of the evidence, no more.

### Severity — so the reader spends attention where it counts

A 30-page map nobody can review reproduces the exact disease this skill treats:
the important sinks into the bulk. So **rank every flag** by material impact,
using this neutral scale (it travels across domains; don't import project-
specific P-codes):

- **correctness-breaking** — produces wrong results, silent data loss, or
  violates a stated invariant. The reader must act.
- **metric-distorting** — results are "valid" but a count / rate / amount is
  skewed (e.g. miscounting units, double-counting revenue). Worth knowing.
- **cosmetic** — naming, staleness with no behavioral effect, latent edge cases
  not currently reachable. Omit these entirely unless one genuinely clarifies
  stale intent, resolves naming confusion, or affects reader-facing semantics —
  a cosmetic tier that fills up with trivia recreates the attention-cost disease
  this skill exists to cure. When in doubt, leave it out.

The handoff (Step 5) leads with the correctness-breaking ones and caps how many
flags it surfaces inline (roughly the top 3-7 by severity); the rest live in the
document for the reader who wants depth. Severity is how a large map stays
reviewable: the reader can stop after the correctness tier and trust that
nothing catastrophic is buried below.

### Routing `class` — shared vocabulary with the review loop

Alongside severity (which is unchanged and still drives attention-ordering),
give every flag a routing `class` — the review protocol's finding `class` — so a
finding surfaced during mapping can be routed and dispositioned by the same
machinery as a review finding, and the operator reads one vocabulary, not two.
The two axes are orthogonal; `class` does **not** replace severity. The four
classes (semantic-map-delta B.1, 2026-07-09):

- `impl_error` — the implementation diverges from the intended behaviour.
- `spec_error` — the spec/decision itself is wrong or inconsistent.
- `new_decision` — a genuine fork only the operator can settle (routes to the
  operator, not to development).
- `map_error` — the semantic map misrepresents the code/decision it documents.

### Confirmed vs. Hypotheses — keep them physically separate

- **Confirmed flags** have a locus and you can show the behavior in the code.
  These go in the divergence register with their severity.
- **Hypotheses** are honest hunches you could NOT verify from a static read —
  there's a real observation behind them (e.g. "this rule looks like it assumes
  X, but whether X holds needs a data run"), but no proof. They are valuable
  (our grace-window finding started as one), so don't discard them — but put
  them in a separate, explicitly-weaker **"Hypotheses / worth checking"**
  section, never mixed into the confirmed flags. A hypothesis with *no* concrete
  observation at all isn't worth writing down; drop it.

### The doc-is-not-intent trap (read this twice)

The single most dangerous move is to read "the design doc says X is
intentional" and therefore conclude X is correct, settled, or no-longer-a-
problem. That is precisely how a wrong ruling survives review: it's written
down, so everyone trusts it. A design doc is a *claim* of intent by whoever
last edited it — it can be stale, aspirational, or simply wrong, and the real
intent may live only in the operator's head and contradict the doc.

Two hard rules:

- **Never assert a ruling is "intentional" / "by design" / "no longer an issue"
  on documentation alone.** The strongest claim you may make from a doc is *"the
  doc states X is intentional (unconfirmed against operator intent)."* If code
  and doc agree, that confirms *the code matches the doc* — not that either
  matches what the business actually wants.

- **Consequential rulings get escalated, not assumed.** For decisions that
  define the grain, identity (what is the same vs. a new unit), boundary/
  segmentation, terminal lifecycle transitions, or anything whose reversal would
  materially change outputs — surface the ruling for explicit operator
  confirmation even when doc and code agree. These are exactly the decisions
  worth a human "yes, that's what we want" before trusting them.

Collect every such item into a dedicated **"Rulings requiring intent
confirmation"** block at the end of the map (and lead the chat handoff with it).
List them for the operator to confirm or correct in one pass — do not interrupt
the analysis to ask about each one individually. The operator's correction of
any item frequently reveals that the *code and the doc are both wrong* together,
which is the highest-value outcome this skill produces.

## Step 4 — Render the map

Read `references/output_template.md` now for the full section-by-section
template and a worked example. The two modes differ in BOTH container and
weight — match the output to the ask:

- **chat answer mode:** render only the relevant subset the user asked about,
  directly in the conversation. No file. Be concise — lead with what they asked
  and the rulings/flags that bear on it. Do **not** impose the full section
  order or a formal coverage table; if your reading was partial or the scope was
  ambiguous, add a one-line coverage note instead. The point of chat mode is a
  precise answer, not a miniature document.
- **document artifact mode:** write a markdown file following the full section
  order from the template, including the **coverage table** (what was fully
  mapped vs. skimmed vs. out-of-scope). Use the path the user gave, or default
  to a sensible one near the scope (e.g. `<scope>/SEMANTIC_MAP.md` or a scratch
  location). Write in the chosen output language. **Always write the file as
  UTF-8** — when the output language is non-Latin (Russian, etc.) this is what
  keeps the text intact. If you additionally render the map to HTML for the
  user, embed the content inline with an explicit `<meta charset="utf-8">`;
  do NOT rely on a runtime `fetch()` of the markdown (a `file://` fetch is
  decoded as Latin-1 by browsers and will mangle non-ASCII text). A document
  that silently omits
  a region reads as "nothing to see here" — the exact failure this skill exists
  to prevent — so the coverage table is mandatory here.

Either mode, the semantic rigor of Steps 1-3 is identical; only the rendering
weight differs.

Diagrams: render state machines, decision trees, and lifecycle flows as Mermaid
— far clearer than prose. Mandatory nowhere, available everywhere: use a diagram
in either mode when it genuinely clarifies, skip it when prose suffices. If the
user later wants a slide deck or standalone shareable document, the markdown +
diagrams are the source; escalate to a presentation/docx format on request
(don't produce one preemptively).

## Step 5 — Hand off

The handoff is an attention budget, not a data dump. Lead with the two things
the user acts on, kept short enough to actually read:

1. **Rulings requiring intent confirmation** — the consequential decisions that
   need an operator "yes, that's what we want" (see Step 3). A short list to
   confirm or correct in one pass.
2. **Divergence flags — top of the severity order, capped.** Surface the
   correctness-breaking flags first, then metric-distorting, and stop at roughly
   3-7 inline; point to the document for the full register and the cosmetic /
   hypothesis tiers. The goal is that the reader can act on the handoff itself
   without scrolling a 30-page map — the depth is there for when they want it,
   not as the price of entry.

If the analysis was clean — few or no flags — say that plainly and briefly; do
not pad the handoff to look busy. Then the headline decisions, then (document
mode) a pointer to the file.

Invite the user to correct any item — their correction often reveals that the
*code and the doc are both wrong*, which is the whole game. Treat such a
correction as new ground-truth intent and reconcile the map to it.
