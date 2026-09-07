# SPDX-License-Identifier: Apache-2.0
"""Step 3 of the audience genre: sections, the reader fingerprint, and genre mode.

Three things are being asserted here, and they fail in different ways:

- the artefact REFUSES what would leave the review with an empty denominator — the failure
  measured on the pilot deck, and the only one here that looks like success;
- the reader fingerprint moves on exactly what the reader sees and on nothing else, because
  it is what decides whether a previous reading may stand as this version's assessment;
- the pass plan reads the GENRE and never the mode, because those are two questions and one
  field answering both gives one of them a value nobody chose.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant_memory.audience.blind_prompt import RoleTemplate, prepare_blind_prompt
from assistant_memory.audience.claim_map import ClaimIdLedger
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractIdLedger,
    ContractItem,
    OperatorFlag,
    PrivateContract,
    PrivateField,
    PublicContract,
    PublicField,
)
from assistant_memory.audience.ledger import LedgerError, canonical_number
from assistant_memory.audience.planner import (
    BlindAction,
    blind_assessment_gate,
    plan_blind_pass,
    plan_iteration,
)
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord
from assistant_memory.audience.report_schema import SectionBudgets
from assistant_memory.audience.sections import (
    ArtifactError,
    PartKind,
    SectionIdLedger,
    load_reader_artifact,
    missing_section_rows,
    parse_reader_artifact,
    reader_fingerprint,
)
from assistant_memory.review import coverage, watcher
from assistant_memory.review.genres import AUDIENCE_GENRE, config_refusals

REPO = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO / "docs" / "prompts" / "audience" / "blind_reader.md"

COMPOSITE = """# Презентация про LTV

Служебная шапка, которой слепой читатель не видит.

Части:
- план по слайдам: документ
- текст для чтения: произносимый
- цифры для графиков: документ

### S-1 (план по слайдам) — О чём это
Один слайд, три тезиса.

### S-2 (текст для чтения) — Вступление
Здравствуйте. Сегодня про то, сколько приносит клиент.

