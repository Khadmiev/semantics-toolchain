# SPDX-License-Identifier: Apache-2.0
"""Tool dispatch core: auth (B3) -> policy (B4) -> access (B2) -> repository (B1).

Each tool is ``async def tool(session, principal, **args) -> dict``. They are the
substance of the data plane and are unit-tested directly (no transport). The
caller (server.py) owns the transaction: it commits on success, rolls back on
error. Functions here flush via the repository but never commit.

Write/share tools run the write-policy engine and branch on its enforcement
venue (decisions §7):
- ``apply``           -> perform the mutation
- ``deny``            -> PermissionDenied
- ``server_proposal`` -> stage a Proposal row, return ``{status: "pending"}``
- ``client_confirm``  -> if ``confirm`` not yet given, return
                         ``{status: "confirm_required", ...}``; the client
                         re-calls with ``confirm=True`` after the human agrees.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import policy
from ..access import Access, containment_subtree
from ..auth.resolver import Principal
from ..config import settings
from ..conventions import FEEDBACK_HUB_LABEL, FEEDBACK_HUB_TEXT, FEEDBACK_TAG
from ..models.base import EDGE_CATEGORIES, NODE_STATUSES, SENSITIVITIES
from ..models.graph import Edge, EdgeType, Node, NodeSpace, NodeType, NodeVersion
from ..models.identity import Account, Credential, Membership, Space
from ..models.policy import Proposal
from ..models.profile import ProfileEntry
from ..profile import service as profile_service
from ..repository import errors as repo_errors
from ..repository import graph as repo
from ..review import errors as review_errors
from ..review import repository as review_repo
from ..scheduler import authoring
from ..scheduler import gather as gather_mod
from ..scheduler import predicate as scheduler_predicate
from ..scheduler.act import FenceError, parse_writes_scope
from ..scheduler.job import KEY_ENABLED
from ..search import get_embedder, index_node
from ..search import search as run_search
from .errors import Conflict, InvalidArgument, NotConfigured, NotFound, PermissionDenied

MAX_TRAVERSE_DEPTH = 5
CONVENTIONS_DOC_LABEL = "Assistant-memory conventions"  # the reserved onboarding anchor
FEEDBACK_KINDS = ("mis-application", "ambiguity", "gap", "suggestion")
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 100
DEFAULT_TIMELINE_LIMIT = 50
MAX_TIMELINE_LIMIT = 200
_DEDUP_POOL = 10  # search candidates scanned for an exact-text duplicate
_WRITE_PERMS = ("write", "admin")

# The containment edge (child -> parent). One name, used by every path that attaches a node to
# its parent, so "created with a parent" cannot mean two different edges.
CONTAINMENT_EDGE = "contained_in"


# --- coercion & serialization -------------------------------------------


def _as_uuid(value: Any, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidArgument(f"{field} is not a valid id: {value!r}") from exc


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _check_status(value: Any) -> str:
    """Validate an epistemic status against the §6 enum (the CHECK is the backstop)."""
    if value not in NODE_STATUSES:
        raise InvalidArgument(f"status must be one of {NODE_STATUSES}, got {value!r}")
    return value


def _node_dict(node: Node) -> dict:
    return {
        "id": str(node.id),
        "type": node.type,
        "label": node.label,
        "properties": node.properties,
        "status": node.status,
        "sensitivity": node.sensitivity,
        "current_version_id": str(node.current_version_id) if node.current_version_id else None,
        "origin_space": str(node.origin_space) if node.origin_space else None,
        "created_at": _iso(node.created_at),
    }


def _edge_dict(edge: Edge) -> dict:
    return {
        "id": str(edge.id),
        "type": edge.type,
        "src": str(edge.src_node),
        "dst": str(edge.dst_node),
        "sensitive": edge.sensitive,
        "properties": edge.properties,
    }


# --- shared helpers ------------------------------------------------------


def _scoped_access(session: AsyncSession, principal: Principal) -> Access:
    scopes = None if principal.scopes is None else {_as_uuid(s, "scope") for s in principal.scopes}
    return Access(session, principal.account_id, scopes=scopes)


def _in_scope(principal: Principal, space_id: uuid.UUID) -> bool:
    if principal.scopes is None:
        return True
    return str(space_id) in {str(s) for s in principal.scopes}


def _require_trusted(principal: Principal) -> None:
    """Ontology-registry changes are top-tier: only a trusted credential may reshape types."""
    if principal.trust != "trusted":
        raise PermissionDenied("extending the type registry requires a trusted credential")


async def _space_permission(
    session: AsyncSession, account_id: uuid.UUID, space_id: uuid.UUID
) -> str | None:
    return await session.scalar(
        select(Membership.permission).where(
            Membership.account_id == account_id, Membership.space_id == space_id
        )
    )


async def _require_space_write(
    session: AsyncSession, principal: Principal, space_id: uuid.UUID
) -> None:
    """The principal must hold write/admin in a space that is within its scope."""
    if not _in_scope(principal, space_id):
        raise PermissionDenied(f"space {space_id} is out of credential scope")
    perm = await _space_permission(session, principal.account_id, space_id)
    if perm not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission in space {space_id}")


async def _writable_space_ids(
    session: AsyncSession, principal: Principal
) -> list[uuid.UUID]:
    """Spaces the credential may write to (permission write/admin, within scope)."""
    access = _scoped_access(session, principal)
    space_ids = await access.space_ids()  # membership ∩ scope
    if not space_ids:
        return []
    rows = await session.scalars(
        select(Membership.space_id).where(
            Membership.account_id == principal.account_id,
            Membership.space_id.in_(space_ids),
            Membership.permission.in_(_WRITE_PERMS),
        )
    )
    return list(rows)


async def _resolve_write_space(
    session: AsyncSession, principal: Principal, space: Any
) -> uuid.UUID:
    """Validate an explicit target space, or default to the sole writable one.

    Writes need a space id. When the client omits it, we fall back to the
    credential's unique writable space (the common single-space case); if there
    are zero or several, we ask the client to pick one (via list_spaces).
    """
    if space is not None:
        space_id = _as_uuid(space, "space")
        await _require_space_write(session, principal, space_id)
        return space_id
    writable = await _writable_space_ids(session, principal)
    if len(writable) == 1:
        return writable[0]
    if not writable:
        raise PermissionDenied("this credential has no writable space")
    raise InvalidArgument(
        "several writable spaces — pass `space` (call list_spaces to choose): "
        + ", ".join(str(s) for s in writable)
    )


# What a staged (pending) write means, said in words the MODEL cannot misread. A bare
# {"status": "pending"} reads to an LLM as "not done yet" — so it re-issues the same call, and
# every retry is a NEW staged side-effect (Incident 4b087d59: three creates became six, the
# retry's arguments REGENERATED rather than replayed, hence a typo in the duplicate). The
# idempotent staging below makes the retry harmless; this note makes it unlikely.
_STAGED_NOTE = (
    "STAGED SUCCESSFULLY — this change is recorded and is now waiting for the operator's "
    "approval. It is NOT lost and NOT incomplete. Do NOT call this tool again for the same "
    "change: a repeat call cannot speed it up and is not needed. Continue with the rest of "
    "the request, or finish your reply."
)


async def _find_pending_twin(
    session: AsyncSession,
    principal: Principal,
    *,
    op: str,
    target_ref: dict,
    payload: dict,
) -> Proposal | None:
    """An identical proposal from this ACCOUNT that is still awaiting approval, if one exists.

    Idempotency key (operator ruling 2026-07-14): account + op + content + still-pending. The
    account (not the credential) is the unit: the operator is one person however many clients
    they drive. Scoped to `pending` so an APPROVED proposal never suppresses a later, genuinely
    intended repeat — asking for the same thing twice after confirming the first is a real
    request, not a retry.
    """
    return await session.scalar(
        select(Proposal)
        .join(Credential, Proposal.proposer_client == Credential.id)
        .where(
            Credential.account_id == principal.account_id,
            Proposal.status == "pending",
            Proposal.op == op,
            Proposal.target_ref == cast(target_ref, JSONB),
            Proposal.payload == cast(payload, JSONB),
        )
        .order_by(Proposal.created_at)
        .limit(1)
    )


async def _stage_proposal(
    session: AsyncSession,
    principal: Principal,
    *,
    op: str,
    target_ref: dict,
    payload: dict,
    decision: policy.Decision,
    sensitivity: str,
) -> tuple[Proposal, bool]:
    """Stage the mutation for approval, IDEMPOTENTLY. Returns (proposal, reused).

    Re-staging an identical still-pending change returns the EXISTING proposal instead of a
    second one. This is the mechanism, not the prose: a model that misreads "pending" as
    "not done" and retries can no longer duplicate the operator's data, whatever it believes.
    """
    twin = await _find_pending_twin(
        session, principal, op=op, target_ref=target_ref, payload=payload
    )
    if twin is not None:
        return twin, True
    proposal = Proposal(
        proposer_client=principal.credential_id,
        op=op,
        target_ref=target_ref,
        payload=payload,
        sensitivity=sensitivity,
        mode=decision.mode,
    )
    session.add(proposal)
    await session.flush()
    return proposal, False


async def _gate(
    session: AsyncSession,
    principal: Principal,
    *,
    operation: str,
    sensitivity: str,
    target_space_id: uuid.UUID | None,
    confirm: bool,
    op: str,
    target_ref: dict,
    payload: dict,
) -> dict | None:
    """Run the write policy and resolve the enforcement venue.

    Returns ``None`` when the caller should apply the mutation, or a terminal
    response dict (pending / confirm_required). Raises ``PermissionDenied`` on
    ``deny``.
    """
    decision = await policy.evaluate(
        session,
        operation=operation,
        effective_sensitivity=sensitivity,
        target_space_id=target_space_id,
        client_trust=principal.trust,
    )
    venue = policy.enforcement(decision, principal.trust)
    if venue == "apply":
        return None
    if venue == "deny":
        raise PermissionDenied(f"{operation} denied by policy (factor: {decision.factor})")
    if venue == "server_proposal":
        proposal, reused = await _stage_proposal(
            session,
            principal,
            op=op,
            target_ref=target_ref,
            payload=payload,
            decision=decision,
            sensitivity=sensitivity,
        )
        return {
            "status": "pending",
            "staged": True,
            "note": _STAGED_NOTE,
            "already_staged": reused,
            "proposal_id": str(proposal.id),
            "mode": decision.mode,
            "factor": decision.factor,
        }
    # client_confirm
    if confirm:
        return None
    return {"status": "confirm_required", "mode": decision.mode, "factor": decision.factor}


# --- read tools ----------------------------------------------------------


async def get(session: AsyncSession, principal: Principal, *, node_id: Any) -> dict:
    access = _scoped_access(session, principal)
    node = await access.get_node(_as_uuid(node_id, "node_id"))
    if node is None:
        raise NotFound(f"node {node_id} not visible")
    return _node_dict(node)


async def search(
    session: AsyncSession,
    principal: Principal,
    *,
    query: str | None = None,
    filters: dict | None = None,
    limit: int | None = None,
) -> dict:
    """Hybrid vector + full-text retrieval, access-filtered (B6)."""
    access = _scoped_access(session, principal)
    capped = min(limit or DEFAULT_SEARCH_LIMIT, MAX_SEARCH_LIMIT)
    nodes = await run_search(session, access, query, limit=capped, filters=filters)
    return {"results": [_node_dict(n) for n in nodes]}


async def _resolve_canonical_conventions(
    session: AsyncSession, *, space_ids: list[uuid.UUID] | None
) -> Node | None:
    """The conventions Document: canonical label, status `current`, oldest first.

    ONE predicate, stated once; what differs between callers is the SCOPE, and it differs
    deliberately. ``space_ids`` restricts the search to those spaces (the caller's visible
    set); ``None`` searches the whole deployment.

    Filtering on `current` is load-bearing, not tidiness. Three separately correct decisions
    compose into the exact defect this code exists to prevent: the retired duplicate stays in
    its old space, the canonical projection moves to the home space, and a credential that can
    see the former but not the latter would silently receive a STALE spec instead of being told
    to ask for access.
    """
    stmt = (
        select(Node)
        .where(
            Node.type == "Document",
            Node.label == CONVENTIONS_DOC_LABEL,
            Node.status == "current",
            Node.deleted_at.is_(None),
        )
        .order_by(Node.created_at)
        .limit(1)
    )
    if space_ids is not None:
        visible = select(NodeSpace.node_id).where(NodeSpace.space_id.in_(space_ids)).distinct()
        stmt = stmt.where(Node.id.in_(visible))
    return await session.scalar(stmt)


async def conventions(session: AsyncSession, principal: Principal) -> dict:
    """The operating conventions for this shared memory — read on first contact.

    Deterministically returns the canonical "Assistant-memory conventions" Document
    (the anchor) plus its atomic children (§1..§18 + plugins), in spec order, so a
    consumer can absorb the rules in one call instead of guessing a search query.
    Access-filtered like every read.
    """
    access = _scoped_access(session, principal)
    space_ids = await access.space_ids()
    doc = (
        await _resolve_canonical_conventions(session, space_ids=space_ids) if space_ids else None
    )
    if doc is None:
        # One answer for both shapes of the miss — sees nothing at all, or sees only a retired
        # copy. It deliberately does NOT suggest seeding: the conventions are ONE projection in
        # a home space shared by read access, and a caller acting on "seed it yourself" is how
        # a second, diverging copy gets made (it happened; 30 nodes).
        return {
            "found": False,
            "message": (
                "The conventions are not visible to this credential. They live in a single "
                "shared home space — ask the operator for READ access to it. Do not seed a "
                "copy: a second projection diverges from the canonical one. Follow the server "
                "instructions meanwhile."
            ),
        }
    children = [
        child
        for _edge, child in await access.neighbors(
            doc.id, direction="in", edge_types=["contained_in"]
        )
    ]
    children.sort(key=lambda n: n.created_at)  # seed order == spec order (§1..§18, plugins)
    sections = [
        {"label": c.label, "status": c.status, "text": (c.properties or {}).get("text")}
        for c in children
    ]
    return {
        "found": True,
        "version": (doc.properties or {}).get("version"),
        "document": _node_dict(doc),
        "sections": sections,
        "how_to_use": (
            "These are the rules for using this shared memory. Follow them, and persist the "
            "behavioral subset into your own local memory so you need not refetch each session. "
            "If you already saved them, compare `version` above with your local copy; on a "
            "mismatch, re-read and update it. Recall from the graph before you derive, decide, "
            "or assert (§17)."
        ),
    }


async def traverse(
    session: AsyncSession,
    principal: Principal,
    *,
    start: Any,
    edges: list[str] | None = None,
    direction: str = "both",
    depth: int = 1,
) -> dict:
    access = _scoped_access(session, principal)
    start_id = _as_uuid(start, "start")
    root = await access.get_node(start_id)
    if root is None:
        raise NotFound(f"node {start} not visible")
    if direction not in ("in", "out", "both"):
        raise InvalidArgument(f"direction must be in/out/both, got {direction!r}")
    depth = max(1, min(int(depth), MAX_TRAVERSE_DEPTH))

    seen_nodes: dict[uuid.UUID, Node] = {start_id: root}
    seen_edges: dict[uuid.UUID, Edge] = {}
    frontier = [start_id]
    for _ in range(depth):
        nxt: list[uuid.UUID] = []
        for nid in frontier:
            for edge, neighbor in await access.neighbors(
                nid, direction=direction, edge_types=edges
            ):
                seen_edges[edge.id] = edge
                if neighbor.id not in seen_nodes:
                    seen_nodes[neighbor.id] = neighbor
                    nxt.append(neighbor.id)
        frontier = nxt
        if not frontier:
            break
    return {
        "start": str(start_id),
        "nodes": [_node_dict(n) for n in seen_nodes.values()],
        "edges": [_edge_dict(e) for e in seen_edges.values()],
    }


async def explain(session: AsyncSession, principal: Principal, *, node_id: Any) -> dict:
    access = _scoped_access(session, principal)
    nid = _as_uuid(node_id, "node_id")
    node = await access.get_node(nid)
    if node is None:
        raise NotFound(f"node {node_id} not visible")
    versions = await session.scalars(
        select(NodeVersion).where(NodeVersion.node_id == nid).order_by(NodeVersion.created_at)
    )
    return {
        "node_id": str(nid),
        "versions": [
            {
                "id": str(v.id),
                "label": v.label,
                "source_ref": v.source_ref,
                "author": str(v.author) if v.author else None,
                "created_at": _iso(v.created_at),
            }
            for v in versions
        ],
    }


def _parse_since(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArgument(f"since is not a valid ISO datetime: {value!r}") from exc


async def timeline(
    session: AsyncSession,
    principal: Principal,
    *,
    entity_id: Any = None,
    since: Any = None,
    limit: int | None = None,
) -> dict:
    """Chronology of node versions (newest first), access-filtered.

    Each version is one change event. Optionally narrowed to a single entity or
    to versions created on/after ``since``. Deleted nodes are excluded, matching
    the read-path (the change feed in the admin UI is where deletions surface).
    """
    access = _scoped_access(session, principal)
    space_ids = await access.space_ids()
    if not space_ids:
        return {"events": []}
    capped = min(int(limit or DEFAULT_TIMELINE_LIMIT), MAX_TIMELINE_LIMIT)

    visible = select(NodeSpace.node_id).where(NodeSpace.space_id.in_(space_ids)).distinct()
    stmt = (
        select(NodeVersion, Node)
        .join(Node, Node.id == NodeVersion.node_id)
        .where(Node.id.in_(visible), Node.deleted_at.is_(None))
    )
    if entity_id is not None:
        nid = _as_uuid(entity_id, "entity_id")
        if await access.get_node(nid) is None:
            raise NotFound(f"node {entity_id} not visible")
        stmt = stmt.where(NodeVersion.node_id == nid)
    if since is not None:
        stmt = stmt.where(NodeVersion.created_at >= _parse_since(since))
    stmt = stmt.order_by(NodeVersion.created_at.desc()).limit(capped)

    events = [
        {
            "node_id": str(node.id),
            "version_id": str(version.id),
            "type": node.type,
            "label": version.label,
            "status": version.status,
            "author": str(version.author) if version.author else None,
            "created_at": _iso(version.created_at),
            "is_current": version.id == node.current_version_id,
        }
        for version, node in await session.execute(stmt)
    ]
    return {"events": events}


async def list_spaces(session: AsyncSession, principal: Principal) -> dict:
    """The spaces this credential can see, with permission and a writable flag.

    Writes need a target space id; this is how a client discovers valid ones.
    """
    access = _scoped_access(session, principal)
    space_ids = await access.space_ids()
    if not space_ids:
        return {"spaces": []}
    rows = await session.execute(
        select(Space.id, Space.name, Space.template, Membership.permission)
        .join(Membership, Membership.space_id == Space.id)
        .where(Membership.account_id == principal.account_id, Space.id.in_(space_ids))
        .order_by(Space.created_at)
    )
    return {
        "spaces": [
            {
                "id": str(sid),
                "name": name,
                "template": template,
                "permission": perm,
                "writable": perm in _WRITE_PERMS,
            }
            for sid, name, template, perm in rows
        ]
    }


async def list_node_types(session: AsyncSession, principal: Principal) -> dict:
    """The registered node types (type, default sensitivity, description)."""
    rows = await session.scalars(select(NodeType).order_by(NodeType.type))
    return {
        "node_types": [
            {"type": t.type, "default_sensitivity": t.default_sensitivity,
             "description": t.description}
            for t in rows
        ]
    }


async def list_edge_types(session: AsyncSession, principal: Principal) -> dict:
    """The registered edge types (type, category, sensitive flag, description)."""
    rows = await session.scalars(select(EdgeType).order_by(EdgeType.type))
    return {
        "edge_types": [
            {"type": t.type, "category": t.category, "sensitive": t.sensitive,
             "description": t.description}
            for t in rows
        ]
    }


# --- ontology (type registry) -------------------------------------------


def _node_type_dict(t: NodeType) -> dict:
    return {"type": t.type, "default_sensitivity": t.default_sensitivity,
            "description": t.description}


def _edge_type_dict(t: EdgeType) -> dict:
    return {"type": t.type, "category": t.category, "sensitive": t.sensitive,
            "description": t.description}


async def create_node_type(
    session: AsyncSession,
    principal: Principal,
    *,
    type: str,
    default_sensitivity: str = "normal",
    description: str | None = None,
) -> dict:
    """Register a new node type (trusted only). Idempotent: existing -> {status: exists}."""
    _require_trusted(principal)
    name = (type or "").strip()
    if not name:
        raise InvalidArgument("type must be a non-empty string")
    if default_sensitivity not in SENSITIVITIES:
        raise InvalidArgument(f"default_sensitivity must be one of {', '.join(SENSITIVITIES)}")
    existing = await session.get(NodeType, name)
    if existing is not None:
        return {"status": "exists", "node_type": _node_type_dict(existing)}
    node_type = NodeType(
        type=name, default_sensitivity=default_sensitivity, description=description
    )
    session.add(node_type)
    await session.flush()
    return {"status": "ok", "node_type": _node_type_dict(node_type)}


async def create_edge_type(
    session: AsyncSession,
    principal: Principal,
    *,
    type: str,
    category: str = "associative",
    sensitive: bool = False,
    description: str | None = None,
) -> dict:
    """Register a new edge type (trusted only). Idempotent: existing -> {status: exists}."""
    _require_trusted(principal)
    name = (type or "").strip()
    if not name:
        raise InvalidArgument("type must be a non-empty string")
    if category not in EDGE_CATEGORIES:
        raise InvalidArgument(f"category must be one of {', '.join(EDGE_CATEGORIES)}")
    existing = await session.get(EdgeType, name)
    if existing is not None:
        return {"status": "exists", "edge_type": _edge_type_dict(existing)}
    edge_type = EdgeType(
        type=name, category=category, sensitive=bool(sensitive), description=description
    )
    session.add(edge_type)
    await session.flush()
    return {"status": "ok", "edge_type": _edge_type_dict(edge_type)}


async def retype_node(
    session: AsyncSession,
    principal: Principal,
    *,
    node_id: Any,
    new_type: str,
    confirm: bool = False,
) -> dict:
    """Change a node's type to another registered type (needs write on the node)."""
    access = _scoped_access(session, principal)
    nid = _as_uuid(node_id, "node_id")
    node = await access.get_node(nid)
    if node is None:
        raise NotFound(f"node {node_id} not visible")
    if await access.effective_permission(nid) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on node {node_id}")
    if await session.get(NodeType, new_type) is None:
        raise InvalidArgument(f"unknown node type {new_type!r}")
    effective = await policy.effective_node_sensitivity(session, node)

    gated = await _gate(
        session,
        principal,
        operation="update",
        sensitivity=effective,
        target_space_id=node.origin_space,
        confirm=confirm,
        op="retype_node",
        target_ref={"node_id": str(nid), "new_type": new_type},
        payload={"new_type": new_type},
    )
    if gated is not None:
        return gated

    old_type = node.type
    node.type = new_type
    await session.flush()
    return {"status": "ok", "node_id": str(nid), "old_type": old_type, "new_type": new_type}


