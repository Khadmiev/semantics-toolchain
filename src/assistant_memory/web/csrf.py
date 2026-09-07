# SPDX-License-Identifier: Apache-2.0
"""Per-session CSRF token for HTMX/HTML forms.

A random token is stored in the signed session and echoed in a hidden field;
state-changing POSTs must send it back. Same-origin forms have it; a cross-site
forgery cannot read the session cookie's contents to forge a matching field.
"""

import hmac
import secrets

from fastapi import HTTPException
from starlette.requests import Request

_CSRF = "csrf"


def csrf_token(request: Request) -> str:
    token = request.session.get(_CSRF)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF] = token
    return token


def verify_csrf(request: Request, submitted: str | None) -> None:
    expected = request.session.get(_CSRF)
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="CSRF check failed")
