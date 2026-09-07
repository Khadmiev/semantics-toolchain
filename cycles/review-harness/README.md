# One cycle, read in order: the review harness

The article says of the loop it describes: "I threw the machinery out and
kept two texts, one for the critic and one for development, a few pages
each, and a thin harness that carries them. The texts reviewed themselves
first, on the rules they had just written." The texts reviewed themselves,
then the harness that carries them, and the operator finalised each. This
directory is that cycle: the run catalogs of the three reviews, verbatim,
with this guide.

Three runs, 2026-09-01 to 2026-09-02:

| Run | Artifact under review | Rounds | Outcome |
|---|---|---|---|
| `1_pair/` | the two instruction texts, `docs/review/critic.md` and `docs/review/development.md`; snapshotted as finalised in `1_pair/artifact_critic.md` and `artifact_development.md` | 3 | clean full pass on round 3; finalised by the operator on 2026-09-01 |
| `2_harness_spec/` | the harness specification, snapshotted as finalised in `2_harness_spec/artifact_spec.md` | 3 | clean full pass on round 3; finalised by the operator on 2026-09-01 |
| `3_harness_code/` | the harness itself, `src/review_harness/` with `tests/harness/` | 22 | clean pass on round 22; finalised by the operator on 2026-09-02 |

Roles: development was Claude Code; the critic was a headless model from
another vendor, launched fresh for every round (the catalogs call it by
its codename, "sol", and its runner "codex"); the operator held the gates.
The texts reviewed in runs 1 and 2 are snapshotted here exactly as
finalised (`artifact_*.md`), so that a later edit or translation of the
live files cannot change what the passes were judging; the live texts are
`docs/review/critic.md` and `docs/review/development.md`. Run 3's artifact
is the harness code, which this repository carries.

## What is in a run catalog

- `task_description.md`: the description of the task the critic checks
  the artifact against. It opens with the numbered theses of round 1,
  states the boundary of the subject, and then accretes one section per
  round, "Дельта круга N", saying what changed in the description and who
  authored the change (the operator's word, or development's own
  decision). The operator's rulings that shaped a run are sections of this
  file: "Граница предмета (решение оператора)", "Решения оператора о
  глубине гарантий", "Финализация прогона".
- `launch_sol.md`: the text the critic was launched with, that is, what
  the critic was told beyond its own instruction file.
- `round_NN.md`: one round. The header carries the date, the artifact
  version, the critic's trend line (findings on new territory / descendants
  of earlier fixes) and the critic's verdict. "Исходы" is development's
  written outcome per finding: **согласен** (agreed), **согласен с
  изменением** (agreed with a change), **не согласен** (disagreed, with an
  argument), **развилка оператора** / **передано оператору** (a fork put to
  the operator, with the ruling). "Проход критика дословно" is the critic's
  pass, verbatim, as received.
- `intent_post_review.md` (run 2 only): the post-review intent, the
  document written for the operator at the exit gate of that review.
- `MANIFEST.md`: generated at export; every file with its size and the
  hash of the source and of the exported text, and the number of
  redactions applied.

## Reading order

1. Read the two instruction texts as finalised: `1_pair/artifact_critic.md`
   and `1_pair/artifact_development.md`. They are the artifact of run 1 and
   the rules every later round ran under (the live copies are
   `docs/review/critic.md` and `docs/review/development.md`).
2. `1_pair/task_description.md`, then `1_pair/launch_sol.md`, then the
   three rounds. Round 1 raised six findings, one of them a fork the
   operator ruled on; round 2 had three descendants of those fixes; round
   3 was clean.
3. `2_harness_spec/artifact_spec.md` is the artifact of run 2. Then
   `2_harness_spec/task_description.md`, the three rounds, and
   `intent_post_review.md`, which is what the operator read before saying
   the word that finalised it.