async def retype_edge(
    session: AsyncSession,
    principal: Principal,
    *,
    edge_id: Any,
    new_type: str,
    confirm: bool = False,
) -> dict:
    """Change an active edge's type (needs write on its source node).

    The sensitive flag is re-derived from the new type and endpoints. Fails with a
    conflict if the change would collide with the single-container or active-edge
    uniqueness rules.
    """
    access = _scoped_access(session, principal)
    eid = _as_uuid(edge_id, "edge_id")
    edge = await session.get(Edge, eid)
    if edge is None or edge.valid_to is not None:
        raise NotFound(f"edge {edge_id} not active")
    if await access.get_node(edge.src_node) is None:
        raise NotFound(f"edge {edge_id} not visible")
    if await access.effective_permission(edge.src_node) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on edge {edge_id}")
    if await session.get(EdgeType, new_type) is None:
        raise InvalidArgument(f"unknown edge type {new_type!r}")

    new_sensitive = await repo._derive_sensitive(session, new_type, edge.src_node, edge.dst_node)
    effective = "high" if (edge.sensitive or new_sensitive) else "normal"
    gated = await _gate(
        session,
        principal,
        operation="update",
        sensitivity=effective,
        target_space_id=None,
        confirm=confirm,
        op="retype_edge",
        target_ref={"edge_id": str(eid), "new_type": new_type},
        payload={"new_type": new_type},
    )
    if gated is not None:
        return gated

    old_type = edge.type
    try:
        async with session.begin_nested():
            edge.type = new_type
            edge.sensitive = new_sensitive
            await session.flush()
    except IntegrityError as exc:
        detail = str(getattr(exc, "orig", exc))
        if "uq_one_container" in detail:
            raise Conflict(f"node {edge.src_node} already has an active container") from exc
        if "uq_active_edge" in detail:
            raise Conflict(
                f"an active {new_type} edge already exists between these nodes"
            ) from exc
        raise
    return {
        "status": "ok",
        "edge_id": str(eid),
        "old_type": old_type,
        "new_type": new_type,
        "sensitive": new_sensitive,
    }


