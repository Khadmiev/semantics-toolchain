# SPDX-License-Identifier: Apache-2.0
"""OAuth 2.1 Authorization Server for the MCP data plane (single-owner).

Lets OAuth-only MCP clients (e.g. ChatGPT) connect: they discover our metadata,
dynamically register, send the human through an authorization step, and receive a
bearer access token. We keep it minimal by leaning on what already exists:

- the human authorization step is our existing Google admin login (owner-gated),
  reached via a consent page (see web/routes.py ``/oauth/consent``);
- the issued access token IS an ordinary account *credential* — so ``/mcp`` token
  validation stays ``resolve_credential`` and the owner can revoke it in the admin.

Registered clients persist in the DB (survive restarts); short-lived authorization
codes and pending requests live in memory.
"""

import secrets
import time
import uuid

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models.identity import Credential
from ..models.oauth import OAuthClient
from .errors import AuthError
from .resolver import resolve_credential
from .service import get_or_create_default_account, issue_credential, revoke_credential
from .tokens import hash_token

_CODE_TTL = 300  # seconds


def _credential_label(client: OAuthClientInformationFull) -> str:
    """Name the issued credential after the OAuth client, so tokens are distinguishable."""
    name = (client.client_name or "").strip() or client.client_id
    return f"{name} (oauth)"


class OwnerOAuthProvider:
    """OAuthAuthorizationServerProvider backed by our credentials + Google login."""

    def __init__(self) -> None:
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, tuple[OAuthClientInformationFull, AuthorizationParams]] = {}

    # --- clients (DCR) ---------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with SessionLocal() as session:
            row = await session.get(OAuthClient, client_id)
            return OAuthClientInformationFull.model_validate(row.data) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with SessionLocal() as session:
            session.add(
                OAuthClient(
                    client_id=client_info.client_id,
                    data=client_info.model_dump(mode="json"),
                )
            )
            await session.commit()

    # --- authorization: defer the human step to our consent page ---------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = (client, params)
        return f"{settings.base_url}/oauth/consent?txn={txn}"

    def pending(self, txn: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        return self._pending.get(txn)

    def finalize(self, txn: str, account_id: uuid.UUID) -> str:
        """Owner approved: mint an authorization code and return the client redirect."""
        client, params = self._pending.pop(txn)
        code_value = f"ac_{secrets.token_urlsafe(24)}"
        self._codes[code_value] = AuthorizationCode(
            code=code_value,
            scopes=params.scopes or [],
            expires_at=time.time() + _CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=str(account_id),
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code_value, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        if code.expires_at < time.time():
            self._codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        account_id = uuid.UUID(authorization_code.subject)
        # Label the credential after the OAuth client (from DCR), so the owner can tell
        # tokens apart in the admin and revoke them individually.
        async with SessionLocal() as session:
            issued = await issue_credential(
                session,
                account_id=account_id,
                label=_credential_label(client),
                trust="trusted",
            )
            await session.commit()
        scope = " ".join(authorization_code.scopes) if authorization_code.scopes else None
        return OAuthToken(access_token=issued.token, token_type="Bearer", scope=scope)

    # --- token validation (access token = a credential) ------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with SessionLocal() as session:
            try:
                principal = await resolve_credential(session, token, touch=False)
            except AuthError:
                return None
        return AccessToken(
            token=token, client_id="mcp", scopes=[], expires_at=None,
            subject=str(principal.account_id),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with SessionLocal() as session:
            cred = await session.scalar(
                select(Credential).where(Credential.token_hash == hash_token(token.token))
            )
            if cred is not None:
                await revoke_credential(session, credential_id=cred.id)
                await session.commit()

    # --- refresh tokens: not used (access tokens do not expire) ----------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        raise NotImplementedError("refresh tokens are not supported")


# Shared singleton — used by the auth routes (main.py) and the consent page.
oauth_provider = OwnerOAuthProvider()


async def owner_account_id(user) -> uuid.UUID:
    """The account whose credential the OAuth token maps to (the owner's default)."""
    async with SessionLocal() as session:
        account = await get_or_create_default_account(session, user)
        await session.commit()
        return account.id
