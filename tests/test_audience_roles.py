# SPDX-License-Identifier: Apache-2.0
"""The v2 roles: the strategic reader and the machine comb, on the common contract.

What the tests hold still is the construction the spec leans on: categories derived from
the contract (never from erudition), admissibility as a predicate (never a silent
switch-off), the quote-address check and the single scope canonicalisation, identities
bound to each role's own instrument, dedup marks by the asymmetric rule, and the two
convergence conditions that exist exactly when the review config declares the role on.
"""


import pytest

from assistant_memory.audience import channel
from assistant_memory.audience.blind_prompt import AssemblyRefused, RoleTemplate
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractError,
    ContractItem,
    OperatorFlag,
    PrivateContract,
    PrivateField,
    PrivateFlag,
    PublicContract,
    PublicField,
    parse_private_contract,
)
from assistant_memory.audience.dedup import mark_findings, mark_refusals
from assistant_memory.audience.machine_comb import (
    MACHINE_CATEGORY_NAMES,
    machine_finding_identity,
    machine_table_version,
)
from assistant_memory.audience.machine_comb import (
    report_refusals as machine_report_refusals,
)
from assistant_memory.audience.records import (
    RunKind,
    RunOutcome,
    credit_role_run,
)
from assistant_memory.audience.role_prompts import (
    MACHINE_SLOTS,
    STRATEGIC_SLOTS,
    prepare_machine_prompt,
    prepare_strategic_prompt,
    role_fingerprint,
)
from assistant_memory.audience.sections import parse_reader_artifact
from assistant_memory.audience.strategic import (
    derive_strategic_categories,
    inclusion_refusals,
    strategic_finding_identity,
)
from assistant_memory.audience.strategic import (
    report_refusals as strategic_report_refusals,
)
from assistant_memory.audience.structured import canonical_scope

ARTIFACT = parse_reader_artifact(
    "Части:\n- слайды: документ\n\n"
    "### S-1 (слайды) — Что это\nМеханизм читает текст двумя читателями.\n\n"
    "### S-2 (слайды) — Зачем\nНезнающий видит непонятное, знающий видит неправду.\n"
)
SECTION_TEXTS = {s.id: s.render() for s in ARTIFACT.sections}


def _contract(*, takeaway=True, request_flag=False) -> AudienceContract:
    private_items = (
        (ContractItem("C-9", PrivateField.TAKEAWAY, "Унести, что всё под контролем."),)
        if takeaway
        else (ContractItem("C-9", PrivateField.DELIVERY, "Метафоры разрешены."),)
    )
    return AudienceContract(
        public=PublicContract(
            version=1,
            items=(
                ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
                ContractItem("C-2", PublicField.PURPOSE, "Понять, стоит ли ввязываться."),
                ContractItem("C-3", PublicField.LANGUAGE, "Русский."),
            ),
            flags={
                OperatorFlag.PURPOSE_INCLUDES_EVALUATION: False,
                OperatorFlag.LANGUAGE_NOT_NATIVE: False,
                OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING: False,
            },
        ),
        private=PrivateContract(
            version=1,
            items=private_items,
            flags={PrivateFlag.GOAL_INCLUDES_AUDIENCE_REQUEST: request_flag},
        ),
    )


STRATEGIC_KWARGS = dict(
    categories=derive_strategic_categories(_contract()).names,
    section_ids=("S-1", "S-2"),
    section_texts=SECTION_TEXTS,
    contract_items=("C-1", "C-2", "C-3", "C-9"),
)


def _strategic_report(**over) -> dict:
    base = {
        "итог": "Текст ведёт к объявленному выносу, но финал разжижает его оговорками.",
        "находки": [
            {
                "категория": "ОГОВОРКИ ОПРОКИДЫВАЮТ",
                "scope": "целое",
                "текст": "К финалу оговорки перевешивают вынос «всё под контролем».",
                "цитаты": ["Незнающий видит непонятное"],
                "пункт_контракта": "C-9",
            }
        ],
    }
    base.update(over)
    return base


def _machine_report(**over) -> dict:
    base = {
        "находки": [
            {
                "категория": "ШТАМП",
                "scope": ["S-2"],
                "текст": "Симметричная связка двух половин — машинный ритм.",
                "цитаты": ["Незнающий видит непонятное, знающий видит неправду."],
            }
        ]
    }
    base.update(over)
    return base