# --- semantic memory tools (high-level wrappers over create_node) --------


def _derive_label(text: str, label: str | None) -> str:
    if label and label.strip():
        return label.strip()[:120]
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first[:120] or "note"


async def _find_duplicate(
    session: AsyncSession, access: Access, *, node_type: str, text: str
) -> Node | None:
    """An existing visible node of this type whose `text` matches (search-first dedup)."""
    norm = text.strip().casefold()
    candidates = await run_search(
        session, access, text, limit=_DEDUP_POOL, filters={"type": node_type}
    )
    for node in candidates:
        existing = (node.properties or {}).get("text")
        if isinstance(existing, str) and existing.strip().casefold() == norm:
            return node
    return None


async def _find_or_create_tag(
    session: AsyncSession, principal: Principal, *, space_id: uuid.UUID, label: str
) -> Node:
    """Reuse a Tag node with this label in the space, else create one (low sensitivity)."""
    norm = label.strip()
    existing = await session.scalar(
        select(Node)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .where(
            NodeSpace.space_id == space_id,
            Node.type == "Tag",
            Node.deleted_at.is_(None),
            func.lower(Node.label) == norm.lower(),
        )
        .limit(1)
    )
    if existing is not None:
        return existing
    tag = await repo.create_node(
        session, type="Tag", space_id=space_id, account_id=principal.account_id, label=norm
    )
    await index_node(session, tag, get_embedder())
    return tag


