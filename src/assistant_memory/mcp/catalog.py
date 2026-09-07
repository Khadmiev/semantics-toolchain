# SPDX-License-Identifier: Apache-2.0
"""Tool catalog + the per-credential visibility gate.

The catalog is the single source of truth for which tools exist and how they are
grouped (read / write / share). The data plane exposes only these; control-plane
operations are intentionally not here (decisions §8).

A credential's ``allowed_tools`` (B3) is a least-privilege allow-list: ``None``
means "all tools", otherwise only the named tools are visible/callable.
"""

from dataclasses import dataclass

from ..auth.resolver import Principal


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: str  # "read" | "write" | "share"
    description: str


# Order is the catalog's presentation order. share_by_tag is intentionally
# omitted until tagging/snapshot semantics land (#5, with B6).
CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        "conventions", "read",
        "READ ME FIRST. The operating rules for using this shared memory: how to write, "
        "when to recall, how knowledge ages. Returns the canonical conventions Document + "
        "its sections in one call. Follow them and persist the behavioral subset locally.",
    ),
    ToolSpec(
        "get", "read",
        "Fetch one node by id — its type, label, properties, and current version id.",
    ),
    ToolSpec(
        "search", "read",
        "Find entry-point nodes by meaning + full text (hybrid, access-filtered). "
        "Use this first to avoid creating duplicates.",
    ),
    ToolSpec(
        "traverse", "read",
        "Walk edges out from a start node (direction in/out/both, depth) to explore "
        "what a node connects to.",
    ),
    ToolSpec(
        "explain", "read",
        "Provenance of a node: its version history, sources (source_ref), and authors.",
    ),
    ToolSpec(
        "timeline", "read",
        "Chronology of changes (node versions), newest first — optionally for one "
        "entity or since a date. Use to see what changed and when.",
    ),
    ToolSpec(
        "list_spaces", "read",
        "List the spaces you can access (id, name, permission, writable). Call this "
        "to get a target space id before writing.",
    ),
    ToolSpec(
        "list_node_types", "read",
        "List the registered node types (type, default sensitivity, description). "
        "Check here before creating a new type.",
    ),
    ToolSpec(
        "list_edge_types", "read",
        "List the registered edge types (type, category, sensitive flag, description). "
        "Check here before creating a new type.",
    ),
    ToolSpec(
        "create_node_type", "write",
        "Register a new node type (trusted only). Reuse an existing type first; add "
        "one only for a genuinely new kind of thing.",
    ),
    ToolSpec(
        "create_edge_type", "write",
        "Register a new edge type (trusted only). Reuse an existing type first; add "
        "one only for a genuinely new kind of relationship.",
    ),
    ToolSpec(
        "retype_node", "write",
        "Change one node's type to another registered type (needs write on the node).",
    ),
    ToolSpec(
        "retype_edge", "write",
        "Change one active edge's type to another registered type (needs write on its source).",
    ),
    ToolSpec(
        "remember_fact", "write",
        "Remember a plain fact as a Note. High-level shortcut over create_node: "
        "de-dupes first, sets the type and sensitivity for you. Prefer this for "
        "'remember that ...'.",
    ),
    ToolSpec(
        "remember_preference", "write",
        "Remember a user preference (a Note tagged `preference`). De-dupes first. "
        "Use for 'I prefer ...' / 'always/never ...'. Pass `domain` (registry key, e.g. "
        "'language', 'answer-format') to make it an OPERATOR-PROFILE rule delivered to "
        "every agent: scope 'global' (all projects; lands in your operator zone) or "
        "'project' (this zone; overrides global per domain). `inferred=true` records an "
        "UNCONFIRMED candidate (binding only after confirm_preference); `new_domain=true` "
        "proposes a new registry key (pending until the operator accepts). Optional `why` "
        "is compiled into local profile files — keep it portable, no personal facts.",
    ),
    ToolSpec(
        "remember_decision", "write",
        "Remember a decision that was made (a Decision node — higher sensitivity). "
        "De-dupes first. Use for 'we decided ...' / 'the plan is ...'.",
    ),
    ToolSpec(
        "feedback", "write",
        "Report feedback about THESE operating conventions or your memory-behavior — a "
        "mis-application, ambiguity, gap, or suggestion. Use this instead of hand-writing into the "
        "memory project's zone: it lands the report where the conventions owner triages it (§14).",
    ),
    ToolSpec(
        "create_node", "write",
        "Create a typed node in a space. Put real content in `properties`; keep "
        "`label` a short title. Write policy may require confirmation.",
    ),
    ToolSpec(
        "update_node", "write",
        "Append a new version with a shallow `patch` to properties. Pass the node's "
        "current_version_id as `expected_version` (optimistic concurrency).",
    ),
    ToolSpec(
        "delete_node", "write",
        "Soft-delete a node (reversible by the user). Pass `expected_version`.",
    ),
    ToolSpec("link", "write", "Create a typed edge between two visible nodes."),
    ToolSpec("unlink", "write", "Retract an edge by id (soft, reversible)."),
    ToolSpec(
        "share_nodes", "share",
        "Make an explicit list of nodes visible in an existing target space.",
    ),
    ToolSpec(
        "share_subtree", "share",
        "Make a node and its containment subtree visible in an existing target space.",
    ),
    ToolSpec(
        "share_edge", "share",
        "Include a sensitive edge in a target space (both endpoints must already be there).",
    ),
    ToolSpec("unshare", "share", "Remove a node reference from a space (always allowed)."),
    ToolSpec("unshare_edge", "share", "Remove a sensitive edge from a space (always allowed)."),
    ToolSpec(
        "create_scheduled_job", "write",
        "Create a scheduled/proactive job (actionable layer): give an `instruction`, a "
        "`trigger` ({kind: one_shot|interval|cron, spec, predicate?}) and optional `tools` — "
        "ordered read-only gather steps [{tool: get|search|traverse|timeline|explain|"
        "recent_nodes_context, args}] whose results feed the reasoner, and an optional "
        "`writes` fence — the node types the job may create/update, as a list of type "
        "strings or {types: [...]}. Authoring validates "
        "executability against THIS runtime (scheduler enabled, real reasoner, valid steps, "
        "delivery channel up) and rejects a job it could not actually run. Pass dry_run=true "
        "to run GATHER+REASON once, immediately, creating nothing — probe before scheduling. "
        "Write policy may require confirmation.",
    ),
    ToolSpec(
        "list_scheduled_jobs", "read",
        "List your scheduled jobs (id, label, enabled, kind, next_run, instruction).",
    ),
    ToolSpec(
        "set_scheduled_job_enabled", "write",
        "Pause (enabled=false) or resume (true) a scheduled job by node_id.",
    ),
    ToolSpec(
        "create_review", "write",
        "Create a dev↔critic review in the review-orchestration service and mint its two "
        "per-review tokens (returned ONCE — store them in files, never chat). Use when "
        "starting an iterative review as the development agent; replaces the manual "
        "bootstrap-credential step. Trusted credentials only.",
    ),
    ToolSpec(
        "get_operator_profile", "read",
        "The compiled operator profile for YOUR account (identity resolved from the "
        "credential — no whoami needed): the resolved effective preference set (project "
        "overrides global per domain), a deterministic `profile_markdown` to write into "
        "the repo-local gitignored profile file, a content-addressed `version` (rewrite "
        "the file only when it changes), and a `withheld` count of sensitivity-masked "
        "rules. Pass `project` (space id) for a project-specific profile.",
    ),
    ToolSpec(
        "confirm_preference", "write",
        "Promote or dispose an operator-profile preference, relaying the operator's "
        "explicit confirmation. A CANDIDATE (inferred, unconfirmed): call with just "
        "`preference_id` — refuses if the effective rule changed since the inference "
        "(re-ask the operator) — or pass `disposition: \"reject\"` to decline it (the "
        "operator's correction: the entry becomes a terminal rejected tombstone and "
        "the domain+scope slot frees for a corrected re-proposal; repeat rejects "
        "answer 'already retired'). A PARKED node (alias-merge collision): "
        "`disposition` required — promote_current | promote_candidate | retire | "
        "detach. Retire a binding rule via a parked/candidate path or "
        "remember_preference; generic update/delete are refused on profile nodes.",
    ),
    ToolSpec(
        "resolve_domain", "write",
        "The preference-domain registry gate (operator decision relayed): `accept` a "
        "pending domain (its preferences become compile-eligible), `reject` it (stored "
        "but never binding), or `alias` it into an accepted canonical key (preferences "
        "re-key; collisions PARK and are enumerated in the result — report them to the "
        "operator). Registry entries are protected nodes; this is their only mutation "
        "path.",
    ),
)

BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in CATALOG}


def is_allowed(principal: Principal, name: str) -> bool:
    """Whether the credential may see/call this tool."""
    if name not in BY_NAME:
        return False
    return principal.allowed_tools is None or name in principal.allowed_tools


def visible_tools(principal: Principal) -> list[ToolSpec]:
    """The catalog filtered to what this credential is allowed to see."""
    return [spec for spec in CATALOG if is_allowed(principal, spec.name)]
