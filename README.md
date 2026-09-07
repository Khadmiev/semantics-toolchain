# semantics-toolchain

A toolchain for treating semantics as source: versioned meaning, gated
lowering, independent review, semantic maps.

This repository is the checkable half of the article [*Semantics Is the New
Source Code*][article]. The article's claim is that with large language models the
source of what you build stops being code and becomes meaning, and that the
model is a probabilistic compiler of that meaning: it can misread its source,
and nothing crashes when it does. What follows from that is not a better
prompt but a toolchain for the new layer: a store where meaning carries
status and provenance, a review loop with an independent critic, a
decompiler that reads back what was actually built, and gates where a person
still decides. Everything in this repository was built through itself; every
incident the article tells comes from that self-assembly.

Code is not a promise. This page is the map from the article's words to the
files, commands and records that let you check them.

## Where the article's words live

| The article says | Look here |
|---|---|
| Meaning kept in a **status-bearing, addressable store**: every claim carries a status (provisional, in force, superseded), a provenance and a reason; rejected alternatives are full citizens; history is never erased, only superseded | `src/assistant_memory/models/graph.py` (node, version, status); MCP tools `remember_decision`, `remember_fact`, `update_node`, `link` (edge type `supersedes`), `explain`, `timeline` in `src/assistant_memory/mcp/tools.py`; the rules in `docs/conventions/assistant_memory_rules.md` §4–6 |
| The **rules for using the memory live in the memory itself**, as nodes with statuses and versions | `docs/conventions/assistant_memory_rules.md` is the source of record; `scripts/seed_conventions.py` projects it into the graph; the MCP tool `conventions` serves it to any agent on first contact |
| The **development cycle** is the unit history is kept in: an anchor node, and everything the slice produced hangs off it, each document written at its own finalisation | `docs/conventions/assistant_memory_rules.md`, Plugin `review` (the anchor rules); `docs/first_steps.md` §2 (how you declare one); the node type `DevelopmentCycle` |
| The compiler is **forbidden to invent semantics**; a hole in the source goes back to a human | `docs/review/critic.md` ("invention where a ready-made solution exists is a finding"), `docs/review/development.md` (the WHAT/HOW boundary; "ready-made first"; forks go to the operator) |
| The review loop is **two texts, a few pages each, and a thin harness** | `docs/review/critic.md`, `docs/review/development.md`; `src/review_harness/` (its specification, as reviewed: `cycles/review-harness/2_harness_spec/artifact_spec.md`) |
| The critic is a **different model**, every pass is **cold**, its memory is the written record of the run | the machine profile binds each role to a command (`src/review_harness/machine_profile.py`); a fresh critic process per round (`src/review_harness/critic.py`, `launcher.py`); the run catalog `docs/review/<run>/round_NN.md` holds the pass verbatim beside development's outcomes |
| **Gates are mine**: the task description is confirmed before the first pass, the review closes only on the operator's word, questions and answers are journaled verbatim, open questions are recorded as open | `src/review_harness/gates.py`; the journal `gates.md` in the run catalog (`src/review_harness/runcat.py`) |
| The critic **recommends** stopping in one line, development **never** declares convergence, the **trend line** reaches the operator untouched | `docs/review/critic.md`, "Verdict and stopping"; `docs/review/development.md`, "Stopping" |
| **Coverage is a duty of the pass**: whole artifact against whole description, both directions, every round | `docs/review/critic.md`, "The pass: full coverage, every round" |
| Every finding gets a **written outcome**; a finding that touches an operator decision, privacy or the threat model is **never closed inside the pair** | `docs/review/development.md`, "Disposing of findings"; `docs/review/critic.md`, "Description and authorship"; the conventions' Plugin `review`, "Finding levels" |
| Findings carry a **confidence level** and say what could not be checked | `docs/review/critic.md`, "Findings" |
| The critic runs **read-only by the shape of the command**, and the tool's own header must attest to it | `src/review_harness/readonly_guard.py`, `src/review_harness/machine_profile.py` |
| **Probes instead of assurances**; a check that cannot run **fails loudly**; liveness windows are grounded in the machine's observed gaps | `src/review_harness/liveness.py`, `src/review_harness/machine_profile.py`; `python -m review_harness.cli profile-check`; the principles as reviewed: `cycles/review-harness/2_harness_spec/artifact_spec.md`, sections 1, 4 and 7 |
| The **intent**: the specification translated into one operator's language, against a recorded profile; a delta of meaning opens every re-read | `docs/prompts/review_orchestration/intents.md`; `docs/conventions/assistant_memory_rules.md` §7 |
| The **operator profile** is compiled from whole exchange pairs; every line is marked as interpretation; a stated preference binds at once, an inferred one only when confirmed | `src/assistant_memory/profile/service.py`, `src/assistant_memory/models/profile.py`; MCP tools `get_operator_profile`, `remember_preference`, `confirm_preference`; the rules in `docs/conventions/assistant_memory_rules.md`, Plugin `operator-profile`; the harness side `src/review_harness/profile_sync.py` |
| The **semantic map**: a blind, base-free reading of the built system, every statement resting on a file and a line; beside the intent, not replacing it; raises no findings | the skill `docs/prompts/review_orchestration/semantic_map/SKILL.md` with its output template; `semantic_map_delta.md` (what the loop does not ask of the skill); `docs/conventions/assistant_memory_rules.md` §7; how to run it blind: `docs/semantic_maps/README.md` |
| **The machinery turned on this repository**: a map of this toolchain's own harness, generated blind against this tree, every pointer resolving; the recipe for mapping the other layers, which ship their maps with later snapshots | `docs/semantic_maps/review_harness.md`; `docs/semantic_maps/README.md` |
| The **audience review**: a contract (who the document is for, what it should leave them with), a blind reader, a seeing pass, a strategic reader that refuses without a declared aim, a comb for signs of machine authorship, a strict report schema, an environment probe of the reader's runner | `src/assistant_memory/audience/`; prompts `docs/prompts/audience/` |
| **One cycle, from an idea to a working mechanism**: the article narrates the cycle that built the audience genre; its records live in the journals of the loop that was thrown out and are not published. What is published, in full and in the same form, is the next cycle: the one that built the review loop itself | `cycles/review-harness/`: the pair reviewing its own text, then the harness specification, then the harness code; 28 critic passes verbatim with development's outcomes and the operator's gates, in reading order |
| **The failures**: semantic loss, semantic invention, semantic contamination, degradation of the verifier | `evidence/`: one dossier per incident — a context header, the primary record verbatim in the language it happened in, a marked English translation, visible redaction markers |
| The **install** is done by your agent, stage by stage, and the service says "not ready" until the acceptance run passes | `docs/setup.md`, `docs/first_steps.md`, `src/assistant_memory/install/`, `docs/acceptance/record.schema.json` |

