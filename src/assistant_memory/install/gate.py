# SPDX-License-Identifier: Apache-2.0
"""The A-2 gate: not installed — not serving, with the unclosed stage named.

Enforcement sits in the REQUEST PATH, at one seam, never inside individual handlers:
an app-level FastAPI dependency runs for every API route (`install_gate`), and the MCP
transport consults the same admission function before dispatching a tool call
(mcp/server.call_tool). The exemption set — A-3's install surface — is declared HERE,
beside the chokepoint, in one place.

The wire contract (A-2): one stable discriminator across both transports,
``not_installed``. HTTP answers 503 with ``{"error": "not_installed", "stage": ...,
"closes_with": ...}``; MCP answers the protocol's ordinary tool-error envelope carrying
the same three fields as JSON text. Request time is also one of A-5's re-evaluation
points: the admission check records a regression event when it observes one.
"""

import re

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from ..db import get_session
from .state import compute_state, observe_regression

# --- A-3: the install surface, i.e. the exemption set ------------------------------
#
# Membership here is THE declaration A-2 requires. Everything not matched is behind
# the gate. `/mcp` is exempt at the HTTP layer only because the MCP transport runs
# the same admission check at the tool layer — a transport-level 503 there would be
# indistinguishable from an outage to an MCP client (A-2's wire-contract rationale).

_EXEMPT_EXACT = {
    # health endpoints (liveness, diagnostic, readiness view)
    "/health",
    "/health/db",
    "/health/ready",
    # the install-status endpoint (this module's router)
    "/install/status",
    # the owner login flow (browser page, Google OAuth redirect and callback)
    "/login",
    "/login/google",
    "/auth/callback",
    "/logout",
    # the OAuth endpoints an MCP client needs to obtain authorization
    "/oauth/consent",
    "/authorize",
    "/token",
    "/register",
    "/revoke",
}
_EXEMPT_PREFIX = (
    "/.well-known/",  # OAuth discovery (authorization server + protected resource)
    "/mcp",  # gated at the tool layer, same admission function
)
# The authenticated admin space surface (A-3): while installation is incomplete it
# admits ONLY space creation and routing-id repair — listing/viewing spaces, creating
# one, and recording the conventions-home id. Member management is NOT exempt.
_EXEMPT_PATTERNS = (
    (re.compile(r"^/spaces$"), {"GET", "POST"}),
    (re.compile(r"^/spaces/[0-9a-fA-F-]{36}$"), {"GET"}),
    (re.compile(r"^/spaces/[0-9a-fA-F-]{36}/set-conventions-home$"), {"POST"}),
)


def is_install_surface(method: str, path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    if any(path.startswith(p) for p in _EXEMPT_PREFIX):
        return True
    return any(
        rx.match(path) and method in methods for rx, methods in _EXEMPT_PATTERNS
    )


# --- the admission check ------------------------------------------------------------


async def _real_admission(session: AsyncSession) -> dict | None:
    """A-2's admission condition at one re-evaluation point. Returns the refusal
    payload while any of stages 1-4 is unclosed, else None. Also A-5's request-time
    observation: a regression seen here is recorded before the refusal is served."""
    state = await compute_state(session)
    if await observe_regression(session, state):
        await session.commit()
        state = await compute_state(session)
    if state.serving_open:
        return None
    failed = state.first_unclosed
    return {
        "error": "not_installed",
        "stage": failed.name,
        "closes_with": failed.closes_with,
    }


# The one indirection the test suite uses: the suite exercises an installed
# instance's capabilities, so its conftest replaces this with an always-open check;
# the gate's own tests point it back at `_real_admission`. Production never touches it.
admission_check = _real_admission


async def _real_completion(session: AsyncSession) -> dict | None:
    """A-3's COMPLETION boundary: None once the status is `installed`, else the same
    refusal payload shape as admission. Distinct from admission on purpose — the
    admin-space surface is owner-only 'while installation is incomplete', and
    VALIDATING (stage 5 open) is incomplete even though admission (stages 1-4) is
    already open (review f46a31d2, finding
    install-space-owner-guard-opens-before-install-completes)."""
    state = await compute_state(session)
    # The completion boundary is a request-path state computation, so it carries the
    # SAME durable-observation contract as admission: a regression seen here is
    # recorded (generation advanced) before the refusal is served — otherwise a
    # non-owner hitting the guard "sees" a lost fact the store never learns of, and
    # exact restoration re-arms the old proof (review f46a31d2, finding
    # completion-boundary-observation-does-not-retire-proof).
    if await observe_regression(session, state):
        await session.commit()
        state = await compute_state(session)
    if state.status == "installed":
        return None
    # Any non-installed status has an unclosed stage (validating -> stage 5).
    failed = state.first_unclosed
    return {
        "error": "not_installed",
        "stage": failed.name,
        "closes_with": failed.closes_with,
    }


# Same indirection contract as admission_check: the suite's conftest opens it, the
# gate's and guard's own tests point it back at `_real_completion`.
completion_check = _real_completion


class InstallRefusal(Exception):
    """Raised by the gate dependency; rendered by the app's exception handler."""

    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload["stage"])


async def install_gate(
    request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    """The app-level dependency: every API route passes here before its handler."""
    if is_install_surface(request.method, request.url.path):
        return
    refusal = await admission_check(session)
    if refusal is not None:
        raise InstallRefusal(refusal)


def install_refusal_handler(_: Request, exc: InstallRefusal) -> JSONResponse:
    return JSONResponse(exc.payload, status_code=503)


# --- the install-status endpoint (A-3) ----------------------------------------------

router = APIRouter()


@router.get("/install/status")
async def install_status(session: AsyncSession = Depends(get_session)) -> dict:
    """The stage list with each predicate's current answer, plus the release and
    generation facts the status names (A-4): the active installed release, any
    pending target that failed to apply, and the named release refusal."""
    state = await compute_state(session)
    if await observe_regression(session, state):
        await session.commit()
        state = await compute_state(session)
    return {
        "status": state.status,
        "stages": [
            {
                "name": s.name,
                "ok": s.ok,
                "closes_with": s.closes_with,
                "detail": s.detail,
            }
            for s in state.stages
        ],
        "first_unclosed": state.first_unclosed.name if state.first_unclosed else None,
        "generation": state.generation,
        "releases": {
            "executing": state.executing,
            "active": state.active_release,
            "pending": state.pending_release,
            "refusal": state.release_refusal,
        },
    }
