# SPDX-License-Identifier: Apache-2.0
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from sqlalchemy import text
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse

from .auth.oauth_provider import oauth_provider
from .bootstrap import bootstrap, validate_configured_spaces
from .config import settings
from .db import SessionLocal, engine
from .install.gate import (
    InstallRefusal,
    install_gate,
    install_refusal_handler,
)
from .install.gate import router as install_router
from .install.residents import InstallCheckpoint, supervise_residents
from .install.state import compute_state, observe_regression, reconcile_release
from .mcp.server import mcp_asgi_app, session_manager
from .review.routes import router as review_router
from .scheduler.notify import to_asyncpg_dsn
from .scheduler.runner import SchedulerRunner
from .search import get_embedder, pending_embeddings, write_embeddings
from .web.routes import router as web_router

logger = logging.getLogger(__name__)


async def _validate_config() -> None:
    """Configured-but-dangling space ids kill the boot. Deliberately NOT inside
    ``_run_bootstrap``: that one swallows every failure into a not-ready observable, which
    is right for a seeding hiccup and wrong for a configuration error nobody would notice.
    """
    async with SessionLocal() as session:
        await validate_configured_spaces(session)


async def _run_bootstrap() -> None:
    """Structural bootstrap (IR-1): guaranteed + gates readiness, but must not fail boot.

    Cheap (no model), so it runs to completion before we serve; a failure is logged
    and swallowed here — /health/ready then reports not-ready (an observable), while
    liveness /health stays ok.
    """
    try:
        async with SessionLocal() as session:
            await bootstrap(session)
            await session.commit()
    except Exception:  # noqa: BLE001 - never fail boot; readiness reflects the failure
        logger.exception("bootstrap failed — /health/ready will report not ready")


async def _warm_embedder() -> bool:
    """Load the embedding model up front (in a thread; no DB writes). Returns warm-ok.

    Avoids a cold-start on the first write. get_embedder() and embed() are blocking,
    so they run OFF the event loop — calling them on the loop would starve uvicorn
    during a cold-cache model download.
    """

    def _load_and_warm() -> str:
        embedder = get_embedder()
        embedder.embed(["warmup"])
        return type(embedder).__name__

    try:
        name = await asyncio.to_thread(_load_and_warm)
        logger.info("embedder warmed: %s", name)
        return True
    except Exception:  # noqa: BLE001 - warmup is best-effort, must not block boot
        logger.exception("embedder warmup failed (continuing; first write will load it)")
        return False


async def _backfill_embeddings() -> None:
    """Fill embeddings for nodes the bootstrap seeded FTS-only (semantic ranking catches
    up; full-text search already works). Best-effort; embeds off the loop, then commits."""
    try:
        async with SessionLocal() as session:
            pending = await pending_embeddings(session)
        if not pending:
            return
        vectors = await asyncio.to_thread(get_embedder().embed, [t for _, t in pending])
        ids = [vid for vid, _ in pending]
        async with SessionLocal() as session:
            await write_embeddings(session, list(zip(ids, vectors, strict=True)))
            await session.commit()
        logger.info("backfilled %d embeddings", len(pending))
    except Exception:  # noqa: BLE001 - best-effort; search still works via full-text
        logger.exception("embedding backfill failed (search still works via full-text)")


