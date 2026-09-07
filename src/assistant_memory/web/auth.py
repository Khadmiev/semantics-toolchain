# SPDX-License-Identifier: Apache-2.0
"""Login resolution: a Google identity -> the User it logs in as.

Kept separate from the OAuth dance (routes.py) so it is pure and testable. Two
ways in (decisions §8): the configured owner is bootstrapped on first login;
anyone else must already be admitted (their google_sub is on a User created by
redeeming an owner invite). Unknown identities are refused — no self-signup.
"""


from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from ..auth.errors import AuthError
from ..auth.service import ensure_owner
from ..config import settings
from ..models.identity import User
from .session import current_uid


class NotAdmitted(AuthError):
    """The Google identity is neither the owner nor a previously-admitted user."""


def is_configured_owner(sub: str | None, email: str | None) -> bool:
    by_sub = bool(settings.owner_google_sub and sub == settings.owner_google_sub)
    by_email = bool(settings.owner_email and email and email == settings.owner_email)
    return by_sub or by_email


async def resolve_login(session: AsyncSession, *, sub: str, email: str | None = None) -> User:
    """The User for this Google identity, bootstrapping the owner. Flushes."""
    if is_configured_owner(sub, email):
        return await ensure_owner(session, google_sub=sub)
    user = await session.scalar(select(User).where(User.google_sub == sub))
    if user is None:
        raise NotAdmitted("not admitted — an owner invite is required")
    return user


async def current_user(session: AsyncSession, request: Request) -> User | None:
    uid = current_uid(request)
    if uid is None:
        return None
    return await session.get(User, uid)


def is_owner(user: User | None) -> bool:
    return user is not None and bool(user.is_owner)
