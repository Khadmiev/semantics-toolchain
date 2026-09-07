# SPDX-License-Identifier: Apache-2.0
"""The mechanical layer: what a reader should never be asked to count.

Deterministic, spends no model quota, runs BEFORE the model passes — and it never blocks
convergence at any threshold value. It marks candidates; the reader judges. Every number
it produces is a hypothesis until it has been calibrated against the places a real reader
named unprompted, and saying so is part of the output rather than a footnote.

TWO BRANCHES, CHOSEN BY THE PART AND NOT BY THE ARTEFACT. A composite artefact carries a
slide plan, a text to be read aloud and figures for charts at once, so the branch follows
the part: spoken text is timed against its slot at words per minute; a document is measured
by how it reads.

WHAT THE DOCUMENT BRANCH COUNTS, AND WHY IT IS NOT WHAT THE SPEC FIRST SAID. The spec said
a document is not timed at all — only returns to terms are counted. That was wrong, and the
operator corrected it on 2026-08-05: SIZE STILL DECIDES. Nobody starts a twenty-five-page
document, however cleanly it is written. So volume gives the floor of the reading estimate,
and returns to terms push that floor UP rather than replacing it. The two then stay legible
as two different signals — "too long" and "too tangled" — instead of one blended number in
which neither can be seen.

WHY FRESHNESS AND NOT DISTANCE ALONE. The forgetting model is not length: a term used right
after it was explained needs no return at all, while new terms introduced in between crowd
the old one out faster than plain distance does. So an occurrence goes stale on EITHER
counter, and each stale one costs the reader a declared price in words.

WHAT IS DECLARED RATHER THAN GUESSED. Two inventories, and both are handed in, never
inferred from prose: what the audience already knows (a property of the audience, i.e. of
the contract) and what the artefact introduces (a property of the artefact). The tracked
inventory is the second minus the first. This is the same rule the category derivation
follows — where the answer lives in free text, it is declared explicitly, because an
inferred inventory would either find nothing or flood the report, and both failures look
like work. An UNDECLARED inventory is a refusal with a reason, never a silent empty run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from assistant_memory.audience.report_schema import READINESS, RETELLING, UPWARD
from assistant_memory.audience.sections import PartKind, ReaderArtifact


class MechanicalRefused(ValueError):
    """The layer did not run, with every reason rather than the first.

    A refusal, not an empty report: a report computed over an undeclared inventory is
    indistinguishable from a clean one, and the layer would then be a formality that always
    passes.
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class MechanicalThresholds:
    """Every number the layer uses, each carrying its unit in its own name.

    The units are in the names because the comparability of two runs rests on these values
    being equal, and a bare number whose unit lives in a docstring cannot be compared by a
    machine. The starting values are a HYPOTHESIS — they were chosen, not measured — and
    they are calibrated against the places the reader named unprompted.
    """

    #: Speaking pace for the spoken branch.
    spoken_words_per_minute: int = 130
    #: Silent reading pace for the document branch. Deliberately lower than the figures
    #: quoted for prose: this genre's documents are technical, and the reader stops.
    reading_words_per_minute: int = 180
    #: Beyond this many words since the previous occurrence, a term has gone stale.
    freshness_distance_words: int = 400
    #: This many DISTINCT other terms introduced in between cools it too, however close.
    freshness_intervening_terms: int = 3
    #: What one stale return costs the reader, expressed in words of reading — so the penalty
    #: lands in the same unit as the volume and the two can simply be added.
    stale_return_cost_words: int = 60

    def digest(self) -> str:
        """Identity of this configuration — two runs are comparable only if it matches."""
        payload = json.dumps(self.__dict__, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TermInventory:
    """What the audience knows and what the artefact introduces — declared, never inferred.

    An empty ``known`` set is a legitimate declaration (this audience knows none of it); the
    absence of the object is not. That is the difference the constructor of this class
    exists to make: passing it is an act, and silence cannot imitate it.
    """

    #: Terms the audience already holds — from the contract, declared by the operator.
    known: frozenset[str]
    #: Terms the artefact introduces, declared alongside the artefact.
    introduced: frozenset[str]

    def tracked(self) -> tuple[str, ...]:
        """The inventory the layer follows: introduced MINUS known, longest term first.

        Longest first because occurrences are matched by scanning: a short term contained
        inside a longer one would otherwise swallow it and both counts would be wrong.
        """
        remaining = {term.strip() for term in self.introduced if term.strip()}
        known = {term.strip().casefold() for term in self.known}
        return tuple(
            sorted(
                (term for term in remaining if term.casefold() not in known),
                key=lambda term: (-len(term), term.casefold()),
            )
        )


@dataclass(frozen=True)
class Occurrence:
    """One use of one term: where it is, and how stale it had gone for the reader."""

    section_id: str
    #: Word offset from the start of the part — the unit the thresholds are declared in.
    at_word: int
    #: Words since the previous occurrence; ``None`` for the introduction itself.
    since_previous_words: int | None
    #: DISTINCT other tracked terms introduced since the previous occurrence.
    intervening_terms: int
    stale: bool
    why_stale: str | None


@dataclass(frozen=True)
class TermTrace:
    term: str
    occurrences: tuple[Occurrence, ...]

    @property
    def stale_returns(self) -> int:
        return sum(1 for occurrence in self.occurrences if occurrence.stale)

    @property
    def introduced_never_used(self) -> bool:
        """Introduced once and never touched again — a term the reader paid for and never spent."""
        return len(self.occurrences) == 1


@dataclass(frozen=True)
class PartReport:
    """One part of the artefact, measured by the branch its kind selects."""

    part: str
    kind: PartKind
    words: int
    #: The floor: volume alone, at the branch's pace.
    base_minutes: float
    #: The floor plus the price of the stale returns. Equal to ``base_minutes`` for spoken
    #: text, where returns are not counted at all.
    estimated_minutes: float
    #: Declared reader budget in minutes, or ``None`` when none was declared.
    budget_minutes: float | None
    traces: tuple[TermTrace, ...] = ()
    candidates: tuple[str, ...] = ()

    @property
    def stale_returns(self) -> int:
        return sum(trace.stale_returns for trace in self.traces)

    @property
    def over_budget_minutes(self) -> float | None:
        if self.budget_minutes is None:
            return None
        return round(self.estimated_minutes - self.budget_minutes, 2)


@dataclass(frozen=True)
class MechanicalReport:
    """The whole layer's output. Informational by construction — it decides nothing."""

    thresholds_digest: str
    parts: tuple[PartReport, ...]
    #: Names of the declared parts the layer measured. Stated because a part with no sections
    #: is refused upstream, and a part measured as empty would otherwise read as "fine".
    measured_parts: tuple[str, ...] = field(default_factory=tuple)
    #: Candidates about the artefact as a whole rather than any one part. A term declared as
    #: introduced and absent from EVERY part belongs here and nowhere else: in a composite
    #: artefact a term is legitimately absent from most parts, so per-part absence says
    #: nothing, and only absence everywhere means the declaration and the text have parted.
    artifact_candidates: tuple[str, ...] = field(default_factory=tuple)

    @property
    def candidates(self) -> tuple[str, ...]:
        return tuple(
            [line for part in self.parts for line in part.candidates]
            + list(self.artifact_candidates)
        )

    def as_message(self) -> dict:
        """The `mech_report` channel payload.

        ``blocks_convergence`` is stated rather than left to be inferred: the layer's whole
        standing rests on being unable to block, and a consumer reading this message should
        not have to know the spec to be sure of that.
        """
        return {
            "phase": "mech_report",
            "blocks_convergence": False,
            "thresholds_digest": self.thresholds_digest,
            "parts": [
                {
                    "part": part.part,
                    "kind": part.kind.value,
                    "words": part.words,
                    "base_minutes": round(part.base_minutes, 2),
                    "estimated_minutes": round(part.estimated_minutes, 2),
                    "budget_minutes": part.budget_minutes,
                    "over_budget_minutes": part.over_budget_minutes,
                    "stale_returns": part.stale_returns,
                    "introduced_never_used": [
                        trace.term for trace in part.traces if trace.introduced_never_used
                    ],
                    "candidates": list(part.candidates),
                }
                for part in self.parts
            ],
        }


def incomparability_reasons(previous: str | None, current: str | None) -> list[str]:
    """Why two runs' mechanical numbers may NOT be compared (empty list = they may).

    The thresholds are a hypothesis, so two reports computed under different ones are two
    different measurements wearing the same units — put side by side they would show a trend
    that never happened. A MISSING digest is treated as "not comparable" rather than as
    agreement: the run may predate the layer, and silence must not read as sameness.
    """
    reasons = []
    if previous is None:
        reasons.append("у прошлого прогона не записаны пороги механического слоя")
    if current is None:
        reasons.append("у текущего прогона не записаны пороги механического слоя")
    if previous is not None and current is not None and previous != current:
        reasons.append(
            f"пороги механического слоя разные ({previous} против {current}) — числа двух "
            "прогонов посчитаны по разным гипотезам и рядом не ставятся"
        )
    return reasons


# --- the retelling diff: what the structured report made measurable -----------------------
#
# A NEW deterministic signal of this layer, bought by the schema for free: same-named
# sections of two CREDITED reports (the current version's and the previous version's) are
# comparable field by field, and their word diff is a measurement of how the reader's
# understanding moved after the edits. INFORMATIONAL like everything here — it marks, it
# blocks nothing at any value, the judge is the operator.

#: The sections the diff runs over — the synthesis fields, where an understanding shift
#: shows; the findings lists have their own lifecycle (dedup, dispositions) and are not
#: prose to diff.
DIFFED_SECTIONS: tuple[str, ...] = (RETELLING, UPWARD, READINESS)


def _word_diff(old: str, new: str) -> dict:
    """One section pair as a word-level diff — collapsed whitespace, deterministic."""
    import difflib

    a, b = old.split(), new.split()
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    fragments: list[str] = []
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("delete", "replace"):
            removed += i2 - i1
            fragments.append("− " + " ".join(a[i1:i2]))
        if tag in ("insert", "replace"):
            added += j2 - j1
            fragments.append("+ " + " ".join(b[j1:j2]))
    return {
        "слов_было": len(a),
        "слов_стало": len(b),
        "убрано_слов": removed,
        "добавлено_слов": added,
        "фрагменты": fragments,
    }


def retelling_diff(previous_report, current_report) -> dict:
    """The synthesis sections of two credited reports, diffed word by word.

    Both arguments are VALIDATED report objects. A section present in only one of the two
    is named as such rather than silently skipped: across versions the derived section set
    can legitimately move (an operator flag flipped), and "the section appeared/vanished"
    is itself the measurement.
    """
    out: dict[str, object] = {}
    for name in DIFFED_SECTIONS:
        in_previous, in_current = name in previous_report, name in current_report
        if not in_previous and not in_current:
            continue
        if in_previous != in_current:
            out[name] = (
                "секция есть только в " + ("прошлой" if in_previous else "текущей") + " версии"
            )
            continue
        out[name] = _word_diff(str(previous_report[name]), str(current_report[name]))
    return out


_WORD = re.compile(r"[\w-]+", re.UNICODE)


def _word_tokens(text: str) -> list[str]:
    return _WORD.findall(text)


def count_words(text: str) -> int:
    return len(_word_tokens(text))


def _term_pattern(term: str) -> re.Pattern[str]:
    """A term matches on word boundaries, case-insensitively, across collapsed whitespace.

    Whitespace inside a declared term is collapsed to "any run of whitespace" so that a
    multi-word term still matches when the artefact wraps the line between its words — a
    difference invisible to the reader must not change the count.
    """
    parts = [re.escape(piece) for piece in term.split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE | re.UNICODE)


def _scan_part(
    sections, terms: Sequence[str]
) -> tuple[list[tuple[int, str, str]], int]:
    """Locate every occurrence of every tracked term, in reading order, by word offset."""
    hits: list[tuple[int, str, str]] = []  # (word offset, term, section id)
    offset = 0
    for section in sections:
        text = f"{section.title}\n{section.body}"
        # Word offsets are taken over the text the reader actually reads, heading included:
        # the heading is part of the section by construction, and skipping it would shift
        # every distance in the part by the length of its headings.
        for term in terms:
            for match in _term_pattern(term).finditer(text):
                hits.append((offset + count_words(text[: match.start()]), term, section.id))
        offset += count_words(text)
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    return hits, offset


def _trace_terms(
    hits: Sequence[tuple[int, str, str]], thresholds: MechanicalThresholds
) -> tuple[TermTrace, ...]:
    first_seen: dict[str, int] = {}
    for at_word, term, _ in hits:
        first_seen.setdefault(term, at_word)

    by_term: dict[str, list[Occurrence]] = {}
    previous: dict[str, int] = {}
    for at_word, term, section_id in hits:
        prior = previous.get(term)
        if prior is None:
            occurrence = Occurrence(section_id, at_word, None, 0, False, None)
        else:
            distance = at_word - prior
            # DISTINCT other terms whose FIRST use falls in the gap: a term reintroduced
            # again and again in between is one intrusion on the reader's memory, not five.
            intervening = sum(
                1
                for other, first in first_seen.items()
                if other != term and prior < first < at_word
            )
            reasons = []
            if distance > thresholds.freshness_distance_words:
                reasons.append(
                    f"{distance} слов с прошлой встречи при пороге "
                    f"{thresholds.freshness_distance_words}"
                )
            if intervening >= thresholds.freshness_intervening_terms:
                reasons.append(
                    f"{intervening} новых терминов вклинилось при пороге "
                    f"{thresholds.freshness_intervening_terms}"
                )
            occurrence = Occurrence(
                section_id, at_word, distance, intervening, bool(reasons),
                "; ".join(reasons) or None,
            )
        by_term.setdefault(term, []).append(occurrence)
        previous[term] = at_word
    return tuple(
        TermTrace(term, tuple(occurrences)) for term, occurrences in sorted(by_term.items())
    )


def measure(
    artifact: ReaderArtifact,
    *,
    inventory: TermInventory | None,
    budgets: Mapping[str, float] | None,
    thresholds: MechanicalThresholds = MechanicalThresholds(),
) -> MechanicalReport:
    """Measure every declared part by the branch its kind selects.

    ``inventory`` is REQUIRED and has no default for the reason in the module note: an
    inventory nobody declared would produce a clean report over nothing. ``budgets`` is
    optional and its absence is honest — the estimate is still computed, it is simply
    compared with nothing, and the report says so instead of implying the artefact fits.
    """
    if inventory is None:
        raise MechanicalRefused(
            [
                "инвентарь терминов не объявлен — слой не запускается: отчёт, посчитанный "
                "по необъявленному инвентарю, неотличим от чистого, и слой стал бы "
                "формальностью, которая всегда проходит"
            ]
        )

    tracked = inventory.tracked()
    budgets = dict(budgets or {})
    unknown = sorted(set(budgets) - {part.name for part in artifact.parts})
    if unknown:
        raise MechanicalRefused(
            [
                f"бюджет объявлен для частей, которых в артефакте нет: {', '.join(unknown)} "
                "— скорее всего часть переименована, и бюджет молча перестал применяться"
            ]
        )

    reports: list[PartReport] = []
    for part in artifact.parts:
        sections = artifact.sections_of(part.name)
        budget = budgets.get(part.name)
        if part.kind is PartKind.SPOKEN:
            reports.append(_spoken(part, sections, budget, thresholds))
        else:
            reports.append(_document(part, sections, tracked, budget, thresholds))

    seen = {trace.term for report in reports for trace in report.traces}
    missing = [
        f"«{term}» объявлен как вводимый, но не встречается ни в одной части — "
        "объявление и текст разошлись"
        for term in tracked
        if term not in seen
    ]
    return MechanicalReport(
        thresholds_digest=thresholds.digest(),
        parts=tuple(reports),
        measured_parts=tuple(part.name for part in artifact.parts),
        artifact_candidates=tuple(sorted(missing)),
    )


def _budget_candidates(part_name: str, estimated: float, budget: float | None) -> list[str]:
    if budget is None:
        return [
            f"часть «{part_name}»: бюджет читателя не объявлен — оценка {estimated:.1f} мин "
            "ни с чем не сравнивается"
        ]
    if estimated > budget:
        return [
            f"часть «{part_name}»: оценка {estimated:.1f} мин против объявленных "
            f"{budget:.1f} — превышение на {estimated - budget:.1f} мин"
        ]
    return []


def _spoken(part, sections, budget, thresholds) -> PartReport:
    """Spoken text: the pace is the speaker's, so volume against the slot is the whole story."""
    words = sum(count_words(f"{s.title}\n{s.body}") for s in sections)
    minutes = words / thresholds.spoken_words_per_minute
    return PartReport(
        part=part.name,
        kind=part.kind,
        words=words,
        base_minutes=minutes,
        estimated_minutes=minutes,
        budget_minutes=budget,
        candidates=tuple(_budget_candidates(part.name, minutes, budget)),
    )


def _document(part, sections, tracked, budget, thresholds) -> PartReport:
    """A document: the pace is the READER's, so volume is the floor and returns raise it."""
    hits, words = _scan_part(sections, tracked)
    traces = _trace_terms(hits, thresholds)
    base = words / thresholds.reading_words_per_minute
    stale = sum(trace.stale_returns for trace in traces)
    estimated = (words + stale * thresholds.stale_return_cost_words) / (
        thresholds.reading_words_per_minute
    )

    candidates: list[str] = []
    # Volume first and separately: it decides on its own. A document nobody will start is
    # not a document with a term problem, and blending the two would hide which is which.
    candidates += _budget_candidates(part.name, estimated, budget)
    if budget is not None and base > budget:
        candidates.append(
            f"часть «{part.name}»: один объём ({words} слов ≈ {base:.1f} мин) уже "
            f"перебирает бюджет {budget:.1f} мин — дело не в возвратах"
        )
    for trace in traces:
        for occurrence in trace.occurrences:
            if occurrence.stale:
                candidates.append(
                    f"{occurrence.section_id}: «{trace.term}» — читатель успел забыть "
                    f"({occurrence.why_stale})"
                )
        if trace.introduced_never_used:
            candidates.append(
                f"«{trace.term}» введён и ни разу не употреблён — читатель заплатил за "
                "термин и не потратил его"
            )
    return PartReport(
        part=part.name,
        kind=part.kind,
        words=words,
        base_minutes=base,
        estimated_minutes=estimated,
        budget_minutes=budget,
        traces=traces,
        candidates=tuple(candidates),
    )
