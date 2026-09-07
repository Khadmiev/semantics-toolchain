# SPDX-License-Identifier: Apache-2.0
"""Step 4 of the audience genre: the claim map, its automaton, and the convergence gate.

The tests are organised around the two things that can go wrong here, and they are not the
same thing:

- SOMEONE SIGNS WHAT THEY MAY NOT SIGN. Development is the side with an interest in closing,
  so it may propose and never confirm. Most tests below assert that a plausible-looking
  terminal status is NOT terminal.
- SOMETHING MOVES UNDER A SIGNATURE. A confirmation is bound to a triple, and an edit to the
  quote, the form or the step must void it. Asserted by editing after signing, not by
  trusting a reset path to have been called.

Plus one test that asserts a NON-guarantee: an ownerless material claim is invisible here on
purpose, and the suite says so rather than letting a later reader assume coverage.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant_memory.audience.claim_map import (
    CLAIM_MAP_PHASE,
    ClaimIdLedger,
    ClaimMapError,
    ClaimRow,
    ClaimStatus,
    Party,
    PresentationForm,
    SimplificationPassport,
    attest,
    check_payload,
    render_markdown,
    to_payload,
    validate_rows,
)
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractItem,
    OperatorFlag,
    PrivateContract,
    PrivateField,
    PublicContract,
    PublicField,
)
from assistant_memory.audience.planner import convergence_gate
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord
from assistant_memory.audience.sections import parse_reader_artifact

ARTIFACT = parse_reader_artifact(
    "Части:\n"
    "- план по слайдам: документ\n"
    "- текст для чтения: произносимый\n"
    "\n"
    "### S-1 (план по слайдам) — О чём это\n"
    "Двадцать пять типов знания.\n"
    "\n"
    "### S-2 (текст для чтения) — Вступление\n"
    "В графе больше тысячи фактов.\n"
)

T0 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _row(**overrides) -> ClaimRow:
    base = dict(id="M-1", section="S-1", quote="двадцать пять типов знания")
    base.update(overrides)
    return ClaimRow(**base)


# B.8 F-2: a CONFIRMED status carries typed evidence of reading — the fixture signs
# like a conforming informed pass.
EVIDENCE = {"ground": "graph", "read_ref": "r1", "node_id": "1afaf640", "version": "v3"}


def _confirmed(**overrides) -> ClaimRow:
    """A row an informed pass has properly signed off."""
    row = _row(recorded_status=ClaimStatus.CONFIRMED, **overrides)
    return ClaimRow(
        **{
            **row.__dict__,
            "status_attestation": attest(
                row, by=Party.INFORMED, basis="реестр типов, 2026-08-04",
                evidence=EVIDENCE,
            ),
        }
    )


def _contract() -> AudienceContract:
    return AudienceContract(
        public=PublicContract(
            version=1,
            items=(
                ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
                ContractItem("C-2", PublicField.PURPOSE, "Понять, стоит ли ввязываться."),
            ),
            flags={
                OperatorFlag.PURPOSE_INCLUDES_EVALUATION: False,
                OperatorFlag.LANGUAGE_NOT_NATIVE: False,
            },
        ),
        private=PrivateContract(
            version=1,
            items=(ContractItem("C-3", PrivateField.DELIVERY, "Упрощения разрешены."),),
        ),
    )


# --- who may sign what -------------------------------------------------------------------


def test_development_cannot_reach_a_terminal_status_by_writing_one():
    """The side with an interest in closing may propose, never confirm."""
    row = _row(recorded_status=ClaimStatus.CONFIRMED)
    row = ClaimRow(
        **{
            **row.__dict__,
            "status_attestation": attest(row, by=Party.DEVELOPMENT, basis="я сверил"),
        }
    )
    assert row.status is ClaimStatus.UNCONFIRMED
    assert not row.terminal
    assert "вправе только" in row.blocking_reason()


def test_confirmed_is_terminal_when_the_informed_pass_signs_it():
    row = _confirmed()
    assert row.status is ClaimStatus.CONFIRMED
    assert row.terminal and row.blocking_reason() is None


def test_only_the_operator_may_withdraw_a_claim():
    row = _row(recorded_status=ClaimStatus.WITHDRAWN)
    by_informed = ClaimRow(
        **{
            **row.__dict__,
            "status_attestation": attest(row, by=Party.INFORMED, basis="убрал"),
        }
    )
    assert by_informed.status is ClaimStatus.UNCONFIRMED

    by_operator = ClaimRow(
        **{
            **row.__dict__,
            "status_attestation": attest(row, by=Party.OPERATOR, basis="снял со слайда"),
        }
    )
    assert by_operator.status is ClaimStatus.WITHDRAWN


def test_a_withdrawn_row_stays_in_the_map():
    """It keeps its number and its history — disappearing would erase the fact it existed."""
    row = _row(recorded_status=ClaimStatus.WITHDRAWN)
    row = ClaimRow(
        **{**row.__dict__, "status_attestation": attest(row, by=Party.OPERATOR, basis="снял")}
    )
    assert validate_rows([row], ARTIFACT) == []
    assert row.id == "M-1"


def test_a_simplification_without_a_passport_is_a_proposal_and_blocks():
    row = _row(recorded_status=ClaimStatus.SIMPLIFIED)
    row = ClaimRow(
        **{**row.__dict__, "status_attestation": attest(row, by=Party.INFORMED, basis="ок")}
    )
    assert row.status is ClaimStatus.UNCONFIRMED
    assert "без полного паспорта" in row.blocking_reason()


def test_a_simplification_with_a_passport_and_a_confirmation_is_terminal():
    row = _row(
        recorded_status=ClaimStatus.SIMPLIFIED,
        passport=SimplificationPassport(
            original_fact="в графе 1131 узел",
            withheld="аудитория не узнает точного числа",
            permitted_by="C-3",
        ),
    )
    row = ClaimRow(
        **{
            **row.__dict__,
            "status_attestation": attest(row, by=Party.INFORMED, basis="сверил"),
        }
    )
    assert row.status is ClaimStatus.SIMPLIFIED and row.terminal


# --- the triple: what an edit voids -------------------------------------------------------


def test_editing_the_quote_voids_the_confirmation():
    row = _confirmed()
    edited = ClaimRow(**{**row.__dict__, "quote": "двадцать шесть типов знания"})
    assert edited.status is ClaimStatus.UNCONFIRMED
    assert "другой тройке" in edited.blocking_reason()


def test_editing_the_presentation_form_voids_the_confirmation_too():
    """The form is in the triple because it is the field that moves the waiver boundary."""
    row = _confirmed(recorded_form=PresentationForm.EXACT)
    edited = ClaimRow(
        **{**row.__dict__, "recorded_form": PresentationForm.ROUNDED, "rounding_step": "100"}
    )
    assert edited.status is ClaimStatus.UNCONFIRMED


def test_editing_only_the_rounding_step_voids_it_as_well():
    row = _confirmed(recorded_form=PresentationForm.ROUNDED, rounding_step="100")
    edited = ClaimRow(**{**row.__dict__, "rounding_step": "1000"})
    assert edited.status is ClaimStatus.UNCONFIRMED


def test_re_attesting_after_the_edit_restores_the_row():
    row = _confirmed()
    edited = ClaimRow(**{**row.__dict__, "quote": "двадцать шесть типов знания"})
    signed = ClaimRow(
        **{
            **edited.__dict__,
            "status_attestation": attest(edited, by=Party.INFORMED, basis="перечитал, 2026-08-05",
                                         evidence=EVIDENCE),
        }
    )
    assert signed.status is ClaimStatus.CONFIRMED


# --- the presentation form ----------------------------------------------------------------


def test_an_unconfirmed_form_counts_as_exact():
    """Fail-closed towards NARROWING the owner's waiver right, deliberately."""
    row = _row(recorded_form=PresentationForm.ROUNDED, rounding_step="100")
    assert row.form is PresentationForm.EXACT


