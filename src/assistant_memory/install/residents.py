# SPDX-License-Identifier: Apache-2.0
"""Resident-consumer gating (B.13 A-2): the shared checkpoint and the supervisor.

The application starts background work of its own — the embedder warm-and-backfill
task, the actionable-layer scheduler runner, and the bot poller — and each of these
reads, mutates, or emits product data. While stages 1-4 do not hold they are NOT
started, and on a regression they are STOPPED. The embedder warm-up may run early (it
touches a model cache, not product data); its backfill half writes product rows and
waits for the gate like everything else.

The contract has two halves, both here:

- ``InstallCheckpoint`` — the shared install-state checkpoint every resident consumer
  re-evaluates BEFORE EACH UNIT of its own work (a scheduler tick, a bot poll
  iteration, a backfill batch). It is also one of A-5's observation points: a
  regression seen here is recorded. It fails CLOSED — a checkpoint that cannot answer
  admits nothing.
- ``supervise_residents`` — the lifespan task that starts the consumers when stages
  1-4 first hold, stops them when the checkpoint fails, and restarts them when it
  holds again. The per-unit check is the detection mechanism (worst-case lag one unit
  of work); the supervisor is what makes "not started / stopped" literal for the
  task objects themselves.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from .state import compute_state, observe_regression

logger = logging.getLogger(__name__)

#: How often the supervisor re-asks the checkpoint while consumers are stopped (or
#: running, as the stop backstop). The per-unit check inside each consumer is the
#: primary detection point; this cadence only bounds how quickly a consumer STARTS
#: once the gate first opens.
SUPERVISOR_POLL_SECONDS = 5.0

#: How long a stopping resident may keep running after its stop event is set, before
#: it is force-cancelled. This is the supervisor's half of the BOUNDED-COMPLETION
#: contract (review f46a31d2, finding resident-stop-cancels-inflight-bot-effect): an
#: in-flight unit — a bot send that has reached Telegram but not yet its database
#: commit — gets to finish or hit its own gate check; only a unit still running after
#: the grace is cut. Immediate cancellation reproduced exactly the delivered-but-
#: pending double-send the contract forbids.
RESIDENT_STOP_GRACE_SECONDS = 10.0


class InstallCheckpoint:
    """The shared install-state checkpoint (A-2). One instance per process."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def holds(self) -> bool:
        try:
            async with self._session_factory() as session:
                state = await compute_state(session)
                if await observe_regression(session, state):
                    await session.commit()
                    state = await compute_state(session)
                return state.serving_open
        except Exception:  # noqa: BLE001 - fail CLOSED: an unanswerable gate admits nothing
            logger.exception("install checkpoint failed to answer — treating as closed")
            return False


#: A resident factory: given a fresh stop event, returns the consumer's coroutine.
ResidentFactory = Callable[[asyncio.Event], Awaitable[None]]

#: Declared resident lifecycles (finding
#: resident-lifecycle-classification-collapses-long-running-and-one-shot): the
#: registry says which shape each resident is, instead of the supervisor inferring
#: it from how the task happened to end.
#: - ``one_shot`` — runs to completion once (the embedding backfill); a CLEAN return
#:   is its success and it is never restarted, while a gate-cancel reruns it.
#: - ``long_running`` — runs until stopped (scheduler, bot); ANY return while the
#:   gate holds — clean or exceptional — is unexpected, and the resident is
#:   restarted on the next poll, independently of its siblings.
RESIDENT_KINDS = ("long_running", "one_shot")

#: How long past the shared stop deadline a force-cancelled task may take to unwind
#: before the supervisor stops waiting and abandons it by name. Cancellation is
#: cooperative; without this second bound a resident that swallows CancelledError
#: could hold the stop episode forever (finding
#: resident-stop-deadline-ends-before-cancellation-cleanup).
RESIDENT_CANCEL_CLEANUP_SECONDS = 2.0


