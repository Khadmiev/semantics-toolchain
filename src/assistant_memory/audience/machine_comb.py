# SPDX-License-Identifier: Apache-2.0
"""The machine comb: the fourth pass of the circle — it hunts marks of machine authorship.

WHY A SEPARATE LENS. The pilot's lesson, operator's words verbatim: «идея в том, чтобы
критик отдельно искал места, где хорошо видно, что текст написан ЛЛМ»; «длинные тире —
туда же». The class "LLM calque / unnatural language" was caught by no standing reader —
the blind one plays the audience, the seeing one checks truth, the strategic one judges
the goal, and each frame looks past it. A separate lens is the only construction under
which this class BELONGS to somebody. Enabled by default for every audience review (the
review config still records that explicitly): the role's whole value is catching what the
author has stopped seeing, it is the cheapest pass of the circle (text only), and the
price of the default is minimal. The operator switches it off at the pre-review gate.

WHAT THE PROMPT CARRIES, and the boundary is the assembler's signature: of the SOURCE
MATERIALS — only the artifact and the public part's LANGUAGE fields (delivery language,
proficiency), which are input for judging a calque, never a condition for a category to
exist. No contract beyond that, no repository, no graph. The role's own instructions —
template, answer-schema block, the category table in full — ride in by the common
structured-answer contract. No knowledge isolation is claimed and no canary runs: the same
boundary as the strategic and visual passes.

THE TABLE IS FIXED, NOT DERIVED: machineness is a property of the text, not of the
audience. It is versioned by its own text and extended by a SPEC edit, never a prompt
edit — a category living only in a prompt is unversioned and outside the fingerprint. The
split into four shelves is development's judgement, contestable by review; the axis and the
long-dash item are the operator's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from assistant_memory.audience.categories import CategoryRule
from assistant_memory.audience.dedup import mark_refusals
from assistant_memory.audience.structured import (
    RenderProfile,
    canonical_scope,
    closed_vocabulary,
    quote_refusals,
    render_report,
    role_findings_lines,
    scope_refusals,
)

FINDINGS = "находки"
FINDING_FIELDS: frozenset[str] = frozenset({"категория", "scope", "текст", "цитаты"})

#: The fixed table (AG-28). The `condition` slot is constant — kept as the shared
#: CategoryRule shape so the prompt rendering is one mechanism, not two.
MACHINE_CATEGORY_TABLE: tuple[CategoryRule, ...] = (
    CategoryRule(
        "КАЛЬКА",
        "всегда",
        "фраза, которую носитель языка подачи так не скажет: машинный перенос конструкции "
        "из другого языка, неестественный порядок слов",
    ),
    CategoryRule(
        "ШТАМП",
        "всегда",
        "канцелярит и штампы машинного письма: «важно отметить», «стоит подчеркнуть», "
        "«в современном мире», «играет ключевую роль»",
    ),
    CategoryRule(
        "ШАБЛОННАЯ СТРУКТУРА",
        "всегда",
        "ритмические параллелизмы, тройчатки, каскады «не X, а Y», перечислительная "
        "сыпь, одинаково скроенные абзацы",
    ),
    CategoryRule(
        "МАРКЕР",
        "всегда",
        "обороты-маркеры машинного текста: злоупотребление длинными тире, однотипные "
        "связки («по сути», «в конечном счёте»), избыточные симметричные оговорки",
    ),
)

MACHINE_CATEGORY_NAMES: tuple[str, ...] = tuple(r.name for r in MACHINE_CATEGORY_TABLE)


def machine_table_version() -> str:
    """The table's identity — a hash of its TEXT (names and descriptions): this table is
    fixed, so unlike the derived sets a re-wording IS a change of the instrument, and the
    version must move with it. Part of the machine fingerprint."""
    material = "\n".join(f"{r.name}|{r.description}" for r in MACHINE_CATEGORY_TABLE)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def machine_finding_identity(
    category: str, scope: Any, text: str, table_version: str
) -> str:
    """The dedup identity: category + canonical scope + normalized text + the CATEGORY
    TABLE's version — the role sees no contract, so the table is the instrument whose
    change makes the same words a different finding."""
    scope_key = canonical_scope(scope)
    scope_text = scope_key if isinstance(scope_key, str) else "+".join(scope_key)
    normalized = " ".join(str(text).split()).lower()
    material = f"{category.strip().lower()}|{scope_text}|{normalized}|{table_version.strip()}"
    return "MF-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def report_refusals(
    obj: Mapping[str, Any],
    *,
    section_ids: Sequence[str],
    section_texts: Mapping[str, str],
    table_version: str | None = None,
) -> list[str]:
    """Every way this object is not a valid machine-comb report (empty = it is).

    No contract anchor exists in this schema BY CONSTRUCTION: demanding a `C-N` of a role
    that was never shown the contract would mean feeding it the contract for a formality.
    """
    del table_version  # the categories are the fixed table's; kept for signature symmetry
    reasons: list[str] = []
    for name in sorted(set(obj) - {FINDINGS}):
        reasons.append(f"поле «{name}» вне закрытой схемы машинного ответа")
    if FINDINGS not in obj:
        return reasons + [f"обязательное поле «{FINDINGS}» отсутствует"]
    raw = obj[FINDINGS]
    if not isinstance(raw, list):
        return reasons + [
            f"поле «{FINDINGS}» обязано быть массивом (пустой — чистый проход)"
        ]
    allowed = set(MACHINE_CATEGORY_NAMES)
    keys: list[tuple] = []
    for position, item in enumerate(raw):
        what = f"машинная находка №{position}"
        if not isinstance(item, Mapping):
            reasons.append(f"{what} не является объектом")
            continue
        for name in sorted(set(item) - FINDING_FIELDS):
            reasons.append(f"{what}: поле «{name}» вне закрытого перечня")
        for name in sorted(FINDING_FIELDS - set(item)):
            reasons.append(f"{what}: нет обязательного поля «{name}»")
        if set(item) != FINDING_FIELDS:
            continue
        if str(item["категория"]) not in allowed:
            # B.11 C-1: THE MEASURED CASE. «ШАМП» for «ШТАМП» — one dropped letter, made
            # by the same model on the run and on its automatic retry, twice each
            # (audience review 66651b62 round 5, 2026-08-22). Marked as a closed-vocabulary
            # violation so the retry clause does not spend a run reproducing it.
            reasons.append(
                closed_vocabulary(
                    f"{what}: категория {item['категория']!r} вне таблицы машинности"
                )
            )
        reasons += scope_refusals(item["scope"], section_ids, what)
        if not isinstance(item["текст"], str) or not item["текст"].strip():
            reasons.append(f"{what}: поле «текст» семантически пусто")
        reasons += quote_refusals(item["цитаты"], item["scope"], section_texts, what)
        keys.append(
            (
                str(item["категория"]).strip().lower(),
                canonical_scope(item["scope"]),
                " ".join(str(item["текст"]).split()).lower(),
            )
        )
    for key in sorted({k for k in keys if keys.count(k) > 1}, key=str):
        reasons.append(
            f"машинная находка повторена дословно (scope {key[1]!r}, категория "
            f"{key[0]}) — одна сущность в двух копиях"
        )
    return reasons


def extract_findings(obj: Mapping[str, Any], *, table_version: str) -> list[dict]:
    """The validated object's findings as phase items — ids derived, marks added later."""
    return [
        {
            "категория": str(item["категория"]),
            "scope": item["scope"],
            "текст": str(item["текст"]),
            "цитаты": list(item["цитаты"]),
            "id": machine_finding_identity(
                str(item["категория"]), item["scope"], str(item["текст"]), table_version
            ),
        }
        for item in obj[FINDINGS]
    ]


