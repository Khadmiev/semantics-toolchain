# SPDX-License-Identifier: Apache-2.0
"""The Assistant-Memory operating conventions, and the seeder that projects them.

The memory system documenting itself: the operating spec every agent must follow
(docs/conventions/assistant_memory_rules.md) is projected into the graph as a
canonical, self-contained Document + atomic children, so any consumer that reaches
the shared memory discovers it on first recall (§17) and persists the behavioral
subset to its own local memory (§18).

``seed_conventions`` is idempotent (upsert by (type, label) within the space) and is
shared by two callers: the startup bootstrap (with ``embedder=None`` -> full-text
index only, no model, so it can gate readiness cheaply) and the CLI script
scripts/seed_conventions.py (with the real embedder). It flushes but does not commit
— the caller owns the transaction.

Two guards bound what a publish pass is allowed to do, both written after the seeder
manufactured a full second projection on 2026-08-10:

* it REFUSES outright when a projection already exists in a DIFFERENT space. The dedup
  above is per-space and therefore blind to precisely the copy it is about to make; a loud
  stop beats two divergent copies of the spec every agent is required to follow.
* it never RESURRECTS a retired node. Retirement (`superseded`, `rejected`) is an operator
  act; publishing a spec edition is not entitled to reverse it. Such nodes are left alone
  and counted as skipped.

It does NOT build the feedback hub. Feedback belongs to the project, not to the
conventions; once the two live in different spaces a hub built here would be the only edge
crossing that boundary, and built in the wrong space. The hub is the feedback tool's own
concern, in the feedback destination space — the labels below stay here only because they
name a reserved node that both sides must agree on.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models.graph import Node, NodeSpace
from .repository import graph as repo
from .search import Embedder, index_fts, index_node


class SeederAmbiguity(RuntimeError):
    """Two same-labeled candidates where the seeder must pick one (B.13 A-8).

    The anchor choice used to be ``limit(1)`` with no ordering — a lottery between
    same-labeled nodes, and a recorded near-miss (2026-08-27: a cancelled duplicate
    with 28 children had to be relabeled because of it). Ambiguity is an ERROR that
    names the candidates, never a coin toss: resolution is the operator's.
    """


class ProjectionElsewhere(RuntimeError):
    """A conventions projection already exists in another space; seeding here would duplicate it."""


# Statuses a publish pass must never lift back to `current` (see the module docstring).
RETIRED_STATUSES = ("superseded", "rejected")

DOC_LABEL = "Assistant-memory conventions"
TAG_LABEL = "assistant-memory-conventions"
SOURCE_PATH = "docs/conventions/assistant_memory_rules.md"
SPEC_VERSION = "v2.7 (2026-09-02)"

DOC_PROPERTIES = {
    "text": (
        "Canonical operating spec for one or more agents (Claude, Codex, ...) sharing ONE "
        "long-lived assistant memory: the knowledge graph (granular domain knowledge) plus the "
        "file-based memory (how the agent works, who the operator is, live status). Read this on "
        "first contact with the memory, then persist the behavioral subset to your own local "
        "memory. Project-agnostic Core (Part I, always on) + composable opt-in plugins (Part II). "
        "The framing: not 'how to keep a graph tidy' but 'how multiple agents share one durable "
        "memory without corrupting it' — consistent writes (this spec is the contract), "
        "disciplined reads (reading precedes acting, §17), honest aging (kind x status, §6), safe "
        "growth (first-encounter policies §11 + periodic review §14). Each rule is an atomic child "
        "of this anchor; the full readable source of record is the markdown at `path`."
    ),
    "path": SOURCE_PATH,
    "scope": "project-agnostic; applies to any long-lived project sharing this memory backend",
    "refresh_note": (
        "Source of record is the repo markdown; the Document is living data that evolves via §14. "
        "After editing the spec, re-run scripts/seed_conventions.py to re-project."
    ),
    "version": SPEC_VERSION,
}

# (key, label, text) — atomic children. REFS (below) wires cross-section references.
SECTIONS: list[tuple[str, str, str]] = [
    ("s1", "§1 Two stores: graph vs files", (
        "Two stores; confusing them is the primary error. The GRAPH holds granular domain "
        "knowledge (how things work, decisions + why, incidents, hypotheses, experiments, "
        "requests) and must be self-contained so 'how did we solve a problem like X' works "
        "WITHOUT repo/source access, from another project. The FILES (MEMORY.md + notes) hold: how "
        "the agent should work (feedback), who the operator is (user), and live operational status "
        "+ external pointers (project/reference). Boundary: 'how the domain / a decision works' -> "
        "graph; 'how I work / who / what is happening right now' -> files. Live status is also "
        "mirrored in the graph. A client with NO persistent local store at all cannot hold this "
        "file layer -- see Plugin stateless-client.")),
    ("s2", "§2 Self-containment", (
        "A link from the graph into a source repo/doc leads nowhere from outside, and an external "
        "consumer never sees the linked artifact. So the graph carries the SUBSTANCE, not a "
        "pointer. provenance / links are an addition for those who have the source, never a "
        "replacement. Applies to every artifact, including semantic maps (the graph cannot "
        "'reference and forget'). TWO MEMORY SURFACES -- private and public (v2.4): a project may "
        "keep memory on two surfaces -- the GRAPH is PRIVATE (the whole process: what was "
        "rejected, the deltas, the rationale) and the REPOSITORY/GIT is PUBLIC (the result that "
        "ships). A fact's absence from the public surface is often DELIBERATE, not an omission: "
        "process artifacts live only in the private one. This STRENGTHENS self-containment -- the "
        "graph does not duplicate the repo, it holds what the public surface intentionally leaves "
        "out (consequence for gitignored process artifacts: §9).")),
    ("s3", "§3 Granularity", (
        "One node = one idea. Tests: would someone pull this out on its own, in another project? "
        "Does it have its own 'why'? Tie-breaker: if two facts are always used together and "
        "neither has independent value, they are ONE node. Organizing pattern: a thin hub holds "
        "orientation, the real content lives in atomic children; a big document = a thin anchor "
        "node + atomic children.")),
    ("s4", "§4 Core node taxonomy", (
        "Project-agnostic node types (plugins add more): Note (fact/rule/contract/observation, "
        "standing carried by status); Decision (one ruling: decision + why + lesson, plus "
        "alternatives/rejected/tradeoffs/consequences where a real fork existed — no separate ADR "
        "type); Hypothesis (a conjecture with its own lifecycle); Experiment (an empirical "
        "investigation that chose/rejected by measurement, incl. negative results); OpenQuestion "
        "(an explicitly parked unresolved design question); ExternalRequest (an incoming ask that "
        "drives work — a stimulus, not knowledge); Incident (a failure: "
        "symptom/root_cause/fix/lesson/status); Idea (a canonical principle over instances — used "
        "sparingly); Document (a thin anchor for a map/doc); ProjectState (live operational state "
        "mirroring a file). Roots: Project, Person, Tag.")),
    ("s5", "§5 Core edge taxonomy (src -> dst)", (
        "contained_in (the tree, one parent: child->hub, hub->Project); depends_on "
        "(dependency/flow: consumer->producer, a blocked OpenQuestion->its unblocker); references "
        "(uses / rests on / cites); supersedes (new->old, evolution/retraction); caused_by / fixes "
        "(incident semantics); relates_to (weak association/contrast); tagged_with (any node->a "
        "Tag); informs (an Experiment->the Decision its evidence justified); motivates (a stimulus "
        "ExternalRequest/Hypothesis->the work it produced: Experiment/Decision/Task).")),
    ("s6", "§6 Epistemic model — kind x status", (
        "Two ORTHOGONAL axes so a year later you can still answer 'is this an established fact, or "
        "do we think so, or was it overturned?'. KIND is the node type (Note/Decision/Hypothesis/"
        "Experiment/OpenQuestion/ExternalRequest) — the nature of the knowledge + its lifecycle. "
        "STATUS is a small first-class, queryable field, default current: current (in force) | "
        "provisional (tentative / a raw observation) | superseded (replaced -- with an edge "
        "recording what replaced it: supersedes for evolution of the same thing, or a "
        "plugin-declared displacement edge, e.g. the skills plugin's displaces) | disputed | "
        "rejected (with rejected_reason). A REJECTED hypothesis/proposal is "
        "RECORDED, not deleted — recording the rejection is the whole point: it stops the idea "
        "being re-proposed and re-argued. Transitions: ->superseded (mechanical) and ->disputed (a "
        "reversible flag) are autonomous; provisional->current and ->rejected need a CONFIRMING "
        "EVENT (experiment result / verification against ground truth / operator confirmation). "
        "Default when unsure: propose, don't flip.")),
    ("s7", "§7 Human-readable rendering (semantic map & intent)", (
        "A person reading top to bottom must SEE what a thing means and decides, without reading "
        "the code or the spec. ONE GENRE in THREE TENSES over the life of a change (v2.4): the "
        "PRE-REVIEW INTENT of a spec ('what we are about to build'), the POST-REVIEW INTENT of a "
        "spec ('what we will finally build' + a delta), and the SEMANTIC MAP after implementation "
        "('what was built' + a delta) -- the same readable projection at the level of MEANING, "
        "differing only in what they are grounded on and when read. Rules of the genre: (1) "
        "COMPLETE ON DECISIONS, FREE OF MECHANICS -- complete on every resolved fork and every "
        "rule that binds behaviour; free to omit the machinery (tables, indexes, structures, "
        "files). Test: 'in this situation the system does that' -> in; 'field X of type Y with a "
        "partial index' -> out. (2) THE ONE EXCEPTION -- a DECLARED CONSTRAINT: machinery enters "
        "when it is the answer to a declared constraint (disk, tokens, latency, cost, privacy, a "
        "size limit; declared by the operator in the moment OR by project rules as load-bearing) "
        "-- then it is itself a decision with its own why, rendered at the level of meaning ('we "
        "stage into a temp table to avoid a second full pass'), as non-technically as the "
        "constraint allows. (3) STYLE: plain continuous prose; every abbreviation expanded on "
        "first use; code/§/commit pointers collected in one closing 'where this lives' section, "
        "never woven inline; bilingual where the operator works bilingually (source-of-record + "
        "canonical copy, kept consistent). (4) DELTA SECTION (opens every re-read) -- FOR THE INTENTS "
        "(v2.5): the POST-REVIEW INTENT, read again after a gate, OPENS with a 'what changed in "
        "meaning since the last reading' section, at the FRONT not the tail -- not a findings "
        "ledger (that is bookkeeping) but a substantive account ('I meant it this way; it turned "
        "out wrong; now it is this way'). THE MAP IS EXEMPT and the exemption is the point: it "
        "has never read the intents, so it has nothing to take a delta against. "
        "(5) TWO GROUNDS, AND THE MAP IS BLIND rather than merely cold (v2.5): the intent is "
        "decompiled from the SPEC, the map from the CODE at the converged commit. The map "
        "is given NO ACCOUNT OF THE WORK AT ALL -- no intents, no review traffic, no findings. "
        "Its independence is the product: a reader who has seen the review's own account cannot "
        "help reproducing it, and a reproduced account is what the operator already read in the "
        "intent. The spec is available as an aid to MEANING and may NEVER ground an assertion -- "
        "every statement in a map rests on a code address. THE MAP DOES NOT SUPERSEDE THE "
        "POST-REVIEW INTENT (v2.5, reversing v2.4): it stands BESIDE it, and the pair is what "
        "makes a defect locatable -- operator ruling 2026-08-23 ('пусть рядом постоит: так можно "
        "восстановить, на каком этапе дыра появилась'). Intents agree and the map differs -> the "
        "defect is in the IMPLEMENTATION; the map matches the code as designed but the "
        "post-review intent misdescribes it -> the defect is in the SELF-DESCRIPTION; all three "
        "agree and the operator still objects -> the defect is in the DESIGN; the map contradicts "
        "the post-review intent OF THE SPEC while the code faithfully implements the spec -> the "
        "spec was SUMMARISED AS SOMETHING IT WAS NOT and consent was given to that (the worst "
        "address, and superseding would erase the document that reveals it). The comparison "
        "itself is not lost: it is a SEPARATE PASS, run by a reader that IS allowed to see the "
        "intents and can therefore quote them, whose output is a list for the operator rather "
        "than a delta inside the map. (6) THE MAP IS THE OPERATOR'S INSTRUMENT, NOT THE LOOP'S "
        "(v2.5): it raises no findings, enters no ledger, is owed no disposition, and cannot "
        "reopen a converged review -- operator ruling 'Карта ничего не переоткроет. Это мое и "
        "только мое решение.' A SPEC CYCLE PRODUCES NO MAP AT ALL (ruling 2026-08-23): a map of a "
        "spec would be a model reading a text against a model's summary of the same text -- one "
        "medium, weak independence, poorer yield. "
        "The rendered document is the "
        "readable long-form; the graph node is the compact index; both carry the substance (§2). "
        "Timing -- when each tense is written -- is §9.")),
    ("s8", "§8 When to write / when NOT to write", (
        "WRITE when the substance is durable and has a future reader: a decision, a fact, an "
        "incident with a lesson, an experiment (incl. negative results), a hypothesis with "
        "re-litigation risk, an external request, an unresolved question. Do NOT write: one-off "
        "discussions and intermediate thoughts; transient ideas that got no substantive "
        "evaluation; restatements of what a doc/repo/git already records (unless something "
        "non-obvious was learned — then store that part); anything derivable on demand or purely "
        "ephemeral; anything with no plausible future reader. The discriminant for rejected ideas "
        "is RE-LITIGATION RISK, not 'rejected': record ideas substantively considered and turned "
        "down; skip ideas that never got real engagement. Long-lived memory degrades from "
        "low-value accumulation more than from bad structure.")),
    ("s9", "§9 Timing, and durable-vs-live status", (
        "Graph timing: (1) NOT inside an iterative-review cycle; (2) on completion of a review, "
        "record the changes; (3) on any change to a doc/spec/map, reflect the substance into the "
        "graph. TWO SEPARATE CLOCKS — do not conflate them: recording DECISIONS/facts fires on "
        "review-completion / any doc-spec change REGARDLESS of whether code exists (record them "
        "as provisional per §6 until a confirming implementation/result); only the readable "
        "RENDERING waits per its tense. 'No implementation yet' is NEVER a reason to withhold a "
        "decision from the graph — that just offloads the catch onto the operator. HUMAN-READABLE "
        "RENDERING TIMING (v2.4): the rendering follows the change through its tenses (§7) -- the "
        "pre-review intent before the review, the post-review intent at convergence, the semantic "
        "map once an implementation exists; both intents obey the §7 genre rules (a delta section "
        "on re-read; grounded cold on the spec). 'A PURE SPEC GETS NO MAP' CLARIFIED: a spec with "
        "no code yet still gets its readable rendering -- the POST-REVIEW INTENT is that "
        "rendering (de-facto the map at the 'no code yet' stage); there is no SEPARATE map until "
        "code exists, and when it does the map (written BLIND from the code) stands BESIDE the "
        "post-review intent rather than replacing it (v2.5) -- the pair is what localises where a "
        "hole appeared, and a SPEC cycle produces no map at all. GRAPH PRIVATE, GIT PUBLIC -- the durable carrier of a promise is the "
        "GRAPH (v2.4): because the graph is private and git public (§2), process artifacts (the "
        "intents) MAY be gitignored by project rules; this does NOT cancel the finalize-time "
        "graph-sync of decisions -- it makes the graph the SOLE DURABLE CARRIER of what was "
        "promised, so for a change with gitignored process files the finalize graph-sync is not "
        "optional, it is the only place the promise survives. AND WHAT IS OWED THAT SYNC IS NOT "
        "ONLY DECISIONS (v2.6): this rule declared the graph the sole durable carrier while the "
        "sync it named covered decisions alone, so the two documents carrying the promise in the "
        "operator's own words -- the INTENT SUMMARIES and the LITERAL TRANSCRIPT of the cycle's "
        "free-form phases -- fell outside it and were durable nowhere; they are inside it now, "
        "each written to the development cycle's anchor IN FULL TEXT rather than as a path, at "
        "its own finalization (Plugin review). A path is unreadable to anyone without the "
        "repository, and the parties that must later read these documents have none. Status by change-speed: "
        "durable/architectural -> the hub's current_version (dated, updated on "
        "rework/registration/milestone); live/operational -> a project_*.md file AND a mirroring "
        "ProjectState node, updated in the SAME motion (that is how drift is prevented). Rule of "
        "thumb: changes on a tick -> file + ProjectState; changes on a registration/rework -> "
        "hub. ENTRY POINTS MOVE WITH THE MEANING (same-motion, v2.2): when a unit's POSITIONING / "
        "canonical meaning / key framing changes (not a routine status tick), update its entry "
        "points in the SAME MOTION as the content -- the hub's summary (Project/Component) and, "
        "for an agent with a local file store, that agent's own session-start record. Recall "
        "reads the hub first (S17), so a canonical shift recorded only as a child node never "
        "reaches the next reader: correct content parked where no reader looks first is a "
        "silent-staleness bug, not a completed write. Test question: 'if the next session reads "
        "only the hub and its own start-up context, does it get the new key?' (Motivating "
        "instance 2026-07-11/12: a confirmed canonical repositioning was recorded as a child Idea "
        "node while the Project hub summary and the agent's local memory kept serving the old "
        "framing -- caught by the operator, not by the rules.)")),
    ("s10", "§10 Source of truth", (
        "The truth is the AUTHORITATIVE GROUND ARTIFACT of the domain; verify against it before "
        "asserting, and trust neither a stale map nor old memory. It varies: engineering -> the "
        "code on HEAD (+ the live schema/warehouse); research -> the data / experiment results; "
        "product/design -> the current design doc / decision of record; legal/knowledge base -> "
        "the primary sources. When there is no single ground artifact, say so and record the claim "
        "as provisional (§6).")),
    ("s11", "§11 Extending the taxonomy & resolving forks", (
        "Prefer an existing type/edge. A new type is justified only when it is a genuinely "
        "distinct KIND that (a) recurs, (b) existing types force you to misuse, and (c) enables a "
        "query you otherwise cannot make; adding one is deliberate and documented. Resolving an "
        "unspecified policy fork: the first time a real fork appears that this ruleset does not "
        "pre-decide (plugin collision, an autonomy question, an ambiguous cleanup call), ASK the "
        "operator, record the choice as a project-scoped policy, then apply it automatically "
        "thereafter. Record every such policy in ONE project-scoped register (clearly marked "
        "project-local, never Core, dated, with the fork that created it) so 'architectural "
        "invariant' vs 'historical local choice' stays answerable; the §14 pass reviews them. Core "
        "invariants are not subject to this — they always win.")),
    ("s11a", "§11a Rule provenance — a rule carries its reason", (
        "When you record a rule/invariant, record its REASON, and tag the KIND of reason (v2.4): "
        "TECHNICAL -- it follows from a limitation of the current tool/implementation; when that "
        "limitation goes away the rule is REVISITED WHOLE, not propped up with exceptions. "
        "PRINCIPLED -- it follows from the nature of the thing; do not touch it. A rule without a "
        "reason will be COMPLETED BY THE NEXT READER WITH AN INVENTION, which they then defend as "
        "if it were the original ground (not hypothetical: a fabricated EPISTEMIC justification "
        "for the review plugin's 'no graph writes' invariant was caught in review; the true "
        "reasons were two TECHNICAL ones). Meeting a rule with no reason, an agent does NOT "
        "invent one -- it ASKS the operator and records the answer.")),
    ("s12", "§12 Writing a node", (
        "Label specific + searchable by meaning ('Silver-driven refund: no REFUND webhook', not "
        "'decision 3'). Properties = a dense but SELF-CONTAINED retrieval unit (abbreviations OK; "
        "dense != empty pointer — understandable without the source; this is the difference from "
        "the readable semantic map). provenance ALWAYS (file:line / commit / § / source ref). "
        "lesson where portable (a takeaway with no project jargon). status where it isn't the "
        "default current (§6).")),
    ("s13", "§13 Write discipline", (
        "Search before writing (dedup) -> update, not a duplicate. Supersede, don't delete (keep "
        "history); delete only a fact that turned out to be wrong. Retry a transport-failed write "
        "(e.g. a gateway flake) with the exact same call — a failed call left no partial write.")),
    ("s14", "§14 Maintenance & decay", (
        "Long-lived memory needs upkeep, not just intake: periodically consolidate duplicates and "
        "near-duplicates; prune stale/low-value nodes; collapse supersedes chains (keep head + "
        "lineage); revisit status (promote a confirmed provisional to current, flip overtaken "
        "nodes to superseded/rejected/disputed, resolve stale OpenQuestions); a long-open "
        "Hypothesis is a review trigger; review project-scoped policies (promote to Core if proven "
        "general, retire if obsolete, else keep local) so first-encounter choices don't accrete "
        "into a shadow Core. Cleanup is CONSERVATIVE, default-to-keep: prune only the demonstrably "
        "wrong or an exact duplicate; low-value/stale content is demoted (status) or consolidated, "
        "not deleted; deleting non-duplicate content needs operator confirmation (writes are "
        "audited/undoable). When in doubt, keep + flag.")),
    ("s15", "§15 Pattern layer — cross-project findability", (
        "pattern:* tags are a CONTROLLED vocabulary (a small fixed set), project-agnostic (NOT "
        "under any project tag — that is the point), tagging any node whose lesson instantiates "
        "the pattern; adding a tag is deliberate (sprawl kills findability). This is the primary "
        "cross-project index. Idea nodes carry the canonical statement of a recurring principle "
        "and reference their instances — used sparingly. lesson strings stay on nodes; tags + "
        "Ideas are the index layer on top.")),
    ("s16", "§16 Recipe (per unit)", (
        "1) Read the unit's map; verify freshness against the ground artifact (§10). 2) Fresh -> "
        "write directly; status header stale -> targeted patch; diverged in substance -> full "
        "rewrite; touched an implemented unit with no/dense map -> write it readable (§7). 3) Sync "
        "the Document refresh_note, then the nodes; if the unit's canonical meaning shifted, "
        "update "
        "its entry points in the same motion (§9): hub summary + the agent's session-start record. "
        "4) Create: thin hub -> Document -> atomic "
        "children (Decision/Note/Incident/Experiment/OpenQuestion/Hypothesis/ExternalRequest). 5) "
        "Link per §5 (+ plugin edges). 6) Report to the operator with the tree + cross-locks.")),
    ("s17", "§17 Reading / recall", (
        "Memory is useless unless consulted at the right moments: READING PRECEDES ACTING — recall "
        "before you derive, decide, or claim. Recall is NOT once-per-session: RE-RUN it on every "
        "TOPIC SHIFT — when the conversation crosses into an adjacent subsystem (especially "
        "cross-cutting shared layers: scheduling/watermark/demand/registry), search the graph anew "
        "under the NEW topic BEFORE proposing 'let's build X' — it usually already exists. Recall "
        "when: starting on a unit (pull its slice — "
        "state, decisions, open questions, incidents, experiments — before acting); before a "
        "decision (existing Decision / rejected alternative / Experiment?); before a proposal "
        "(OpenQuestions + rejected Hypotheses — don't re-propose what was turned down); before "
        "investigating a failure (Incidents + Experiments); before asserting a fact (check its "
        "status; re-verify a load-bearing claim against ground truth); when the user references "
        "prior work; cross-project (search pattern:*). How: SEARCH first for entry points, then "
        "TRAVERSE (contained_in for the subtree, depends_on for lineage, informs/motivates for "
        "rationale, supersedes for currency); read the hub first. Trust on read: a node reflects "
        "what was true WHEN WRITTEN — check status + date, re-verify load-bearing claims, confirm "
        "a named file/function/flag still exists; SYNTHESIZE the conclusion for the user, do not "
        "dump raw nodes.")),
    ("s18", "§18 Onboarding a project", (
        "The backend is SHARED across agents (one graph), so a project is bootstrapped ONCE; every "
        "agent then reads/writes the same store under this ruleset (the shared contract that keeps "
        "writes consistent). Write each step at the level of a CAPABILITY so any agent maps it to "
        "its own tools. Procedure: 1) Discovery — the project root + its ground artifact; confirm "
        "the shared backend is reachable; detect capabilities (typed graph vs file-only). 2) Roots "
        "— a Project, a Person (operator), a project Tag. 3) Attach plugins that apply. 4) Seed "
        "structure — for each substantial unit: thin hub -> Document -> atomic children mined from "
        "docs + code + history. 5) Wire — contained_in tree, depends_on cascade, produces/consumes "
        "lineage. 6) Backfill the 'why' from history, each verified against HEAD. 7) Operational "
        "state + record the behavioral subset of this ruleset (a stateless client can't -- see "
        "Plugin stateless-client). Idempotent: search before creating; a second agent that finds "
        "the project onboarded just uses it.")),
    ("s19", "§19 Project zones & cross-project writes", (
        "Each project owns its ZONE (its Project subtree). Never write into ANOTHER project's zone "
        "without explicit operator permission — work in the project of the current task. A foreign "
        "project hand-writing nodes into another's subtree is the anti-pattern (a service writing "
        "to another service's tables instead of calling its API). The ONE sanctioned cross-project "
        "write is FEEDBACK about the conventions / memory-behavior: use the `feedback` capability "
        "(a mis-application, ambiguity, gap, or suggestion), which lands a provisional Feedback "
        "report in the conventions owner's zone for the §14 triage — never hand-write an Incident "
        "into the memory project yourself. Same spirit for other projects: contribute through a "
        "published capability, not by reaching into the subtree.")),
    ("p0", "Plugins — structural vs behavioral", (
        "A plugin bundles {extra node types, extra edge types, extra rules} for one capability; "
        "the Core is always on and a project attaches only the plugins it has. A plugin is "
        "STRUCTURAL (adds node/edge types + rules) or BEHAVIORAL (modifies write/update/visibility "
        "rules — privacy, audit, multi-operator, sync — possibly with no new types). The Core is "
        "INVIOLABLE: a behavioral plugin may not override a Core rule. Plugins are additive and "
        "must not silently alter each other; a genuine collision is resolved by the §11 "
        "first-encounter rule.")),
    ("p1", "Plugin: database", (
        "For any project with a data store (almost all). Adds a Table node — a physical/logical "
        "table/view, self-contained to browse: fqn, kind (main|audit|view|pointer|catalog), "
        "purpose, grain, key_columns, notable_columns, partitioning, notes — and the edges "
        "produces (owner Component -> Table) + consumes (reader -> Table) = table-grain lineage. "
        "Tables are NOT in the containment tree: the owner is produces; they float, reached by "
        "lineage + tag.")),
    ("p2", "Plugin: components", (
        "For any project with large subsystems. Adds a Component node — a thin hub for a "
        "subsystem/layer/module (role, code_location, current_version status, key facts + "
        "limitations); children contained_in it. Cross-component dependency via depends_on; one "
        "semantic map per Component (a §7 specialization). current_version holds the durable "
        "architectural status (§9).")),
    ("p3", "Plugin: versioned-pipeline", (
        "For projects with a versioned execution/orchestration substrate. Concepts: "
        "algorithm-version vs pipeline-version (code vs bindings), watermark cursor, registration, "
        "rollback, orchestrator run states, producer/consumer pv dependency. current_version "
        "records the current av/pv + lineage; registration/rework is the milestone that updates "
        "the hub (§9).")),
    ("p4", "Plugin: stateless-client", (
        "BEHAVIORAL. For any agent/client lacking a persistent local file store and/or a "
        "session-start hook -- e.g. a bare MCP/OAuth connector (ChatGPT-style) rather than an "
        "agent with its own CLAUDE.md/MEMORY.md + hooks (Claude Code-style). Overrides S1/S18: "
        "such a client has no 'files' store to hold the behavioral subset (S1) or the working "
        "agreement (S18 step 7); whatever the host platform's own memory/personalization feature "
        "holds is NOT this ruleset's file store -- treat it as ordinary user data, never as "
        "compliance with S1/S18, unless freshly re-verified against the graph. "
        "'MEMORY-CONNECTED CLIENT' != 'MEMORY-BACKED AGENT' (v2.2): a client counts as a "
        "memory-backed agent only if its runtime provides all three -- (a) durable storage for the "
        "operating contract, (b) GUARANTEED reachability of the memory tools in every execution "
        "context where a MEMORY-DEPENDENT OBLIGATION runs or is delegated (a recall, a write, "
        "acting on memory-derived state; a context with no such obligation is out of scope of this "
        "test), and (c) an enforceable recall-before-acting hook in each such context. Anything "
        "less -- including a bare chat client with opportunistic MCP access -- is a "
        "memory-connected client: for it the external memory is advisory, not a dependable "
        "cognitive substrate, and nothing that MUST happen (a guaranteed recall, a memory-"
        "dependent scheduled job, a required write) may be entrusted to it alone. "
        "ENFORCEMENT LIVES "
        "AT THE LAYER WHERE THE CLIENT DECIDES TO REACH FOR A TOOL -- which may be a GLOBAL layer, "
        "not the project/context one: empirically (ChatGPT, 2026-07-04) a detailed context/Project-"
        "level prompt did NOT make the client attempt tool discovery at all; that decision was "
        "governed by the platform's global custom-instructions layer. So install the discovery-"
        "first / tool-first rule GLOBALLY as a short general rule ('if a request may involve a "
        "tool/connector/MCP, try discovery before answering; never claim an integration is "
        "unavailable until discovery or a real call has failed this turn'), and the detailed "
        "memory "
        "contract in the project/context layer -- ship BOTH (global rule makes the client try the "
        "tool, context prompt makes it work with the graph correctly; neither alone sufficed). "
        "S17 enforcement "
        "shifts onto the platform's customization layer, not the server: no hook can inject a "
        "reminder before a stateless client acts, so the operator must add a standing instruction "
        "at the platform's own customization surface that (a) calls conventions at the start of "
        "every conversation AND on every memory-touching turn, binding for that turn -- nothing "
        "to cache, and a version loaded a few messages ago must not be relied on; (b) calls "
        "search/traverse before answering any question that could be project/domain-specific, not "
        "only when explicitly told -- example triggers (terminology, decisions, incidents, "
        "status, 'how did we do this before') are illustrative, never exhaustive; (c) never "
        "concludes a tool is absent from a partial/self-reported tool list, UNCONDITIONALLY -- "
        "regardless of whether the operator asserted the tool exists -- and matches a tool by "
        "meaning/purpose, not exact bare name, since the same capability may appear under a "
        "different namespace per client (observed: feedback surfaced as "
        "assistant_memory.feedback for ChatGPT vs mcp__assistant-memory__feedback for Claude). "
        "The same guard covers claims that the CONNECTOR / MCP TRANSPORT ITSELF is unavailable or "
        "'disconnected' -- never assert that from cached/stale connection state; re-run discovery "
        "(reopen the connector / list_resources) and attempt a real call in the same turn before "
        "refusing, and if it genuinely fails name the concrete error, not a blanket 'unavailable' "
        "(a distinct failure mode from tool-in-list absence: a stateless client refused a write "
        "claiming the whole MCP was unavailable from stale cached state, re-discovering only after "
        "an operator challenge). DISCOVERY-FIRST / NO REASONING BEFORE DISCOVERY (lazy-loading "
        "clients): where capabilities appear only after a discovery call, absence of a capability "
        "from the current turn's visible list means NOTHING -- (re)run discovery before any memory "
        "access, every such turn and again whenever a needed capability isn't loaded, never "
        "carrying availability over from a prior turn; do NO reasoning about MCP/tool/graph state "
        "or write-ability before that discovery (no explaining, hypothesizing, or proposing "
        "workarounds first). Forbidden until a fresh discovery: 'the tool was here and vanished', "
        "'the connector was lost', 'MCP is off', 'no memory this turn', 'I don't see it so it's "
        "absent'. An operator asserting the connector should work is a strong signal to "
        "re-discover, not to argue. "
        "SCHEDULED / DELEGATED EXECUTION RUNS IN A DIFFERENT RUNTIME -- never assume it inherits "
        "the connectors (v2.2). Empirically (ChatGPT Scheduled Tasks, controlled A/B 2026-07-04): "
        "tasks created from a Project chat and from a normal chat both execute in a separate "
        "runtime with NO access to the user's MCP/connectors at all; project instructions, the "
        "global tool-first rule and explicit discovery-first task prompts cannot compensate -- the "
        "connector surface simply is not there (below prompt/tool-choice enforcement). Rule: a "
        "memory-dependent scheduled or delegated workflow needs a runtime that ACTUALLY HAS "
        "connector access (an external scheduler/agent runtime, e.g. the backend's own actionable "
        "layer), or an explicit capability check that proves connector access FROM WITHIN THAT "
        "EXECUTION RUNTIME ITSELF (e.g. a dry-run task that must reach the memory before the real "
        "one is scheduled) -- connector discovery in the ORIGINATING session proves NOTHING about "
        "the runtime that will execute the task. A task that silently runs memory-blind is worse "
        "than one that refuses to be scheduled. "
        "SPEAK THE PLATFORM'S PRODUCT LANGUAGE in operator-facing instructions (v2.2): ChatGPT's "
        "user surface calls an MCP-backed integration an 'app' (Apps), not 'MCP' -- onboarding "
        "docs, custom-instructions text and troubleshooting steps aimed at that platform's users "
        "must say 'connect/use the app' and must not assume the surface exposes the term MCP; keep "
        "'MCP' as the protocol/transport term in agent-facing prose. "
        "WRITE-GATING (on initiative, not capability): a stateless client IS a full read/write "
        "client and SHOULD write when asked -- maintaining the operator's content (personal-"
        "domain especially: shopping lists, task lists, notes) is a first-class use case, not "
        "something to be reluctant about. The GRAPH IS THE SOURCE OF TRUTH for anything the "
        "operator asks to remember: do NOT use the host platform's built-in memory (e.g. ChatGPT "
        "bio) as a substitute -- write to the graph first, and to the platform store only if the "
        "operator explicitly asks for THAT store or the graph is genuinely unavailable after all "
        "rules above. Before a create (or an update whose target id is unknown), SEARCH FIRST to "
        "update an existing node instead of duplicating (Core S13); feedback/get/update-by-known-"
        "id need no search. What is gated is initiative: it does NOT "
        "create/update/delete/link/unlink/retype graph content on its OWN initiative -- only on "
        "the operator's direct, in-turn instruction to write. Recall stays proactive with no "
        "exception. The one proactive-write exemption: the feedback capability (S19) may be "
        "called on the client's own initiative -- it is the sanctioned low-stakes reporting "
        "channel and gating it would defeat its purpose. This is the intended design for a write-"
        "enabled stateless client (server-side write-denial is deliberately NOT used -- it would "
        "break the personal-domain use case); plugin-scoped, stricter than Core S13 only on the "
        "WHETHER-to-write-autonomously axis (S13 governs HOW). Promote to Core via S11/S14 only "
        "if it proves to generalize beyond stateless clients. DEGRADATION TRANSPARENCY: when a "
        "layer OUTSIDE this backend (typically the host platform's own safety/content filter) "
        "blocks or rejects a memory call, the client must NOT silently degrade graph semantics to "
        "route around it -- dropping edge properties, swapping in vaguer labels, or omitting "
        "relationships just to get a call through lands quietly-wrong memory, worse than a visible "
        "failure. Instead preserve intent: tell the operator the exact class of blocked operation, "
        "keep the precise metadata (in a follow-up note or via feedback), and treat a block on "
        "benign personal-domain data (kinship labels, a family member's birth/residence) as a "
        "false positive to surface, not to quietly comply with -- and do NOT retry a platform-"
        "blocked call with altered/weakened data without explicit operator permission. TRANSIENT "
        "vs DETERMINISTIC blocks are distinguished by retrying the EXACT query (v2.2): if the "
        "identical call succeeds on a later attempt under MATERIALLY EQUIVALENT tool/auth/"
        "connector/runtime conditions, the block was context-sensitive/transient (a false positive "
        "to note), not a deterministic payload block; if those conditions changed between "
        "attempts, "
        "record the outcome as inconclusive/environment-sensitive, not transient. Never diagnose "
        "by "
        "retrying a WEAKENED variant -- that corrupts the diagnosis AND violates the no-silent-"
        "weakening rule. (Observed 2026-07-04: a benign recall query blocked once, then succeeded "
        "verbatim on multiple retries in the same session.) Motivating "
        "instance: an incident "
        "where ChatGPT needed three escalating operator nudges to recall from the graph and to "
        "discover an already-registered feedback tool -- verified as a client-side gap, not a "
        "server registration bug. Second feedback instance: a stateless client hit platform-safety "
        "blocks on ordinary family writes and worked around them by dropping edge properties, "
        "landing incomplete relationship metadata.")),
    ("p5", "Plugin: review", (
        "BEHAVIORAL. For any project doing LLM-assisted iterative review of an artifact (spec, "
        "code, prompt) with an independent critic. Adds no node types -- transient review state "
        "lives in the project's own store, never the graph; only the durable outcome folds in via "
        "Core types. REBUILT (v2.7, 2026-09-02) after the B.15 post-mortem (a 17-round run, 54% "
        "descendant findings): the loop now runs on instruction PAIR v1 -- source of record "
        "docs/review/critic.md + docs/review/development.md (RU, assistant_memory repo); this "
        "section is the canon summary, the pair files are the letter. The old service-side flow "
        "(create_review transitions, waiver machinery, epistemic tiers, one-shot challenge, cold- "
        "verdict-first) is LEGACY: read-only for old runs, drives nothing new, removed after the "
        "first live harness run (operator's word). Proven before adoption: the pair reviewed "
        "itself (3 rounds), its harness spec (3), the harness code (22), each finalized by the "
        "operator. ROLES are abstract: development (produces the artifact, disposes findings) and "
        "critic (reviews); the model<->role binding is machine-profile config. INDEPENDENCE BY "
        "MODEL (invariant): the critic runs on a DIFFERENT model from development; self-review is "
        "a degraded fallback, labelled as such, followed by independent review or an owner waiver "
        "before finalization. FULL SEMANTIC COVERAGE, EVERY PASS: the critic checks the WHOLE "
        "artifact against the WHOLE description each round -- every thesis, in both directions "
        "(artifact violates description; description no longer matches artifact) -- never diff- "
        "only; a pass that skimmed is not a pass. THE WHAT/HOW BOUNDARY, TESTED BY THE WRONG- "
        "RESULT SCENARIO: the description owns WHAT, development owns HOW, but a HOW can be an "
        "unstated part of a WHAT; the test is whether the operator, seeing the result, would call "
        "it wrong -- then it was WHAT. Boundary disputes are legal and go to the OPERATOR as a "
        "fork; the critic honors settled boundaries. TREND LINE, EVERY PASS (invariant): findings "
        "split into NEW TERRITORY vs DESCENDANTS of prior fixes, reported honestly; the trend is "
        "the operator's primary convergence instrument, and continuing vs stopping is the "
        "operator's call. THREE STOP PATHS; THE OPERATOR ALONE FINALIZES: (1) a clean pass over "
        "the current version; (2) the operator's judgment on a visible plateau; (3) the "
        "operator's word, unconditional. The critic RECOMMENDS; development NEVER declares "
        "convergence; only the operator declares finalization. FINDING LEVELS: a LEVEL-1 finding "
        "(touches an operator decision, privacy, or the threat model) is NEVER closed inside the "
        "pair -- it escalates to the operator and blocks finalization until answered; everything "
        "else is the WORKING LAYER, disposed terminally by development with a written per-finding "
        "outcome -- no silent drop. THREE TRUST LEVELS OF DECISIONS: operator-understood (the "
        "operator grasped and ruled -- the critic honors it) | delegated (the call is "
        "development's by the operator's word -- the critic may probe the delegation's edges) | "
        "working (development's own -- fully challengeable). OPERATOR DEPTH RULINGS CLOSE WHOLE "
        "FINDING CLASSES, and the critic judges against them instead of re-raising the class. "
        "DEVELOPMENT DUTIES (each learned by paying): SELF-REVIEW before the critic (run the "
        "critic's checklist on your own change first); SWEEP THE CLASS ALONG ALL ITS AXES "
        "(closing the visible half and declaring the whole class closed is a recorded failure "
        "mode -- name the axes, mutation-test the closure); ONE INVESTIGATED FIX (a single fix "
        "verified end-to-end, never an '(A)/(B) your choice' menu); READY-MADE FIRST (check the "
        "project and common practice before inventing; use-vs-invent is the operator's call when "
        "it matters). DESCRIPTION DELTAS CARRY AUTHORSHIP MARKS: the task description accretes a "
        "per-round delta section naming what changed and WHO authored it; a SEMANTIC change to "
        "the description requires explicit operator fixation -- batched as a numbered list closed "
        "by ONE operator word; silence fixes nothing. THE HARNESS IS THE STANDARD VEHICLE "
        "(review_harness package; mechanics, never verdicts): a durable run catalog restored FROM "
        "FILES ONLY (git-ignored under the reader criterion -- hygiene, not defense); operator "
        "gates journaled VERBATIM (question and answer); the critic launched as codex exec pinned "
        "READ- ONLY -- a structural whitelist on the command template plus attestation from the "
        "tool's own header, silence != read-only; both roles watched by a DETECTION WINDOW "
        "grounded in observed gaps of the machine (machine profile) with the operator notified "
        "within his profiled notify-within; completed rounds are incombustible, the current round "
        "is replayable after a crash; falling back to operator hand-relay is ANNOUNCED, never "
        "silent. THE CRITIC SEES AND READS THE WHOLE REPOSITORY; its FOCUS is the artifact under "
        "review: access and reading are unbounded; critique concentrates on what is under review, "
        "the rest is ground, and the critic MUST read the places the artifact touches (reach = "
        "focus of attention, not a read- fence). The critic is SIGHTED with graph access in this "
        "genre; as critic it writes NO graph nodes -- graph sync is development's job. PROVENANCE "
        "= ORIGIN, NOT AUTHORITY: the base is the source of truth about what the system DOES, not "
        "what it SHOULD do -- doubt it as obligation, lean on it as fact; a finding must LAND ON "
        "THE ARTIFACT under review, and a defect found in the base is an ESCALATION to the "
        "operator (graph-deduped first), not a finding of this review. DURABLE OUTCOME ON "
        "COMPLETION: at run completion development writes the run summary into the graph -- a "
        "Decision on the cycle anchor (operator decisions incl. depth rulings, finalization, "
        "trend, commit) -- and the operator-exchange pairs flow to the graph through the profile "
        "buffer (raw material of the operator profile); transient round state never enters the "
        "graph. "
        "THE DEVELOPMENT CYCLE HAS AN ANCHOR IN THE GRAPH, AND BOTH OF ITS REVIEWS BELONG TO IT "
        "(v2.6): a cycle is one slice of work -- from the operator's first free-form sentence to "
        "acceptance -- recorded as a `DevelopmentCycle` node; everything the slice produced hangs "
        "off it: BOTH reviews (each review's Decision linked to the anchor at finalization), all "
        "four Intent Summaries, the spec, and the LITERAL transcript of the two free-form phases. "
        "Commits are a property of the anchor -- an ordered list of full 40-character shas and "
        "nothing else (a commit already has an unforgeable timestamp and an authoritative store; "
        "a node per commit is a second implementation of history on top of git). THE OPERATOR "
        "DECLARES THE CYCLE; development NEVER infers one. The declaration may be late; what is "
        "recorded as the START is the timestamp and identifier of the cycle's FIRST message, "
        "written onto the anchor at declaration -- not the moment anyone remembered the process. "
        "Past cycles are NEVER backfilled. EACH CYCLE DOCUMENT IS WRITTEN AT ITS OWN "
        "FINALIZATION, IN FULL TEXT, WITH ITS TYPE AND -- PER KIND -- ITS REVIEW OR ITS "
        "SUBJECT: not in a batch at the end, "
        "because the moment of writing IS data -- 'which documents did this decision stand on' is "
        "answered by comparing timestamps (one version per document, the latest created before "
        "the decision), and four documents written a minute apart at the end make that comparison "
        "meaningless. Violating it does not break the mechanism visibly; it makes it LIE, and "
        "nothing can check it (no server observes when a document was really written), so it "
        "holds because it is followed. Each document carries a TYPE LABEL from a closed "
        "vocabulary -- intent_pre_review | intent_post_review | spec | free_phase_transcript -- "
        "and, for the POST-review intent alone, the ID OF THE REVIEW it reports; for the spec, "
        "the transcript AND the pre-review intent that affinity is written out as explicitly "
        "empty, because they belong to the CYCLE rather than to either review (operator "
        "amendment 2026-08-27: the earlier rule demanded the pre-review intent be written "
        "before its review existed while carrying that review's id -- impossible). The "
        "pre-review intent instead names WHICH ARTIFACT IT TRANSLATES (spec | implementation), "
        "since a cycle has several of them and a label is not an identity. Without the label a "
        "reading pass "
        "cannot tell a recorded conversation from a promise, and the two do not have the same "
        "standing. THE FREE-FORM PHASES ARE STORED AS A LITERAL TRANSCRIPT: the prose turns of "
        "both sides in order, extracted mechanically from the session record -- not a summary, "
        "not a template, no sections. A summary written by the party whose renderings the "
        "comparison exists to check inherits that party's blind spots; and any structure imposed "
        "on the record formalizes through the back door exactly the two phases that are "
        "unformalized by design. The agent's own reasoning between turns is NOT included: what "
        "was SAID is recorded, not what was thought. A COMPARISON READS ITS DOCUMENT SET FROM THE "
        "ANCHOR, AND DERIVING IT FROM FILENAMES IS FORBIDDEN -- nor from a declaration on the "
        "channel, which is unverifiable by construction when most of the documents are files and "
        "the server has no repository; filename derivation is not rigour but a guaranteed error "
        "(intent filenames are not paired and several have no twin). EVERY ITEM THE LOOP PUTS TO "
        "A HUMAN CARRIES A HUMAN PROJECTION, AND THE PROJECTION IS A RECORD: four parts, in the "
        "operator's language -- what happened in ordinary words; what it concerns (how much, of "
        "what, where); what is proposed and why; what each available answer will actually do, "
        "said as outcomes rather than as the names of fields. One record carries the machine item "
        "VERBATIM BESIDE its translation, under the key of the item it explains, and the human is "
        "called from that record. TIDIED MACHINE MATERIAL IS NOT A PROJECTION: printing a table "
        "more legibly, grouping its rows or typing its reasons still leaves the human reading a "
        "machine table. The default is that the operator does not read what is written for "
        "machines (Plugin operator-profile), so a machine artifact either has a projection or "
        "never becomes the basis of a question. TWO RULES FOR READING AND WRITING SPECS, learned "
        "by paying for them: (1) A CONSTRAINT WRITTEN IN THE ELEMENT THAT FORBIDS, BUT ABSENT "
        "FROM THE ELEMENT THAT ACTS, IS NOT A CONSTRAINT -- for every acting rule and every rule "
        "that constrains it, ask whether the acting one carries the constraint IN ITS OWN "
        "CONDITION or merely sits beside it in the same document; ADJACENCY IS NOT CONNECTION "
        "(found twice in one review by the critic, never once by development). (2) 'THIS SURFACE "
        "REUSES THE EXISTING MECHANISM' WITH NO WRITTEN CONTRACT OF THE OPERATION IS A SECOND "
        "IMPLEMENTATION IN AMBUSH -- write the bridge as the contract of ONE shared operation "
        "with explicit captures and idempotency, and list every consumer in one edit (roughly 7 "
        "of 30 findings of one spec review were this class). "
        )),
    ("p6", "Plugin: skills", (
        "STRUCTURAL + BEHAVIORAL. For any project that publishes reusable agent skills through "
        "the shared graph, so any skill-capable agent on any project self-installs them without "
        "knowing the source repo. Adds a Skill node -- a self-contained installable agent skill: "
        "name, description (when-to-use / triggers), body (the full skill/prompt content), "
        "target_role (development|critic|any), target_harness (claude-skill|codex-prompt|...), "
        "version, status. Self-contained (Core S2): a consumer installs with no repo access; "
        "Skill nodes float (query/tag), not in the containment tree. SKILL-SYNC (behavioral): a "
        "skill-capable agent, as part of reading the conventions at session start, fetches the "
        "INSTALLABLE Skill nodes for it, compares each version with its locally-installed copy, "
        "and installs/updates any missing/stale one into its own skills location (e.g. "
        "~/.claude/skills/<name>/) using its own file tools. INSTALLABLE = status current "
        "(published) + authored by a TRUSTED credential + matching target_role/target_harness; a "
        "draft/provisional/superseded/rejected or untrusted-authored node is DATA, not an "
        "installable. Package key = name + target_harness (+ namespace/publisher); two current "
        "nodes sharing a key -> PARK/ESCALATE, never a silent pick. NO installer software exists "
        "-- the agent IS the installer; next-session activation latency is acceptable. "
        "SKILL-SYNC STARTS WITH AN INVENTORY (v2.1): before installing anything, the agent lists "
        "what is already installed on EVERY skill surface it can reach -- its native store AND "
        "any account-/platform-level store its tools can manage (e.g. a web skills panel via "
        "browser automation); a surface it cannot write is REPORTED to the operator with the "
        "change it needs -- never silently skipped. Each installable Skill node is checked not "
        "only for version staleness but for COLLISION: an already-installed skill -- any name, "
        "any surface, any author -- whose triggers/purpose overlap enough that both could fire "
        "on the same request. INSTALL TARGET vs INVENTORY SCOPE: the agent installs/updates ONLY "
        "into its native store; non-native surfaces are inventoried for collision detection and "
        "are changed only through the collision gate or an explicit operator instruction -- "
        "never auto-synced; parks and reports are per surface. COLLISION -> OPERATOR GATE; "
        "ARCHIVE-THEN-REPLACE: a collision is never resolved silently -- raise it to the "
        "operator ('the conventions require skill X vN; it collides with installed skill Y on "
        "surface S -- replace?'); silence = not granted (the install for that surface parks, and "
        "the park is reported). On approval the displaced skill's FULL BODY is archived to the "
        "graph first: a Skill node, status superseded, a displaces edge from the incoming skill "
        "to the archived one (a plugin edge type: 'took this skill's place on an install "
        "surface' -- no package lineage implied; supersedes stays reserved for a new version of "
        "the same package key; the superseded-status pairing with a displacement edge is named "
        "in Core S6), provenance naming the surface, the date, and the approval -- "
        "self-contained enough to reinstall from the node alone (Core S2; the rollback path). "
        "Only then is it removed or replaced. THE ARCHIVE RECORD IS ITS OWN NODE -- never a "
        "relabelled distribution copy: a published current distribution Skill node's publication "
        "state is untouched (it changes only through the publication gate); the archive record "
        "references it for lineage; dedup (Core S13) applies only to an existing archive record "
        "of the same body + surface event. SAME PACKAGE KEY, NEW VERSION -> NOTIFY, NOT GATE: "
        "when an installed skill and an installable Skill node share the full package key (name "
        "+ target_harness + namespace/publisher where present) and only version differs, the "
        "publication gate has already vetted the body: install/update and tell the operator in "
        "one chat line ('skill X updated vA->vB') -- no second gate, and no silent update; a "
        "differing namespace/publisher/author is NEVER a plain version bump (collision / "
        "package-key gates apply). HARNESS-UNIVERSAL BY CONSTRUCTION: the steps above are "
        "written at capability level (Core S18) and bind ANY skill-capable agent -- Claude Code "
        "(file skills), Codex (prompt/config surface), Cursor (rules surface), future harnesses; "
        "target_harness names the surface; a new harness value is DATA, not a spec change; each "
        "agent maps the steps to its own tools and what it cannot do itself it hands to the "
        "operator explicitly. TRUST -- "
        "THE GATE IS PUBLICATION: a skill body is a BEHAVIORAL CONTROL PLANE, not ordinary memory "
        "-- once installed it governs the agent; a new/changed body enters as a draft "
        "(provisional, not installable) and the operator PUBLISHES by promoting it to current (a "
        "Core S6 status transition needing the operator's confirming event); auto-install stays "
        "frictionless for published skills while every new/changed body passes a human gate. "
        "PUBLISHED VERSIONS ARE IMMUTABLE (invariant): install-relevant fields of a current "
        "version (body, description, targeting) are FROZEN -- any change is a NEW version "
        "re-entering as a draft through the publication gate, never an in-place edit; one version "
        "= one body; an agent finding a current body differing from its installed copy at the "
        "SAME version treats it as invalid and parks/escalates rather than silently diverging. "
        "SOURCE OF RECORD vs DISTRIBUTION: the editable source -- and its review cycle -- lives "
        "in the authoring project's repo; the graph Skill node is the PUBLISHED distribution "
        "copy, re-seeded on a version bump (the conventions' own refresh_note pattern); consumers "
        "read ONLY the graph and never touch a source repo.")),
    ("p7", "Plugin: operator-profile", (
        "STRUCTURAL + BEHAVIORAL. For any project whose operator wants their INTERACTION "
        "PREFERENCES (language, explanation style, format habits) known to EVERY agent on every "
        "machine, not just the one that learned them. Preferences are a BEHAVIORAL CONTROL PLANE "
        "(like skills): once in force they govern how agents address the operator, so entry into "
        "force is GATED. COLLECTION (behavioral): any agent, any project, watches for PREFERENCE "
        "SIGNALS -- direct instructions about interaction style ('answer in Russian', 'no "
        "section-number references', 'no buttons'), repeated clarification over the same kind of "
        "output, the operator's own language choice -- and records each as a PORTABLE RULE with "
        "its why (not a chat quote). EPISTEMIC GATE: a preference the operator STATED DIRECTLY "
        "enters status current immediately (the statement is the confirming event); an INFERRED "
        "one enters provisional and is proposed in one chat line, becoming binding only through "
        "the explicit confirmation path (confirm_preference, a Core S6 transition -- same "
        "principle as the skills publication gate). One preference = one node (Core S3): a Note "
        "auto-tagged 'preference' (the existing remember_preference substrate) carrying scope "
        "(global|project), a domain key, the rule text, and a PORTABLE why (safe outside the "
        "graph: interaction style, no personal facts -- it is compiled into local files). "
        "IDENTITY & CONFLICTS (structural): a profile preference carries a DOMAIN -- a short "
        "kebab-case key naming the aspect of interaction it governs. Domain keys are a CONTROLLED "
        "VOCABULARY exactly like the pattern:* tag layer (Core S15): the plugin ships an initial "
        "registry (language, answer-format, doc-references, ui-buttons, verbosity, tone, "
        "explanation-level) + an alias map, both living in the graph as a registry the S14 pass "
        "maintains. The server NORMALIZES on write: a known alias resolves to its canonical key; "
        "an unknown key is a controlled error listing the registry, UNLESS the caller passes "
        "new_domain:true -- then the key is created PENDING: the preference is stored, but a "
        "pending domain NEVER COMPILES and never participates in conflict resolution until "
        "ACCEPTED through the registry gate (resolve_domain -- trusted-credential, "
        "operator-relayed, audited; the agent proposes the new domain in one chat line). So a "
        "freeform key can never affect anyone's compiled behavior before a human accepted it -- "
        "deliberate growth, no silent fragmentation into answer-format/response-format/format "
        "synonyms. Domain keys are NON-SENSITIVE BY CONSTRUCTION (they name an aspect of "
        "interaction, never its content). THE DOMAIN IS THE CONFLICT KEY: different domains are "
        "additive; same domain = the same rule at different strengths of authority. Within one "
        "(account, scope, domain[, project]) key the identity is SPLIT BY STANDING: at most ONE "
        "'member' (a binding rule) AND at most ONE 'candidate' (an unconfirmed inference) coexist. "
        "A STATED write creates/updates the member (the statement is the confirming event) and in "
        "the same transaction SUPERSEDES any pending candidate for the key (a direct statement "
        "outranks a pending inference). An INFERRED write (inferred:true) NEVER touches the "
        "member: it creates/updates the key's single candidate, recording as its BASE the resolved "
        "effective rule it was inferred against (project member, else global member, else null -- "
        "scope, node id, version), and stays out of every compiled profile. CONFIRMATION IS WHAT "
        "REPLACES: promoting the candidate flips it to member and supersedes the previous member, "
        "BUT ONLY IF that member still matches the candidate's recorded base; a base mismatch "
        "(the rule changed after the inference) REFUSES the promotion pending explicit "
        "re-confirmation. TWO STORAGE AXES -- the plugin never touches the Core status vocabulary: "
        "Core 'status' stays the EPISTEMIC axis (current/provisional/superseded/rejected, Core S6 "
        "transitions as usual), while the profile marker carries a separate 'standing' field -- "
        "member (binding) | candidate (unconfirmed inference) | parked (an alias-collision loser "
        "awaiting disposition). COMPILATION AND RESOLUTION READ STANDING ONLY: members compile, "
        "resolution considers members, candidates and parked nodes are invisible to both. The "
        "capabilities update the two axes in defined pairs: stated write -> member + current; "
        "inferred write -> candidate + provisional; promotion -> candidate becomes member/current "
        "and the displaced member becomes superseded (standing removed); retirement -> superseded, "
        "standing removed; parking (alias) -> parked, Core status UNCHANGED (epistemic history "
        "preserved); detach -> marker + standing removed, Core status untouched. CROSS-SCOPE "
        "RESOLUTION is deterministic and happens BEFORE delivery filtering: per domain, the "
        "project-scoped member wins if present, else the global one; if the WINNING node is "
        "above-normal sensitivity it MASKS the domain (withheld entirely rather than falling back "
        "to a lower-precedence deliverable rule -- precedence is never silently inverted by "
        "filtering). The server returns only the RESOLVED EFFECTIVE SET -- a client never merges. "
        "STORAGE (structural), two scopes: PROJECT preferences live in the project's own zone "
        "(ordinary write); GLOBAL preferences attach to the operator's Person node in their "
        "personal space -- a cross-project write sanctioned like feedback (THE SECOND EXCEPTION to "
        "the zone rule S19): remember_preference routes scope:global writes to the calling "
        "credential's operator zone regardless of the calling project. No Person node yet -> the "
        "write CREATES it in the account's personal space; no personal space -> the write FAILS "
        "with an explicit error naming the missing prerequisite (never a silent fallback). "
        "DELIVERY (behavioral) -- THE GUARDED BOOTSTRAP: repo-based harnesses (Claude Code, Codex, "
        "Cursor, future) read agent-instruction files from the repo; those files are a contract "
        "with EVERY contributor, so the committed bootstrap block is IMPERSONAL and degrades "
        "silently. At session start the agent makes ONE honest availability attempt (a real "
        "discovery/capability call, not a cached belief); failure is handled BY CLASS -- an "
        "ABSENCE failure (no connector, graph unreachable, graph up but profile capability "
        "missing) -> SILENTLY skip the block (no error, no mention, no retry this session); an "
        "AUTHENTICATION failure (401/403 from a CONFIGURED connector) is NOT silent-skipped -- "
        "only a user who configured credentials can receive it, so the agent tells ITS user once, "
        "briefly, and proceeds without hydration. (This NARROWS the stateless-client "
        "discovery-first rule: one real attempt before concluding absence; concluding absence with "
        "NO attempt stays forbidden.) STALE-CACHE POLICY: if hydration fails and a prior local "
        "profile file exists, the agent MAY follow it as an explicitly stale cache (profile "
        "content is interaction style, not authorization; worst case is outdated style, and "
        "deleting the cache would punish transient outages) -- refreshed on the next successful "
        "hydration, no re-fetch within the failed session; the header carries version + fetched_at "
        "so staleness is visible. On success the agent calls the profile capability; the SERVER "
        "resolves who the operator is from the calling credential's ACCOUNT (the account, not the "
        "credential, is the operator unit -- one person, many clients), compiles the resolved "
        "effective set (per-domain, project overrides global), and returns it with a single "
        "VERSION. BEFORE WRITING the agent verifies the target path is ignored AND untracked "
        "(git check-ignore + not in the index); if not, it does NOT write profile content but "
        "proposes the .gitignore fix through the self-install gate (or silently skips if no "
        "operator is present) -- personal content never reaches a git-tracked path. The local "
        "file is TWO PARTS with separate rewrite rules: a CLIENT-maintained cache-metadata header "
        "(fetched_at, updated on EVERY successful hydration) then the SERVER-produced "
        "profile_markdown verbatim (deterministic: version header + rules, NO timestamps) -- "
        "fetched_at always refreshed on success, the server part rewritten only when the returned "
        "version differs, so compiled content stays byte-identical across harnesses for the same "
        "version. SELF-INSTALL (behavioral) -- the bootstrap installs itself: the committed block "
        "carries a VERSION MARKER (operator-profile bootstrap vN) whose current N the conventions "
        "state. FILE OWNERSHIP mirrors the skill-sync surface rule -- each harness owns ITS OWN "
        "agent-instruction file (Claude Code -> CLAUDE.md, Codex -> AGENTS.md, Cursor -> its rules "
        "file, ...) and checks/maintains the block ONLY in the file its own harness reads; "
        "per-file states resolve independently: own file block ABSENT -> PROPOSE the install (one "
        "commit adding the block + the shared .gitignore line if missing, both impersonal; one "
        "operator 'yes' per repo per file); PRESENT at current version -> nothing to do, hydrate; "
        "PRESENT but STALE (older N) -> propose an operator-gated update commit, never silently "
        "rewrite a committed file; OTHER harnesses' files -> INVENTORY not action (report, never "
        "edit a file another harness owns). Hydration follows the agent's OWN file's block only, "
        "so a mixed-version repo cannot make one agent follow another harness's stale semantics. "
        "Idempotent: one block per harness file serves ALL users of a shared repo. INVARIANTS: "
        "the committed bootstrap text has NO personal data and NO hard dependency on private infra "
        "(silent no-op for any contributor without the graph); operator-profile content is NEVER "
        "written to git-tracked files and the writer VERIFIES ignore/untracked before writing "
        "(canon lives in the graph; the local file is a gitignored compiled copy); only "
        "operator-CONFIRMED preferences are binding (provisional inferred ones are excluded from "
        "the compiled profile); SENSITIVITY GATE -- a preference above normal sensitivity is NEVER "
        "compiled into profile_markdown (non-deliverable through the repo bootstrap by design, "
        "served only through surfaces that keep it off disk: the platform layer under "
        "stateless-client, or direct graph reads), and the omission is NEVER silent (compilation "
        "returns a withheld-count and the profile carries a one-line note 'N preference(s) "
        "withheld: sensitivity'); ROLLOUT ORDERING -- the server capability is LIVE before this "
        "canon section publishes (re-seed), with defense in depth (the bootstrap's silent-skip "
        "already covers 'graph up, capability missing', so a wrong order degrades gracefully). "
        "BOUNDARY WITH PLUGIN stateless-client: operator-profile owns WHAT the preference layer is "
        "(collection, identity/conflict semantics, storage, the compiled profile) and its "
        "REPO-BASED delivery; platform clients without a repo (ChatGPT-style) keep their existing "
        "delivery -- the platform's customization layer under stateless-client -- and may read the "
        "SAME preference nodes through it; no rule is duplicated, each plugin owns its own delivery "
        "surface.")),
]

# Cross-section references (src key -> dst keys), mirroring the spec's own §-links.
REFS: dict[str, list[str]] = {
    "s1": ["s9"],
    "s4": ["s6", "s11"],
    "s6": ["s8", "s11", "s13"],
    "s8": ["s6"],
    "s9": ["s6"],
    "s11": ["s14"],
    "s11a": ["s14"],
    "s12": ["s2", "s6"],
    "s16": ["s7", "s13"],
    "s17": ["s8", "s10"],
    "s19": ["s13", "s14"],
    "p0": ["s11"],
    "p4": ["s1", "s17", "s18"],
    "p5": ["s6", "s9", "s11a"],
    "p6": ["s2", "s6"],
    "p7": ["s6", "s15", "s19", "p4"],
}

TAG_TEXT = "Canonical Assistant-Memory operating conventions."

# The feedback inbox: a thin hub the `feedback` tool attaches reports to, so an agent
# (often from another project) never hand-writes into the conventions owner's zone. The
# names live here because they are reserved and both sides must agree on them; the hub
# itself is created by the feedback tool, in the feedback destination space — the seeder
# stopped building it when the two stopped sharing a space.
FEEDBACK_HUB_LABEL = "Conventions feedback"
FEEDBACK_TAG = "conventions-feedback"
FEEDBACK_HUB_TEXT = (
    "Inbox for feedback about these conventions and the agent's memory-behavior, submitted via "
    "the `feedback` tool (often from another project). Each item is a provisional Feedback node "
    "contained here; the §14 maintenance pass triages it into a convention edit, an Incident, or "
    "rejects it."
)


def _matches(existing_props: dict, wanted: dict) -> bool:
    """Subset match: every wanted key equals what's stored (ignores extra keys)."""
    return all(existing_props.get(k) == v for k, v in wanted.items())


