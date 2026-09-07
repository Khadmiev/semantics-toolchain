# SPDX-License-Identifier: Apache-2.0
"""LISTEN/NOTIFY listener for ScheduledJob changes (D6 precision layer).

A dedicated asyncpg connection (separate from the SQLAlchemy pool) LISTENs the
``scheduled_job_changed`` channel that the DB trigger raises on any ScheduledJob
insert/update, and sets an ``asyncio.Event`` so the runner wakes immediately instead of
waiting for the next rescan. If this connection drops, reactivity is lost but the rescan
backstop (D6) still fires jobs — no correctness impact, only latency.
"""

import asyncio
import logging
from contextlib import suppress

import asyncpg

logger = logging.getLogger(__name__)

CHANNEL = "scheduled_job_changed"
DELIVERY_CHANNEL = "delivery_changed"


def to_asyncpg_dsn(database_url: str) -> str:
    """SQLAlchemy URL (``postgresql+asyncpg://…``) → a plain DSN ``asyncpg.connect`` accepts."""
    return database_url.replace("+asyncpg", "")


class NotifyListener:
    """Generic LISTEN → set an ``asyncio.Event``: owns a dedicated asyncpg connection on one
    channel and pokes ``wakeup`` on each notification. Dropping the connection degrades to the
    caller's poll backstop (latency only, not correctness). Used for both the scheduler and the
    bot outbound worker."""

    def __init__(self, dsn: str, channel: str, wakeup: asyncio.Event) -> None:
        self._dsn = dsn
        self._channel = channel
        self._wakeup = wakeup
        self._conn: asyncpg.Connection | None = None

    async def start(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(self._channel, self._on_notify)

    def _on_notify(self, connection: object, pid: int, channel: str, payload: str) -> None:
        self._wakeup.set()

    async def stop(self) -> None:
        if self._conn is None:
            return
        with suppress(Exception):
            await self._conn.remove_listener(self._channel, self._on_notify)
        with suppress(Exception):
            await self._conn.close()
        self._conn = None


class ScheduledJobListener:
    """Owns the listen connection and pokes ``wakeup`` on each notification."""

    def __init__(self, dsn: str, wakeup: asyncio.Event) -> None:
        self._dsn = dsn
        self._wakeup = wakeup
        self._conn: asyncpg.Connection | None = None

    async def start(self) -> None:
        self._conn = await asyncpg.connect(self._dsn)
        await self._conn.add_listener(CHANNEL, self._on_notify)

    def _on_notify(self, connection: object, pid: int, channel: str, payload: str) -> None:
        # asyncpg listener callback (sync). Just wake the runner; it re-reads the graph.
        self._wakeup.set()

    async def stop(self) -> None:
        if self._conn is None:
            return
        with suppress(Exception):
            await self._conn.remove_listener(CHANNEL, self._on_notify)
        with suppress(Exception):
            await self._conn.close()
        self._conn = None