### S-3 (цифры для графиков) — Данные к первому графику
2024: 118. 2025: 131.
"""

T0 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _public() -> PublicContract:
    return PublicContract(
        version=1,
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
            ContractItem("C-2", PublicField.PURPOSE, "Понять, стоит ли ввязываться."),
        ),
        flags={
            OperatorFlag.PURPOSE_INCLUDES_EVALUATION: False,
            OperatorFlag.LANGUAGE_NOT_NATIVE: False,
            OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING: False,
        },
    )


def _contract(takeaway: str = "Совсем другая мысль.") -> AudienceContract:
    return AudienceContract(
        public=_public(),
        private=PrivateContract(
            version=1, items=(ContractItem("C-9", PrivateField.TAKEAWAY, takeaway),)
        ),
    )


def _blind_record(**overrides) -> RunRecord:
    base = dict(
        id="b1",
        kind=RunKind.BLIND,
        iteration=1,
        artifact_seq=10,
        profile_digest="p",
        launch_number=2,
        started_at=T0,
        finished_at=T0 + timedelta(minutes=5),
        transcript_path="t.log",
        transcript_sha256="h",
        tool_calls=(),
        model="gpt-x",
        model_version="1",
        spend=100,
        outcome=RunOutcome.HAPPENED,
        canary_record_id="c1",
        prompt_digest="pd",
        reader_digest="rd",
        contract_version=1,
        contract_sha256="cs",
        category_set_version="cv",
        runner_version="0.145.0",
    )
    base.update(overrides)
    return RunRecord(**base)


# --- the artefact: what it refuses, and why ----------------------------------------------


def test_a_composite_artefact_parses_with_one_flat_numbering():
    artifact = parse_reader_artifact(COMPOSITE)
    assert artifact.section_ids() == ("S-1", "S-2", "S-3")
    assert [part.name for part in artifact.parts] == [
        "план по слайдам", "текст для чтения", "цифры для графиков"
    ]
    assert artifact.part("текст для чтения").kind is PartKind.SPOKEN
    assert artifact.part("цифры для графиков").kind is PartKind.DOCUMENT
    assert artifact.sections_of("текст для чтения")[0].id == "S-2"


def test_the_mechanical_branch_is_a_property_of_the_part_not_of_the_artefact():
    """The consequence that made the flat address workable: one artefact, both branches."""
    artifact = parse_reader_artifact(COMPOSITE)
    kinds = {artifact.part(section.part).kind for section in artifact.sections}
    assert kinds == {PartKind.SPOKEN, PartKind.DOCUMENT}


def test_a_section_at_the_wrong_heading_level_is_refused():
    """The measured failure: the coverage builder sees zero rows and the review looks fine."""
    text = COMPOSITE.replace("### S-2 (текст для чтения)", "## S-2 (текст для чтения)")
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("не третьего уровня" in reason for reason in refusal.value.reasons)


def test_a_third_level_heading_that_is_not_a_section_is_refused():
    text = COMPOSITE + "\n### Приложение\nЕщё текст.\n"
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("не является разделом" in reason for reason in refusal.value.reasons)


def test_a_section_naming_an_undeclared_part_is_refused():
    text = COMPOSITE.replace("(цифры для графиков)", "(раздатка)")
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("не объявлена в перечне частей" in reason for reason in refusal.value.reasons)


def test_a_part_kind_outside_the_closed_set_is_refused():
    text = COMPOSITE.replace("- цифры для графиков: документ", "- цифры для графиков: таблица")
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("вне закрытого перечня" in reason for reason in refusal.value.reasons)


def test_a_missing_parts_declaration_is_refused_even_for_one_part():
    text = "### S-1 (текст) — Заголовок\nТело.\n"
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("нет раздела «Части:»" in reason for reason in refusal.value.reasons)


def test_a_declared_part_with_no_sections_is_refused():
    text = COMPOSITE.replace(
        "### S-3 (цифры для графиков) — Данные к первому графику\n2024: 118. 2025: 131.\n", ""
    )
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("части без единого раздела" in reason for reason in refusal.value.reasons)


def test_a_repeated_identifier_is_refused():
    text = COMPOSITE.replace("### S-3 (цифры", "### S-1 (цифры")
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("выдан дважды" in reason for reason in refusal.value.reasons)


def test_the_reader_is_shown_the_sections_and_nothing_else_from_the_file():
    rendered = parse_reader_artifact(COMPOSITE).render()
    assert "Служебная шапка" not in rendered
    assert "Части:" not in rendered
    assert "# Презентация про LTV" not in rendered
    assert rendered.startswith("### S-1 (план по слайдам) — О чём это")


# --- the reader fingerprint --------------------------------------------------------------


def _fingerprint(text: str = COMPOSITE, **overrides) -> str:
    args = dict(instructions_digest="i", model="gpt-x", model_version="1")
    args.update(overrides)
    return reader_fingerprint(parse_reader_artifact(text), **args)


def test_the_fingerprint_moves_when_section_text_moves():
    changed = COMPOSITE.replace("2024: 118.", "2024: 121.")
    assert _fingerprint() != _fingerprint(changed)


def test_the_fingerprint_moves_when_a_heading_is_renamed_though_the_number_does_not():
    renamed = COMPOSITE.replace("— Вступление", "— Как начнём")
    assert parse_reader_artifact(renamed).section_ids() == ("S-1", "S-2", "S-3")
    assert _fingerprint() != _fingerprint(renamed)


def test_the_fingerprint_ignores_what_the_reader_never_sees():
    """A title and a service header are in the file and not in the reading."""
    edited = COMPOSITE.replace(
        "Служебная шапка, которой слепой читатель не видит.", "Совсем другая шапка."
    ).replace("# Презентация про LTV", "# Другое имя файла")
    assert _fingerprint() == _fingerprint(edited)


def test_the_fingerprint_moves_with_the_model_because_the_reader_is_an_instrument():
    assert _fingerprint() != _fingerprint(model="gpt-y")
    assert _fingerprint() != _fingerprint(model_version="2")


def test_the_fingerprint_moves_with_the_instructions_but_not_with_the_private_goal():
    """The instructions digest is the prompt WITHOUT the artefact — public only."""
    template = RoleTemplate.from_file(TEMPLATE_PATH)
    first = prepare_blind_prompt(
        template, _contract("Цель А."), parse_reader_artifact(COMPOSITE), SectionBudgets()
    )
    second = prepare_blind_prompt(
        template, _contract("Цель Б."), parse_reader_artifact(COMPOSITE), SectionBudgets()
    )
    assert first.instructions_digest == second.instructions_digest
    assert first.instructions_digest != first.digest  # the artefact is not counted twice
    assert _fingerprint(instructions_digest=first.instructions_digest) == _fingerprint(
        instructions_digest=second.instructions_digest
    )
    assert _fingerprint(instructions_digest="иные инструкции") != _fingerprint(
        instructions_digest=first.instructions_digest
    )


def test_the_denominator_names_the_sections_a_report_skipped():
    artifact = parse_reader_artifact(COMPOSITE)
    assert missing_section_rows(artifact.section_ids(), ["S-1", "S-3"]) == ["S-2"]
    assert missing_section_rows(artifact.section_ids(), ["S-1", "S-2", "S-3"]) == []


# --- the section ledger ------------------------------------------------------------------


def test_a_retired_section_number_is_never_handed_to_a_new_section(tmp_path):
    ledger = SectionIdLedger.load(tmp_path / "sections.json")
    ledger.register(parse_reader_artifact(COMPOSITE), 10)
    assert ledger.next_free() == "S-4"

    shrunk = COMPOSITE.replace(
        "### S-3 (цифры для графиков) — Данные к первому графику\n2024: 118. 2025: 131.\n", ""
    ).replace("- цифры для графиков: документ\n", "")
    ledger.register(parse_reader_artifact(shrunk), 11)
    assert ledger.entries[3].retired_in == 11

    with pytest.raises(ArtifactError) as refusal:
        ledger.register(parse_reader_artifact(COMPOSITE), 12)
    assert any("снят в версии 11" in reason for reason in refusal.value.reasons)


def test_a_section_moving_between_parts_keeps_its_number(tmp_path):
    ledger = SectionIdLedger.load(tmp_path / "sections.json")
    ledger.register(parse_reader_artifact(COMPOSITE), 10)
    # the emptied part goes with it: a declared part with no sections is itself a refusal
    moved = COMPOSITE.replace(
        "### S-3 (цифры для графиков)", "### S-3 (план по слайдам)"
    ).replace("- цифры для графиков: документ\n", "")
    ledger.register(parse_reader_artifact(moved), 11)
    assert ledger.entries[3].retired_in is None
    assert ledger.entries[3].introduced_in == 10
    assert ledger.entries[3].label == "план по слайдам"


# --- the ledger FILE: the second entrance to the same table ------------------------------

_LEDGERS = [(ContractIdLedger, "C"), (SectionIdLedger, "S"), (ClaimIdLedger, "M")]


def _write_ledger(path: Path, prefix: str, entries: dict) -> Path:
    path.write_text(
        json.dumps(
            {"prefix": prefix, "last_version": 3, "entries": entries}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(("ledger_cls", "prefix"), _LEDGERS)
def test_two_spellings_of_one_number_in_the_ledger_file_are_refused(
    tmp_path, ledger_cls, prefix
):
    """Critic finding `persisted-id-aliases`. The canonical spelling was closed where an
    identifier is PARSED and left open where the ledger comes back from DISK: keys "1" and
    "01" both went through ``int()``, landed on one entry, and the later one overwrote the
    earlier — the file returned having forgotten that the number was ever issued and retired,
    which is the single thing it exists to remember. All three spaces share the loader, so
    all three are checked here."""
    path = _write_ledger(
        tmp_path / "ids.json",
        prefix,
        {
            "1": {"introduced_in": 1, "retired_in": 3, "label": "снятый"},
            "01": {"introduced_in": 3, "retired_in": None, "label": "тот же номер иначе"},
        },
    )
    with pytest.raises(LedgerError) as refusal:
        ledger_cls.load(path)
    assert any("неканонические номера" in reason for reason in refusal.value.reasons)


@pytest.mark.parametrize(
    "key", ["01", " 1", "1 ", "1\n", "+1", "1_0", "0", "-1", "1.0", "один"]
)
def test_a_ledger_key_has_exactly_one_spelling(tmp_path, key):
    """``int()`` takes the first five of these silently — "1_0" as TEN — and dies obscurely on
    the rest. Neither is what should happen to a permanent number arriving from disk, so the
    spelling is checked before the conversion and the whole file is refused: a ledger read as
    "almost right" hands out a number it has already handed out."""
    path = _write_ledger(
        tmp_path / "ids.json", "S", {key: {"introduced_in": 1, "retired_in": None, "label": "x"}}
    )
    with pytest.raises(LedgerError) as refusal:
        SectionIdLedger.load(path)
    assert any("неканонические номера" in reason for reason in refusal.value.reasons)


def test_a_trailing_newline_does_not_make_a_second_spelling():
    """Found while fixing the loader: the identifier pattern ended in `$`, which also matches
    BEFORE a trailing newline — so "S-1\\n" passed as canonical and came out of int() as 1.
    The same two-spellings-one-number hole, one character wide, on the parsing side."""
    assert canonical_number("S", "S-1") == 1
    with pytest.raises(LedgerError):
        canonical_number("S", "S-1\n")


@pytest.mark.parametrize(("ledger_cls", "prefix"), _LEDGERS)
def test_a_canonically_written_ledger_still_loads(tmp_path, ledger_cls, prefix):
    """The refusal above must not have closed the door on the ordinary file."""
    path = _write_ledger(
        tmp_path / "ids.json",
        prefix,
        {
            "1": {"introduced_in": 1, "retired_in": None, "label": "первый"},
            "10": {"introduced_in": 3, "retired_in": 3, "label": "снятый"},
        },
    )
    ledger = ledger_cls.load(path)
    assert ledger.entries[1].label == "первый"
    assert ledger.entries[10].retired_in == 3
    assert ledger.next_free() == f"{prefix}-11"


# --- genre mode: planning the two passes -------------------------------------------------


def test_with_no_previous_reading_the_blind_pass_runs():
    decision = plan_blind_pass(current_reader_digest="rd", iteration=1, artifact_seq=10)
    assert decision.action == BlindAction.RUN


def test_a_moved_fingerprint_forces_a_fresh_blind_run():
    decision = plan_blind_pass(
        current_reader_digest="СДВИНУЛСЯ",
        iteration=2,
        artifact_seq=11,
        previous_records=[_blind_record()],
        credited_ids={"b1"},
    )
    assert decision.action == BlindAction.RUN
    assert any("отпечаток изменился" in reason for reason in decision.reasons)


def test_an_unmoved_fingerprint_carries_the_previous_result_by_reference():
    decision = plan_blind_pass(
        current_reader_digest="rd",
        iteration=2,
        artifact_seq=11,
        previous_records=[_blind_record()],
        credited_ids={"b1"},
        live_runner_version="0.145.0",
    )
    assert decision.action == BlindAction.CARRY
    assert decision.carry_from == "b1" and decision.carry_iteration == 1


def test_a_carry_that_would_go_backwards_is_blocked_not_silently_allowed():
    decision = plan_blind_pass(
        current_reader_digest="rd",
        iteration=1,
        artifact_seq=10,
        previous_records=[_blind_record()],
        credited_ids={"b1"},
        live_runner_version="0.145.0",
    )
    assert decision.action == BlindAction.BLOCKED



def test_a_run_that_was_never_credited_is_never_carried():
    """Critic finding `uncredited-run-carried-forward`: being credited is a property of the
    linked PAIR plus channel facts, and the planner has neither. An earlier version filtered
    on kind/outcome/launch-number and called that "credited", so a reading with a dirty
    canary or a vanished transcript could open the next version's convergence gate."""
    decision = plan_blind_pass(
        current_reader_digest="rd",
        iteration=2,
        artifact_seq=11,
        previous_records=[_blind_record()],
        credited_ids=set(),  # nothing was blessed
    )
    assert decision.action == BlindAction.RUN
    assert decision.carry_from is None


