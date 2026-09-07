# SPDX-License-Identifier: Apache-2.0
"""Prompt assembly for the strategic and machine passes — separate signatures on purpose.

THE SEPARATION IS THE TYPES, not a discipline. The strategic assembler takes the WHOLE
``AudienceContract`` — the role holds both parts by definition, and the leak screen does
not apply to it; the machine assembler takes ONLY the public part's language items and
physically cannot be handed anything else. Neither set of inputs satisfies the blind
assembler's signature, and the blind assembler's inputs satisfy neither of these — the one
protection that survived the pilot is the one where the wrong call does not compile.

Each role's template carries its OWN output contract — the answer-schema block and the
full text of its category set — by the common structured-answer rule: a role asked for a
schema it was never shown is a defect of assembly, not of the role. And each pass gets its
own fingerprint, the launch gate of AR-15 transfer: the normalized section text, the
prompt WITHOUT the artifact, and the model with its version. The strategic fingerprint
covers the FULL contract through the prompt (the role reads the private part — a transfer
across a changed takeaway would pass a judgement of the old goal off as one of the new),
which is exactly the difference from the blind role, where a private edit annuls nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from assistant_memory.audience.blind_prompt import (
    SLOT_ARTIFACT,
    SLOT_CATEGORIES,
    SLOT_SCHEMA,
    AssemblyRefused,
    RoleTemplate,
    _substitute,
    render_public_contract,
)
from assistant_memory.audience.categories import CategoryRule, CategorySet
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractItem,
    PrivateField,
    PublicField,
)
from assistant_memory.audience.machine_comb import (
    MACHINE_CATEGORY_TABLE,
    machine_table_version,
)
from assistant_memory.audience.sections import ReaderArtifact
from assistant_memory.audience.strategic import (
    STRATEGIC_CATEGORY_TABLE,
    SUMMARY_CEILING_WORDS,
    derive_strategic_categories,
)

SLOT_FULL_CONTRACT = "{КОНТРАКТ_ПОЛНЫЙ}"
SLOT_LANGUAGE = "{ЯЗЫКОВЫЕ_ПОЛЯ}"

STRATEGIC_SLOTS: tuple[str, ...] = (
    SLOT_FULL_CONTRACT, SLOT_CATEGORIES, SLOT_SCHEMA, SLOT_ARTIFACT,
)
MACHINE_SLOTS: tuple[str, ...] = (
    SLOT_LANGUAGE, SLOT_CATEGORIES, SLOT_SCHEMA, SLOT_ARTIFACT,
)

#: The two public fields the machine pass may see — input for judging a calque, never a
#: condition for a category to exist.
LANGUAGE_FIELDS: frozenset[PublicField] = frozenset(
    {PublicField.LANGUAGE, PublicField.PROFICIENCY}
)


def _render_rules(rules: Sequence[CategoryRule]) -> str:
    """One rendering for every role's category block — the description lives at the table,
    never in the template, so the prompt cannot hold a second copy of the list."""
    lines = [f"- {rule.name} — {rule.description};" for rule in rules]
    lines[-1] = lines[-1].removesuffix(";") + "."
    return "\n".join(lines)


def render_full_contract(contract: AudienceContract) -> str:
    """Both parts, identifiers kept — the strategic reader judges the DECLARED goal, so the
    private items ride verbatim, flags included."""
    blocks = [render_public_contract(contract.public)]
    for field in PrivateField:
        items = tuple(i for i in contract.private.items if i.field is field)
        if not items:
            continue
        lines = [f"{field.value.upper()} (приватная часть):"]
        lines += [f"- {item.id} — {item.text}" for item in items]
        blocks.append("\n".join(lines))
    flags = ["ПРИВАТНЫЕ ФЛАГИ:"] + [
        f"- {flag.value}: {'да' if contract.private.flags.get(flag) else 'нет'}"
        for flag in sorted(contract.private.flags, key=lambda f: f.value)
    ]
    blocks.append("\n".join(flags))
    return "\n\n".join(blocks)


_COMMON_SCHEMA_RULES = (
    "Ответ — РОВНО ОДИН JSON-объект; допустима одна обрамляющая ограда ```json … ```. "
    "Любой содержательный текст вне объекта — отказ.\n"
    "Перечни полей закрыты в обе стороны на каждом уровне: неизвестное поле — отказ, "
    "отсутствующее обязательное — отказ. Семантически пустое обязательное поле (пустая "
    "строка, пустой массив цитат) — отказ.\n"
    "«scope» — либо литерал \"целое\" (эффект, размазанный по всему артефакту), либо "
    "непустой массив существующих разделов \"S-N\".\n"
    "«цитаты» — непустой массив ДОСЛОВНЫХ мест артефакта, каждая цитата обязана "
    "встречаться в тексте разделов её scope (пробелы схлопываются)."
)


def render_strategic_schema_block(categories: CategorySet) -> str:
    return (
        _COMMON_SCHEMA_RULES
        + "\n\nПоля объекта:\n"
        + f'- "итог" — строка, непустая: связное суждение о достижении объявленного '
        f"выноса; потолок {SUMMARY_CEILING_WORDS} слов (пробелы схлопываются, слово — "
        "непустой токен между пробелами).\n"
        + '- "находки" — массив, МОЖЕТ быть пустым (пустой список = чистый проход); '
        'элемент — объект ровно с пятью полями: {"категория": "<из списка ЧТО ИСКАТЬ>", '
        '"scope": "целое" | ["S-N", …], "текст": "<суть; формулировки контракта, включая '
        'приватные, цитируй здесь>", "цитаты": ["<дословные места артефакта>", …], '
        '"пункт_контракта": "C-N" — из объявленных пунктов ЛЮБОЙ из двух частей '
        "контракта}."
    )


def render_machine_schema_block() -> str:
    return (
        _COMMON_SCHEMA_RULES
        + "\n\nПоля объекта:\n"
        + '- "находки" — единственное поле; массив, МОЖЕТ быть пустым (пустой список = '
        "чистый проход); элемент — объект ровно с четырьмя полями: "
        '{"категория": "<из таблицы ЧТО ИСКАТЬ>", "scope": "целое" | ["S-N", …], '
        '"текст": "<почему это выдаёт машинное авторство>", '
        '"цитаты": ["<дословные места артефакта>", …]}. Якоря к контракту нет: контракта '
        "ты не видишь."
    )


@dataclass(frozen=True)
class StrategicPrompt:
    """The assembled strategic prompt and what the run record must say about it."""

    text: str
    categories: CategorySet
    contract_version: int
    contract_sha256: str
    #: The prompt WITHOUT the artifact — template, BOTH contract parts, the category set.
    #: The strategic fingerprint takes this, so a private-part edit moves it by construction.
    instructions_digest: str


@dataclass(frozen=True)
class MachinePrompt:
    """The assembled machine prompt and what the run record must say about it."""

    text: str
    table_version: str
    instructions_digest: str


def prepare_strategic_prompt(
    template: RoleTemplate, contract: AudienceContract, artifact: ReaderArtifact
) -> StrategicPrompt:
    """Assemble the strategic prompt from the WHOLE contract. No leak screen by definition
    of the role — the private part belongs in this prompt; what is separated is the
    SIGNATURE, which the blind assembler's inputs cannot satisfy."""
    problems = contract.validate()
    if problems:
        raise AssemblyRefused(problems)
    categories = derive_strategic_categories(contract)
    values = {
        SLOT_FULL_CONTRACT: render_full_contract(contract),
        SLOT_CATEGORIES: _render_rules(
            [r for r in STRATEGIC_CATEGORY_TABLE if r.name in categories.names]
        ),
        SLOT_SCHEMA: render_strategic_schema_block(categories),
        SLOT_ARTIFACT: artifact.render(),
    }
    text = _substitute(template.reader_text, values)
    instructions = _substitute(template.reader_text, {**values, SLOT_ARTIFACT: ""})
    return StrategicPrompt(
        text=text,
        categories=categories,
        contract_version=contract.version,
        contract_sha256=contract.sha256,
        instructions_digest=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    )