# --- private flags (the contract side of the strategic role) --------------------------------


def test_private_flags_parse_and_a_repeat_or_absence_is_refused():
    text = (
        "Версия: 1\n\nФлаги оператора:\n- цель включает запрос к аудитории: да\n\n"
        "Пункты:\n- C-9 (желаемый вынос) — Вынос.\n"
    )
    private = parse_private_contract(text)
    assert private.flags[PrivateFlag.GOAL_INCLUDES_AUDIENCE_REQUEST] is True

    with pytest.raises(ContractError) as refusal:
        parse_private_contract(
            text.replace(
                "- цель включает запрос к аудитории: да",
                "- цель включает запрос к аудитории: да\n"
                "- цель включает запрос к аудитории: нет",
            )
        )
    assert any("объявлен дважды" in r for r in refusal.value.reasons)

    with pytest.raises(ContractError) as refusal:
        parse_private_contract(text.replace("- цель включает запрос к аудитории: да\n", ""))
    assert any("нет приватного флага" in r for r in refusal.value.reasons)


def test_a_private_flag_moves_the_full_hash_and_not_the_public_one():
    """The flag decides which strategic categories exist, so it lives in the FULL canon —
    and through it in the strategic fingerprint; the blind reader's half stands still."""
    off, on = _contract(request_flag=False), _contract(request_flag=True)
    assert off.sha256 != on.sha256
    assert off.public_sha256 == on.public_sha256


# --- the strategic categories and the admissibility predicate -------------------------------


def test_strategic_categories_derive_from_the_private_flag():
    base = derive_strategic_categories(_contract())
    assert "ЗАПРОС ОТСУТСТВУЕТ ИЛИ СЛАБ" not in base.names
    assert len(base.names) == 3
    flagged = derive_strategic_categories(_contract(request_flag=True))
    assert "ЗАПРОС ОТСУТСТВУЕТ ИЛИ СЛАБ" in flagged.names
    assert flagged.version != base.version


def test_enabling_the_role_without_a_takeaway_is_a_configuration_refusal():
    """Never a silent switch-off: a review where the role quietly did not run is
    indistinguishable from one where it ran clean."""
    assert inclusion_refusals(True, _contract(takeaway=True)) == []
    reasons = inclusion_refusals(True, _contract(takeaway=False))
    assert any("отказ конфигурации" in r for r in reasons)
    assert inclusion_refusals(False, _contract(takeaway=False)) == []


# --- the two answer schemas -----------------------------------------------------------------


def test_a_conforming_strategic_report_is_valid_and_may_anchor_into_the_private_part():
    assert strategic_report_refusals(_strategic_report(), **STRATEGIC_KWARGS) == []


def test_the_strategic_summary_ceiling_and_closed_fields_hold():
    long = _strategic_report(итог="слово " * 250)
    assert any("при потолке" in r for r in strategic_report_refusals(long, **STRATEGIC_KWARGS))
    alien = _strategic_report(советы="переписать")
    assert any(
        "вне закрытой схемы" in r for r in strategic_report_refusals(alien, **STRATEGIC_KWARGS)
    )
    clean = _strategic_report(находки=[])
    assert strategic_report_refusals(clean, **STRATEGIC_KWARGS) == []


def test_a_quote_that_is_not_in_its_scope_refuses_crediting():
    report = _strategic_report()
    report["находки"][0]["scope"] = ["S-1"]
    report["находки"][0]["цитаты"] = ["Незнающий видит непонятное"]  # lives in S-2
    reasons = strategic_report_refusals(report, **STRATEGIC_KWARGS)
    assert any("не встречается дословно" in r for r in reasons)


def test_the_whole_scope_searches_every_section_and_collapses_whitespace():
    report = _machine_report()
    report["находки"][0]["scope"] = "целое"
    report["находки"][0]["цитаты"] = ["Механизм  читает\nтекст двумя читателями."]
    assert machine_report_refusals(
        report, section_ids=("S-1", "S-2"), section_texts=SECTION_TEXTS
    ) == []