def test_only_the_blessed_record_may_be_carried():
    older = _blind_record(id="b0", launch_number=1)
    newer = _blind_record(id="b1", launch_number=2)
    decision = plan_blind_pass(
        current_reader_digest="rd",
        iteration=2,
        artifact_seq=11,
        previous_records=[older, newer],
        credited_ids={"b0"},  # the NEWER one was not credited
        live_runner_version="0.145.0",
    )
    assert decision.action == BlindAction.CARRY
    assert decision.carry_from == "b0"


def test_the_informed_pass_reads_every_round():
    """The graph, the repository and the claim map all move while the reader's text stands still."""
    plan = plan_iteration(
        iteration=2,
        artifact_seq=11,
        current_reader_digest="rd",
        previous_records=[_blind_record()],
        credited_ids={"b1"},
        live_runner_version="0.145.0",
    )
    assert plan.informed is True
    assert plan.blind.action == BlindAction.CARRY


def test_an_annulled_previous_run_is_not_carried():
    annulled = _blind_record(
        outcome=RunOutcome.ANNULLED, outcome_reason="грязная канарейка"
    )
    decision = plan_blind_pass(
        current_reader_digest="rd", iteration=2, artifact_seq=11, previous_records=[annulled]
    )
    assert decision.action == BlindAction.RUN


