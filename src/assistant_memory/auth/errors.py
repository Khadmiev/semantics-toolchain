# SPDX-License-Identifier: Apache-2.0
class AuthError(Exception):
    """Base class for auth-layer errors (the edge maps these to 401/403)."""


class InvalidTokenError(AuthError):
    """No invite/credential matches the presented token."""


class ExpiredError(AuthError):
    """The invite or credential has passed its expiry."""


class AlreadyRedeemedError(AuthError):
    """The invite was already redeemed (single-use)."""


class RevokedError(AuthError):
    """The credential has been revoked (instant, server-side)."""


class UnknownCredentialError(AuthError):
    """Admin referenced a credential id that does not exist."""