MACHINE_RENDER_PROFILE = RenderProfile(
    role="прочёс машинности",
    titles={FINDINGS: "Находки"},
    formatters={FINDINGS: lambda v, o: role_findings_lines(v, o, with_anchor=False)},
)


def render_machine_report(obj: Mapping[str, Any]) -> str:
    """The one render skeleton, this role's profile."""
    return render_report(MACHINE_RENDER_PROFILE, obj, (FINDINGS,))


#: Derived from the answer schema plus the transfer pair — never a second list.
PHASE_ITEM_FIELDS: frozenset[str] = frozenset(FINDING_FIELDS | {"id", "дедуп"})


def phase_item_refusals(item: Mapping[str, Any], *, table_version: str) -> list[str]:
    """Ways one PUBLISHED machine phase item cannot be trusted (empty = it can) — the
    artifact-independent half, exactly as the strategic twin explains."""
    what = f"машинная находка {item.get('id', '?')}"
    reasons = [
        f"{what}: поле «{name}» вне закрытого перечня элемента фазы"
        for name in sorted(set(item) - PHASE_ITEM_FIELDS)
    ]
    reasons += [
        f"{what}: нет поля «{name}»" for name in sorted(PHASE_ITEM_FIELDS - set(item))
    ]
    if reasons:
        return reasons
    expected = machine_finding_identity(
        str(item["категория"]), item["scope"], str(item["текст"]), table_version
    )
    if item["id"] != expected:
        reasons.append(
            f"{what}: идентификатор не выведен из содержимого — идентичность находки это "
            "её ключ дедупликации (включая версию таблицы категорий), а не свободное поле"
        )
    reasons += mark_refusals(item.get("дедуп"), what)
    return reasons


def phase_items_refusals(
    items: Sequence[Mapping[str, Any]], *, table_version: str
) -> list[str]:
    reasons: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            reasons.append(f"элемент фазы не является объектом: {str(item)[:60]!r}")
            continue
        reasons += phase_item_refusals(item, table_version=table_version)
    ids = [str(i.get("id")) for i in items if isinstance(i, Mapping)]
    reasons += [
        f"идентификатор машинной находки {rid} выдан дважды в одной публикации"
        for rid in sorted({i for i in ids if ids.count(i) > 1})
    ]
    return reasons
