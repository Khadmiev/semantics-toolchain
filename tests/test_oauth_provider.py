# SPDX-License-Identifier: Apache-2.0
from mcp.shared.auth import OAuthClientInformationFull

from assistant_memory.auth.oauth_provider import _credential_label


def _client(**kw) -> OAuthClientInformationFull:
    base = {"client_id": "cid-123", "redirect_uris": ["https://example.test/cb"]}
    base.update(kw)
    return OAuthClientInformationFull.model_validate(base)


def test_credential_label_uses_client_name() -> None:
    assert _credential_label(_client(client_name="Claude Code")) == "Claude Code (oauth)"


def test_credential_label_falls_back_to_client_id() -> None:
    # no client_name -> label from the client_id, still distinguishable
    assert _credential_label(_client()) == "cid-123 (oauth)"
    assert _credential_label(_client(client_name="   ")) == "cid-123 (oauth)"
