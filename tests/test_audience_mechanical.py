# SPDX-License-Identifier: Apache-2.0
"""Step 5 of the audience genre: the mechanical layer.

Three things are asserted here, and they fail differently:

- the layer NEVER blocks and never pretends to judge — it produces candidates, and the one
  thing it must not do is look like a verdict;
- the document branch keeps VOLUME and RETURNS as two separate signals: a document nobody
  will start is not a document with a term problem, and the operator has to see which it is;
- an undeclared inventory is a refusal, not an empty clean run — the failure mode that would
  make the whole layer a formality that always passes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from assistant_memory.audience.mechanical import (
    MechanicalRefused,
    MechanicalThresholds,
    TermInventory,
    count_words,
    incomparability_reasons,
    measure,
)
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord
from assistant_memory.audience.sections import PartKind, parse_reader_artifact

WORDS_20 = " ".join(f"слово{i}" for i in range(20))
T0 = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _blind_record(**over) -> RunRecord:
    base = dict(
        id="blind-1", kind=RunKind.BLIND, iteration=1, artifact_seq=1, profile_digest="P",
        launch_number=2, started_at=T0, finished_at=T0 + timedelta(minutes=1),
        transcript_path="runs/blind-1.log", transcript_sha256="h", tool_calls=(),
        model="gpt-x", model_version="2026-08", spend=1, outcome=RunOutcome.HAPPENED,
        runner_version="0.145.0", sandbox_mode="read-only", approval_mode="never",
        canary_record_id="canary-1", prompt_digest="pr", reader_digest="R",
        contract_version=1, contract_sha256="c1", category_set_version="cs1",
    )
    return RunRecord(**{**base, **over})


def _artifact(*sections: str, parts: str = "- документ для чтения: документ\n") -> str:
    return "# Артефакт\n\nЧасти:\n" + parts + "\n" + "\n\n".join(sections) + "\n"


def _section(number: int, part: str, title: str, body: str) -> str:
    return f"### S-{number} ({part}) — {title}\n{body}"


def _inventory(introduced=("леджер",), known=()) -> TermInventory:
    return TermInventory(known=frozenset(known), introduced=frozenset(introduced))


# --- the refusal that keeps the layer honest ---------------------------------------------


def test_an_undeclared_inventory_is_a_refusal_not_an_empty_clean_run():
    """The failure this refusal exists for: a report computed over an inventory nobody
    declared is indistinguishable from a clean one, and the layer becomes a formality."""
    artifact = parse_reader_artifact(
        _artifact(_section(1, "документ для чтения", "Раздел", "Текст."))
    )
    with pytest.raises(MechanicalRefused) as refusal:
        measure(artifact, inventory=None, budgets=None)
    assert any("инвентарь терминов не объявлен" in r for r in refusal.value.reasons)


def test_an_empty_known_set_is_a_declaration_and_runs():
    """An audience that knows none of it is a legitimate declaration — only the ABSENCE of
    the object is a refusal. The two must not be conflated, or declaring becomes pointless."""
    artifact = parse_reader_artifact(
        _artifact(_section(1, "документ для чтения", "Раздел", "Леджер тут."))
    )
    report = measure(artifact, inventory=_inventory(known=()), budgets=None)
    assert report.parts[0].words > 0


def test_a_budget_for_a_part_that_does_not_exist_is_a_refusal():
    """A renamed part would otherwise make the budget stop applying in silence — the exact
    shape of failure this genre keeps finding: nothing looks wrong."""
    artifact = parse_reader_artifact(
        _artifact(_section(1, "документ для чтения", "Раздел", "Текст."))
    )
    with pytest.raises(MechanicalRefused) as refusal:
        measure(artifact, inventory=_inventory(), budgets={"текст для чтения": 5})
    assert any("которых в артефакте нет" in r for r in refusal.value.reasons)


# --- the branches, chosen by the PART -----------------------------------------------------


def test_the_branch_follows_the_part_not_the_artifact():
    """A composite artefact carries both at once: the spoken part is timed at speaking pace,
    the document part at reading pace, and the document part alone carries term traces."""
    text = _artifact(
        _section(1, "текст для чтения", "Вслух", WORDS_20),
        _section(2, "документ для чтения", "Глазами", f"Леджер. {WORDS_20}"),
        parts="- текст для чтения: произносимый\n- документ для чтения: документ\n",
    )
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets=None)
    spoken = next(p for p in report.parts if p.kind is PartKind.SPOKEN)
    document = next(p for p in report.parts if p.kind is PartKind.DOCUMENT)
    assert spoken.traces == ()
    assert [t.term for t in document.traces] == ["леджер"]
    # Same branch would give the same minutes-per-word. It does not: 22 words at speaking
    # pace against 24 at reading pace, and the spoken part takes longer per word.
    assert spoken.base_minutes / spoken.words == pytest.approx(1 / 130, rel=1e-6)
    assert document.base_minutes / document.words == pytest.approx(1 / 180, rel=1e-6)


def test_spoken_text_is_timed_against_its_slot():
    body = " ".join(["слово"] * 260)
    text = _artifact(
        _section(1, "речь", "Вслух", body), parts="- речь: произносимый\n"
    )
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets={"речь": 1.0})
    part = report.parts[0]
    assert part.base_minutes == pytest.approx(2.0, abs=0.05)  # 260 words at 130 wpm
    assert part.over_budget_minutes == pytest.approx(1.0, abs=0.05)
    assert any("превышение" in c for c in part.candidates)


# --- the operator's correction of 2026-08-05: SIZE STILL DECIDES --------------------------


def test_a_long_document_without_a_single_return_is_still_flagged():
    """Operator ruling 2026-08-05, correcting the spec: a document IS timed. Nobody starts a
    twenty-five-page document however cleanly it is written, so volume is the floor of the
    estimate rather than a quantity the layer declines to compute."""
    body = " ".join(["слово"] * 9000)  # ~25 pages, no tracked term at all
    text = _artifact(_section(1, "документ для чтения", "Длинный", body))
    report = measure(
        parse_reader_artifact(text), inventory=_inventory(), budgets={"документ для чтения": 10}
    )
    part = report.parts[0]
    assert part.stale_returns == 0
    assert part.base_minutes == part.estimated_minutes  # nothing to add — and still over
    assert part.over_budget_minutes > 0
    assert any("дело не в возвратах" in c for c in part.candidates)


def test_volume_and_returns_stay_two_separate_signals():
    """The two candidates must be distinguishable: "too long" and "too tangled" have
    different fixes, and one blended number would hide which one is in front of you."""
    filler = " ".join(["слово"] * 600)
    text = _artifact(
        _section(1, "документ для чтения", "Введение", f"Леджер — это журнал. {filler}"),
        _section(2, "документ для чтения", "Позже", f"Снова леджер. {filler}"),
    )
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets=None)
    part = report.parts[0]
    assert part.stale_returns == 1
    assert part.estimated_minutes > part.base_minutes  # the return PUSHED the floor up
    assert any("успел забыть" in c for c in part.candidates)
    assert not any("дело не в возвратах" in c for c in part.candidates)


# --- freshness, not distance alone ---------------------------------------------------------


def test_a_use_right_after_the_introduction_is_not_a_return():
    text = _artifact(
        _section(1, "документ для чтения", "Раздел", "Леджер — это журнал. Леджер помнит всё.")
    )
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets=None)
    assert report.parts[0].stale_returns == 0


def test_intervening_terms_cool_a_term_that_distance_alone_would_not():
    """The forgetting model is freshness, not length: three new terms crowd the old one out
    even though the two uses are a few dozen words apart."""
    text = _artifact(
        _section(
            1,
            "документ для чтения",
            "Раздел",
            "Леджер это журнал. Канарейка это проба. Отпечаток это хэш. "
            "Диспозиция это ответ. И снова леджер.",
        )
    )
    inventory = _inventory(introduced=("леджер", "канарейка", "отпечаток", "диспозиция"))
    report = measure(parse_reader_artifact(text), inventory=inventory, budgets=None)
    ledger = next(t for t in report.parts[0].traces if t.term == "леджер")
    assert ledger.stale_returns == 1
    assert "новых терминов вклинилось" in ledger.occurrences[1].why_stale


def test_a_term_the_audience_already_knows_is_not_tracked():
    """The inventory is a function of the CONTRACT: what the audience holds is subtracted."""
    text = _artifact(
        _section(1, "документ для чтения", "Раздел", f"Леджер. {' '.join(['слово'] * 600)} Леджер.")
    )
    inventory = TermInventory(known=frozenset({"Леджер"}), introduced=frozenset({"леджер"}))
    report = measure(parse_reader_artifact(text), inventory=inventory, budgets=None)
    assert report.parts[0].traces == ()
    assert report.parts[0].stale_returns == 0


def test_a_term_introduced_and_never_used_again_is_a_candidate():
    text = _artifact(_section(1, "документ для чтения", "Раздел", "Леджер — это журнал."))
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets=None)
    assert any("ни разу не употреблён" in c for c in report.parts[0].candidates)


def test_a_declared_term_absent_from_the_whole_artifact_is_reported_once():
    """Per part this would be noise — in a composite artefact a term is legitimately absent
    from most parts. Only absence EVERYWHERE means the declaration and the text have parted."""
    text = _artifact(
        _section(1, "документ для чтения", "Раздел", "Текст без терминов."),
        _section(2, "текст для чтения", "Вслух", "И тут нет."),
        parts="- документ для чтения: документ\n- текст для чтения: произносимый\n",
    )
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets=None)
    absent = [c for c in report.candidates if "не встречается ни в одной части" in c]
    assert len(absent) == 1


def test_a_multi_word_term_matches_across_a_line_break():
    """A wrap is invisible to the reader, so it must not change the count."""
    text = _artifact(
        _section(
            1, "документ для чтения", "Раздел",
            "Слепой\nчитатель приходит. Слепой читатель уходит.",
        )
    )
    report = measure(
        parse_reader_artifact(text),
        inventory=_inventory(introduced=("слепой читатель",)),
        budgets=None,
    )
    assert len(report.parts[0].traces[0].occurrences) == 2


# --- what the layer is NOT allowed to be ---------------------------------------------------


def test_the_report_says_out_loud_that_it_blocks_nothing():
    """The layer's whole standing rests on being unable to block convergence at ANY threshold
    value. A consumer must not have to read the spec to be sure of that."""
    text = _artifact(_section(1, "документ для чтения", "Раздел", " ".join(["слово"] * 9000)))
    message = measure(
        parse_reader_artifact(text), inventory=_inventory(), budgets={"документ для чтения": 1}
    ).as_message()
    assert message["phase"] == "mech_report"
    assert message["blocks_convergence"] is False


def test_a_missing_budget_is_said_out_loud_rather_than_read_as_a_fit():
    text = _artifact(_section(1, "документ для чтения", "Раздел", " ".join(["слово"] * 9000)))
    report = measure(parse_reader_artifact(text), inventory=_inventory(), budgets=None)
    assert report.parts[0].budget_minutes is None
    assert any("ни с чем не сравнивается" in c for c in report.parts[0].candidates)


def test_two_runs_are_comparable_only_when_the_thresholds_match():
    """Comparability rests on the numbers being equal, so the digest has to move when any of
    them does — otherwise two reports computed under different hypotheses look like a trend."""
    default = MechanicalThresholds()
    moved = MechanicalThresholds(stale_return_cost_words=61)
    assert default.digest() != moved.digest()
    assert default.digest() == MechanicalThresholds().digest()


def test_a_missing_threshold_digest_means_not_comparable_never_the_same():
    """Silence must not read as sameness: a run predating the layer carries no digest, and
    treating that as agreement would put two different measurements side by side as a trend."""
    digest = MechanicalThresholds().digest()
    assert incomparability_reasons(digest, digest) == []
    assert incomparability_reasons(None, digest)
    assert incomparability_reasons(digest, None)
    moved = MechanicalThresholds(reading_words_per_minute=1).digest()
    assert any("разные" in r for r in incomparability_reasons(digest, moved))


def test_the_mechanical_layer_cannot_make_a_run_uncreditable():
    """It blocks nothing — so its threshold field must stay out of the required list, or its
    absence would gain exactly the power the layer is denied."""
    assert "mech_thresholds_digest" not in _blind_record().missing_fields()


def test_words_are_counted_the_same_way_everywhere():
    assert count_words("два-три слова, и ещё") == 4


# --- the retelling diff (AG-10) -------------------------------------------------------------


def test_retelling_diff_counts_words_and_names_fragments():
    from assistant_memory.audience.mechanical import retelling_diff

    previous = {"пересказ": "механизм читает текст двумя читателями"}
    current = {"пересказ": "механизм читает артефакт двумя внимательными читателями"}
    diff = retelling_diff(previous, current)["пересказ"]
    assert diff["слов_было"] == 5 and diff["слов_стало"] == 6
    assert diff["убрано_слов"] == 1 and diff["добавлено_слов"] == 2
    assert any(f.startswith("−") for f in diff["фрагменты"])
    assert any(f.startswith("+") for f in diff["фрагменты"])


def test_retelling_diff_names_a_section_that_exists_on_one_side_only():
    """Across versions the derived set can move (a flag flipped) — "the section appeared"
    is itself the measurement, never a silent skip."""
    from assistant_memory.audience.mechanical import retelling_diff

    diff = retelling_diff(
        {"пересказ": "то же"}, {"пересказ": "то же", "передать_наверх": "новая секция"}
    )
    assert diff["передать_наверх"] == "секция есть только в текущей версии"
    assert diff["пересказ"]["убрано_слов"] == 0 and diff["пересказ"]["добавлено_слов"] == 0
