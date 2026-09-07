# SPDX-License-Identifier: Apache-2.0
"""Opaque bearer tokens: a random secret shown once, only its sha256 stored.

Not JWTs — a token carries no claims, so revocation is instant (delete/flag the
row) and the DB never holds anything that re-derives the secret (architecture
decisions, "credential format"). The same scheme backs both invites and credentials.
"""

import hashlib
import secrets

_TOKEN_BYTES = 32


def generate_token() -> str:
    """A URL-safe random secret (~43 chars). Returned to the client once; never stored."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """sha256 hex of the token — the only form persisted, and the lookup key."""
    return hashlib.sha256(token.encode()).hexdigest()
