# SPDX-License-Identifier: Apache-2.0
"""Execute a validated, keyed action list within the job's fence (D8/D11/D21).

Actions are GRAPH operations for now: `deliver`/`elicit` land as `Delivery` nodes the bot
reads and sends later (D3 — the graph is the coordination substrate; delivery is decoupled
from the bot); `writes` are graph creates in the job's declared writes-scope. There are no
external side-effects yet, so the whole occurrence is atomic within the tick's transaction
(a mid-tick crash rolls back cleanly — the D25 before-act journal / recovery watchdog become
load-bearing only once external actions exist).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import Node
from ..repository import graph
from .job import ScheduledJob
from .plan import Action

DELIVERY_TYPE = "Delivery"


class FenceError(ValueError):
    """A plan action outside the job's declared fence (fail closed: execute nothing)."""


def parse_writes_scope(writes) -> set[str]:
    """THE canonical `writes`-fence parser (F23, review dc694faf) — shared by the D36
    authoring gate and ACT, so the fence ENFORCED at runtime is always exactly the fence
    DECLARED at authoring; a malformed declaration is an error on both sides, never a
    silent empty/garbled fence. Canonical forms: a list of non-empty node-type strings,
    or ``{"types": [...]}``. Absent/empty → no writes (least privilege)."""
    if writes is None:
        return set()
    if isinstance(writes, dict):
        unknown = set(writes) - {"types"}
        if unknown:
            raise FenceError(f"writes has unknown keys {sorted(unknown)}")
        writes = writes.get("types")
        if writes is None:
            return set()
    if not isinstance(writes, list):
        raise FenceError(
            "writes must be a list of node types (or {'types': [...]}), got "
            f"{type(writes).__name__}"
        )
    for entry in writes:
        if not isinstance(entry, str) or not entry:
            raise FenceError(f"writes entries must be non-empty strings, got {entry!r}")
    return set(writes)


def _allowed_write_types(job: ScheduledJob) -> set[str]:
    """The node types this job may create — its declared `writes` scope, via the shared
    canonical parser. Default: none (least-privilege)."""
    return parse_writes_scope(job.props.get("writes"))


async def execute_actions(
    session: AsyncSession,
    *,
    job: ScheduledJob,
    actions: list[Action],
    account_id: uuid.UUID,
    space_id: uuid.UUID,
    delivery_target: str | None,
) -> list[dict]:
    """Execute the canonical actions. Fence-checks all writes FIRST (so a violation rejects the
    whole plan without partial writes), then performs them. Returns per-action summaries."""
    allowed = _allowed_write_types(job)
    update_targets: dict[str, Node] = {}
    for action in actions:
        if action.kind != "write":
            continue
        if action.payload.get("op") == "update":
            update_targets[action.key] = await _fence_update_target(
                session, action.payload, allowed=allowed, space_id=space_id
            )
        elif action.payload.get("type") not in allowed:
            raise FenceError(
                f"write type {action.payload.get('type')!r} is outside the job's writes scope "
                f"{sorted(allowed)}"
            )

    executed: list[dict] = []
    for action in actions:
        if action.kind == "deliver":
            await _record_delivery(
                session,
                account_id=account_id,
                space_id=space_id,
                key=action.key,
                text=action.payload.get("text", ""),
                target=delivery_target,
                kind="message",
            )
        elif action.kind == "elicit":
            await _record_delivery(
                session,
                account_id=account_id,
                space_id=space_id,
                key=action.key,
                text=action.payload.get("question", ""),
                target=delivery_target,
                kind="question",
                extra={
                    "target_node": action.payload.get("target_node"),
                    "target_field": action.payload.get("target_field"),
                    "default": action.payload.get("default"),
                },
            )
        elif action.kind == "write":
            if action.payload.get("op") == "update":
                node = update_targets[action.key]
                await graph.update_node(
                    session,
                    node_id=node.id,
                    account_id=account_id,
                    expected_version=node.current_version_id,
                    properties_patch=action.payload.get("patch") or {},
                )
            else:
                await graph.create_node(
                    session,
                    type=action.payload["type"],
                    space_id=space_id,
                    account_id=account_id,
                    label=action.payload.get("label"),
                    properties=action.payload.get("properties") or {},
                )
        executed.append({"key": action.key, "kind": action.kind})
    return executed


async def _fence_update_target(
    session: AsyncSession, payload: dict, *, allowed: set[str], space_id: uuid.UUID
) -> Node:
    """Load + fence an `update` target: it must exist, live in the job's space, and be an allowed
    type. Loaded in the fence phase so a violation rejects the whole plan before any write."""
    raw = payload.get("node_id")
    node = await graph.get_node(session, uuid.UUID(str(raw))) if raw else None
    if node is None or node.deleted_at is not None:
        raise FenceError(f"update target {raw!r} not found")
    if node.origin_space != space_id:
        raise FenceError(f"update target {raw} is outside the job's space")
    if node.type not in allowed:
        raise FenceError(
            f"update target type {node.type!r} is outside the writes scope {sorted(allowed)}"
        )
    return node


async def _record_delivery(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    space_id: uuid.UUID,
    key: str,
    text: str,
    target: str | None,
    kind: str,
    extra: dict | None = None,
) -> None:
    props = {"text": text, "kind": kind, "target": target, "key": key, "status": "pending"}
    if extra:
        props.update({k: v for k, v in extra.items() if v is not None})
    await graph.create_node(
        session,
        type=DELIVERY_TYPE,
        space_id=space_id,
        account_id=account_id,
        label=text[:60] or kind,
        properties=props,
    )