def prepare_machine_prompt(
    template: RoleTemplate,
    language_items: Sequence[ContractItem],
    artifact: ReaderArtifact,
) -> MachinePrompt:
    """Assemble the machine prompt from the artifact and the LANGUAGE FIELDS ONLY.

    The signature takes bare items rather than a contract, and refuses anything outside
    the two language fields: the closed input is what makes "the prompt carries only the
    artifact and the language fields" a property instead of a promise.
    """
    problems = [
        f"{item.id}: поле «{item.field.value}» не является языковым — машинному прочёсу "
        "из исходных материалов положены только артефакт и языковые поля"
        for item in language_items
        if item.field not in LANGUAGE_FIELDS
    ]
    if problems:
        raise AssemblyRefused(problems)
    if language_items:
        language_block = "\n".join(
            f"- {item.field.value}: {item.text}" for item in language_items
        )
    else:
        language_block = "- язык подачи не объявлен — суди по самому тексту"
    values = {
        SLOT_LANGUAGE: language_block,
        SLOT_CATEGORIES: _render_rules(MACHINE_CATEGORY_TABLE),
        SLOT_SCHEMA: render_machine_schema_block(),
        SLOT_ARTIFACT: artifact.render(),
    }
    text = _substitute(template.reader_text, values)
    instructions = _substitute(template.reader_text, {**values, SLOT_ARTIFACT: ""})
    return MachinePrompt(
        text=text,
        table_version=machine_table_version(),
        instructions_digest=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
    )


def role_fingerprint(
    role: str,
    artifact: ReaderArtifact,
    *,
    instructions_digest: str,
    model: str,
    model_version: str,
) -> str:
    """The launch gate of one role's pass — what decides whether a previous result may be
    transferred (AR-15). The same shape as the reader fingerprint, with the ROLE in the
    material so two roles' fingerprints can never collide, and the prompt digest covering
    whatever that role is shown (for the strategic role — the full contract, so a private
    edit invalidates a transfer by construction)."""
    material = "\n".join(
        [
            f"role={role}",
            artifact.normalised_text(),
            f"instructions={instructions_digest}",
            f"model={model}",
            f"model_version={model_version}",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
