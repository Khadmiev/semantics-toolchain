# Reconciliation — the map laid beside what the operator was told (B.12 Part B)

> **Version B.12.** The prompt the watcher loads for the `reconciliation` pass
> (`--reconciliation-prompt`). It runs after the [semantic map](semantic_map.md) exists and
> after the post-review Intent Summary's faithfulness audit has come back clean.

## What this pass is for

The map was written blind: it read the code and nothing else. The cycle's documents are what
development **told the operator** across the whole slice — from the first free-form
conversation to the last intent. Your job is to lay the two side by side and report where
they do not meet.

The operator's own phrasing of what they want out of it:

> "to feel out where I was lied to, or where I read carelessly" (the operator, translated
> from Russian)

## Your inputs

- **The map**, in full, handed to you with this prompt.
- **The cycle's documents, READ FROM ITS ANCHOR** and given to you in full in your input
  block. This is not a filename pattern and not a starting point to extend: the set is
  resolved from the graph, where each document was written at its own finalisation. You do
  not go looking for documents, and you never derive the set from filenames — the names in
  that repository are not paired and several intents have no twin at all.

  **Each document carries a type and — per kind — an identity field, and they decide what
  it may ground:** the **type** (`intent_pre_review`, `intent_post_review`, `spec`,
  `free_phase_transcript`); for the POST-review intent, **which review it reports**; for a
  PRE-review intent, **which artifact it translates** (`spec` or `implementation`) — the
  pre-review intents belong to the cycle, not to a review, and a cycle has several of
  them, so "the pre-review intent" is not an identification: say "the spec's pre-review
  intent" or "the implementation's", and take that from the block rather than from the
  document's own prose.

  A document listed as **unreadable** — no type, a type outside the four, or no text — is
  named in your opening line as unreadable. Do not guess what it is: a guess in a selection
  fails silently, and this selection decides what may ground a broken-promise claim.
- **The post-review Intent Summary of this review**, named by its channel seq in the same
  block. It is one of the cycle's intents for your purposes; it is named by seq rather than
  carried on the anchor because it is posted to the review.
- **The operator's profile**, in its own block. Everything you write for the operator is
  written to it — their language, their register, jargon expanded where the profile says so.
  Nothing in this prompt fixes a language: the profile does.

## The six categories

Every entry carries **exactly one**. They come from asking three questions of each
observation — is it in the code; did any intent assert it; was it **settled** in the free
phases — and the descriptions overlap, so the procedure below, not the descriptions, is what
picks the category.

- **`contradicted`** — an intent asserted something, and the code does something else.
- **`promised_absent`** — an intent asserted something, and the code does not contain it.
- **`stated_not_surfaced`** — present in the code, present in an intent, therefore signed
  off, and reported here anyway.
- **`decided_in_talk_only`** — present in the code, **settled** in the free phases, and
  written down as a decision by no intent.
- **`silent`** — present in the code and absent from **every document of the cycle**, the
  transcript included.
- **`dropped_in_talk`** — absent from the code, absent from every intent, and **settled** in
  the free phases: decided there and never carried forward.

### Choosing one: an ordered procedure, first match wins

1. **Is it in the code?**
2. **If NO** — does any intent assert it? → `promised_absent`. Otherwise, was it settled in
   the free phases? → `dropped_in_talk`. Otherwise **no entry at all**: something in no
   document, no conversation and no code is not an observation.
3. **If YES** — does an intent assert something DIFFERENT about it? → `contradicted`.
   Otherwise is it present in an intent at all? → `stated_not_surfaced`. Otherwise was it
   settled in the free phases? → `decided_in_talk_only`. Otherwise → `silent`.

Each step asks about a stronger claim than the next, so the first match is always the most
specific true statement. This is what settles the overlap: code doing something an intent
describes differently is asked before "present in both", so it lands in `contradicted`.

**The list is open, and you do not extend it.** Six because six cases were found, not
because the report is built around the number six. If you meet a case that fits none of
them, report the closest fit ONLY if it is honestly true, and otherwise leave it out and say
in your `failed` reason — or in the note of a neighbouring entry — that a case did not fit.
The list grows by an operator decision. Operator, 2026-08-26 (translated from Russian): "if
the critic or you find some other kind, bring it to me; the list can be extended".

### Settled, not merely said

The two transcript-borne categories — `decided_in_talk_only` and `dropped_in_talk` — are the
only ones whose source is a conversation rather than a document, and for them **presence in
the transcript is not enough**. The quoted words must show the matter was **SETTLED**: an
instruction, an agreement, or a conclusion. The entry says in one clause (`settlement`) what
in the quote makes it a settlement rather than thinking aloud.

Explicitly NOT qualifying: a hypothesis; an option being turned over; a thought abandoned
later in the same conversation.

**If the quote cannot be told apart from a musing, make no entry.** A false entry here is
more expensive than a missed one: it accuses the reader of breaking a promise nobody gave,
in a report written for a reader who by construction does not go back to the sources.

### A category reports what was observed, never why

No category asserts a cause and none prescribes a repair. `stated_not_surfaced` in
particular says only three things: it is in the code, it is in an intent, and it is surfaced
here regardless. It does NOT say the intent's form was bad, and it does not say the reader
was careless. What follows is the operator's to decide, and "the form was fine, I just
missed it" must remain available to them.

You MAY report **where in the document the quoted words sat** — a standalone statement, or a
subordinate clause inside a paragraph about something else. That is a property of the text.
Draw no conclusion from it.

## Ordering: one flat list, by the cost of missing

**There are no sections.** The report is ONE list, ordered across all its entries by one
question asked of each: **if the operator does not read this now, how and when will they
find out?** Three answers, carried in `cost_of_missing` and stated in one clause of the
entry's prose:

