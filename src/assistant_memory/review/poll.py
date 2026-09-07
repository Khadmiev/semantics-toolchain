# SPDX-License-Identifier: Apache-2.0
"""Baseline foreground long-poll client (spec §6.1) + announced-degradation trigger (§12).

The portable wake primitive: block on the service's long-poll and return on the FIRST new
message. A foreground skill loop re-runs it; Claude Code's background re-invoke (§6.2) can
wrap it (spawn it, it exits when a message lands, the harness re-invokes the agent). Codex
can only use the foreground form (no background survival / auto-wake — Note on measured
capabilities). Counts consecutive transport failures so development announces the switch to
manual reserve after N (§12) rather than hanging silently.

Core (`poll_until_message`) is transport-injected and side-effect-free for testing; the
httpx getter + CLI wire it to the real service.
"""

import argparse
import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime

from ..config import settings

# A getter fetches messages with seq > after, holding up to `wait`s. Returns [] on an empty
# hold (re-issue); raises TransportError on a retryable failure; raises anything else to abort.
Getter = Callable[[int, float], Awaitable[list[dict]]]


class TransportError(Exception):
    """A retryable long-poll failure (connection refused, timeout, 5xx)."""


class ServiceUnreachable(Exception):
    """N consecutive long-polls failed — development should announce manual reserve (§12)."""


async def poll_until_message(
    get: Getter,
    *,
    after: int,
    per_wait: float,
    max_failures: int = 3,
    on_failure: Callable[[int, Exception], None] | None = None,
    alarm_after: float | None = None,
    on_alarm: Callable[[float], None] | None = None,
    on_idle: Callable[[float], object] | None = None,
) -> list[dict]:
    """Long-poll until a message arrives; raise ``ServiceUnreachable`` after ``max_failures``.

    A successful (even empty) poll resets the failure counter — only a *consecutive* run of
    transport failures trips the degradation threshold. Empty holds simply re-issue (the
    server already waited ``per_wait``); the block costs no model steps (§6.1).

    Silence alarm (spec Part K, K.4.3): with ``alarm_after`` set, ``on_alarm`` fires ONCE
    when that much wall time passes with no message, then polling simply continues (one
    shot — the alarm is a nudge to a human, not a loop condition). The CALLER owns the
    state-awareness: pass ``alarm_after`` only while the review waits on the counterpart
    (``critic_reviewing`` / ``artifact_ready``), never on your own turn or on states that
    wait on a human by design. The budget is the caller's (~2x the review's median pass
    when known; a fixed default otherwise) — this client is one-shot and keeps no
    cross-invocation statistics.

    ``on_idle`` (B.7 H-2) runs after every empty hold, with the elapsed wait — the seam a
    caller needs for anything that must happen WHILE nothing is arriving. The parking
    ping's fallback is exactly that: it exists for the case where the operator does not
    answer, and a client that only wakes on messages could never fire it. Its return value
    is awaited when awaitable, so the hook may post to the channel.
    """
    failures = 0
    started = asyncio.get_event_loop().time()
    alarm_fired = False
    while True:
        try:
            messages = await get(after, per_wait)
        except TransportError as exc:
            failures += 1
            if on_failure is not None:
                on_failure(failures, exc)
            if failures >= max_failures:
                raise ServiceUnreachable(
                    f"{failures} consecutive failed long-polls: {exc}"
                ) from exc
            continue
        failures = 0
        if messages:
            return messages
        if on_idle is not None:
            result = on_idle(asyncio.get_event_loop().time() - started)
            if inspect.isawaitable(result):
                await result
        if alarm_after is not None and not alarm_fired:
            elapsed = asyncio.get_event_loop().time() - started
            if elapsed >= alarm_after:
                alarm_fired = True
                if on_alarm is not None:
                    on_alarm(elapsed)


#: Until three passes are measured, the stall alarm runs on the operator's own number:
#: typical iterations take ~10 minutes, so 15 is late enough not to nag and early enough
#: to matter (round-9 gate, 2026-08-08). Carried per review by the `review_config` notice.
BOOTSTRAP_STALL_THRESHOLD_S = 900.0
_STALL_MIN_SAMPLES = 3


