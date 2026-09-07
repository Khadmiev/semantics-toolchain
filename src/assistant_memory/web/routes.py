# SPDX-License-Identifier: Apache-2.0
"""Web routes: Google OAuth login, owner invites, and the admin home.

The OAuth dance is thin — Authlib handles redirect + token exchange; we pull the
verified ``userinfo`` and hand (sub, email) to ``resolve_login``. A pending invite
token (stashed in the session by the redeem landing) lets a not-yet-admitted
identity be created on first login. The client is registered only when credentials
are configured, so the app boots without them and ``/login`` explains what's missing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..auth.errors import AuthError
from ..auth.oauth_provider import oauth_provider
from ..auth.service import (
    create_invite,
    get_or_create_default_account,
    redeem_invite,
    revoke_credential,
)
from ..auth.tokens import hash_token
from ..config import settings
from ..db import get_session
from ..install import state as install_state
from ..models.base import PERMISSIONS
from ..models.install import SETTING_CONVENTIONS_SPACE_ID
from ..models.graph import Node, NodeSpace, NodeType, NodeVersion
from ..models.identity import Account, Credential, Invite, Membership, Space, User
from ..models.policy import Proposal
from ..profile import service as profile_service
from ..proposals import apply_proposal
from ..repository import errors as repo_errors
from ..search import get_embedder, index_node
from .auth import NotAdmitted, current_user, resolve_login
from .csrf import csrf_token, verify_csrf
from .session import login_session, logout_session

_TEMPLATES = Path(__file__).parent / "templates"
router = APIRouter()
templates = Jinja2Templates(
    directory=str(_TEMPLATES),
    context_processors=[lambda request: {"csrf_token": csrf_token(request)}],
)

_GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"
_PENDING_INVITE = "pending_invite"
_NEXT = "post_login_next"


def _safe_next(target: str) -> str:
    """Only allow same-site redirects (guard against open-redirect via ?next=)."""
    return target if target.startswith("/") and not target.startswith("//") else "/"

oauth = OAuth()
_GOOGLE_READY = bool(settings.google_client_id and settings.google_client_secret)
if _GOOGLE_READY:
    oauth.register(
        name="google",
        server_metadata_url=_GOOGLE_METADATA,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )


def _oauth_configured() -> bool:
    return _GOOGLE_READY


async def require_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    """Logged-in user or a 303 redirect to /login (for browser routes)."""
    user = await current_user(session, request)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def require_owner(user: User = Depends(require_user)) -> User:
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="owner only")
    return user


# --- auth ----------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    return templates.TemplateResponse(request, "login.html", {"configured": _oauth_configured()})


@router.get("/login/google")
async def login_google(request: Request, next: str = "/"):
    if not _oauth_configured():
        return RedirectResponse("/login", status_code=303)
    request.session[_NEXT] = _safe_next(next)
    return await oauth.google.authorize_redirect(request, settings.base_url + "/auth/callback")


@router.get("/auth/callback")
async def auth_callback(request: Request, session: AsyncSession = Depends(get_session)):
    token = await oauth.google.authorize_access_token(request)
    userinfo = token.get("userinfo") or {}
    sub = userinfo.get("sub")
    if not sub:
        return RedirectResponse("/login", status_code=303)
    email = userinfo.get("email")
    pending = request.session.pop(_PENDING_INVITE, None)

    # A-5: resolve_login can RESTORE a lost owner binding (ensure_owner re-binds the
    # google_sub) — observe the state as it stands BEFORE the restoring write, so the
    # regression is durably recorded first (review f46a31d2, reopened finding
    # install-surface-state-restoration-still-bypasses-observation).
    pre_state = await install_state.compute_state(session)
    if await install_state.observe_regression(session, pre_state):
        await session.commit()

    try:
        user = await resolve_login(session, sub=sub, email=email)
    except NotAdmitted:
        if not pending:
            return templates.TemplateResponse(
                request, "login.html", {"configured": True, "denied": True}, status_code=403
            )
        try:
            user = await redeem_invite(session, token=pending, google_sub=sub, label=email)
        except AuthError:
            return templates.TemplateResponse(
                request, "login.html", {"configured": True, "denied": True}, status_code=403
            )
    await session.commit()
    login_session(request, user.id)
    return RedirectResponse(_safe_next(request.session.pop(_NEXT, "/")), status_code=303)


# --- OAuth authorization (MCP clients like ChatGPT) ----------------------


@router.get("/oauth/consent", response_class=HTMLResponse)
async def oauth_consent(
    request: Request, txn: str, session: AsyncSession = Depends(get_session)
):
    """Human authorization step of the OAuth flow — gated on the owner's login."""
    user = await current_user(session, request)
    if user is None or not user.is_owner:
        nxt = quote(f"/oauth/consent?txn={txn}", safe="")
        return RedirectResponse(f"/login/google?next={nxt}", status_code=303)
    pending = oauth_provider.pending(txn)
    if pending is None:
        raise HTTPException(status_code=400, detail="unknown or expired authorization request")
    client, _params = pending
    return templates.TemplateResponse(
        request,
        "consent.html",
        {"user": user, "txn": txn, "client_name": client.client_name or client.client_id},
    )


