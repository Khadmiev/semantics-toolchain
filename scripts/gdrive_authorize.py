# SPDX-License-Identifier: Apache-2.0
"""One-time: obtain a Google Drive refresh token for the headless backup job.

Reuses the existing web OAuth client (AM_GOOGLE_CLIENT_ID / AM_GOOGLE_CLIENT_SECRET) with the
MINIMAL `drive.file` scope — the backup job only ever touches files IT creates (its own folder),
never the rest of your Drive. Run ONCE on a machine with a browser:

    uv run python scripts/gdrive_authorize.py

Google Console prereqs (one-time, see docs/ops/deploy-bot-to-prod.md): (1) enable the Drive API;
(2) publish the OAuth consent screen to "In production" — in "Testing" the refresh token expires in
7 days; (3) add redirect URI http://localhost:8765/ to the OAuth client.

Prints the refresh token → put it in .env as AM_GDRIVE_REFRESH_TOKEN. No new dependencies (stdlib +
httpx, already a project dep)."""

import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from assistant_memory.config import settings

REDIRECT = "http://localhost:8765/"
PORT = 8765
SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public OAuth endpoint, not a secret

_result: dict[str, str | None] = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _result["code"] = (params.get("code") or [None])[0]
        _result["error"] = (params.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        ok = "Authorization received — close this tab and return to the terminal."
        self.wfile.write((ok if _result.get("code") else f"Error: {_result.get('error')}").encode())

    def log_message(self, *args: object) -> None:  # silence the default request logging
        pass


def main() -> None:
    cid, secret = settings.google_client_id, settings.google_client_secret
    if not cid or not secret:
        sys.exit("AM_GOOGLE_CLIENT_ID / AM_GOOGLE_CLIENT_SECRET not set in .env")

    auth = AUTH_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # force a refresh token even on re-consent
        }
    )
    print("Opening the browser for consent. If it does not open, paste this URL:\n\n" + auth + "\n")
    webbrowser.open(auth)
    HTTPServer(("localhost", PORT), _Handler).handle_request()  # serve exactly the redirect

    if _result.get("error") or not _result.get("code"):
        sys.exit(f"authorization failed: {_result.get('error')}")

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "code": _result["code"],
            "redirect_uri": REDIRECT,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    refresh = resp.json().get("refresh_token")
    if not refresh:
        sys.exit(
            "no refresh_token returned — check the consent screen is 'In production' and that "
            "prompt=consent + access_type=offline were sent (they are)."
        )
    print("\n=== SUCCESS — add this line to prod .env ===")
    print(f"AM_GDRIVE_REFRESH_TOKEN={refresh}")


if __name__ == "__main__":
    main()