def test_a_diverged_or_unprobed_runner_version_routes_to_a_fresh_run_not_a_block():
    """AG-30 (operator decision, review b5fd70de): the environment saying "re-measure" is
    a fresh run, never a parked round — and an unprobed environment is not assumed still."""
    diverged = plan_blind_pass(
        current_reader_digest="rd",
        iteration=2,
        artifact_seq=11,
        previous_records=[_blind_record()],
        credited_ids={"b1"},
        live_runner_version="0.146.0",
    )
    assert diverged.action == BlindAction.RUN
    assert any("версия запускающего изменилась" in r for r in diverged.reasons)

    unprobed = plan_blind_pass(
        current_reader_digest="rd",
        iteration=2,
        artifact_seq=11,
        previous_records=[_blind_record()],
        credited_ids={"b1"},
    )
    assert unprobed.action == BlindAction.RUN
    assert any("не установлена" in r for r in unprobed.reasons)


# --- genre mode: the gate before convergence ---------------------------------------------


def test_without_a_run_and_without_a_carry_there_is_no_blind_assessment():
    verdict = blind_assessment_gate(
        artifact_seq=11, iteration=2, current_reader_digest="rd", blind=None, canary=None
    )
    assert not verdict.credited


def test_a_carry_that_does_not_name_its_round_is_refused():
    verdict = blind_assessment_gate(
        artifact_seq=11,
        iteration=2,
        current_reader_digest="rd",
        blind=None,
        canary=None,
        carried_from=_blind_record(),
        credited_ids={"b1"},
    )
    assert not verdict.credited
    assert any("не называет исходный круг" in reason for reason in verdict.reasons)


