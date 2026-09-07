# SPDX-License-Identifier: Apache-2.0
"""MCP `create_review` tool — the POST /reviews twin on the data plane.

Covers: happy path (review + both tokens minted, tokens resolve to the SAME review),
trust gate (untrusted credential denied), input validation (bad mode / empty slug),
and catalog/schema/dispatch registration.
"""

import pytest

from assistant_memory.auth import issue_credential, resolve_credential
from assistant_memory.mcp import catalog, tools
from assistant_memory.mcp.errors import InvalidArgument, PermissionDenied
from assistant_memory.review.repository import resolve_review_token
from tests.instrument_helpers import seed_instruments


async def _principal(session, account, *, trust="trusted"):
    issued = await issue_credential(session, account_id=account.id, trust=trust)
    return await resolve_credential(session, issued.token)


async def test_create_review_mints_review_and_tokens(session, account):
    principal = await _principal(session, account)

    result = await tools.create_review(
        session,
        principal,
        slug="test-spec-review",
        mode="spec",
        artifact_ref={"repo": "assistant_memory", "commit": "abc1234"},
        config={"critic": "codex"},
        instrument=await seed_instruments(session),
    )

    assert result["status"] == "ok"
    assert result["state"] == "created"
    assert result["dev_token"] != result["critic_token"]

    dev = await resolve_review_token(session, result["dev_token"])
    critic = await resolve_review_token(session, result["critic_token"])
    assert str(dev.review_id) == result["review_id"]
    assert dev.review_id == critic.review_id  # both authorize exactly this review
    assert dev.role == "development"
    assert critic.role == "critic"


async def test_create_review_requires_trusted_credential(session, account):
    principal = await _principal(session, account, trust="untrusted")

    with pytest.raises(PermissionDenied):
        await tools.create_review(session, principal, slug="nope", mode="spec")


async def test_create_review_rejects_bad_input(session, account):
    principal = await _principal(session, account)

    with pytest.raises(InvalidArgument):
        await tools.create_review(session, principal, slug="x", mode="prose")
    with pytest.raises(InvalidArgument):
        await tools.create_review(session, principal, slug="   ", mode="spec")


def test_create_review_registered_everywhere():
    # The import-time assert in server.py already guards drift; this pins the intent.
    from assistant_memory.mcp.server import SCHEMAS

    assert "create_review" in catalog.BY_NAME
    assert catalog.BY_NAME["create_review"].kind == "write"
    assert "create_review" in tools.HANDLERS
    assert set(SCHEMAS["create_review"]["required"]) == {"slug", "mode"}
