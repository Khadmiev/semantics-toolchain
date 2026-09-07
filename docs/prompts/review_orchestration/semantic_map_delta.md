# Semantic-map skill — the delta this review loop applies (B.11, rewritten)

> **This file was rewritten at B.11 and its previous three changes are WITHDRAWN.** They
> are named below with the reason, because a superseded contract that merely disappears is
> a contract someone re-derives from memory six months later.
>
> The base skill it amends is now published: [semantic_map/](semantic_map/) holds the
> source of record, byte-identical to the copy installed on the operator's machine at
> `~/.claude/skills/semantic-map/`. Until B.11 it existed only as that installed copy —
> so this delta was published by all the rules while the thing it amends was not, and a
> reader of the delta could not see what it amended.

## What survives, and it is the substance

The skill's method is unchanged and is what the review loop uses:

- **generation from the CODE**, at the altitude of meaning rather than mechanics — what
  the system decides, under what conditions, with what boundaries and defaults;
- **cold reading**: the map is written by a party that did not build the thing, and it does
  not start from anybody's account of the work;
- the **severity taxonomy** (`correctness-breaking` / `metric-distorting` / `cosmetic`) and
  its attention-ordering;
- readable long-form prose, every abbreviation expanded on first use, pointers collected
  where a reader can find them.

## Change 1 (WITHDRAWN) — the map no longer opens with a delta against the intent

The withdrawn clause required any map read after a gate to open with a "what changed in
meaning since the last reading" section, split into intended change and unintended
code↔spec divergence.

**Why it is gone:** in this loop the map has never seen an Intent Summary and never will
(B.11 E-1/E-6). Its whole value is that it is written blind — a reader who has seen the
review's own account of the work cannot help reproducing it. A clause telling it to open
with a delta against a document it is forbidden to read is not merely dead; it is an
instruction to break the pass's one constraint.

The comparison itself was not dropped. It moved to a **separate pass with its own prompt**
([reconciliation.md](reconciliation.md)), where a reader IS allowed to see the intents and
can therefore cite them honestly.

## Change 2 (WITHDRAWN) — the map does not `supersede` the post-review intent

The withdrawn clause had the map supersede the post-review Intent Summary as "the later
reading of the same thing".

**Why it is gone:** they are not the same thing, and the loop now depends on their standing
**side by side**. Operator ruling of 2026-08-23 (translated from Russian): "Better let it
stand beside. That way one can reconstruct at which stage the hole appeared." The pair
localises the stage, which neither
document does alone:

- the intents agree and the map differs → the defect is in the **implementation**;
- the map matches the code as designed but the post-review intent misdescribes it → the
  defect is in the **self-description**, in the very report the operator relies on;
- all three agree and the operator still objects → the defect is in the **design**;
- the map contradicts the post-review intent OF THE SPEC while the code faithfully
  implements the spec → the spec was **summarised to the operator as something it was
  not**, and consent was given to that. This is the worst address, and superseding would
  have made it unreachable.

Superseding erases exactly the second document the localisation needs.

## Change 3 (WITHDRAWN) — map findings route nowhere

The withdrawn clause added the review protocol's routing `class` axis (`impl_error` /
`spec_error` / `new_decision` / `map_error`) so that a finding surfaced during mapping could
be dispositioned by the review loop's machinery.

**Why it is gone:** the B.11 map routes nothing, and its only consumer is the operator. Its
items are "what I am signing" statements, not protocol findings — they enter no ledger, are
owed no disposition, and cannot reopen a converged circle (B.11 E-5/E-8). A routing axis
with no route is a vocabulary that reads as a promise.

The four class names remain a perfectly good **vocabulary** for talking about where a
problem lives, and a map is welcome to use the words in its prose. What is withdrawn is the
claim that using them puts anything into the review loop's machinery.

## What replaces all three, for this loop

One file: [semantic_map.md](semantic_map.md), the prompt the watcher loads for the `map`
pass. It states the contract that actually binds — empty projection, code as the only
admissible ground, Russian to the operator's frozen profile, scope derived from the frozen
`base`+`commit` pair, no gate, no findings, no references to any intent.

Outside this loop, the base skill stands as published, unamended by any of the above.