async def _remember(
    session: AsyncSession,
    principal: Principal,
    *,
    node_type: str,
    auto_tags: tuple[str, ...],
    text: Any,
    space: Any = None,
    label: str | None = None,
    tags: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    """Shared body for remember_fact/preference/decision.

    De-dupes on exact text (returns the existing node), otherwise creates a typed
    node through the same write policy as create_node, then attaches `tagged_with`
    edges to Tag nodes. Tags are only wired up once the node itself is applied.
    ``space`` may be omitted when the credential has a single writable space.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidArgument("text must be a non-empty string")
    space_id = await _resolve_write_space(session, principal, space)

    access = _scoped_access(session, principal)
    dup = await _find_duplicate(session, access, node_type=node_type, text=text)
    if dup is not None:
        return {"status": "exists", "node": _node_dict(dup)}

    node_type_row = await session.get(NodeType, node_type)
    effective = node_type_row.default_sensitivity  # sensitivity follows the type
    resolved_label = _derive_label(text, label)
    properties = {"text": text.strip()}

    gated = await _gate(
        session,
        principal,
        operation="create",
        sensitivity=effective,
        target_space_id=space_id,
        confirm=confirm,
        op="create_node",
        target_ref={"space": str(space_id), "type": node_type},
        payload={"label": resolved_label, "properties": properties, "sensitivity": None},
    )
    if gated is not None:
        return gated

    node = await repo.create_node(
        session,
        type=node_type,
        space_id=space_id,
        account_id=principal.account_id,
        label=resolved_label,
        properties=properties,
    )
    await index_node(session, node, get_embedder())

    wanted = [t for t in [*auto_tags, *(tags or [])] if isinstance(t, str) and t.strip()]
    attached = []
    for tag_label in dict.fromkeys(wanted):  # de-dupe, preserve order
        tag = await _find_or_create_tag(session, principal, space_id=space_id, label=tag_label)
        await repo.link(
            session,
            type="tagged_with",
            src_node=node.id,
            dst_node=tag.id,
            account_id=principal.account_id,
        )
        attached.append({"id": str(tag.id), "label": tag.label})

    return {"status": "ok", "node": _node_dict(node), "tags": attached}


async def remember_fact(
    session: AsyncSession,
    principal: Principal,
    *,
    text: Any,
    space: Any = None,
    label: str | None = None,
    tags: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    return await _remember(
        session, principal, node_type="Note", auto_tags=(),
        text=text, space=space, label=label, tags=tags, confirm=confirm,
    )


async def remember_preference(
    session: AsyncSession,
    principal: Principal,
    *,
    text: Any,
    space: Any = None,
    label: str | None = None,
    tags: list[str] | None = None,
    confirm: bool = False,
    domain: Any = None,
    scope: Any = None,
    inferred: bool = False,
    new_domain: bool = False,
    why: Any = None,
    retire: bool = False,
) -> dict:
    """Legacy shape untouched: WITHOUT `domain` this is exactly the old call (a
    preference Note in the current zone, outside profile compilation). WITH
    `domain` it opts into operator-profile semantics (spec B2): registry-validated
    conflict key, standing split (stated=member / inferred=candidate with a
    recorded resolved base), scope routing (global → the operator zone, the
    sanctioned cross-project write), server-set membership marker."""
    if domain is None:
        if scope is not None or inferred or new_domain or retire:
            raise InvalidArgument(
                "scope/inferred/new_domain/retire are profile arguments — they require `domain`"
            )
        return await _remember(
            session, principal, node_type="Note", auto_tags=("preference",),
            text=text, space=space, label=label, tags=tags, confirm=confirm,
        )

    if retire and inferred:
        raise InvalidArgument("retire is a stated operation — it cannot be inferred")
    if not retire and (not isinstance(text, str) or not text.strip()):
        raise InvalidArgument("text must be a non-empty string")
    if scope is None:
        scope_val = "project"
    elif isinstance(scope, str):
        scope_val = scope.strip().lower()
    else:
        scope_val = None
    if scope_val not in ("global", "project"):
        raise InvalidArgument("scope must be 'global' or 'project'")

    account = await session.get(Account, principal.account_id)
    try:
        # Validate + normalize the domain at AUTHOR time (fail at the author,
        # immediately — the wire-contract precedent), even when the write stages.
        key, _state = await profile_service.normalize_domain(
            session, domain, new_domain=new_domain
        )
        if scope_val == "global":
            # B3: the operator zone. VALIDATION ONLY before the gate (F002) — the
            # Person bootstrap is a mutation and happens inside the applied write.
            space_id = await profile_service.personal_space_id(session, principal.account_id)
            if space_id is None:
                raise profile_service.ProfileError(
                    "this account has no personal space — onboarding is incomplete; a "
                    "global preference cannot be stored (and will not fall back into "
                    "another zone)"
                )
            project_space_id = None
        else:
            space_id = await _resolve_write_space(session, principal, space)
            project_space_id = space_id
    except profile_service.ProfileError as exc:
        raise InvalidArgument(str(exc)) from exc

    gated = await _gate(
        session,
        principal,
        operation="update" if retire else "create",
        sensitivity="normal",
        target_space_id=space_id,
        confirm=confirm,
        op="profile_write",
        target_ref={"space": str(space_id), "domain": key, "scope": scope_val},
        payload={
            "text": text.strip() if isinstance(text, str) else None,
            "why": why if isinstance(why, str) else None,
            "inferred": bool(inferred),
            "new_domain": bool(new_domain),
            "retire": bool(retire),
        },
    )
    if gated is not None:
        return gated

    caller_cred = (
        await session.get(Credential, principal.credential_id)
        if principal.credential_id
        else None
    )
    try:
        if retire:
            return await profile_service.retire_preference(
                session,
                account=account,
                domain=key,
                scope=scope_val,
                project_space_id=project_space_id,
                credential=caller_cred,
            )
        result = await profile_service.write_preference(
            session,
            account=account,
            text=text,
            domain=key,
            scope=scope_val,
            space_id=space_id,
            project_space_id=project_space_id,
            why=why if isinstance(why, str) else None,
            inferred=bool(inferred),
            new_domain=bool(new_domain),
            credential=caller_cred,
        )
    except profile_service.ProfileError as exc:
        raise InvalidArgument(str(exc)) from exc
    node = await session.get(Node, uuid.UUID(result["node_id"]))
    await index_node(session, node, get_embedder())
    return {"status": "ok", **result}


async def get_operator_profile(
    session: AsyncSession,
    principal: Principal,
    *,
    project: Any = None,
) -> dict:
    """B1: the compiled operator profile for this credential's ACCOUNT (identity
    folded in per B3 — no separate whoami). `project` is an optional space id;
    project-scoped preferences of that space override the globals per domain."""
    account = await session.get(Account, principal.account_id)
    project_space_id = None
    if project is not None:
        # F009: the contract says id OR slug/name — resolve a non-UUID by space name.
        try:
            project_space_id = uuid.UUID(str(project))
        except (ValueError, AttributeError, TypeError):
            project_space_id = await session.scalar(
                select(Space.id)
                .join(Membership, Membership.space_id == Space.id)
                .where(
                    Membership.account_id == principal.account_id,
                    Space.name == str(project),
                )
                .order_by(Space.created_at)
                .limit(1)
            )
            if project_space_id is None:
                raise NotFound(
                    f"no project space named {project!r} for this account"
                ) from None
    # F011: the CREDENTIAL's effective read scope (membership ∩ scopes) drives
    # both the project check and the resolution — access changes change the
    # resolved set, and therefore the version.
    scopes = None if principal.scopes is None else {
        _as_uuid(s, "scope") for s in principal.scopes
    }
    visible = await profile_service.visible_space_ids(
        session, account_id=principal.account_id, scopes=scopes
    )
    if project_space_id is not None and project_space_id not in visible:
        raise PermissionDenied(
            "the requested project space is outside this credential's effective scope"
        )
    return await profile_service.build_profile(
        session,
        account=account,
        project_space_id=project_space_id,
        visible_spaces=visible,
    )


async def confirm_preference(
    session: AsyncSession,
    principal: Principal,
    *,
    preference_id: Any,
    disposition: Any = None,
    confirm: bool = False,
    reason: Any = None,
) -> dict:
    """B4: candidate confirmation (no disposition), candidate REJECTION
    (disposition="reject", B.13 A-9's correction branch — the entry becomes a
    terminal rejected tombstone and the slot frees for a corrected re-proposal),
    or parked disposition (disposition required). Ownership = account equality;
    the operator's confirmation is relayed by the calling agent."""
    pid = _as_uuid(preference_id, "preference_id")
    if reason is not None and not isinstance(reason, str):
        raise InvalidArgument("reason must be a string")
    # F004: ordinary access applies on top of account-equality — the node must be
    # VISIBLE to this credential and the account must hold write on it.
    access = _scoped_access(session, principal)
    node = await access.get_node(pid)
    if node is None:
        raise NotFound(f"node {preference_id} not visible")
    if await access.effective_permission(pid) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on node {preference_id}")
    if disposition is not None and not isinstance(disposition, str):
        raise InvalidArgument("disposition must be a string")

    # F005: capture the slot state the operator is ruling on, so a STAGED
    # disposition refuses at apply time if the slot moved meanwhile.
    entry_row = await session.get(ProfileEntry, pid)
    snapshot = None
    if entry_row is not None:
        # F019: a project preference is authorized through ITS OWN project space —
        # visibility/write via a shared copy elsewhere is not enough.
        try:
            await profile_service.check_entry_access(
                session,
                entry=entry_row,
                scopes=None if principal.scopes is None else {
                    _as_uuid(s, "scope") for s in principal.scopes
                },
            )
        except profile_service.OwnershipError as exc:
            raise PermissionDenied(str(exc)) from exc
        if entry_row.standing == "parked":
            member_slot, candidate_slot = await profile_service.current_slots(
                session, entry_row
            )
            snapshot = profile_service.observed_slots(member_slot, candidate_slot)

    gated = await _gate(
        session,
        principal,
        operation="update",
        sensitivity="normal",
        target_space_id=node.origin_space,
        confirm=confirm,
        op="profile_confirm",
        target_ref={"node_id": str(pid)},
        # The FULL parameter set rides the staged payload — a parameter present on the
        # direct path and absent here is silently replaced at apply time (review
        # f46a31d2, finding staged-rejection-drops-reason: a staged rejection lost the
        # operator's stated reason to the fallback text).
        payload={"disposition": disposition, "slot_snapshot": snapshot, "reason": reason},
    )
    if gated is not None:
        return gated

    account = await session.get(Account, principal.account_id)
    credential = (
        await session.get(Credential, principal.credential_id)
        if principal.credential_id
        else None
    )
    try:
        return await profile_service.confirm_preference(
            session,
            account=account,
            credential=credential,
            preference_id=pid,
            disposition=disposition,
            expected_slots=snapshot,
            reason=reason,
        )
    except profile_service.BaseMismatch as exc:
        return exc.payload
    except profile_service.OwnershipError as exc:
        raise PermissionDenied(str(exc)) from exc
    except profile_service.ProfileError as exc:
        raise InvalidArgument(str(exc)) from exc