def test_a_legitimate_carry_passes_the_gate():
    verdict = blind_assessment_gate(
        artifact_seq=11,
        iteration=2,
        current_reader_digest="rd",
        blind=None,
        canary=None,
        carried_from=_blind_record(),
        carry_iteration=1,
        credited_ids={"b1"},
        live_runner_version="0.145.0",
    )
    assert verdict.credited


def test_a_carry_that_does_not_move_the_round_forward_is_refused():
    """Regression for a self-audit finding: the gate used to SYNTHESISE the current round
    as "the carried round plus one", which made this check pass unconditionally. A check
    that can never fail is not a check."""
    verdict = blind_assessment_gate(
        artifact_seq=11,
        iteration=1,
        current_reader_digest="rd",
        blind=None,
        canary=None,
        carried_from=_blind_record(),
        carry_iteration=1,
        credited_ids={"b1"},
        live_runner_version="0.145.0",
    )
    assert not verdict.credited
    assert any("перенос вперёд невозможен" in reason for reason in verdict.reasons)


def test_a_carry_naming_a_round_its_record_does_not_have_is_refused():
    verdict = blind_assessment_gate(
        artifact_seq=11,
        iteration=2,
        current_reader_digest="rd",
        blind=None,
        canary=None,
        carried_from=_blind_record(),
        carry_iteration=7,
        credited_ids={"b1"},
    )
    assert not verdict.credited
    assert any("относится к кругу 1" in reason for reason in verdict.reasons)



