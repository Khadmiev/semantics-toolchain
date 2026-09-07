# SPDX-License-Identifier: Apache-2.0
"""Bearer resolver: a presented token -> the principal it authorizes.

Every MCP request carries an opaque credential. We hash it, look up the row,
check it is neither revoked nor expired, and return the authorization tuple
(account, scopes, allowed_tools, trust) for policy (B4) / access (B2) to act on.
Resolution is the only auth check on the data plane; transport (B5) calls this.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.identity import Credential
from .errors import ExpiredError, InvalidTokenError, RevokedError
from .tokens import hash_token


@dataclass(frozen=True)
class Principal:
    """Who a credential authorizes, and how far. Result of resolving a token."""

    account_id: uuid.UUID
    credential_id: uuid.UUID
    trust: str
    scopes: list | None  # space ids the credential may touch (None = account-wide)
    allowed_tools: list | None  # MCP tools the credential may call (None = all)


async def resolve_credential(
    session: AsyncSession, token: str, *, touch: bool = True
) -> Principal:
    """Resolve a bearer token to its ``Principal``, or raise an ``AuthError``.

    ``touch`` stamps ``last_used_at`` (set False for side-effect-free probes).
    Order matters: revoked is checked before expiry so a revoked-then-stale
    credential still reports the actionable reason.
    """
    credential = await session.scalar(
        select(Credential).where(Credential.token_hash == hash_token(token))
    )
    if credential is None:
        raise InvalidTokenError
    if credential.revoked_at is not None:
        raise RevokedError
    now = datetime.now(UTC)
    if credential.expires_at is not None and credential.expires_at <= now:
        raise ExpiredError

    if touch:
        credential.last_used_at = now
        await session.flush()

    return Principal(
        account_id=credential.account_id,
        credential_id=credential.id,
        trust=credential.trust,
        scopes=credential.scopes,
        allowed_tools=credential.allowed_tools,
    )