def test_a_machine_category_outside_the_fixed_table_is_refused():
    report = _machine_report()
    report["находки"][0]["категория"] = "НЕ УБЕЖДАЕТ"
    reasons = machine_report_refusals(
        report, section_ids=("S-1", "S-2"), section_texts=SECTION_TEXTS
    )
    assert any("вне таблицы машинности" in r for r in reasons)
    assert "МАРКЕР" in MACHINE_CATEGORY_NAMES  # the operator's long-dash shelf exists


# --- identities and the single scope canonicalisation ---------------------------------------


def test_scope_is_canonicalised_once_for_validation_and_identity_alike():
    assert canonical_scope(["S-2", "S-1", "S-2"]) == ("S-1", "S-2")
    assert canonical_scope("целое") == "целое"
    a = strategic_finding_identity("К", ["S-2", "S-1"], "текст", "c1")
    b = strategic_finding_identity("К", ["S-1", "S-2", "S-2"], "текст", "c1")
    assert a == b


def test_each_role_identity_is_bound_to_its_own_instrument():
    strategic_a = strategic_finding_identity("К", "целое", "текст", "полный-хэш-1")
    strategic_b = strategic_finding_identity("К", "целое", "текст", "полный-хэш-2")
    assert strategic_a != strategic_b  # the full contract IS the premise
    machine_a = machine_finding_identity("К", "целое", "текст", "таблица-1")
    machine_b = machine_finding_identity("К", "целое", "текст", "таблица-2")
    assert machine_a != machine_b  # the table is the instrument
    assert strategic_a.startswith("SF-") and machine_a.startswith("MF-")


# --- prompt assembly: the separation is the types -------------------------------------------

STRATEGIC_TEMPLATE = RoleTemplate.from_text(
    "Ты — стратегический читатель.\n{КОНТРАКТ_ПОЛНЫЙ}\n{КАТЕГОРИИ}\n"
    "{СХЕМА_ОТВЕТА}\n{АРТЕФАКТ}\n",
    slots=STRATEGIC_SLOTS,
)
MACHINE_TEMPLATE = RoleTemplate.from_text(
    "Ты — прочёсчик машинности.\n{ЯЗЫКОВЫЕ_ПОЛЯ}\n{КАТЕГОРИИ}\n"
    "{СХЕМА_ОТВЕТА}\n{АРТЕФАКТ}\n",
    slots=MACHINE_SLOTS,
)


def test_the_strategic_prompt_carries_both_parts_and_no_leak_screen_applies():
    prompt = prepare_strategic_prompt(STRATEGIC_TEMPLATE, _contract(), ARTIFACT)
    assert "Унести, что всё под контролем." in prompt.text  # the private takeaway, verbatim
    assert "цель включает запрос к аудитории" in prompt.text  # the private flag section
    assert "ОГОВОРКИ ОПРОКИДЫВАЮТ" in prompt.text
    assert prompt.contract_sha256 == _contract().sha256  # the FULL hash rides to the record


def test_the_machine_assembler_refuses_anything_beyond_the_language_fields():
    language = tuple(
        i for i in _contract().public.items if i.field is PublicField.LANGUAGE
    )
    prompt = prepare_machine_prompt(MACHINE_TEMPLATE, language, ARTIFACT)
    assert "Русский." in prompt.text
    assert "Понять, стоит ли ввязываться." not in prompt.text  # no contract beyond language
    with pytest.raises(AssemblyRefused) as refusal:
        prepare_machine_prompt(
            MACHINE_TEMPLATE,
            tuple(_contract().public.items),  # purpose and knowledge in the input
            ARTIFACT,
        )
    assert any("не является языковым" in r for r in refusal.value.reasons)


def test_role_fingerprints_never_collide_across_roles():
    same = dict(instructions_digest="d", model="m", model_version="v")
    assert role_fingerprint("strategic", ARTIFACT, **same) != role_fingerprint(
        "machine", ARTIFACT, **same
    )