## What a role needs, not which model

Roles are configuration, not doctrine. The binding of a model to a role lives
in the machine profile of the installation.

| Role | Needs | Verified live | Expected, not verified |
|---|---|---|---|
| **development** (owns the artifact between rounds, disposes findings, talks to the operator) | an interactive agent session with file tools and an HTTP or MCP client to the memory server | Claude Code | any agent with the same capabilities |
| **critic** (reads the whole repository, reviews the artifact, writes nothing) | a headless one-shot invocation by command, with a read-only sandbox the tool attests to in its own output | Codex CLI (`codex exec`, sandbox `read-only`) | other headless CLIs; an OpenAI-compatible endpoint behind a small runner |
| **blind reader** of the audience review | an isolated profile with no knowledge of the project, launched per pass | Codex CLI | — |

The rule that survives any binding: the side that produces never signs off
its own work, and the critic runs on a different model from development.

## Installing

Do not install this by hand. Hand the repository to your LLM agent and point
it at `docs/setup.md`: the runbook is written for an agent, stage by stage,
with a check after every stage. The agent does not go past a red check and
does not improvise around one.

What stays with you: the secrets (an OAuth client, tokens, passwords), which
the agent never types, and a few decisions the runbook asks for at step zero.
About ten minutes.

Three deployment variants are covered: a local machine, a remote machine you
own, a cloud host. The tools the author uses are named as examples; what the
product requires is a property (an address reachable by your clients, TLS),
not a vendor.

