# SPDX-License-Identifier: Apache-2.0
"""Shaping helpers for authoring ScheduledJob nodes via MCP (create_scheduled_job).

Turns friendly tool args into the ScheduledJob properties blob + the initial next_run, and
validates the trigger. Keeps the job schema (OQ2) in one place so the MCP handler stays thin
glue. Trigger errors are raised as ``TriggerError`` (the handler maps them to InvalidArgument).
"""

from datetime import UTC, datetime

from croniter import croniter

from .job import (
    KEY_AGENCY,
    KEY_BUDGET,
    KEY_DELIVERY,
    KEY_ENABLED,
    KEY_INSTRUCTION,
    KEY_NEXT_RUN,
    KEY_TOOLS,
    KEY_TRIGGER,
    KEY_WRITES,
    SUPPORTED_KINDS,
    iso,
    parse_iso,
)


class TriggerError(ValueError):
    """A trigger the scheduler cannot schedule (bad kind/spec)."""


def _parse_iso_or_trigger_error(value, field: str):
    """A malformed ISO timestamp is a NAMED trigger rejection, not a raw ValueError
    escaping the authoring contract (F24, review dc694faf)."""
    try:
        return parse_iso(value)
    except (ValueError, TypeError) as exc:
        raise TriggerError(f"{field} must be an ISO datetime, got {value!r}") from exc


def initial_next_run(trigger: dict, now: datetime) -> datetime:
    """The first fire time for a freshly-authored job.

    one_shot → spec.at (required). interval → spec.start or now. cron → next cron time.
    """
    if not isinstance(trigger, dict) or "kind" not in trigger:
        raise TriggerError("trigger must be an object with a 'kind'")
    kind = trigger.get("kind")
    spec = trigger.get("spec")
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        raise TriggerError(f"trigger.spec must be an object, got {type(spec).__name__}")
    if kind == "one_shot":
        at = _parse_iso_or_trigger_error(spec.get("at"), "one_shot spec.at")
        if at is None:
            raise TriggerError("one_shot trigger needs spec.at (ISO datetime)")
        return at
    if kind == "interval":
        if "seconds" not in spec:
            raise TriggerError("interval trigger needs spec.seconds")
        # The runtime advances with int(seconds) — accept only what it can execute, and
        # only a FORWARD interval (F13, review dc694faf: authoring must not persist a
        # trigger the first occurrence would die on).
        try:
            seconds = int(spec["seconds"])
        except (TypeError, ValueError) as exc:
            raise TriggerError(
                f"interval spec.seconds must be an integer, got {spec['seconds']!r}"
            ) from exc
        if seconds < 1:
            raise TriggerError(f"interval spec.seconds must be >= 1, got {seconds}")
        return _parse_iso_or_trigger_error(spec.get("start"), "interval spec.start") or now
    if kind == "cron":
        expr = spec.get("expr")
        if not isinstance(expr, str) or not expr:
            raise TriggerError(f"cron trigger needs a non-empty string spec.expr, got {expr!r}")
        try:
            nxt = croniter(expr, now).get_next(datetime)
        except (ValueError, KeyError, AttributeError, TypeError) as exc:
            # croniter raises AttributeError/TypeError on exotic inputs (F27)
            raise TriggerError(f"invalid cron expr: {expr!r}") from exc
        return nxt if nxt.tzinfo else nxt.replace(tzinfo=UTC)
    raise TriggerError(f"unsupported trigger kind: {kind!r} (supported: {SUPPORTED_KINDS})")


def build_job_properties(
    *,
    instruction: str,
    trigger: dict,
    now: datetime | None = None,
    agency: str | None = None,
    tools: list | None = None,
    writes: object = None,
    budget: object = None,
    delivery: dict | None = None,
    enabled: bool = True,
) -> dict:
    """Assemble the ScheduledJob ``properties`` blob (validating the trigger + computing the
    first next_run). Optional fence/delivery fields are only written when provided."""
    if not instruction:
        raise TriggerError("a scheduled job needs an instruction")
    next_run = initial_next_run(trigger, now or datetime.now(UTC))
    props: dict = {
        KEY_ENABLED: bool(enabled),
        KEY_TRIGGER: trigger,
        KEY_INSTRUCTION: instruction,
        KEY_NEXT_RUN: iso(next_run),
    }
    for key, value in (
        (KEY_AGENCY, agency),
        (KEY_TOOLS, tools),
        (KEY_WRITES, writes),
        (KEY_BUDGET, budget),
        (KEY_DELIVERY, delivery),
    ):
        if value is not None:
            props[key] = value
    return props
