# SPDX-License-Identifier: Apache-2.0
"""The LLM seam for the REASON step (D16).

A ``Reasoner`` turns a rendered job context into a §1 output — a ``final_plan`` (or a
``read_request`` in the autonomous loop, a later slice). Three backends:
- ``StubReasoner`` — deterministic, returns a preset plan (tests/dev); the default.
- ``OpenAIReasoner`` — the cheap tier via OpenAI (JSON mode). The provider is behind an
  injectable ``complete`` callable so the reasoner is testable without a network call.
- ``StrongReasoner`` — the strong tier (D17): the same headless subscription-CLI runner the
  bot's escalation uses, reasoning over the GATHERED context only (no MCP access inside the
  reasoner — gather_then_judge already assembled the data; propose-only stays structural).
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

Complete = Callable[[list[dict]], Awaitable[str]]

# Compact system contract for the simple-mode REASON (both tiers). This MUST mirror
# plan.validate_final_plan exactly — the validator is the ground truth, the prompt is its
# advertisement (OQ10 review 2f924059, F1: divergence here gives the model contradictory
# interface rules). When the contract changes, change BOTH in the same commit.
_FINAL_PLAN_SYSTEM = (
    "You are the reasoning step of a scheduled job in a personal assistant. Return ONE JSON "
    "object and nothing else — a final_plan with the shape: "
    '{"type":"final_plan", '
    '"deliver":{"text":str, "urgent":bool (optional)} or omitted, '
    '"writes":[{"op":"create","type":str,"label":str (optional),"properties":object (optional)}'
    ' | {"op":"update","type":str,"node_id":str,"patch":object}] or omitted, '
    '"elicit":{"target_field":str, "question":str (optional), "target_node":str (optional), '
    '"default":str (optional)} or omitted, '
    '"rationale_summary":str (optional)}. '
    "No other keys anywhere: deliver accepts ONLY text and urgent (any recipient/routing key "
    "is rejected — the runner owns delivery routing); elicit accepts ONLY the four keys above; "
    "write ops other than create/update (share, unshare, delete) are rejected. "
    "An empty final_plan (no deliver/writes/elicit) is a valid no-op. You do NOT call tools or "
    "act — a runner executes your plan within a fence; never include an `act` field. "
    "Ground everything in the given context; if data is missing or marked truncated, say so "
    "via a deliver rather than inventing."
)


class ReasonerError(RuntimeError):
    """The reasoner produced un-parseable output (a protocol failure, NOT an intentional
    empty plan) — the runner records it as a FAILED run, never as a quiet no-op."""


@runtime_checkable
class Reasoner(Protocol):
    async def reason(self, context: dict) -> dict: ...


class StubReasoner:
    """Returns a preset plan (tests/dev). With no preset → an empty ``final_plan`` (a no-op)."""

    def __init__(self, plan: dict | None = None) -> None:
        self._plan = plan

    async def reason(self, context: dict) -> dict:
        return self._plan if self._plan is not None else {"type": "final_plan"}


def _render(context: dict) -> list[dict]:
    """Render the job context into chat messages for the cheap-tier REASON."""
    instruction = context.get("instruction", "")
    gathered = context.get("context")
    user = f"Job instruction:\n{instruction}"
    if gathered:
        user += f"\n\nGathered context:\n{gathered}"
    return [
        {"role": "system", "content": _FINAL_PLAN_SYSTEM},
        {"role": "user", "content": user},
    ]


class OpenAIReasoner:
    """Cheap-tier reasoner over OpenAI (JSON mode). ``complete`` is an injectable async callable
    ``(messages) -> raw_json_str`` so this is testable without a network call. Non-JSON output
    raises ``ReasonerError`` — a protocol failure, distinct from an intentional empty plan."""

    def __init__(self, complete: Complete) -> None:
        self._complete = complete

    async def reason(self, context: dict) -> dict:
        raw = await self._complete(_render(context))
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ReasonerError(f"openai reasoner returned non-JSON output: {exc}") from exc


def _strip_json_fences(raw: str) -> str:
    """``claude -p`` has no JSON mode; tolerate a ```json fenced block around the object."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class StrongReasoner:
    """Strong-tier REASON (D16/D17): headless subscription CLI via the bot's ``Runner`` seam.
    Context-only by design — the runner passes no MCP config, so the subprocess cannot reach
    any tool; it judges over the gathered context and returns one JSON ``final_plan``.
    Non-JSON output raises ``ReasonerError`` (a protocol failure, never a silent no-op)."""

    def __init__(self, runner) -> None:  # runner: bot.strong.Runner
        self._runner = runner

    async def reason(self, context: dict) -> dict:
        messages = _render(context)
        raw = await self._runner(messages[0]["content"], messages[1]["content"])
        try:
            return json.loads(_strip_json_fences(raw))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ReasonerError(f"strong reasoner returned non-JSON output: {exc}") from exc


def openai_complete(api_key: str, model: str) -> Complete:
    """Build an OpenAI-backed ``complete`` callable (JSON mode). openai is imported lazily."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)

    async def _complete(messages: list[dict]) -> str:
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""

    return _complete


def get_reasoner() -> Reasoner:
    """The configured reasoner: ``stub`` (default), ``openai`` (cheap tier) or ``strong``
    (subscription CLI, D17 — reuses the bot's strong-tier token/model settings)."""
    from ..config import settings

    kind = settings.scheduler_reasoner
    if kind == "stub":
        return StubReasoner()
    if kind == "openai":
        if not settings.openai_api_key:
            raise ValueError("AM_OPENAI_API_KEY is required for the 'openai' reasoner")
        return OpenAIReasoner(openai_complete(settings.openai_api_key, settings.openai_model))
    if kind == "strong":
        try:
            from ..bot.strong import claude_cli_runner
        except ImportError as exc:  # the bot layer is optional and can be absent
            raise ValueError(
                "the 'strong' reasoner is served by the Telegram bot layer, which is not "
                "part of this distribution; use 'stub' or 'openai'"
            ) from exc

        if not settings.strong_cli_token:
            raise ValueError("AM_STRONG_CLI_TOKEN is required for the 'strong' reasoner")
        return StrongReasoner(
            claude_cli_runner(settings.strong_cli_token, model=settings.strong_model)
        )
    raise ValueError(f"unknown scheduler reasoner: {kind!r}")
