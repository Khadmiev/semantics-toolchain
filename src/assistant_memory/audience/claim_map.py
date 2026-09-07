# SPDX-License-Identifier: Apache-2.0
"""The claim map: what the artefact asserts about reality, and who is allowed to sign it off.

WHAT IT IS. A private companion to the reader artefact — the audience never sees it. One row
per claim the artefact makes ("twenty-five kinds of knowledge", "deliveries happened"),
carrying the source that makes the claim re-checkable. Convergence is not declared while any
row is non-terminal, so a deck cannot "converge" with an unverified assertion still in it.

WHY A ROW HAS ITS OWN IDENTIFIER AND A QUOTE HASH. Without them a status attaches to a
SECTION, and a section holds more than one assertion: a substituted or added claim would
inherit a neighbour's "confirmed", and the inheritance would be indistinguishable from a
check. So addressing is by ``M-N``, and the confirmation is bound to the quote it was given.

THE CONFIRMATION IS ABOUT A TRIPLE, NOT A ROW: identifier + quote hash + presentation form
with its rounding step. Editing any member voids it. This is implemented by RECOMPUTING
validity from the row's current fields rather than by mutating on edit — an edit path that
must remember to reset something is an edit path that will one day forget.

THE PRESENTATION FORM IS IN THE TRIPLE for a reason that is not symmetry: it is the single
field that moves the boundary of the owner's waiver right. A confirmation surviving an edit
to it would let that boundary be moved with nothing re-confirmed. Until confirmed, and on any
ambiguous reading, the form counts as EXACT — deliberately erring towards NARROWING the
right, because that error costs one extra iteration while the opposite silently widens a
boundary the operator drew by hand.

WHAT THIS DELIBERATELY DOES NOT DO. An OWNERLESS material claim — an assertion in the
artefact for which nobody opened a row — is not detectable here and no mechanism here
pretends otherwise. A row that does not exist produces no gap: the machine cannot notice what
it was never told. That check is the informed reader's judgement, and saying so is worth more
than a guarantee that reads well and holds nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from assistant_memory.audience.contract import AudienceContract
from assistant_memory.audience.ledger import IdLedger, LedgerError, canonical_number
from assistant_memory.audience.sections import ReaderArtifact

#: B.8 F-2: ONE evidence-shape implementation for every carrier of the requirement —
#: the claim-map confirmation and the coverage-report row cite the same schema.
from assistant_memory.review.round_gate import evidence_shape_problems as _evidence_problems


class ClaimMapError(LedgerError):
    """A claim map that will not be accepted, with every reason rather than the first."""


class ClaimStatus(StrEnum):
    UNCONFIRMED = "не подтверждено"
    CONFIRMED = "подтверждено"
    SIMPLIFIED = "намеренно упрощено"
    WITHDRAWN = "снято"


#: Terminal statuses. Convergence needs every row here — that is the whole point of the map.
TERMINAL: frozenset[ClaimStatus] = frozenset(
    {ClaimStatus.CONFIRMED, ClaimStatus.SIMPLIFIED, ClaimStatus.WITHDRAWN}
)


class Party(StrEnum):
    DEVELOPMENT = "разработка"
    INFORMED = "зрячий проход"
    OPERATOR = "оператор"


#: Who may put a row into each terminal status. Development appears NOWHERE: it is the side
#: with an interest in closing, so it may only PROPOSE (see the simplification passport).
#: Confirms the one who sees the basis; withdraws the one whose intent it is.
MAY_SET: dict[ClaimStatus, Party] = {
    ClaimStatus.CONFIRMED: Party.INFORMED,
    ClaimStatus.SIMPLIFIED: Party.INFORMED,
    ClaimStatus.WITHDRAWN: Party.OPERATOR,
}


class PresentationForm(StrEnum):
    EXACT = "точная"
    ROUNDED = "округлённая"


@dataclass(frozen=True)
class SimplificationPassport:
    """What "deliberately simplified" has to answer before it can be proposed at all.

    Without it the status becomes a polite name for "we did not check": the three questions
    cannot be filled in without checking on the merits.
    """

    original_fact: str
    withheld: str
    #: The contract item that PERMITS the simplification, by identifier — a sanction that
    #: lives in conversation does not exist for the reader.
    permitted_by: str

    def missing(self) -> list[str]:
        return [
            name
            for name in ("original_fact", "withheld", "permitted_by")
            if not str(getattr(self, name)).strip()
        ]


@dataclass(frozen=True)
class Attestation:
    """A signature, and the exact triple it was given against.

    B.8 F-2: a CONFIRMED status additionally carries typed ``evidence`` of READING —
    ``{ground, read_ref, quote | node_id+version | element_id}``, the same block a
    coverage-report confirmation carries — because ``basis`` is free text a fabricated
    citation once wore convincingly (review f0c0b685, runs 6 and 8: «подтверждено |
    DECK_BRIEF §4» invented from file names in the prompt, the files never opened).

    THE EVIDENCE IS ACCEPTED ON FORM AND SIGNATURE ONLY: no tract in this genre keeps a
    run-scoped read log or re-reads the cited content, so a well-shaped block proves the
    author filled the schema, not that the read happened. The published row SAYS this
    (`evidence_verification: form_only ...`) rather than looking verified to the strength
    of a coverage row whose quote was held against a real read — the claim carries its own
    strength (operator ruling, B.8 impl review, round 3). Binding the map to the seeing
    pass's actual reads is an open genre-development item, deliberately out of B.8's scope.
    """

    by: Party
    basis: str
    triple: str
    evidence: dict | None = None


@dataclass(frozen=True)
class ClaimRow:
    """One claim. Immutable — a change is a new row value, and the attestations then go stale."""

    id: str
    section: str
    quote: str
    recorded_status: ClaimStatus = ClaimStatus.UNCONFIRMED
    #: Numeric claims only. ``None`` means the claim is not numeric and no form applies.
    recorded_form: PresentationForm | None = None
    rounding_step: str | None = None
    passport: SimplificationPassport | None = None
    status_attestation: Attestation | None = None
    form_attestation: Attestation | None = None

    @property
    def number(self) -> int:
        """Numeric identity, refusing any spelling but the canonical one."""
        return canonical_number("M", self.id)

    @property
    def quote_sha256(self) -> str:
        return hashlib.sha256(" ".join(self.quote.split()).encode("utf-8")).hexdigest()

    @property
    def triple(self) -> str:
        """Identifier + quote hash + form-with-step. What every attestation is bound to."""
        form = "—" if self.recorded_form is None else self.recorded_form.value
        material = f"{self.id}|{self.quote_sha256}|{form}|{self.rounding_step or '—'}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _valid(self, attestation: Attestation | None, expected: Party | None) -> bool:
        if attestation is None or attestation.triple != self.triple:
            return False
        return expected is None or attestation.by is expected

    @property
    def status(self) -> ClaimStatus:
        """The status that COUNTS — recomputed, never trusted as stored.

        Fail-closed in three ways at once: a stale attestation, a missing one, or one signed
        by a party not entitled to that status all fall back to "not confirmed". Development
        cannot reach a terminal status by writing one into the row.
        """
        if self.recorded_status is ClaimStatus.UNCONFIRMED:
            return ClaimStatus.UNCONFIRMED
        if self.recorded_status is ClaimStatus.SIMPLIFIED and (
            self.passport is None or self.passport.missing()
        ):
            return ClaimStatus.UNCONFIRMED
        if not self._valid(self.status_attestation, MAY_SET[self.recorded_status]):
            return ClaimStatus.UNCONFIRMED
        # B.8 F-2: «подтверждено» without well-formed typed evidence of reading falls
        # back to unconfirmed — fail-closed, like every other gap in this automaton.
        if self.recorded_status is ClaimStatus.CONFIRMED and _evidence_problems(
            self.status_attestation.evidence if self.status_attestation else None
        ):
            return ClaimStatus.UNCONFIRMED
        return self.recorded_status

    @property
    def form(self) -> PresentationForm | None:
        """The form that COUNTS. ``None`` for a non-numeric claim; EXACT until confirmed."""
        if self.recorded_form is None:
            return None
        if not self._valid(self.form_attestation, Party.INFORMED):
            return PresentationForm.EXACT
        return self.recorded_form

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def blocking_reason(self) -> str | None:
        """Why this row blocks convergence (None = it does not)."""
        if self.terminal:
            return None
        if self.recorded_status is ClaimStatus.UNCONFIRMED:
            return f"{self.id}: статус «{ClaimStatus.UNCONFIRMED}» — строка ещё не разобрана"
        if self.recorded_status is ClaimStatus.SIMPLIFIED and (
            self.passport is None or self.passport.missing()
        ):
            return (
                f"{self.id}: «{ClaimStatus.SIMPLIFIED}» без полного паспорта — предложение "
                "разработки, а не терминальный статус"
            )
        attestation = self.status_attestation
        if attestation is None:
            return (
                f"{self.id}: статус «{self.recorded_status}» никем не подтверждён — поставить "
                f"его вправе только {MAY_SET[self.recorded_status]}"
            )
        if attestation.triple != self.triple:
            return (
                f"{self.id}: подтверждение относится к другой тройке — с тех пор изменилась "
                "цитата, форма подачи или шаг округления, и подтверждали не это"
            )
        if attestation.by is not MAY_SET[self.recorded_status]:
            return (
                f"{self.id}: статус «{self.recorded_status}» подтверждён стороной "
                f"«{attestation.by}», а вправе только «{MAY_SET[self.recorded_status]}»"
            )
        problems = _evidence_problems(attestation.evidence)
        return (
            f"{self.id}: «{ClaimStatus.CONFIRMED}» без типизированного доказательства "
            f"чтения (B.8 F-2): {'; '.join(problems)} — свободный текст основания "
            "однажды носил сфабрикованную цитату"
        )


def attest(
    row: ClaimRow, *, by: Party, basis: str, evidence: dict | None = None
) -> Attestation:
    """Sign the row's CURRENT triple. Attesting a row you have not re-read is not possible.

    ``evidence`` is required in substance for a CONFIRMED status (B.8 F-2): without a
    well-formed block the confirmation falls back to unconfirmed, fail-closed.
    """
    return Attestation(by=by, basis=basis, triple=row.triple, evidence=evidence)


class ClaimIdLedger(IdLedger):
    """The ``M-N`` ledger. Same permanence rule as ``C-N`` and ``S-N``, same implementation."""

    @classmethod
    def load(cls, path, *, prefix: str = "M") -> ClaimIdLedger:  # type: ignore[override]
        return super().load(path, prefix=prefix)  # type: ignore[return-value]

    def register(self, rows: Sequence[ClaimRow], artifact_seq: int) -> None:  # type: ignore[override]
        super().register(
            artifact_seq,
            {row.number: row.section for row in rows},
            error=ClaimMapError,
        )


def validate_rows(
    rows: Sequence[ClaimRow],
    artifact: ReaderArtifact,
    *,
    contract: AudienceContract | None = None,
) -> list[str]:
    """Ways this set of rows is not an admissible claim map (empty = it is).

    THE ONE MECHANICAL CROSS-CHECK the spec keeps: a row naming a section that does not
    exist is a refusal. Everything else about materiality stays the informed reader's
    judgement — see the module docstring.
    """
    problems: list[str] = []
    known = set(artifact.section_ids())
    seen: set[int] = set()
    contract_ids = (
        {item.id for item in (*contract.public.items, *contract.private.items)}
        if contract
        else None
    )

    for row in rows:
        try:
            number = row.number  # refuses a non-canonical spelling outright
        except LedgerError as refusal:
            problems += refusal.reasons
            continue
        if number in seen:
            problems.append(f"идентификатор {row.id} выдан дважды в одной версии")
        seen.add(number)
        if row.section not in known:
            problems.append(
                f"{row.id}: назван раздел {row.section}, которого нет в этой версии артефакта "
                f"({', '.join(sorted(known)) or 'разделов нет'})"
            )
        if not row.quote.strip():
            problems.append(f"{row.id}: пустая цитата клейма — подтверждать было бы нечего")
        if row.recorded_form is PresentationForm.ROUNDED and not (row.rounding_step or "").strip():
            problems.append(
                f"{row.id}: форма «{PresentationForm.ROUNDED}» без шага округления — шаг и есть "
                "единица допуска, без неё форма ничего не задаёт"
            )
        if row.recorded_form is None and row.rounding_step:
            problems.append(
                f"{row.id}: шаг округления задан у клейма без формы подачи — шаг относится "
                "к числу, а число здесь не объявлено"
            )
        if row.recorded_status is ClaimStatus.SIMPLIFIED and row.passport is None:
            problems.append(
                f"{row.id}: «{ClaimStatus.SIMPLIFIED}» без паспорта — паспорт часть самого "
                "предложения, а не украшение к нему"
            )
        if row.passport is not None:
            for field in row.passport.missing():
                problems.append(f"{row.id}: в паспорте упрощения не заполнено поле «{field}»")
            if contract_ids is not None and row.passport.permitted_by not in contract_ids:
                problems.append(
                    f"{row.id}: паспорт ссылается на пункт контракта "
                    f"{row.passport.permitted_by}, которого в этой версии контракта нет — "
                    "санкция вне контракта"
                )
    return problems


def blocking_rows(rows: Iterable[ClaimRow]) -> list[str]:
    """Every reason the claim map blocks convergence, collected rather than short-circuited."""
    return [reason for row in rows if (reason := row.blocking_reason()) is not None]


# --- the wire form ----------------------------------------------------------------------
#
# The channel message is the CARRIER of the map; the markdown table below is a rendering of
# it, not the other way round. The pilot kept the map as a hand-maintained table, but this
# map is written by development rather than by the operator, and parsing a markdown table
# back into structure is fragile in exactly the places that matter (a quote containing a
# pipe, a wrapped cell). The schema is closed in both directions, like the others.

_ROW_FIELDS = frozenset(
    {
        "id", "section", "quote", "quote_sha256", "status", "status_by", "status_basis",
        "status_evidence", "evidence_verification", "form", "rounding_step", "passport",
        "form_by", "form_basis", "triple",
    }
)
_PAYLOAD_FIELDS = frozenset({"phase", "iteration", "artifact_seq", "rows"})
CLAIM_MAP_PHASE = "claim_map"

#: B.8 impl review, round 3 (finding b8-audience-evidence-unverified, operator-accepted):
#: what a claim-map confirmation's evidence actually establishes in this genre. Emitted on
#: every published CONFIRMED row and REQUIRED by `check_payload` — a confirmed row that
#: does not state its verification level is the silent form-as-proof this closes.
FORM_ONLY_EVIDENCE_VERIFICATION = (
    "form_only — содержимое цитированного чтения в этом жанре никем не перечитывается; "
    "доказательство принято по форме и подписи"
)


def to_payload(rows: Sequence[ClaimRow], *, iteration: int, artifact_seq: int) -> dict:
    """The channel message for this version of the map."""
    out = []
    for row in rows:
        item: dict = {
            "id": row.id,
            "section": row.section,
            "quote": row.quote,
            "quote_sha256": row.quote_sha256,
            "status": row.status.value,
            "triple": row.triple,
        }
        if row.status_attestation is not None:
            item["status_by"] = row.status_attestation.by.value
            item["status_basis"] = row.status_attestation.basis
            if row.status_attestation.evidence is not None:
                item["status_evidence"] = dict(row.status_attestation.evidence)
        if row.status is ClaimStatus.CONFIRMED:
            # The claim carries its own strength: no tract here re-reads the content.
            item["evidence_verification"] = FORM_ONLY_EVIDENCE_VERIFICATION
        if row.recorded_form is not None:
            item["form"] = (row.form or PresentationForm.EXACT).value
            if row.rounding_step:
                item["rounding_step"] = row.rounding_step
        if row.form_attestation is not None:
            item["form_by"] = row.form_attestation.by.value
            item["form_basis"] = row.form_attestation.basis
        if row.passport is not None:
            item["passport"] = {
                "original_fact": row.passport.original_fact,
                "withheld": row.passport.withheld,
                "permitted_by": row.passport.permitted_by,
            }
        out.append(item)
    return {
        "phase": CLAIM_MAP_PHASE,
        "iteration": iteration,
        "artifact_seq": artifact_seq,
        "rows": out,
    }


def check_payload(payload: dict) -> list[str]:
    """Ways this message is not a claim-map phase. Unknown field = refusal, as everywhere."""
    problems: list[str] = []
    for name in sorted(set(payload) - _PAYLOAD_FIELDS):
        problems.append(f"неизвестное поле фазы: {name}")
    for name in sorted(_PAYLOAD_FIELDS - set(payload)):
        problems.append(f"нет обязательного поля фазы: {name}")
    if payload.get("phase") != CLAIM_MAP_PHASE:
        problems.append(f"фаза {payload.get('phase')!r}, а ожидалась {CLAIM_MAP_PHASE!r}")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        problems.append("поле rows не является списком")
        return problems
    for item in rows:
        if not isinstance(item, dict):
            problems.append(f"строка не является объектом: {item!r}")
            continue
        for name in sorted(set(item) - _ROW_FIELDS):
            problems.append(f"{item.get('id', '?')}: неизвестное поле строки: {name}")
        for name in ("id", "section", "quote", "status"):
            if not str(item.get(name, "")).strip():
                problems.append(f"{item.get('id', '?')}: нет обязательного поля строки {name}")
        # B.8 F-2: a published «подтверждено» must carry well-formed typed evidence of
        # reading — refused the same way as a coverage confirmation without one.
        if str(item.get("status", "")) == ClaimStatus.CONFIRMED.value:
            for problem in _evidence_problems(item.get("status_evidence")):
                problems.append(
                    f"{item.get('id', '?')}: «{ClaimStatus.CONFIRMED}» без "
                    f"типизированного доказательства чтения — {problem}"
                )
            # The row must SAY what its evidence establishes: in this genre — form and
            # signature, never re-read content. A confirmed row without the mark is the
            # silent form-as-proof this field exists to close. EXACT equality with the
            # canonical constant: a prefix check let «form_only; content independently
            # verified» ride an overclaim inside the very mark that exists to exclude
            # overclaims (finding b8-form-only-prefix-allows-overclaim).
            verification = str(item.get("evidence_verification", ""))
            if verification != FORM_ONLY_EVIDENCE_VERIFICATION:
                problems.append(
                    f"{item.get('id', '?')}: «{ClaimStatus.CONFIRMED}» без пометки "
                    "evidence_verification=form_only… — подтверждение обязано называть "
                    "силу своего доказательства (в этом жанре содержимое чтения никем "
                    "не перечитывается)"
                )
    return problems


def render_markdown(rows: Sequence[ClaimRow]) -> str:
    """The map for human eyes. A rendering — never the source of record."""
    header = (
        "| M-N | Раздел | Утверждение | Форма | Статус | Кем и на каком основании |\n"
        "|-----|--------|-------------|-------|--------|--------------------------|"
    )
    lines = [header]
    for row in rows:
        form = "—" if row.form is None else row.form.value
        if row.form is PresentationForm.ROUNDED and row.rounding_step:
            form += f" (шаг {row.rounding_step})"
        signed = "—"
        if row.status_attestation is not None and row.status is not ClaimStatus.UNCONFIRMED:
            signed = f"{row.status_attestation.by}: {row.status_attestation.basis}"
        quote = " ".join(row.quote.split()).replace("|", "\\|")
        # The human rendering shows the evidence strength too — the mark exists for the
        # operator reading this table, not for the wire.
        status = str(row.status)
        if row.status is ClaimStatus.CONFIRMED:
            status += " (доказательство: форма)"
        lines.append(
            f"| {row.id} | {row.section} | «{quote}» | {form} | {status} | {signed} |"
        )
    return "\n".join(lines)
