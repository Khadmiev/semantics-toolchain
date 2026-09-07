# SPDX-License-Identifier: Apache-2.0
"""Daily Google Drive backup of the graph DB: ``pg_dump -Fc`` uploaded to an app-owned Drive folder,
keeping only the N most recent dumps. Runs INSIDE the app container (the DB is on the compose
network); the HOST scheduler (Windows Task Scheduler) fires it daily:

    docker exec assistant_memory_app python -m assistant_memory.ops.gdrive_backup

Auth: the existing OAuth client (AM_GOOGLE_CLIENT_ID/SECRET) + a ``drive.file`` refresh token
(AM_GDRIVE_REFRESH_TOKEN from scripts/gdrive_authorize.py). ``drive.file`` grants access ONLY to
files this job created, so it sees/owns just its own backup folder — never the rest of the Drive.
No new dependencies (stdlib + httpx). pg_dump (v16, matching the DB) is baked into the image."""

import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config import settings

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public OAuth endpoint, not a secret
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


def _access_token() -> str:
    """Exchange the long-lived refresh token for a short-lived access token."""
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": settings.gdrive_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _pg_dump(dest: Path) -> None:
    """Custom-format dump of the whole DB. pg_dump needs a libpq URL (strip the +asyncpg driver)."""
    url = settings.database_url.replace("+asyncpg", "")
    subprocess.run(["pg_dump", "-Fc", "-d", url, "-f", str(dest)], check=True)  # noqa: S603,S607


def _folder_id(client: httpx.Client, name: str) -> str:
    """Find the job's own backup folder (drive.file only sees folders IT created) or create it."""
    q = f"name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    r = client.get(f"{DRIVE}/files", params={"q": q, "fields": "files(id,name)", "spaces": "drive"})
    r.raise_for_status()
    found = r.json().get("files", [])
    if found:
        return found[0]["id"]
    r = client.post(f"{DRIVE}/files", json={"name": name, "mimeType": FOLDER_MIME})
    r.raise_for_status()
    return r.json()["id"]


def _upload(client: httpx.Client, folder_id: str, path: Path) -> str:
    """Resumable upload (safe for dumps larger than a few MB)."""
    init = client.post(
        f"{UPLOAD}?uploadType=resumable",
        json={"name": path.name, "parents": [folder_id]},
        headers={"X-Upload-Content-Type": "application/octet-stream"},
    )
    init.raise_for_status()
    session_uri = init.headers["Location"]
    put = client.put(
        session_uri, content=path.read_bytes(), headers={"Content-Type": "application/octet-stream"}
    )
    put.raise_for_status()
    return put.json()["id"]


def _rotate(client: httpx.Client, folder_id: str, keep: int) -> list[str]:
    """Keep the ``keep`` newest dumps in the folder, delete the rest."""
    r = client.get(
        f"{DRIVE}/files",
        params={
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "files(id,name,createdTime)",
            "orderBy": "createdTime desc",
        },
    )
    r.raise_for_status()
    deleted = []
    for f in r.json().get("files", [])[keep:]:
        client.delete(f"{DRIVE}/files/{f['id']}").raise_for_status()
        deleted.append(f["name"])
    return deleted


def main() -> None:
    if not settings.gdrive_refresh_token:
        sys.exit("AM_GDRIVE_REFRESH_TOKEN not set — run scripts/gdrive_authorize.py first")
    token = _access_token()
    with tempfile.TemporaryDirectory() as tmp:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        dump = Path(tmp) / f"assistant_memory_{stamp}.dump"
        _pg_dump(dump)
        size_mb = dump.stat().st_size / 1e6
        with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=180) as client:
            folder_id = _folder_id(client, settings.gdrive_folder_name)
            file_id = _upload(client, folder_id, dump)
            deleted = _rotate(client, folder_id, settings.gdrive_keep_last)
    print(
        f"backup uploaded: {dump.name} ({size_mb:.1f} MB) id={file_id}; "
        f"kept {settings.gdrive_keep_last}, deleted {len(deleted)}: {deleted}"
    )


if __name__ == "__main__":
    main()