Until the acceptance run passes, every entry point of the service answers
"not ready" and names the stage that is still open. That is behaviour, not
documentation.

**What was and was not checked for this release.** The whole test suite runs
inside this tree, and the schema was built from these migrations against an
empty database and matched the author's own installation table for table. The
runbook itself was NOT walked end to end for this release: no container was
built from this tree, no owner logged in, no finishing probe was performed. The
runbook is the one the author installs by, and the install machinery is covered
by tests, but a full clean install from this snapshot has not been performed.
If yours fails, that is worth telling the author about — see below.

## Languages

This project is developed in Russian with English mixed in, and this
distribution carries that. Everything you need in order to install,
configure and run it is in English: this README, the setup guide, the
first-steps walkthrough, and the comments and docstrings throughout the
code. That last one is checked at export by a gate rather than promised
here — the release cannot be assembled while a Russian comment remains.

What is still in Russian is text the machine consumes rather than text you
read: the operating texts of the review pair under `docs/review/`, the
prompts of the audience roles, and the strings the product hands to a model
or shows an operator. Translating those changes what the machine receives,
so it is a change to the product with a live run of its own, not a text
pass. It is the next release, and the development log carries it.

A ruling quoted in the language it was said in stays in that language —
inside the code's prose as much as under `evidence/`, where a primary record
is quoted with a marked translation beside it. A translated record is a
retelling, not a record; the English around a quote says what it settled.

The text addressed to a human at runtime follows the language you choose at
install; English and Russian have been run live, other languages have not.

## Where this came from

This repository is assembled from a private working one, and the seams show.
Comments here and there cite that repository's decision log, its review
records and its graph node ids, and a few point at design documents that are
not part of the distribution. They are traces of the process this project is
about; you need none of them to read or run the code.

## What is not here

- The hyperparameter-optimisation module and the Telegram bot of the working
  installation: neither is promised by the article, and neither is in the
  first publication.
- The fifteen-module sweep of the commercial system, and the primary records
  of the two incidents from it: they carry a client's table names, model
  identifiers and business rules. The article narrates them anonymised.
- The working repository's history. This repository is assembled from a
  manifest at release; the tag names the commit the article's claims were
  checked against.

The audience review, applied to the article itself, is described in the
article by fact: where it stopped, and why.

## Reporting a problem

Your agent can report a defect to the maintainer. What leaves your
installation is what you saw and confirmed, and nothing else: which call was
made, in what state, what the product answered, what was expected, whether it
reproduces on the stock release, and the list of files that differ from the
release tag. No node contents, no quotes from your project. A change you made
to prompts, conventions or thresholds is a declared use, not a defect; a
defect is what reproduces on stock.

Open an issue. There is no template and no form: the list above is what makes
a report useful, not a shape it has to take.

## Contributions

This repository is a published snapshot, not a working tree. It stands at the
commit the article's claims were checked against, and it changes only when the
article's author re-exports it, so pull requests cannot be merged here. Fork
it, change it, run it — and if something is broken, an issue is more useful
than a patch.

## Licences

Unless otherwise noted, all original content in this repository, including
source code, skill definitions, prompts, documentation, the conventions
contract and the incident dossiers, is licensed under the Apache License 2.0
(`LICENSE`, SPDX `Apache-2.0`). In ordinary words: use, copy, modify, run
and redistribute it, inside a company as much as at home, in open or closed
products; keep the copyright notices, the licence text and this NOTICE with
it, and mark the files you changed. The licence includes a patent grant from
the author to every user.

Third-party quotations and other third-party materials remain subject to
the rights of their respective owners and are not licensed under the Apache
License 2.0 except where explicitly stated.

See `NOTICE` for the exact statement.

[article]: https://medium.com/@hadmievdm/semantics-is-the-new-source-code-c3286dca1d15
