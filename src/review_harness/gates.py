# SPDX-License-Identifier: Apache-2.0
"""The operator's gates: entry, exit, and the exchanges in between.

The gates section of the spec. Three load-bearing rules:

* the harness cannot refuse the operator — the gate API has no refusal branch;
* "are you sure?" is asked no more than once, and the answer is carried out (the
  precedent of a principle-level defect: finalisation refused in B.15 because of
  unclosed machine states);
* every question and its verbatim answer are recorded in the run catalogue
  immediately (an exchange in between is a gate too; its answers are part of the
  state a new session is obliged to restore).

The v1 channel is the terminal the harness was started in (a working decision,
named to the operator in the plan of 2026-09-01). Input and output are injected so
that the gates are testable without a terminal.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .runcat import RunCatalog


class Gate:
    """One channel of communication with the operator, for one run."""

    def __init__(
        self,
        catalog: RunCatalog,
        *,
        ask_fn: Callable[[str], str] = input,
        say_fn: Callable[[str], None] = print,
    ) -> None:
        self._catalog = catalog
        self._ask = ask_fn
        self._say = say_fn

    def ask(self, question: str, *, kind: str, record: bool = True) -> str:
        """Ask the operator a question; the verbatim answer goes into the gate journal.

        Returns the answer as it stands: interpreting it is development's business,
        not the harness's (the harness does not speak for the operator).

        ``record=False`` is for callers whose verbatim answer lands in their OWN
        durable state (run.json for the entry gate, the JSON of a batch of answers)
        in ONE write: two carriers of truth for one answer left a crash window in
        which an answer already written to the journal was asked again (a finding of
        round 8). The gates.md journal is a derivative, not a source.
        """
        self._say(f"\n[gate: {kind}]")
        # The question goes into the input prompt itself: ask_fn receives the full text
        # of the question rather than a bare invitation — otherwise gates substituted in
        # tests (and in future channels) would be answering blind.
        answer = self._ask(f"{question}\n> ")
        if record:
            self._catalog.append_gate_record(question, answer, kind=kind)
        return answer

    def ask_yes_no_verbatim(self, question: str, *, kind: str) -> tuple[bool, str, str]:
        """Like ask_yes_no, but the final answer is NOT journalled — the caller
        journals it AFTER its own durable write of state.

        Returns (verdict, the verbatim answer, the actual text of the question the
        answer was given to). The order is load-bearing (round 13): journal before
        state left a crash window in which the operator's recorded word was asked
        for again. Unrecognised exchanges in between (which carry no state) are
        journalled here immediately.
        """
        prompt = question
        answer = self.ask(prompt, kind=kind, record=False)
        while True:
            verdict = _read_yes_no(answer)
            if verdict is not None:
                return verdict, answer, prompt
            # An unrecognised answer IN BETWEEN is journalled against THE text the
            # operator actually answered (after the first clarification that is the text
            # of the clarification, not the original question) — otherwise the pair of
            # question and answer cannot be reconstructed (a finding of round 10).
            self._catalog.append_gate_record(prompt, answer, kind=kind)
            prompt = _reask_text(answer)
            answer = self.ask(prompt, kind="a clarification of the record", record=False)

    def ask_yes_no(self, question: str, *, kind: str) -> bool:
        """A yes/no question with one semantics for the operator's word.

        Expanded affirmatives, the idioms «не возражаю» and «не против», morphology —
        all read by one parser; what it does not recognise is honestly asked again.
        """
        answer = self.ask(question, kind=kind)
        while True:
            verdict = _read_yes_no(answer)
            if verdict is not None:
                return verdict
            answer = self.ask(_reask_text(answer), kind="a clarification of the record")

    def confirm_unconditional(self, action: str) -> bool:
        """The operator's word, unconditional: one "are you sure?" and then execution.

        Returns False only when the operator changed his own mind. There is no "the
        harness refused" branch here and there cannot be — in particular, an expanded
        affirmative («Финализацию принимаю», «да, финализируй») must not turn into a
        silent cancellation because of a poor vocabulary. An unrecognised answer is
        not decided for the operator in either direction: the harness says it did not
        understand and asks again — that is a clarification of the record of his word,
        not a second "are you sure?".
        """
        return self.ask_yes_no(
            f"{action} — are you sure? (yes/no; a \"no\" simply cancels the action)",
            kind="a confirmation",
        )


_NEGATIVE_FIRST = {"нет", "no", "n", "не", "отмена", "отменяй", "стоп", "stop",
                   "not", "never", "nope", "don't", "dont"}
_AFFIRMATIVE_FIRST = {"да", "yes", "y", "д", "ага", "давай", "конечно", "точно"}
_AFFIRMATIVE_STEMS = ("принима", "принял", "подтвержда", "соглас", "финализир", "финализац")
# Words that form an idiom of AGREEMENT with the particle «не», not a refusal.
_CONSENT_AFTER_NE = {"возражаю", "против", "возражаем"}

# WHITELIST of empty words: intensifiers, particles, politeness. Consent is accepted
# only when the answer consists of consent words and these. Any other tail
# («да, только после исправлений», "yes, but only after I review") is a reservation
# the parser does NOT read: it asks again. Conditions cannot be enumerated, so the
# boundary is drawn by what is accepted, not by what is rejected (cold review of
# the snapshot, 2026-09-07).
_EMPTY_WORDS = {"и", "же", "ну", "с", "со", "пожалуйста", "please", "now", "сейчас",
                "немедленно", "непременно", "точно", "конечно", "давай", "ага", "всё",
                "все", "all", "go", "ahead"}


def _reask_text(answer: str) -> str:
    """One re-ask text for both askers, so that they cannot drift apart."""
    return (
        f"I did not recognise the answer {answer!r} as a plain yes or no; the action "
        "was neither performed nor cancelled. Answer with the word alone, yes or no, "
        "or say what has to change first."
    )


def _read_yes_no(answer: str) -> bool | None:
    """Read the operator's word; None is an honest "I did not understand", not a decision.

    Negation dominates: «точно нет», «давай не будем», «принимаю, но не сейчас» must
    all read as a refusal — the price of a false "yes" (an action carried out) is
    higher than the price of asking again.
    """
    # A question is not a decision: a question mark anywhere sends the word back
    # for clarification BEFORE any other check («Согласен?» is not consent; cold
    # review, 2026-09-07).
    if "?" in answer:
        return None
    # Punctuation that carries no meaning is cut: «да!» is a yes, «точно нет!» a no.
    words = re.sub(r"[,.!;:]", " ", answer.strip().lower()).split()
    if not words:
        return None
    # Idioms of agreement carrying the particle «не»: «не возражаю», «не против» are
    # Russian agreement, not refusal (round 5). The pair collapses into an
    # affirmative token BEFORE the scan for negations.
    collapsed: list[str] = []
    i = 0
    while i < len(words):
        if words[i] == "не" and i + 1 < len(words) and words[i + 1] in _CONSENT_AFTER_NE:
            collapsed.append("да")
            i += 2
        else:
            collapsed.append(words[i])
            i += 1
    words = collapsed
    # Morphological negation dominates too, but ONLY over affirmative stems:
    # «несогласен» is «не» + «соглас» and means refusal, while «немедленно» and
    # «непременно» are ordinary adverbs rather than negations (round 4: a broad
    # «не» prefix was eating «да, немедленно финализируй»).
    def _neg(w: str) -> bool:
        return w in _NEGATIVE_FIRST or (
            w.startswith("не") and any(stem in w for stem in _AFFIRMATIVE_STEMS)
        )

    if any(_neg(w) for w in words):
        return False

    def _consent(w: str) -> bool:
        return w in _AFFIRMATIVE_FIRST or (
            any(stem in w for stem in _AFFIRMATIVE_STEMS) and not w.startswith("не")
        )

    if not any(_consent(w) for w in words):
        return None
    if all(_consent(w) or w in _EMPTY_WORDS for w in words):
        return True
    # Consent with a tail is a reservation. What the tail says the parser does not
    # know and does not guess: the word is asked again, the action neither done nor cancelled.
    return None
