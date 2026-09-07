# SPDX-License-Identifier: Apache-2.0
"""The audience contract as data: a closed public part, a separate private part, and
identifiers that outlive contract versions.

WHY THE PARTS ARE TWO STRUCTURES AND NOT TWO FLAGS. The public part is what the blind
reader is shown; the private part — the desired takeaway, the delivery policy in the
author's own words, the owner's decisions — is what turns the measurement into an echo the
moment it leaks. A single record with a ``private: bool`` on each item satisfies every
prose prohibition and still hands the whole thing to a serialiser that dumps the record.
So the split is physical: the assembler's signature accepts ``PublicContract`` and cannot
be handed the other one.

WHY THE FIELD LIST IS CLOSED IN BOTH DIRECTIONS. A field outside the list is a refusal, not
a skip: the failure being prevented is a field added later — after this code was reviewed —
silently riding into the prompt. A missing required field is a refusal for the mirror
reason: silence must mean refusal, never consent.

WHY IDENTIFIERS OUTLIVE VERSIONS. A finding cites the contract item it breaks, and findings
live longer than contract versions. If a retired item's number were handed to a new item,
an old finding would start pointing at a different rule without changing a single character
— and nothing would show. Hence the ledger: it hands out numbers, never reuses them, and
refuses a version that brings a retired number back.

JUDGEMENT MADE HERE, NOT IN THE SPEC. The spec says a missing REQUIRED field is a refusal
but does not enumerate which are required. Required here: "what the audience knows" and
"why it is reading" — without the first the always-on category "unclear" has nothing to be
measured against, and without the second the reader has no end to read for. The other four
are declared-or-not, and their absence is meaningful rather than broken: it switches the
corresponding finding category off (see ``categories``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from assistant_memory.audience.ledger import IdLedger, LedgerError, canonical_number


class PublicField(StrEnum):
    """The closed list of what may ride to the blind reader. Nothing else, ever."""

    KNOWS = "что знает"
    CEILING = "потолок прав"
    PURPOSE = "зачем читает"
    LANGUAGE = "язык подачи"
    PROFICIENCY = "уровень владения языком"
    BUDGET = "слот или объём"


#: Without these the reader is not a reader: whose knowledge, and to what end. See the
#: module docstring — this is a judgement, and it is stated rather than implied.
REQUIRED_PUBLIC: frozenset[PublicField] = frozenset({PublicField.KNOWS, PublicField.PURPOSE})


class PrivateField(StrEnum):
    """Never shown to the blind reader. Lives in its own structure and its own file."""

    TAKEAWAY = "желаемый вынос"
    DELIVERY = "политика подачи"
    OWNER_DECISION = "решение владельца"


class OperatorFlag(StrEnum):
    """Declarations the operator makes ABOUT the free text, because the machine may not read it.

    Category derivation is mechanical by rule: a condition on a field, never an
    interpretation of prose. Where the answer genuinely lives inside free text — does the
    purpose include evaluating, is the delivery language foreign to the audience — the
    operator declares it at the gate and the free text takes no part in the derivation.
    """

    PURPOSE_INCLUDES_EVALUATION = "цель чтения включает оценку"
    LANGUAGE_NOT_NATIVE = "язык не родной аудитории"
    #: Switches the report section «передать_наверх» on. A PUBLIC flag deliberately: it
    #: describes the READER's own purpose ("I will retell this to my management"), which the
    #: blind reader legitimately knows about himself — unlike the audience-request flag,
    #: which describes the AUTHOR's goal and therefore lives in the private part.
    PURPOSE_INCLUDES_UPWARD_RETELLING = "цель чтения включает пересказ наверх"


class PrivateFlag(StrEnum):
    """The private part's own closed flag section — the operator's declarations about the
    AUTHOR'S GOAL, mirrored on the public flags in mechanics and opposite in visibility.

    Private because the flag describes a property of the goal: showing it to the blind
    reader would tell him what the text is trying to achieve, and that is exactly the
    knowledge whose absence makes the blind measurement a measurement. The flags enter the
    canonical FULL contract text and its fingerprint (and through it the strategic role's
    fingerprint — flipping one invalidates a strategic carry); they cannot reach the blind
    prompt assembler by construction, because its signature takes the public part only, and
    the public part has no such section.
    """

    GOAL_INCLUDES_AUDIENCE_REQUEST = "цель включает запрос к аудитории"


class ContractError(LedgerError):
    """A contract that will not be read, with EVERY reason rather than the first.

    Collected rather than short-circuited for the same reason the crediting check collects:
    an operator fixing a contract wants the whole list, not one tripwire followed by another
    parse that trips the next.
    """


@dataclass(frozen=True)
class ContractItem:
    """One item: ONE assertion about the artefact's text, not a paragraph of five.

    A paragraph would reopen the loophole the identifier closes — citing a block of five
    requirements without saying which one is broken.
    """

    id: str
    field: PublicField | PrivateField
    text: str

    @property
    def number(self) -> int:
        """Numeric identity, refusing any spelling but the canonical one."""
        return canonical_number("C", self.id)


def _sorted_items(items: Iterable[ContractItem]) -> tuple[ContractItem, ...]:
    return tuple(sorted(items, key=lambda item: item.number))


def _duplicate_ids(items: Iterable[ContractItem]) -> list[str]:
    """The same number on two items, which would make a finding's citation ambiguous.

    Checked per part as well as across both, because either part can be parsed on its own
    and a check that only exists in the joined object is a check with a way around it.
    """
    seen: set[int] = set()
    problems: list[str] = []
    for item in items:
        try:
            number = item.number  # refuses a non-canonical spelling outright
        except LedgerError as refusal:
            problems += refusal.reasons
            continue
        if number in seen:
            problems.append(f"идентификатор {item.id} выдан дважды в одной версии")
        seen.add(number)
    return problems


@dataclass(frozen=True)
class PublicContract:
    """What the blind reader is shown, and the operator's flags about it."""

    version: int
    items: tuple[ContractItem, ...]
    flags: dict[OperatorFlag, bool]

    def validate(self) -> list[str]:
        """Ways this public part is not admissible input for prompt assembly."""
        problems: list[str] = _duplicate_ids(self.items)
        for item in self.items:
            if not isinstance(item.field, PublicField):
                problems.append(
                    f"{item.id}: поле «{item.field}» не из публичного перечня — "
                    "приватному пункту в публичной части не место"
                )
        declared = self.declared_fields()
        for required in sorted(REQUIRED_PUBLIC, key=lambda f: f.value):
            if required not in declared:
                problems.append(f"нет обязательного поля «{required.value}»")
        if PublicField.PROFICIENCY in declared and PublicField.LANGUAGE not in declared:
            problems.append(
                "объявлен уровень владения языком, но не объявлен сам язык подачи — "
                "уровень без языка ни к чему не относится"
            )
        for flag in OperatorFlag:
            if flag not in self.flags:
                problems.append(
                    f"нет флага оператора «{flag.value}» — от него зависит набор категорий, "
                    "и умолчание здесь означало бы, что его выбрала не та сторона"
                )
        return problems

    def declared_fields(self) -> frozenset[PublicField]:
        return frozenset(
            item.field for item in self.items if isinstance(item.field, PublicField)
        )

    def items_of(self, field: PublicField) -> tuple[ContractItem, ...]:
        return tuple(item for item in self.items if item.field is field)


