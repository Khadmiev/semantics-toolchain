# SPDX-License-Identifier: Apache-2.0
"""Raw material of the operator profile: exchange pairs, the outbox, the cache.

The section of the spec on assembling the operator profile:

* the raw material is WHOLE exchange pairs (wording → signal → unfolding →
  reaction); halves are not written — the quality of a rendering cannot be
  reconstructed from the operator's replies alone;
* when the graph is unavailable the pair is recorded immediately in a durable
  local outbox and sent on when the graph is back: the graph does not block the
  cycle and the raw material does not die (the operator's act of 2026-09-01);
* the outbox is the same class of data as the run catalogues: inside the
  repository it is admissible only under a git ignore (tree hygiene — the
  operator's decision; safety is closed by publishing a separate clean
  repository, R1.1);
* it is development that writes the raw material; the critic writes nothing to
  the graph — by construction this module has no caller in the critic's code.

The cache of the compiled profile is refreshed from the graph's canon; its version
is content-addressed, so the file is rewritten only when the version changes.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .errors import HarnessError

BUFFER_FILENAME = "pair_buffer.jsonl"


def _fsync_dir(dirpath: Path) -> None:
    """Durability of a file name: fsync of the directory entry (POSIX).

    Without it a "durably written" pair can vanish on a power cut, and a deleted one
    can come back and go to the graph a second time (round 10). On Windows renames
    are journalled by NTFS.
    """
    if os.name != "posix":
        return
    dfd = os.open(dirpath, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@dataclass
class ExchangePair:
    """One exchange pair with the operator — whole."""

    agent_phrasing: str          # the agent's wording
    operator_signal: str         # the operator's signal (verbatim)
    expansion: str               # the agent's unfolding (empty when there was none)
    operator_reaction: str       # the operator's reaction to the unfolding (empty when there was none)
    # Ambiguous signals are stored ambiguous: the labelling is nullable and gets
    # finished when the profile is rebuilt; confidence is not squeezed out of an
    # observation.
    signal_reading: str | None = None
    captured_at: str = ""

    def validate(self) -> None:
        # Types before meaning: after JSON corruption a field can become a number, and
        # .strip() would give a raw AttributeError instead of a refusal (round 10).
        for fname in ("agent_phrasing", "operator_signal", "expansion",
                      "operator_reaction", "captured_at"):
            if not isinstance(getattr(self, fname), str):
                raise HarnessError(
                    f"Field {fname} of the pair has the wrong type",
                    cause=f"a string was expected, got {type(getattr(self, fname)).__name__}",
                    next_action="repair the outbox line or delete it",
                )
        if self.signal_reading is not None and not isinstance(self.signal_reading, str):
            raise HarnessError(
                "Field signal_reading of the pair has the wrong type",
                cause="a string or null was expected",
                next_action="repair the outbox line or delete it",
            )
        if not self.agent_phrasing.strip() or not self.operator_signal.strip():
            raise HarnessError(
                "The exchange pair is incomplete: no agent wording or no operator signal",
                cause="halves of pairs are not written — the quality of a rendering cannot be recovered from them",
                next_action="pass the pair whole or write nothing",
            )
        if self.operator_reaction.strip() and not self.expansion.strip():
            raise HarnessError(
                "The exchange pair contradicts itself: a reaction to an unfolding with no unfolding",
                cause="the operator's reaction is meaningful only relative to the unfolding",
                next_action="pass the unfolding or remove the reaction",
            )
        if self.expansion.strip() and not self.operator_reaction.strip():
            # Symmetry of wholeness: an unfolding with no reaction is half a pair, and from
            # it the profile compiler cannot say whether the rendering helped. If a reaction
            # genuinely never came — write that down in explicit words; it is a fact of the
            # exchange, not an emptiness.
            raise HarnessError(
                "The exchange pair is incomplete: an unfolding with no operator reaction",
                cause="from half a pair there is no telling whether the rendering worked",
                next_action=(
                    "wait for the reaction, or write «(no reaction came)» explicitly"
                ),
            )


class GraphClient(Protocol):
    """Transport to the graph. The implementation is the caller's business."""

    def send_pair(self, pair: ExchangePair) -> None: ...
    def fetch_profile(self) -> tuple[str, str]:
        """Return the canon's (version, profile_markdown)."""
        ...