@router.post("/oauth/consent")
async def oauth_consent_submit(
    request: Request,
    txn: str = Form(...),
    csrf: str = Form(...),
    user: User = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    if oauth_provider.pending(txn) is None:
        raise HTTPException(status_code=400, detail="unknown or expired authorization request")
    account = await get_or_create_default_account(session, user)
    await session.commit()
    return RedirectResponse(oauth_provider.finalize(txn, account.id), status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf: str = Form(...)):
    verify_csrf(request, csrf)
    logout_session(request)
    return RedirectResponse("/login", status_code=303)


# --- invites -------------------------------------------------------------


def _invite_status(invite: Invite, now: datetime) -> str:
    if invite.redeemed_at is not None:
        return "redeemed"
    if invite.expires_at is not None and invite.expires_at <= now:
        return "expired"
    return "pending"


@router.get("/invites", response_class=HTMLResponse)
async def invites_page(
    request: Request,
    user: User = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
):
    now = datetime.now(UTC)
    rows = await session.scalars(select(Invite).order_by(Invite.created_at.desc()))
    invites = [{"obj": inv, "status": _invite_status(inv, now)} for inv in rows]
    return templates.TemplateResponse(request, "invites.html", {"user": user, "invites": invites})


@router.post("/invites", response_class=HTMLResponse)
async def invites_create(
    request: Request,
    csrf: str = Form(...),
    email: str = Form(""),
    ttl_days: int = Form(7),
    user: User = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    account = await get_or_create_default_account(session, user)
    issued = await create_invite(
        session,
        created_by=account.id,
        email=email.strip() or None,
        ttl=timedelta(days=ttl_days) if ttl_days > 0 else None,
    )
    await session.commit()

    now = datetime.now(UTC)
    rows = await session.scalars(select(Invite).order_by(Invite.created_at.desc()))
    invites = [{"obj": inv, "status": _invite_status(inv, now)} for inv in rows]
    new_link = f"{settings.base_url}/invite/{issued.token}"
    return templates.TemplateResponse(
        request, "invites.html", {"user": user, "invites": invites, "new_link": new_link}
    )


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_landing(
    request: Request, token: str, session: AsyncSession = Depends(get_session)
):
    invite = await session.scalar(select(Invite).where(Invite.token_hash == hash_token(token)))
    now = datetime.now(UTC)
    valid = (
        invite is not None
        and invite.redeemed_at is None
        and (invite.expires_at is None or invite.expires_at > now)
    )
    if valid:
        request.session[_PENDING_INVITE] = token
    return templates.TemplateResponse(
        request, "invite.html", {"valid": valid, "configured": _oauth_configured()}
    )


# --- credentials (self-service, per account) -----------------------------


def _credential_status(cred: Credential, now: datetime) -> str:
    if cred.revoked_at is not None:
        return "revoked"
    if cred.expires_at is not None and cred.expires_at <= now:
        return "expired"
    return "active"


async def _credentials_context(session: AsyncSession, user: User) -> dict:
    account = await get_or_create_default_account(session, user)
    now = datetime.now(UTC)
    creds = await session.scalars(
        select(Credential)
        .where(Credential.account_id == account.id)
        .order_by(Credential.created_at.desc())
    )
    return {
        "user": user,
        "credentials": [{"obj": c, "status": _credential_status(c, now)} for c in creds],
    }


@router.get("/credentials", response_class=HTMLResponse)
async def credentials_page(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    ctx = await _credentials_context(session, user)
    return templates.TemplateResponse(request, "credentials.html", ctx)


@router.post("/credentials/{cred_id}/revoke")
async def credentials_revoke(
    request: Request,
    cred_id: uuid.UUID,
    csrf: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    account = await get_or_create_default_account(session, user)
    cred = await session.get(Credential, cred_id)
    if cred is None or cred.account_id != account.id:
        raise HTTPException(status_code=404, detail="credential not found")
    await revoke_credential(session, credential_id=cred_id)
    await session.commit()
    return RedirectResponse("/credentials", status_code=303)


# --- spaces & memberships ------------------------------------------------


async def _my_permission(
    session: AsyncSession, account_id: uuid.UUID, space_id: uuid.UUID
) -> str | None:
    return await session.scalar(
        select(Membership.permission).where(
            Membership.account_id == account_id, Membership.space_id == space_id
        )
    )


async def _owner_only_while_not_installed(session: AsyncSession, user: User) -> None:
    """A-3: the exempt admin-space surface is OWNER-authenticated while installation
    is incomplete (spec: 'owner-authenticated, and while installation is incomplete
    admitting only space creation and routing-id repair'). 'Incomplete' means the
    COMPLETION boundary — stage 5 included — so the VALIDATING window is still
    owner-only (finding install-space-owner-guard-opens-before-install-completes).
    After installation the surface behaves as ordinary member UI. Uses the gate's
    completion_check indirection, so the suite's installed-instance bypass leaves
    ordinary tests alone (finding install-space-surface-not-owner-scoped)."""
    if user.is_owner:
        return
    from ..install import gate as install_gate

    if await install_gate.completion_check(session) is not None:
        raise HTTPException(
            status_code=403,
            detail="the install surface admits only the owner while installation "
            "is incomplete",
        )


@router.get("/spaces", response_class=HTMLResponse)
async def spaces_page(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    await _owner_only_while_not_installed(session, user)
    account = await get_or_create_default_account(session, user)
    rows = await session.execute(
        select(Space, Membership.permission)
        .join(Membership, Membership.space_id == Space.id)
        .where(Membership.account_id == account.id)
        .order_by(Space.name)
    )
    spaces = [{"obj": sp, "permission": perm} for sp, perm in rows]
    home_raw = await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    # This page is the NAMED repair surface for a malformed recorded id (the seed
    # predicate's detail sends the operator here) — so it must render in exactly that
    # state, showing the malformed value, with set-conventions-home still usable to
    # replace it (finding malformed-routing-id-breaks-repair-ui).
    home_id = None
    home_malformed: str | None = None
    if home_raw is not None:
        try:
            home_id = uuid.UUID(str(home_raw))
        except ValueError:
            home_malformed = str(home_raw)
    return templates.TemplateResponse(
        request,
        "spaces.html",
        {
            "user": user,
            "spaces": spaces,
            "conventions_home_id": home_id,
            "conventions_home_malformed": home_malformed,
        },
    )


@router.post("/spaces")
async def spaces_create(
    request: Request,
    csrf: str = Form(...),
    name: str = Form(...),
    template: str = Form("personal"),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    await _owner_only_while_not_installed(session, user)
    if not name.strip():
        raise HTTPException(status_code=400, detail="name required")
    # A-5: creating a space can RESTORE the default-space seed fact — observe the
    # pre-write state durably first (same seam as set-conventions-home; review
    # f46a31d2, reopened finding install-surface-state-restoration-still-bypasses-
    # observation).
    pre_state = await install_state.compute_state(session)
    if await install_state.observe_regression(session, pre_state):
        await session.commit()
    account = await get_or_create_default_account(session, user)
    space = Space(name=name.strip(), template=template.strip() or "personal", created_by=account.id)
    session.add(space)
    await session.flush()
    session.add(Membership(account_id=account.id, space_id=space.id, permission="admin"))
    await session.commit()
    return RedirectResponse("/spaces", status_code=303)


@router.post("/spaces/{space_id}/set-conventions-home")
async def set_conventions_home(
    request: Request,
    space_id: uuid.UUID,
    csrf: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Routing-id repair (B.13 A-1/A-3): record this space as the conventions home.

    Part of the admin space surface the install gate exempts, so a missing or
    dangling routing id is repairable while installation is incomplete. Owner-only:
    the routing id names where the conventions live for every account, which is the
    operator's call, not any admitted user's. Verification before the write —
    existence and the owner's own membership — is A-1's adoption rule; identity is
    the recorded id, never a name.
    """
    verify_csrf(request, csrf)
    if not user.is_owner:
        raise HTTPException(status_code=403, detail="the routing id is the owner's to set")
    account = await get_or_create_default_account(session, user)
    # A-5: repair is a state-RESTORING write, so the state as it stands is observed
    # (durably recording any regression, advancing the generation) BEFORE the repair
    # lands — a lost fact must not be restorable faster than its loss is recorded
    # (finding install-surface-bypasses-regression-observation).
    pre_state = await install_state.compute_state(session)
    if await install_state.observe_regression(session, pre_state):
        await session.commit()
    # The SHARED routing-home contract (install_state.routing_home_problem) — the same
    # check the bootstrap's adoption branches and the seed predicate run, so repair
    # cannot record an id the other sites would refuse.
    problem = await install_state.routing_home_problem(session, space_id, account.id)
    if problem == "the space does not exist":
        raise HTTPException(status_code=404, detail="no such space")
    if problem is not None:
        raise HTTPException(
            status_code=403,
            detail=f"ownership verification failed ({problem}) — nothing recorded",
        )
    # The home must HOLD the QUALIFYING conventions projection when one exists
    # anywhere — judged by the SAME shared predicate the seed stage runs
    # (install_state.qualifying_projection_space: current, FTS-indexed,
    # owner-visible), so repair and seed cannot disagree about one space (findings
    # routing-home-not-joined-to-conventions-projection and
    # routing-home-projection-qualification-not-shared). With no qualifying
    # projection anywhere the recording is allowed — the pre-seed repair flow.
    holder = await install_state.qualifying_projection_space(session, account.id)
    if holder is not None:
        in_target = await install_state.qualifying_projection_space(
            session, account.id, space_id=space_id
        )
        if in_target is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "that space holds no qualifying conventions projection — the "
                    f"current projection lives in space {holder}; record THAT "
                    "space, or reseed first if you intend to move the home"
                ),
            )
    await install_state.set_setting(
        session, SETTING_CONVENTIONS_SPACE_ID, str(space_id)
    )
    await session.commit()
    return RedirectResponse("/spaces", status_code=303)


@router.get("/spaces/{space_id}", response_class=HTMLResponse)
async def space_detail(
    request: Request,
    space_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    await _owner_only_while_not_installed(session, user)
    account = await get_or_create_default_account(session, user)
    my_perm = await _my_permission(session, account.id, space_id)
    if my_perm is None:
        raise HTTPException(status_code=404, detail="space not found")
    space = await session.get(Space, space_id)
    rows = await session.execute(
        select(Account, Membership.permission)
        .join(Membership, Membership.account_id == Account.id)
        .where(Membership.space_id == space_id)
        .order_by(Membership.permission)
    )
    members = [{"account": acc, "permission": perm} for acc, perm in rows]
    return templates.TemplateResponse(
        request,
        "space_detail.html",
        {
            "user": user,
            "space": space,
            "members": members,
            "is_admin": my_perm == "admin",
            "permissions": PERMISSIONS,
            "my_account_id": account.id,
        },
    )


async def _require_space_admin(
    session: AsyncSession, user: User, space_id: uuid.UUID
) -> None:
    account = await get_or_create_default_account(session, user)
    if await _my_permission(session, account.id, space_id) != "admin":
        raise HTTPException(status_code=403, detail="space admin only")


@router.post("/spaces/{space_id}/members")
async def space_set_member(
    request: Request,
    space_id: uuid.UUID,
    csrf: str = Form(...),
    account_id: str = Form(...),
    permission: str = Form("read"),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """Add a member or change their permission (upsert). Owner adds by account id
    until proper space-invites land."""
    verify_csrf(request, csrf)
    await _require_space_admin(session, user, space_id)
    if permission not in PERMISSIONS:
        raise HTTPException(status_code=400, detail="invalid permission")
    try:
        target = uuid.UUID(account_id.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid account id") from exc
    if await session.get(Account, target) is None:
        raise HTTPException(status_code=404, detail="account not found")

    membership = await session.scalar(
        select(Membership).where(
            Membership.account_id == target, Membership.space_id == space_id
        )
    )
    if membership is None:
        session.add(Membership(account_id=target, space_id=space_id, permission=permission))
    else:
        membership.permission = permission
    await session.commit()
    return RedirectResponse(f"/spaces/{space_id}", status_code=303)


@router.post("/spaces/{space_id}/members/{account_id}/remove")
async def space_remove_member(
    request: Request,
    space_id: uuid.UUID,
    account_id: uuid.UUID,
    csrf: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    await _require_space_admin(session, user, space_id)
    await session.execute(
        delete(Membership).where(
            Membership.account_id == account_id, Membership.space_id == space_id
        )
    )
    await session.commit()
    return RedirectResponse(f"/spaces/{space_id}", status_code=303)


# --- change feed + undo (core safety net, decisions §7 #8) ---------------

_PERM_ORDER = {"read": 0, "write": 1, "admin": 2}
_HIGH = ("high", "critical")


async def _node_permission(
    session: AsyncSession, account_id: uuid.UUID, node_id: uuid.UUID
) -> str | None:
    """Max permission the account has on a node (across member spaces). Works for
    deleted nodes too (the feed shows them), unlike the access read-path."""
    perms = set(
        await session.scalars(
            select(Membership.permission)
            .join(NodeSpace, NodeSpace.space_id == Membership.space_id)
            .where(Membership.account_id == account_id, NodeSpace.node_id == node_id)
        )
    )
    return max(perms, key=lambda p: _PERM_ORDER[p]) if perms else None


async def _undo_node(session: AsyncSession, node: Node, account_id: uuid.UUID) -> str:
    """Reverse the last change to a node (everything is reversible, #8/#4-write).

    delete -> undelete; the lone create -> soft-delete; otherwise revert to the
    previous version by appending a new version with its content.
    """
    if node.deleted_at is not None:
        node.deleted_at = None
        await session.flush()
        await index_node(session, node, get_embedder())
        return "undeleted"

    # The previous version = the most recent one that is not the current. Excluding
    # current_version_id (rather than trusting created_at order) is robust when
    # versions share a transaction timestamp.
    prev = await session.scalar(
        select(NodeVersion)
        .where(NodeVersion.node_id == node.id, NodeVersion.id != node.current_version_id)
        .order_by(NodeVersion.created_at.desc())
        .limit(1)
    )
    if prev is None:
        node.deleted_at = datetime.now(UTC)
        await session.flush()
        return "deleted"

    restored = NodeVersion(
        node_id=node.id,
        label=prev.label,
        properties=prev.properties,
        source_ref=prev.source_ref,
        author=account_id,
    )
    session.add(restored)
    await session.flush()
    node.label = prev.label
    node.properties = prev.properties
    node.current_version_id = restored.id
    await session.flush()
    await index_node(session, node, get_embedder())
    return "reverted"


@router.get("/feed", response_class=HTMLResponse)
async def feed_page(
    request: Request,
    author: str = "",
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    account = await get_or_create_default_account(session, user)
    space_ids = set(
        await session.scalars(
            select(Membership.space_id).where(Membership.account_id == account.id)
        )
    )
    if not space_ids:
        return templates.TemplateResponse(
            request, "feed.html", {"user": user, "events": [], "authors": [], "author": ""}
        )

    visible = select(NodeSpace.node_id).where(NodeSpace.space_id.in_(space_ids)).distinct()
    author_acc = aliased(Account)
    stmt = (
        select(Node, NodeVersion.created_at, author_acc.label, NodeType.default_sensitivity)
        .join(NodeVersion, NodeVersion.id == Node.current_version_id)
        .join(NodeType, NodeType.type == Node.type)
        .outerjoin(author_acc, author_acc.id == NodeVersion.author)
        .where(Node.id.in_(visible))
    )
    if author:
        try:
            stmt = stmt.where(NodeVersion.author == uuid.UUID(author))
        except ValueError:
            pass
    stmt = stmt.order_by(NodeVersion.created_at.desc()).limit(50)

    events = []
    for node, changed_at, author_label, default_sens in await session.execute(stmt):
        sensitivity = node.sensitivity or default_sens
        events.append(
            {
                "node": node,
                "changed_at": changed_at,
                "author_label": author_label or "—",
                "sensitivity": sensitivity,
                "high": sensitivity in _HIGH,
                "deleted": node.deleted_at is not None,
            }
        )

    author_rows = await session.execute(
        select(Account.id, Account.label)
        .join(NodeVersion, NodeVersion.author == Account.id)
        .join(Node, Node.current_version_id == NodeVersion.id)
        .where(Node.id.in_(visible))
        .distinct()
    )
    authors = [{"id": aid, "label": label or str(aid)} for aid, label in author_rows]
    return templates.TemplateResponse(
        request, "feed.html", {"user": user, "events": events, "authors": authors, "author": author}
    )


@router.post("/feed/undo/{node_id}")
async def feed_undo(
    request: Request,
    node_id: uuid.UUID,
    csrf: str = Form(...),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    account = await get_or_create_default_account(session, user)
    if await _node_permission(session, account.id, node_id) not in ("write", "admin"):
        raise HTTPException(status_code=403, detail="no write permission on this node")
    node = await session.get(Node, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="node not found")
    # Operator-profile total protection (E34): an undo appends a version OUTSIDE
    # the validated profile path — it would silently de-publish a rule
    # (current_version_id != validated_version_id) or delete registry state.
    protected = await profile_service.protected_kind(session, node_id)
    if protected is not None:
        raise HTTPException(
            status_code=403,
            detail=f"this node is operator-profile control-plane data ({protected}) — "
            "undo is refused; use the profile capabilities",
        )
    await _undo_node(session, node, account.id)
    await session.commit()
    return RedirectResponse("/feed", status_code=303)


# --- proposals queue (server-staged confirms) ----------------------------


@router.get("/proposals", response_class=HTMLResponse)
async def proposals_page(
    request: Request,
    user: User = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.scalars(
        select(Proposal).where(Proposal.status == "pending").order_by(Proposal.created_at)
    )
    return templates.TemplateResponse(
        request, "proposals.html", {"user": user, "proposals": list(rows)}
    )


async def _decide_proposal(session: AsyncSession, proposal_id: uuid.UUID) -> Proposal:
    proposal = await session.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="proposal already decided")
    return proposal


@router.post("/proposals/{proposal_id}/approve")
async def proposals_approve(
    request: Request,
    proposal_id: uuid.UUID,
    csrf: str = Form(...),
    user: User = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    account = await get_or_create_default_account(session, user)
    proposal = await _decide_proposal(session, proposal_id)
    try:
        await apply_proposal(session, proposal, account.id)
    except (repo_errors.RepositoryError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=f"could not apply: {exc}") from exc
    proposal.status = "approved"
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = account.id
    await session.commit()
    return RedirectResponse("/proposals", status_code=303)


@router.post("/proposals/{proposal_id}/reject")
async def proposals_reject(
    request: Request,
    proposal_id: uuid.UUID,
    csrf: str = Form(...),
    user: User = Depends(require_owner),
    session: AsyncSession = Depends(get_session),
):
    verify_csrf(request, csrf)
    account = await get_or_create_default_account(session, user)
    proposal = await _decide_proposal(session, proposal_id)
    proposal.status = "rejected"
    proposal.decided_at = datetime.now(UTC)
    proposal.decided_by = account.id
    await session.commit()
    return RedirectResponse("/proposals", status_code=303)


# --- home ----------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "index.html", {"user": user})