@dataclass(frozen=True)
class PrivateContract:
    """The private companion. Never an argument to the blind assembler — only to the screen
    and to the roles that hold both parts by definition (the strategic reader)."""

    version: int
    items: tuple[ContractItem, ...]
    #: The private flag section (see ``PrivateFlag``). A default exists so that the many
    #: places constructing the object stay terse — validation still demands every flag
    #: DECLARED, for the same reason the public part does: silence must not choose whether
    #: a whole strategic category exists.
    flags: dict[PrivateFlag, bool] = field(default_factory=dict)

    def validate(self) -> list[str]:
        problems = _duplicate_ids(self.items) + [
            f"{item.id}: поле «{item.field}» не из приватного перечня"
            for item in self.items
            if not isinstance(item.field, PrivateField)
        ]
        for flag in PrivateFlag:
            if flag not in self.flags:
                problems.append(
                    f"нет приватного флага «{flag.value}» — от него зависит набор "
                    "стратегических категорий, и умолчание означало бы, что его выбрала "
                    "не та сторона"
                )
        return problems

    def declared_takeaway(self) -> bool:
        """Whether a desired takeaway is declared — the strategic role's admissibility
        predicate reads this, because the role's whole subject is the declared goal."""
        return any(item.field is PrivateField.TAKEAWAY for item in self.items)