async def resolve_domain(
    session: AsyncSession,
    principal: Principal,
    *,
    domain: Any,
    action: Any,
    canonical: Any = None,
    reason: Any = None,
    confirm: bool = False,
) -> dict:
    """B5: the registry gate — accept | reject | alias, relaying the operator's
    decision. Registry entries are totally protected; this is their ONLY
    mutation path. Trusted credentials apply; limited-trust ones stage."""
    if action not in ("accept", "reject", "alias"):
        raise InvalidArgument("action must be accept | reject | alias")

    gated = await _gate(
        session,
        principal,
        operation="update",
        sensitivity="normal",
        target_space_id=None,
        confirm=confirm,
        op="profile_domain",
        target_ref={"domain": str(domain), "action": str(action)},
        payload={"canonical": canonical, "reason": reason},
    )
    if gated is not None:
        return gated

    account = await session.get(Account, principal.account_id)
    try:
        return await profile_service.resolve_domain_action(
            session,
            account=account,
            domain=str(domain),
            action=str(action),
            canonical=str(canonical) if canonical else None,
            reason=str(reason) if reason else None,
        )
    except profile_service.ProfileError as exc:
        raise InvalidArgument(str(exc)) from exc


async def remember_decision(
    session: AsyncSession,
    principal: Principal,
    *,
    text: Any,
    space: Any = None,
    label: str | None = None,
    tags: list[str] | None = None,
    confirm: bool = False,
) -> dict:
    return await _remember(
        session, principal, node_type="Decision", auto_tags=(),
        text=text, space=space, label=label, tags=tags, confirm=confirm,
    )


async def _find_or_create_feedback_hub(
    session: AsyncSession, principal: Principal, space_id: uuid.UUID
) -> Node:
    """The reserved 'Conventions feedback' hub, in the destination space only.

    It used to search every space the credential could see, unordered and LIMIT 1 — so with
    two hubs present the database picked the parent, and a report created in one space could
    be attached to a parent in another. With a fixed destination the wider search buys
    nothing, so it is gone.

    B.11 D-4: STATUS IS FILTERED, so a RETIRED hub cannot silently become the destination
    of new reports. Found by combing the class of A-1 rather than by an incident — this one
    has never fired. It is repaired because it is the last member of that class whose
    status-blindness is not deliberate: the two other status-blind lookups nearby
    (`conventions_exist_anywhere`, `_refuse_if_projected_elsewhere`) ignore status ON
    PURPOSE and say so in their own docstrings. That is the rule this closes the class
    with — a resolution either filters status or records why it does not — and this
    docstring is now the fourth entry under it.
    """
    hub = await session.scalar(
        select(Node)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .where(
            NodeSpace.space_id == space_id,
            Node.type == "Note",
            Node.label == FEEDBACK_HUB_LABEL,
            Node.deleted_at.is_(None),
            Node.status == "current",
        )
        .order_by(Node.created_at)
        .limit(1)
    )
    if hub is not None:
        return hub
    hub = await repo.create_node(
        session, type="Note", space_id=space_id, account_id=principal.account_id,
        label=FEEDBACK_HUB_LABEL, properties={"text": FEEDBACK_HUB_TEXT},
    )
    await index_node(session, hub, get_embedder())
    return hub


