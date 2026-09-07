# Semantic maps of this toolchain

A semantic map is a reading of what got built, reported at the level of
decisions: what this system decides, in what order, with every statement
resting on a file and a line. It is the phase of the development cycle that
reads the built system whole, without a base, and it is the operator's
instrument: it raises no findings, enters no ledger, and cannot reopen a
converged review.

The maps in this directory were produced by the skill in
`docs/prompts/review_orchestration/semantic_map/SKILL.md` against this tree,
at the commit named in each map's header. Every `file:line` a map cites
resolves in that commit.

## How a map is produced blind

The value of a map is that it is written by a party that has seen no account
of the work. Running it is a recipe, not a mechanism, and the recipe is the
whole guarantee:

1. Start a fresh agent session in a clean checkout of the commit to be mapped.
   Give it no intents, no review traffic, no findings, no chat history of the
   work. The session may read the specification of the layer as an aid to
   meaning; it may never use it as the ground of a statement.
2. Install the skill from `docs/prompts/review_orchestration/semantic_map/`
   (the directory is the skill; its `references/output_template.md` is the
   shape of the output).
3. Ask for the semantic map of one layer, by directory, and nothing else. One
   layer per map; a map of everything is a map of nothing.
4. Save the output here as `<layer>.md`, with the commit hash in the header.
5. Read it against the design and the post-review intent yourself. The pair of
   documents is what locates a defect: the intents agree and the map differs,
   look in the implementation first; the map matches the code and the intent
   misdescribes it, look in the self-description first; all three agree and
   you still object, look in the design first.

`semantic_map_delta.md` beside the skill records what the review loop does
NOT ask of the skill, so that nobody re-derives a withdrawn contract from
memory.

## The maps

Regenerated at every release against the released commit. A map whose
pointers no longer resolve is stale by definition and is removed rather than
kept.

| Layer | Map | Generated against | Pointers |
|---|---|---|---|
| the review harness, `src/review_harness/` with `tests/harness/` | `review_harness.md` | the exported tree of commit `960f86b`, 2026-09-07, the tree this release ships; regenerated blind after the operator-word parser changed following the cold review of the first export. **Boundary of applicability:** after that commit `src/review_harness/gates.py` changed in one place (a question mark anywhere sends the word back for clarification; `!`, `;`, `:` are cut like commas), so the parser table in section 5 of the map predates that rule; everything else in the map is unaffected | 437, every one re-resolved after writing |

One map ships with this release. The other layers (the audience genre, the
graph core with its MCP tools, the install, the operator profile) get their
maps with later snapshots, each generated against the tree it ships in.

The map was produced by the recipe above: a fresh agent session on a clean
copy of the exported tree, given the skill and the name of one directory,
nothing else. Its divergence flags and the rulings it asks for are the map's
own claims, written as interpretation; the operator's rulings on them are not
part of the map.
