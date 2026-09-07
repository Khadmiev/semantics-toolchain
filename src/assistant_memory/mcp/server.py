# SPDX-License-Identifier: Apache-2.0
"""Thin streamable-HTTP MCP transport over the tool core.

Stateless: every request carries the opaque bearer credential, which we resolve
per call (matching "every request resolves" — decisions §9). The substance lives
in ``tools``; this module only binds transport -> auth -> dispatch and filters the
advertised tool list by the credential's ``allowed_tools`` (so disallowed tools are
not even visible).

Mounting (see main.py): the session manager's ``run()`` must wrap the app lifespan;
the ASGI handler is mounted under ``/mcp``.
"""

import json
from typing import Any

import mcp.types as mcp_types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request

from sqlalchemy import select

from ..auth.resolver import Principal, resolve_credential
from ..db import SessionLocal
from ..install import gate as install_gate
from ..install.state import write_completion_record
from . import catalog
from .errors import ToolError, ToolNotAllowed
from .tools import HANDLERS

SERVER_NAME = "assistant-memory"

_STR = {"type": "string"}
_OBJ = {"type": "object"}

# Minimal input schemas (uuids are strings over the wire). Kept here, next to the
# transport, so the catalog stays pure metadata.
SCHEMAS: dict[str, dict] = {
    "conventions": {"type": "object", "properties": {}},
    "get": {
        "type": "object",
        "properties": {"node_id": _STR},
        "required": ["node_id"],
    },
    "search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural-language query (RU/EN)."},
            "filters": {"type": "object",
                        "description": "Optional. {\"type\": \"Note\"} and/or "
                                       "{\"status\": \"current\"} (status may be a list, e.g. "
                                       "[\"current\", \"provisional\"])."},
            "limit": {"type": "integer", "description": "Max results (default 20)."},
        },
    },
    "traverse": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "Start node id (UUID)."},
            "edges": {"type": "array", "items": _STR, "description": "Optional edge-type filter."},
            "direction": {"type": "string", "enum": ["in", "out", "both"],
                          "description": "Edge direction to follow (default both)."},
            "depth": {"type": "integer", "description": "Hops to walk, 1-5 (default 1)."},
        },
        "required": ["start"],
    },
    "explain": {
        "type": "object",
        "properties": {"node_id": _STR},
        "required": ["node_id"],
    },
    "timeline": {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string",
                          "description": "Optional node id (UUID) to scope to one entity."},
            "since": {"type": "string",
                      "description": "Optional ISO datetime; only newer versions (e.g. "
                                     "2026-06-01 or 2026-06-01T00:00:00Z)."},
            "limit": {"type": "integer", "description": "Max events (default 50, cap 200)."},
        },
    },
    "list_spaces": {"type": "object", "properties": {}},
    "list_node_types": {"type": "object", "properties": {}},
    "list_edge_types": {"type": "object", "properties": {}},
    "create_node_type": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "New node type name (e.g. Meeting)."},
            "default_sensitivity": {"type": "string",
                                    "description": "low|normal|high|critical (default normal)."},
            "description": {"type": "string", "description": "What this type is for."},
        },
        "required": ["type"],
    },
    "create_edge_type": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "New edge type name (e.g. attended)."},
            "category": {"type": "string", "enum": ["associative", "containment"],
                         "description": "Edge category (default associative)."},
            "sensitive": {"type": "boolean",
                          "description": "Whether edges of this type are always sensitive."},
            "description": {"type": "string", "description": "What this relationship means."},
        },
        "required": ["type"],
    },
    "retype_node": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id (UUID) to retype."},
            "new_type": {"type": "string", "description": "Target node type (must exist)."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["node_id", "new_type"],
    },
    "retype_edge": {
        "type": "object",
        "properties": {
            "edge_id": {"type": "string", "description": "Edge id (UUID) to retype."},
            "new_type": {"type": "string", "description": "Target edge type (must exist)."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["edge_id", "new_type"],
    },
    "remember_fact": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The fact, in the user's words."},
            "space": {"type": "string",
                      "description": "Target space id (UUID). Optional if you have one "
                                     "writable space; else call list_spaces."},
            "label": {"type": "string", "description": "Optional short title (else derived)."},
            "tags": {"type": "array", "items": _STR, "description": "Optional tag labels."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["text"],
    },
    "remember_preference": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The preference, in the user's words."},
            "space": {"type": "string",
                      "description": "Target space id (UUID). Optional if you have one "
                                     "writable space; else call list_spaces."},
            "label": {"type": "string", "description": "Optional short title (else derived)."},
            "tags": {"type": "array", "items": _STR,
                     "description": "Extra tag labels (always tagged `preference`)."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
            "domain": {"type": "string",
                       "description": "OPT-IN to operator-profile semantics: the registry "
                                      "conflict key this rule governs (e.g. 'language', "
                                      "'answer-format'). Without it the call keeps its "
                                      "legacy behavior (a plain preference Note)."},
            "scope": {"type": "string", "enum": ["global", "project"],
                      "description": "Profile scope (default project). global = all "
                                     "projects, lands in your operator zone; project = "
                                     "this zone, overrides global per domain."},
            "inferred": {"type": "boolean",
                         "description": "true = an UNCONFIRMED candidate inferred from "
                                        "behavior — excluded from profiles until "
                                        "confirm_preference; never touches the binding "
                                        "rule."},
            "new_domain": {"type": "boolean",
                           "description": "Propose a new registry key — created PENDING "
                                          "(non-binding) until the operator accepts it "
                                          "via resolve_domain."},
            "why": {"type": "string",
                    "description": "Portable rationale compiled into local profile files "
                                   "— interaction style only, no personal facts."},
            "retire": {"type": "boolean",
                       "description": "Stated retirement of the domain's BINDING rule "
                                      "(requires domain; text may be the reason): the "
                                      "member is superseded with no successor and leaves "
                                      "compilation."},
        },
        "required": ["text"],
    },
    "remember_decision": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The decision that was made."},
            "space": {"type": "string",
                      "description": "Target space id (UUID). Optional if you have one "
                                     "writable space; else call list_spaces."},
            "label": {"type": "string", "description": "Optional short title (else derived)."},
            "tags": {"type": "array", "items": _STR, "description": "Optional tag labels."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["text"],
    },
    # No `space` and no `confirm`, by design: the destination is the server's and the report
    # is not queued for approval. See tools.feedback.
    "feedback": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The feedback, in your words."},
            "kind": {"type": "string",
                     "enum": ["mis-application", "ambiguity", "gap", "suggestion"],
                     "description": "Optional category of the feedback."},
            "section": {"type": "string", "description": "Optional convention section, e.g. §9."},
        },
        "required": ["text"],
    },
    "create_node": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "Node type, e.g. Note, Task, Decision."},
            "space": {"type": "string",
                      "description": "Target space id (UUID); needs write. Optional if you "
                                     "have one writable space; else call list_spaces."},
            "properties": {"type": "object", "description": "Node content as JSON (may be large)."},
            "label": {"type": "string", "description": "Short human-readable title."},
            "sensitivity": {"type": "string",
                            "description": "Override low|normal|high|critical (default per type)."},
            "status": {"type": "string",
                       "enum": ["current", "provisional", "superseded", "disputed", "rejected"],
                       "description": "Epistemic validity (§6); default current. Use provisional "
                                      "for an unconfirmed observation."},
            "parent": {"type": "object",
                       "description": "Optional parent to contain this node (a contained_in edge "
                                      "is created WITH the node, atomically). Exactly one of: "
                                      "{\"node_id\": \"<uuid>\"} for a node that already exists, "
                                      "or {\"proposal_id\": \"<uuid>\"} to reference a parent you "
                                      "staged EARLIER IN THIS SAME TURN (use the proposal_id its "
                                      "staging returned) — the parent's node does not exist yet, "
                                      "and this is the only way to put a new child inside a new "
                                      "parent. Without it the child is created detached."},
            "confirm": {"type": "boolean",
                        "description": "Re-submit with true after the user approved a "
                                       "confirm_required write."},
        },
        "required": ["type"],
    },
    "update_node": {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "Node id (UUID) to update."},
            "expected_version": {"type": "string",
                                 "description": "The node's current_version_id (optimistic lock)."},
            "patch": {"type": "object", "description": "Shallow merge into properties."},
            "label": {"type": "string", "description": "New label (optional)."},
            "status": {"type": "string",
                       "enum": ["current", "provisional", "superseded", "disputed", "rejected"],
                       "description": "New epistemic status (§6). superseded/disputed are safe; "
                                      "provisional->current and rejected need a confirming event."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["node_id", "expected_version"],
    },
    "delete_node": {
        "type": "object",
        "properties": {
            "node_id": _STR,
            "expected_version": _STR,
            "confirm": {"type": "boolean"},
        },
        "required": ["node_id", "expected_version"],
    },
    "link": {
        "type": "object",
        "properties": {
            "type": _STR,
            "src": _STR,
            "dst": _STR,
            "properties": _OBJ,
            "confirm": {"type": "boolean"},
        },
        "required": ["type", "src", "dst"],
    },
    "unlink": {
        "type": "object",
        "properties": {"edge_id": _STR},
        "required": ["edge_id"],
    },
    "share_nodes": {
        "type": "object",
        "properties": {
            "target_space": _STR,
            "node_ids": {"type": "array", "items": _STR},
            "confirm": {"type": "boolean"},
        },
        "required": ["target_space", "node_ids"],
    },
    "share_subtree": {
        "type": "object",
        "properties": {
            "target_space": _STR,
            "anchor": _STR,
            "confirm": {"type": "boolean"},
        },
        "required": ["target_space", "anchor"],
    },
    "share_edge": {
        "type": "object",
        "properties": {
            "target_space": _STR,
            "edge_id": _STR,
            "confirm": {"type": "boolean"},
        },
        "required": ["target_space", "edge_id"],
    },
    "unshare": {
        "type": "object",
        "properties": {"node_id": _STR, "space": _STR},
        "required": ["node_id", "space"],
    },
    "unshare_edge": {
        "type": "object",
        "properties": {"edge_id": _STR, "space": _STR},
        "required": ["edge_id", "space"],
    },
    "create_scheduled_job": {
        "type": "object",
        "properties": {
            "instruction": {"type": "string",
                            "description": "Natural-language goal for the job."},
            "trigger": {
                "type": "object",
                "description": "When the job fires. kind one_shot: spec.at = ISO datetime; "
                               "kind interval: spec.seconds = integer >= 1 (optional "
                               "spec.start ISO); kind cron: spec.expr = non-empty cron "
                               "string. Optional predicate (recurring kinds ONLY — "
                               "one_shot+predicate is rejected): a deterministic boolean "
                               "over graph state, composite {all: [...]}|{any: [...]}|"
                               "{not: ...} or leaf {node: <uuid visible in the job's "
                               "target space>, path: 'a.b.c', op: eq|ne|lt|le|gt|ge|"
                               "exists|absent, value?}.",
                "properties": {
                    "kind": {"type": "string", "enum": ["one_shot", "interval", "cron"]},
                    "spec": {
                        "type": "object",
                        "properties": {
                            "at": {"type": "string", "description": "one_shot: ISO datetime"},
                            "seconds": {"type": "integer", "minimum": 1,
                                        "description": "interval period"},
                            "start": {"type": "string",
                                      "description": "interval: optional ISO first fire"},
                            "expr": {"type": "string", "minLength": 1,
                                     "description": "cron expression"},
                        },
                    },
                    "predicate": {
                        "type": "object",
                        "description": "Deterministic gate over structured graph state "
                                       "(leaf/composite grammar above); node refs are "
                                       "resolved in the job's effective scope.",
                    },
                },
                "required": ["kind"],
            },
            "space": _STR,
            "label": _STR,
            "agency": {"type": "string", "description": "gather_then_judge | autonomous."},
            "tools": {
                "type": "array",
                "description": "Ordered GATHER read-steps (D34); each result feeds REASON.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": "get | search | traverse | timeline | explain | "
                                           "recent_nodes_context",
                        },
                        "args": _OBJ,
                    },
                    "required": ["tool"],
                },
            },
            "writes": {
                "description": "Write fence (D8/D11): the node types this job may create/"
                               "update. Canonical forms: a list of non-empty type strings, "
                               "or {types: [...]}. Absent = no writes (least privilege).",
                "oneOf": [
                    {"type": "array", "items": {"type": "string", "minLength": 1}},
                    {
                        "type": "object",
                        "properties": {
                            "types": {"type": "array",
                                      "items": {"type": "string", "minLength": 1}},
                        },
                        "additionalProperties": False,
                    },
                ],
            },
            "budget": {"type": "object",
                       "description": "Fence caps; gather_chars (int, default 24000) caps the "
                                      "assembled GATHER context."},
            "delivery": {
                "type": "object",
                "description": "Where the job's report goes: {channel?: 'bot', target?: "
                               "'<recipient id>'}. Strings only; requires the bot channel "
                               "to be available on this runtime.",
                "properties": {"channel": _STR, "target": _STR},
                "additionalProperties": False,
            },
            "enabled": {"type": "boolean"},
            "confirm": {"type": "boolean"},
            "dry_run": {"type": "boolean",
                        "description": "Execute GATHER+REASON once, immediately, creating "
                                       "nothing — the D36 preflight probe."},
        },
        "required": ["instruction", "trigger"],
    },
    "list_scheduled_jobs": {
        "type": "object",
        "properties": {"space": _STR, "include_disabled": {"type": "boolean"}},
    },
    "set_scheduled_job_enabled": {
        "type": "object",
        "properties": {
            "node_id": _STR,
            "enabled": {"type": "boolean"},
            "confirm": {"type": "boolean"},
        },
        "required": ["node_id", "enabled"],
    },
    "create_review": {
        "type": "object",
        "properties": {
            "slug": {"type": "string",
                     "description": "Short review name, e.g. conventions-v2.2-foo."},
            "mode": {"type": "string", "enum": ["spec", "code"],
                     "description": "Critic-scope profile: spec = assembled context bundle; "
                                    "code = repo at artifact_ref."},
            "artifact_ref": {"type": "object",
                             "description": "Optional artifact pointer (repo/path/commit)."},
            "config": {"type": "object",
                       "description": "Optional review config (critic binding, acceptance notes)."},
            "instrument": {
                "type": "object",
                "description": "B.9 D-1 instrument block: host {hostname, username} "
                               "(required — where the critic runs) plus critic {engine, "
                               "model, effort} and development {engine, model}, each "
                               "either explicit or resolved from the recorded default; "
                               "optional self_check_waiver {granted_by, operator_quote}. "
                               "Creation refuses with a preparation route when the "
                               "host×engine has no proven launch profile or a model has "
                               "no (verified) list entry.",
            },
        },
        "required": ["slug", "mode"],
    },
    "get_operator_profile": {
        "type": "object",
        "properties": {
            "project": {"type": "string",
                        "description": "Optional project space id (UUID) or space NAME: "
                                       "that project's preferences override the globals "
                                       "per domain."},
        },
    },
    "confirm_preference": {
        "type": "object",
        "properties": {
            "preference_id": {"type": "string",
                              "description": "The preference node id (from "
                                             "remember_preference or get_operator_profile)."},
            "disposition": {"type": "string",
                            "enum": ["promote_current", "promote_candidate", "retire",
                                     "detach", "reject"],
                            "description": "REQUIRED for a parked node (promote_current | "
                                           "promote_candidate | retire | detach). For a "
                                           "CANDIDATE: omit to confirm, or pass 'reject' to "
                                           "decline it — the entry becomes a terminal "
                                           "rejected tombstone, freeing the domain+scope "
                                           "slot for a corrected re-proposal (B.13 A-9); "
                                           "repeating a reject answers 'already retired'."},
            "reason": {"type": "string",
                       "description": "reject only: the operator's rejection reason, "
                                      "recorded on the tombstone as rejected_reason."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["preference_id"],
    },
    "resolve_domain": {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "The registry key to act on."},
            "action": {"type": "string", "enum": ["accept", "reject", "alias"]},
            "canonical": {"type": "string",
                          "description": "alias only: the ACCEPTED canonical key to fold "
                                         "into."},
            "reason": {"type": "string", "description": "reject only: the operator's reason."},
            "confirm": {"type": "boolean", "description": "Re-submit after confirm_required."},
        },
        "required": ["domain", "action"],
    },
}

