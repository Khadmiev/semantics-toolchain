# SPDX-License-Identifier: Apache-2.0
"""Typed view over a ScheduledJob node's schemaless ``properties`` blob.

A ScheduledJob's definition lives in the graph (D3); the runner reads these nodes. Node
content is a JSONB blob, so the "schema" is a convention enforced here in code, not in the
DB. Field names are the implementer's call (OQ2, resolved 2026-07-05).

Trigger kinds: ``one_shot``, ``interval`` (spec.seconds), ``cron`` (spec.expr, croniter).
``event`` and full timezone-aware cron land in later slices; the trigger predicate (D5/D30)
is evaluated by scheduler/predicate.py.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from croniter import croniter

# Property keys on the ScheduledJob node (the schema convention).
KEY_ENABLED = "enabled"
KEY_TRIGGER = "trigger"  # {"kind", "spec", "predicate"?}
KEY_INSTRUCTION = "instruction"
KEY_AGENCY = "agency"  # "gather_then_judge" | "autonomous"
KEY_TOOLS = "tools"
KEY_WRITES = "writes"
KEY_BUDGET = "budget"
KEY_DELIVERY = "delivery"  # {"channel", "target"}
KEY_NEXT_RUN = "next_run"  # ISO-8601 UTC, microsecond precision
KEY_LAST_RUN = "last_run"
KEY_CLAIMED_AT = "claimed_at"  # D23 lease: set on claim, cleared on finish
KEY_FINISHED_AT = "finished_at"

SUPPORTED_KINDS = ("one_shot", "interval", "cron")
RECURRING_KINDS = ("interval", "cron")


def iso(dt: datetime) -> str:
    """Canonical timestamp string: UTC, fixed microsecond precision so lexicographic order
    equals chronological order — the runner query compares next_run/claimed_at as JSONB
    text, which is only sound with a single, fixed format."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class ScheduledJob:
    """A parsed, read-only view of a ScheduledJob node."""

    node_id: uuid.UUID
    version_id: uuid.UUID | None
    props: dict

    @classmethod
    def from_node(cls, node) -> "ScheduledJob":
        return cls(node.id, node.current_version_id, node.properties or {})

    @property
    def enabled(self) -> bool:
        return bool(self.props.get(KEY_ENABLED))

    @property
    def trigger(self) -> dict:
        # A truthy non-dict trigger yields {} -> kind "" -> unsupported kind -> the job is
        # skipped BEFORE any claim (never an AttributeError mid-pipeline; F19).
        raw = self.props.get(KEY_TRIGGER)
        return raw if isinstance(raw, dict) else {}

    @property
    def kind(self) -> str:
        return self.trigger.get("kind", "")

    @property
    def spec(self) -> dict:
        """Raises ``ValueError`` on a truthy non-dict spec — inside ``advance()`` that maps
        to the durable ``{stage: "advance"}`` failure path, never an AttributeError escape
        past the claim (F19, review dc694faf)."""
        raw = self.trigger.get("spec")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ValueError(f"trigger.spec must be an object, got {type(raw).__name__}")
        return raw

    @property
    def has_predicate(self) -> bool:
        # PRESENCE, not truthiness (F30, review dc694faf): a falsy malformed predicate
        # ([], "", 0) must reach validation and fail closed, not read as "no gate".
        return self.trigger.get("predicate") is not None

    @property
    def predicate(self):
        """The RAW persisted predicate — no normalization; the runtime validates it."""
        return self.trigger.get("predicate")

    @property
    def instruction(self) -> str:
        return self.props.get(KEY_INSTRUCTION, "")

    @property
    def next_run(self) -> datetime | None:
        return parse_iso(self.props.get(KEY_NEXT_RUN))

    def supported_kind(self) -> bool:
        return self.kind in SUPPORTED_KINDS

    def is_recurring(self) -> bool:
        return self.kind in RECURRING_KINDS

    def advance(self, now: datetime) -> tuple[datetime | None, bool]:
        """Return ``(next_run, disable)`` after a fire at ``now``.

        ``one_shot`` → ``(None, True)``: runs once, then disabled.
        ``interval`` → ``(now + spec.seconds, False)``.
        ``cron``     → ``(croniter next after now, False)`` — UTC for now (tz-aware in a
        later slice, D29).
        """
        if self.kind == "one_shot":
            return None, True
        if self.kind == "interval":
            seconds = int(self.spec.get("seconds", 0))
            return now + timedelta(seconds=seconds), False
        if self.kind == "cron":
            expr = self.spec.get("expr")
            if not isinstance(expr, str) or not expr:
                raise ValueError(f"cron trigger needs a non-empty string spec.expr, got {expr!r}")
            try:
                nxt = croniter(expr, now).get_next(datetime)
            except (ValueError, KeyError, AttributeError, TypeError) as exc:
                # croniter raises AttributeError/TypeError on exotic inputs — normalize to
                # ValueError so _process's durable {stage: advance} path catches it (F27)
                raise ValueError(f"invalid cron expr {expr!r}: {exc}") from exc
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=UTC)
            return nxt, False
        raise ValueError(f"unsupported trigger kind: {self.kind!r}")
