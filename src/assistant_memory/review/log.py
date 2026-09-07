# SPDX-License-Identifier: Apache-2.0
"""Reserve-mode local log (spec §9.1, §12).

Written ONLY in degraded manual-reserve mode, when the service store is unreachable
and a local append-only file is the sole record of the observed timeline (reconciled
into the store on recovery). In the normal path the store is authoritative and the
readable view is rendered on demand (render.render_timeline) — nothing is written here.

Append-only, human-readable, one entry per message. `review_<slug>_<shortid>.md` under
a configured reviews directory (§15 knob).
"""

from pathlib import Path

from .render import Message, render_message

DEFAULT_REVIEWS_DIR = "reviews"


def _shortid(review_id) -> str:
    return str(review_id).replace("-", "")[:8]


def reserve_log_path(review_id, slug: str, *, reviews_dir: str | Path = DEFAULT_REVIEWS_DIR) -> Path:
    """`<reviews_dir>/review_<slug>_<shortid>.md` (spec §9.1 naming)."""
    safe_slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in slug) or "review"
    return Path(reviews_dir) / f"review_{safe_slug}_{_shortid(review_id)}.md"


def append_messages(path: str | Path, messages: list[Message]) -> Path:
    """Append rendered entries to the reserve log, creating the file + parents if needed.

    Append-only: never rewrites existing entries (INV-9 — the local file is authoritative
    for the observed timeline while the store is unreachable). Returns the path written.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists()
    with p.open("a", encoding="utf-8") as fh:
        if new_file:
            fh.write("# Review reserve log (service unreachable — sole record until sync)\n\n")
        for m in messages:
            fh.write(render_message(m) + "\n")
    return p
