# Release notes — the well-known location

One file per release, named by its tag (`<tag>.md`, e.g. `v3.md`). This is the location
the update operation reads: before applying, the updater reads the notes between the
installed release and the target, **for context** (docs/setup.md §13).

**A note is DESCRIPTIVE and never the sole carrier of a required action.** Everything an
update *requires* must exist as a stage predicate of the target version's install state
machine — automatic work as migrations and bootstrap, human work as an unclosed stage
whose refusal names it. The note explains; the machine demands. A note that carries a
required action with no predicate behind it is a **release defect**.

Skipping releases is safe by construction: the state machine checks the target version's
predicates against current facts and replays no history, so notes accumulate here purely
as explanation.
