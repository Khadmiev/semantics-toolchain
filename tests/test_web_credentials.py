# SPDX-License-Identifier: Apache-2.0
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from assistant_memory.auth.service import issue_credential
from assistant_memory.models.identity import Account, Credential
from assistant_memory.web.routes import _credential_status


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)


async def _owner_account(session, owner) -> Account:
    return await session.scalar(select(Account).where(Account.user_id == owner.id))


# --- status (unit) -------------------------------------------------------


def test_credential_status() -> None:
    now = datetime.now(UTC)
    assert _credential_status(Credential(), now) == "active"
    assert _credential_status(Credential(expires_at=now - timedelta(seconds=1)), now) == "expired"
    assert _credential_status(Credential(revoked_at=now), now) == "revoked"


# --- listing / revoke (integration harness) ------------------------------
# Tokens are minted only by the OAuth flow now — there is no manual issue form.
# The admin page lists connected clients (OAuth-issued credentials) and revokes them.


async def test_credentials_page_lists_oauth_credentials(owner_client, session):
    client, owner = owner_client
    account = await _owner_account(session, owner)
    await issue_credential(session, account_id=account.id, label="ChatGPT (oauth)")
    await session.commit()

    page = await client.get("/credentials")
    assert page.status_code == 200
    assert "ChatGPT (oauth)" in page.text
    assert "Issue" not in page.text  # no manual issuance affordance


async def test_revoke_credential_end_to_end(owner_client, session):
    client, owner = owner_client
    account = await _owner_account(session, owner)
    issued = await issue_credential(session, account_id=account.id, label="claude (oauth)")
    await session.commit()
    assert issued.credential.revoked_at is None

    page = await client.get("/credentials")
    csrf = _csrf_from(page.text)
    resp = await client.post(
        f"/credentials/{issued.credential.id}/revoke", data={"csrf": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    await session.refresh(issued.credential)
    assert issued.credential.revoked_at is not None


async def test_revoke_rejects_bad_csrf(owner_client, session):
    client, owner = owner_client
    account = await _owner_account(session, owner)
    issued = await issue_credential(session, account_id=account.id, label="x")
    await session.commit()

    await client.get("/credentials")
    resp = await client.post(
        f"/credentials/{issued.credential.id}/revoke", data={"csrf": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    await session.refresh(issued.credential)
    assert issued.credential.revoked_at is None