async def _backfill_resident(_stop: asyncio.Event) -> None:
    """The backfill half as a GATED resident (B.13 A-2): it writes product rows, so
    it waits for the install gate like everything else; the warm half ran early (it
    touches a model cache, not product data). One transactional batch — its own unit;
    a no-op when nothing is pending, so restarts after a gate reopen are free."""
    await _backfill_embeddings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # The streamable-HTTP session manager must run for the app's lifetime.
    async with session_manager.run():
        # Configuration first — a dangling space id is a hard stop, not a degraded start.
        await _validate_config()
        # A-4's release compare/promote step, then A-5's startup observation: a
        # regression seen at boot is recorded before anything serves. Both write
        # through their own session; a failure degrades the observable, never the boot.
        try:
            async with SessionLocal() as session:
                await reconcile_release(session)
                await observe_regression(session, await compute_state(session))
                await session.commit()
        except Exception:  # noqa: BLE001 - the status surface will report the state
            logger.exception("startup release/regression pass failed")
        # Structural bootstrap next (cheap, gates readiness); then the early embedder
        # warm-up (a model cache, not product data — A-2 allows it before the gate).
        await _run_bootstrap()
        warm = asyncio.create_task(_warm_embedder())

        # B.13 A-2: the enumerated resident consumers, each classified against the
        # gate. All three touch product data, so none starts while stages 1-4 do not
        # hold, and each re-evaluates the shared checkpoint before its own unit of
        # work (the embedding backfill batch, the scheduler tick, the bot poll
        # iteration). The supervisor starts them when the gate first holds, stops
        # them on a regression, and restarts them when it holds again.
        checkpoint = InstallCheckpoint(SessionLocal)
        residents: list[tuple] = [
            # (name, factory, declared lifecycle — see install/residents.RESIDENT_KINDS)
            ("embedding-backfill", _backfill_resident, "one_shot"),
        ]
        if settings.scheduler_enabled:
            runner = SchedulerRunner(
                SessionLocal,
                rescan_seconds=settings.scheduler_rescan_seconds,
                lease_seconds=settings.scheduler_lease_seconds,
                notify_dsn=to_asyncpg_dsn(settings.database_url),
                unit_gate=checkpoint.holds,
            )
            residents.append(("scheduler", runner.run, "long_running"))
            logger.info("actionable-layer scheduler enabled (install-gated)")
        if settings.bot_enabled and settings.bot_token:
            # Imported here, not at module load: the bot is an optional layer, and a
            # distribution assembled without it must still start the server.
            from .bot.accounts import parse_chat_accounts
            from .bot.delivery import parse_quiet_hours
            from .bot.inbound import parse_chat_ids
            from .bot.service import BotService

            bot_agent = None
            if settings.bot_agent_enabled and settings.openai_api_key:
                from .bot.agent import ConversationAgent, openai_chat
                from .bot.prompts import (
                    CONVERSATION_AGENT_SYSTEM_DRAFT,
                    CONVERSATION_AGENT_SYSTEM_WRITE_DRAFT,
                )

                system_prompt = (
                    CONVERSATION_AGENT_SYSTEM_WRITE_DRAFT
                    if settings.bot_agent_write
                    else CONVERSATION_AGENT_SYSTEM_DRAFT
                )
                bot_agent = ConversationAgent(
                    openai_chat(settings.openai_api_key, settings.openai_model),
                    system_prompt=system_prompt,
                    trust=settings.bot_agent_trust,
                    write_enabled=settings.bot_agent_write,
                )
                logger.info("actionable-layer bot: conversation agent enabled")
            bot_strong = None
            if settings.bot_strong_enabled and settings.strong_cli_token:
                from .bot.credential import strong_readonly_minter
                from .bot.prompts import STRONG_TIER_SYSTEM_DRAFT
                from .bot.strong import StrongTier, claude_cli_runner

                bot_strong = StrongTier(
                    claude_cli_runner(settings.strong_cli_token, model=settings.strong_model),
                    system_prompt=STRONG_TIER_SYSTEM_DRAFT,
                    mcp_url=settings.strong_mcp_url,
                    minter=strong_readonly_minter(SessionLocal),
                )
                logger.info(
                    "actionable-layer bot: strong-tier escalation enabled (graph=%s)",
                    bool(settings.strong_mcp_url),
                )
            bot = BotService(
                SessionLocal,
                token=settings.bot_token,
                operator_chat_ids=parse_chat_ids(settings.bot_operator_chat_ids),
                chat_accounts=parse_chat_accounts(settings.bot_chat_accounts),
                owner_google_sub=settings.owner_google_sub,
                agent=bot_agent,
                strong=bot_strong,
                quiet=parse_quiet_hours(settings.bot_quiet_hours, settings.bot_timezone),
                notify_dsn=to_asyncpg_dsn(settings.database_url),
                poll_seconds=settings.bot_poll_seconds,
                unit_gate=checkpoint.holds,
            )
            residents.append(("bot", bot.run, "long_running"))
            logger.info("actionable-layer bot enabled (install-gated)")

        supervisor_stop = asyncio.Event()
        supervisor = asyncio.create_task(
            supervise_residents(checkpoint, supervisor_stop, residents)
        )
        try:
            yield
        finally:
            warm.cancel()
            with suppress(BaseException):
                await warm
            # Graceful: the supervisor's loop exits on the event (its wait wakes
            # immediately), and its own finally stops every child resident.
            supervisor_stop.set()
            with suppress(BaseException):
                await supervisor


