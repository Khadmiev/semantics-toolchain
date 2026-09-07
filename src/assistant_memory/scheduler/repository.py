# SPDX-License-Identifier: Apache-2.0
"""DB helpers for the scheduler: find due jobs, claim (D23 lease), finish.

Functions flush but do not commit — the caller (the runner tick) owns the transaction.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import Node
from ..repository import graph
from ..repository.errors import ConflictError
from .job import (
    KEY_CLAIMED_AT,
    KEY_ENABLED,
    KEY_FINISHED_AT,
    KEY_LAST_RUN,
    KEY_NEXT_RUN,
    iso,
    parse_iso,
)

JOB_TYPE = "ScheduledJob"


async def due_jobs(
    session: AsyncSession, *, now: datetime, lease_seconds: int, limit: int = 100
) -> list[Node]:
    """Enabled ScheduledJob nodes whose next_run has passed and that are not currently
    claimed (or whose claim is stale past the lease — D23). Unscoped: the runner must see
    every job to schedule it (per-job effective scope applies later, at execute time)."""
    now_s = iso(now)
    lease_cutoff = iso(now - timedelta(seconds=lease_seconds))
    stmt = (
        select(Node)
        .where(
            Node.type == JOB_TYPE,
            Node.deleted_at.is_(None),
            Node.properties[KEY_ENABLED].astext == "true",
            Node.properties[KEY_NEXT_RUN].astext <= now_s,
            or_(
                Node.properties[KEY_CLAIMED_AT].astext.is_(None),
                Node.properties[KEY_CLAIMED_AT].astext <= lease_cutoff,
            ),
        )
        .limit(limit)
    )
    return list(await session.scalars(stmt))


async def claim_job(
    session: AsyncSession, *, node: Node, account_id: uuid.UUID, now: datetime
) -> Node | None:
    """Atomically claim a job by stamping claimed_at (CAS on the node version — D23).
    Returns the updated node, or None if another worker won the race."""
    try:
        return await graph.update_node(
            session,
            node_id=node.id,
            account_id=account_id,
            expected_version=node.current_version_id,
            properties_patch={KEY_CLAIMED_AT: iso(now)},
        )
    except ConflictError:
        return None


async def finish_job(
    session: AsyncSession,
    *,
    node: Node,
    account_id: uuid.UUID,
    now: datetime,
    next_run: datetime | None,
    disable: bool,
    extra: dict | None = None,
    source_ref: str | None = None,
) -> Node:
    """Record a completed occurrence: stamp last_run + finished_at, clear the claim, advance
    next_run (or disable). ``extra`` merges extra props (e.g. the run journal ``last_plan``);
    ``source_ref`` labels the finish VERSION with a per-run summary so the job's version chain
    reads back as a run-log via ``explain`` (OQ2 run-history = the audit). ``node`` must be the
    node returned by ``claim_job`` (its version id is the CAS token)."""
    patch: dict = {
        KEY_LAST_RUN: iso(now),
        KEY_FINISHED_AT: iso(now),
        KEY_CLAIMED_AT: None,
    }
    if disable:
        patch[KEY_ENABLED] = False
        patch[KEY_NEXT_RUN] = None
    else:
        patch[KEY_NEXT_RUN] = iso(next_run) if next_run else None
    if extra:
        patch.update(extra)
    return await graph.update_node(
        session,
        node_id=node.id,
        account_id=account_id,
        expected_version=node.current_version_id,
        properties_patch=patch,
        source_ref=source_ref,
    )


async def next_due(session: AsyncSession) -> datetime | None:
    """Earliest next_run among enabled jobs (drives the runner's precision sleep). None if
    there are no scheduled jobs. Min over fixed-format ISO text = chronological min."""
    val = await session.scalar(
        select(func.min(Node.properties[KEY_NEXT_RUN].astext)).where(
            Node.type == JOB_TYPE,
            Node.deleted_at.is_(None),
            Node.properties[KEY_ENABLED].astext == "true",
        )
    )
    return parse_iso(val)
