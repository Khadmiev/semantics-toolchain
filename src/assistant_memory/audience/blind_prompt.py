# SPDX-License-Identifier: Apache-2.0
"""Assembling the blind reader's prompt: one input, a closed field list, and a screen.

THE NAME. This is the AUDIENCE genre's blind pass, and it is deliberately not called what
the critic watcher's cold-verdict-first mechanism is called. The two must not share a name
in code or in prompts, because with a shared name the blind pass gets "implemented" by
editing context inside one session — the exact thing the canon forbids — and the defect is
invisible, since a mechanism with a similar name already works and already passes tests.

THREE LAYERS, AND THEY CATCH DIFFERENT FAILURES — this is not belt-and-braces:

1. The closed field list catches a field added later, after this code was reviewed.
2. The physical split of the structures catches a bulk serialisation: ``assemble_blind_prompt``
   accepts a ``PublicContract`` and there is no argument through which the private part
   could arrive. The prohibition in prose is a property; this is the mechanism.
3. The screen over the finished text catches an error in the substitution itself — the one
   layer that is empirical, the one with false positives, and therefore the one whose
   outcome is a question to the OPERATOR rather than a silent filter. Whether a phrase is
   really private or belongs in the public part is his answer, not development's.

WHY LINE-WISE SUBSTITUTION. In the pilot, assembly was a textual paste into one file, and
two things followed: the operator's launch checklist rode into the blind session announcing
that a reading check was under way, and a global replace mangled the checklist inside the
prompt itself. So instructions to the launcher and text for the reader are separate
assembly fields here, only the second is assembled, and a placeholder is a WHOLE LINE —
nothing inside a line is ever substituted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from assistant_memory.audience.categories import CategorySet, derive_categories, render_categories
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractItem,
    PrivateContract,
    PublicContract,
    PublicField,
)
from assistant_memory.audience.report_schema import (
    SectionBudgets,
    SectionSet,
    derive_sections,
    render_schema_block,
)
from assistant_memory.audience.sections import ReaderArtifact

SLOT_CONTRACT = "{КОНТРАКТ}"
SLOT_ARTIFACT = "{АРТЕФАКТ}"
SLOT_CATEGORIES = "{КАТЕГОРИИ}"
#: The report's OUTPUT CONTRACT — the derived section schema with its budgets. A slot and
#: not template prose, because the section set is dynamic (a function of the public part's
#: flags): a template carrying its own field list would be a second copy of the rule table,
#: and a role asked for a schema it was never shown is a defect of assembly, not of the role.
SLOT_SCHEMA = "{СХЕМА_ОТВЕТА}"

#: Every slot the assembler knows how to fill. A template line that looks like a slot and is
#: not one of these is a refusal: an unfilled placeholder would ride to the reader verbatim.
KNOWN_SLOTS: tuple[str, ...] = (SLOT_CONTRACT, SLOT_ARTIFACT, SLOT_CATEGORIES, SLOT_SCHEMA)

#: A whole line that is nothing but a brace-delimited token. Anything braced INSIDE a line is
#: not a placeholder and is left alone — that is what "line-wise, not global" means here.
_SLOT_LINE = re.compile(r"^\{[^{}]+\}$")

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


class AssemblyRefused(Exception):
    """Assembly did not happen, with every reason and with WHO has to answer.

    ``needs_operator`` separates the two natures of refusal. A schema refusal — unknown
    field, missing required one, malformed template — is development's own error and
    development fixes it. A private-value match is not: it is either a leak or a phrase
    that is actually public and should move, and only the owner of the intent knows which.
    """

    def __init__(self, reasons: list[str], *, needs_operator: bool = False) -> None:
        self.reasons = reasons
        self.needs_operator = needs_operator
        super().__init__("; ".join(reasons))

    def report(self) -> str:
        """The refusal as durable evidence — what a not-started run record stores."""
        who = "решает оператор" if self.needs_operator else "исправляет разработка"
        return "\n".join(
            [f"СБОРКА СЛЕПОГО ПРОМПТА ОТКАЗАНА ({who})", ""]
            + [f"- {reason}" for reason in self.reasons]
        )


@dataclass(frozen=True)
class RoleTemplate:
    """The role template split into its two assembly fields.

    ``launcher_instructions`` exists so that it can be carried somewhere OTHER than the
    prompt. It is parsed out rather than merely ignored, because in the pilot it was an
    HTML comment marking itself as non-insertable and it was inserted anyway.
    """

    reader_text: str
    launcher_instructions: str
    source: str = ""

    @classmethod
    def from_text(
        cls, text: str, *, source: str = "", slots: tuple[str, ...] = KNOWN_SLOTS
    ) -> RoleTemplate:
        """Parse one role's template against ITS slot list. ``slots`` defaults to the blind
        role's; the other roles pass their own — the validation mechanics are one copy, the
        slot vocabularies are each role's, and a template is refused against exactly the
        list its assembler will fill."""
        instructions = "\n".join(match.strip() for match in _COMMENT.findall(text))
        reader_text = _COMMENT.sub("", text).strip() + "\n"

        reasons: list[str] = []
        if "<!--" in reader_text or "-->" in reader_text:
            reasons.append(
                "в тексте читателя остался незакрытый комментарий — операторские инструкции "
                "могли бы уехать в промпт"
            )
        lines = [line.strip() for line in reader_text.splitlines()]
        found = [line for line in lines if _SLOT_LINE.match(line)]
        for unknown in sorted({slot for slot in found} - set(slots)):
            reasons.append(f"шаблон содержит неизвестный плейсхолдер {unknown}")
        for required in slots:
            count = found.count(required)
            if count != 1:
                reasons.append(
                    f"плейсхолдер {required} встречается {count} раз, а должен ровно один"
                )
        if reasons:
            raise AssemblyRefused(reasons)
        return cls(reader_text=reader_text, launcher_instructions=instructions, source=source)

    @classmethod
    def from_file(cls, path: Path, *, slots: tuple[str, ...] = KNOWN_SLOTS) -> RoleTemplate:
        return cls.from_text(path.read_text(encoding="utf-8"), source=str(path), slots=slots)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.reader_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AssembledPrompt:
    """The finished prompt and everything the run record has to carry about it."""

    text: str
    categories: CategorySet
    #: The derived report section set — the reader was told this schema, so its version is
    #: part of what the run record must say about the prompt.
    sections: SectionSet
    #: The section budget values in force, canonically — the record carries them so that
    #: comparability of two runs is an equality of numbers.
    section_budgets: str
    contract_version: int
    contract_sha256: str
    contract_public_sha256: str
    template_source: str
    template_digest: str
    #: The prompt WITHOUT the artefact — role template, public contract, category set. This
    #: is what the reader fingerprint takes, and it is a separate number on purpose: over
    #: the whole prompt it would count the artefact twice, and it would move on an edit to
    #: the file that the reader never sees.
    instructions_digest: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def render_public_contract(public: PublicContract) -> str:
    """The public part as the reader sees it — by field, with the identifiers kept.

    The identifiers ride into the prompt on purpose: a finding must cite the item it
    breaks, and a reader who was never shown the numbers can only paraphrase, which puts
    the check back into someone's reading.
    """
    blocks: list[str] = []
    for field in PublicField:
        items: tuple[ContractItem, ...] = public.items_of(field)
        if not items:
            continue
        lines = [f"{field.value.upper()}:"]
        lines += [f"- {item.id} — {item.text}" for item in items]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _substitute(reader_text: str, values: dict[str, str]) -> str:
    out: list[str] = []
    for line in reader_text.splitlines():
        replacement = values.get(line.strip()) if _SLOT_LINE.match(line.strip()) else None
        out.append(replacement if replacement is not None else line)
    return "\n".join(out) + "\n"


def assemble_blind_prompt(
    template: RoleTemplate,
    public: PublicContract,
    artifact: str,
    budgets: SectionBudgets,
) -> tuple[str, CategorySet, SectionSet]:
    """Build the prompt from the PUBLIC part, the artefact and the budgets. Nothing else
    reaches here.

    Note the signature: there is no parameter through which the private part could arrive,
    and that is the whole point of layer 2. The budgets are an argument because the reader
    is TOLD them (the schema block carries the numbers) and they move the prompt — which is
    what makes two runs under different budgets non-comparable by construction. It returns
    bare text — the record fields are assembled a level up, where the contract's digests
    are known.
    """
    problems = public.validate() + budgets.refusals()
    if problems:
        raise AssemblyRefused(problems)
    categories = derive_categories(public)
    sections = derive_sections(public)
    text = _substitute(
        template.reader_text,
        {
            SLOT_CONTRACT: render_public_contract(public),
            SLOT_ARTIFACT: artifact,
            SLOT_CATEGORIES: render_categories(categories),
            SLOT_SCHEMA: render_schema_block(sections, budgets),
        },
    )
    return text, categories, sections


def _normalise(text: str) -> str:
    """Case folded, whitespace collapsed — the comparison the screen is specified to make."""
    return " ".join(text.casefold().split())


@dataclass(frozen=True)
class PrivateMatch:
    item_id: str
    field: str
    text: str


def screen_for_private(prompt: str, private: PrivateContract) -> list[PrivateMatch]:
    """Private values found in the finished prompt.

    This function sees the private part and the FINISHED text; it cannot influence assembly,
    only block it. False positives are expected — a short private item can occur in ordinary
    prose — and they are not silently thresholded away, because the designed outcome of a
    match is a question to the operator, not a verdict.
    """
    haystack = _normalise(prompt)
    return [
        PrivateMatch(item_id=item.id, field=item.field.value, text=item.text)
        for item in private.items
        if _normalise(item.text) and _normalise(item.text) in haystack
    ]


def prepare_blind_prompt(
    template: RoleTemplate,
    contract: AudienceContract,
    artifact: ReaderArtifact,
    budgets: SectionBudgets,
) -> AssembledPrompt:
    """Assemble, then screen. The only place that holds both parts, and it does nothing else.

    Someone must hold both to run the screen at all; what matters is that the holder cannot
    influence what was assembled. It calls the assembler with the public part only, and the
    screen with a text that is already finished.

    The artefact arrives PARSED, and the type is the guard — the same trick as the private
    part. An unparsed blob is exactly how a deck marked up at the wrong heading level rides
    to the reader and leaves the review with an empty denominator, which is the one failure
    here that looks like success.
    """
    text, categories, sections = assemble_blind_prompt(
        template, contract.public, artifact.render(), budgets
    )
    instructions, _, _ = assemble_blind_prompt(template, contract.public, "", budgets)
    matches = screen_for_private(text, contract.private)
    if matches:
        raise AssemblyRefused(
            [
                f"в собранном промпте найдено приватное значение {match.item_id} "
                f"(«{match.field}»): {match.text!r} — это либо утечка, либо формулировка, "
                "которая на самом деле публичная и должна переехать в публичную часть"
                for match in matches
            ],
            needs_operator=True,
        )
    return AssembledPrompt(
        text=text,
        categories=categories,
        sections=sections,
        section_budgets=budgets.canonical(),
        contract_version=contract.version,
        contract_sha256=contract.sha256,
        contract_public_sha256=contract.public_sha256,
        template_source=template.source,
        template_digest=template.digest,
        instructions_digest=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    )
