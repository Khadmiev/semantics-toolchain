# SPDX-License-Identifier: Apache-2.0
"""Resident-consumer gating (B.13 A-2): the supervisor and the checkpoint."""

import asyncio

import pytest

from assistant_memory.install.residents import InstallCheckpoint, supervise_residents

pytestmark = pytest.mark.asyncio


class StubCheckpoint:
    def __init__(self, value: bool):
        self.value = value

    async def holds(self) -> bool:
        return self.value


class Resident:
    """A resident that records its lifecycle and runs until its stop event."""

    def __init__(self):
        self.starts = 0
        self.stopped = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        self.starts += 1
        try:
            await stop.wait()
        finally:
            self.stopped.set()


async def _tick():
    await asyncio.sleep(0.15)  # > poll_seconds below: one supervisor pass


async def test_residents_not_started_while_gate_closed_and_started_when_open():
    checkpoint = StubCheckpoint(False)
    resident = Resident()
    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("r", resident.run)], poll_seconds=0.05
        )
    )
    await _tick()
    assert resident.starts == 0  # not started while stages 1-4 do not hold

    checkpoint.value = True
    await _tick()
    assert resident.starts == 1  # started when the gate first holds

    checkpoint.value = False
    await _tick()
    assert resident.stopped.is_set()  # stopped on the regression

    checkpoint.value = True
    await _tick()
    assert resident.starts == 2  # restarted when the gate holds again

    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_supervisor_shutdown_stops_children():
    checkpoint = StubCheckpoint(True)
    resident = Resident()
    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("r", resident.run)], poll_seconds=0.05
        )
    )
    await _tick()
    assert resident.starts == 1
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert resident.stopped.is_set()


async def test_stop_lets_an_inflight_unit_reach_its_commit():
    """Bounded completion at the supervisor seam (review f46a31d2, finding
    resident-stop-cancels-inflight-bot-effect): a unit already past its external
    effect gets to reach its commit; only a task still running after the grace is
    force-cancelled."""
    checkpoint = StubCheckpoint(True)
    committed = asyncio.Event()

    async def resident(stop: asyncio.Event) -> None:
        await stop.wait()
        # The in-flight unit: external effect already happened; the commit is next.
        await asyncio.sleep(0.1)
        committed.set()

    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("bot", resident)],
            poll_seconds=0.05, stop_grace_seconds=1.0,
        )
    )
    await _tick()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert committed.is_set()  # the commit happened BEFORE the supervisor returned


async def test_stop_force_cancels_a_unit_exceeding_the_grace():
    checkpoint = StubCheckpoint(True)
    cancelled = asyncio.Event()

    async def stubborn(stop: asyncio.Event) -> None:
        try:
            await asyncio.sleep(3600)  # ignores its stop event entirely
        except asyncio.CancelledError:
            cancelled.set()
            raise

    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("stubborn", stubborn)],
            poll_seconds=0.05, stop_grace_seconds=0.1,
        )
    )
    await _tick()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert cancelled.is_set()


async def test_stop_grace_is_shared_not_additive():
    """One deadline per stop episode (review f46a31d2, finding
    resident-stop-grace-is-additive): N stubborn residents stop within ~one grace,
    not N of them."""
    checkpoint = StubCheckpoint(True)

    async def stubborn(stop: asyncio.Event) -> None:
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await asyncio.sleep(3600)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop,
            [("s1", stubborn), ("s2", stubborn), ("s3", stubborn)],
            poll_seconds=0.05, stop_grace_seconds=0.3,
        )
    )
    await _tick()
    started = loop.time()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    elapsed = loop.time() - started
    assert elapsed < 0.9, f"stop took {elapsed:.2f}s — the grace multiplied per resident"


async def test_completed_one_shot_is_not_restarted():
    """A cleanly finished one-shot never restarts (review f46a31d2, finding
    completed-backfill-restarts-forever) — while a gate-cancelled resident
    legitimately reruns on reopen (covered by the restart test above)."""
    checkpoint = StubCheckpoint(True)
    starts = 0

    async def one_shot(stop: asyncio.Event) -> None:
        nonlocal starts
        starts += 1  # completes immediately: the backfill shape

    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("backfill", one_shot, "one_shot")], poll_seconds=0.03
        )
    )
    await asyncio.sleep(0.3)  # many supervisor polls
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert starts == 1


