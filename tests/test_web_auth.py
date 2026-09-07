# SPDX-License-Identifier: Apache-2.0
import pytest
from fastapi.testclient import TestClient

from assistant_memory.config import settings
from assistant_memory.main import app
from assistant_memory.models.identity import User
from assistant_memory.web.auth import NotAdmitted, resolve_login

# --- login resolution (pure) --------------------------------------------


async def test_resolve_login_bootstraps_owner(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_email", "me@example.com")
    monkeypatch.setattr(settings, "owner_google_sub", None)

    user = await resolve_login(session, sub="google-123", email="me@example.com")
    assert user.is_owner is True
    assert user.google_sub == "google-123"


async def test_resolve_login_admitted_user(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_email", "me@example.com")
    monkeypatch.setattr(settings, "owner_google_sub", None)
    session.add(User(google_sub="sub-abc", label="guest"))
    await session.flush()

    user = await resolve_login(session, sub="sub-abc", email="guest@example.com")
    assert user.label == "guest"
    assert user.is_owner is False


async def test_resolve_login_unknown_is_refused(session, monkeypatch):
    monkeypatch.setattr(settings, "owner_email", "me@example.com")
    monkeypatch.setattr(settings, "owner_google_sub", None)
    with pytest.raises(NotAdmitted):
        await resolve_login(session, sub="stranger", email="nobody@example.com")


# --- routes --------------------------------------------------------------


# NOTE: no `with` — entering the app lifespan runs the MCP session manager, which
# may start only once per process (test_mcp_server owns that single run). These
# routes need only SessionMiddleware + get_session, neither of which needs lifespan.


def test_home_requires_login() -> None:
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_page_renders() -> None:
    client = TestClient(app)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Assistant Memory" in resp.text