def test_a_changed_private_part_moves_the_strategic_fingerprint():
    """The stated difference between the roles: the blind carry survives a private edit,
    the strategic transfer must not."""
    first = prepare_strategic_prompt(STRATEGIC_TEMPLATE, _contract(), ARTIFACT)
    moved = prepare_strategic_prompt(
        STRATEGIC_TEMPLATE, _contract(request_flag=True), ARTIFACT
    )
    a = role_fingerprint(
        "strategic", ARTIFACT,
        instructions_digest=first.instructions_digest, model="m", model_version="v",
    )
    b = role_fingerprint(
        "strategic", ARTIFACT,
        instructions_digest=moved.instructions_digest, model="m", model_version="v",
    )
    assert a != b


# --- dedup marks ----------------------------------------------------------------------------


def _item(item_id, category="К", scope="целое", text="текст"):
    return {"id": item_id, "категория": category, "scope": scope, "текст": text}


def test_dedup_marks_follow_the_asymmetric_rule():
    marked = mark_findings(
        [
            _item("SF-1"),                              # exact id in the registry → «повтор»
            _item("SF-2", category="Д"),                # open in a past iteration → «повтор»
            _item("SF-3", category="Х", text="иначе"),  # coarse match with prior → «кандидат»
            _item("SF-4", category="Н"),                # nothing matches → «новая»
        ],
        disposed={"SF-1": "waived"},
        prior_open=[_item("SF-2", category="Д"), _item("SF-9", category="Х")],
    )
    verdicts = [m["дедуп"]["вердикт"] for m in marked]
    assert verdicts == ["повтор", "повтор", "кандидат", "новая"]
    assert marked[0]["дедуп"]["диспозиция"] == "waived"
    assert marked[1]["дедуп"]["диспозиция"] == "открыта"
    assert marked[2]["дедуп"]["оригинал"] == "SF-9"


def test_an_intra_run_near_double_is_a_candidate_of_its_earlier_twin():
    marked = mark_findings(
        [_item("SF-1", category="Х"), _item("SF-2", category="Х", text="иначе")],
        disposed={},
        prior_open=[],
    )
    assert marked[1]["дедуп"] == {"вердикт": "кандидат", "оригинал": "SF-1"}


def test_a_mark_nobody_can_read_is_refused():
    assert mark_refusals({"вердикт": "повтор"}, "x")  # no original, no disposition
    assert mark_refusals({"вердикт": "новая", "оригинал": "SF-1"}, "x")
    assert mark_refusals("повтор", "x")
    assert mark_refusals({"вердикт": "новая"}, "x") == []


# --- records and the crediting gate ---------------------------------------------------------


def _role_record(**over):
    from datetime import UTC, datetime

    from assistant_memory.audience.records import RunRecord

    t0 = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    base = dict(
        id="st-1", kind=RunKind.STRATEGIC, iteration=1, artifact_seq=10,
        profile_digest="P", launch_number=4, started_at=t0, finished_at=t0,
        transcript_path="runs/st-1.log", transcript_sha256="h", tool_calls=(),
        model="gpt-x", model_version="v", spend=1, outcome=RunOutcome.HAPPENED,
        runner_version="0.145.0", prompt_digest="pd", role_digest="RD",
        category_set_version="cs", contract_version=1, contract_sha256="c1",
        render_path="runs/st-1_report.md", render_sha256="r1",
    )
    return RunRecord(**{**base, **over})


def test_role_records_need_no_isolation_fields_but_do_need_the_render_pair():
    record = _role_record()
    assert record.missing_fields() == []  # sandbox/approval/launch adjacency not demanded
    naked = _role_record(render_path=None, render_sha256=None)
    assert set(naked.missing_fields()) == {"render_path", "render_sha256"}
    machine = _role_record(
        kind=RunKind.MACHINE, contract_version=None, contract_sha256=None
    )
    assert machine.missing_fields() == []  # the contract pair is the strategic kind's own


def test_credit_role_run_checks_digest_and_the_retry_link():
    record = _role_record()
    assert credit_role_run(
        record, all_records=[record], current_role_digest="RD"
    ).credited
    moved = credit_role_run(record, all_records=[record], current_role_digest="OTHER")
    assert any("не совпадает с текущим" in r for r in moved.reasons)

    annulled = _role_record(
        id="st-0", outcome=RunOutcome.NOT_STARTED, outcome_reason="отказ валидации",
    )
    unlinked = credit_role_run(
        record, all_records=[record, annulled], current_role_digest="RD"
    )
    assert any("ОБЯЗАНА ссылаться" in r for r in unlinked.reasons)
    linked = credit_role_run(
        _role_record(annulled_record_id="st-0"),
        all_records=[record, annulled],
        current_role_digest="RD",
    )
    assert linked.credited, linked.reasons