def test_convergence_refuses_a_carry_whose_source_was_never_credited():
    """Critic finding `convergence-carry-credit-not-enforced`. The planning path got this a
    round earlier and the convergence path did not: the class had been swept along the axis
    "names that assert a check" instead of "every path that can produce a carry", so the
    sibling survived. An earlier version of the acceptance test below asserted the OPPOSITE —
    that a bare record with no canary, no channel facts and no transcript is carried — which
    is exactly how a test locks in a hole."""
    verdict = blind_assessment_gate(
        artifact_seq=11,
        iteration=2,
        current_reader_digest="rd",
        blind=None,
        canary=None,
        carried_from=_blind_record(),
        carry_iteration=1,
        credited_ids=set(),
    )
    assert not verdict.credited
    assert any("не прошедшую проверку засчитываемости" in r for r in verdict.reasons)


def test_a_run_of_another_version_does_not_satisfy_this_version():
    canary = _blind_record(
        id="c1",
        kind=RunKind.CANARY,
        launch_number=1,
        started_at=T0 - timedelta(minutes=10),
        finished_at=T0 - timedelta(minutes=5),
        canary_verdict_clean=True,
        markers_message_seq=1,
        answer_message_seq=2,
        answer_sha256="a",
        canary_record_id=None,
        prompt_digest=None,
        reader_digest=None,
        contract_version=None,
        contract_sha256=None,
        category_set_version=None,
    )
    verdict = blind_assessment_gate(
        artifact_seq=99,
        iteration=2,
        current_reader_digest="rd",
        blind=_blind_record(),
        canary=canary,
        markers_published_at=T0 - timedelta(minutes=20),
        observed_answer_sha256="a",
    )
    assert not verdict.credited
    assert any("гейт спрашивает про 99" in reason for reason in verdict.reasons)


# --- the split axes ----------------------------------------------------------------------


# B.9 A-5/B-4 retired the cold pass for EVERY review (`cold_pass_assigned` is gone):
# each pass now runs in a fresh session over the default-hidden projection, which is the
# stronger form of what the cold pass guarded. The audience genre's own declaration
# below is untouched — its config contract predates the retirement and stays explicit
# (the audience genre is out of B.9's scope, S-2).


def test_an_audience_review_must_DECLARE_that_the_cold_pass_is_not_assigned():
    assert config_refusals({"genre": AUDIENCE_GENRE}) != []
    assert config_refusals({"genre": AUDIENCE_GENRE, "cold_verdict_first": True}) != []
    assert config_refusals(
        {"genre": AUDIENCE_GENRE, "cold_verdict_first": False,
         "strategic_reader": False, "machine_comb": False}
    ) == []
    assert config_refusals(None) == []


def test_an_unknown_genre_is_refused_rather_than_treated_as_none():
    assert config_refusals({"genre": "домашнее"}) != []


