# SPDX-License-Identifier: Apache-2.0
"""The blind report's sections, DERIVED from the public contract — and their budgets.

THE SAME DEVICE AS THE CATEGORIES, on purpose. The report's section set is a function of
the public part's fields and flags: a rule table «section ← condition on a field», the
condition never an interpretation of prose. Where the answer genuinely lives in free text —
does the reading purpose include retelling upward — the operator declares it with a flag at
the gate, exactly the fork the category derivation already resolved; a SECOND derivation
mechanism would be a second answer to one question.

TWO LIMITS ARE STRUCTURAL, NOT STYLISTIC. First: a condition may name ONLY public fields
and flags. A section derived from a private field would tailor the report's shape to the
desired takeaway — an echo manufactured by structure, the exact thing the physical split of
the contract exists to prevent. Second: the free valve is exactly one section; every
unlimited text field is a road back to free-form markdown, whose failure class this schema
buries.

WHY THE SET IS VERSIONED. Reports produced under different section sets are not comparable,
so the version rides in the run record. It hashes the RULE TABLE with the values it was
computed on — never the wording, which would move the version on a re-phrasing that changes
no section.

BUDGETS ARE CONFIGURATION, NOT PROMPT PROSE. The retelling has a FLOOR as well as a
ceiling — it is the genre's main product, and for a synthesis the cheap failure is the
throwaway, not the padding; findings have no floor and never will (squeezing is the
findings' cheap failure — the asymmetry is the design). The values are knobs with their
units in their names, written into the run record: comparability of two runs is checked by
equality of numbers, not by a diff of prose.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from assistant_memory.audience.contract import OperatorFlag, PublicContract

# The section names ARE the JSON field names of the report object (AG-24: one field per
# derived section, the field names are the section names of the rule table).
RETELLING = "пересказ"
SECTION_TABLE_DENOMINATOR = "таблица_по_разделам"
FINDINGS = "замечания"
QUESTIONS = "вопросы"
IMPRESSION = "впечатление"
FREE_VALVE = "что_ещё_заметил"
UPWARD = "передать_наверх"
READINESS = "готовность_и_уверенность"


@dataclass(frozen=True)
class SectionRule:
    """One section, the condition that switches it on, and the wording sent to the reader.

    The description is prompt text and lives here rather than in the template, for the same
    reason the category descriptions do: the set is dynamic, and a template carrying its own
    list would be a second copy of this table.
    """

    name: str
    condition: str
    description: str


#: The rule table. Order is the order of sections in the prompt's schema block and in the
#: render, and it is part of the version hash. The free valve is deliberately LAST: it is
#: the report's single overflow, not one shelf among shelves.
SECTION_TABLE: tuple[SectionRule, ...] = (
    SectionRule(
        RETELLING,
        "всегда",
        "синтез глазами описанного читателя: о чём это, что ты отсюда унесёшь, какая "
        "сквозная линия держит части вместе",
    ),
    SectionRule(
        SECTION_TABLE_DENOMINATOR,
        "всегда",
        "твой знаменатель: ровно одна строка на каждый раздел S-N, чистые включительно — "
        "чистый раздел это видимый результат, а не отсутствие работы",
    ),
    SectionRule(
        FINDINGS,
        "всегда",
        "гипотезы о препятствиях для описанного читателя; пустой список — законное чистое "
        "чтение",
    ),
    SectionRule(
        QUESTIONS,
        "всегда",
        "вопросы, которые этот читатель действительно задаст; исчерпывающий каталог не "
        "нужен",
    ),
    SectionRule(
        IMPRESSION,
        "всегда",
        "впечатление ЦЕЛОГО: где было интересно, где заскучал, что запомнилось, что "
        "накопилось к финалу — в том числе эффекты, у которых нет одного раздела-адреса",
    ),
    SectionRule(
        UPWARD,
        "флаг оператора: цель чтения включает пересказ наверх",
        "что я передам своему руководству: короткий пересказ, который этот читатель "
        "понесёт наверх своими словами",
    ),
    SectionRule(
        READINESS,
        "флаг оператора: цель чтения включает оценку",
        "что я считаю готовым и не готовым, и насколько я в этом уверен",
    ),
    SectionRule(
        FREE_VALVE,
        "всегда",
        "ровно один свободный клапан на весь отчёт: наблюдение, которому нет места в "
        "других секциях; может быть пустым",
    ),
)


@dataclass(frozen=True)
class SectionSet:
    """The sections this contract's report carries, plus the version making reports comparable."""

    names: tuple[str, ...]
    version: str
    #: What the version was computed on — kept so a mismatch can be explained, not merely
    #: detected.
    inputs: tuple[tuple[str, bool], ...]

    def rules(self) -> tuple[SectionRule, ...]:
        return tuple(rule for rule in SECTION_TABLE if rule.name in self.names)


def _conditions(public: PublicContract) -> dict[str, bool]:
    """Condition → value, on FIELDS AND FLAGS ONLY. Nothing here reads prose, and nothing
    here reads the private part — both limits are the module's charter, and the test suite
    holds every condition string against the public registry."""
    return {
        "всегда": True,
        "флаг оператора: цель чтения включает пересказ наверх": bool(
            public.flags.get(OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING)
        ),
        "флаг оператора: цель чтения включает оценку": bool(
            public.flags.get(OperatorFlag.PURPOSE_INCLUDES_EVALUATION)
        ),
    }


