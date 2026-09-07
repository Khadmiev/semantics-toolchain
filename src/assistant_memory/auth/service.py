# SPDX-License-Identifier: Apache-2.0
"""Identity lifecycle: owner bootstrap, accounts, invites, credentials.

Functions flush but do not commit — the caller owns the transaction (same
contract as repository/graph.py). A token's plaintext is returned once at
creation; only its sha256 is persisted.

Roles (decisions §8): the *owner* admits *humans* via single-use invites; a
human self-provisions *accounts* (the access principal); each account issues
opaque bearer *credentials* for its LLM clients.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.identity import Account, Credential, Invite, User
from .errors import (
    AlreadyRedeemedError,
    ExpiredError,
    InvalidTokenError,
    UnknownCredentialError,
)
from .tokens import generate_token, hash_token

DEFAULT_INVITE_TTL = timedelta(days=7)


@dataclass
class IssuedInvite:
    """A freshly created invite plus its one-time plaintext token."""

    token: str
    invite: Invite


@dataclass
class IssuedCredential:
    """A freshly issued credential plus its one-time plaintext token."""

    token: str
    credential: Credential


# --- owner & accounts ----------------------------------------------------


async def create_account(
    session: AsyncSession, *, user_id: uuid.UUID, label: str | None = None
) -> Account:
    """Provision a new account (access principal) for a user."""
    account = Account(user_id=user_id, label=label)
    session.add(account)
    await session.flush()
    return account


async def get_or_create_default_account(session: AsyncSession, user: User) -> Account:
    """The user's earliest account, creating a 'personal' one if they have none."""
    account = await session.scalar(
        select(Account).where(Account.user_id == user.id).order_by(Account.created_at).limit(1)
    )
    if account is None:
        account = await create_account(session, user_id=user.id, label="personal")
    return account


async def ensure_owner(
    session: AsyncSession,
    *,
    google_sub: str | None = None,
    label: str = "owner",
) -> User:
    """Idempotently ensure the configured owner exists, with a default account.

    Safe to call on every startup. Keyed on ``google_sub`` once OAuth is wired
    (B7); before that the ``is_owner`` flag is the key, so re-runs reuse the same
    row. The owner needs an account to author invites/spaces, so we ensure one.
    """
    user: User | None = None
    if google_sub is not None:
        user = await session.scalar(select(User).where(User.google_sub == google_sub))
    if user is None:
        user = await session.scalar(select(User).where(User.is_owner.is_(True)))

    if user is None:
        user = User(google_sub=google_sub, label=label, is_owner=True)
        session.add(user)
        await session.flush()
    else:
        user.is_owner = True
        if google_sub is not None:
            user.google_sub = google_sub
        await session.flush()

    await get_or_create_default_account(session, user)
    return user


# --- invites -------------------------------------------------------------


async def create_invite(
    session: AsyncSession,
    *,
    created_by: uuid.UUID,
    email: str | None = None,
    ttl: timedelta | None = DEFAULT_INVITE_TTL,
) -> IssuedInvite:
    """Mint a single-use admission invite. ``email`` optionally pins the redeemer."""
    token = generate_token()
    invite = Invite(
        token_hash=hash_token(token),
        email=email,
        created_by=created_by,
        expires_at=datetime.now(UTC) + ttl if ttl is not None else None,
    )
    session.add(invite)
    await session.flush()
    return IssuedInvite(token=token, invite=invite)


async def redeem_invite(
    session: AsyncSession,
    *,
    token: str,
    google_sub: str | None = None,
    label: str | None = None,
) -> User:
    """Redeem an invite into a new admitted user (with a default account).

    Raises ``InvalidTokenError`` / ``AlreadyRedeemedError`` / ``ExpiredError``.
    """
    invite = await session.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    if invite is None:
        raise InvalidTokenError
    if invite.redeemed_at is not None:
        raise AlreadyRedeemedError
    now = datetime.now(UTC)
    if invite.expires_at is not None and invite.expires_at <= now:
        raise ExpiredError

    user = User(google_sub=google_sub, label=label or invite.email)
    session.add(user)
    await session.flush()
    invite.redeemed_at = now
    invite.redeemed_by = user.id
    await get_or_create_default_account(session, user)
    await session.flush()
    return user


# --- credentials ---------------------------------------------------------


async def issue_credential(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    label: str | None = None,
    trust: str = "trusted",
    scopes: list | None = None,
    allowed_tools: list | None = None,
    ttl: timedelta | None = None,
    sensitive_capable: bool = False,
) -> IssuedCredential:
    """Issue an opaque bearer credential for an account's LLM client.

    ``scopes`` restricts which spaces the client may touch (subset of the
    account's memberships); ``allowed_tools`` restricts which MCP tools it sees.
    ``None`` on either means "no extra restriction" (account-wide / all tools).
    ``sensitive_capable`` (operator-profile E38, default False) designates a
    credential allowed to receive above-normal-sensitivity preference content —
    an issuance-time operator decision, never a caller claim.
    """
    token = generate_token()
    credential = Credential(
        account_id=account_id,
        token_hash=hash_token(token),
        label=label,
        trust=trust,
        scopes=scopes,
        allowed_tools=allowed_tools,
        expires_at=datetime.now(UTC) + ttl if ttl is not None else None,
        sensitive_capable=sensitive_capable,
    )
    session.add(credential)
    await session.flush()
    return IssuedCredential(token=token, credential=credential)


async def revoke_credential(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> Credential:
    """Revoke a credential (instant). Idempotent; raises if the id is unknown."""
    credential = await session.get(Credential, credential_id)
    if credential is None:
        raise UnknownCredentialError
    if credential.revoked_at is None:
        credential.revoked_at = datetime.now(UTC)
        await session.flush()
    return credential
