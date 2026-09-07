# SPDX-License-Identifier: Apache-2.0
"""GATHER: run a job's declared read-steps and assemble the REASON context (D33-D35).

D33: gather reuses the MCP read-tool HANDLERS in-process — no parallel query surface; reads
stay access-filtered exactly like any client's, under the job's EFFECTIVE principal (D18/D19:
the job's creating account, scoped to the job's origin space — never the scheduler's service
identity). D34: the job's `tools` field is an ordered list of executable steps
``{"tool": <registry name>, "args": {...}}``; composites cover runtime-dependent loops so job
properties never carry a foreach/templating DSL. D35: the assembled context is capped by
``budget.gather_chars`` — overflow drops whole blocks from the end and says so.

Fail closed: any invalid declaration or failing read raises ``GatherError`` — the runner must
not REASON over a partial/hallucinated picture (D21: better no report than a wrong one).
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.resolver import Principal
from .job import KEY_BUDGET, KEY_TOOLS, ScheduledJob, iso

logger = logging.getLogger(__name__)

DEFAULT_GATHER_CHARS = 24_000
_TRUNCATION_NOTE = "[gather truncated: {n} step result(s) dropped to fit the context budget]"

# The gather principal is in-process (no credential row); a fixed sentinel id keeps the
# Principal shape intact and greppable in any log line that prints it.
GATHER_CREDENTIAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")

# Gather-step args that reference a concrete node id — the authoring gate resolves these
# under the job's effective principal so a job pointed at an invisible/nonexistent node is
# rejected at create, not discovered by every failing occurrence (F28, review dc694faf).
NODE_REF_ARGS: dict[str, tuple[str, ...]] = {
    "get": ("node_id",),
    "explain": ("node_id",),
    "traverse": ("start",),
    "timeline": ("entity_id",),
}

# Composite defaults (bounded so one step cannot silently sweep the whole graph).
_RECENT_DEFAULT_HOURS = 24
_RECENT_DEFAULT_NEIGHBORS = 5
_RECENT_DEFAULT_LIMIT = 50
_RECENT_MAX_LIMIT = 200

# --- hygiene acks (D38, Decision 964a2cbb) ---------------------------------
#
# An ack ("this node MAY be big", "this pair are NOT duplicates") is an operator judgement
# recorded as an EXTERNAL node: it `references` its target(s) and PINS the target's version
# id as of the approval. External because an ack stored INSIDE the node would itself bump
# the version it pins — self-annulling at write time.
#
# The comparison lives in CODE, not in the job prompt (Incident a7a0583b / D36: a rule with
# no mechanism gets broken even by its own authors). The model receives a ready boolean.
#
# FAIL CLOSED, both ways: no ack = no approval (the node simply resurfaces — forgetting to
# ack is safe, noisy); and an ack whose pin no longer matches the target's CURRENT version
# is ignored — someone touched the node, so the operator gets asked again.
ACK_EDGE = "references"
ACK_KIND_KEY = "ack_kind"
ACK_OVERSIZED = "oversized_ok"  # pin: acked_version_id (exactly one target)
ACK_NOT_DUPLICATES = "not_duplicates"  # pin: acked_version_ids {node_id: version_id} (exactly 2)
ACK_PIN_KEY = "acked_version_id"
ACK_PINS_KEY = "acked_version_ids"
ACK_KINDS = (ACK_OVERSIZED, ACK_NOT_DUPLICATES)

# The judgement is the OPERATOR's; the agent is only the scribe. The schema says an ack must
# record that — so gather ENFORCES it (F5): an ack that does not claim operator judgement is
# not an approval, however well-formed it looks. Without the check the sentence would be a
# rule with no mechanism, and this project already has an incident about exactly that (D36).
# It does not (and cannot) prove the operator really said it — it makes the claim mandatory,
# explicit and auditable, so an ack-shaped note nobody vouched for grants nothing.
ACK_JUDGED_BY_KEY = "judged_by"
ACK_JUDGED_BY_OPERATOR = "operator"
ACK_SCRIBE_KEY = "scribe"  # who wrote the operator's judgement down — required, not decorative
ACK_NODE_TYPE = "Note"  # the ack is an ordinary Note (no new node type — conventions §11)
ACK_PAIR_SIZE = 2


class GatherError(ValueError):
    """A gather declaration or execution the runner must not silently paper over."""


def effective_principal(job_node) -> Principal:
    """The job's effective ACCESS principal (D18/D19): its creating account, scoped to its
    origin space. Reads run under this — never under the scheduler's service account.

    FAIL CLOSED on a missing origin_space (F9, review dc694faf): ``scopes=None`` would mean
    the creator's FULL account membership to the Access layer — a malformed job must get no
    reads at all, never broader reads."""
    if job_node.created_by is None:
        raise GatherError(f"job {job_node.id} has no created_by — cannot derive read scope")
    if job_node.origin_space is None:
        raise GatherError(
            f"job {job_node.id} has no origin_space — refusing account-wide reads"
        )
    return Principal(
        account_id=job_node.created_by,
        credential_id=GATHER_CREDENTIAL_ID,
        trust="untrusted",  # reads don't consult trust; least privilege on principle
        scopes=[str(job_node.origin_space)],
        allowed_tools=None,  # the gather registry below IS the tool fence for this principal
    )


def _looks_like_ack(node: dict) -> bool:
    """Carries an ack marker — a CLAIM to be an ack, not yet a verified one (F2)."""
    props = node.get("properties") or {}
    return isinstance(props, dict) and props.get(ACK_KIND_KEY) in ACK_KINDS


def _ack_pin_matches(pin, current_version_id) -> bool:
    """A pin honours a node only if both sides are present AND equal. Two missing values are
    NOT a match — a node with no current version must never read as approved."""
    return bool(pin) and bool(current_version_id) and str(pin) == str(current_version_id)


def _ack_pins(node: dict) -> dict[str, str] | None:
    """The {node_id: version_id} an ack claims to pin, or None if the record is malformed.

    ``oversized_ok`` pins ONE version and names no node — the node it pins is the one it
    references, so the caller supplies the id. ``not_duplicates`` names its pair explicitly.
    A shape that does not parse is not an ack: it grants nothing and (F2) stays visible to
    the survey rather than being silently suppressed.
    """
    props = node.get("properties") or {}
    if not isinstance(props, dict):
        return None
    kind = props.get(ACK_KIND_KEY)
    if kind == ACK_OVERSIZED:
        pin = props.get(ACK_PIN_KEY)
        return {} if isinstance(pin, str) and pin else None  # target id filled in by the caller
    if kind == ACK_NOT_DUPLICATES:
        pins = props.get(ACK_PINS_KEY)
        # EXACTLY a pair (F4): a three-way ack would suppress several duplicate pairs at once
        # off a single approval the operator never gave in that shape.
        if not isinstance(pins, dict) or len(pins) != ACK_PAIR_SIZE:
            return None
        if not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in pins.items()):
            return None
        return dict(pins)
    return None


async def _ack_targets(session, principal, ack_id: str) -> set[str]:
    """The node ids an ack actually `references` (its outgoing edges)."""
    from ..mcp import tools as mcp_tools

    walked = await mcp_tools.traverse(
        session, principal, start=ack_id, edges=[ACK_EDGE], direction="out", depth=1
    )
    return {e["dst"] for e in walked["edges"] if e["src"] == str(ack_id)}


async def _is_valid_ack(session, principal, node: dict) -> bool:
    """A STRUCTURALLY valid ack: a parsable record whose references match what it pins (F1/F2).

    The full contract: an ack is a ``Note`` (F7 — the type the operator's schema approved, not
    "any node shaped like one"); it claims OPERATOR judgement and names the SCRIBE who wrote it
    down (F5/F8 — the schema promises both, so both are required); an `oversized_ok` ack
    references exactly one node; a `not_duplicates` ack references exactly the pair it pins.
    Anything else is a broken record, not an approval — and a broken record must never be able
    to hide a node from the survey.
    """
    if not _looks_like_ack(node):
        return False
    if node.get("type") != ACK_NODE_TYPE:
        return False  # a content node shaped like an ack is not an ack (F7)
    props = node.get("properties") or {}
    if props.get(ACK_JUDGED_BY_KEY) != ACK_JUDGED_BY_OPERATOR:
        return False  # the judgement is the operator's, or it is not an approval (F5)
    scribe = props.get(ACK_SCRIBE_KEY)
    if not isinstance(scribe, str) or not scribe.strip():
        return False  # who wrote it down is part of the promised provenance (F8)
    pins = _ack_pins(node)
    if pins is None:
        return False
    targets = await _ack_targets(session, principal, node["id"])
    kind = (node.get("properties") or {}).get(ACK_KIND_KEY)
    if kind == ACK_OVERSIZED:
        return len(targets) == 1
    return targets == set(pins)  # not_duplicates: referenced set == pinned set, exactly


async def _incoming_acks(session, principal, node_id: str) -> list[dict]:
    """Ack nodes that `references` this node and are still `current`. Validity (F1/F2) is
    checked by the caller, which also needs the parsed record."""
    from ..mcp import tools as mcp_tools

    walked = await mcp_tools.traverse(
        session, principal, start=node_id, edges=[ACK_EDGE], direction="in", depth=1
    )
    by_id = {n["id"]: n for n in walked["nodes"]}
    acks = []
    for edge in walked["edges"]:
        if edge["dst"] != str(node_id):
            continue
        candidate = by_id.get(edge["src"])
        if candidate is None or candidate.get("status") != "current":
            continue
        if not _looks_like_ack(candidate):
            continue
        acks.append(candidate)
    return acks


async def _current_version(session, principal, node_id: str, cache: dict) -> str | None:
    from ..mcp import tools as mcp_tools

    if node_id not in cache:
        try:
            other = await mcp_tools.get(session, principal, node_id=node_id)
        except Exception:
            cache[node_id] = None  # invisible or gone — an unverifiable ack does not hold
        else:
            cache[node_id] = other.get("current_version_id")
    return cache[node_id]


async def _ack_flags(
    session, principal, node: dict, *, version_of: dict[str, str | None]
) -> tuple[bool, list[str]]:
    """(oversized_ack, not_duplicate_of) for one changed node — computed by CODE (D38).

    ``version_of`` caches each node's current version id across the whole gather run, so the
    pair-ack's second target is fetched at most once per run.
    """
    node_id = node["id"]
    current = node.get("current_version_id")
    version_of[node_id] = current

    oversized = False
    not_duplicate_of: list[str] = []
    for ack in await _incoming_acks(session, principal, node_id):
        if not await _is_valid_ack(session, principal, ack):
            continue  # a broken ack record grants nothing (F1/F2)
        props = ack.get("properties") or {}
        kind = props.get(ACK_KIND_KEY)
        if kind == ACK_OVERSIZED:
            # Valid ⇒ this node is the ack's ONLY referenced target, so the single pin is
            # unambiguously about it.
            if _ack_pin_matches(props.get(ACK_PIN_KEY), current):
                oversized = True
            continue
        pins = _ack_pins(ack) or {}
        if node_id not in pins or not _ack_pin_matches(pins[node_id], current):
            continue  # not about this node, or this side moved since the approval
        peers = [nid for nid in pins if nid != node_id]
        held = True
        for peer_id in peers:
            peer_version = await _current_version(session, principal, peer_id, version_of)
            if not _ack_pin_matches(pins[peer_id], peer_version):
                held = False  # a pinned side moved (or vanished) — the pair must be re-judged
                break
        if held:
            not_duplicate_of.extend(peers)
    return oversized, sorted(set(not_duplicate_of))


async def _recent_nodes_context(
    session: AsyncSession,
    principal: Principal,
    *,
    hours: int = _RECENT_DEFAULT_HOURS,
    neighbors_k: int = _RECENT_DEFAULT_NEIGHBORS,
    include_properties: bool = True,
    limit: int = _RECENT_DEFAULT_LIMIT,
) -> dict:
    """Composite (D34): what changed in the last ``hours`` + each changed node's semantic
    neighborhood. timeline(since) → get each changed node → top-k `search` neighbors per node.
    General-purpose (briefings, maintenance surveys); the loop lives in code, not in the LLM
    and not in a job-property DSL.

    Each entry also carries the node's ``current_version_id`` and the two ack booleans a
    hygiene survey needs — ``oversized_ack`` and ``not_duplicate_of`` (D38): version pins are
    compared HERE, so the prompt only has to honour a ready answer. STRUCTURALLY VALID ack
    nodes are left out of ``changed`` — they are records about the graph, not content in it;
    an ack-like node whose record does not parse stays visible and grants nothing (F1/F2)."""
    from ..mcp import tools as mcp_tools

    since = datetime.now(UTC) - timedelta(hours=int(hours))
    events = (
        await mcp_tools.timeline(
            session,
            principal,
            since=iso(since),
            limit=min(int(limit), _RECENT_MAX_LIMIT),
        )
    )["events"]

    changed: list[dict] = []
    seen: set[str] = set()
    version_of: dict[str, str | None] = {}
    for event in events:
        node_id = event["node_id"]
        if node_id in seen:
            continue
        seen.add(node_id)
        node = await mcp_tools.get(session, principal, node_id=node_id)
        if await _is_valid_ack(session, principal, node):
            continue  # a real ack is bookkeeping about the graph, not content in it
        props = node.get("properties") or {}
        oversized_ack, not_duplicate_of = await _ack_flags(
            session, principal, node, version_of=version_of
        )
        entry = {
            "id": node_id,
            "type": node.get("type"),
            "label": node.get("label"),
            "status": node.get("status"),
            "changed_at": event.get("created_at"),
            "current_version_id": node.get("current_version_id"),
            "properties_chars": len(json.dumps(props, ensure_ascii=False)),
            # Operator approvals, resolved in code (D38): honour them, don't re-derive them.
            "oversized_ack": oversized_ack,
            "not_duplicate_of": not_duplicate_of,
        }
        if include_properties:
            entry["properties"] = props
        if int(neighbors_k) > 0 and node.get("label"):
            hits = (
                await mcp_tools.search(
                    session, principal, query=node["label"], limit=int(neighbors_k) + 1
                )
            )["results"]
            entry["semantic_neighbors"] = [
                {"id": h["id"], "type": h["type"], "label": h["label"], "status": h["status"]}
                for h in hits
                if h["id"] != node_id
            ][: int(neighbors_k)]
        changed.append(entry)
    return {"since": iso(since), "changed": changed}


# --- per-argument VALUE validators (F17, review dc694faf): the gate must reject at
# authoring what the handler would reject at every occurrence -----------------------


def _v_uuid(name):
    def check(v):
        try:
            uuid.UUID(str(v))
        except (ValueError, TypeError, AttributeError):
            raise GatherError(f"{name} must be a UUID, got {v!r}") from None

    return check


def _v_int(name, minimum=None):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise GatherError(f"{name} must be an integer, got {v!r}") from None
        if minimum is not None and v < minimum:
            raise GatherError(f"{name} must be >= {minimum}, got {v}")

    return check


def _v_bool(name):
    def check(v):
        if not isinstance(v, bool):
            raise GatherError(f"{name} must be a boolean, got {v!r}")

    return check


def _v_str(name):
    def check(v):
        if not isinstance(v, str):
            raise GatherError(f"{name} must be a string, got {v!r}")

    return check


def _v_dict(name):
    def check(v):
        if not isinstance(v, dict):
            raise GatherError(f"{name} must be an object, got {v!r}")

    return check


def _v_str_list(name):
    def check(v):
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise GatherError(f"{name} must be a list of strings, got {v!r}")

    return check


def _v_enum(name, values):
    def check(v):
        if v not in values:
            raise GatherError(f"{name} must be one of {sorted(values)}, got {v!r}")

    return check


def _v_iso(name):
    def check(v):
        try:
            datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise GatherError(f"{name} must be an ISO datetime, got {v!r}") from None

    return check


def _validators() -> dict[str, dict]:
    return {
        "get": {"node_id": _v_uuid("node_id")},
        "search": {
            "query": _v_str("query"),
            "filters": _v_dict("filters"),
            "limit": _v_int("limit", minimum=1),
        },
        "traverse": {
            "start": _v_uuid("start"),
            "edges": _v_str_list("edges"),
            "direction": _v_enum("direction", ("in", "out", "both")),
            "depth": _v_int("depth", minimum=1),
        },
        "timeline": {
            "entity_id": _v_uuid("entity_id"),
            "since": _v_iso("since"),
            "limit": _v_int("limit", minimum=1),
        },
        "explain": {"node_id": _v_uuid("node_id")},
        "recent_nodes_context": {
            "hours": _v_int("hours", minimum=1),
            "neighbors_k": _v_int("neighbors_k", minimum=0),
            "include_properties": _v_bool("include_properties"),
            "limit": _v_int("limit", minimum=1),
        },
    }


def _registry() -> dict[str, tuple]:
    """name -> (async handler(session, principal, **args), allowed arg names). Read-only
    handlers ONLY — write/share/authoring tools are absent by construction (D33).

    A composite (e.g. ``recent_nodes_context``) is an ATOMIC allowlist capability: declaring
    it does not require — or implicitly grant — the standalone entries its fixed code calls.
    Enforcement lives at the principal/access layer, not the tool-name layer: every inner read
    runs under the same effective principal, so a composite cannot see anything the job could
    not see by declaring the primitives itself (D34; F3, review dc694faf)."""
    from ..mcp import tools as mcp_tools

    # name -> (handler, allowed args, REQUIRED args) — required per the handler contract,
    # gated at authoring so a job cannot persist a step every occurrence would die on (F14).
    return {
        "get": (mcp_tools.get, frozenset({"node_id"}), frozenset({"node_id"})),
        "search": (mcp_tools.search, frozenset({"query", "filters", "limit"}), frozenset()),
        "traverse": (
            mcp_tools.traverse,
            frozenset({"start", "edges", "direction", "depth"}),
            frozenset({"start"}),
        ),
        "timeline": (mcp_tools.timeline, frozenset({"entity_id", "since", "limit"}), frozenset()),
        "explain": (mcp_tools.explain, frozenset({"node_id"}), frozenset({"node_id"})),
        "recent_nodes_context": (
            _recent_nodes_context,
            frozenset({"hours", "neighbors_k", "include_properties", "limit"}),
            frozenset(),
        ),
    }


def validate_gather_steps(steps) -> None:
    """Validate a `tools` declaration (used by the D36 authoring gate AND re-checked by the
    runner — the node is data, not trusted config). Raises ``GatherError``."""
    if not isinstance(steps, list):
        raise GatherError("`tools` must be a list of gather steps")
    registry = _registry()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise GatherError(f"gather step {i} must be an object {{tool, args}}")
        name = step.get("tool")
        if name not in registry:
            raise GatherError(
                f"gather step {i}: unknown tool {name!r} (available: {sorted(registry)})"
            )
        # Explicit type check even for falsy values — `[] or {}` would silently swallow a
        # wrong-typed args and defer the failure to every scheduled occurrence (F14).
        args = step.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise GatherError(f"gather step {i} ({name}): args must be an object")
        _handler, allowed, required = registry[name]
        unknown = set(args) - allowed
        if unknown:
            raise GatherError(
                f"gather step {i} ({name}): unknown args {sorted(unknown)} "
                f"(allowed: {sorted(allowed)})"
            )
        missing = required - set(args)
        if missing:
            raise GatherError(
                f"gather step {i} ({name}): missing required args {sorted(missing)}"
            )
        checks = _validators()[name]
        for arg, value in args.items():
            try:
                checks[arg](value)
            except GatherError as exc:
                raise GatherError(f"gather step {i} ({name}): {exc}") from None
        extra = set(step) - {"tool", "args"}
        if extra:
            raise GatherError(f"gather step {i}: unknown keys {sorted(extra)}")


def gather_budget_chars(props: dict) -> int:
    budget = props.get(KEY_BUDGET)
    if budget is None:
        budget = {}
    if not isinstance(budget, dict):
        # A truthy non-object budget must be a structured failure, not an AttributeError
        # escaping past the claim (F20, review dc694faf).
        raise GatherError(f"budget must be an object, got {type(budget).__name__}")
    try:
        value = int(budget.get("gather_chars", DEFAULT_GATHER_CHARS))
    except (TypeError, ValueError) as exc:
        raise GatherError(f"budget.gather_chars is not an integer: {budget!r}") from exc
    return max(1, value)


async def run_steps(
    session: AsyncSession,
    principal: Principal,
    steps: list,
    *,
    budget_chars: int = DEFAULT_GATHER_CHARS,
) -> tuple[str, dict]:
    """Execute validated gather steps in declared order; return ``(context_text, stats)``.

    Truncation (D35) drops WHOLE step results from the end (never mid-block) and appends an
    explicit marker so REASON knows the picture is partial."""
    validate_gather_steps(steps)
    registry = _registry()
    blocks: list[str] = []
    for i, step in enumerate(steps):
        name = step["tool"]
        args = step.get("args")
        if args is None:
            args = {}
        handler = registry[name][0]
        try:
            result = await handler(session, principal, **args)
        except GatherError:
            raise
        except Exception as exc:
            # Fail closed with the step named: a partial context must not reach REASON.
            raise GatherError(f"gather step {i} ({name}) failed: {exc}") from exc
        rendered = json.dumps(result, ensure_ascii=False, default=str)
        header = f"## gather step {i}: {name} {json.dumps(args, ensure_ascii=False)}"
        blocks.append(f"{header}\n{rendered}")

    kept: list[str] = []
    used = 0
    for block in blocks:
        cost = len(block) + 2  # the joining blank line
        if used + cost > budget_chars:
            break
        kept.append(block)
        used += cost
    dropped = len(blocks) - len(kept)
    if dropped:
        kept.append(_TRUNCATION_NOTE.format(n=dropped))
    text = "\n\n".join(kept)
    stats = {
        "steps": len(blocks),
        "chars": len(text),
        "budget_chars": budget_chars,
        "dropped_blocks": dropped,
    }
    return text, stats


async def run_gather(session: AsyncSession, *, job: ScheduledJob, node) -> tuple[str, dict]:
    """The runner's entry: execute the job's declared steps under its effective principal."""
    steps = job.props.get(KEY_TOOLS) or []
    return await run_steps(
        session,
        effective_principal(node),
        steps,
        budget_chars=gather_budget_chars(job.props),
    )
