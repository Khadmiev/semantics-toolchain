# SPDX-License-Identifier: Apache-2.0
from datetime import timedelta

import pytest
from sqlalchemy import select

from assistant_memory.auth import (
    AlreadyRedeemedError,
    ExpiredError,
    InvalidTokenError,
    RevokedError,
    UnknownCredentialError,
    create_invite,
    ensure_owner,
    generate_token,
    hash_token,
    issue_credential,
    redeem_invite,
    resolve_credential,
    revoke_credential,
)
from assistant_memory.models.identity import Account, User

# --- tokens --------------------------------------------------------------


def test_tokens_unique_and_hash_stable():
    a, b = generate_token(), generate_token()
    assert a != b
    assert hash_token(a) == hash_token(a)
    assert hash_token(a) != hash_token(b)
    # only the hash is stored, and it is not the plaintext
    assert hash_token(a) != a


# --- owner bootstrap -----------------------------------------------------


async def test_ensure_owner_idempotent(session):
    first = await ensure_owner(session, google_sub="sub-123", label="me")
    again = await ensure_owner(session, google_sub="sub-123", label="me")

    assert first.id == again.id
    assert again.is_owner is True
    owners = (await session.scalars(select(User).where(User.is_owner.is_(True)))).all()
    assert len(owners) == 1
    # owner got a default account to author invites/spaces
    account = await session.scalar(select(Account).where(Account.user_id == first.id))
    assert account is not None


# --- invites -------------------------------------------------------------


async def test_redeem_invite_creates_user_and_account(session, account):
    issued = await create_invite(session, created_by=account.id, email="guest@example.com")
    assert issued.invite.redeemed_at is None

    user = await redeem_invite(session, token=issued.token, google_sub="g-sub")
    assert user.google_sub == "g-sub"
    assert user.label == "guest@example.com"  # falls back to the pinned email

    await session.refresh(issued.invite)
    assert issued.invite.redeemed_at is not None
    assert issued.invite.redeemed_by == user.id
    # admitted human gets a default account (>= 1 account invariant)
    acc = await session.scalar(select(Account).where(Account.user_id == user.id))
    assert acc is not None


async def test_redeem_is_single_use(session, account):
    issued = await create_invite(session, created_by=account.id)
    await redeem_invite(session, token=issued.token)
    with pytest.raises(AlreadyRedeemedError):
        await redeem_invite(session, token=issued.token)


async def test_redeem_unknown_token(session):
    with pytest.raises(InvalidTokenError):
        await redeem_invite(session, token="nope")


async def test_redeem_expired_invite(session, account):
    issued = await create_invite(session, created_by=account.id, ttl=timedelta(seconds=-1))
    with pytest.raises(ExpiredError):
        await redeem_invite(session, token=issued.token)


# --- credentials & resolver ----------------------------------------------


async def test_issue_and_resolve_credential(session, account, space):
    issued = await issue_credential(
        session,
        account_id=account.id,
        label="claude",
        trust="trusted",
        scopes=[str(space.id)],
        allowed_tools=["get_node", "create_node"],
    )

    principal = await resolve_credential(session, issued.token)
    assert principal.account_id == account.id
    assert principal.credential_id == issued.credential.id
    assert principal.trust == "trusted"
    assert principal.scopes == [str(space.id)]
    assert principal.allowed_tools == ["get_node", "create_node"]
    # resolving stamps last_used_at
    assert issued.credential.last_used_at is not None


async def test_resolve_unknown_token(session):
    with pytest.raises(InvalidTokenError):
        await resolve_credential(session, "not-a-real-token")


async def test_resolve_revoked_credential(session, account):
    issued = await issue_credential(session, account_id=account.id)
    await revoke_credential(session, credential_id=issued.credential.id)
    with pytest.raises(RevokedError):
        await resolve_credential(session, issued.token)


async def test_resolve_expired_credential(session, account):
    issued = await issue_credential(session, account_id=account.id, ttl=timedelta(seconds=-1))
    with pytest.raises(ExpiredError):
        await resolve_credential(session, issued.token)


async def test_revoke_is_idempotent_and_guards_unknown(session, account):
    import uuid

    issued = await issue_credential(session, account_id=account.id)
    first = await revoke_credential(session, credential_id=issued.credential.id)
    stamp = first.revoked_at
    again = await revoke_credential(session, credential_id=issued.credential.id)
    assert again.revoked_at == stamp  # not re-stamped

    with pytest.raises(UnknownCredentialError):
        await revoke_credential(session, credential_id=uuid.uuid4())
