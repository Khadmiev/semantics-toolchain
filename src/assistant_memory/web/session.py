# SPDX-License-Identifier: Apache-2.0
"""Signed-cookie session helpers (over Starlette's SessionMiddleware).

The session holds only the logged-in user's id; everything else is loaded fresh.
"""

import uuid

from starlette.requests import Request

_UID = "uid"


def login_session(request: Request, user_id: uuid.UUID) -> None:
    request.session[_UID] = str(user_id)


def current_uid(request: Request) -> uuid.UUID | None:
    raw = request.session.get(_UID)
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def logout_session(request: Request) -> None:
    request.session.clear()