# Guard against catalog/dispatch/schema drift at import time.
assert set(SCHEMAS) == set(catalog.BY_NAME) == set(HANDLERS), "tool catalog/schema/dispatch drift"

# Server-level guidance returned at `initialize` — tells the client LLM how this
# memory works and how to behave (the "read me first" for the data plane).
INSTRUCTIONS = """\
Assistant Memory — a persistent, shared knowledge graph you read and write on the \
user's behalf. Nodes are typed facts; edges connect them. Everything is access-\
filtered to your credential and audited (the user can review and undo your writes).

FIRST CONTACT — before your first substantive read or write this session, call \
`conventions` (no arguments). It returns the operating rules for this shared memory \
(two stores, self-containment, kind x status, when to write, recall-before-acting, \
onboarding). Follow them, and persist the behavioral subset into your OWN local memory \
so you need not refetch every session. If you ALREADY have them saved locally from a \
past session, don't refetch in full — but once per session call `conventions` and \
compare its `version`; if it differs from your saved copy, re-read and update your local \
memory. If `conventions` is somehow empty, fall back to `search "assistant-memory \
conventions"`.

Read before you write: use `search` (semantic + full-text) or `get`/`traverse`/\
`explain`/`timeline` to check what already exists, then update instead of duplicating.

Node types (pick the closest): Note, Idea, Task, Person, List, ListItem, Recipe, \
Ingredient, Document, DocumentChunk, Project, Decision, ProjectState, Tag.
Edge types: contained_in (containment — one parent per node), relates_to, \
references, mentions, supersedes, uses, tagged_with, has_friend.

Ontology: list_node_types / list_edge_types show the current registry. Prefer an \
existing type; only when nothing fits, create_node_type / create_edge_type add one \
(trusted credentials only — keep the taxonomy small, avoid near-duplicates). Use \
retype_node / retype_edge to correct the type of an existing node or edge.

Where to write: every write goes into a space. Call `list_spaces` to see the \
spaces you can write to and their ids. If you have exactly one writable space you \
may omit `space` and it is used automatically; otherwise pass the id you chose.

Remembering (prefer these — they pick the type, set sensitivity, and de-dupe for you):
- remember_fact: a plain fact -> a Note.
- remember_preference: a user preference -> a Note tagged `preference`. With a `domain` \
argument it becomes an OPERATOR-PROFILE rule (delivered to every repo agent via \
get_operator_profile); `inferred=true` for rules you deduced (binding only after \
confirm_preference relays the operator's yes).
- remember_decision: a decision that was made -> a Decision (higher sensitivity).
Each takes `text` (and an optional `space`); if the same text already exists it \
returns that node ({"status":"exists"}) instead of creating a duplicate. Reach for \
create_node only when you need a type these don't cover.

Writing (low-level):
- create_node: put the real content in `properties` (JSON); keep `label` a short title.
- update_node: pass the node's current_version_id as `expected_version`. On a stale \
version you get a conflict — re-read (get) and retry.
- delete_node and unlink are soft and reversible.

Status (§6) — every node carries an epistemic `status` (default `current`), the \
validity axis orthogonal to its type: current | provisional | superseded | disputed \
| rejected. Set it on create_node / change it on update_node; filter on it in search \
({"status": "provisional"}). superseded/disputed are safe to flip; promoting \
provisional->current or marking rejected needs a confirming event — when unsure, keep \
it as is and say so. A rejected node is kept (not deleted) so it is not re-litigated.

Project zones & feedback: each project owns its zone (its Project subtree). Do NOT \
write into ANOTHER project's zone without explicit operator permission — work in the \
project of the current task. The one sanctioned cross-project write is `feedback`: to \
report that these conventions are unclear/mis-applied or to suggest an improvement, \
call `feedback` (not a hand-written node in the memory project) — it lands the report \
where the conventions owner triages it.

Sharing only changes visibility into spaces that ALREADY exist (share_nodes / \
share_subtree / share_edge). You cannot create spaces, accounts, memberships, or \
credentials — those live in the human admin UI, not here.

Write-policy responses — a write returns one of:
- {"status":"ok", ...}               applied.
- {"status":"confirm_required", ...} the user must approve; ask them, and only on \
their agreement re-call the SAME tool with confirm=true.
- {"status":"pending", ...}          staged for the human to review later; do NOT \
assume it took effect.
Sensitive content and sharing into multi-person spaces tighten this automatically.
"""