def test_a_confirmed_form_counts_as_declared():
    row = _row(recorded_form=PresentationForm.ROUNDED, rounding_step="100")
    row = ClaimRow(
        **{
            **row.__dict__,
            "form_attestation": attest(row, by=Party.INFORMED, basis="прочитал «около 1400»"),
        }
    )
    assert row.form is PresentationForm.ROUNDED


def test_editing_the_quote_sends_the_form_back_to_exact():
    row = _row(recorded_form=PresentationForm.ROUNDED, rounding_step="100")
    row = ClaimRow(
        **{**row.__dict__, "form_attestation": attest(row, by=Party.INFORMED, basis="прочитал")}
    )
    edited = ClaimRow(**{**row.__dict__, "quote": "совсем другое утверждение"})
    assert edited.form is PresentationForm.EXACT


def test_a_non_numeric_claim_has_no_form_at_all():
    assert _row().form is None


# --- validation against the artefact ------------------------------------------------------


def test_a_row_naming_a_section_that_does_not_exist_is_refused():
    """The one mechanical cross-check the spec keeps between the map and the artefact."""
    problems = validate_rows([_row(section="S-9")], ARTIFACT)
    assert any("которого нет в этой версии артефакта" in p for p in problems)


def test_a_free_text_address_is_refused_as_well():
    """The pilot's map addressed rows like «подводка ДЕМО-1 (после S-5)». Not any more."""
    problems = validate_rows([_row(section="подводка ДЕМО-1 (после S-5)")], ARTIFACT)
    assert problems