4. `src/review_harness/` is the artifact of run 3. Then
   `3_harness_code/task_description.md` (22 theses; the two sections of
   operator depth rulings, after round 10 and before round 22, are the
   level-1 escalations the article's "finding levels" rule is about) and
   the 22 rounds.

## The rounds of run 3, by the numbers

Taken from the round headers; the tests count is the suite at the end of
each round's fixes.

| Round | Date | New territory / descendants | Critic's verdict | Tests after |
|---|---|---|---|---|
| 1 | 09-01 | 13 / 0 | not ready for a live run | 56 |
| 2 | 09-01 | 7 / 6 | noticeably advanced, not ready | 70 |
| 3 | 09-01 | 2 / 11 | not ready | 78 |
| 4 | 09-01 | (class-wide fixes of round 3) | substantially strengthened, not ready | 85 |
| 5 | 09-01 | 2 / 7 | not ready: safe path of the real critic launch | 87 |
| 6 | 09-01 | 4 / 6 | not ready: publication of private material after a .gitignore change | 93 |
| 7 | 09-01 | 2 / 5 | not ready: privacy on the standard development path | 94 |
| 8 | 09-01 | 2 / 4 | not ready: privacy semantics need the operator's decision | 95 |
| 9 | 09-01 | 4 / 5 | not ready; the operator decides | 98 |
| 10 | 09-01 | 2 / 7 | operator depth rulings follow this round | 98 |
| 11 | 09-01 | 0 / 5 | first round with no new territory | 105 |
| 12 | 09-01 | 0 / 4 | | 105 |
| 13 | 09-01 | 2 / 2 | not ready to stop | 105 |
| 14 | 09-01 | 1 / 2 | not ready to stop | 110 |
| 15 | 09-01 | 0 / 2 | not ready to stop | 113 |
| 16 | 09-01 | 0 / 2 | not ready to stop | 116 |
| 17 | 09-01 | 0 / 1 | not ready to stop | 117 |
| 18 | 09-01 | 2 / 0 | not ready to stop | 119 |
| 19 | 09-01 | 2 / 1 | not ready to stop | 122 |
| 20 | 09-02 | 1 / 0 | not ready to stop | 123 |
| 21 | 09-02 | 0 / 1 | not ready to stop | 125 |
| 22 | 09-02 | 0 / 0 | ready to stop; finalised by the operator's word | 125 |

117 findings over the run as raised, round by round: 46 on new territory and 71
descendants of earlier fixes (round 4 carried no count); a descendant raised
again in a later round counts again. Every one has a written outcome. Two
findings were level-1 escalations (both about privacy) and were closed by
the operator's depth rulings recorded in `task_description.md`; everything
else was the working layer, disposed of by development.

## Where the human gates are

The harness that now journals every operator question and answer verbatim
(`gates.md` in a run catalog) is the thing being built in run 3, so these
three catalogs predate it. The operator's rulings are recorded where they
landed at the time: as sections of `task_description.md`, and inside the
outcomes as "развилка оператора, решена" or "передано оператору и решено
им", with the operator's words quoted where they were quoted («Да,
переноси», «Согласен с финализацией», «Финализировали»). The gate at the
entry of each run, the operator confirming the round-1 description, is
recorded in the authorship note at the top of each `task_description.md`.

## What is not here

- No pre-review intents as files for runs 1 and 3: the round-1 description
  served as the entry gate. Run 2 has its post-review intent.
- No semantic map: this cycle's code was not mapped. The map phase closed
  the two code cycles that preceded it, and the article says so.
- The free-form phases (the operator's first sentence and the discussion
  that followed) are not exported, and for this cycle their literal
  transcript was not written onto the cycle's anchor either: the anchor in
  the author's private knowledge graph holds the pre-review intent of the
  earlier specification this rebuild replaced, the run summary written at
  finalisation, and links to the feedback records the cycle answered.
  Nothing from the graph is exported here.
- The critic's passes are as received, but the critic's own working (the
  commands it ran, the files it read) was not captured by the manual
  launches of these runs; the harness records that from its first live run
  on.

## Language and redactions

The cycle ran in Russian, and the records are quoted in Russian: a
translated record would be a retelling. The guide is in English. Two
mentions of a commercial project, cited as a precedent (one in the outcomes
of run 1, one in the description of run 2), are replaced by
`[redacted: project name]`; `MANIFEST.md` shows which files carry a
redaction and which are byte-for-byte the source.
