# SPDX-License-Identifier: Apache-2.0
"""The common contract of a role's structured answer — one copy, every role an instance.

WHY ONE MODULE AND NOT THREE DISCIPLINES. The genre now has more than one role that answers
with a structured report (the blind reader today; the spec names two more), and the first
review of the v2 spec caught the exact class this module closes: a schema declared for one
role and not declared for another. Three hand-rolled copies of "one JSON object, closed
schema, validate before crediting" drift the same way two copies of one list drift — so the
shared halves live here, and a role brings only its own schema and its own render profile.

WHAT THE CONTRACT SAYS, in the order things happen:

1. The answer is EXACTLY ONE JSON object; one surrounding code fence is tolerated (the same
   markdown tolerance the anchors already extend). Any substantive text outside the object
   is a refusal — prose next to the object is exactly the free-form report whose failure
   class (findings existing that the parser cannot see) this format buries.

2. Validation happens AT THE BOUNDARY, in the runner, BEFORE crediting. A run whose answer
   is refused is ANNULLED: its record carries the outcome «не состоялся» — one state, two
   names — with the full list of reasons. One automatic retry is allowed, as a NEW run whose
   record names the annulled one; a second refusal in a row goes to the operator, because a
   model that cannot hold a schema is a property of the instrument, not a quota expense.

3. The human-readable form is built by a DETERMINISTIC renderer in code — one skeleton for
   all roles (this module's), one render profile per role — after successful validation and
   BEFORE crediting, and the render's path and sha256 ride in the run record where the
   crediting gate can demand them.

WORD COUNTING IS DEFINED ONCE, here, for every budget of every role: whitespace collapses,
a word is a non-empty token between spaces. A budget compared under two counting rules is
two budgets wearing one number.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def word_count(text: str) -> int:
    """The one counting rule for every section budget: whitespace collapses, a word is a
    non-empty token between spaces."""
    return len(str(text).split())


#: One optional surrounding fence — the same tolerance to markdown furniture the block
#: anchors already extend. The label is free (```json, ```JSON, bare ```); what is NOT free
#: is anything substantive outside the fence.
_FENCE = re.compile(r"\A```[\w-]*[ \t]*\n(?P<body>.*)\n```\s*\Z", re.DOTALL)


def findings_signature_json(payload: Mapping[str, Any]) -> tuple:
    """Every finding of a role phase, canonically, order-independent — the comparison unit
    of the phase-integrity and carry checks. Full canonical content, never ids alone: an id
    pins only the identity material, and everything outside it could be rebound under a
    disposed id."""
    return tuple(
        sorted(
            json.dumps(dict(item), ensure_ascii=False, sort_keys=True)
            for item in payload.get("findings") or []
            if isinstance(item, Mapping)
        )
    )


def parse_structured_answer(answer: str) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Exactly one JSON object, or every reason it is not one.

    Returns ``(object, [])`` or ``(None, reasons)``. The rule is the contract's first
    clause verbatim: one object, one optional fence, and any substantive text outside the
    object is a refusal — an aside living next to the object is a finding the schema cannot
    see, which is the failure class this format exists to bury.
    """
    text = answer.strip()
    if not text:
        return None, ["ответ пуст — валидировать нечего"]
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group("body").strip()
    try:
        obj, end = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as broken:
        return None, [
            f"ответ не разбирается как JSON-объект ({broken.msg}, строка {broken.lineno}) — "
            "контракт ответа: ровно один объект, допустима одна ограда ```json"
        ]
    if text[end:].strip():
        return None, [
            "вне JSON-объекта есть содержательный текст "
            f"({text[end:].strip()[:80]!r}) — ответ это ровно один объект: замечанию, "
            "живущему рядом с объектом, некуда разрешиться, и оно молча перестало бы "
            "существовать"
        ]
    if not isinstance(obj, dict):
        return None, [
            f"верхний уровень ответа — {type(obj).__name__}, а контракт требует объект"
        ]
    return obj, []