# The A-2 gate runs as an app-level dependency: every API route passes the admission
# check before its handler; the exemption set (A-3's install surface) is declared in
# install/gate.py beside the check. The /mcp mount is gated at the tool layer with the
# same admission function (mcp/server.call_tool).
app = FastAPI(
    title="Assistant Memory",
    version="0.0.1",
    lifespan=lifespan,
    dependencies=[Depends(install_gate)],
)
app.add_exception_handler(InstallRefusal, install_refusal_handler)
# Signed-cookie sessions for the web admin (OAuth state + logged-in user).
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness — does not touch the database."""
    return {"status": "ok"}


@app.get("/health/db")
async def health_db() -> dict[str, str]:
    """Diagnostic — pings Postgres."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok", "db": "ok"}


@app.get("/health/ready")
async def health_ready() -> JSONResponse:
    """Readiness as a VIEW of the install state machine (B.13 A-8): ok exactly when
    stages 1-4 hold — one definition of readiness, not two. The container healthcheck
    keys on LIVENESS (/health) per A-3, so the tunnel rises on a fresh install; this
    endpoint remains for the runbook and for anything that wants the stricter answer.
    503 with the first unclosed stage named until then.

    Reading the state here is also one of A-5's observation points: a regression this
    view computes is durably recorded, not merely reported (finding
    install-surface-bypasses-regression-observation — an exempt read must not know
    about a degradation the store does not)."""
    async with SessionLocal() as session:
        state = await compute_state(session)
        if await observe_regression(session, state):
            await session.commit()
            state = await compute_state(session)
    if not state.serving_open:
        failed = state.first_unclosed
        return JSONResponse(
            {"status": "not_ready", "stage": failed.name}, status_code=503
        )
    return JSONResponse({"status": "ok"})


# Install surface: the status endpoint (A-3).
app.include_router(install_router)

# Web admin (control plane): human login + management UI.
app.include_router(web_router)

# Review orchestration (spec docs/design/2026-07-07_review_orchestration_spec.md):
# the dev<->critic review bus. Per-review token auth on the data plane; the
# bootstrap credential on POST /reviews + the console.
app.include_router(review_router)

# --- OAuth for MCP clients (ChatGPT etc.) --------------------------------
# Discovery + authorization-server + dynamic client registration + token/revoke,
# and a bearer gate on /mcp that answers 401 + WWW-Authenticate (which is what
# makes an OAuth-only client discover how to authenticate).
_issuer = AnyHttpUrl(settings.base_url)
_resource_url = AnyHttpUrl(f"{settings.base_url}/mcp")

for _route in create_auth_routes(
    oauth_provider,
    issuer_url=_issuer,
    client_registration_options=ClientRegistrationOptions(enabled=True),
    revocation_options=RevocationOptions(enabled=True),
):
    app.router.routes.append(_route)

for _route in create_protected_resource_routes(
    resource_url=_resource_url, authorization_servers=[_issuer]
):
    app.router.routes.append(_route)

# MCP data plane: bearer required (OAuth-issued token == an account credential).
_mcp_authed = AuthenticationMiddleware(
    AuthContextMiddleware(
        RequireAuthMiddleware(
            mcp_asgi_app,
            required_scopes=[],
            resource_metadata_url=build_resource_metadata_url(_resource_url),
        )
    ),
    backend=BearerAuthBackend(ProviderTokenVerifier(oauth_provider)),
)
app.mount("/mcp", _mcp_authed)
