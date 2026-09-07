# SPDX-License-Identifier: Apache-2.0
"""Shared B.9 C+D test ground: a proven launch profile + model-list entries + the
instrument block review creation now requires (D-1).

Since B.9 ``create_review`` refuses without an instrument whose host × engine resolves
to an active, host-proven profile version (C-5) and whose models resolve to list
entries (D-4). Nearly every review test needs that ground once; this helper seeds it
idempotently inside the test's rolled-back session and returns the instrument block to
pass to ``create_review`` — with independence step ``different_provider`` (anthropic
development vs openai critic), so no self-check waiver is needed.
"""

from assistant_memory.review import instruments

TEST_HOSTNAME = "TESTBOX"
TEST_USERNAME = "tester"


def make_content(**over) -> dict:
    # Commands use forward slashes and invoke the recorded engine binary by its
    # absolute path — the round-1 contract (engine-binary binding; POSIX tokenization
    # in both executors strips backslashes).
    base = {
        # B.14 A-1: the profile names where the review MACHINERY lives; the subject
        # under review is a separate creation input (see seed_instruments below).
        "machinery_root": "C:\\proj\\repo",
        "python": "C:\\py\\python.exe",
        "engine_binary": "C:/bin/codex.exe",
        "token_dir": "C:\\proj\\repo\\.review_tokens",
        "projection_root": "C:\\proj\\projections",
        "base_url": "http://localhost:8000",
        "shell": "cmd",
        "probes": [{"name": "engine_version", "cmd": "C:/bin/codex.exe --version"}],
        "codex_cmd": "C:/bin/codex.exe exec -s read-only -",
    }
    base.update(over)
    return base


def make_verification(hostname=TEST_HOSTNAME, username=TEST_USERNAME, effort="high") -> dict:
    """A complete D-4 verification-run record (round 1: structured, not 'any dict')."""
    return {
        "at": "2026-08-18T00:00:00Z",
        "hostname": hostname,
        "username": username,
        "engine_accepted": True,
        "sandbox_held": True,
        "effort_checked": effort,
    }


def make_evidence(
    hostname=TEST_HOSTNAME, username=TEST_USERNAME, probe_names=None, tool_versions=None
) -> dict:
    return {
        "hostname": hostname,
        "username": username,
        "at": "2026-08-18T00:00:00Z",
        "results": [
            {"name": n, "passed": True} for n in (probe_names or ["engine_version"])
        ],
        # B.12 D-2: which tool version the probes passed on. Required of every stored
        # version, so the shared helper carries it and each test that cares overrides it.
        "tool_versions": (
            {"codex": "0.147.0"} if tool_versions is None else tool_versions
        ),
    }


async def _ensure_model(session, **kw):
    existing = await instruments.list_engine_models(session, kw["engine"])
    for row in existing:
        if row.invocation_alias == kw["invocation_alias"]:
            return row
    return await instruments.add_engine_model(session, **kw)


async def seed_instruments(
    session,
    *,
    hostname: str = TEST_HOSTNAME,
    username: str = TEST_USERNAME,
    engine: str = "codex",
    critic_model: str = "gpt-5.2-codex",
    effort: str | None = "high",
    dev_engine: str = "claude-code",
    dev_model: str = "claude-fable-5",
    content_over: dict | None = None,
) -> dict:
    """Seed profile + model entries (idempotent per session); return the instrument block."""
    profile = await instruments.upsert_profile(
        session, hostname=hostname, username=username, engine=engine
    )
    if profile.active_version_id is None:
        await instruments.add_profile_version(
            session,
            profile_id=profile.id,
            content=make_content(**(content_over or {})),
            probe_evidence=make_evidence(hostname, username),
        )
    await _ensure_model(
        session,
        engine=engine,
        invocation_alias=critic_model,
        provider="openai",
        provider_model_id=critic_model,
        family="gpt",
        effort_domain=["low", "medium", "high", "xhigh"] if effort else None,
        entry_class="verified",
        verification=make_verification(hostname, username, effort=effort),
    )
    await _ensure_model(
        session,
        engine=dev_engine,
        invocation_alias=dev_model,
        provider="anthropic",
        provider_model_id=dev_model,
        family="claude",
        entry_class="facts_only",
    )
    return {
        "host": {"hostname": hostname, "username": username},
        "critic": {"engine": engine, "model": critic_model, "effort": effort},
        "development": {"engine": dev_engine, "model": dev_model},
        # B-5: creation freezes the operator-confirmed threat context with the rest of
        # the gate's settlements — creation without it is refused.
        "threat_context": {
            "threat_model": "test deployment: single user, no foreign input",
            "operating_scale": "test scale: one channel, one reader",
            "granted_by": "test operator, pre-gate",
        },
        # B.14 A-3: the subject root is a required creation input for every review —
        # named even when it equals the machinery root, never defaulted.
        "subject_root": "C:\\proj\\repo",
    }