# --- the channel phases ---------------------------------------------------------------------


def _phase_items(contract_sha="c1"):
    item = {
        "категория": "ОГОВОРКИ ОПРОКИДЫВАЮТ",
        "scope": "целое",
        "текст": "Оговорки перевешивают.",
        "цитаты": ["Незнающий видит непонятное"],
        "пункт_контракта": "C-9",
    }
    item["id"] = strategic_finding_identity(
        item["категория"], item["scope"], item["текст"], contract_sha
    )
    item["дедуп"] = {"вердикт": "новая"}
    return [item]


def test_the_strategic_phase_builds_and_refuses_a_foreign_anchor():
    payload = channel.strategic_findings_message(
        10, _phase_items(), run_record_id="st-1", role_digest="RD", iteration=1,
        contract_items=["C-1", "C-9"], contract_sha256="c1",
    )
    assert payload["contract_items"] == ["C-1", "C-9"]
    with pytest.raises(channel.ChannelError) as refusal:
        channel.strategic_findings_message(
            10, _phase_items(), run_record_id="st-1", role_digest="RD", iteration=1,
            contract_items=["C-1"], contract_sha256="c1",
        )
    assert any("вне объявленного перечня" in r for r in refusal.value.reasons)


def test_a_phase_item_with_a_free_floating_id_or_broken_mark_is_refused():
    items = _phase_items()
    items[0]["id"] = "SF-anything"
    with pytest.raises(channel.ChannelError) as refusal:
        channel.strategic_findings_message(
            10, items, run_record_id="st-1", role_digest="RD", iteration=1,
            contract_items=["C-9"], contract_sha256="c1",
        )
    assert any("не выведен из содержимого" in r for r in refusal.value.reasons)

    items = _phase_items()
    items[0]["дедуп"] = {"вердикт": "повтор"}
    with pytest.raises(channel.ChannelError):
        channel.strategic_findings_message(
            10, items, run_record_id="st-1", role_digest="RD", iteration=1,
            contract_items=["C-9"], contract_sha256="c1",
        )


def test_the_machine_phase_builds_with_no_contract_anchor_by_construction():
    item = {
        "категория": "ШТАМП",
        "scope": ["S-2"],
        "текст": "Машинный ритм.",
        "цитаты": ["дословно"],
    }
    item["id"] = machine_finding_identity(
        item["категория"], item["scope"], item["текст"], machine_table_version()
    )
    item["дедуп"] = {"вердикт": "новая"}
    payload = channel.machine_findings_message(
        10, [item], run_record_id="mc-1", role_digest="MD", iteration=1,
        table_version=machine_table_version(),
    )
    assert "contract_items" not in payload


# --- AG-30: the runner probe on role transfers ----------------------------------------------


def test_role_transfer_requires_the_probed_runner_version():
    from assistant_memory.audience.records import carried_role_forward

    ok = carried_role_forward(
        _role_record(), kind=RunKind.STRATEGIC, iteration=2, artifact_seq=11,
        current_role_digest="RD", live_runner_version="0.145.0",
    )
    assert ok.credited, ok.reasons
    moved = carried_role_forward(
        _role_record(), kind=RunKind.STRATEGIC, iteration=2, artifact_seq=11,
        current_role_digest="RD", live_runner_version="0.146.0",
    )
    assert any("версия запускающего изменилась" in r for r in moved.reasons)
    unprobed = carried_role_forward(
        _role_record(), kind=RunKind.STRATEGIC, iteration=2, artifact_seq=11,
        current_role_digest="RD",
    )
    assert any("не установлена" in r for r in unprobed.reasons)


def test_the_probe_parses_a_version_and_fails_closed_on_a_missing_binary(tmp_path):
    import sys as _sys
    from pathlib import Path

    from assistant_memory.audience.isolation import probe_runner_version

    live = probe_runner_version(Path(_sys.executable))
    assert live and live[0].isdigit()  # "Python 3.x.y" -> "3.x.y"
    assert probe_runner_version(tmp_path / "нет_такого.exe") is None