def test_a_duplicate_identifier_is_refused():
    problems = validate_rows([_row(), _row(section="S-2")], ARTIFACT)
    assert any("выдан дважды" in p for p in problems)


def test_a_rounded_form_without_a_step_is_refused():
    problems = validate_rows([_row(recorded_form=PresentationForm.ROUNDED)], ARTIFACT)
    assert any("без шага округления" in p for p in problems)


def test_a_step_without_a_form_is_refused():
    problems = validate_rows([_row(rounding_step="100")], ARTIFACT)
    assert any("без формы подачи" in p for p in problems)


def test_a_passport_citing_a_contract_item_that_does_not_exist_is_refused():
    """A sanction living outside the contract does not exist for the reader."""
    row = _row(
        recorded_status=ClaimStatus.SIMPLIFIED,
        passport=SimplificationPassport("факт", "чего не узнает", "C-99"),
    )
    problems = validate_rows([row], ARTIFACT, contract=_contract())
    assert any("санкция вне контракта" in p for p in problems)


def test_a_clean_map_validates():
    assert validate_rows([_confirmed(), _row(id="M-2", section="S-2")], ARTIFACT) == []


def test_an_ownerless_material_claim_is_NOT_detected_and_that_is_deliberate():
    """The named limit, asserted so nobody later assumes coverage that was never claimed.

    S-2 asserts «больше тысячи фактов» and no row covers it. The map still validates: a row
    that does not exist produces no gap, because the machine cannot notice what it was never
    told. Catching this is the informed reader's judgement.
    """
    assert validate_rows([_confirmed()], ARTIFACT) == []
    assert "тысячи фактов" in ARTIFACT.render()


# --- the identifier ledger ----------------------------------------------------------------


def test_a_retired_claim_number_is_never_reissued(tmp_path: Path):
    ledger = ClaimIdLedger.load(tmp_path / "claims.json")
    ledger.register([_row(), _row(id="M-2", section="S-2")], 10)
    assert ledger.next_free() == "M-3"

    ledger.register([_row()], 11)
    assert ledger.entries[2].retired_in == 11

    with pytest.raises(ClaimMapError) as refusal:
        ledger.register([_row(), _row(id="M-2", section="S-2", quote="другое")], 12)
    assert any("снят в версии 11" in reason for reason in refusal.value.reasons)


# --- the wire form ------------------------------------------------------------------------


def test_the_payload_publishes_the_status_that_COUNTS_not_the_one_written_down():
    """A row development marked confirmed but nobody signed goes out as 'not confirmed'."""
    row = _row(recorded_status=ClaimStatus.CONFIRMED)
    payload = to_payload([row], iteration=2, artifact_seq=11)
    assert payload["rows"][0]["status"] == ClaimStatus.UNCONFIRMED.value
    assert payload["phase"] == CLAIM_MAP_PHASE
    assert check_payload(payload) == []


def test_a_confirmed_row_goes_out_with_its_signature():
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    row = payload["rows"][0]
    assert row["status"] == ClaimStatus.CONFIRMED.value
    assert row["status_by"] == Party.INFORMED.value and row["status_basis"]


def test_a_confirmed_row_states_its_evidence_strength():
    """B.8 impl review round 3 (operator-accepted): no tract in this genre re-reads the
    cited content, so a published confirmation SAYS it was accepted on form — in the wire
    payload, in the validator, and in the human rendering the operator actually reads."""
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    row = payload["rows"][0]
    assert row["evidence_verification"].startswith("form_only")
    assert check_payload(payload) == []
    assert "(доказательство: форма)" in render_markdown([_confirmed()])


