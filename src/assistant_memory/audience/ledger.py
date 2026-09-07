# SPDX-License-Identifier: Apache-2.0
"""Identifiers that outlive the thing they name — one rule, one implementation.

The genre hands out two families of permanent identifier: ``C-N`` for contract items and
``S-N`` for the reader artefact's sections. The rule is identical for both — a number is
issued once, never reused, and a version that brings a retired number back is refused —
and it is identical for the same reason: a finding, a claim-map row and a dedup key all
outlive the version they were raised under. A retired number handed to a new thing would
silently re-point every one of them at something else.

So the rule lives HERE, once. Two copies of one rule drift; this spec has been bitten by
that twice already, and the second bite is precisely why the numbers exist.

WHAT THIS DOES NOT DO. It records what a number was issued FOR (a contract field, a part of
the artefact) but never enforces it. A contract item legitimately moves from the private
part to the public one — the leak screen exists to invite exactly that move — and a section
legitimately moves between parts of a composite artefact. Refusing those would fight the
design; recording them keeps the history readable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class LedgerError(ValueError):
    """A registration that will not happen, with EVERY reason rather than the first."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


#: A number has exactly ONE spelling — no leading zeros, no sign, no spaces, no underscores.
#: ``int()`` accepts all four; the point here is that it must not.
_NUMBER = "[1-9][0-9]*"

#: An identifier has exactly ONE spelling. Without this, ``C-1`` and ``C-01`` are two
#: distinct strings and one integer: the duplicate checks (which compare text) let both
#: through, and the ledger (which keys by number) silently folds them into one entry. Two
#: supposedly permanent identifiers would then be the same identifier, which defeats both
#: the non-reuse rule and the point of citing one — a finding could name either spelling and
#: nobody could tell whether it meant the same item.
#: ``\Z``, not ``$``: ``$`` also matches before a trailing newline, so ``C-1\n`` would pass
#: as canonical and come out of ``int()`` as 1 — the same two-spellings-one-number hole one
#: character wide.
_CANONICAL = re.compile(rf"^([A-Z]+)-({_NUMBER})\Z")

#: The same rule for the ledger FILE's keys. It is the same rule for the same reason and it
#: has to be applied a second time, because the file is a second entrance to the same table:
#: closing the spelling at parse time says nothing about a number that arrives from disk.
_CANONICAL_KEY = re.compile(rf"^{_NUMBER}\Z")


def canonical_number(prefix: str, identifier: str) -> int:
    """The numeric identity of ``<prefix>-N``, refusing every other spelling.

    Refuses rather than normalises: the identifier is a NAME that lives in findings, waivers
    and claim-map rows, and silently rewriting it would leave the channel carrying one
    spelling while the author wrote another.
    """
    match = _CANONICAL.match(identifier)
    if not match or match.group(1) != prefix:
        raise LedgerError(
            [
                f"идентификатор {identifier!r} записан не канонически — ожидается "
                f"«{prefix}-N» без ведущих нулей: у номера ровно одно написание, иначе "
                f"{prefix}-1 и {prefix}-01 разошлись бы как текст и слились как число"
            ]
        )
    return int(match.group(2))


@dataclass(frozen=True)
class IdEntry:
    introduced_in: int
    retired_in: int | None
    label: str


@dataclass
class IdLedger:
    """Hands out ``<prefix>-N``, remembers every number ever handed out, never takes one back.

    WHAT IT COSTS, stated rather than discovered: reinstating a previously retired thing
    under its old number is refused too, because a reinstatement and a re-use are
    mechanically indistinguishable. A fresh number is allocated for it. Refusing in that
    direction is the deliberate choice.
    """

    path: Path
    prefix: str
    entries: dict[int, IdEntry]
    last_version: int = 0

    @classmethod
    def load(cls, path: Path, *, prefix: str) -> IdLedger:
        if not path.exists():
            return cls(path=path, prefix=prefix, entries={}, last_version=0)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("prefix", prefix) != prefix:
            raise LedgerError(
                [
                    f"реестр по пути {path} ведёт номера «{raw.get('prefix')}», "
                    f"а запрошены «{prefix}» — это разные пространства номеров"
                ]
            )
        # KEYS ARE CHECKED BEFORE THEY BECOME NUMBERS. ``int("01")``, ``int(" 1")``,
        # ``int("+1")`` and ``int("1_0")`` all succeed, so building the table straight from
        # ``int(key)`` would fold two file keys into one entry and let the later one
        # overwrite the earlier — the ledger would come back from disk having quietly
        # forgotten that a number was ever issued, which is the one thing it is for.
        # Refusing the whole file rather than the bad key: a ledger read as "almost right"
        # would hand out a number it has already handed out.
        bad = sorted(
            key for key in raw["entries"] if not _CANONICAL_KEY.match(str(key))
        )
        if bad:
            raise LedgerError(
                [
                    f"реестр по пути {path} содержит неканонические номера "
                    f"{', '.join(repr(key) for key in bad)} — ожидается десятичная запись "
                    "без ведущих нулей, знака и пробелов: иначе «1» и «01» приехали бы "
                    "с диска как один номер, и последняя запись затёрла бы первую"
                ]
            )
        # No separate collision check: canonical spellings and positive integers are in
        # one-to-one correspondence, so once every key is canonical two distinct keys
        # cannot land on one number. (JSON itself collapses literally identical keys.)
        entries = {
            int(number): IdEntry(
                introduced_in=data["introduced_in"],
                retired_in=data["retired_in"],
                label=data["label"],
            )
            for number, data in raw["entries"].items()
        }
        return cls(
            path=path, prefix=prefix, entries=entries, last_version=raw["last_version"]
        )

    def save(self) -> None:
        payload = {
            "prefix": self.prefix,
            "last_version": self.last_version,
            "entries": {
                str(number): {
                    "introduced_in": entry.introduced_in,
                    "retired_in": entry.retired_in,
                    "label": entry.label,
                }
                for number, entry in sorted(self.entries.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def next_free(self) -> str:
        return f"{self.prefix}-{max(self.entries, default=0) + 1}"

    def refusals(self, version: int, items: Mapping[int, str]) -> list[str]:
        """Why this version cannot be registered (empty = it can). Pure: changes nothing."""
        reasons: list[str] = []
        if version <= self.last_version:
            reasons.append(f"версия не растёт: {version} после {self.last_version}")
        for number in sorted(items):
            entry = self.entries.get(number)
            if entry is not None and entry.retired_in is not None:
                reasons.append(
                    f"{self.prefix}-{number} снят в версии {entry.retired_in} и не может "
                    "быть выдан снова — номер живёт дольше того, что им названо"
                )
        return reasons

    def register(
        self,
        version: int,
        items: Mapping[int, str],
        *,
        extra_problems: Sequence[str] = (),
        error: type[LedgerError] = LedgerError,
    ) -> None:
        """Record this version's identifiers, or refuse it outright and change NOTHING.

        A half-registered ledger would be worse than none: the next version would then be
        checked against a state no version ever had.
        """
        reasons = list(extra_problems) + self.refusals(version, items)
        if reasons:
            raise error(reasons)

        for number, label in items.items():
            entry = self.entries.get(number)
            self.entries[number] = IdEntry(
                introduced_in=entry.introduced_in if entry else version,
                retired_in=None,
                label=label,
            )
        for number, entry in self.entries.items():
            if number not in items and entry.retired_in is None:
                self.entries[number] = IdEntry(
                    introduced_in=entry.introduced_in,
                    retired_in=version,
                    label=entry.label,
                )
        self.last_version = version