def derive_sections(public: PublicContract) -> SectionSet:
    """Which sections this contract's report carries, and the version of that answer."""
    values = _conditions(public)
    names = tuple(rule.name for rule in SECTION_TABLE if values[rule.condition])
    material = json.dumps(
        {
            "table": [(rule.name, rule.condition) for rule in SECTION_TABLE],
            "values": sorted(values.items()),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return SectionSet(
        names=names,
        version=hashlib.sha256(material.encode("utf-8")).hexdigest(),
        inputs=tuple(sorted(values.items())),
    )


@dataclass(frozen=True)
class SectionBudgets:
    """Every section budget, in words, each knob carrying its unit in its name.

    The starting values are a HYPOTHESIS calibrated by the first live review under the new
    schema. Only the retelling has a floor — see the module note for why the asymmetry is
    the design. A budget violation IN EITHER DIRECTION refuses the run at validation.
    """

    retelling_floor_words: int = 250
    retelling_ceiling_words: int = 800
    impression_ceiling_words: int = 300
    free_valve_ceiling_words: int = 150

    def refusals(self) -> list[str]:
        """Ways this configuration is not a usable budget set (empty = it is)."""
        reasons = [
            f"бюджет «{name}» не положителен: {value}"
            for name, value in sorted(self.__dict__.items())
            if not (isinstance(value, int) and not isinstance(value, bool) and value > 0)
        ]
        if not reasons and self.retelling_floor_words > self.retelling_ceiling_words:
            reasons.append(
                f"пол пересказа ({self.retelling_floor_words} слов) выше потолка "
                f"({self.retelling_ceiling_words}) — такому бюджету нельзя удовлетворить"
            )
        return reasons

    def canonical(self) -> str:
        """The values as the run record carries them — one canonical string, compared by
        equality: two runs are comparable exactly when these numbers match."""
        return json.dumps(self.__dict__, ensure_ascii=False, sort_keys=True)

    def bounds(self, section: str) -> tuple[int | None, int | None]:
        """(floor, ceiling) in words for one section; (None, None) = no budget declared."""
        return {
            RETELLING: (self.retelling_floor_words, self.retelling_ceiling_words),
            IMPRESSION: (None, self.impression_ceiling_words),
            FREE_VALVE: (None, self.free_valve_ceiling_words),
        }.get(section, (None, None))


#: The three admissible values of a question's «ответ» field — the closed list the reader
#: is shown and the validator holds the answer against.
QUESTION_ANSWERS: tuple[str, ...] = ("в артефакте", "в непоказываемой части", "отсутствует")


def _budget_note(budgets: SectionBudgets, section: str) -> str:
    floor, ceiling = budgets.bounds(section)
    if floor is not None and ceiling is not None:
        return f" Бюджет: не меньше {floor} и не больше {ceiling} слов."
    if ceiling is not None:
        return f" Потолок: {ceiling} слов."
    return ""


def render_schema_block(sections: SectionSet, budgets: SectionBudgets) -> str:
    """The block that goes into the prompt: the derived schema, spelled out for the reader.

    Rendered from the derived set rather than written into the template, because the set is
    dynamic and a template carrying its own field list would be a second copy of the rule
    table. The role is told its WHOLE output contract here — a role asked for a schema it
    was never shown is a defect of assembly, not of the role.
    """
    shape = {
        RETELLING: "строка, непустая",
        SECTION_TABLE_DENOMINATOR: (
            'массив объектов {"раздел": "S-N", "находки": [<индексы>]} — ровно одна '
            "строка на КАЖДЫЙ раздел артефакта; «находки» — индексы замечаний этого "
            f"раздела в массиве «{FINDINGS}», счёт с нуля; пустой список = раздел чист"
        ),
        FINDINGS: (
            'массив объектов {"раздел": "S-N", "категория": "<имя из списка ЧТО ИСКАТЬ>", '
            '"текст": "<суть>", "пункт_контракта": "C-N"} — пустой массив это законное '
            "чистое чтение; идентификаторы находкам не выдавай, их выводит принимающая "
            "сторона"
        ),
        QUESTIONS: (
            'массив объектов {"текст": "<вопрос>", "ответ": "'
            + '" | "'.join(QUESTION_ANSWERS)
            + '"}'
        ),
        IMPRESSION: "строка, непустая",
        UPWARD: "строка, непустая",
        READINESS: "строка, непустая",
        FREE_VALVE: "строка, МОЖЕТ быть пустой",
    }
    lines = [
        "Ответ — РОВНО ОДИН JSON-объект; допустима одна обрамляющая ограда ```json … ```. "
        "Любой содержательный текст вне объекта — отказ: отчёт не засчитывается.",
        "Перечень полей закрыт в обе стороны: неизвестное поле — отказ, отсутствующее "
        "обязательное — отказ. То же на каждом уровне вложенности.",
        "Подсчёт слов для бюджетов: пробелы схлопываются, слово — непустой токен между "
        "пробелами.",
        "",
        "Поля объекта (все обязательны):",
    ]
    for rule in sections.rules():
        lines.append(
            f'- "{rule.name}" — {shape[rule.name]}: {rule.description}.'
            f"{_budget_note(budgets, rule.name)}"
        )
    return "\n".join(lines)