async def supervise_residents(
    checkpoint: InstallCheckpoint,
    stop: asyncio.Event,
    residents: list[tuple],
    *,
    poll_seconds: float = SUPERVISOR_POLL_SECONDS,
    stop_grace_seconds: float = RESIDENT_STOP_GRACE_SECONDS,
    cancel_cleanup_seconds: float = RESIDENT_CANCEL_CLEANUP_SECONDS,
) -> None:
    """Start the gated residents when stages 1-4 hold; stop them when they do not.

    ``residents`` is the enumerated list A-2 requires — each lifespan-started task,
    classified as ``(name, factory, kind)`` with kind from ``RESIDENT_KINDS`` (a
    two-element entry defaults to ``long_running``). The consumers ALSO re-check the
    checkpoint before each unit (their own code carries that call); this supervisor
    is the start/stop half.

    Lifecycle is DECLARED, not inferred (finding
    resident-lifecycle-classification-collapses-long-running-and-one-shot): a
    ``one_shot``'s clean return is success and it never restarts (a gate-cancel
    reruns it on reopen); a ``long_running`` resident's return of ANY shape while
    the gate holds is unexpected, and it is restarted on the next poll —
    independently of its siblings, not only when the whole list is empty.

    Stopping honours the BOUNDED-COMPLETION contract twice over: the stop event is
    set first, every task shares ONE deadline (``stop_grace_seconds``) so N stubborn
    residents cannot multiply the grace, force-cancel follows, and the cancelled
    task's unwinding is ALSO bounded (``cancel_cleanup_seconds``) — a resident that
    swallows the cancellation is abandoned by name rather than awaited forever.
    """
    entries: list[tuple[str, ResidentFactory, str]] = []
    for entry in residents:
        name, factory = entry[0], entry[1]
        kind = entry[2] if len(entry) > 2 else "long_running"
        if kind not in RESIDENT_KINDS:
            raise ValueError(f"resident {name!r}: unknown lifecycle kind {kind!r}")
        entries.append((name, factory, kind))

    running: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}
    finished: set[str] = set()  # one-shots that completed cleanly — never restarted

    def _start_missing() -> None:
        for name, factory, _kind in entries:
            if name in running or name in finished:
                continue
            child_stop = asyncio.Event()
            task = asyncio.create_task(factory(child_stop), name=f"resident:{name}")
            running[name] = (task, child_stop)
            logger.info("install gate open — resident started: %s", name)

    survivors_logged: set[str] = set()  # zombies named once, not once per poll

    async def _stop_all(reason: str, *, terminal: bool) -> None:
        # Signal every resident FIRST, then grant ONE shared deadline for the whole
        # stop episode: each await uses the REMAINING time, so N stubborn residents
        # together cannot exceed the declared grace (finding
        # resident-stop-grace-is-additive). The post-cancel unwinding gets its own
        # short bound (finding resident-stop-deadline-ends-before-cancellation-
        # cleanup). What happens to a task STILL alive after both bounds depends on
        # which exit this is (finding resident-abandonment-escapes-gate-supervision):
        # - terminal (process shutdown): logged and abandoned — the process exits;
        # - live gate closure: the task STAYS in the running map, so no duplicate
        #   can start on reopen and the next poll retries the bounded stop; when it
        #   finally ends it is reaped and becomes restartable like any other return.
        for _, child_stop in running.values():
            child_stop.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + stop_grace_seconds
        for name, (task, _) in list(running.items()):
            remaining = max(0.0, deadline - loop.time())
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                task.cancel()
            except BaseException:  # noqa: BLE001 - a resident's own error ends it too
                pass
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=cancel_cleanup_seconds)
            except (TimeoutError, asyncio.TimeoutError):
                pass
            except BaseException:  # noqa: BLE001
                pass
            if task.done():
                running.pop(name, None)
                survivors_logged.discard(name)
                logger.info("resident stopped (%s): %s", reason, name)
            elif terminal:
                running.pop(name, None)
                logger.error(
                    "resident %s did not unwind within the cleanup bound — "
                    "abandoned at process shutdown (still-live task)", name,
                )
            else:
                if name not in survivors_logged:
                    survivors_logged.add(name)
                    logger.error(
                        "resident %s survived cancellation past the cleanup bound — "
                        "KEPT under gate supervision: no replacement will start and "
                        "the stop retries each poll until it ends", name,
                    )

    kinds = {name: kind for name, _f, kind in entries}

    def _reap() -> None:
        for name in [n for n, (t, _) in running.items() if t.done()]:
            task, _ = running.pop(name)
            clean = not task.cancelled() and task.exception() is None
            if kinds[name] == "one_shot" and clean:
                finished.add(name)
                logger.info("one-shot resident finished cleanly: %s", name)
            elif clean:
                logger.warning(
                    "long-running resident %s returned unexpectedly (clean) — "
                    "will restart on the next poll", name,
                )
            else:
                logger.warning(
                    "resident %s ended (cancelled/failed) — will restart on the "
                    "next poll while the gate holds", name,
                )

    try:
        while not stop.is_set():
            holds = await checkpoint.holds()
            # Reap BEFORE the start predicate: a finished one-shot must be recorded
            # before "missing" can mean "start it again".
            _reap()
            if holds:
                _start_missing()
            elif running:
                await _stop_all("install gate closed", terminal=False)
            with suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
    finally:
        await _stop_all("shutdown", terminal=True)