class PairBuffer:
    """The durable local outbox.

    The outbox is the same class of data as the run catalogues: inside the repository
    only an ignored location is accepted (hygiene of the repository tree — the
    operator's decision in round 9; safety is closed at the level of R1.1).
    """

    def __init__(self, buffer_dir: Path) -> None:
        from .runcat import assert_outside_git

        buffer_dir = Path(buffer_dir)
        try:
            buffer_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError(
                f"Could not create the outbox directory: {buffer_dir}",
                cause=f"a disk error: {exc}",
                next_action="check the permissions and the free space",
            ) from exc
        assert_outside_git(buffer_dir)
        self.path = buffer_dir / BUFFER_FILENAME

    def append(self, pair: ExchangePair) -> None:
        """Record the pair immediately — before any attempt at the graph.

        That the outbox directory is ignored is checked on every write, as it is for a
        run catalogue (hygiene, not defence).

        The pair is written when the exchange CLOSES: if the operator's signal called
        for an unfolding, the caller waits for the unfolding and the reaction and only
        then writes — otherwise a rebuild of the profile has nothing to compare by
        content diff. An exchange with no unfolding (a judgement of the wording, a
        direct preference) is closed at once and legitimately carries empty second
        halves.
        """
        from .runcat import assert_outside_git

        assert_outside_git(self.path.parent)
        pair.validate()
        if not pair.captured_at:
            pair.captured_at = datetime.now(UTC).isoformat()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(pair), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            _fsync_dir(self.path.parent)
        except OSError as exc:
            raise HarnessError(
                f"Could not durably write the pair to the outbox: {self.path}",
                cause=f"a disk error: {exc}",
                next_action="check the permissions and the free space; the pair was NOT written — repeat",
            ) from exc

    def pending(self) -> list[ExchangePair]:
        if not self.path.exists():
            return []
        pairs = []
        from .errors import read_text_contract

        raw = read_text_contract(self.path, "the outbox")
        for lineno, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                pair = ExchangePair(**json.loads(line))
                # A restored pair is checked exactly as an incoming one: the outbox must not
                # send on to the graph's canon what append would have rejected — corrupted raw
                # material would distort the profile silently.
                pair.validate()
                pairs.append(pair)
            except (json.JSONDecodeError, TypeError, HarnessError) as exc:
                raise HarnessError(
                    f"The outbox is corrupted: {self.path}, line {lineno}",
                    cause=f"the line does not read as an exchange pair: {exc}",
                    next_action=(
                        "repair or delete the corrupted line; the other pairs are intact — "
                        "the outbox is line by line for exactly this reason"
                    ),
                ) from exc
        return pairs

    def flush(self, graph: GraphClient) -> int:
        """Send what has accumulated on to the graph; the outbox is cleared item by item.

        A sent pair leaves the outbox DURABLY right after its own success rather than
        at the end of the whole send: otherwise a crash between sends would leave what
        was already delivered in the outbox, and a new session would send it again —
        duplicates in a slowly growing graph distort the profile's raw material. The
        window for a repeat send is narrowed to one pair (a crash strictly between
        send_pair succeeding and the outbox being rewritten); exactly-once would
        require idempotency on the graph's side and lies outside this slice. The first
        failure stops the send; the remainder is intact and goes on the next call.
        """
        pairs = self.pending()
        sent = 0
        for pair in pairs:
            try:
                graph.send_pair(pair)
            except Exception as exc:
                raise HarnessError(
                    f"Sending the raw material stopped at pair {sent + 1} of {len(pairs)}",
                    cause=f"the graph is unavailable or refused the write: {exc}",
                    next_action=(
                        "what was not sent stayed in the outbox and is not lost; "
                        "repeat the flush when the graph is back"
                    ),
                ) from exc
            sent += 1
            self._rewrite(pairs[sent:])
        return sent

    def _rewrite(self, remaining: list[ExchangePair]) -> None:
        from .runcat import assert_outside_git

        assert_outside_git(self.path.parent)
        try:
            if not remaining:
                if self.path.exists():
                    self.path.unlink()
                    _fsync_dir(self.path.parent)
                return
        except OSError as exc:
            raise HarnessError(
                f"Could not clear the outbox: {self.path}",
                cause=f"a disk error: {exc}",
                next_action="check the permissions; the outbox state is intact — repeat the flush",
            ) from exc
        text = "".join(json.dumps(asdict(p), ensure_ascii=False) + "\n" for p in remaining)
        tmp = self.path.with_suffix(".tmp")
        try:
            self._rewrite_body(tmp, text)
        except OSError as exc:
            raise HarnessError(
                f"Could not rewrite the outbox: {self.path}",
                cause=f"a disk error: {exc}",
                next_action=(
                    "check the permissions and the free space; the outbox is intact as it was — an "
                    "already sent pair may go to the graph a second time on a repeat "
                    "(admissible by the operator's decision on the depth of protection)"
                ),
            ) from exc

    def _rewrite_body(self, tmp: Path, text: str) -> None:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)


_CACHE_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def cache_version(cache_text: str) -> str | None:
    """The version from the cache — by parsing the line exactly, not by substring.

    A substring search would count version aaa as "already written" when the old one
    was baaa — content addressing requires equality, not containment.
    """
    m = _CACHE_VERSION_RE.search(cache_text)
    return m.group(1) if m else None


def refresh_cache(cache_path: Path, graph: GraphClient) -> bool:
    """Refresh the profile cache from the graph's canon. True when the file was rewritten.

    The cache is rewritten only when the content-addressed version changes; when the
    graph is unavailable the calling code works from the existing cache and records
    its version in the round — the graph is not a condition of starting the pair.
    """
    version, markdown = graph.fetch_profile()
    from .errors import read_text_contract

    if cache_path.exists():
        current = cache_version(read_text_contract(cache_path, "the profile cache"))
        if current == version:
            return False
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        raise HarnessError(
            f"Could not write the profile cache: {cache_path}",
            cause=f"a disk error: {exc}",
            next_action="check the permissions and the free space, then repeat the cache refresh",
        ) from exc
    return True
