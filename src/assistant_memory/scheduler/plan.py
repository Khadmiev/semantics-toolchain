# SPDX-License-Identifier: Apache-2.0
"""Validate a reasoner output against the §1 ``final_plan`` contract and normalize it into a
canonical, keyed action list (``deliver`` → ``writes[]`` → ``elicit``).

Simple mode only for now: ``read_request`` (the autonomous read-loop) is rejected here and
handled in a later slice. The canonical order + per-action keys are what make the run
journalable and the recovery diff stable (D24/D25).
"""

from dataclasses import dataclass

# Exactly the keys the REASON prompt advertises (OQ10: prompt and validator move in the
# same commit). `reads_used` returns here WITH a prompt mention when the autonomous
# read-loop slice lands — until then accepting it would be an unadvertised key.
_ALLOWED_TOP = {"type", "deliver", "writes", "elicit", "rationale_summary"}
# create + update; share/unshare/delete stay forbidden in a plan (D27: irreversible / out of band).
_ALLOWED_WRITE_OPS = {"create", "update"}
# Per-op write-object key whitelists (OQ10 F5: the prompt says "no other keys anywhere" —
# the validator must make that true).
_ALLOWED_WRITE_KEYS = {
    "create": {"op", "type", "label", "properties"},
    "update": {"op", "type", "node_id", "patch"},
}


class PlanError(ValueError):
    """A reasoner output that violates the §1 plan contract (fail closed: do not execute)."""


@dataclass(frozen=True)
class Action:
    key: str
    kind: str  # "deliver" | "write" | "elicit"
    payload: dict


def validate_final_plan(output: dict) -> dict:
    """Validate a reasoner output as a §1 ``final_plan``. Raises ``PlanError`` on anything off."""
    if not isinstance(output, dict):
        raise PlanError("reasoner output is not an object")
    kind = output.get("type")
    if kind == "read_request":
        raise PlanError("read_request (the autonomous read-loop) is not supported in this slice")
    if kind != "final_plan":
        raise PlanError(f"output.type must be 'final_plan', got {kind!r}")
    if "act" in output:
        raise PlanError("the `act` flag was removed; an empty plan is the no-op signal")
    unknown = set(output) - _ALLOWED_TOP
    if unknown:
        raise PlanError(f"unknown plan fields: {sorted(unknown)}")

    deliver = output.get("deliver")
    if deliver is not None and (not isinstance(deliver, dict) or "text" not in deliver):
        raise PlanError("deliver must be an object with `text`")
    if deliver is not None:
        if not isinstance(deliver.get("text"), str):
            raise PlanError("deliver.text must be a string")
        # The runner OWNS delivery routing (D18/D22 immutable target): recipient/routing
        # keys in a plan are rejected structurally, not just forbidden in the prompt
        # (F2, review 2f924059). `urgent` is the only routing-adjacent flag allowed.
        unknown = set(deliver) - {"text", "urgent"}
        if unknown:
            raise PlanError(f"deliver has unknown keys {sorted(unknown)} (allowed: text, urgent)")
        if "urgent" in deliver and not isinstance(deliver["urgent"], bool):
            raise PlanError("deliver.urgent must be a boolean")

    # No `or []` normalization (F18): a falsy non-list (`{}`, `""`) is malformed reasoner
    # output and must fail closed, not masquerade as a valid empty decision.
    writes = output.get("writes")
    if writes is None:
        writes = []
    if not isinstance(writes, list):
        raise PlanError("writes must be a list")
    for write in writes:
        if not isinstance(write, dict):
            raise PlanError("each write must be an object")
        op = write.get("op")
        if op in ("share", "unshare", "delete"):
            raise PlanError(
                f"write op {op!r} is forbidden in a plan (D27: irreversible / out of band)"
            )
        if op not in _ALLOWED_WRITE_OPS:
            raise PlanError(f"write op must be one of {sorted(_ALLOWED_WRITE_OPS)}, got {op!r}")
        if not isinstance(write.get("type"), str) or not write["type"]:
            raise PlanError("each write needs a non-empty string node `type`")
        if "label" in write and not isinstance(write["label"], str):
            raise PlanError("a write `label` must be a string")
        if "properties" in write and not isinstance(write["properties"], dict):
            raise PlanError("write `properties` must be an object")
        if op == "update":
            if not isinstance(write.get("node_id"), str) or not write["node_id"]:
                raise PlanError("an `update` write needs a string `node_id`")
            if not isinstance(write.get("patch"), dict):
                raise PlanError("an `update` write needs a `patch` object")
        unknown_keys = set(write) - _ALLOWED_WRITE_KEYS[op]
        if unknown_keys:
            raise PlanError(
                f"a {op!r} write has unknown keys {sorted(unknown_keys)} "
                f"(allowed: {sorted(_ALLOWED_WRITE_KEYS[op])})"
            )

    elicit = output.get("elicit")
    if elicit is not None and (not isinstance(elicit, dict) or "target_field" not in elicit):
        raise PlanError("elicit must be an object with `target_field`")
    if elicit is not None:
        # ACT renders these as text — a non-string would raise mid-ACT after earlier
        # actions already ran (F15): validate every rendered field up front.
        if not isinstance(elicit.get("target_field"), str):
            raise PlanError("elicit.target_field must be a string")
        if "question" in elicit and not isinstance(elicit["question"], str):
            raise PlanError("elicit.question must be a string")
        if "target_node" in elicit and not isinstance(elicit["target_node"], str):
            raise PlanError("elicit.target_node must be a string")
        unknown = set(elicit) - {"target_field", "question", "target_node", "default"}
        if unknown:
            raise PlanError(f"elicit has unknown keys {sorted(unknown)}")
        if "default" in elicit and not isinstance(elicit["default"], str):
            raise PlanError("elicit.default must be a string")

    if "rationale_summary" in output and not isinstance(output["rationale_summary"], str):
        raise PlanError("rationale_summary must be a string")

    return output


def canonical_actions(plan: dict, occurrence_id: str) -> list[Action]:
    """The plan's actions in canonical order with deterministic keys `{occurrence_id}:{index}`
    (deliver, then writes in array order, then elicit)."""
    actions: list[Action] = []

    def _key() -> str:
        return f"{occurrence_id}:{len(actions)}"

    if plan.get("deliver") is not None:
        actions.append(Action(_key(), "deliver", plan["deliver"]))
    for write in plan.get("writes") or []:
        actions.append(Action(_key(), "write", write))
    if plan.get("elicit") is not None:
        actions.append(Action(_key(), "elicit", plan["elicit"]))
    return actions
