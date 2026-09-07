# SPDX-License-Identifier: Apache-2.0
"""Settings invariants. The load-bearing one: the agent's write path is only safe under
trust='limited' (OQ7) — any other trust lets a normal `allow` write apply silently instead of
staging a proposal, so the app must refuse to start rather than run in that state."""

import pytest
from pydantic import ValidationError

from assistant_memory.config import Settings


def test_write_requires_limited_trust_rejects_trusted() -> None:
    with pytest.raises(ValidationError):
        Settings(bot_agent_write=True, bot_agent_trust="trusted")


def test_write_requires_limited_trust_rejects_untrusted() -> None:
    with pytest.raises(ValidationError):
        Settings(bot_agent_write=True, bot_agent_trust="untrusted")


def test_write_with_limited_trust_is_allowed() -> None:
    s = Settings(bot_agent_write=True, bot_agent_trust="limited")
    assert s.bot_agent_write and s.bot_agent_trust == "limited"


def test_read_only_agent_allows_any_trust() -> None:
    # trust is not consulted on the read path, so a non-limited value is fine when write is OFF
    s = Settings(bot_agent_write=False, bot_agent_trust="trusted")
    assert s.bot_agent_trust == "trusted"


# session_secret guard: the default secret makes admin session cookies forgeable, so a "real"
# deployment (OAuth client configured, or non-local base_url) must refuse to start with it.
# _env_file=None keeps the developer's local .env out of these constructions.

_DEFAULT_SECRET = "dev-insecure-change-me"


def test_default_secret_rejected_when_oauth_configured() -> None:
    with pytest.raises(ValidationError):
        Settings(
            session_secret=_DEFAULT_SECRET, google_client_id="x.apps.example", _env_file=None
        )


def test_default_secret_rejected_on_public_base_url() -> None:
    with pytest.raises(ValidationError):
        Settings(
            session_secret=_DEFAULT_SECRET, base_url="https://memory.example.dev", _env_file=None
        )


def test_default_secret_allowed_for_bare_local_dev() -> None:
    # no OAuth client + localhost base_url: no login path mints cookies, default is harmless
    s = Settings(
        session_secret=_DEFAULT_SECRET,
        base_url="http://localhost:8000",
        google_client_id=None,
        _env_file=None,
    )
    assert s.session_secret == _DEFAULT_SECRET


def test_real_secret_passes_with_oauth_and_public_url() -> None:
    s = Settings(
        session_secret="a-long-random-real-secret",
        google_client_id="x.apps.example",
        base_url="https://memory.example.dev",
        _env_file=None,
    )
    assert s.session_secret != _DEFAULT_SECRET
