# SPDX-License-Identifier: Apache-2.0
"""Dedup of repeated findings — the machine step on the INFORMED side (AR-22 mechanics).

WHY THE INFORMED SIDE. A role with no memory between versions re-raises what was already
rejected (measured on the pilot's pass four: three findings of four were repeats of
matter closed in Q&A prep). But the registry of the settled cannot be fed to the role —
that is knowledge moved into an unknowing state, and the role would fall silent exactly
where the audience stumbles. So dedup lives here, marks ride to the operator, and a repeat
becomes a SIGNAL on the way: independent reproduction across versions is an argument that
the obstacle is real.

THE ASYMMETRY IS THE RULE. Only an EXACT identity match is a repeat (marked with the
original and its disposition); a match on category + scope with diverged text is a
CANDIDATE, marked and never auto-closed. An extra mark costs the operator one glance; a
false closure removes a real finding from the board and does it silently.

Three sets are consulted, not one: the dispositions registry, this same run's own findings
(internal doubles — the exact ones are refused by the answer schema upstream, so what can
reach here is the coarse kind), and the still-open findings of past iterations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from assistant_memory.audience.structured import canonical_scope

#: The mark's closed vocabulary and closed field list — a mark nobody can read is a mark
#: nobody can act on, and the phase schema validates these on the way in as well.
NEW = "новая"
REPEAT = "повтор"
CANDIDATE = "кандидат"
MARK_VERDICTS: tuple[str, ...] = (NEW, REPEAT, CANDIDATE)
MARK_FIELDS: frozenset[str] = frozenset({"вердикт", "оригинал", "диспозиция"})


def coarse_key(item: Mapping[str, Any]) -> tuple:
    """Category + canonical scope — the near-match key of AR-22, one copy."""
    return (
        str(item.get("категория", "")).strip().lower(),
        canonical_scope(item.get("scope")),
    )


def mark_findings(
    items: Sequence[Mapping[str, Any]],
    *,
    disposed: Mapping[str, str],
    prior_open: Sequence[Mapping[str, Any]],
) -> list[dict]:
    """Each finding with its dedup mark attached — the transfer step of the phase.

    ``items`` are the phase items ALREADY carrying their derived ``id``; ``disposed`` maps
    finding id → terminal outcome from the dispositions registry; ``prior_open`` are the
    not-yet-disposed findings of past iterations (with their ids). Returns new dicts; the
    inputs are not mutated.
    """
    prior_by_id = {str(p.get("id")): p for p in prior_open}
    prior_by_coarse: dict[tuple, str] = {}
    for prior in prior_open:
        prior_by_coarse.setdefault(coarse_key(prior), str(prior.get("id")))

    marked: list[dict] = []
    seen_coarse: dict[tuple, str] = {}
    for item in items:
        item_id = str(item.get("id"))
        key = coarse_key(item)
        if item_id in disposed:
            mark = {"вердикт": REPEAT, "оригинал": item_id, "диспозиция": disposed[item_id]}
        elif item_id in prior_by_id:
            mark = {"вердикт": REPEAT, "оригинал": item_id, "диспозиция": "открыта"}
        elif key in seen_coarse:
            # the same run's own near-double: the exact double is refused by the answer
            # schema upstream, so what reaches here is the coarse kind
            mark = {"вердикт": CANDIDATE, "оригинал": seen_coarse[key]}
        elif key in prior_by_coarse:
            mark = {"вердикт": CANDIDATE, "оригинал": prior_by_coarse[key]}
        else:
            mark = {"вердикт": NEW}
        seen_coarse.setdefault(key, item_id)
        marked.append({**dict(item), "дедуп": mark})
    return marked


def mark_refusals(mark: Any, what: str) -> list[str]:
    """Ways one dedup mark is not readable (empty = it is) — checked at the phase schema,
    because a mark written by hand or by a drifted version must refuse, not pass."""
    if not isinstance(mark, Mapping):
        return [f"{what}: пометка дедупа не является объектом"]
    reasons = [
        f"{what}: поле пометки дедупа «{name}» вне закрытого перечня"
        for name in sorted(set(mark) - MARK_FIELDS)
    ]
    verdict = mark.get("вердикт")
    if verdict not in MARK_VERDICTS:
        reasons.append(
            f"{what}: вердикт пометки дедупа {verdict!r} вне перечня "
            f"({', '.join(MARK_VERDICTS)})"
        )
        return reasons
    if verdict == NEW and set(mark) != {"вердикт"}:
        reasons.append(f"{what}: пометка «{NEW}» не несёт ничего, кроме вердикта")
    if verdict == REPEAT:
        for name in ("оригинал", "диспозиция"):
            if not str(mark.get(name) or "").strip():
                reasons.append(
                    f"{what}: пометка «{REPEAT}» обязана нести «{name}» — повтор без "
                    "ссылки на оригинал и его диспозицию нечем проверить"
                )
    if verdict == CANDIDATE and not str(mark.get("оригинал") or "").strip():
        reasons.append(
            f"{what}: пометка «{CANDIDATE}» обязана нести «оригинал» — кандидат в "
            "повторы без оригинала не кандидат"
        )
    return reasons