@dataclass(frozen=True)
class AudienceContract:
    """Both parts under ONE version number, because the version labels the contract.

    Two digests, and they answer different questions. ``sha256`` covers both parts — that
    is the number's companion, the thing that makes an edit without a version bump visible,
    and it is what a finding records. ``public_sha256`` covers only what the reader was
    shown, which is the honest way to ask later whether the READING was of a different
    contract or whether the operator merely re-worded his own private goal.
    """

    public: PublicContract
    private: PrivateContract

    @property
    def version(self) -> int:
        return self.public.version

    def canonical_text(self) -> str:
        """The WHOLE contract, both parts. Built from the public canon so the two cannot
        drift: an earlier version repeated the public serialisation here and dropped the
        operator flags in both copies at once.

        The PRIVATE FLAGS are part of the canon for the same reason the public ones are:
        they decide which strategic categories exist, so they enter the full fingerprint —
        and through it the strategic role's own fingerprint, which is what makes flipping
        one invalidate a strategic carry by construction.
        """
        lines = [self.canonical_public_text(), "private_flags:"]
        lines += [
            f"{flag.value}={'да' if self.private.flags.get(flag) else 'нет'}"
            for flag in sorted(self.private.flags, key=lambda f: f.value)
        ]
        lines.append("private:")
        lines += [f"{i.id}|{i.field.value}|{i.text}" for i in self.private.items]
        return "\n".join(lines)

    def canonical_public_text(self) -> str:
        # THE FLAGS ARE PART OF THE CONTRACT, not metadata about it: they decide which
        # finding categories the blind reader is given. Left out, an edit to a flag changed
        # what the reader is asked to look for while the contract's hash stood still — and
        # the hash exists precisely so that an edit without a version bump cannot hide.
        lines = [f"version={self.version}", "flags:"]
        lines += [
            f"{flag.value}={'да' if self.public.flags.get(flag) else 'нет'}"
            for flag in sorted(self.public.flags, key=lambda f: f.value)
        ]
        lines.append("public:")
        lines += [f"{i.id}|{i.field.value}|{i.text}" for i in self.public.items]
        return "\n".join(lines)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_text().encode("utf-8")).hexdigest()

    @property
    def public_sha256(self) -> str:
        return hashlib.sha256(self.canonical_public_text().encode("utf-8")).hexdigest()

    def validate(self) -> list[str]:
        problems = self.public.validate() + self.private.validate()
        if self.public.version != self.private.version:
            problems.append(
                f"версии частей расходятся: публичная {self.public.version}, "
                f"приватная {self.private.version} — версия у контракта одна"
            )
        return problems + _duplicate_ids((*self.public.items, *self.private.items))


# --- the file format -------------------------------------------------------------------
#
# One line per item, because the line IS the granularity rule: a paragraph cannot be an
# item, so the format refuses what the spec forbids instead of asking a reviewer to notice
# it. Anything else under the items heading is a refusal, not a comment.

_VERSION = re.compile(r"^Версия:\s*(\d+)\s*$", re.MULTILINE)
_ITEM = re.compile(r"^-\s*(C-\d+)\s*\(([^)]+)\)\s*[—–-]\s*(.+?)\s*$")
_FLAG = re.compile(r"^-\s*([^:]+?)\s*:\s*(да|нет)\s*$", re.IGNORECASE)

_ITEMS_HEADING = "Пункты:"
_FLAGS_HEADING = "Флаги оператора:"


