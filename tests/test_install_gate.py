# SPDX-License-Identifier: Apache-2.0
"""The A-2 gate and the A-3 install surface (B.13).

The suite at large runs with the gate held open (conftest `_installed_admission`);
these tests point `admission_check` back at the real function and exercise the
actual admission path: the wire contract of the refusal, the exemption set, and
the install-status endpoint.
"""

import pytest

from assistant_memory.install import gate as install_gate
from assistant_memory.install.gate import is_install_surface

pytestmark = pytest.mark.asyncio


@pytest.fixture
def real_gate(monkeypatch):
    monkeypatch.setattr(
        install_gate, "admission_check", install_gate._real_admission
    )


@pytest.fixture
def unconfigured(monkeypatch):
    from assistant_memory.config import settings

    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_client_secret", None)
    monkeypatch.setattr(settings, "owner_email", None)
    monkeypatch.setattr(settings, "session_secret", "dev-insecure-change-me")
    monkeypatch.delenv("AM_GIT_COMMIT", raising=False)


# --- the exemption set (A-3), declared in one place --------------------------------


def test_install_surface_membership():
    # the named install surface
    assert is_install_surface("GET", "/health")
    assert is_install_surface("GET", "/health/ready")
    assert is_install_surface("GET", "/install/status")
    assert is_install_surface("GET", "/login")
    assert is_install_surface("GET", "/login/google")
    assert is_install_surface("GET", "/auth/callback")
    assert is_install_surface("GET", "/oauth/consent")
    assert is_install_surface("POST", "/token")
    assert is_install_surface("GET", "/.well-known/oauth-authorization-server")
    # the admin space surface: creation and routing-id repair only
    assert is_install_surface("GET", "/spaces")
    assert is_install_surface("POST", "/spaces")
    sid = "12345678-1234-1234-1234-123456789abc"
    assert is_install_surface("GET", f"/spaces/{sid}")
    assert is_install_surface("POST", f"/spaces/{sid}/set-conventions-home")
    # NOT exempt: member management, substantive surfaces
    assert not is_install_surface("POST", f"/spaces/{sid}/members")
    assert not is_install_surface("GET", "/feed")
    assert not is_install_surface("GET", "/invites")
    assert not is_install_surface("GET", "/")
    assert not is_install_surface("GET", "/reviews")


# --- the HTTP refusal (wire contract) ----------------------------------------------


async def test_http_refusal_names_stage_and_close(owner_client, real_gate, unconfigured):
    client, _ = owner_client
    resp = await client.get("/feed")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "not_installed"
    assert body["stage"] == "config"
    assert "configuration" in body["closes_with"]


async def test_install_surface_serves_while_not_installed(
    owner_client, real_gate, unconfigured
):
    client, _ = owner_client
    # The install surface answers; the status endpoint reports the stage list.
    resp = await client.get("/install/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "installing"
    assert [s["name"] for s in body["stages"]] == [
        "schema", "config", "seed", "owner", "validated",
    ]
    assert body["first_unclosed"] == "config"
    assert body["releases"]["refusal"] is not None  # no AM_GIT_COMMIT in tests
    # health stays served too
    assert (await client.get("/health")).status_code == 200


async def test_admin_space_surface_serves_while_not_installed(
    owner_client, real_gate, unconfigured
):
    client, _ = owner_client
    assert (await client.get("/spaces")).status_code == 200