server: Server = Server(SERVER_NAME, instructions=INSTRUCTIONS)


def _annotations(spec: catalog.ToolSpec) -> mcp_types.ToolAnnotations:
    """Safety/UX hints for the client, derived from the tool's kind."""
    read_only = spec.kind == "read"
    idempotent = {
        "share_nodes", "share_edge", "unshare", "unshare_edge", "link",
        # remember_* de-dupe on exact text, so re-calling is safe.
        "remember_fact", "remember_preference", "remember_decision",
        # type-registry + retype converge to the same end state on re-call.
        "create_node_type", "create_edge_type", "retype_node", "retype_edge",
    }
    return mcp_types.ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=spec.name == "delete_node",
        idempotentHint=spec.name in idempotent,
    )


def _bearer_token() -> str:
    request: Request = server.request_context.request  # type: ignore[assignment]
    header = request.headers.get("authorization", "") if request is not None else ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ToolError("missing or malformed bearer token")
    return token.strip()


@server.list_tools()
async def list_tools() -> list[mcp_types.Tool]:
    token = _bearer_token()
    async with SessionLocal() as session:
        principal: Principal = await resolve_credential(session, token, touch=False)
    return [
        mcp_types.Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=SCHEMAS[spec.name],
            annotations=_annotations(spec),
        )
        for spec in catalog.visible_tools(principal)
    ]