async def test_long_running_resident_restarts_on_any_return():
    """Declared lifecycle (review f46a31d2, finding
    resident-lifecycle-classification-collapses-long-running-and-one-shot): a
    long-running resident's return — clean OR failed — is unexpected and restarts on
    the next poll, independently of a living sibling."""
    checkpoint = StubCheckpoint(True)
    starts = {"clean": 0, "failing": 0}

    async def returns_clean(stop: asyncio.Event) -> None:
        starts["clean"] += 1  # returns immediately: unexpected for long_running

    async def fails(stop: asyncio.Event) -> None:
        starts["failing"] += 1
        raise RuntimeError("boom")

    async def sibling(stop: asyncio.Event) -> None:
        await stop.wait()  # lives forever: must not mask the restarts

    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop,
            [
                ("clean", returns_clean, "long_running"),
                ("failing", fails, "long_running"),
                ("sibling", sibling, "long_running"),
            ],
            poll_seconds=0.03,
        )
    )
    await asyncio.sleep(0.3)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert starts["clean"] >= 2, "clean return of a long-runner must restart"
    assert starts["failing"] >= 2, "a failed long-runner must restart despite the sibling"


async def test_stop_bounds_the_post_cancel_cleanup():
    """Cancellation is cooperative: a resident that swallows CancelledError must not
    hold the stop episode (review f46a31d2, finding
    resident-stop-deadline-ends-before-cancellation-cleanup)."""
    checkpoint = StubCheckpoint(True)

    swallowed = {"n": 0}

    async def swallows_cancel(stop: asyncio.Event) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Swallows the SUPERVISOR's cancel (what the bound protects
                # against); the next cancel — the event loop's teardown — is
                # honoured so the test session can end.
                swallowed["n"] += 1
                if swallowed["n"] >= 2:
                    raise
                continue

    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("undead", swallows_cancel, "long_running")],
            poll_seconds=0.05, stop_grace_seconds=0.1, cancel_cleanup_seconds=0.1,
        )
    )
    await _tick()
    stop.set()
    await asyncio.wait_for(task, timeout=2)  # returns despite the undead resident


async def test_gate_closure_survivor_stays_supervised_without_duplicate():
    """A task that outlives cancellation during a LIVE gate closure stays under
    supervision (review f46a31d2, finding
    resident-abandonment-escapes-gate-supervision): while it lives, no duplicate
    with its name starts on reopen; once it ends it is reaped and restarted."""
    import contextlib

    checkpoint = StubCheckpoint(True)
    starts = 0

    async def survivor(stop: asyncio.Event) -> None:
        nonlocal starts
        starts += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.4)  # slow unwind, past the cleanup bound

    stop = asyncio.Event()
    task = asyncio.create_task(
        supervise_residents(
            checkpoint, stop, [("s", survivor, "long_running")],
            poll_seconds=0.2, stop_grace_seconds=0.05, cancel_cleanup_seconds=0.05,
        )
    )
    await asyncio.sleep(0.05)
    assert starts == 1
    checkpoint.value = False        # one poll (~t=0.2) runs one stop episode
    await asyncio.sleep(0.25)
    checkpoint.value = True         # reopened BEFORE the next poll (~t=0.45)
    await asyncio.sleep(0.25)       # the reopen poll ran while the survivor lives
    assert starts == 1, "a live survivor must block a same-name duplicate"
    await asyncio.sleep(0.5)        # survivor unwound and returned; reaped; restarted
    assert starts == 2, "the ended survivor must be reaped and restarted"
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_checkpoint_fails_closed(monkeypatch):
    """A checkpoint that cannot answer admits nothing."""

    class Boom:
        def __call__(self):
            raise RuntimeError("db down")

    checkpoint = InstallCheckpoint(Boom())
    assert await checkpoint.holds() is False