# --- scope and quotes: shared by every role whose findings address the artifact ------------

#: The reserved scope literal: an effect smeared over the whole artifact gets a legal
#: address instead of being stretched onto one section.
WHOLE_SCOPE = "целое"


def canonical_scope(scope: Any) -> Any:
    """The ONE canonicalisation of a finding's scope — sorted and deduplicated, named once
    and used both at validation and inside the identity formulas; the literal «целое» needs
    none. Two canonicalisations would let one finding carry two identities."""
    if scope == WHOLE_SCOPE:
        return WHOLE_SCOPE
    if isinstance(scope, (list, tuple)):
        return tuple(sorted({str(section) for section in scope}))
    return scope


def scope_refusals(scope: Any, known_sections: Sequence[str], what: str) -> list[str]:
    """Ways one finding's scope is not a legal address (empty = it is)."""
    if scope == WHOLE_SCOPE:
        return []
    if not isinstance(scope, (list, tuple)) or not scope:
        return [
            f"{what}: scope обязан быть либо литералом «{WHOLE_SCOPE}», либо непустым "
            f"списком разделов S-N, а несёт {str(scope)[:60]!r}"
        ]
    known = {str(section) for section in known_sections}
    # B.11 C-1: the artifact's section ids are a closed, published list — a scope naming
    # one that does not exist is reproduced by an identical prompt.
    return [
        closed_vocabulary(
            f"{what}: scope называет раздел {section!r}, которого нет в этой версии артефакта"
        )
        for section in scope
        if str(section) not in known
    ]


def _collapsed(text: str) -> str:
    return " ".join(str(text).split())


def quote_refusals(
    quotes: Any,
    scope: Any,
    section_texts: Mapping[str, str],
    what: str,
) -> list[str]:
    """The quote-address check: every quote must occur VERBATIM (after whitespace collapse,
    as everywhere in normalisation) in the text of one of its scope's sections; for the
    scope «целое» — in any section of the version. A failing quote refuses the crediting:
    a quote that is not in the text it addresses is a finding about some other text.
    """
    if not isinstance(quotes, list) or not quotes or any(
        not isinstance(q, str) or not q.strip() for q in quotes
    ):
        return [
            f"{what}: «цитаты» обязаны быть непустым массивом непустых строк — цитаты "
            "конкретных мест и есть адресность находки"
        ]
    if scope == WHOLE_SCOPE:
        searched = list(section_texts)
    else:
        searched = [str(s) for s in scope] if isinstance(scope, (list, tuple)) else []
    haystacks = [_collapsed(section_texts.get(name, "")) for name in searched]
    return [
        f"{what}: цитата {quote.strip()[:60]!r} не встречается дословно в тексте "
        f"разделов её scope ({', '.join(searched) or '—'})"
        for quote in quotes
        if not any(_collapsed(quote) in haystack for haystack in haystacks)
    ]


# --- the render skeleton -------------------------------------------------------------------
#
# One skeleton for every role, one profile per role. The skeleton owns the walk — section
# order, headings, the file's overall shape — and the profile owns only how one section's
# value becomes lines. A formatter receives the WHOLE object alongside the value, because a
# section may legitimately render against another (the blind denominator table numbers its
# findings by their position in the findings list).

#: How one section's value becomes lines of the render. ``(value, whole_object) -> lines``.
SectionFormatter = Callable[[Any, Mapping[str, Any]], list[str]]


@dataclass(frozen=True)
class RenderProfile:
    """One role's rendering: its name, its headings, its per-section formatters.

    A section without a formatter renders as plain text — the common case; anything richer
    (tables, numbered lists) is the role's own and lives in its profile.
    """

    role: str
    titles: Mapping[str, str]
    formatters: Mapping[str, SectionFormatter]


def _text_lines(value: Any, _obj: Mapping[str, Any]) -> list[str]:
    text = str(value).strip()
    return [text if text else "—"]


