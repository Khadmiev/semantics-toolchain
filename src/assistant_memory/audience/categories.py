# SPDX-License-Identifier: Apache-2.0
"""The blind reader's finding categories, DERIVED from the contract rather than fixed.

WHY DERIVED. Measured on the pilot's fourth blind pass: all four findings landed in
"question without an answer" — including one that was a breach of the declared ceiling of
rights. That category is the only one asserting nothing about the text, so everything with
no shelf of its own drains into it, and both the tier of a finding and its routing are lost.
The categories the reader is given have to be the ones this contract can actually support.

WHY THE DERIVATION READS FIELDS AND NOT PROSE. A condition on a field is checkable; an
interpretation of free text is a second judgement sitting in front of the first. Where the
answer really does live in the prose — does the purpose include evaluating, is the delivery
language foreign to the audience — the operator declares it with a flag at the gate, and
the free text takes no part in the derivation at all.

WHY THE SET IS VERSIONED. Reports produced under different category sets are not comparable
by composition, so the version rides in the run record. It hashes the RULE TABLE together
with the values it was computed on — never the free text, which would make the version move
on a re-wording that changes no category.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from assistant_memory.audience.contract import OperatorFlag, PublicContract, PublicField


@dataclass(frozen=True)
class CategoryRule:
    """One category, the condition that switches it on, and the wording sent to the reader.

    The description is prompt text and lives here rather than in the template, because the
    set is dynamic: a template carrying its own list would be a second copy of this table,
    and two copies of one rule drift.
    """

    name: str
    condition: str
    description: str


#: The rule table. Order is the order in the prompt, and it is part of the version hash.
CATEGORY_TABLE: tuple[CategoryRule, ...] = (
    CategoryRule(
        "НЕПОНЯТНО",
        "всегда",
        "слово, фраза или место, которых описанный читатель не поймёт: термин вне его "
        "знания, необъяснённое сокращение, скачок логики",
    ),
    CategoryRule(
        "ПЕРЕГРУЖЕНО",
        "всегда",
        "место, где слишком много всего сразу, чтобы удержать",
    ),
    CategoryRule(
        "ТЕРЯЕТСЯ НИТЬ",
        "всегда",
        "место, где перестаёт быть ясно, зачем это и как связано с предыдущим",
    ),
    CategoryRule(
        "ВОПРОС БЕЗ ОТВЕТА",
        "объявлено «зачем читает»",
        "вопрос, который этот читатель, скорее всего, задаст, и ответа на него нет",
    ),
    CategoryRule(
        "НЕ УБЕЖДАЕТ",
        "флаг оператора: цель чтения включает оценку",
        "место, где утверждение заявлено, но читателю нечем его принять",
    ),
    CategoryRule(
        "ПРОВИСАЕТ",
        "объявлен слот или объём",
        "место, которое не окупает занятого им объёма при объявленном слоте",
    ),
    CategoryRule(
        "ВЫХОД ЗА РАМКИ",
        "объявлен потолок прав",
        "содержимое, которое по объявленному потолку прав этому читателю показывать нельзя",
    ),
    CategoryRule(
        "НЕЕСТЕСТВЕННО",
        "объявлен язык подачи или флаг оператора: язык не родной аудитории",
        "фраза, которую носитель языка аудитории так не скажет: калька, канцелярит, "
        "штамп машинного текста",
    ),
)

#: The strongest fitting category, never the safest one — stated to the reader, because the
#: pilot's drain effect is a reader behaviour and no amount of table design prevents it.
SELECTION_RULE = (
    "Выбирай самую сильную подходящую категорию, а не самую безопасную: замечание, "
    "положенное не на ту полку, теряет и свой вес, и свой маршрут."
)


@dataclass(frozen=True)
class CategorySet:
    """The categories this contract supports, plus the version that makes reports comparable."""

    names: tuple[str, ...]
    version: str
    #: What the version was computed on, kept so a mismatch can be explained rather than
    #: merely detected.
    inputs: tuple[tuple[str, bool], ...]

    def rules(self) -> tuple[CategoryRule, ...]:
        return tuple(rule for rule in CATEGORY_TABLE if rule.name in self.names)


def _conditions(public: PublicContract) -> dict[str, bool]:
    declared = public.declared_fields()
    return {
        "всегда": True,
        "объявлено «зачем читает»": PublicField.PURPOSE in declared,
        "флаг оператора: цель чтения включает оценку": bool(
            public.flags.get(OperatorFlag.PURPOSE_INCLUDES_EVALUATION)
        ),
        "объявлен слот или объём": PublicField.BUDGET in declared,
        "объявлен потолок прав": PublicField.CEILING in declared,
        "объявлен язык подачи или флаг оператора: язык не родной аудитории": (
            PublicField.LANGUAGE in declared
            or bool(public.flags.get(OperatorFlag.LANGUAGE_NOT_NATIVE))
        ),
    }


def derive_categories(public: PublicContract) -> CategorySet:
    """Which categories this contract supports, and the version of that answer."""
    values = _conditions(public)
    names = tuple(rule.name for rule in CATEGORY_TABLE if values[rule.condition])
    material = json.dumps(
        {
            "table": [(rule.name, rule.condition) for rule in CATEGORY_TABLE],
            "values": sorted(values.items()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return CategorySet(
        names=names,
        version=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        inputs=tuple(sorted(values.items())),
    )


def render_categories(categories: CategorySet) -> str:
    """The block that goes into the prompt — the derived set, spelled out for the reader."""
    lines = [f"- {rule.name} — {rule.description};" for rule in categories.rules()]
    lines[-1] = lines[-1].removesuffix(";") + "."
    return "\n".join(lines) + "\n\n" + SELECTION_RULE
