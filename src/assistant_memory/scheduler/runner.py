# SPDX-License-Identifier: Apache-2.0
"""Resident scheduler runner (actionable layer).

Slices: (1) rescan + claim/finish; (2) LISTEN/NOTIFY precision, deterministic predicate (D30),
cron; (3) the execute pipeline — GATHER → REASON (a pluggable Reasoner seam, D16) → validate the
§1 plan → ACT within the fence (D8/D21), for trusted, simple-mode jobs; (4) GATHER runs the
job's declared read-steps (D33/D34, scheduler/gather.py). The autonomous read-loop stays a
later slice.
"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ..access import Access
from . import predicate
from .act import FenceError, execute_actions
from .gather import GatherError, run_gather
from .job import KEY_TOOLS, ScheduledJob, iso
from .notify import ScheduledJobListener
from .plan import PlanError, canonical_actions, validate_final_plan
from .principal import ensure_scheduler_account
from .reasoning import Reasoner, ReasonerError, get_reasoner
from .repository import claim_job, due_jobs, finish_job, next_due

logger = logging.getLogger(__name__)

_MIN_SLEEP = 1.0  # floor (s) so a stuck/unsupported job can never spin the loop


def _run_source_ref(now: datetime, should_execute: bool, summary: dict | None) -> str:
    """A one-line per-run summary for the finish version's source_ref, so the job's version chain
    reads back as a run-log via ``explain`` (OQ2 run-history = the audit). A FAILED occurrence is
    rendered as such — durably distinguishable from an intentional no-op plan."""
    if not should_execute:
        return f"run {iso(now)}: skipped (predicate false)"
    error = (summary or {}).get("error")
    if error:
        detail = error.get("detail", "")[:80]
        return f"run {iso(now)}: FAILED at {error.get('stage', '?')}: {detail}"
    actions = (summary or {}).get("actions") or []
    kinds = ", ".join(a.get("kind", "?") for a in actions) if actions else "no-op"
    return f"run {iso(now)}: {len(actions)} action(s) [{kinds}]"


class SchedulerRunner:
    """``tick`` is one pass over one session (no commit); ``run`` is the resident loop that owns
    sessions + commits, sleeps until the next due time (or a NOTIFY), and stops on ``stop``."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        rescan_seconds: int = 30,
        lease_seconds: int = 300,
        notify_dsn: str | None = None,
        reasoner: Reasoner | None = None,
        unit_gate: Callable[[], "asyncio.Future | object"] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._rescan_seconds = rescan_seconds
        self._lease_seconds = lease_seconds
        self._notify_dsn = notify_dsn
        self._reasoner = reasoner or get_reasoner()
        # B.13 A-2: the shared install-state checkpoint, re-evaluated before each
        # tick (this consumer's unit of work). A failing check exits the loop —
        # the tick is transactional, so nothing straddles the boundary; the install
        # supervisor restarts the runner when the gate holds again.
        self._unit_gate = unit_gate

    async def execute(
        self,
        session: AsyncSession,
        *,
        job: ScheduledJob,
        node,
        account_id,
        occurrence_id: str,
    ) -> dict:
        """GATHER → REASON → validate → ACT for one occurrence. ALWAYS returns a run summary for
        the journal: on success ``{occurrence_id, actions, ...}``; on any failure/skip a durable
        ``{occurrence_id, error: {stage, detail}}`` — a job that did no useful work must say so
        in its run record, not only in a process log (F2, review dc694faf)."""

        def _failed(stage: str, detail: str) -> dict:
            return {
                "occurrence_id": occurrence_id,
                "error": {"stage": stage, "detail": detail[:300]},
            }

        agency = job.props.get("agency") or "gather_then_judge"
        if agency == "autonomous":
            logger.info(
                "scheduler: job %s is autonomous — the read-loop is a later slice; skipped",
                job.node_id,
            )
            return _failed("agency", "autonomous read-loop not implemented in this slice")

        # GATHER (D33/D34): run the job's declared read-steps under its EFFECTIVE principal
        # (the creating account scoped to the origin space, D18/D19). Fail closed on a bad
        # declaration or a failed read — REASON must not judge over a partial picture.
        context = {"instruction": job.instruction}
        gather_stats = None
        if job.props.get(KEY_TOOLS):
            try:
                context["context"], gather_stats = await run_gather(session, job=job, node=node)
            except GatherError as exc:
                logger.exception(
                    "scheduler: job %s gather failed — not executing", job.node_id
                )
                return _failed("gather", str(exc))
        try:
            output = await self._reasoner.reason(context)
        except ReasonerError as exc:
            logger.exception("scheduler: job %s reasoner output unusable", job.node_id)
            return _failed("reason", str(exc))
        except Exception as exc:  # noqa: BLE001 - backend/transport failure (timeout, API,
            # missing CLI) must land in the run record like any other failure (F5), not
            # escape to tick() and leave only a stale claim + a process log line.
            logger.exception("scheduler: job %s reasoner backend failed", job.node_id)
            return _failed("reason_transport", str(exc))
        try:
            plan = validate_final_plan(output)
        except PlanError as exc:
            logger.exception(
                "scheduler: job %s produced an invalid plan — not executing", job.node_id
            )
            return _failed("plan", str(exc))

        actions = canonical_actions(plan, occurrence_id)
        if not actions:
            summary = {"occurrence_id": occurrence_id, "actions": []}
            if gather_stats is not None:
                summary["gather"] = gather_stats
            return summary

        space_id = node.origin_space
        if space_id is None:
            logger.warning(
                "scheduler: job %s has no origin_space — cannot execute writes/deliveries",
                job.node_id,
            )
            return _failed("space", "job node has no origin_space")
        delivery = job.props.get("delivery")
        if delivery is None:
            delivery = {}
        if not isinstance(delivery, dict):
            # a truthy non-object delivery must not AttributeError past the claim (F22)
            return _failed("delivery", f"delivery must be an object, got {type(delivery).__name__}")
        delivery_target = delivery.get("target")
        try:
            # Savepoint around ACT (F15): if any action fails mid-list, the partial
            # side-effects roll back — never "first delivery committed, rest lost, claim
            # stale". The failure still finishes the occurrence with a durable record.
            async with session.begin_nested():
                executed = await execute_actions(
                    session,
                    job=job,
                    actions=actions,
                    account_id=account_id,
                    space_id=space_id,
                    delivery_target=delivery_target,
                )
        except FenceError as exc:
            logger.exception(
                "scheduler: job %s plan violated the fence — nothing executed", job.node_id
            )
            return _failed("fence", str(exc))
        except Exception as exc:  # noqa: BLE001 - any ACT failure: partial actions rolled
            # back by the savepoint; journal durably instead of escaping to tick()
            logger.exception("scheduler: job %s ACT failed — rolled back", job.node_id)
            return _failed("act", str(exc))
        summary = {
            "occurrence_id": occurrence_id,
            "actions": executed,
            "rationale": plan.get("rationale_summary"),
        }
        if gather_stats is not None:
            summary["gather"] = gather_stats
        return summary

    async def _process(self, session: AsyncSession, node, account_id, now: datetime) -> int:
        """Handle one due job: predicate-gate, claim (D23), execute (§3 pipeline), record."""
        job = ScheduledJob.from_node(node)
        if not job.supported_kind():
            logger.warning(
                "scheduler: skip job %s — unsupported trigger kind %r", job.node_id, job.kind
            )
            return 0
        should_execute = True
        predicate_error: str | None = None
        if job.has_predicate:
            if not job.is_recurring():
                # Authoring rejects this combo now; a PERSISTED one must land in the same
                # durable finish+disable path, not be silently skipped while due (F26).
                predicate_error = "one_shot + predicate is not supported by the runner"
            elif node.created_by is None or node.origin_space is None:
                # Predicate reads run under the job's effective scope (F31, D18/D19) —
                # a job we cannot scope must not read at all.
                predicate_error = "job has no created_by/origin_space — cannot scope reads"
            else:
                try:
                    # Static grammar check FIRST (F26): shapes like {"all": "bad"} would
                    # raise AttributeError deep inside evaluation, past this catch.
                    predicate.validate(job.predicate)
                    access = Access(session, node.created_by, scopes={node.origin_space})
                    should_execute = await predicate.evaluate(session, job.predicate, access)
                except ValueError as exc:
                    # A malformed persisted predicate raises on EVERY occurrence — journal
                    # durably and disable, never a due-but-unjournaled loop (F21).
                    logger.exception("scheduler: job %s predicate malformed", job.node_id)
                    predicate_error = str(exc)

        claimed = await claim_job(session, node=node, account_id=account_id, now=now)
        if claimed is None:
            return 0  # another worker won the claim

        occurrence_id = f"{node.id}:{iso(now)}"
        if predicate_error is not None:
            summary = {
                "occurrence_id": occurrence_id,
                "error": {"stage": "predicate", "detail": predicate_error[:300]},
            }
            await finish_job(
                session,
                node=claimed,
                account_id=account_id,
                now=now,
                next_run=None,
                disable=True,
                extra={"last_plan": summary},
                source_ref=_run_source_ref(now, True, summary),
            )
            return 0
        # Advance the trigger BEFORE any ACT side effect (F12, review dc694faf): malformed
        # persisted trigger data (bad interval seconds, bad cron expr) must fail here —
        # finished with a durable record and DISABLED (it can never schedule again) — not
        # after deliveries/writes are staged, where an escape would leave a stale claim
        # that retries the side effects when the lease expires.
        try:
            next_run, disable = job.advance(now)
        except (KeyError, TypeError, ValueError) as exc:
            logger.exception("scheduler: job %s trigger cannot advance", job.node_id)
            summary = {
                "occurrence_id": occurrence_id,
                "error": {"stage": "advance", "detail": str(exc)[:300]},
            }
            await finish_job(
                session,
                node=claimed,
                account_id=account_id,
                now=now,
                next_run=None,
                disable=True,
                extra={"last_plan": summary},
                source_ref=_run_source_ref(now, True, summary),
            )
            return 0

        summary = None
        if should_execute:
            summary = await self.execute(
                session, job=job, node=claimed, account_id=account_id, occurrence_id=occurrence_id
            )
        await finish_job(
            session,
            node=claimed,
            account_id=account_id,
            now=now,
            next_run=next_run,
            disable=disable,
            extra={"last_plan": summary} if summary is not None else None,
            source_ref=_run_source_ref(now, should_execute, summary),
        )
        if should_execute:
            return 1
        logger.info("scheduler: job %s predicate false — rescheduled, not executed", job.node_id)
        return 0

    async def tick(self, session: AsyncSession) -> int:
        """One rescan pass over one session (no commit). Returns the number of jobs fired."""
        now = datetime.now(UTC)
        account_id = await ensure_scheduler_account(session)
        fired = 0
        for node in await due_jobs(session, now=now, lease_seconds=self._lease_seconds):
            try:
                fired += await self._process(session, node, account_id, now)
            except Exception:  # noqa: BLE001 - one bad job must not stop the whole pass
                logger.exception("scheduler: job %s failed", node.id)
        return fired

    def _sleep_seconds(self, next_due_at: datetime | None) -> float:
        """Sleep until the next due time, capped at the safety rescan interval and floored so a
        stuck job can't spin."""
        if next_due_at is None:
            return float(self._rescan_seconds)
        delta = (next_due_at - datetime.now(UTC)).total_seconds()
        return max(_MIN_SLEEP, min(float(self._rescan_seconds), delta))

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until ``stop``: tick, commit, then sleep until the next due time (or a NOTIFY)."""
        logger.info(
            "scheduler runner started (rescan<=%ss, lease=%ss, notify=%s)",
            self._rescan_seconds,
            self._lease_seconds,
            bool(self._notify_dsn),
        )
        wakeup = asyncio.Event()
        listener: ScheduledJobListener | None = None
        if self._notify_dsn:
            listener = ScheduledJobListener(self._notify_dsn, wakeup)
            try:
                await listener.start()
                logger.info("scheduler: LISTEN/NOTIFY active")
            except Exception:  # noqa: BLE001 - degrade to rescan-only if LISTEN fails
                logger.exception("scheduler: LISTEN setup failed — rescan-only")
                listener = None
        try:
            while not stop.is_set():
                if self._unit_gate is not None and not await self._unit_gate():
                    logger.warning(
                        "scheduler: install gate closed — stopping before the next tick"
                    )
                    break
                sleep_for = float(self._rescan_seconds)
                try:
                    async with self._session_factory() as session:
                        fired = await self.tick(session)
                        await session.commit()
                        if fired:
                            logger.info("scheduler: fired %d job(s)", fired)
                        sleep_for = self._sleep_seconds(await next_due(session))
                except Exception:  # noqa: BLE001 - a bad tick must not kill the resident runner
                    logger.exception("scheduler tick failed")
                wakeup.clear()
                await self._wait(stop, wakeup, sleep_for)
        finally:
            if listener is not None:
                with suppress(Exception):
                    await listener.stop()
        logger.info("scheduler runner stopped")

    async def _wait(self, stop: asyncio.Event, wakeup: asyncio.Event, timeout: float) -> None:
        """Sleep up to ``timeout``, waking early on ``stop`` or a NOTIFY ``wakeup``."""
        waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(wakeup.wait())]
        try:
            await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiters:
                task.cancel()
            for task in waiters:
                with suppress(asyncio.CancelledError):
                    await task