def test_a_confirmed_row_without_the_strength_mark_is_refused():
    """A confirmed row that does not name what its evidence establishes is the silent
    form-as-proof the mark exists to close — including a mark claiming more than form."""
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    del payload["rows"][0]["evidence_verification"]
    assert any("evidence_verification" in p for p in check_payload(payload))
    payload["rows"][0]["evidence_verification"] = "verified against the graph"
    assert any("evidence_verification" in p for p in check_payload(payload))


def test_a_form_only_prefixed_overclaim_is_refused():
    """b8-form-only-prefix-allows-overclaim: a prefix check accepted «form_only; content
    independently verified» — an overclaim riding inside the very mark that exists to
    exclude overclaims. Only the canonical constant passes."""
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    payload["rows"][0]["evidence_verification"] = (
        "form_only; content independently verified"
    )
    assert any("evidence_verification" in p for p in check_payload(payload))


def test_an_unknown_field_in_the_phase_is_refused():
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    payload["severity"] = "high"
    assert any("неизвестное поле фазы" in p for p in check_payload(payload))


def test_a_missing_required_field_is_refused():
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    del payload["artifact_seq"]
    assert any("нет обязательного поля фазы" in p for p in check_payload(payload))


def test_an_unknown_field_inside_a_row_is_refused():
    payload = to_payload([_confirmed()], iteration=1, artifact_seq=10)
    payload["rows"][0]["confidence"] = 0.9
    assert any("неизвестное поле строки" in p for p in check_payload(payload))


def test_the_markdown_is_a_rendering_and_survives_a_pipe_in_the_quote():
    rows = [_confirmed(quote="a | b"), _row(id="M-2", section="S-2")]
    table = render_markdown(rows)
    assert "M-1" in table and "M-2" in table
    assert r"a \| b" in table
    assert table.count("\n") == len(rows) + 1  # header + separator + one line per row


# --- the convergence gate ------------------------------------------------------------------


def _blind_record(**overrides) -> RunRecord:
    base = dict(
        id="b1", kind=RunKind.BLIND, iteration=1, artifact_seq=10, profile_digest="p",
        launch_number=2, started_at=T0, finished_at=T0 + timedelta(minutes=5),
        transcript_path="t.log", transcript_sha256="h", tool_calls=(), model="gpt-x",
        model_version="1", spend=100, outcome=RunOutcome.HAPPENED, canary_record_id="c1",
        prompt_digest="pd", reader_digest="rd", contract_version=1, contract_sha256="cs",
        category_set_version="cv", runner_version="0.145.0",
    )
    base.update(overrides)
    return RunRecord(**base)


def _gate(rows, *, informed_clean=True):
    return convergence_gate(
        artifact_seq=11,
        iteration=2,
        current_reader_digest="rd",
        claim_rows=rows,
        informed_clean=informed_clean,
        blind=None,
        canary=None,
        carried_from=_blind_record(),
        carry_iteration=1,
        credited_ids={"b1"},
        live_runner_version=_blind_record().runner_version,
    )


def test_convergence_is_refused_while_a_single_row_is_not_terminal():
    verdict = _gate([_confirmed(), _row(id="M-2", section="S-2")])
    assert not verdict.credited
    assert any("M-2" in reason for reason in verdict.reasons)


def test_convergence_is_refused_when_the_informed_pass_is_not_clean():
    verdict = _gate([_confirmed()], informed_clean=False)
    assert not verdict.credited
    assert any("зрячий проход" in reason for reason in verdict.reasons)


def test_convergence_passes_when_every_input_is_met():
    assert _gate([_confirmed()]).credited


def test_every_failing_reason_is_collected_not_just_the_first():
    verdict = _gate([_row(), _row(id="M-2", section="S-2")], informed_clean=False)
    assert len(verdict.reasons) >= 3


def test_a_claim_identifier_has_exactly_one_spelling():
    problems = validate_rows([_row(), _row(id="M-01", section="S-2")], ARTIFACT)
    assert any("не канонически" in p for p in problems)


def test_a_section_reference_with_a_padded_number_is_not_a_different_section():
    """The address is text here, and S-01 is simply not a section this version has."""
    assert validate_rows([_row(section="S-01")], ARTIFACT)