def _section(text: str, heading: str) -> list[str] | None:
    """Lines under ``heading`` up to the next blank-line-separated heading (None if absent)."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return None
    collected: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.endswith(":") and not stripped.startswith("-"):
            break
        collected.append(stripped)
    return collected


def _parse_version(text: str, reasons: list[str]) -> int:
    match = _VERSION.search(text)
    if not match:
        reasons.append("в файле контракта нет строки «Версия: N»")
        return 0
    return int(match.group(1))


def _parse_items(
    text: str, allowed: type[PublicField] | type[PrivateField], reasons: list[str]
) -> tuple[ContractItem, ...]:
    lines = _section(text, _ITEMS_HEADING)
    if lines is None:
        reasons.append(f"в файле контракта нет раздела «{_ITEMS_HEADING}»")
        return ()
    by_value = {member.value: member for member in allowed}
    items: list[ContractItem] = []
    for line in lines:
        match = _ITEM.match(line)
        if not match:
            reasons.append(
                f"строка не является пунктом вида «- C-N (поле) — текст»: {line!r}"
            )
            continue
        item_id, field_name, item_text = match.groups()
        field = by_value.get(field_name.strip().lower())
        if field is None:
            reasons.append(
                f"{item_id}: поле «{field_name.strip()}» вне закрытого перечня "
                f"({', '.join(sorted(by_value))})"
            )
            continue
        try:
            # Canonical spelling, collected as a reason rather than raised raw.
            canonical_number("C", item_id)
        except LedgerError as refusal:
            reasons += refusal.reasons
            continue
        items.append(ContractItem(id=item_id, field=field, text=item_text))
    return _sorted_items(items)


def _parse_flags(text: str, allowed, part_label: str, reasons: list[str]) -> dict:
    """The flag section of one part, closed and repeat-refusing. ONE parser for both parts:
    the private section is the public one's mirror, and two copies of this loop would drift
    the way two copies of one list drift."""
    flags: dict = {}
    flag_lines = _section(text, _FLAGS_HEADING)
    if flag_lines is None:
        reasons.append(f"в {part_label} части нет раздела «{_FLAGS_HEADING}»")
        return flags
    by_value = {member.value: member for member in allowed}
    for line in flag_lines:
        match = _FLAG.match(line)
        if not match:
            reasons.append(f"строка не является флагом вида «- название: да/нет»: {line!r}")
            continue
        name, value = match.groups()
        flag = by_value.get(name.strip().lower())
        if flag is None:
            reasons.append(
                f"флаг «{name.strip()}» вне закрытого перечня ({', '.join(sorted(by_value))})"
            )
            continue
        # A REPEATED FLAG IS AMBIGUITY, NOT AN UPDATE. Taking the last line silently, a
        # contract declaring «да» and then «нет» parsed as «нет» — and the flag decides
        # whether a whole reader category exists and enters the contract hash, so the
        # review would converge against a contract the source text does not declare.
        # ANY repeat is refused, not only a contradicting one — unlike the transcript
        # header, where a repeat comes from a provider we do not author and only a
        # CONFLICT is evidence of anything. This file is written by the operator, one
        # declaration per line; a second line is a mistake worth a one-line edit.
        if flag in flags:
            reasons.append(
                f"флаг «{name.strip()}» объявлен дважды — повтор это неоднозначность, "
                "а не уточнение: флаг решает, существует ли целая категория чтения, "
                "и входит в отпечаток контракта"
            )
            continue
        flags[flag] = value.strip().lower() == "да"
    return flags


def parse_public_contract(text: str) -> PublicContract:
    reasons: list[str] = []
    version = _parse_version(text, reasons)
    items = _parse_items(text, PublicField, reasons)
    flags = _parse_flags(text, OperatorFlag, "публичной", reasons)
    contract = PublicContract(version=version, items=items, flags=flags)
    reasons += contract.validate()
    if reasons:
        raise ContractError(reasons)
    return contract


def parse_private_contract(text: str) -> PrivateContract:
    reasons: list[str] = []
    version = _parse_version(text, reasons)
    items = _parse_items(text, PrivateField, reasons)
    flags = _parse_flags(text, PrivateFlag, "приватной", reasons)
    contract = PrivateContract(version=version, items=items, flags=flags)
    reasons += contract.validate()
    if reasons:
        raise ContractError(reasons)
    return contract


# --- the identifier ledger --------------------------------------------------------------


class ContractIdLedger(IdLedger):
    """The ``C-N`` ledger. The permanence rule itself lives in ``IdLedger`` — one copy of it.

    WHAT IT CATCHES: a version that gives a retired item's number to a new item. That is the
    failure the permanence rule exists for — an old finding silently re-pointed at a
    different rule.

    A field CHANGE is recorded, not refused: an item moving from the private part to the
    public one is exactly what the leak screen asks the operator to consider, and refusing
    it would fight a move the design invites.
    """

    @classmethod
    def load(cls, path: Path, *, prefix: str = "C") -> ContractIdLedger:
        return super().load(path, prefix=prefix)  # type: ignore[return-value]

    def register(self, contract: AudienceContract) -> None:  # type: ignore[override]
        """Record this version's identifiers, or refuse the contract outright."""
        items = {
            item.number: item.field.value
            for item in (*contract.public.items, *contract.private.items)
        }
        super().register(
            contract.version,
            items,
            extra_problems=contract.validate(),
            error=ContractError,
        )


def load_contract(
    public_path: Path, private_path: Path, *, ledger: ContractIdLedger | None = None
) -> AudienceContract:
    """Read both parts from their SEPARATE files and, if given a ledger, register them.

    Two paths and not one file with two sections: the separation the assembler relies on is
    worth more when a careless read of "the contract" cannot pick up the private half.
    """
    public = parse_public_contract(public_path.read_text(encoding="utf-8"))
    private = parse_private_contract(private_path.read_text(encoding="utf-8"))
    contract = AudienceContract(public=public, private=private)
    problems = contract.validate()
    if problems:
        raise ContractError(problems)
    if ledger is not None:
        ledger.register(contract)
    return contract
