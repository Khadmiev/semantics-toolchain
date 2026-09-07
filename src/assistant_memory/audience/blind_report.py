# SPDX-License-Identifier: Apache-2.0
"""The blind report as a closed schema — validation, extraction, and the finding identity.

WHY A SCHEMA AND NOT ANCHORS. Both manual reserves of review f0c0b685 were regex over
markdown headings: findings existed, the parser did not see them, and emptiness was
indistinguishable from cleanliness (incident 93f7f216; B.8 D-1/D-2 closed the measured hole
— not the class). The schema closes the class: "did not parse" no longer has a silent form,
because the answer is one JSON object against a closed schema, and it is either valid or
refused with every reason. The same doctrine the contract split stands on — a physical
constraint beats a prose prohibition: development's advice has nowhere to live in the
report, because there is no field for it. The regex block anchors that used to live here
(``_BLOCK_START`` and kin) are gone WITH their whole refusal class.

WHAT DID NOT MOVE, and is said out loud because it is easy to lose: the published item's
field list (``ITEM_FIELDS``) and the identity formula (``finding_identity``) did not change
by one character. A format change is a change of the finding's TRANSPORT, not its essence:
dispositions given before the change keep covering the same findings after it, the dedup
registry is not re-seeded, and a rejected finding cannot ride back in through a technical
re-format.

THE IDENTITY IS THE DEDUP KEY, AND THE CONTRACT IS PART OF IT. An item's id is derived from
category + section + normalized text — the identity the spec assigns to a repeated finding —
PLUS the contract's fingerprint, because a finding is a claim about how a DESCRIBED reader
met the text: change the description and the same words are a different finding, owed its
own review. Baking the contract into the id is what makes that re-review structural — a
disposition is looked up by id, so a disposition given under the old contract simply stops
covering, with nothing to remember to reset. The reader does NOT supply identifiers: the
formula belongs to the informed side, and the id is derived at extraction.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from assistant_memory.audience.report_schema import (
    FINDINGS,
    FREE_VALVE,
    IMPRESSION,
    QUESTION_ANSWERS,
    QUESTIONS,
    RETELLING,
    SECTION_TABLE_DENOMINATOR,
    SectionBudgets,
    SectionSet,
)
from assistant_memory.audience.structured import RenderProfile, render_report, word_count

#: Exactly the fields a published blind-finding item carries — closed in both directions,
#: like every schema in this genre. The contract pair rides on EVERY item (the spec puts
#: both values in each finding), and the gate holds it against the reading's own record.
ITEM_FIELDS: frozenset[str] = frozenset(
    {
        "id", "section", "category", "text", "contract_item",
        "contract_version", "contract_sha256",
    }
)

#: A canonical contract-item identifier: C-N, no leading zeros.
_CANONICAL_ANCHOR = re.compile(r"C-(?:0|[1-9]\d*)")


def finding_identity(category: str, section: str, text: str, contract_sha256: str) -> str:
    """The dedup identity: category + section + normalized text + the contract it was read
    under. The contract is in the material so that changing the reader's description breaks
    the identity — and with it, silently inheriting a disposition given to other premises."""
    normalized = " ".join(text.split()).lower()
    material = (
        f"{category.strip().lower()}|{section.strip()}|{normalized}|{contract_sha256.strip()}"
    )
    return "B-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


# --- the answer's own fields (the reader's side of the schema, AG-24) ----------------------

_ROW_FIELDS = frozenset({"раздел", "находки"})
_FINDING_FIELDS = frozenset({"раздел", "категория", "текст", "пункт_контракта"})
_QUESTION_FIELDS = frozenset({"текст", "ответ"})


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _closed_object(
    item: Any, allowed: frozenset[str], what: str, reasons: list[str]
) -> Mapping[str, Any] | None:
    """One nested object against its closed field list; None when it cannot be read at all."""
    if not isinstance(item, Mapping):
        reasons.append(f"{what} не является объектом: {str(item)[:60]!r}")
        return None
    for name in sorted(set(item) - allowed):
        reasons.append(f"{what}: поле «{name}» вне закрытого перечня")
    for name in sorted(allowed - set(item)):
        reasons.append(f"{what}: нет обязательного поля «{name}»")
    return item if set(item) == allowed else None


def _budget_reasons(
    section: str, text: str, budgets: SectionBudgets, reasons: list[str]
) -> None:
    floor, ceiling = budgets.bounds(section)
    words = word_count(text)
    if floor is not None and words < floor:
        reasons.append(
            f"секция «{section}»: {words} слов при поле бюджета {floor} — у синтеза "
            "отсутствие пола порождает отписку, поэтому нарушение бюджета в любую сторону "
            "это отказ"
        )
    if ceiling is not None and words > ceiling:
        reasons.append(
            f"секция «{section}»: {words} слов при потолке бюджета {ceiling}"
        )


def report_refusals(
    obj: Mapping[str, Any],
    *,
    sections: SectionSet,
    budgets: SectionBudgets,
    categories: Sequence[str],
    section_ids: Sequence[str],
    public_items: Sequence[str],
) -> list[str]:
    """Every way this object is not a valid blind report of THIS derivation (empty = it is).

    The derived section set, the category names, the artifact's section denominator and the
    public item ids are ARGUMENTS, not lookups — the caller knows which derivation the
    reading actually ran under, because it is in the run record. All reasons are collected
    rather than short-circuited: the reasons list is what the annulled run's record carries,
    and whoever reads it wants the whole list, not one tripwire per retry.

    The closed-list rule works in both directions at the top level too, and that is what
    makes the conditional sections checkable: a section the derivation did not produce is an
    unknown field (refused), a derived section the answer lacks is a missing required one
    (refused) — the same two refusals, no special case.
    """
    reasons: list[str] = []
    expected = list(sections.names)
    for name in sorted(set(obj) - set(expected)):
        reasons.append(
            f"поле «{name}» вне выведенного набора секций этой версии — секция, которую "
            "никто не выводил, это отказ, не подарок"
        )
    for name in (n for n in expected if n not in obj):
        reasons.append(f"обязательная секция «{name}» отсутствует")

    # -- plain text sections, budgets included ------------------------------------------
    for name in expected:
        if name not in obj or name in (SECTION_TABLE_DENOMINATOR, FINDINGS, QUESTIONS):
            continue
        value = obj[name]
        if not isinstance(value, str):
            reasons.append(f"секция «{name}» обязана быть строкой, а несёт "
                           f"{type(value).__name__}")
            continue
        if name != FREE_VALVE and not value.strip():
            reasons.append(
                f"секция «{name}» семантически пуста — заполненная форма пустого поля; "
                "пустой вправе быть только строка свободного клапана"
            )
        _budget_reasons(name, value, budgets, reasons)

    # -- findings -------------------------------------------------------------------------
    findings_ok = False
    findings: list[Mapping[str, Any]] = []
    if FINDINGS in obj:
        raw = obj[FINDINGS]
        if not isinstance(raw, list):
            reasons.append(f"секция «{FINDINGS}» обязана быть массивом")
        else:
            findings_ok = True
            allowed_items = {str(i) for i in public_items}
            allowed_categories = {str(c) for c in categories}
            known_sections = {str(s) for s in section_ids}
            for position, item in enumerate(raw):
                checked = _closed_object(
                    item, _FINDING_FIELDS, f"замечание №{position}", reasons
                )
                if checked is None:
                    findings_ok = False
                    continue
                findings.append(checked)
                if str(checked["раздел"]) not in known_sections:
                    reasons.append(
                        f"замечание №{position}: раздел {checked['раздел']!r} не существует "
                        "в этой версии артефакта"
                    )
                if str(checked["категория"]) not in allowed_categories:
                    reasons.append(
                        f"замечание №{position}: категория {checked['категория']!r} вне "
                        "выведенного набора этой версии"
                    )
                if not _is_text(checked["текст"]):
                    reasons.append(f"замечание №{position}: поле «текст» семантически пусто")
                anchor = str(checked["пункт_контракта"])
                if not _CANONICAL_ANCHOR.fullmatch(anchor):
                    reasons.append(
                        f"замечание №{position}: якорь {anchor!r} не является каноническим "
                        "идентификатором пункта контракта (форма C-N без ведущих нулей)"
                    )
                elif anchor not in allowed_items:
                    reasons.append(
                        f"замечание №{position}: якорь {anchor} не разрешается в публичную "
                        "часть контракта этого чтения — якорь, разрешённый в ничто, не якорь"
                    )
            # One essence must not live in two copies: two entries with one identity would
            # publish one finding twice under one id, which the phase schema refuses later —
            # the boundary is where the reader can still be told.
            keys = [
                (
                    str(item.get("категория", "")).strip().lower(),
                    str(item.get("раздел", "")).strip(),
                    " ".join(str(item.get("текст", "")).split()).lower(),
                )
                for item in findings
            ]
            for key in sorted({k for k in keys if keys.count(k) > 1}):
                reasons.append(
                    f"замечание повторено дословно (раздел {key[1]}, категория {key[0]}) — "
                    "одна сущность в двух копиях"
                )

    # -- the denominator table, and the cross-checks with the findings --------------------
    if SECTION_TABLE_DENOMINATOR in obj:
        raw = obj[SECTION_TABLE_DENOMINATOR]
        if not isinstance(raw, list):
            reasons.append(f"секция «{SECTION_TABLE_DENOMINATOR}» обязана быть массивом")
        else:
            reported: list[str] = []
            referenced: list[int] = []
            rows_ok = True
            for position, row in enumerate(raw):
                checked = _closed_object(
                    row, _ROW_FIELDS, f"строка знаменателя №{position}", reasons
                )
                if checked is None:
                    rows_ok = False
                    continue
                section = str(checked["раздел"])
                reported.append(section)
                indexes = checked["находки"]
                if not isinstance(indexes, list) or any(
                    isinstance(i, bool) or not isinstance(i, int) for i in indexes
                ):
                    reasons.append(
                        f"строка знаменателя №{position}: «находки» обязаны быть массивом "
                        "целых индексов"
                    )
                    rows_ok = False
                    continue
                duplicated_in_row = sorted({i for i in indexes if indexes.count(i) > 1})
                if duplicated_in_row:
                    reasons.append(
                        f"строка знаменателя №{position}: индексы {duplicated_in_row} "
                        "названы дважды в одной строке"
                    )
                referenced += indexes
                if findings_ok:
                    for index in indexes:
                        if not 0 <= index < len(findings):
                            reasons.append(
                                f"строка знаменателя №{position}: индекс {index} не "
                                "существует в массиве замечаний"
                            )
                        elif str(findings[index]["раздел"]) != section:
                            reasons.append(
                                f"строка знаменателя №{position}: замечание №{index} "
                                f"относится к разделу {findings[index]['раздел']!r}, а "
                                f"перечислено в строке раздела {section!r}"
                            )
            expected_sections = [str(s) for s in section_ids]
            missing_rows = [s for s in expected_sections if s not in set(reported)]
            unknown_rows = sorted(set(reported) - set(expected_sections))
            duplicated = sorted({s for s in reported if reported.count(s) > 1})
            if missing_rows:
                reasons.append(
                    f"в знаменателе нет строк для разделов {missing_rows} — обязательная "
                    "строка на КАЖДЫЙ раздел, включая чистые: без неё чистый раздел "
                    "неотличим от непрочитанного"
                )
            if unknown_rows:
                reasons.append(
                    f"знаменатель называет разделы, которых нет в этой версии артефакта: "
                    f"{unknown_rows}"
                )
            if duplicated:
                reasons.append(
                    f"знаменатель называет разделы дважды: {duplicated} — одна строка на "
                    "раздел"
                )
            if findings_ok and rows_ok:
                orphaned = sorted(set(range(len(findings))) - set(referenced))
                if orphaned:
                    reasons.append(
                        f"замечания {orphaned} не перечислены ни в одной строке "
                        "знаменателя — каждая находка живёт ровно в одной строке"
                    )
                twice = sorted({i for i in referenced if referenced.count(i) > 1})
                if twice:
                    reasons.append(
                        f"замечания {twice} перечислены более чем в одной строке "
                        "знаменателя — каждая находка живёт ровно в одной строке"
                    )

    # -- questions ------------------------------------------------------------------------
    if QUESTIONS in obj:
        raw = obj[QUESTIONS]
        if not isinstance(raw, list):
            reasons.append(f"секция «{QUESTIONS}» обязана быть массивом")
        else:
            for position, question in enumerate(raw):
                checked = _closed_object(
                    question, _QUESTION_FIELDS, f"вопрос №{position}", reasons
                )
                if checked is None:
                    continue
                if not _is_text(checked["текст"]):
                    reasons.append(f"вопрос №{position}: поле «текст» семантически пусто")
                if checked["ответ"] not in QUESTION_ANSWERS:
                    reasons.append(
                        f"вопрос №{position}: «ответ» несёт {checked['ответ']!r}, а "
                        f"закрытый перечень: {', '.join(QUESTION_ANSWERS)}"
                    )
    return reasons


def extract_findings(
    obj: Mapping[str, Any], *, contract_version: Any, contract_sha256: str
) -> list[dict]:
    """The validated object's findings as published items — ids DERIVED, never asked for.

    Call only on an object ``report_refusals`` returned empty for: extraction is a transport
    step, not a second validation. The identity formula is untouched by the format change —
    that is what keeps every disposition given before the change covering the same finding
    after it.
    """
    return [
        {
            "id": finding_identity(
                str(item["категория"]), str(item["раздел"]), str(item["текст"]),
                contract_sha256,
            ),
            "section": str(item["раздел"]),
            "category": str(item["категория"]),
            "text": str(item["текст"]),
            "contract_item": str(item["пункт_контракта"]),
            "contract_version": contract_version,
            "contract_sha256": contract_sha256,
        }
        for item in obj[FINDINGS]
    ]


# --- the render profile (the blind instance of the shared skeleton) -------------------------


def _findings_lines(value: Any, _obj: Mapping[str, Any]) -> list[str]:
    items = list(value)
    if not items:
        return ["Замечаний нет — чистое чтение."]
    return [
        f"{position + 1}. [{item['раздел']}] {item['категория']} — {item['текст']} — "
        f"{item['пункт_контракта']}"
        for position, item in enumerate(items)
    ]


def _denominator_lines(value: Any, obj: Mapping[str, Any]) -> list[str]:
    lines = ["| Раздел | Вердикт |", "|---|---|"]
    for row in value:
        indexes = row["находки"]
        verdict = (
            "чисто"
            if not indexes
            else "замечания: " + ", ".join(f"№{i + 1}" for i in sorted(indexes))
        )
        lines.append(f"| {row['раздел']} | {verdict} |")
    return lines


def _question_lines(value: Any, _obj: Mapping[str, Any]) -> list[str]:
    if not value:
        return ["Вопросов нет."]
    return [f"- {question['текст']} — ответ: {question['ответ']}" for question in value]


BLIND_RENDER_PROFILE = RenderProfile(
    role="слепой читатель",
    titles={
        RETELLING: "Пересказ",
        SECTION_TABLE_DENOMINATOR: "Таблица по разделам",
        FINDINGS: "Замечания",
        QUESTIONS: "Вопросы",
        IMPRESSION: "Впечатление",
        "передать_наверх": "Передать наверх",
        "готовность_и_уверенность": "Готовность и уверенность",
        FREE_VALVE: "Что ещё заметил",
    },
    formatters={
        SECTION_TABLE_DENOMINATOR: _denominator_lines,
        FINDINGS: _findings_lines,
        QUESTIONS: _question_lines,
    },
)


def render_blind_report(
    obj: Mapping[str, Any],
    sections: SectionSet,
    *,
    retelling_diff: Mapping[str, Any] | None = None,
) -> str:
    """The validated report as the operator reads it. A derivative of the transcript: on any
    divergence the transcript is the truth — its path and hash are already in the record.

    ``retelling_diff`` is the mechanical layer's between-versions signal (AG-10), appended
    for the operator because the render is what he actually opens. It is an INPUT, not a
    lookup, so the render stays a pure function of its arguments — and it is informational:
    a diff section changes nothing about what the report says.
    """
    text = render_report(BLIND_RENDER_PROFILE, obj, sections.names)
    if retelling_diff is None:
        return text
    lines = ["", "## Дифф синтеза с прошлой версии (механический слой, информационно)", ""]
    for name, value in retelling_diff.items():
        if isinstance(value, str):
            lines.append(f"- «{name}»: {value}")
            continue
        lines.append(
            f"- «{name}»: слов {value['слов_было']} → {value['слов_стало']}, "
            f"убрано {value['убрано_слов']}, добавлено {value['добавлено_слов']}"
        )
        lines += [f"  {fragment}" for fragment in value["фрагменты"]]
    return text.rstrip() + "\n" + "\n".join(lines) + "\n"


# --- the published item (the channel's side — unchanged by the format change) ---------------


def item_refusals(item: Mapping) -> list[str]:
    """Ways one published item is not a well-formed blind finding (empty = it is)."""
    reasons = [
        f"поле «{name}» вне закрытого перечня элемента слепой находки"
        for name in sorted(set(item) - ITEM_FIELDS)
    ]
    reasons += [
        f"у элемента слепой находки нет поля «{name}»"
        for name in sorted(ITEM_FIELDS)
        if not str(item.get(name) or "").strip()
    ]
    if not reasons:
        expected = finding_identity(
            str(item["category"]),
            str(item["section"]),
            str(item["text"]),
            str(item["contract_sha256"]),
        )
        if item["id"] != expected:
            reasons.append(
                f"идентификатор элемента {item['id']!r} не выведен из его же содержимого — "
                "идентичность находки это её ключ дедупликации (включая контракт), а не "
                "свободное поле"
            )
        # The anchor is an IDENTIFIER, not free text: the reader's rule is "no contract
        # item — no finding", and an anchor in a non-canonical spelling (or no C-N shape at
        # all) is an anchor that resolves to nothing while looking resolved.
        if not _CANONICAL_ANCHOR.fullmatch(str(item["contract_item"])):
            reasons.append(
                f"якорь находки {item.get('id')} не является каноническим идентификатором "
                f"пункта контракта: {item['contract_item']!r} — ожидается форма C-N без "
                "ведущих нулей"
            )
    return reasons


def canonical_item(item: Mapping) -> tuple:
    """One item as the tuple its integrity is compared by — the WHOLE content.

    Comparing by id alone lets every field outside the identity material — the anchor
    above all — be rebound under a disposed id. The findings of a completed run are fixed
    with its transcript, so the comparison unit is the full canonical content; the carry
    metadata lives on the PHASE, not the item, and is deliberately not here.
    """
    return tuple(str(item.get(name) or "") for name in sorted(ITEM_FIELDS))


def findings_signature(payload: Mapping) -> tuple:
    """Every item of a findings phase, canonically, order-independent."""
    return tuple(
        sorted(
            canonical_item(item)
            for item in payload.get("findings") or []
            if isinstance(item, Mapping)
        )
    )


def items_refusals(items: Sequence[Mapping]) -> list[str]:
    """Validate a published findings list wholesale, duplicates included."""
    reasons: list[str] = []
    for item in items:
        reasons += item_refusals(item)
    ids = [str(item.get("id")) for item in items if isinstance(item, Mapping)]
    reasons += [
        f"идентификатор слепой находки {rid} выдан дважды в одной публикации"
        for rid in sorted({i for i in ids if ids.count(i) > 1})
    ]
    return reasons
