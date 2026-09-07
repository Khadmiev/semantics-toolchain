# SPDX-License-Identifier: Apache-2.0
"""The scheduler's service identity (D19 ``component_id``).

A dedicated account the resident runner attributes its own bookkeeping writes to (claim /
finish on ScheduledJob nodes). This is the runner's SERVICE identity for audit — NOT the
effective access account, which (when REASON/ACT land) is the job's owning account. Access
and policy must key on the effective account, never on this service identity. Create-or-get,
idempotent for a single runner.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.identity import Account, User

SERVICE_USER_LABEL = "__scheduler_service__"
SERVICE_ACCOUNT_LABEL = "scheduler"


async def ensure_scheduler_account(session: AsyncSession) -> uuid.UUID:
    """Return the scheduler service account id, creating the service User + Account once."""
    acc = await session.scalar(
        select(Account)
        .join(User, Account.user_id == User.id)
        .where(User.label == SERVICE_USER_LABEL, Account.label == SERVICE_ACCOUNT_LABEL)
    )
    if acc is not None:
        return acc.id
    user = await session.scalar(select(User).where(User.label == SERVICE_USER_LABEL))
    if user is None:
        user = User(label=SERVICE_USER_LABEL, is_owner=False)
        session.add(user)
        await session.flush()
    acc = Account(user_id=user.id, label=SERVICE_ACCOUNT_LABEL)
    session.add(acc)
    await session.flush()
    return acc.id