def role_findings_lines(
    value: Any, _obj: Mapping[str, Any], *, with_anchor: bool
) -> list[str]:
    """The scoped-findings list as the operator reads it — one shape for both v2 roles
    (the strategic one shows its contract anchor, the machine one has none)."""
    items = list(value)
    if not items:
        return ["Находок нет — чистый проход."]
    lines: list[str] = []
    for position, item in enumerate(items):
        scope = canonical_scope(item["scope"])
        label = scope if isinstance(scope, str) else ", ".join(scope)
        anchor = f" — {item['пункт_контракта']}" if with_anchor else ""
        lines.append(
            f"{position + 1}. [{label}] {item['категория']} — {item['текст']}{anchor}"
        )
        lines += [f"   > {quote}" for quote in item["цитаты"]]
    return lines


def render_report(
    profile: RenderProfile, obj: Mapping[str, Any], order: Sequence[str]
) -> str:
    """The validated object as a human-readable file — deterministically, or not at all.

    A pure function of (profile, object, section order): no clock, no environment, no
    randomness — the record carries this render's hash, and a hash of an unreproducible
    text binds nothing. Sections render in the ORDER given (the derived section set's),
    and only the sections the object carries: a conditional section absent from a valid
    object was not derived for this contract.
    """
    lines: list[str] = [f"# Отчёт: {profile.role}", ""]
    for name in order:
        if name not in obj:
            continue
        lines.append(f"## {profile.titles.get(name, name)}")
        lines.append("")
        formatter = profile.formatters.get(name, _text_lines)
        lines += formatter(obj[name], obj)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class ClosedVocabularyRefusal(str):
    """A refusal that names a value outside a CLOSED vocabulary (B.11 C-1).

    A `str` subclass and nothing more: it joins, formats and lands in a run record exactly
    like the plain refusals beside it, and NOTHING about it is visible to the operator.
    What it carries is the one fact the retry clause needs and could not otherwise have —
    that an identical prompt reproduces this refusal by construction.

    Why a type rather than a prefix in the text: these strings are read by the operator,
    and a machine tag inside operator-facing prose is the same defect this project keeps
    finding elsewhere — one carrier, two audiences. Why a type rather than a regex over
    the prose: matching refusal wording would be a lexical sweep, and a lexical sweep over
    prose is not a class closure (measured three rounds running in this slice's own spec
    review). A new closed vocabulary gets classified correctly the moment its validator
    calls `closed_vocabulary`, and is otherwise simply not claimed to be deterministic.
    """


def closed_vocabulary(text: str) -> ClosedVocabularyRefusal:
    """Build a refusal that says: this value is not a member of a fixed, published list."""
    return ClosedVocabularyRefusal(text)


def annulment_is_deterministic(refusals) -> bool:
    """Would an identical prompt reproduce this annulment? (B.11 C-1)

    STRUCTURAL, and deliberately narrow. True only when some refusal was BUILT as a
    closed-vocabulary violation — the case measured in round 5 of audience review
    66651b62 (2026-08-22), where the machine-comb role was annulled twice with the same
    cause: the category «ШАМП» is not in the table, whose real member is «ШТАМП», and
    gpt-5.6-terra drops the letter. Both the original run and the automatic retry made the
    error, twice each.

    Everything else stays transient and keeps its retry, including a merely malformed or
    truncated answer: the retry clause rests on treating a validation failure as an
    independent random event, and against a mangled envelope that assumption is fine. It
    is only against a deterministic model error that the repeat reproduces the error and
    buys nothing.

    ERRING TOWARD TRANSIENT IS THE CHEAPER MISTAKE HERE, which is why the predicate claims
    only what it can see: a wrong "transient" costs one run, while a wrong "deterministic"
    spends the operator's attention — and this project treats that as the expensive one.
    """
    return any(isinstance(r, ClosedVocabularyRefusal) for r in refusals or ())