def _ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def stall_threshold(
    messages: list[dict], *, bootstrap: float = BOOTSTRAP_STALL_THRESHOLD_S
) -> float:
    """How long silence may last before it counts as a stall (B.7 H-4).

    THE ALARM IS MECHANICS, NOT DISCIPLINE. Today the stall duty lives in the development
    prompt as a habit («~2x медианы прохода — скажи оператору»), which is the same
    prompt-only binding this design forbids everywhere else; a poll client is the natural
    carrier because it is the thing that watches silence for a living.

    The threshold cannot be disabled: it is the review's configured bootstrap value until
    three completed passes have been measured, then twice the running median pass time of
    THIS review — a review whose passes take an hour must not alarm every fifteen minutes,
    and one whose passes take two must not stay silent for thirty.
    """
    starts: dict[int, float] = {
        m["seq"]: _ts(m.get("created_at"))
        for m in messages
        if m.get("kind") == "artifact" and _ts(m.get("created_at")) is not None
    }
    durations: list[float] = []
    previous_end: float | None = None
    for m in messages:
        if m.get("kind") != "status" or m.get("role") != "critic":
            continue
        end = _ts(m.get("created_at"))
        if end is None:
            continue
        anchor = starts.get((m.get("payload") or {}).get("artifact_seq"))
        start = max([t for t in (anchor, previous_end) if t is not None], default=None)
        if start is not None and end > start:
            durations.append(end - start)
        previous_end = end
    if len(durations) < _STALL_MIN_SAMPLES:
        return bootstrap
    ordered = sorted(durations)
    middle = len(ordered) // 2
    median = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )
    return 2 * median


def httpx_getter(base_url: str, token: str, review_id: str, *, timeout: float) -> Getter:
    """A getter backed by the real HTTP endpoint. 5xx/connection/timeout → retryable
    ``TransportError``; a 4xx (bad/foreign token, unknown review) aborts immediately."""
    import httpx

    url = f"{base_url.rstrip('/')}/reviews/{review_id}/messages"
    headers = {"Authorization": f"Bearer {token}"}

    async def get(after: int, wait: float) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    url, params={"after": after, "wait": wait}, headers=headers
                )
        except httpx.HTTPError as exc:  # connect refused, read timeout, etc.
            raise TransportError(str(exc)) from exc
        if resp.status_code >= 500:
            raise TransportError(f"server {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"request rejected: {resp.status_code} {resp.text}")
        return resp.json().get("messages", [])

    return get


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Review long-poll client (spec §6.1).")
    ap.add_argument("--base-url", default=settings.base_url)
    ap.add_argument("--token", required=True, help="per-review bearer token")
    ap.add_argument("--review-id", required=True)
    ap.add_argument("--after", type=int, default=0, help="last seq seen (cursor)")
    # Ordering HOLD_MAX <= wait < timeout (§6): server holds up to HOLD_MAX (25s default),
    # so wait=25 returns cleanly and timeout must exceed it.
    ap.add_argument("--wait", type=float, default=25.0)
    ap.add_argument("--timeout", type=float, default=35.0)
    ap.add_argument("--max-failures", type=int, default=3)
    ap.add_argument("--alarm-after", type=float, default=0.0,
                    help="seconds of silence before a ONE-SHOT stderr alarm (K.4.3); "
                         "0 = off. Pass only while waiting on the counterpart.")
    return ap.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    get = httpx_getter(args.base_url, args.token, args.review_id, timeout=args.timeout)
    try:
        messages = await poll_until_message(
            get,
            after=args.after,
            per_wait=args.wait,
            max_failures=args.max_failures,
            on_failure=lambda n, exc: print(
                f"long-poll failed ({n}/{args.max_failures}): {exc}", file=sys.stderr
            ),
            alarm_after=args.alarm_after or None,
            on_alarm=lambda elapsed: print(
                f"SILENCE ALARM: no message for {elapsed:.0f}s — the counterpart may be "
                "stalled; check the review state (GET /reviews/{id}) and tell the operator",
                file=sys.stderr,
            ),
        )
    except ServiceUnreachable as exc:
        # Exit 2 = the §12 signal: development announces the switch to manual reserve.
        print(f"SERVICE UNREACHABLE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"messages": messages}))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(_parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
