# SPDX-License-Identifier: Apache-2.0
"""Deterministic trigger-predicate evaluator (D30).

A job's predicate gates whether it fires. It MUST be reproducible and auditable, so it is a
small, closed BOOLEAN over structured graph state — never a semantic search (D30). Each leaf
reads one node's property by id and compares it; composition is ``all`` / ``any`` / ``not``.
No loops, no code, bounded cost (one ``get_node`` per leaf).

Predicate grammar (JSON):
    composite : {"all": [pred, ...]} | {"any": [pred, ...]} | {"not": pred}
    leaf      : {"node": "<uuid>", "path": "a.b.c",
                 "op": "eq|ne|lt|le|gt|ge|exists|absent", "value": <literal>?}

A missing node or path → the value is ABSENT: ``exists`` → False, ``absent`` → True,
comparisons → False. An empty/absent predicate → True (no gate). A malformed predicate
raises ``ValueError`` — the caller fails closed (does not fire).
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..access import Access

_MISSING: Any = object()

_COMPARATORS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
}


def _resolve_path(props: dict, path: str) -> Any:
    """Walk a dotted path into the properties dict; ``_MISSING`` if any hop is absent."""
    current: Any = props
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def validate(predicate) -> None:
    """STATIC validation of the predicate grammar (no DB) — the D36 authoring gate's half:
    reject at create time exactly what ``evaluate`` would raise on at every occurrence
    (F21, review dc694faf). Raises ``ValueError``."""
    if predicate is None or predicate == {}:
        return
    if not isinstance(predicate, dict):
        raise ValueError(f"predicate must be an object, got {type(predicate).__name__}")
    composites = [k for k in ("all", "any", "not") if k in predicate]
    if composites:
        if len(predicate) != 1:
            raise ValueError(f"a composite predicate holds exactly one key, got {predicate!r}")
        key = composites[0]
        if key == "not":
            validate(predicate["not"])
            return
        clauses = predicate[key]
        if not isinstance(clauses, list) or not clauses:
            raise ValueError(f"predicate {key!r} needs a non-empty list of clauses")
        for clause in clauses:
            validate(clause)
        return
    # leaf
    unknown = set(predicate) - {"node", "path", "op", "value"}
    if unknown:
        raise ValueError(f"predicate leaf has unknown keys {sorted(unknown)}")
    op = predicate.get("op")
    if op not in ("exists", "absent", *_COMPARATORS):
        raise ValueError(f"unknown predicate op: {op!r}")
    try:
        uuid.UUID(str(predicate.get("node")))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"predicate leaf has a bad node id: {predicate.get('node')!r}") from exc
    if "path" in predicate and not isinstance(predicate["path"], str):
        raise ValueError("predicate leaf `path` must be a string")


def leaf_node_ids(predicate) -> list[str]:
    """All node ids referenced by a (validated) predicate — for the authoring gate to
    resolve under the job's effective scope (F31, review dc694faf)."""
    if not isinstance(predicate, dict) or not predicate:
        return []
    if "not" in predicate:
        return leaf_node_ids(predicate["not"])
    for key in ("all", "any"):
        if key in predicate:
            out: list[str] = []
            for clause in predicate[key]:
                out.extend(leaf_node_ids(clause))
            return out
    return [str(predicate["node"])] if "node" in predicate else []


async def evaluate(session: AsyncSession, predicate: dict | None, access: Access) -> bool:
    """Evaluate a predicate against current graph state, READING THROUGH the job's
    effective ``access`` — never unscoped (F31, review dc694faf): a node outside the
    job's account/space scope reads as ABSENT, same isolation rule as GATHER (D18/D19).
    See module docstring for grammar."""
    if not predicate:
        return True
    if "all" in predicate:
        for clause in predicate["all"]:
            if not await evaluate(session, clause, access):
                return False
        return True
    if "any" in predicate:
        for clause in predicate["any"]:
            if await evaluate(session, clause, access):
                return True
        return False
    if "not" in predicate:
        return not await evaluate(session, predicate["not"], access)
    return await _eval_leaf(session, predicate, access)


async def _eval_leaf(session: AsyncSession, leaf: dict, access: Access) -> bool:
    op = leaf.get("op")
    node_ref = leaf.get("node")
    if op is None or node_ref is None:
        raise ValueError(f"malformed predicate leaf: {leaf!r}")
    try:
        node_id = uuid.UUID(str(node_ref))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"predicate leaf has a bad node id: {node_ref!r}") from exc

    node = await access.get_node(node_id)
    actual = (
        _MISSING if node is None else _resolve_path(node.properties or {}, leaf.get("path", ""))
    )

    if op == "exists":
        return actual is not _MISSING
    if op == "absent":
        return actual is _MISSING
    comparator = _COMPARATORS.get(op)
    if comparator is None:
        raise ValueError(f"unknown predicate op: {op!r}")
    if actual is _MISSING:
        return False
    try:
        return bool(comparator(actual, leaf.get("value")))
    except TypeError:
        return False  # incomparable types → false, never a crash