def test_the_watcher_carries_the_genre_and_refuses_an_undeclared_one():
    msgs = [
        {"seq": 1, "role": "development", "kind": "artifact",
         "payload": {"mode": "spec", "bundle": {"spec_markdown": "x"}}}
    ]
    declared = watcher.plan(
        msgs,
        {"genre": AUDIENCE_GENRE, "cold_verdict_first": False,
         "strategic_reader": False, "machine_comb": False},
    )
    assert declared == {
        "action": "review", "artifact_seq": 1, "mode": "spec",
        "genre": AUDIENCE_GENRE,
        "genre_roles": {"strategic_reader": False, "machine_comb": False},
        "intent_summary": False,
    }
    silent = watcher.plan(msgs, {"genre": AUDIENCE_GENRE})
    assert silent["action"] == "config_refused"


# --- the denominator on the coverage manifest --------------------------------------------


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def test_the_audience_denominator_is_exactly_the_sections(repo):
    """No divergence rows: a deck has no scope over the repository to diverge from."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    body = parse_reader_artifact(COMPOSITE).render()
    manifest = coverage.build_manifest(
        mode="spec",
        base=head,
        commit=head,
        repo_root=repo,
        spec_markdown=body,
        genre=AUDIENCE_GENRE,
    )
    kinds = {row["kind"] for row in manifest["rows"]}
    assert kinds == {"element", "blind_edge"}
    assert sorted(r["locator"] for r in manifest["rows"] if r["kind"] == "element") == [
        "S-1", "S-2", "S-3"
    ]


def test_without_the_genre_the_same_body_gets_spec_divergence_rows(repo):
    """Shown side by side, because this is exactly what the genre axis is for."""
    head = _git(repo, "rev-parse", "HEAD").strip()
    body = parse_reader_artifact(COMPOSITE).render() + "\n`Dockerfile` упомянут в тексте.\n"
    manifest = coverage.build_manifest(
        mode="spec", base=head, commit=head, repo_root=repo, spec_markdown=body
    )
    assert any(row["kind"] == "divergence" for row in manifest["rows"])


def test_an_audience_artifact_is_carried_as_a_text_bundle(repo):
    head = _git(repo, "rev-parse", "HEAD").strip()
    with pytest.raises(coverage.ManifestError):
        coverage.build_manifest(
            mode="code", base=head, commit=head, repo_root=repo, genre=AUDIENCE_GENRE
        )


def test_the_genre_is_reachable_from_the_command_line(repo, tmp_path, capsys):
    """The sanctioned path is the CLI; a branch only reachable from Python is not reachable.

    Found by the pre-submission self-audit: `genre` existed on the function and nothing
    could set it through the command the loop actually runs.
    """
    head = _git(repo, "rev-parse", "HEAD").strip()
    body = tmp_path / "artifact.md"
    body.write_text(parse_reader_artifact(COMPOSITE).render(), encoding="utf-8")
    stakes = tmp_path / "stakes.txt"
    stakes.write_text("", encoding="utf-8")

    rc = coverage.main(
        [
            "--mode", "spec", "--base", head, "--commit", head,
            "--repo-root", str(repo), "--spec-file", str(body),
            "--high-stakes-file", str(stakes), "--genre", AUDIENCE_GENRE,
        ]
    )
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert {row["kind"] for row in manifest["rows"]} == {"element", "blind_edge"}


def test_the_artefact_loads_from_a_file(tmp_path):
    path = tmp_path / "artifact.md"
    path.write_text(COMPOSITE, encoding="utf-8")
    assert load_reader_artifact(path).section_ids() == ("S-1", "S-2", "S-3")


def test_a_section_identifier_has_exactly_one_spelling():
    text = COMPOSITE.replace("### S-3 (цифры", "### S-03 (цифры")
    with pytest.raises(ArtifactError) as refusal:
        parse_reader_artifact(text)
    assert any("не канонически" in reason for reason in refusal.value.reasons)
