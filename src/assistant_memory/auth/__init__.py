# SPDX-License-Identifier: Apache-2.0
"""Identity & auth: owner bootstrap, invites, credentials, bearer resolution.

The data plane (MCP) authenticates an opaque bearer *credential* of an account;
the control plane (web admin, B7) authenticates a *human* via Google OAuth and
runs the invite lifecycle. This package is the service layer over the identity
tables — transport (FastAPI/MCP) wires it in at the edge (B5).
"""

from .errors import (
    AlreadyRedeemedError,
    AuthError,
    ExpiredError,
    InvalidTokenError,
    RevokedError,
    UnknownCredentialError,
)
from .resolver import Principal, resolve_credential
from .service import (
    DEFAULT_INVITE_TTL,
    IssuedCredential,
    IssuedInvite,
    create_account,
    create_invite,
    ensure_owner,
    get_or_create_default_account,
    issue_credential,
    redeem_invite,
    revoke_credential,
)
from .tokens import generate_token, hash_token

__all__ = [
    "DEFAULT_INVITE_TTL",
    "AlreadyRedeemedError",
    "AuthError",
    "ExpiredError",
    "InvalidTokenError",
    "IssuedCredential",
    "IssuedInvite",
    "Principal",
    "RevokedError",
    "UnknownCredentialError",
    "create_account",
    "create_invite",
    "ensure_owner",
    "generate_token",
    "get_or_create_default_account",
    "hash_token",
    "issue_credential",
    "redeem_invite",
    "resolve_credential",
    "revoke_credential",
]
