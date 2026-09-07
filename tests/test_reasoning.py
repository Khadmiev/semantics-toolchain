# SPDX-License-Identifier: Apache-2.0
"""Reasoner seam: OpenAI cheap-tier + strong-tier reasoners (injectable, no network/CLI)
+ get_reasoner."""

import json

import pytest

from assistant_memory.scheduler.reasoning import (
    OpenAIReasoner,
    ReasonerError,
    StrongReasoner,
    StubReasoner,
    get_reasoner,
)


async def test_openai_reasoner_parses_final_plan():
    async def fake(_messages):
        return json.dumps({"type": "final_plan", "deliver": {"text": "hi"}})

    out = await OpenAIReasoner(fake).reason({"instruction": "say hi"})
    assert out["type"] == "final_plan"
    assert out["deliver"]["text"] == "hi"


async def test_openai_reasoner_bad_json_raises():
    async def fake(_messages):
        return "not json {"

    with pytest.raises(ReasonerError):
        await OpenAIReasoner(fake).reason({"instruction": "x"})


async def test_openai_reasoner_renders_instruction_and_context():
    seen = {}

    async def fake(messages):
        seen["messages"] = messages
        return "{}"

    await OpenAIReasoner(fake).reason({"instruction": "do the thing", "context": "weather=14C"})
    assert seen["messages"][0]["role"] == "system"
    user = seen["messages"][1]["content"]
    assert "do the thing" in user
    assert "weather=14C" in user


async def test_strong_reasoner_parses_plain_and_fenced_json():
    async def plain(_system, _user, **_kw):
        return json.dumps({"type": "final_plan", "deliver": {"text": "strong"}})

    out = await StrongReasoner(plain).reason({"instruction": "x"})
    assert out["deliver"]["text"] == "strong"

    async def fenced(_system, _user, **_kw):
        return '```json\n{"type": "final_plan", "deliver": {"text": "fenced"}}\n```'

    out = await StrongReasoner(fenced).reason({"instruction": "x"})
    assert out["deliver"]["text"] == "fenced"


async def test_backend_failures_propagate_uncaught():
    """Transport/backend errors are NOT swallowed by the reasoners — the runner journals
    them as a failed occurrence (F5, review dc694faf)."""

    async def broken_complete(_messages):
        raise RuntimeError("openai: connection error")

    with pytest.raises(RuntimeError, match="connection error"):
        await OpenAIReasoner(broken_complete).reason({"instruction": "x"})

    async def broken_runner(_system, _user, **_kw):
        raise RuntimeError("claude CLI not found on PATH")

    with pytest.raises(RuntimeError, match="CLI not found"):
        await StrongReasoner(broken_runner).reason({"instruction": "x"})


async def test_strong_reasoner_non_json_raises_and_context_rendered():
    seen = {}

    async def chatty(system, user, **_kw):
        seen["system"], seen["user"] = system, user
        return "I think the answer is probably fine."

    with pytest.raises(ReasonerError):
        await StrongReasoner(chatty).reason({"instruction": "survey", "context": "data=42"})
    assert "final_plan" in seen["system"]
    assert "survey" in seen["user"]
    assert "data=42" in seen["user"]


def test_get_reasoner_stub_and_openai_key_required(monkeypatch):
    from assistant_memory.config import settings

    monkeypatch.setattr(settings, "scheduler_reasoner", "stub")
    assert isinstance(get_reasoner(), StubReasoner)

    monkeypatch.setattr(settings, "scheduler_reasoner", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(ValueError):
        get_reasoner()

    monkeypatch.setattr(settings, "scheduler_reasoner", "bogus")
    with pytest.raises(ValueError):
        get_reasoner()


def test_get_reasoner_strong_requires_token(monkeypatch):
    # The strong reasoner is served by the bot layer, which is optional: a distribution
    # assembled without it cannot build one, and the test says so instead of failing.
    pytest.importorskip("assistant_memory.bot.strong")
    from assistant_memory.config import settings

    monkeypatch.setattr(settings, "scheduler_reasoner", "strong")
    monkeypatch.setattr(settings, "strong_cli_token", "")
    with pytest.raises(ValueError):
        get_reasoner()

    monkeypatch.setattr(settings, "strong_cli_token", "tok")
    assert isinstance(get_reasoner(), StrongReasoner)