async def _refuse_if_projected_elsewhere(session: AsyncSession, space_id) -> None:
    """Stop, naming the other space, when the projection already exists outside ``space_id``.

    Any status counts, retired included: a retired copy is still a copy, and the point is to
    never end up with two of them. The caller is told WHERE it is, because the only useful
    next action is to look there.
    """
    other = await session.scalar(
        select(NodeSpace.space_id)
        .join(Node, Node.id == NodeSpace.node_id)
        .where(
            Node.type == "Document",
            Node.label == DOC_LABEL,
            Node.deleted_at.is_(None),
            NodeSpace.space_id != space_id,
        )
        .limit(1)
    )
    if other is not None:
        raise ProjectionElsewhere(
            f"the conventions are already projected into space {other} — refusing to create a "
            f"second projection in {space_id}. Move the existing one, or seed into that space."
        )


async def seed_conventions(
    session: AsyncSession,
    *,
    space_id,
    account_id,
    embedder: Embedder | None = None,
) -> dict[str, int]:
    """Idempotently project the conventions Document + children into ``space_id``.

    With ``embedder=None`` each node is indexed full-text only (``index_fts``, no
    model) — enough to be searchable and (with membership) visible; embeddings are
    backfilled later. With an embedder, nodes are fully indexed. Flushes; the caller
    commits. Returns {created, updated, unchanged, skipped} counts.

    Raises ``ProjectionElsewhere`` when the conventions already live in another space.
    """
    await _refuse_if_projected_elsewhere(session, space_id)

    counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}

    async def upsert(*, type_, label, properties, status="current") -> Node:
        # Deterministic resolution (B.13 A-8): the seeder never picks between
        # same-labeled candidates — two matches is a named error listing them, and
        # resolution is the operator's. (`limit(2)` is the cheapest ambiguity probe.)
        matches = (
            (
                await session.execute(
                    select(Node)
                    .join(NodeSpace, NodeSpace.node_id == Node.id)
                    .where(
                        NodeSpace.space_id == space_id,
                        Node.type == type_,
                        Node.label == label,
                        Node.deleted_at.is_(None),
                    )
                    .limit(2)
                )
            )
            .scalars()
            .all()
        )
        if len(matches) > 1:
            raise SeederAmbiguity(
                f"two nodes in space {space_id} carry type={type_!r} label={label!r} "
                f"({', '.join(str(n.id) for n in matches)}) — the seeder refuses to "
                "pick; retire or relabel one of them and re-run"
            )
        existing = matches[0] if matches else None
        if existing is not None and existing.status in RETIRED_STATUSES:
            # Left exactly as the operator left it — no property patch, no status lift,
            # no re-index. Without this the first re-seed silently undoes a maintenance
            # disposition (the update branch below restores `current` unconditionally).
            counts["skipped"] += 1
            return existing
        if existing is None:
            node = await repo.create_node(
                session, type=type_, space_id=space_id, account_id=account_id,
                label=label, properties=properties, status=status,
            )
            counts["created"] += 1
        elif _matches(existing.properties or {}, properties) and existing.status == status:
            counts["unchanged"] += 1
            return existing
        else:
            node = await repo.update_node(
                session, node_id=existing.id, account_id=account_id,
                expected_version=existing.current_version_id,
                properties_patch=properties, status=status,
            )
            counts["updated"] += 1
        if embedder is None:
            await index_fts(session, node)
        else:
            await index_node(session, node, embedder)
        return node

    doc = await upsert(type_="Document", label=DOC_LABEL, properties=DOC_PROPERTIES)
    tag = await upsert(type_="Tag", label=TAG_LABEL, properties={"text": TAG_TEXT})

    by_key: dict[str, Node] = {}
    for key, label, text in SECTIONS:
        node = await upsert(type_="Note", label=label, properties={"text": text})
        by_key[key] = node
        await repo.link(session, type="contained_in", src_node=node.id, dst_node=doc.id,
                        account_id=account_id)
        await repo.link(session, type="tagged_with", src_node=node.id, dst_node=tag.id,
                        account_id=account_id)
    await repo.link(session, type="tagged_with", src_node=doc.id, dst_node=tag.id,
                    account_id=account_id)

    for src_key, dst_keys in REFS.items():
        for dst_key in dst_keys:
            await repo.link(session, type="references",
                            src_node=by_key[src_key].id, dst_node=by_key[dst_key].id,
                            account_id=account_id)

    return counts