async def _is_owner_account(session, account_id) -> bool:
    from ..models.identity import Account, User

    return (
        await session.scalar(
            select(User.id)
            .join(Account, Account.user_id == User.id)
            .where(Account.id == account_id, User.is_owner.is_(True))
            .limit(1)
        )
    ) is not None


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[mcp_types.TextContent]:
    token = _bearer_token()
    async with SessionLocal() as session:
        # The A-2 gate, at the MCP transport's own layer: the refusal is the
        # protocol's ordinary tool-error envelope carrying the wire contract's three
        # fields as JSON — never a transport-level failure a client cannot tell from
        # an outage. Same admission function as the HTTP chokepoint (install/gate.py).
        refusal = await install_gate.admission_check(session)
        if refusal is not None:
            raise ToolError(json.dumps(refusal))
        principal = await resolve_credential(session, token)
        if not catalog.is_allowed(principal, name):
            raise ToolNotAllowed(f"tool {name!r} not allowed for this credential")
        handler = HANDLERS.get(name)
        if handler is None:
            raise ToolError(f"unknown tool {name!r}")
        try:
            result = await handler(session, principal, **(arguments or {}))
            # A-4: the canonical probe. A successful authenticated `conventions` call
            # by the owner's credential over this transport is the ONE operation that
            # closes stage 5 — the server observes it here and writes the completion
            # record itself, in the same transaction. No other tool closes anything,
            # and no API exists for an agent to assert "installed".
            if name == "conventions" and await _is_owner_account(
                session, principal.account_id
            ):
                await write_completion_record(
                    session,
                    account_id=principal.account_id,
                    observation={
                        "operation": "conventions",
                        "transport": "mcp",
                        "credential_id": str(principal.credential_id),
                    },
                )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return [mcp_types.TextContent(type="text", text=json.dumps(result))]


# Stateless: each POST re-authenticates and runs independently (instant revoke).
session_manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)


async def mcp_asgi_app(scope: Any, receive: Any, send: Any) -> None:
    """ASGI entrypoint mounted under /mcp (see main.py). Lifespan runs the manager."""
    await session_manager.handle_request(scope, receive, send)
