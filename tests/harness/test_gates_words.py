# SPDX-License-Identifier: Apache-2.0
"""The operator's word at a gate: the boundary of what the parser accepts.

The rule (cold review of the published snapshot, 2026-09-07): a negation anywhere is a
refusal; consent is accepted ONLY when the answer consists of consent words and a closed
list of empty words (intensifiers, particles, politeness); consent with any other tail is
a reservation the parser does not read, and the word is asked again. Conditions cannot be
enumerated, so the boundary is drawn by what is accepted, not by what is rejected.
"""

import pytest

from review_harness.gates import _read_yes_no


@pytest.mark.parametrize(
    "answer",
    [
        "нет", "точно нет", "принимаю, но не сейчас", "несогласен", "да, но не в этом виде",
        "yes, but not now", "not yet", "never", "yes but no",
    ],
)
def test_negation_anywhere_is_a_refusal(answer: str) -> None:
    assert _read_yes_no(answer) is False


@pytest.mark.parametrize(
    "answer",
    [
        "yes", "да", "подтверждаю", "не возражаю", "Не против", "Да, не возражаю",
        "да, финализируй", "Финализацию принимаю", "Согласен с финализацией", "Финализировали",
        "Да, немедленно финализируй", "непременно согласен", "да, давай", "yes, go ahead",
    ],
)
def test_plain_consent_is_accepted(answer: str) -> None:
    assert _read_yes_no(answer) is True


@pytest.mark.parametrize(
    "answer",
    [
        "да, если исправишь замечания", "yes if you fix the remarks",
        "yes, but only after I review the changes", "да, только после исправлений",
        "принимаю с оговоркой", "да, а тесты потом", "Да, переноси",
    ],
)
def test_consent_with_a_tail_is_asked_again(answer: str) -> None:
    assert _read_yes_no(answer) is None


@pytest.mark.parametrize("answer", ["хм", "ok", ""])
def test_no_word_is_asked_again(answer: str) -> None:
    assert _read_yes_no(answer) is None


@pytest.mark.parametrize(
    "answer",
    ["Согласен?", "Финализируем?", "Финализация?", "да?", "нет?", "yes?", "да, а тесты?"],
)
def test_a_question_is_not_a_decision(answer: str) -> None:
    assert _read_yes_no(answer) is None


@pytest.mark.parametrize(
    "answer, verdict",
    [("да!", True), ("финализируй!", True), ("Да, подтверждаю!", True), ("точно нет!", False), ("нет.", False)],
)
def test_meaningless_punctuation_is_cut(answer: str, verdict: bool) -> None:
    assert _read_yes_no(answer) is verdict
