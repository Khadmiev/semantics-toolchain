# Semantic Map — output template

Write the deliverable in the user's chosen output language. Keep identifiers,
enum literals, table/column names, event types, and file paths verbatim
(untranslated). Translate only prose, explanations, and headers.

Use the section order below. Omit a section only if genuinely empty, and say so
("No state machine in this scope") rather than silently dropping it. Every
captured decision/rule carries a provenance pointer (`file:line` or `doc
§section`).

---

## Recommended section order

### 0. Scope & coverage
- What was analyzed (paths / logical scope), which docs were read.
- Honest coverage note: what is fully mapped vs. skimmed vs. not reached.

### 1. Domain glossary & grain
- The core nouns and what each MEANS in domain terms.
- The **grain**: what is one unit, and the identity rule — what makes two
  observations the SAME unit vs. DIFFERENT units. (This is where the most
  consequential decisions usually hide.)

### 2. Input taxonomy
- Every input event / signal / record type in scope.
- For each: its real-world meaning + what it triggers in the system.
- A table works well: `Event | Meaning | Triggers`.

### 3. State machine(s)
- States + their meaning, transitions + what drives each.
- Render as a Mermaid `stateDiagram-v2`. Prose for nuances the diagram can't hold.

### 4. Boundary / segmentation / identity rules
- The rules that SPLIT or MERGE units. What starts a new unit, what continues an
  existing one, what closes one, what fuses two.
- This is the section the EXPIRATION example belongs to.

### 5. Classification & enum semantics
- Each enum and the meaning of every value.
- "X counts as Y when …" rules.

### 6. Decision register
- The heart of the map. One entry per material decision:
  - **Decision**: what was decided.
  - **Alternatives**: what else could have been chosen (if discernible).
  - **Provenance**: doc-only / code-only / both / conflict + pointer.
  - **Rationale**: stated reason, or "none stated".

### 7. Invariants
- What must always hold, and where it's enforced (QC check / DB constraint /
  implicit-and-unenforced). Flag invariants that are assumed but not enforced.

### 8. ⚠ Divergence flags (confirmed, evidence-backed, severity-ranked)
- Only flags you can point at in the code/doc go here — every one carries a
  `file:line` / `doc §`. No locus → it is a hypothesis (§8b), not a flag.
- **Order by severity**, and label each: **correctness-breaking** (wrong
  results / silent data loss / invariant violation) → **metric-distorting**
  (valid but a count/rate/amount is skewed) → **cosmetic** (naming, harmless
  staleness, unreachable edge case). Include a cosmetic item only if it clarifies
  stale intent, naming confusion, or reader-facing semantics — otherwise omit it.
- Group secondarily by type: code-only decision, doc-vs-code conflict,
  self-acknowledged degradation, data-availability-sensitive, unstated
  assumption, stale intent.
- Each flag: severity · what the code does (`file:line`) · what intent it
  contradicts (or "no stated intent") · why it matters in domain terms.
- **An empty or near-empty section is a valid result** on a sound system. Say
  "examined X, no divergence found" — do not manufacture flags to look thorough.

### 8b. Hypotheses / worth checking (unverified — kept separate)
- Honest hunches with a concrete observation behind them but no proof from a
  static read (e.g. "this rule looks like it assumes X; needs a data run to
  confirm"). Valuable but explicitly weaker — never mix into §8.
- A hunch with no concrete observation at all does not belong here either; drop
  it. This section is the controlled outlet for genuine uncertainty, not for
  speculation.

### 8c. MVP shortcuts / lower-priority semantic risks — MVP / move-fast scopes only
- When the system is an MVP, route deliberate-corner-cutting observations here
  as a short list, instead of dressing each as a problem in §8. Reserve §8 for
  things that bite even given the accepted scope. Omit this section entirely for
  mature/production scopes.
- **Naming honesty:** call an item "accepted/known" ONLY if the artifact or the
  operator explicitly says so; otherwise it is a *likely shortcut / lower-
  priority risk*, not a settled decision.
- **Hard floor:** never route silent data loss, irreversible state/lifecycle
  transitions, security/privacy impact, or paid-/user-visible wrongness here —
  those stay as correctness-breaking flags in §8 regardless of maturity.

### 9. Rulings requiring intent confirmation
- The LAST section of the file and the FIRST thing in the chat handoff.
- Consequential rulings that need an operator yes/no even when code and doc
  agree (grain, identity, boundary, terminal lifecycle, anything whose reversal
  changes outputs materially). See the dedicated block below for format.

---

## Worked example of a divergence flag (the calibration target)

This is the standard of insight the map should reach. The example is from a
generic order-fulfillment service — **illustrative only; your domain will differ
entirely. Reach this standard of reasoning, do not pattern-match to this
example's specifics.**

> ### ⚠ [metric-distorting] Code-only decision + self-acknowledged degradation — order "completed" status
>
> **What the code does:** an order is marked `COMPLETED` the moment the shipping
> label is created (`fulfillment/status.py:88`), not when delivery is confirmed.
> A later delivery-failure event is recorded but does NOT move the order out of
> `COMPLETED`.
>
> **Intended semantics (claimed by the spec):** `STATUS_SPEC.md §4` defines
> `COMPLETED` as "the customer has received the goods", explicitly distinct from
> `SHIPPED`. (Note this is a *doc claim* — whether it reflects current business
> intent is for the operator to confirm.)
>
> **Why it diverged:** a comment at `status.py:84` notes "we don't get reliable
> delivery webhooks from carrier X, so treat label-created as terminal for now"
> — a degradation adopted because a data source was unavailable, never reflected
> back into the spec.
>
> **Why it matters:** completion-rate and revenue-recognition metrics count
> shipped-but-undelivered orders as completed, overstating both; lost-in-transit
> orders never reopen for refund. Surfaced only when carrier X's failure rate
> rose.
>
> **Provenance:** code `fulfillment/status.py:84,88`; doc `STATUS_SPEC.md §4`.
> **Confidence:** confirmed.

What makes this flag good: it states the code behavior, the contradicted intent
(while labeling it a doc *claim*, not settled truth), the *root cause* of the
divergence (a missing/unreliable data source forcing a degraded rule), the
domain-level consequence, exact pointers, and a confidence label — with zero
implementation mechanics beyond what's needed to locate and justify the ruling.

---

## "Rulings requiring intent confirmation" block (Step 3)

End the map with this block, and lead the chat handoff with it. These are the
consequential rulings (grain, identity, boundary/segmentation, terminal
lifecycle, or anything whose reversal materially changes outputs) that should
get an explicit operator "yes, that's what we want" — even when code and doc
agree, because agreement only proves code matches doc, not that either matches
business intent.

Format each as a one-line claim + where it's encoded + the question, so the
operator can confirm/correct the whole list in one pass:

> ## Rulings requiring intent confirmation
>
> 1. **Same vs. new unit on lapse-then-resume.** Code + `DESIGN §4` agree that
>    an order resumed after a failed delivery is the SAME order. Encoded:
>    `status.py:88`, `DESIGN §4`. → Confirm: is resume-after-failure the same
>    order, or should a new one be opened?
> 2. **Grain = one row per …** Code derives the unit key as `uuid5(customer |
>    sku | first_order_ts)`. Encoded: `keys.py:30`. → Confirm this is the
>    intended notion of "one unit".
>
> *(An operator "no" on any line typically means code AND doc are both wrong —
> the highest-value outcome.)*
