# SPDX-License-Identifier: Apache-2.0
"""The sanctioned launcher's launch journal: monotonic numbers and an exclusive lock.

The journal answers a question the run record cannot: not "is what was written true" but
"was everything written". A record proves it was not edited afterwards; it says nothing
about what was never put in it. A monotonic counter answers that — with a gap.

WHAT THIS DEFENDS AGAINST, AND WHAT IT DOES NOT. It catches an unrecorded run that
happened by oversight: the extra launch moves the counter, the numbers stop being
adjacent, and the binding breaks. It does NOT defend against someone editing the journal
afterwards to hide a run, and it is not meant to — under the declared build horizon the
environment is local and single-user, and there is no adversary in the model (operator
ruling 2026-08-04). An earlier draft chained the lines by hash and published the segment
for recomputation; that was removed as over-building against a threat the horizon
excludes. If the genre ever moves to an untrusted environment, the question returns with it.

TWO PLACEMENT RULES, both load-bearing:

- the journal lives OUTSIDE the settings directory. The read-only sandbox forbids the
  blind reader to write, not to read, so a log of project runs sitting in its own settings
  directory would be one more channel of knowledge about the project;
- the LOCK file lives outside it too, and for a second reason: the profile fingerprint is
  taken over the directory's contents, so a lock file created inside it would change the
  fingerprint mid-run and break the very binding it exists to protect.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JournalLocked(RuntimeError):
    """Another launch holds the profile. Never stolen silently — see ``LaunchJournal.launch``."""


@dataclass(frozen=True)
class JournalEntry:
    """One launch, as recorded at the moment it started."""

    number: int
    profile_digest: str
    started_at: datetime

    def as_line(self) -> str:
        return json.dumps(
            {
                "number": self.number,
                "profile_digest": self.profile_digest,
                "started_at": self.started_at.isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_line(cls, line: str) -> JournalEntry:
        raw = json.loads(line)
        return cls(
            number=int(raw["number"]),
            profile_digest=str(raw["profile_digest"]),
            started_at=datetime.fromisoformat(raw["started_at"]),
        )


class LaunchJournal:
    """Append-only launch log for one profile, with an exclusive launch lock.

    ``path`` must not sit inside the profile's settings directory (see the module note);
    the caller is responsible for that placement, and ``guard_placement`` makes the mistake
    loud rather than subtle.
    """

    def __init__(self, path: Path, *, now: Callable[[], datetime] = _utc_now) -> None:
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._now = now

    # --- placement ---------------------------------------------------------

    def guard_placement(self, settings_dir: Path) -> None:
        """Refuse a journal (or lock) placed inside the profile it logs.

        Checked rather than documented: this is the kind of mistake that stays invisible
        until it silently changes a fingerprint or leaks a run log to the blind reader,
        and both failures surface far from their cause.
        """
        settings = settings_dir.resolve()
        for candidate, what in ((self.path, "журнал"), (self.lock_path, "замок")):
            resolved = candidate.resolve()
            if resolved == settings or settings in resolved.parents:
                raise ValueError(
                    f"{what} запусков лежит внутри каталога настроек ({resolved}): "
                    "он попадёт в отпечаток профиля и станет виден слепому читателю"
                )

    # --- reading -----------------------------------------------------------

    def entries(self) -> list[JournalEntry]:
        if not self.path.exists():
            return []
        return [
            JournalEntry.from_line(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def next_number(self) -> int:
        entries = self.entries()
        return entries[-1].number + 1 if entries else 1

    # --- writing -----------------------------------------------------------

    @contextmanager
    def launch(
        self, profile_digest: str, *, settings_dir: Path | None = None
    ) -> Iterator[JournalEntry]:
        """Take the exclusive launch lock, append the entry, and hold the lock for the run.

        The entry is written when the run STARTS, not when it finishes: a launch that
        crashes must still consume its number, or a crashed run becomes an invisible one —
        precisely the case the counter exists to catch.

        A held lock is never stolen. If the previous holder died, the lock file remains and
        this raises with its contents, so the operator decides — an automatic steal would
        turn "another launch is running" into "another launch was running, probably", and
        that guess is exactly what must not be made silently.
        """
        if settings_dir is not None:
            self.guard_placement(settings_dir)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = ""
            try:
                held = self.lock_path.read_text(encoding="utf-8").strip()
            except OSError:  # unreadable lock is still a held lock
                held = "<не читается>"
            raise JournalLocked(
                f"профиль занят другим запуском (замок {self.lock_path}): {held}. "
                "Замок не отбирается автоматически — решает оператор"
            ) from None
        try:
            # THE NUMBER IS READ UNDER THE LOCK, never before it. Read outside, two launchers
            # could both see the same "next" one and the second would append a duplicate after
            # the first released — destroying exactly the uniqueness the counter exists to
            # prove. The declared operating scale makes this reachable rather than theoretical:
            # up to five reviews may run at once, and they share one blind profile's journal.
            entry = JournalEntry(
                number=self.next_number(),
                profile_digest=profile_digest,
                started_at=self._now(),
            )
            os.write(handle, f"pid={os.getpid()} number={entry.number} "
                             f"started_at={entry.started_at.isoformat()}\n".encode())
            os.close(handle)
            with self.path.open("a", encoding="utf-8") as journal:
                journal.write(entry.as_line() + "\n")
            yield entry
        finally:
            try:
                os.close(handle)
            except OSError:
                pass  # already closed on the happy path
            self.lock_path.unlink(missing_ok=True)