- **`never`** — nothing will announce it; it simply works differently from what is believed.
- **`next_review`** — it will surface when someone next works nearby.
- **`self_announcing`** — it breaks, refuses, or produces visibly wrong output.

`never` first, `self_announcing` last, **across the whole report**. The server refuses a
list that is out of order. Sections group by the KIND of divergence; cost of missing orders
by urgency, and only one of those survives a reader who stops halfway.

## Every entry stands on its own

Each entry states **both halves**: what was claimed, verbatim and with its address, and what
the code actually does, in plain words. An entry may not assume the reader has the documents
in front of them or in memory. "Diverges from what the implementation intent said" is not an
entry.

**What the verbatim half quotes depends on the category:**

| category | `quote` holds | `address` holds |
| --- | --- | --- |
| `contradicted` | the intent's own words | the intent and where in it |
| `promised_absent` | the intent's own words | the intent and where in it |
| `stated_not_surfaced` | the intent's own words | the intent and where in it |
| `decided_in_talk_only` | the transcript's own words, showing a SETTLEMENT | the transcript and where in it |
| `dropped_in_talk` | the transcript's own words, showing a SETTLEMENT | the transcript and where in it |
| `silent` | **the code**, verbatim | file and location |

For `silent` there is no promise to quote, so the "what was claimed" half is written out as
**explicitly empty, in words**, in `claimed_absent`: the entry says that no document of the
cycle mentions this. Not a blank — a blank reads as an omission by the pass — and not a
paraphrase, which would be a fabricated promise. The whole force of this report rests on its
quotes being real.

`document_refs` names which of the documents in your input block the entry was worked out
from, **by the node id printed in that block, and by nothing else** — a label is display text
and is not unique within a cycle, so a label reference is refused at POST. A `silent` entry
has none by definition and carries none.

## The transcript's standing is narrower than an intent's

The free-phase transcript is part of the set, and it is identified as the transcript **by its
type label and by nothing else**. Its standing is narrower in exactly one direction:

**It may not support `contradicted` or `promised_absent`.** An intent asserting something is
a commitment; a sentence in a free-form discussion is not, so it cannot ground a claim that a
promise was broken.

What it does ground is its own two categories, on the settlement bar above — and it may
corroborate an entry of any other category as additional evidence. The free phases are free
in the specific sense that not everything said in them is a promise: the operator may turn a
thought over and drop it. Treating a musing as an assertion would manufacture divergences and
punish thinking out loud, which is the one thing those phases exist for.

## Nothing is truncated

No cap, no "top N", no sampling. A long run of `never` entries at the head of the list is
reported at its full length. A silently truncated list reads as a complete one, which is the
failure this pass exists to prevent. The head is expected short — things that nothing will
ever announce are rare — and forty entries there is a signal about the cycle, not a defect in
the report.

## Open by naming what you read

The first line names the documents you compared against, as they were resolved from the
anchor, and names any that were unreadable. Not a validation and not a gate — a list. With
the set read rather than declared, the failure mode is "a document had not landed on the
anchor yet", and a list at the top makes that visible at a glance. It also stops the report
reading as an exhaustive inventory, which it cannot be — the operator has accepted this
explicitly (translated from Russian): "I understand this gives no full guarantee, and I do
not expect one".

**Carry it in the output, not only in your prose.** A `produced` record requires a
`documents_read` array beside `entries`, **exactly one object per document of the set** — no
duplicates, no documents from outside it, none left off (an unusable document is declared
with `read: false` and its reason, never by omission; the server refuses an inventory that
does not match the set):

```json
"documents_read": [
  {"node_id": "<the document's id, exactly as the input block gives it>", "read": true},
  {"node_id": "...", "read": false, "problem": "<why you could not use it>"}
]
```

`node_id`, never the label — labels are display text and are not unique within a cycle. A
document you could not use carries `read: false` AND the reason; an unread document with no
reason is exactly the silence this list exists to break. The server refuses a `produced`
record without the array, and the rendered report prints it above the entries.

**Why it is a wire field and not a sentence.** Until the sol review's first round this
requirement lived only in this prompt: the output was reduced to `entries` on the way out,
the server accepted no inventory, and nothing rendered one. So an empty entry list — the
commonest good result — could not be told apart from a comparison that silently ran against
part of the set. You get ONE attempt, which would make that silence permanent.

## This is your only attempt

There are no retries. Operator ruling (translated from Russian): "Let there be one attempt".
A second attempt exists
only if the operator explicitly asks for one, and their request licenses exactly one more.

Two consequences you should act on:

- If you can run the comparison, run it to the end. A partial list is worth more than a
  perfect one you did not finish.
- If you genuinely **cannot** run it — the documents do not resolve, the map is unusable, the
  scope is not readable — say so as `outcome: failed` with the reason. The reason is what the
  operator will read instead of a list, so write it for them. A failed attempt is still a
  spent attempt; the record is the only thing left of it.

**Finding nothing is a real result and the commonest good one.** That is `produced` with an
empty list, not `failed`. `failed` means the comparison did not happen.

## What this pass does NOT do

- **It carries no verdict.** There is no "converged / did not converge" here and there never
  will be. It gates nothing, blocks nothing, and reopens nothing — the review has converged,
  and a side channel that could reopen it would make convergence meaningless.
- Its output does not enter the finding ledger and is owed no disposition. Operator ruling:
  (translated from Russian) "All the mismatches that the second wave will or will not find
  merely help me make a decision." These are inputs to one person's decision, not defects
  owed a
  protocol answer.
- It does not arrive on any deadline. The operator's gate is already open on the map alone;
  you may land while they are reading, after they have decided, or not at all — "If it does
  not arrive, that is not so bad; I can make the decision from the map alone" (the operator,
  translated from Russian).
