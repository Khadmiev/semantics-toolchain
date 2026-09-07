# SPDX-License-Identifier: Apache-2.0
"""The reader artefact: parts, sections ``S-N``, and the fingerprint that gates a re-read.

WHY ``S-N`` IS THE UNIT OF THE WHOLE GENRE. Three different things in the pilot were called
"a section", "a slide" and "a row", and their boundaries coincided only because a human was
paying attention. One mechanically checkable identifier removes a whole class of
divergence: a finding with no address, dedup by paraphrase, a denominator that does not
add up to the artefact.

COMPOSITE ARTEFACTS (operator decision, 2026-08-04). One artefact may be assembled from
several parts — a slide plan, the text to be read aloud, the numbers behind the charts —
and the composition is open-ended. The address stays FLAT: one ``S-N`` sequence across the
whole thing, because the address is used in four places and a second coordinate makes each
of them dearer. Membership of a part is a PROPERTY of the section, declared in its heading
and filtered as a property. The consequence that made the decision necessary: the
mechanical layer branches per PART, not per artefact — text read aloud is timed, a table of
numbers is not — so an artefact-level setting could not express it.

WHY THE HEADING LEVEL IS ENFORCED. Measured, not assumed: the coverage builder recognises a
row by a third-level heading, and the pilot deck was marked up with second-level ones. A run
of the tool over it produced ZERO rows — an artefact accepted in silence with an empty
denominator, which is the worst failure available here because it looks like success. So a
second-level heading that looks like a section is a refusal, not a comment.

WHAT THE READER IS SHOWN is exactly the sections, never the file. The file holds things the
blind reader does not see (the parts declaration, a title, editing notes) and the
fingerprint is taken over what he DOES see — which is why rendering and fingerprinting read
from the same parsed structure rather than from the bytes on disk.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from assistant_memory.audience.ledger import IdLedger, LedgerError, canonical_number


class PartKind(StrEnum):
    """Which branch of the mechanical layer a part answers to.

    Two values because the mechanical layer has two branches and no more: text read aloud is
    timed against the declared slot at speaking pace; a document is timed at READING pace and
    additionally measured by returns to terms, which push that estimate up. Declared here, in
    step 3, rather than retrofitted in step 5 — a field added later would invalidate every
    artefact already written.

    An earlier wording said a document is not timed at all. That was corrected by the
    operator on 2026-08-05: size decides on its own, and nobody starts a twenty-five-page
    document however cleanly it is written.
    """

    SPOKEN = "произносимый"
    DOCUMENT = "документ"


class ArtifactError(LedgerError):
    """A reader artefact that will not be read, with every reason rather than the first."""


@dataclass(frozen=True)
class ArtifactPart:
    name: str
    kind: PartKind


@dataclass(frozen=True)
class Section:
    """One addressable section. The heading is part of it: the reader sees the heading."""

    id: str
    part: str
    title: str
    body: str

    @property
    def number(self) -> int:
        """Numeric identity, refusing any spelling but the canonical one."""
        return canonical_number("S", self.id)

    def normalised(self) -> str:
        """Whitespace collapsed, trailing stripped — the form the fingerprint is taken over."""
        return " ".join(f"{self.id}|{self.part}|{self.title}|{self.body}".split())

    def render(self) -> str:
        return f"### {self.id} ({self.part}) — {self.title}\n{self.body.strip()}"


@dataclass(frozen=True)
class ReaderArtifact:
    parts: tuple[ArtifactPart, ...]
    sections: tuple[Section, ...]

    def part(self, name: str) -> ArtifactPart | None:
        return next((p for p in self.parts if p.name == name), None)

    def sections_of(self, part_name: str) -> tuple[Section, ...]:
        return tuple(s for s in self.sections if s.part == part_name)

    def render(self) -> str:
        """What goes into the prompt — the sections, and nothing else from the file."""
        return "\n\n".join(section.render() for section in self.sections)

    def normalised_text(self) -> str:
        return "\n".join(section.normalised() for section in self.sections)

    def section_ids(self) -> tuple[str, ...]:
        return tuple(section.id for section in self.sections)


_PARTS_HEADING = "Части:"
_PART = re.compile(r"^-\s*([^:]+?)\s*:\s*(.+?)\s*$")
_SECTION = re.compile(r"^###\s+(S-\d+)\s*\(([^)]+)\)\s*[—–-]\s*(.+?)\s*$")
_ANY_H3 = re.compile(r"^###\s+")
#: The measured failure: a section marked up at the wrong heading level is invisible to the
#: coverage builder, and the review proceeds with an empty denominator.
_WRONG_LEVEL = re.compile(r"^#{1,2}\s+S-\d+\b")


def _parts_block(text: str, reasons: list[str]) -> tuple[ArtifactPart, ...]:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == _PARTS_HEADING)
    except StopIteration:
        reasons.append(
            f"в артефакте нет раздела «{_PARTS_HEADING}» — перечень частей объявляется даже "
            "для артефакта из одной части, иначе появляется второй путь исполнения"
        )
        return ()

    kinds = {kind.value: kind for kind in PartKind}
    parts: list[ArtifactPart] = []
    seen: set[str] = set()
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("-"):
            break
        match = _PART.match(stripped)
        if not match:
            reasons.append(f"строка не является частью вида «- имя: вид»: {stripped!r}")
            continue
        name, kind_name = match.group(1), match.group(2).strip().lower()
        kind = kinds.get(kind_name)
        if kind is None:
            reasons.append(
                f"часть «{name}»: вид «{kind_name}» вне закрытого перечня "
                f"({', '.join(sorted(kinds))})"
            )
            continue
        if name in seen:
            reasons.append(f"часть «{name}» объявлена дважды")
        seen.add(name)
        parts.append(ArtifactPart(name=name, kind=kind))
    if not parts and not reasons:
        reasons.append("перечень частей пуст")
    return tuple(parts)


def parse_reader_artifact(text: str) -> ReaderArtifact:
    """Parse the artefact, or refuse it with every reason at once."""
    reasons: list[str] = []
    parts = _parts_block(text, reasons)
    declared = {part.name for part in parts}

    sections: list[Section] = []
    body: list[str] = []
    current: tuple[str, str, str] | None = None
    seen_ids: set[str] = set()

    def close() -> None:
        if current is not None:
            sections.append(
                Section(id=current[0], part=current[1], title=current[2], body="\n".join(body))
            )

    for line in text.splitlines():
        if _WRONG_LEVEL.match(line):
            reasons.append(
                f"раздел размечен заголовком не третьего уровня: {line.strip()[:60]!r} — "
                "построитель покрытия такой раздел не увидит, и знаменатель окажется пустым"
            )
            continue
        if _ANY_H3.match(line):
            match = _SECTION.match(line)
            if not match:
                reasons.append(
                    f"заголовок третьего уровня не является разделом вида "
                    f"«### S-N (часть) — название»: {line.strip()[:60]!r}"
                )
                continue
            close()
            section_id, part_name, title = match.group(1), match.group(2).strip(), match.group(3)
            try:
                # Canonical spelling, collected as a reason rather than raised raw.
                canonical_number("S", section_id)
            except LedgerError as refusal:
                reasons += refusal.reasons
                current, body = None, []
                continue
            if section_id in seen_ids:
                reasons.append(f"идентификатор {section_id} выдан дважды в одной версии")
            seen_ids.add(section_id)
            if part_name not in declared:
                reasons.append(
                    f"{section_id}: часть «{part_name}» не объявлена в перечне частей "
                    f"({', '.join(sorted(declared)) or 'перечень пуст'})"
                )
            current, body = (section_id, part_name, title), []
            continue
        if current is not None:
            body.append(line)
    close()

    if not sections and not reasons:
        reasons.append("в артефакте нет ни одного раздела «### S-N (часть) — название»")
    unused = sorted(declared - {section.part for section in sections})
    if unused:
        reasons.append(
            f"объявлены части без единого раздела: {', '.join(unused)} — объявление без "
            "содержимого делает перечень частей недостоверным"
        )
    if reasons:
        raise ArtifactError(reasons)
    return ReaderArtifact(
        parts=parts, sections=tuple(sorted(sections, key=lambda s: s.number))
    )


def load_reader_artifact(path: Path) -> ReaderArtifact:
    return parse_reader_artifact(path.read_text(encoding="utf-8"))


class SectionIdLedger(IdLedger):
    """The ``S-N`` ledger, registered against the artefact version from the CHANNEL.

    The version here is ``artifact_seq`` — the sequence number of the message the version
    rode in — because the genre has no other carrier of "artefact version" and a second one
    would be a second answer to one question. It follows that registration happens AFTER the
    version is posted, not before: the number does not exist until the channel assigns it.
    Monotonicity comes free, the channel being append-only.
    """

    @classmethod
    def load(cls, path: Path, *, prefix: str = "S") -> SectionIdLedger:
        return super().load(path, prefix=prefix)  # type: ignore[return-value]

    def register(self, artifact: ReaderArtifact, artifact_seq: int) -> None:  # type: ignore[override]
        super().register(
            artifact_seq,
            {section.number: section.part for section in artifact.sections},
            error=ArtifactError,
        )


def reader_fingerprint(
    artifact: ReaderArtifact,
    *,
    instructions_digest: str,
    model: str,
    model_version: str,
) -> str:
    """sha256 of everything that decides the reading — and nothing that does not.

    THE MODEL IS IN IT because the blind reader is a measuring instrument, not merely an
    input: changing the model changes the measurement while the text stands still, and
    carrying a previous result forward would then pass one instrument's reading off as an
    assessment of the current version.

    THE REST OF THE LAUNCH PROFILE IS NOT: it lives in the profile fingerprint and moves on
    a token refresh, which has nothing to do with the reading. Neither is the private
    companion nor the claim map — the blind reader never sees them, so an edit there must
    not make him re-read text that did not move.

    ``instructions_digest`` is the assembled prompt WITHOUT the artefact — role template,
    public contract, category set. Taking it over the whole prompt would count the artefact
    twice and would move this fingerprint on an edit the reader cannot see.
    """
    material = "\n".join(
        [
            artifact.normalised_text(),
            f"instructions={instructions_digest}",
            f"model={model}",
            f"model_version={model_version}",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def missing_section_rows(section_ids: Sequence[str], reported: Sequence[str]) -> list[str]:
    """Sections the blind report failed to account for — its denominator, checked.

    A report whose row count differs from the version's section count is not credited: the
    denominator is what makes a clean section a visible result rather than an absence of
    work, and without the check the reader drifts back into being a constant-volume
    generator (measured on the pilot: 26 findings on any version, until the denominator was
    introduced).
    """
    return [section_id for section_id in section_ids if section_id not in set(reported)]
