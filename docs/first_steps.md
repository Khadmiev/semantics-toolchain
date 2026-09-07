# First steps with your Assistant Memory

> **Status.** Every section below was finalized from the author's own acceptance runs
> on the working repository. This snapshot has not been re-walked from a stock install
> (README, "What was and was not checked for this release"), so wording that describes
> a concrete walk is the author's record, not yet a stranger's.

> **To the installing agent:** you deliver this guide to the operator **in their
> language** (the step-zero choice), section by section, as the last mandatory act of
> the install. This canonical file stays English; your delivery does not.

Your instance is installed and validated: the service computed every install fact and
observed the whole loop close. This guide covers what you do with it in the first week.

## 1. Add an existing project

*(This section is finalized from the first acceptance run's "day after" walk — the
concrete steps below are what that walk proves.)*

Give each project its own **space** — the unit of membership and visibility:

1. In the admin UI (`<your base URL>/spaces`), create a space named after the project.
2. Connect your agent to the memory (it already is, if it performed the install), and
   tell it to work in that space for that project's material.
3. On first contact the agent reads the conventions (`conventions` tool), recalls
   before acting (`search`/`traverse`), and writes new facts into the project's space.

## 2. Declare a development cycle

The system **never infers** when a cycle of work begins — you declare it, in your own
words ("we're starting a new slice: ..."). What the declaration buys you: the cycle
gets an anchor record in the graph, everything the slice produces (specs, review
outcomes, decisions) hangs off that anchor, and months later "what was this change and
why" is answerable by walking the graph instead of archaeology.

## 3. Preferences, and how to report problems

Anything you tell your agent about how you like to work — language, answer format,
tone, review depth — routes into your **operator profile** through the preference
mechanism: stated preferences bind at once; what the agent merely infers about you
stays a **pending proposal** until you confirm it. Your install already seeded the
first one (your language).

Problems with the product itself are reported to the project through the channel named
in the repository's README. No in-product feedback tool ships with a stock install —
that is deliberate.

## 4. Care of the instance

**Your instance's state lives in four places, and a machine move carries two of
them.** (1) The **Postgres database volume** — the graph, the accounts, the install
facts; back it up (`pg_dump`, or the shipped backup scripts). (2) The **`.env` file** —
your configuration, whose secrets only you re-enter or copy (they are gitignored and
are in no database dump). (3) **`OPERATOR_OBSERVATIONS.md`** — optional and local;
move it by hand if you want its hints. (4) **Caches** (the embedding model) — never
moved, re-downloaded on the new machine. A move is therefore: database dump + your
`.env`, then rebuild. If the database volume is lost, the service is honest about it:
it returns to "installing" and refuses substantive work until the facts exist again —
it never serves from a memory it no longer has. A restored backup returns to service
after one validation call (the same `conventions` probe that finished your install).

## 5. Updating

The repository is alive; updates arrive as **release tags**. The operation is in the
runbook (docs/setup.md §13): fetch, read the accumulated release notes for context,
check out the tag, record the target, rebuild, restart. Everything an update *requires*
is enforced by the install state machine itself — if a new release needs something from
you, the service refuses with that stage named, and your agent walks you through
exactly that delta. Skipping releases is safe by construction.

## 6. The observations file

During the install your agent kept a local file, `OPERATOR_OBSERVATIONS.md`, in the
install directory: what it noticed about you in plain conversation — language,
register, questions you asked. It is **excluded from git**, never carries secrets, and
exists for one purpose: to seed your operator profile as **proposals you accept or
reject** — nothing in it binds by itself. You can read it, edit it, or delete it at any
time; the graph is the canon, the file is a hint-cache.

## 7. The choices your first review will ask about

When you first run a review (the paired develop-and-criticize loop this product
ships), its gate will ask you to rule on a few settings. The gate asks live, in plain
terms, at the moment each value is needed — this section exists so you have somewhere
to have READ what the words mean before it does. It describes what the shipped
conventions and prompts actually enforce.

- **Depth tier — `light` / `standard` / `deep`.** How much machinery the review runs.
  `light`: file-level coverage, cheapest, for small or low-stakes changes. `standard`:
  the middle, default tier — symbol-level coverage of the changed surface. `deep`:
  everything `standard` does plus the extra audit passes — for mechanisms on the path
  of every request, hard-to-reverse changes, or things that ship to other people. Each
  step up costs more critic passes and more wall-clock time.
- **Round-gate mode — `auto` / `all_wait`.** Every round, the developer proposes what
  to do with each finding before building anything, and you see the round as a digest.
  In `auto` (the default), **judgement** findings — security and defence questions,
  anything proposed for a waiver, escalations — wait for your ruling, while
  **mechanical** ones (plain correctness/completeness fixes) proceed on their accepted
  proposals; you can still override any of them until the next version is accepted. In
  `all_wait`, every finding of every round waits for you.
- **The build horizon.** "Is this built for how you use it today, or for other people
  running it themselves?" Everything about proportionality is judged against your
  answer — a review that assumes the widest deployment hardens things you may never
  need; one that assumes personal use keeps the change lean. Only you can answer it.
- **Procedural waivers.** A review may propose skipping a named part of its own
  pipeline for this one run, with the reason stated (size, reversibility). Granting
  one is your call, per review.
- **What silence does.** Every question has a stated default: an unanswered tier
  proposal falls back to your profile's default (or the full pipeline); an unanswered
  waiver is **not granted** (the full check runs); an unanswered escalation **parks
  the review** until you answer — nothing auto-advances past a decision that is yours.

That is the whole vocabulary the first gate will use. Answer in your own words; the
agent translates.
