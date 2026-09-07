# SPDX-License-Identifier: Apache-2.0
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from assistant_memory.main import app
from assistant_memory.models.identity import Invite
from assistant_memory.web.csrf import csrf_token, verify_csrf
from assistant_memory.web.routes import _invite_status


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)


class _FakeRequest:
    def __init__(self) -> None:
        self.session: dict = {}


# --- csrf ----------------------------------------------------------------


def test_csrf_roundtrip() -> None:
    req = _FakeRequest()
    token = csrf_token(req)
    assert token and req.session["csrf"] == token
    verify_csrf(req, token)  # no raise


def test_csrf_rejects_wrong_and_missing() -> None:
    req = _FakeRequest()
    csrf_token(req)
    with pytest.raises(HTTPException):
        verify_csrf(req, "wrong")
    with pytest.raises(HTTPException):
        verify_csrf(_FakeRequest(), "anything")  # no expected token


# --- invite status -------------------------------------------------------


def test_invite_status() -> None:
    now = datetime.now(UTC)
    assert _invite_status(Invite(), now) == "pending"
    assert _invite_status(Invite(expires_at=now - timedelta(seconds=1)), now) == "expired"
    assert _invite_status(Invite(redeemed_at=now), now) == "redeemed"
    # redeemed wins even if also past expiry
    redeemed = Invite(redeemed_at=now, expires_at=now - timedelta(days=1))
    assert _invite_status(redeemed, now) == "redeemed"


# --- route gates ---------------------------------------------------------


def test_invites_requires_login() -> None:
    client = TestClient(app)
    resp = client.get("/invites", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_logout_requires_csrf() -> None:
    client = TestClient(app)
    resp = client.post("/logout", data={"csrf": "bogus"}, follow_redirects=False)
    assert resp.status_code == 403


def test_invalid_invite_landing() -> None:
    client = TestClient(app)
    resp = client.get("/invite/not-a-real-token")
    assert resp.status_code == 200
    assert "invalid" in resp.text.lower()


# --- route write path (integration harness) ------------------------------


async def test_owner_creates_invite_end_to_end(owner_client, session):
    client, _owner = owner_client

    page = await client.get("/invites")
    assert page.status_code == 200
    csrf = _csrf_from(page.text)

    resp = await client.post(
        "/invites", data={"csrf": csrf, "email": "guest@example.com", "ttl_days": "7"}
    )
    assert resp.status_code == 200
    assert "/invite/" in resp.text  # one-time link shown

    count = await session.scalar(select(func.count()).select_from(Invite))
    assert count == 1


async def test_create_invite_rejects_bad_csrf(owner_client, session):
    client, _owner = owner_client
    await client.get("/invites")  # establishes a session csrf token
    resp = await client.post("/invites", data={"csrf": "forged", "ttl_days": "7"})
    assert resp.status_code == 403
    assert await session.scalar(select(func.count()).select_from(Invite)) == 0
