# SPDX-License-Identifier: Apache-2.0
"""The strategic reader: the third pass of the circle — it judges the DECLARED goal.

WHAT THE ROLE IS. The seeing pass judges truth, the blind pass judges intelligibility;
neither owns the question "does the text ACHIEVE what its author declared". Three classes
of findings from the pilot's free outside pass had no role, no category and no address —
and the pass that produced them also missed, precisely because it substituted a
genre-default goal ("a C-level deck = a pitch") for the declared one. Both lessons are
built in: the role holds BOTH parts of the contract, the desired takeaway and the delivery
policy verbatim, and judges exactly them; a category for "no request to the audience"
exists only when the operator's private flag declares the goal includes one — findings
about a goal nobody declared are cut off by category derivation, not by asking the model
to be careful.

ADMISSIBILITY IS A PREDICATE, NOT A PREFERENCE. Enabling the role REQUIRES a declared
desired takeaway: its subject is by definition the achievement of the declared goal, so
"enabled with no takeaway" is a configuration refusal with a named reason — never a silent
switch-off, which would let a review believe the role ran. The recommended default is
"on when the takeaway is declared"; the decision is the operator's at the pre-review gate,
and the review config records it explicitly.

ISOLATION HERE IS ISOLATION OF INFLUENCE, NOT OF KNOWLEDGE. The pass runs as a fresh
one-off session with no repository and no graph: the role does not need them, and
everything given beyond the necessary is surface for other roles' judgements to leak
through. The blind reports, the seeing reports, the dispositions registry and the claim
map are NOT shown — convergence of independent lenses is the argument that an obstacle is
real, and a shown foreign report turns convergence into an echo. The leak screen does NOT
apply — the private part belongs in this prompt by the role's definition — and the
assembler is a separate function whose signature the blind assembler's inputs cannot
satisfy: the one protection that survived the pilot is the one where the wrong call does
not compile.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from assistant_memory.audience.categories import CategoryRule, CategorySet
from assistant_memory.audience.contract import AudienceContract, PrivateFlag
from assistant_memory.audience.dedup import mark_refusals
from assistant_memory.audience.structured import (
    RenderProfile,
    canonical_scope,
    quote_refusals,
    render_report,
    role_findings_lines,
    scope_refusals,
    word_count,
)

#: The answer's own field lists — closed on every level. The PHASE element's field list is
#: NOT a second copy: it is derived from these plus the transfer pair (id, «дедуп»), which is
#: what "closedness is inherited from the answer schema" means in code.
SUMMARY = "итог"
FINDINGS = "находки"
FINDING_FIELDS: frozenset[str] = frozenset(
    {"категория", "scope", "текст", "цитаты", "пункт_контракта"}
)

#: The summary's ceiling in words — fixed by the spec (not a config knob like the blind
#: budgets): 200 words of connected judgement about the declared takeaway.
SUMMARY_CEILING_WORDS = 200

#: The rule table (AG-15) — the same device as the blind categories and AR-16: conditions
#: on fields and flags, never on prose. The unconditional rows exist exactly when the role
#: does (an enabled role implies a declared takeaway); the conditional one exists by the
#: operator's PRIVATE flag, because it describes a property of the author's goal.
STRATEGIC_CATEGORY_TABLE: tuple[CategoryRule, ...] = (
    CategoryRule(
        "ВЫНОС НЕ ДОСТИГАЕТСЯ",
        "всегда (при включённой роли)",
        "текст в сумме не приводит читателя к объявленному выносу",
    ),
    CategoryRule(
        "ОГОВОРКИ ОПРОКИДЫВАЮТ",
        "всегда (при включённой роли)",
        "каждая оговорка честна и обязательна по отдельности, но их суммарная плотность "
        "к финалу переворачивает впечатление против выноса",
    ),
    CategoryRule(
        "НЕЧЕСТНОЕ СОПОСТАВЛЕНИЕ",
        "всегда (при включённой роли)",
        "сопоставляются разные метрики или уровни агрегации так, что сравнение внушает "
        "не то, что верно, — при истинности каждой стороны по отдельности",
    ),
    CategoryRule(
        "ЗАПРОС ОТСУТСТВУЕТ ИЛИ СЛАБ",
        "приватный флаг: цель включает запрос к аудитории",
        "объявленная цель включает запрос к аудитории, а текст его не делает или делает "
        "так, что он не читается как запрос",
    ),
)


def _conditions(contract: AudienceContract) -> dict[str, bool]:
    return {
        "всегда (при включённой роли)": True,
        "приватный флаг: цель включает запрос к аудитории": bool(
            contract.private.flags.get(PrivateFlag.GOAL_INCLUDES_AUDIENCE_REQUEST)
        ),
    }


def derive_strategic_categories(contract: AudienceContract) -> CategorySet:
    """Which strategic categories this contract supports, and the version of that answer.

    The same versioning device as the blind set: the RULE TABLE with the values it was
    computed on, never the wording — a re-phrasing that changes no category must not move
    the version.
    """
    values = _conditions(contract)
    names = tuple(
        rule.name for rule in STRATEGIC_CATEGORY_TABLE if values[rule.condition]
    )
    material = json.dumps(
        {
            "table": [(rule.name, rule.condition) for rule in STRATEGIC_CATEGORY_TABLE],
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


def inclusion_refusals(enabled: bool, contract: AudienceContract) -> list[str]:
    """The admissibility predicate: enabling the role REQUIRES a declared takeaway.

    A refusal with a named reason, never a silent switch-off — a review where the role
    quietly did not run is indistinguishable from one where it ran clean.
    """
    if enabled and not contract.private.declared_takeaway():
        return [
            "стратегический читатель включён, а желаемый вынос в приватной части не "
            "объявлен — предмет роли по определению есть достижение объявленной цели, "
            "безвыносного режима у неё нет: это отказ конфигурации, а не тихое выключение"
        ]
    return []


def strategic_finding_identity(
    category: str, scope: Any, text: str, contract_sha256: str
) -> str:
    """The dedup identity: category + CANONICAL scope + normalized text + the FULL
    contract's hash. The full hash — not the public half — because the role reads the
    private part: change the takeaway and the same words judge a different goal, owed its
    own review."""
    scope_key = canonical_scope(scope)
    scope_text = scope_key if isinstance(scope_key, str) else "+".join(scope_key)
    normalized = " ".join(str(text).split()).lower()
    material = (
        f"{category.strip().lower()}|{scope_text}|{normalized}|{contract_sha256.strip()}"
    )
    return "SF-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def report_refusals(
    obj: Mapping[str, Any],
    *,
    categories: Sequence[str],
    section_ids: Sequence[str],
    section_texts: Mapping[str, str],
    contract_items: Sequence[str],
) -> list[str]:
    """Every way this object is not a valid strategic report (empty = it is).

    ``contract_items`` is the declared universe of BOTH parts — the role sees both, and an
    anchor may legally point into the private part (the takeaway, the delivery policy).
    Private wording is quoted only in the finding's «текст»; the «цитаты» array is for
    verbatim places of the ARTIFACT, and the quote-address check holds each against its
    scope's sections.
    """
    reasons: list[str] = []
    allowed_top = {SUMMARY, FINDINGS}
    for name in sorted(set(obj) - allowed_top):
        reasons.append(f"поле «{name}» вне закрытой схемы стратегического ответа")
    for name in sorted(allowed_top - set(obj)):
        reasons.append(f"обязательное поле «{name}» отсутствует")

    if SUMMARY in obj:
        summary = obj[SUMMARY]
        if not isinstance(summary, str) or not summary.strip():
            reasons.append(f"поле «{SUMMARY}» обязано быть непустой строкой")
        elif word_count(summary) > SUMMARY_CEILING_WORDS:
            reasons.append(
                f"поле «{SUMMARY}»: {word_count(summary)} слов при потолке "
                f"{SUMMARY_CEILING_WORDS}"
            )

    if FINDINGS in obj:
        raw = obj[FINDINGS]
        if not isinstance(raw, list):
            reasons.append(f"поле «{FINDINGS}» обязано быть массивом (пустой — чистый проход)")
        else:
            allowed_categories = {str(c) for c in categories}
            allowed_items = {str(i) for i in contract_items}
            keys: list[tuple] = []
            for position, item in enumerate(raw):
                what = f"стратегическая находка №{position}"
                if not isinstance(item, Mapping):
                    reasons.append(f"{what} не является объектом")
                    continue
                for name in sorted(set(item) - FINDING_FIELDS):
                    reasons.append(f"{what}: поле «{name}» вне закрытого перечня")
                for name in sorted(FINDING_FIELDS - set(item)):
                    reasons.append(f"{what}: нет обязательного поля «{name}»")
                if set(item) != FINDING_FIELDS:
                    continue
                if str(item["категория"]) not in allowed_categories:
                    reasons.append(
                        f"{what}: категория {item['категория']!r} вне выведенного набора "
                        "этой версии"
                    )
                reasons += scope_refusals(item["scope"], section_ids, what)
                if not isinstance(item["текст"], str) or not item["текст"].strip():
                    reasons.append(f"{what}: поле «текст» семантически пусто")
                reasons += quote_refusals(item["цитаты"], item["scope"], section_texts, what)
                if str(item["пункт_контракта"]) not in allowed_items:
                    reasons.append(
                        f"{what}: якорь {item['пункт_контракта']!r} не разрешается в "
                        "объявленные пункты контракта (обеих частей)"
                    )
                keys.append(
                    (
                        str(item["категория"]).strip().lower(),
                        canonical_scope(item["scope"]),
                        " ".join(str(item["текст"]).split()).lower(),
                    )
                )
            for key in sorted({k for k in keys if keys.count(k) > 1}, key=str):
                reasons.append(
                    f"стратегическая находка повторена дословно (scope {key[1]!r}, "
                    f"категория {key[0]}) — одна сущность в двух копиях"
                )
    return reasons


def extract_findings(obj: Mapping[str, Any], *, contract_sha256: str) -> list[dict]:
    """The validated object's findings as phase items — ids DERIVED by the identity
    formula, the dedup marks added by the dedup step afterwards. Call only on an object
    ``report_refusals`` returned empty for."""
    return [
        {
            "категория": str(item["категория"]),
            "scope": item["scope"],
            "текст": str(item["текст"]),
            "цитаты": list(item["цитаты"]),
            "пункт_контракта": str(item["пункт_контракта"]),
            "id": strategic_finding_identity(
                str(item["категория"]), item["scope"], str(item["текст"]), contract_sha256
            ),
        }
        for item in obj[FINDINGS]
    ]


STRATEGIC_RENDER_PROFILE = RenderProfile(
    role="стратегический читатель",
    titles={SUMMARY: "Итог", FINDINGS: "Находки"},
    formatters={FINDINGS: lambda v, o: role_findings_lines(v, o, with_anchor=True)},
)


def render_strategic_report(obj: Mapping[str, Any]) -> str:
    """The one render skeleton, this role's profile — deterministic, per the common
    structured-answer contract."""
    return render_report(STRATEGIC_RENDER_PROFILE, obj, (SUMMARY, FINDINGS))


#: The PHASE element's field list — NOT a second copy of the answer schema: derived from it
#: plus exactly the transfer pair, which is what "closedness is inherited" means in code.
PHASE_ITEM_FIELDS: frozenset[str] = frozenset(FINDING_FIELDS | {"id", "дедуп"})


def phase_item_refusals(item: Mapping[str, Any], *, contract_sha256: str) -> list[str]:
    """Ways one PUBLISHED strategic phase item cannot be trusted (empty = it can).

    Artifact-independent by design — the channel gate holds no artifact, so the checks
    here are the closed field list, the derived identity and the dedup mark; the
    artifact-dependent checks (quote-address, section existence) ran at the boundary and
    annulled the run on failure.
    """
    what = f"стратегическая находка {item.get('id', '?')}"
    reasons = [
        f"{what}: поле «{name}» вне закрытого перечня элемента фазы"
        for name in sorted(set(item) - PHASE_ITEM_FIELDS)
    ]
    reasons += [
        f"{what}: нет поля «{name}»"
        for name in sorted(PHASE_ITEM_FIELDS - set(item))
    ]
    if reasons:
        return reasons
    expected = strategic_finding_identity(
        str(item["категория"]), item["scope"], str(item["текст"]), contract_sha256
    )
    if item["id"] != expected:
        reasons.append(
            f"{what}: идентификатор не выведен из содержимого — идентичность находки это "
            "её ключ дедупликации (включая полный контракт), а не свободное поле"
        )
    reasons += mark_refusals(item.get("дедуп"), what)
    return reasons


def phase_items_refusals(
    items: Sequence[Mapping[str, Any]], *, contract_sha256: str
) -> list[str]:
    reasons: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            reasons.append(f"элемент фазы не является объектом: {str(item)[:60]!r}")
            continue
        reasons += phase_item_refusals(item, contract_sha256=contract_sha256)
    ids = [str(i.get("id")) for i in items if isinstance(i, Mapping)]
    reasons += [
        f"идентификатор стратегической находки {rid} выдан дважды в одной публикации"
        for rid in sorted({i for i in ids if ids.count(i) > 1})
    ]
    return reasons