async def feedback(
    session: AsyncSession,
    principal: Principal,
    *,
    text: Any,
    kind: str | None = None,
    section: str | None = None,
) -> dict:
    """Report feedback about the conventions / memory-behavior — the sanctioned channel
    so a foreign-project agent never hand-writes into the conventions owner's zone.

    Lands a provisional `Feedback` node in the 'Conventions feedback' hub, tagged and
    attributed to the reporting account; the §14 pass triages it.

    A SERVER-ROUTED operation. The caller supplies text, kind and section and nothing else:
    the destination space, type, status, parent, tags, reporting account and conventions
    version are all the server's. Three consequences, each deliberate:

    * There is no `space` parameter, now or later. Feedback is about the memory project,
      wherever the reporter happens to be working — the destination is not the caller's
      business, and handing it back to them is what left this channel dead (the old code
      asked a resolver that refuses whenever a credential has several writable spaces, from
      a tool with no way to answer: two refusals facing each other, no working call at all).
    * The caller's write permission on the destination is NOT checked. Otherwise a restricted
      account — the case the whole channel exists for — cannot report at all. The escalation
      is bounded by construction (one node type, one status, one parent) and attribution is
      preserved on the node.
    * It does not pass the write-policy gate. Queueing bug reports for approval defeats a
      cheap defect-reporting channel; the canon already names feedback the one exemption.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidArgument("text must be a non-empty string")
    if kind is not None and kind not in FEEDBACK_KINDS:
        raise InvalidArgument(f"kind must be one of {FEEDBACK_KINDS}, got {kind!r}")
    space_id = settings.feedback_space_id
    if space_id is None:
        # No fallback. A node must live somewhere, and guessing where is precisely how a
        # second conventions projection got made; so this names the missing step instead.
        raise NotConfigured(
            "feedback has no destination: this deployment has not configured the feedback "
            "space (AM_FEEDBACK_SPACE_ID). The installation is unfinished — ask the operator "
            "to set it and restart the service."
        )
    node_type = await session.get(NodeType, "Feedback")
    if node_type is None:
        raise InvalidArgument("Feedback node type is not registered")

    properties: dict[str, Any] = {
        "text": text.strip(),
        "reporter_account": str(principal.account_id),
    }
    if kind:
        properties["kind"] = kind
    if section:
        properties["section"] = section
    # The conventions edition this report is about, read by the server at write time and
    # resolved across the DEPLOYMENT — a reporter is not required to be able to SEE the
    # conventions in order to report on them. A version stamp is durable where an edge is
    # not: it survives re-projection, renumbering, and the projection changing space. When
    # it cannot be read the field is simply omitted; a report never fails over a version.
    doc = await _resolve_canonical_conventions(session, space_ids=None)
    version = (doc.properties or {}).get("version") if doc is not None else None
    if version:
        properties["conventions_version"] = version
    label = _derive_label(f"feedback: {text}", None)

    node = await repo.create_node(
        session, type="Feedback", space_id=space_id, account_id=principal.account_id,
        label=label, properties=properties, status="provisional",
    )
    await index_node(session, node, get_embedder())

    hub = await _find_or_create_feedback_hub(session, principal, space_id)
    await repo.link(session, type="contained_in", src_node=node.id, dst_node=hub.id,
                    account_id=principal.account_id)
    tag = await _find_or_create_tag(session, principal, space_id=space_id, label=FEEDBACK_TAG)
    await repo.link(session, type="tagged_with", src_node=node.id, dst_node=tag.id,
                    account_id=principal.account_id)

    return {"status": "ok", "node": _node_dict(node)}


# --- write tools ---------------------------------------------------------


async def _resolve_parent(
    session: AsyncSession, principal: Principal, parent: Any
) -> dict:
    """Validate a ``parent`` reference and normalize it for the proposal payload.

    Accepts exactly one of ``{"node_id": ...}`` (a node that already exists) or
    ``{"proposal_id": ...}`` (a SIBLING create_node proposal staged in the same turn — its node
    does not exist yet and will be resolved at apply time through ``Proposal.created_node_id``).

    The sibling form is the whole point: under propose-not-act a staged create returns no node
    id, so without it "put this item in that list" cannot be expressed at all and every created
    structure arrives with no edges (Incident 4b087d59).
    """
    if not isinstance(parent, dict):
        raise InvalidArgument('parent must be an object: {"node_id": ...} or {"proposal_id": ...}')
    keys = {k for k in ("node_id", "proposal_id") if parent.get(k) is not None}
    if len(keys) != 1:
        raise InvalidArgument('parent needs exactly one of "node_id" or "proposal_id"')

    if "node_id" in keys:
        pid = _as_uuid(parent["node_id"], "parent.node_id")
        access = _scoped_access(session, principal)
        node = await access.get_node(pid)
        if node is None:
            raise NotFound(f"parent node {parent['node_id']} not visible")
        if await access.effective_permission(pid) not in _WRITE_PERMS:
            raise PermissionDenied(f"no write permission on parent node {parent['node_id']}")
        return {"node_id": str(pid)}

    sid = _as_uuid(parent["proposal_id"], "parent.proposal_id")
    sibling = await session.scalar(
        select(Proposal)
        .join(Credential, Proposal.proposer_client == Credential.id)
        .where(
            Proposal.id == sid,
            Credential.account_id == principal.account_id,
            Proposal.op == "create_node",
            Proposal.status == "pending",
        )
    )
    if sibling is None:
        raise InvalidArgument(
            f"parent.proposal_id {parent['proposal_id']} is not one of your pending create_node "
            "proposals — a parent proposal must be staged (and still awaiting approval) first"
        )
    return {"proposal_id": str(sid)}


async def create_node(
    session: AsyncSession,
    principal: Principal,
    *,
    type: str,
    space: Any = None,
    properties: dict | None = None,
    label: str | None = None,
    sensitivity: str | None = None,
    status: str = "current",
    parent: dict | None = None,
    confirm: bool = False,
) -> dict:
    space_id = await _resolve_write_space(session, principal, space)
    node_type = await session.get(NodeType, type)
    if node_type is None:
        raise InvalidArgument(f"unknown node type {type!r}")
    _check_status(status)
    effective = sensitivity or node_type.default_sensitivity
    parent_ref = (
        await _resolve_parent(session, principal, parent) if parent is not None else None
    )

    gated = await _gate(
        session,
        principal,
        operation="create",
        sensitivity=effective,
        target_space_id=space_id,
        confirm=confirm,
        op="create_node",
        target_ref={"space": str(space_id), "type": type},
        payload={
            "label": label,
            "properties": properties,
            "sensitivity": sensitivity,
            "status": status,
            "parent": parent_ref,
        },
    )
    if gated is not None:
        return gated

    node = await repo.create_node(
        session,
        type=type,
        space_id=space_id,
        account_id=principal.account_id,
        label=label,
        properties=properties,
        sensitivity=sensitivity,
        status=status,
    )
    await index_node(session, node, get_embedder())
    result = {"status": "ok", "node": _node_dict(node)}
    if parent_ref is not None:
        # Applied (trusted) path: the parent must already be a real node — a trusted caller does
        # not stage, so there is no sibling proposal to resolve. Same transaction as the create:
        # a node that was asked for WITH a parent is never left orphaned.
        if "proposal_id" in parent_ref:
            raise InvalidArgument(
                "parent.proposal_id is only meaningful for staged (propose-not-act) writes; "
                "your writes apply directly — pass parent.node_id instead"
            )
        edge = await repo.link(
            session,
            type=CONTAINMENT_EDGE,
            src_node=node.id,
            dst_node=uuid.UUID(parent_ref["node_id"]),
            account_id=principal.account_id,
        )
        result["edge"] = _edge_dict(edge)
    return result


async def update_node(
    session: AsyncSession,
    principal: Principal,
    *,
    node_id: Any,
    expected_version: Any,
    patch: dict | None = None,
    label: Any = repo._UNSET,
    status: Any = repo._UNSET,
    confirm: bool = False,
) -> dict:
    access = _scoped_access(session, principal)
    nid = _as_uuid(node_id, "node_id")
    node = await access.get_node(nid)
    if node is None:
        raise NotFound(f"node {node_id} not visible")
    if await access.effective_permission(nid) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on node {node_id}")
    # Operator-profile total protection (E34): a profile preference or registry
    # node is control-plane data — ANY generic update is refused (a benign edit
    # would mint a current version outside the validated path and silently
    # de-publish the rule). Mutations go through the profile capabilities.
    protected = await profile_service.protected_kind(session, nid)
    if protected is not None:
        raise PermissionDenied(
            f"node {node_id} is operator-profile control-plane data ({protected}) — generic "
            "update_node is refused; use remember_preference / confirm_preference / "
            "resolve_domain"
        )
    if status is not repo._UNSET:
        _check_status(status)
    effective = await policy.effective_node_sensitivity(session, node)

    gated = await _gate(
        session,
        principal,
        operation="update",
        sensitivity=effective,
        target_space_id=node.origin_space,
        confirm=confirm,
        op="update_node",
        target_ref={"node_id": str(nid)},
        payload={
            "patch": patch,
            "label": None if label is repo._UNSET else label,
            "status": None if status is repo._UNSET else status,
            # Carry the CAS token INTO the proposal. Applying against whatever version happens to
            # be current at APPROVAL time can never conflict — so a node edited between staging and
            # approval was silently overwritten by the stale proposal.
            "expected_version": str(_as_uuid(expected_version, "expected_version")),
        },
    )
    if gated is not None:
        return gated

    try:
        updated = await repo.update_node(
            session,
            node_id=nid,
            account_id=principal.account_id,
            expected_version=_as_uuid(expected_version, "expected_version"),
            label=label,
            properties_patch=patch,
            status=status,
        )
    except repo_errors.ConflictError as exc:
        raise Conflict(str(exc)) from exc
    except repo_errors.NotFoundError as exc:
        raise NotFound(str(exc)) from exc
    await index_node(session, updated, get_embedder())
    return {"status": "ok", "node": _node_dict(updated)}


async def delete_node(
    session: AsyncSession,
    principal: Principal,
    *,
    node_id: Any,
    expected_version: Any,
    confirm: bool = False,
) -> dict:
    access = _scoped_access(session, principal)
    nid = _as_uuid(node_id, "node_id")
    node = await access.get_node(nid)
    if node is None:
        raise NotFound(f"node {node_id} not visible")
    if await access.effective_permission(nid) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on node {node_id}")
    # Operator-profile total protection (E34): deletes are refused the same as
    # updates — retirement goes through the profile path (retire/detach).
    protected = await profile_service.protected_kind(session, nid)
    if protected is not None:
        raise PermissionDenied(
            f"node {node_id} is operator-profile control-plane data ({protected}) — generic "
            "delete_node is refused; use confirm_preference (retire/detach) or resolve_domain"
        )
    effective = await policy.effective_node_sensitivity(session, node)

    gated = await _gate(
        session,
        principal,
        operation="delete",
        sensitivity=effective,
        target_space_id=node.origin_space,
        confirm=confirm,
        op="delete_node",
        target_ref={"node_id": str(nid)},
        # Same CAS token as update: a delete approved after the node changed must CONFLICT, not
        # quietly destroy the newer version.
        payload={"expected_version": str(_as_uuid(expected_version, "expected_version"))},
    )
    if gated is not None:
        return gated

    try:
        await repo.delete_node(
            session,
            node_id=nid,
            expected_version=_as_uuid(expected_version, "expected_version"),
        )
    except repo_errors.ConflictError as exc:
        raise Conflict(str(exc)) from exc
    except repo_errors.NotFoundError as exc:
        raise NotFound(str(exc)) from exc
    return {"status": "ok", "node_id": str(nid)}


async def link(
    session: AsyncSession,
    principal: Principal,
    *,
    type: str,
    src: Any,
    dst: Any,
    properties: dict | None = None,
    confirm: bool = False,
) -> dict:
    access = _scoped_access(session, principal)
    src_id, dst_id = _as_uuid(src, "src"), _as_uuid(dst, "dst")
    if await access.get_node(src_id) is None:
        raise NotFound(f"node {src} not visible")
    if await access.get_node(dst_id) is None:
        raise NotFound(f"node {dst} not visible")
    if await access.effective_permission(src_id) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on node {src}")

    sensitive = await repo._derive_sensitive(session, type, src_id, dst_id)
    effective = "high" if sensitive else "normal"
    gated = await _gate(
        session,
        principal,
        operation="create",
        sensitivity=effective,
        target_space_id=None,
        confirm=confirm,
        op="link",
        target_ref={"src": str(src_id), "dst": str(dst_id), "type": type},
        payload={"properties": properties},
    )
    if gated is not None:
        return gated

    try:
        edge = await repo.link(
            session,
            type=type,
            src_node=src_id,
            dst_node=dst_id,
            account_id=principal.account_id,
            properties=properties,
            sensitive=sensitive,
        )
    except repo_errors.ContainerConflictError as exc:
        raise Conflict(str(exc)) from exc
    return {"status": "ok", "edge": _edge_dict(edge)}


async def unlink(session: AsyncSession, principal: Principal, *, edge_id: Any) -> dict:
    access = _scoped_access(session, principal)
    eid = _as_uuid(edge_id, "edge_id")
    edge = await session.get(Edge, eid)
    if edge is None or await access.get_node(edge.src_node) is None:
        raise NotFound(f"edge {edge_id} not visible")
    if await access.effective_permission(edge.src_node) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on edge {edge_id}")
    removed = await repo.unlink(session, edge_id=eid)
    return {"status": "ok", "removed": removed}


# --- share tools ---------------------------------------------------------


async def _share_one(
    session: AsyncSession,
    principal: Principal,
    *,
    node: Node,
    target_space_id: uuid.UUID,
    confirm: bool,
) -> dict:
    effective = await policy.effective_node_sensitivity(session, node)
    gated = await _gate(
        session,
        principal,
        operation="share",
        sensitivity=effective,
        target_space_id=target_space_id,
        confirm=confirm,
        op="share_nodes",
        target_ref={"node_id": str(node.id), "space": str(target_space_id)},
        payload={},
    )
    if gated is not None:
        return {"node_id": str(node.id), **gated}
    added = await repo.share_node(
        session, node_id=node.id, space_id=target_space_id, account_id=principal.account_id
    )
    return {"node_id": str(node.id), "status": "ok", "added": added}


async def share_nodes(
    session: AsyncSession,
    principal: Principal,
    *,
    target_space: Any,
    node_ids: list[Any],
    confirm: bool = False,
) -> dict:
    space_id = _as_uuid(target_space, "target_space")
    await _require_space_write(session, principal, space_id)
    access = _scoped_access(session, principal)

    results = []
    for raw in node_ids:
        node = await access.get_node(_as_uuid(raw, "node_id"))
        if node is None:
            results.append({"node_id": str(raw), "status": "not_found"})
            continue
        results.append(
            await _share_one(
                session, principal, node=node, target_space_id=space_id, confirm=confirm
            )
        )
    return {"target_space": str(space_id), "results": results}


async def share_subtree(
    session: AsyncSession,
    principal: Principal,
    *,
    target_space: Any,
    anchor: Any,
    confirm: bool = False,
) -> dict:
    space_id = _as_uuid(target_space, "target_space")
    await _require_space_write(session, principal, space_id)
    access = _scoped_access(session, principal)
    anchor_id = _as_uuid(anchor, "anchor")
    if await access.get_node(anchor_id) is None:
        raise NotFound(f"anchor {anchor} not visible")

    subtree = await containment_subtree(session, anchor_id)
    results = []
    for nid in subtree:
        node = await access.get_node(nid)
        if node is None:  # subtree may reach nodes outside this credential's view
            continue
        results.append(
            await _share_one(
                session, principal, node=node, target_space_id=space_id, confirm=confirm
            )
        )
    return {"target_space": str(space_id), "anchor": str(anchor_id), "results": results}


async def share_edge(
    session: AsyncSession,
    principal: Principal,
    *,
    target_space: Any,
    edge_id: Any,
    confirm: bool = False,
) -> dict:
    space_id = _as_uuid(target_space, "target_space")
    await _require_space_write(session, principal, space_id)
    eid = _as_uuid(edge_id, "edge_id")
    edge = await session.get(Edge, eid)
    if edge is None or edge.valid_to is not None:
        raise NotFound(f"edge {edge_id} not active")
    # both endpoints must already be in the target space (decisions §9)
    for end in (edge.src_node, edge.dst_node):
        present = await session.scalar(
            select(NodeSpace.node_id).where(
                NodeSpace.node_id == end, NodeSpace.space_id == space_id
            )
        )
        if present is None:
            raise PermissionDenied("both edge endpoints must already be in the target space")

    effective = "high" if edge.sensitive else "normal"
    gated = await _gate(
        session,
        principal,
        operation="share",
        sensitivity=effective,
        target_space_id=space_id,
        confirm=confirm,
        op="share_edge",
        target_ref={"edge_id": str(eid), "space": str(space_id)},
        payload={},
    )
    if gated is not None:
        return gated
    added = await repo.share_edge(
        session, edge_id=eid, space_id=space_id, account_id=principal.account_id
    )
    return {"status": "ok", "added": added}


async def unshare(
    session: AsyncSession, principal: Principal, *, node_id: Any, space: Any
) -> dict:
    """Remove a node reference from a space. Always allowed (lowers visibility)."""
    space_id = _as_uuid(space, "space")
    await _require_space_write(session, principal, space_id)
    removed = await repo.unshare_node(
        session, node_id=_as_uuid(node_id, "node_id"), space_id=space_id
    )
    return {"status": "ok", "removed": removed}


async def unshare_edge(
    session: AsyncSession, principal: Principal, *, edge_id: Any, space: Any
) -> dict:
    space_id = _as_uuid(space, "space")
    await _require_space_write(session, principal, space_id)
    removed = await repo.unshare_edge(
        session, edge_id=_as_uuid(edge_id, "edge_id"), space_id=space_id
    )
    return {"status": "ok", "removed": removed}


# --- scheduler: authoring ScheduledJob nodes (actionable layer) ----------


def _check_job_executability(
    *,
    agency: str | None,
    tools: list | None,
    writes: Any,
    delivery: dict | None,
    trigger: dict | None,
    dry_run: bool,
) -> None:
    """The D36 executability gate: this server IS the runtime that will execute the job, so
    validate against its LIVE configuration at authoring time — a job that can only run
    memory-blind must refuse to be scheduled, not fire and lie. ``dry_run`` skips only the
    enablement checks (it executes immediately, so the resident runner is not involved)."""
    from ..config import settings

    if agency == "autonomous":
        raise InvalidArgument(
            "agency 'autonomous' (the LLM-directed read-loop) is not implemented yet — "
            "declare gather steps in `tools` and use gather_then_judge"
        )
    if tools is not None:
        try:
            gather_mod.validate_gather_steps(tools)
        except gather_mod.GatherError as exc:
            raise InvalidArgument(str(exc)) from exc
    # Gate the writes fence with THE SAME parser ACT enforces with (F23) — the accepted
    # declaration and the runtime fence cannot diverge.
    try:
        parse_writes_scope(writes)
    except FenceError as exc:
        raise InvalidArgument(f"writes: {exc}") from exc
    if isinstance(trigger, dict) and trigger.get("predicate") is not None:
        # The predicate is part of the trigger contract — gate its grammar here so a job
        # cannot persist a predicate every occurrence would raise on (F21).
        try:
            scheduler_predicate.validate(trigger["predicate"])
        except ValueError as exc:
            raise InvalidArgument(f"trigger.predicate: {exc}") from exc
        if trigger.get("kind") == "one_shot":
            raise InvalidArgument(
                "one_shot + predicate is not supported yet (the runner skips it) — "
                "use a recurring trigger or drop the predicate"
            )
    if delivery is not None:
        # Shape-gate delivery (F22): the runner reads .get('target') on it pre-ACT.
        if not isinstance(delivery, dict):
            raise InvalidArgument(
                f"delivery must be an object, got {type(delivery).__name__}"
            )
        unknown = set(delivery) - {"channel", "target"}
        if unknown:
            raise InvalidArgument(f"delivery has unknown keys {sorted(unknown)}")
        for key in ("channel", "target"):
            if key in delivery and not isinstance(delivery[key], str):
                raise InvalidArgument(f"delivery.{key} must be a string")
        if not (settings.bot_enabled and settings.bot_token):
            raise InvalidArgument(
                "the job declares a delivery, but the bot channel is not available on this "
                "runtime (AM_BOT_ENABLED / AM_BOT_TOKEN) — the report would never reach anyone"
            )
    if dry_run:
        return
    if not settings.scheduler_enabled:
        raise InvalidArgument(
            "the scheduler runner is DISABLED on this runtime (AM_SCHEDULER_ENABLED) — the "
            "job would never fire; enable the scheduler first, or probe with dry_run=true"
        )
    if settings.scheduler_reasoner == "stub":
        raise InvalidArgument(
            "the scheduler reasoner on this runtime is the no-op 'stub' "
            "(AM_SCHEDULER_REASONER) — the job would run but never produce anything"
        )
    from ..scheduler.reasoning import get_reasoner

    try:
        get_reasoner()  # constructible = its key/token is actually present on this runtime
    except ValueError as exc:
        raise InvalidArgument(f"the scheduler reasoner is misconfigured: {exc}") from exc


async def create_scheduled_job(
    session: AsyncSession,
    principal: Principal,
    *,
    instruction: str,
    trigger: dict,
    space: Any = None,
    label: str | None = None,
    agency: str | None = None,
    tools: list | None = None,
    writes: Any = None,
    budget: Any = None,
    delivery: dict | None = None,
    enabled: bool = True,
    confirm: bool = False,
    dry_run: bool = False,
) -> dict:
    """Author a ScheduledJob: validate the trigger AND executability (D36), compute the first
    next_run, create the node (through the write gate). With ``dry_run=true``, nothing is
    created or scheduled: GATHER + REASON run immediately under the PERSISTED job's exact
    effective principal (the caller's account scoped to the resolved target space — not the
    caller credential's possibly-broader access) and the validated plan + gather stats are
    returned — the proving occurrence the conventions' scheduled-runtime rule requires before
    the first real one is trusted."""
    space_id = await _resolve_write_space(session, principal, space)
    node_type = await session.get(NodeType, "ScheduledJob")
    if node_type is None:
        raise InvalidArgument("the ScheduledJob node type is not registered (run migrations)")
    _check_job_executability(
        agency=agency, tools=tools, writes=writes, delivery=delivery, trigger=trigger,
        dry_run=dry_run,
    )
    for write_type in sorted(parse_writes_scope(writes)):
        # the registry is available at authoring — a fence naming an unregistered type
        # could never produce a successful write (F23)
        if await session.get(NodeType, write_type) is None:
            raise InvalidArgument(f"writes names an unregistered node type {write_type!r}")
    probe_access = Access(session, principal.account_id, scopes={space_id})
    if tools:
        # Resolve node-referencing gather args under the PERSISTED job's effective scope
        # (F28): a step pointed at an invisible node would fail every occurrence. This is
        # a point-in-time reference check; end-to-end semantic proof stays dry_run's job.
        for i, step in enumerate(tools):
            for arg in gather_mod.NODE_REF_ARGS.get(step.get("tool"), ()):
                if arg not in (step.get("args") or {}):
                    continue
                ref = step["args"][arg]
                if await probe_access.get_node(_as_uuid(ref, arg)) is None:
                    raise InvalidArgument(
                        f"gather step {i} ({step['tool']}): {arg} {ref} is not visible in "
                        "the job's target space — the read would fail on every occurrence"
                    )
    if isinstance(trigger, dict) and trigger.get("predicate") is not None:
        # Predicate reads run under the SAME effective scope at runtime (F31) — resolve
        # its node references here so a predicate gating on an invisible node is rejected,
        # not silently evaluated against ABSENT forever.
        for ref in scheduler_predicate.leaf_node_ids(trigger["predicate"]):
            if await probe_access.get_node(_as_uuid(ref, "predicate node")) is None:
                raise InvalidArgument(
                    f"trigger.predicate references node {ref}, which is not visible in "
                    "the job's target space"
                )
    try:
        properties = authoring.build_job_properties(
            instruction=instruction,
            trigger=trigger,
            agency=agency,
            tools=tools,
            writes=writes,
            budget=budget,
            delivery=delivery,
            enabled=enabled,
        )
    except authoring.TriggerError as exc:
        raise InvalidArgument(str(exc)) from exc
    try:
        # A malformed budget would make EVERY occurrence fail in GATHER after acceptance —
        # exactly the accepted-but-unexecutable class D36 exists to reject (F10).
        gather_mod.gather_budget_chars(properties)
    except gather_mod.GatherError as exc:
        raise InvalidArgument(str(exc)) from exc

    if dry_run:
        from ..scheduler.plan import PlanError, validate_final_plan
        from ..scheduler.reasoning import ReasonerError, get_reasoner

        # Probe under the PERSISTED job's exact effective principal (D18/D19): the caller's
        # account (it would be created_by) scoped to the resolved target space — NOT the
        # caller's own possibly-broader credential scope. A probe that sees more than the
        # job would see is an unsound preflight (F1, review dc694faf).
        probe = Principal(
            account_id=principal.account_id,
            credential_id=gather_mod.GATHER_CREDENTIAL_ID,
            trust="untrusted",
            scopes=[str(space_id)],
            allowed_tools=None,
        )
        context: dict = {"instruction": instruction}
        gather_stats = None
        if tools:
            context["context"], gather_stats = await gather_mod.run_steps(
                session,
                probe,
                tools,
                budget_chars=gather_mod.gather_budget_chars(properties),
            )
        try:
            reasoner = get_reasoner()
        except ValueError as exc:  # misconfigured backend = a missing capability (D36)
            raise InvalidArgument(f"the scheduler reasoner is misconfigured: {exc}") from exc
        plan: dict | None = None
        plan_error: str | None = None
        try:
            plan = validate_final_plan(await reasoner.reason(context))
        except (PlanError, ReasonerError) as exc:
            plan_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - backend/transport failure: mirror the
            # runner's reason_transport classification as a structured probe result (F11)
            plan_error = f"reasoner backend failure (reason_transport): {exc}"
        from ..config import settings

        result: dict = {
            "status": "dry_run",
            "plan": plan,
            "plan_error": plan_error,
            # A stub probe is NOT the D36 proving occurrence — say so explicitly (F16),
            # so a success on the no-op backend cannot be mistaken for proven capability.
            "reasoner": settings.scheduler_reasoner,
        }
        if settings.scheduler_reasoner == "stub":
            result["warning"] = (
                "the reasoner is the no-op 'stub': this dry_run exercises GATHER and plan "
                "validation only and does NOT prove REASON capability"
            )
        if gather_stats is not None:
            result["gather"] = gather_stats
        return result

    gated = await _gate(
        session,
        principal,
        operation="create",
        sensitivity=node_type.default_sensitivity,
        target_space_id=space_id,
        confirm=confirm,
        op="create_node",
        target_ref={"space": str(space_id), "type": "ScheduledJob"},
        payload={"label": label, "properties": properties},
    )
    if gated is not None:
        return gated

    node = await repo.create_node(
        session,
        type="ScheduledJob",
        space_id=space_id,
        account_id=principal.account_id,
        label=label or instruction[:60],
        properties=properties,
    )
    await index_node(session, node, get_embedder())
    return {"status": "ok", "node": _node_dict(node)}


async def list_scheduled_jobs(
    session: AsyncSession,
    principal: Principal,
    *,
    space: Any = None,
    include_disabled: bool = True,
) -> dict:
    """List ScheduledJob nodes visible to this credential (access-filtered)."""
    access = _scoped_access(session, principal)
    space_ids = await access.space_ids()
    if not space_ids:
        return {"jobs": []}
    visible = select(NodeSpace.node_id).where(NodeSpace.space_id.in_(space_ids)).distinct()
    stmt = (
        select(Node)
        .where(Node.id.in_(visible), Node.type == "ScheduledJob", Node.deleted_at.is_(None))
        .order_by(Node.created_at)
    )
    if space is not None:
        stmt = stmt.where(Node.origin_space == _as_uuid(space, "space"))
    jobs = []
    for node in await session.scalars(stmt):
        props = node.properties or {}
        if not include_disabled and not props.get(KEY_ENABLED):
            continue
        jobs.append(
            {
                "id": str(node.id),
                "label": node.label,
                "enabled": bool(props.get(KEY_ENABLED)),
                "kind": (props.get("trigger") or {}).get("kind"),
                "instruction": props.get("instruction"),
                "next_run": props.get("next_run"),
                "last_run": props.get("last_run"),
            }
        )
    return {"jobs": jobs}


async def set_scheduled_job_enabled(
    session: AsyncSession,
    principal: Principal,
    *,
    node_id: Any,
    enabled: bool,
    confirm: bool = False,
) -> dict:
    """Pause (enabled=false) or resume (true) a ScheduledJob by node id."""
    access = _scoped_access(session, principal)
    nid = _as_uuid(node_id, "node_id")
    node = await access.get_node(nid)
    if node is None:
        raise NotFound(f"node {node_id} not visible")
    if node.type != "ScheduledJob":
        raise InvalidArgument(f"node {node_id} is not a ScheduledJob")
    if await access.effective_permission(nid) not in _WRITE_PERMS:
        raise PermissionDenied(f"no write permission on node {node_id}")

    gated = await _gate(
        session,
        principal,
        operation="update",
        sensitivity=await policy.effective_node_sensitivity(session, node),
        target_space_id=node.origin_space,
        confirm=confirm,
        op="update_node",
        target_ref={"node_id": str(nid)},
        payload={"properties": {KEY_ENABLED: bool(enabled)}},
    )
    if gated is not None:
        return gated

    updated = await repo.update_node(
        session,
        node_id=nid,
        account_id=principal.account_id,
        expected_version=node.current_version_id,
        properties_patch={KEY_ENABLED: bool(enabled)},
    )
    await index_node(session, updated, get_embedder())
    return {"status": "ok", "node": _node_dict(updated)}


# --- dispatch table ------------------------------------------------------

async def create_review(
    session: AsyncSession,
    principal: Principal,
    *,
    slug: str,
    mode: str,
    artifact_ref: dict | None = None,
    config: dict | None = None,
    instrument: dict | None = None,
) -> dict:
    """Create a review-orchestration review + mint its two per-review tokens (trusted only).

    The MCP twin of ``POST /reviews`` (review spec §3.2/§8): the already-authenticated
    agent credential stands in for the operator's bootstrap credential, removing the
    manual token-issuance step (§16 scenario-1 operator verdict, 2026-07-09). Token
    plaintexts are returned ONCE. They are per-review isolation keys
    (``review_orchestration.ReviewToken``), not account credentials — the control-plane
    boundary (decisions §8: no spaces/accounts/memberships/credentials from the data
    plane) is untouched.
    """
    if principal.trust != "trusted":
        raise PermissionDenied("creating a review requires a trusted credential")
    name = (slug or "").strip()
    if not name:
        raise InvalidArgument("slug must be a non-empty string")
    # B.11 E-3: the operator profile is resolved from the graph HERE, at creation, and
    # frozen into the review's config. The semantic map is written "in my language, according
    # to my profile" (the operator, translated), and the profile is real data rather than a
    # figure of speech — it
    # records that jargon is expanded on first use and that a reference to a spec section
    # number is worthless to this reader. Freezing it is the same discipline as every other
    # frozen instrument of this loop: a document produced by an instrument that can change
    # underneath it is not comparable to the next one.
    scopes = None if principal.scopes is None else {
        _as_uuid(sc, "scope") for sc in principal.scopes
    }
    operator_profile = await profile_service.snapshot_for_review(
        session, account_id=principal.account_id, scopes=scopes
    )
    try:
        issued = await review_repo.create_review(
            session, slug=name, mode=mode, artifact_ref=artifact_ref, config=config,
            instrument=instrument, operator_profile=operator_profile,
        )
    except review_errors.ReviewCreationRefusedError as e:
        # The B.9 instrument gate (C-5/D-1/D-3/D-4): the refusal is a ROUTE — it names
        # the missing ground and the preparation path; surface it verbatim.
        raise InvalidArgument(e.reason) from None
    except ValueError as e:  # unknown mode — fail early with the repository's message
        raise InvalidArgument(str(e)) from None
    return {
        "status": "ok",
        "review_id": str(issued.review.id),
        "state": issued.review.state,
        "dev_token": issued.dev_token,
        "critic_token": issued.critic_token,
        "note": "tokens are shown once — write them to files, never into chat/transcripts",
    }


HANDLERS = {
    "conventions": conventions,
    "get": get,
    "search": search,
    "traverse": traverse,
    "explain": explain,
    "timeline": timeline,
    "list_spaces": list_spaces,
    "list_node_types": list_node_types,
    "list_edge_types": list_edge_types,
    "create_node_type": create_node_type,
    "create_edge_type": create_edge_type,
    "retype_node": retype_node,
    "retype_edge": retype_edge,
    "remember_fact": remember_fact,
    "remember_preference": remember_preference,
    "remember_decision": remember_decision,
    "get_operator_profile": get_operator_profile,
    "confirm_preference": confirm_preference,
    "resolve_domain": resolve_domain,
    "feedback": feedback,
    "create_node": create_node,
    "update_node": update_node,
    "delete_node": delete_node,
    "link": link,
    "unlink": unlink,
    "share_nodes": share_nodes,
    "share_subtree": share_subtree,
    "share_edge": share_edge,
    "unshare": unshare,
    "unshare_edge": unshare_edge,
    "create_scheduled_job": create_scheduled_job,
    "list_scheduled_jobs": list_scheduled_jobs,
    "set_scheduled_job_enabled": set_scheduled_job_enabled,
    "create_review": create_review,
}
