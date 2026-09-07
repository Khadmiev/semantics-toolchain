# SPDX-License-Identifier: Apache-2.0
"""Step 2 of the audience genre: the contract as structure, and the prompt built from it.

The tests are organised around what each of the three layers catches, because they catch
DIFFERENT failures and a test suite that only exercised the happy path would make them look
like three copies of one precaution:

- the closed field list catches a field added after review;
- the physical split of the structures catches a bulk serialisation;
- the screen over the finished text catches an error in the substitution itself.

Plus the identifier rule, which is about time rather than leakage: a finding outlives the
contract version it was raised under, so a retired number must never come back.
"""

from pathlib import Path

import pytest

from assistant_memory.audience.blind_prompt import (
    SLOT_ARTIFACT,
    SLOT_CATEGORIES,
    SLOT_CONTRACT,
    SLOT_SCHEMA,
    AssemblyRefused,
    RoleTemplate,
    assemble_blind_prompt,
    prepare_blind_prompt,
    render_public_contract,
    screen_for_private,
)
from assistant_memory.audience.categories import derive_categories
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractError,
    ContractIdLedger,
    ContractItem,
    OperatorFlag,
    PrivateContract,
    PrivateField,
    PrivateFlag,
    PublicContract,
    PublicField,
    parse_private_contract,
    parse_public_contract,
)
from assistant_memory.audience.report_schema import SectionBudgets
from assistant_memory.audience.sections import ReaderArtifact, parse_reader_artifact

REPO = Path(__file__).resolve().parents[1]

# The audience pilot's records are the author's working material and do not travel with
# the distribution (operator ruling 2026-09-03). The tests that read them say so instead
# of failing in every clone; everything that does not need them keeps running.
PILOT_RECORDS = pytest.mark.skipif(
    not (REPO / "docs" / "pilots" / "audience_review_2026-08").is_dir(),
    reason="the audience pilot's records are not part of this distribution",
)
PILOT = REPO / "docs" / "pilots" / "audience_review_2026-08"
TEMPLATE_PATH = REPO / "docs" / "prompts" / "audience" / "blind_reader.md"

PUBLIC_TEXT = """
Версия: 2

Флаги оператора:
- цель чтения включает оценку: нет
- язык не родной аудитории: нет
- цель чтения включает пересказ наверх: нет

Пункты:
- C-1 (что знает) — Знает статистику на уровне первого курса.
- C-2 (зачем читает) — Понять, стоит ли ввязываться.
"""

PRIVATE_TEXT = """
Версия: 2

Флаги оператора:
- цель включает запрос к аудитории: нет

Пункты:
- C-3 (желаемый вынос) — Читатель должен унести, что риск управляем.
"""


def _public(**overrides) -> PublicContract:
    base = dict(
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
    base.update(overrides)
    return PublicContract(**base)


def _private(*items: ContractItem, version: int = 1) -> PrivateContract:
    return PrivateContract(
        version=version,
        items=items,
        flags={PrivateFlag.GOAL_INCLUDES_AUDIENCE_REQUEST: False},
    )


def _template() -> RoleTemplate:
    return RoleTemplate.from_text(
        "<!-- чек-лист оператора: свежая сессия, песочница read-only -->\n"
        "Ты — читатель.\n\n"
        "ЧИТАТЕЛЬ:\n"
        f"{SLOT_CONTRACT}\n\n"
        "ЧТО ИСКАТЬ:\n"
        f"{SLOT_CATEGORIES}\n\n"
        "ФОРМАТ ОТВЕТА:\n"
        f"{SLOT_SCHEMA}\n\n"
        "АРТЕФАКТ:\n"
        f"{SLOT_ARTIFACT}\n",
        source="тест",
    )


def _artifact(body: str = "Текст раздела.") -> ReaderArtifact:
    """A one-section reader artefact — the assembler takes the parsed form, never a blob."""
    return parse_reader_artifact(
        "Части:\n- текст: документ\n\n### S-1 (текст) — Заголовок\n" + body + "\n"
    )


# --- the file format and the closed field list ------------------------------------------


def test_public_contract_parses_items_flags_and_version():
    public = parse_public_contract(PUBLIC_TEXT)
    assert public.version == 2
    assert [item.id for item in public.items] == ["C-1", "C-2"]
    assert public.items[0].field is PublicField.KNOWS
    assert public.flags[OperatorFlag.PURPOSE_INCLUDES_EVALUATION] is False


@pytest.mark.parametrize("repeated", ["да", "нет"])
def test_a_flag_declared_twice_is_refused_whatever_the_second_line_says(repeated):
    """Critic finding `duplicate-operator-flags-accepted`: the flags dict was built in a parse
    loop with no repeat check, so the LAST line won silently. A contract saying «да» and then
    «нет» parsed as «нет» — and the flag decides whether a whole reader category exists and
    enters the contract hash, so the review would converge against a contract the source text
    does not declare. The equal-value repeat is refused too: this file is authored by the
    operator, one declaration per line, and a second line is a mistake, not an update."""
    text = PUBLIC_TEXT.replace(
        "- цель чтения включает оценку: нет",
        f"- цель чтения включает оценку: нет\n- цель чтения включает оценку: {repeated}",
    )
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("объявлен дважды" in reason for reason in refusal.value.reasons)


def test_field_outside_the_closed_list_is_a_refusal_not_a_skip():
    text = PUBLIC_TEXT.replace(
        "- C-2 (зачем читает) — Понять, стоит ли ввязываться.",
        "- C-2 (зачем читает) — Понять, стоит ли ввязываться.\n"
        "- C-9 (тон подачи) — Говорить бодро.",
    )
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("вне закрытого перечня" in reason for reason in refusal.value.reasons)


def test_missing_required_field_is_a_refusal():
    text = PUBLIC_TEXT.replace("- C-2 (зачем читает) — Понять, стоит ли ввязываться.\n", "")
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("зачем читает" in reason for reason in refusal.value.reasons)


def test_paragraph_instead_of_an_item_is_refused():
    """One item is one assertion: the line format refuses what the granularity rule forbids."""
    text = PUBLIC_TEXT + "\nЧитатель также ценит краткость и не любит воду.\n"
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("не является пунктом" in reason for reason in refusal.value.reasons)


def test_a_private_field_in_the_public_file_is_refused():
    text = PUBLIC_TEXT.replace(
        "- C-1 (что знает) — Знает статистику на уровне первого курса.",
        "- C-1 (что знает) — Знает статистику на уровне первого курса.\n"
        "- C-8 (желаемый вынос) — Должна унести, что всё под контролем.",
    )
    with pytest.raises(ContractError):
        parse_public_contract(text)


def test_missing_operator_flag_is_a_refusal_not_a_default():
    """Silence must not choose a category set: the flag belongs to the operator."""
    text = PUBLIC_TEXT.replace("- цель чтения включает оценку: нет\n", "")
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("нет флага оператора" in reason for reason in refusal.value.reasons)


def test_the_same_identifier_twice_is_refused_in_a_single_part_too():
    """A duplicate makes a finding's citation ambiguous, and either part can be parsed alone."""
    text = PUBLIC_TEXT.replace(
        "- C-2 (зачем читает) — Понять, стоит ли ввязываться.",
        "- C-2 (зачем читает) — Понять, стоит ли ввязываться.\n"
        "- C-1 (что знает) — И ещё что-то знает.",
    )
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("выдан дважды" in reason for reason in refusal.value.reasons)


def test_proficiency_without_a_language_is_refused():
    public = _public(
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Знает."),
            ContractItem("C-2", PublicField.PURPOSE, "Читает."),
            ContractItem("C-3", PublicField.PROFICIENCY, "Уровень B2."),
        )
    )
    assert any("не объявлен сам язык" in problem for problem in public.validate())


def test_parts_with_different_versions_do_not_form_a_contract():
    contract = AudienceContract(
        public=parse_public_contract(PUBLIC_TEXT),
        private=parse_private_contract(PRIVATE_TEXT.replace("Версия: 2", "Версия: 3")),
    )
    assert any("версии частей расходятся" in problem for problem in contract.validate())


# --- the identifier ledger --------------------------------------------------------------


def test_ledger_never_hands_a_retired_number_to_a_new_item(tmp_path):
    ledger = ContractIdLedger.load(tmp_path / "ids.json")
    ledger.register(AudienceContract(public=_public(), private=_private()))

    shrunk = _public(
        version=2,
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
            ContractItem("C-2", PublicField.PURPOSE, "Понять, стоит ли ввязываться."),
            ContractItem("C-3", PublicField.CEILING, "Без рабочих цифр."),
        ),
    )
    ledger.register(AudienceContract(public=shrunk, private=_private(version=2)))
    assert ledger.next_free() == "C-4"

    without_ceiling = _public(version=3)
    ledger.register(AudienceContract(public=without_ceiling, private=_private(version=3)))
    assert ledger.entries[3].retired_in == 3

    reused = _public(
        version=4,
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
            ContractItem("C-2", PublicField.PURPOSE, "Понять, стоит ли ввязываться."),
            ContractItem("C-3", PublicField.CEILING, "Совсем другое правило."),
        ),
    )
    with pytest.raises(ContractError) as refusal:
        ledger.register(AudienceContract(public=reused, private=_private(version=4)))
    assert any("снят в версии 3" in reason for reason in refusal.value.reasons)


def test_ledger_refuses_a_version_that_does_not_grow(tmp_path):
    ledger = ContractIdLedger.load(tmp_path / "ids.json")
    ledger.register(AudienceContract(public=_public(version=5), private=_private(version=5)))
    with pytest.raises(ContractError):
        ledger.register(AudienceContract(public=_public(version=5), private=_private(version=5)))


def test_ledger_records_a_move_between_parts_without_refusing_it(tmp_path):
    """Moving an item to the public part is what the leak screen asks the operator to consider."""
    ledger = ContractIdLedger.load(tmp_path / "ids.json")
    private = _private(ContractItem("C-3", PrivateField.DELIVERY, "Метафоры разрешены."))
    ledger.register(AudienceContract(public=_public(), private=private))
    assert ledger.entries[3].label == PrivateField.DELIVERY.value

    moved = _public(
        version=2,
        items=(
            *_public().items,
            ContractItem("C-3", PublicField.CEILING, "Метафоры разрешены."),
        ),
    )
    ledger.register(AudienceContract(public=moved, private=_private(version=2)))
    assert ledger.entries[3].label == PublicField.CEILING.value
    assert ledger.entries[3].retired_in is None
    assert ledger.entries[3].introduced_in == 1


def test_ledger_survives_a_round_trip(tmp_path):
    path = tmp_path / "ids.json"
    ledger = ContractIdLedger.load(path)
    ledger.register(AudienceContract(public=_public(), private=_private()))
    ledger.save()
    assert ContractIdLedger.load(path).entries == ledger.entries


def test_a_refused_registration_changes_nothing(tmp_path):
    ledger = ContractIdLedger.load(tmp_path / "ids.json")
    ledger.register(AudienceContract(public=_public(version=7), private=_private(version=7)))
    before = dict(ledger.entries)
    with pytest.raises(ContractError):
        ledger.register(AudienceContract(public=_public(version=6), private=_private(version=6)))
    assert ledger.entries == before and ledger.last_version == 7


# --- categories derived from the contract -----------------------------------------------


def test_categories_switch_on_by_field_presence():
    minimal = derive_categories(_public())
    assert minimal.names == ("НЕПОНЯТНО", "ПЕРЕГРУЖЕНО", "ТЕРЯЕТСЯ НИТЬ", "ВОПРОС БЕЗ ОТВЕТА")

    with_ceiling = derive_categories(
        _public(items=(*_public().items, ContractItem("C-3", PublicField.CEILING, "Без цифр.")))
    )
    assert "ВЫХОД ЗА РАМКИ" in with_ceiling.names
    assert with_ceiling.version != minimal.version


def test_evaluation_category_comes_from_the_operator_flag_not_from_the_prose():
    """The purpose text says «оценить» in both; only the flag may switch the category on."""
    prose = _public(
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Знает."),
            ContractItem("C-2", PublicField.PURPOSE, "Прочитать и оценить замысел."),
        )
    )
    assert "НЕ УБЕЖДАЕТ" not in derive_categories(prose).names

    flagged = _public(
        items=prose.items,
        flags={
            OperatorFlag.PURPOSE_INCLUDES_EVALUATION: True,
            OperatorFlag.LANGUAGE_NOT_NATIVE: False,
            OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING: False,
        },
    )
    assert "НЕ УБЕЖДАЕТ" in derive_categories(flagged).names


def test_category_version_ignores_a_rewording_that_changes_no_category():
    reworded = _public(
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Совершенно другой текст про знание."),
            ContractItem("C-2", PublicField.PURPOSE, "И другой текст про цель."),
        )
    )
    assert derive_categories(reworded).version == derive_categories(_public()).version


# --- the role template: two assembly fields, whole-line placeholders ---------------------


def test_launcher_instructions_are_a_separate_field_and_never_reach_the_reader():
    template = _template()
    assert "чек-лист оператора" in template.launcher_instructions
    assert "чек-лист оператора" not in template.reader_text
    text, _, _ = assemble_blind_prompt(template, _public(), "АРТЕФАКТ", SectionBudgets())
    assert "чек-лист" not in text


def test_template_with_an_unknown_placeholder_is_refused():
    with pytest.raises(AssemblyRefused) as refusal:
        RoleTemplate.from_text(
            f"Ты — читатель.\n{SLOT_CONTRACT}\n{SLOT_CATEGORIES}\n{SLOT_ARTIFACT}\n{{ЦЕЛЬ}}\n"
        )
    assert any("неизвестный плейсхолдер" in reason for reason in refusal.value.reasons)


def test_template_missing_a_placeholder_is_refused():
    with pytest.raises(AssemblyRefused):
        RoleTemplate.from_text(f"Ты — читатель.\n{SLOT_CONTRACT}\n{SLOT_ARTIFACT}\n")


def test_substitution_is_line_wise_and_leaves_braces_inside_a_line_alone():
    template = RoleTemplate.from_text(
        f"Пиши ответ как {{КОНТРАКТ}} внутри строки — это не плейсхолдер.\n"
        f"{SLOT_CONTRACT}\n{SLOT_CATEGORIES}\n{SLOT_SCHEMA}\n{SLOT_ARTIFACT}\n"
    )
    text, _, _ = assemble_blind_prompt(template, _public(), "АРТЕФАКТ", SectionBudgets())
    assert "как {КОНТРАКТ} внутри строки" in text


def test_the_artifact_arrives_whole_even_when_it_looks_like_a_placeholder():
    template = _template()
    text, _, _ = assemble_blind_prompt(
        template, _public(), "{КОНТРАКТ}\nи ещё строка", SectionBudgets()
    )
    assert "и ещё строка" in text


# --- assembly, and what it carries -------------------------------------------------------


def test_identifiers_ride_into_the_prompt_so_a_finding_can_cite_one():
    text, _, _ = assemble_blind_prompt(_template(), _public(), "АРТЕФАКТ", SectionBudgets())
    assert "C-1 —" in text and "C-2 —" in text
    assert "ЧТО ЗНАЕТ:" in render_public_contract(_public())


def test_assembly_refuses_an_invalid_public_part_before_building_anything():
    broken = _public(items=(ContractItem("C-1", PublicField.KNOWS, "Знает."),))
    with pytest.raises(AssemblyRefused):
        assemble_blind_prompt(_template(), broken, "АРТЕФАКТ", SectionBudgets())


def test_the_assembler_has_no_parameter_for_the_private_part():
    """Layer 2 is the signature itself, so it is asserted as a property of the signature."""
    import inspect

    parameters = inspect.signature(assemble_blind_prompt).parameters
    annotations = {str(p.annotation) for p in parameters.values()}
    assert not any("Private" in annotation for annotation in annotations)


# --- the screen over the finished text ---------------------------------------------------


def test_a_private_value_in_the_prompt_blocks_the_run_and_goes_to_the_operator():
    leaking = _public(
        items=(
            ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
            ContractItem(
                "C-2", PublicField.PURPOSE, "Понять, что риск управляем — это главное."
            ),
        )
    )
    private = _private(
        ContractItem("C-9", PrivateField.TAKEAWAY, "что риск   УПРАВЛЯЕМ")
    )
    contract = AudienceContract(public=leaking, private=private)
    with pytest.raises(AssemblyRefused) as refusal:
        prepare_blind_prompt(_template(), contract, _artifact(), SectionBudgets())
    assert refusal.value.needs_operator is True
    assert any("C-9" in reason for reason in refusal.value.reasons)


def test_the_screen_normalises_case_and_whitespace():
    matches = screen_for_private(
        "текст, в котором   Риск\nуправляем",
        _private(ContractItem("C-9", PrivateField.TAKEAWAY, "риск управляем")),
    )
    assert [match.item_id for match in matches] == ["C-9"]


def test_a_clean_contract_assembles_and_carries_both_digests():
    contract = AudienceContract(
        public=_public(),
        private=_private(ContractItem("C-9", PrivateField.TAKEAWAY, "Совсем другая мысль.")),
    )
    prompt = prepare_blind_prompt(_template(), contract, _artifact(), SectionBudgets())
    assert prompt.contract_version == 1
    assert prompt.contract_sha256 != prompt.contract_public_sha256
    assert prompt.digest and prompt.template_digest
    assert "ВОПРОС БЕЗ ОТВЕТА" in prompt.text


def test_editing_only_the_private_part_moves_the_contract_digest_but_not_the_public_one():
    """The two digests answer different questions, and the test says which is which."""
    first = AudienceContract(
        public=_public(), private=_private(ContractItem("C-9", PrivateField.TAKEAWAY, "А."))
    )
    second = AudienceContract(
        public=_public(), private=_private(ContractItem("C-9", PrivateField.TAKEAWAY, "Б."))
    )
    assert first.sha256 != second.sha256
    assert first.public_sha256 == second.public_sha256


# --- the genre must not share a name with the critic watcher's cold pass -----------------


#: The two exemptions, both narrow and both stated rather than discovered later.
#:
#: A line citing the pilot's own frozen directory: those filenames were written before the
#: naming rule existed and they are evidence, so a reference to them is traceability, not a
#: mechanism sharing a name. Renaming them would break every document that cites them.
#:
#: A line naming the OTHER mechanism in order to say we do not share its name: forbidding
#: that would forbid explaining the rule in the place it applies.
_COLD_NAME_EXEMPT = ("docs/pilots/audience_review_2026-08/", "cold-verdict", "cold_verdict")


def test_the_audience_module_shares_no_name_with_the_cold_verdict_mechanism():
    """Checked by search over both names, because a shared name is how the blind pass gets
    "implemented" as context editing inside one session — and the defect stays invisible."""
    package = REPO / "src" / "assistant_memory" / "audience"
    offenders = {}
    for path in package.glob("*.py"):
        hits = [
            number
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "cold" in line.lower()
            and not any(exempt in line.lower() for exempt in _COLD_NAME_EXEMPT)
        ]
        if hits:
            offenders[path.name] = hits
    assert not offenders


def test_the_production_template_carries_no_cold_name():
    assert "cold" not in TEMPLATE_PATH.read_text(encoding="utf-8").lower()


# --- a refused assembly is a RECORDED attempt, and it moves no counter -------------------


def test_a_refused_assembly_is_recorded_and_does_not_consume_a_launch_number(tmp_path):
    """Every attempt is recorded, refused ones included — otherwise the series shows only wins.

    And the journal must NOT move: a number consumed by something that never launched would
    push the next honest canary/reading pair apart and break a binding with nothing wrong
    with it.
    """
    from assistant_memory.audience.journal import LaunchJournal
    from assistant_memory.audience.launcher import BlindLauncher
    from assistant_memory.audience.profile import LaunchProfile
    from assistant_memory.audience.records import RunKind, RunOutcome

    settings = tmp_path / "codex_blind"
    settings.mkdir()
    (settings / "auth.json").write_text("token", encoding="utf-8")
    journal = LaunchJournal(tmp_path / "launches.jsonl")
    launcher = BlindLauncher(
        profile=LaunchProfile(settings_dir=settings, model="gpt-x"),
        journal=journal,
        invoke=lambda prompt: pytest.fail("модель не должна вызываться при отказе сборки"),
        transcripts_dir=tmp_path / "transcripts",
    )

    contract = AudienceContract(
        public=_public(
            items=(
                ContractItem("C-1", PublicField.KNOWS, "Знает статистику."),
                ContractItem("C-2", PublicField.PURPOSE, "Понять, что риск управляем."),
            )
        ),
        private=_private(ContractItem("C-9", PrivateField.TAKEAWAY, "риск управляем")),
    )
    with pytest.raises(AssemblyRefused) as refusal:
        prepare_blind_prompt(_template(), contract, _artifact(), SectionBudgets())

    record = launcher.record_refusal(
        refusal.value, kind=RunKind.BLIND, iteration=1, artifact_seq=7, model_version="gpt-x"
    )
    assert record.outcome is RunOutcome.NOT_STARTED
    assert record.launch_number is None
    assert record.missing_fields() == []
    assert journal.entries() == [] and journal.next_number() == 1
    assert "C-9" in Path(record.transcript_path).read_text(encoding="utf-8")


# --- golden: the pilot's own contract, decomposed ----------------------------------------

#: The pilot's contract files are FROZEN evidence and predate the v2 public flag «цель
#: чтения включает пересказ наверх» (the flag list is closed and every flag must be
#: declared). The evidence is not edited; the extension is applied at the test boundary,
#: which is exactly what an operator re-freezing that contract today would write.
_V2_FLAG_LINE = "- цель чтения включает пересказ наверх: нет"


def _pilot_contract() -> AudienceContract:
    public_text = (PILOT / "contract_v1_public.md").read_text(encoding="utf-8").replace(
        "- язык не родной аудитории: нет",
        "- язык не родной аудитории: нет\n" + _V2_FLAG_LINE,
    )
    private_text = (PILOT / "contract_v1_private.md").read_text(encoding="utf-8").replace(
        "Пункты:",
        "Флаги оператора:\n- цель включает запрос к аудитории: нет\n\nПункты:",
        1,
    )
    return AudienceContract(
        public=parse_public_contract(public_text),
        private=parse_private_contract(private_text),
    )



@PILOT_RECORDS
def test_pilot_contract_loads_and_registers(tmp_path):
    contract = _pilot_contract()
    ContractIdLedger.load(tmp_path / "ids.json").register(contract)
    assert contract.version == 1
    assert len(contract.public.items) == 11
    assert len(contract.private.items) == 3
    assert contract.public.declared_fields() == {
        PublicField.KNOWS,
        PublicField.CEILING,
        PublicField.PURPOSE,
        PublicField.BUDGET,
    }


@PILOT_RECORDS
def test_pilot_contract_yields_the_categories_the_pilot_prompt_was_missing():
    """The measurement this whole derivation exists for.

    The pilot's prompt carried a hand-written list of five categories. The contract it was
    frozen against declares a ceiling of rights and a slot, and the operator declares the
    purpose includes evaluating — so three categories were owed and absent. On blind pass
    number four all four findings landed in "question without an answer", including one
    that was a breach of the ceiling: the only category asserting nothing about the text is
    where everything without a shelf drains.

    It also DROPS one the pilot prompt had: nothing in that contract declares a delivery
    language, and the audience's language is native, so "unnatural" had no contractual
    basis. Derivation removes as well as adds, and that direction is asserted too.
    """
    contract = _pilot_contract()
    names = derive_categories(contract.public).names
    assert "ВЫХОД ЗА РАМКИ" in names
    assert "ПРОВИСАЕТ" in names
    assert "НЕ УБЕЖДАЕТ" in names
    assert "НЕЕСТЕСТВЕННО" not in names


@PILOT_RECORDS
def test_the_pilot_delivery_policy_is_private_and_the_screen_would_catch_it():
    """It rode inside the pilot's single contract file, one handle among four."""
    contract = _pilot_contract()
    policy = next(
        item for item in contract.private.items if item.field is PrivateField.DELIVERY
    )
    assert screen_for_private(f"…и ещё: {policy.text}", contract.private)


@PILOT_RECORDS
def test_the_pilot_contract_assembles_against_the_production_template():
    contract = _pilot_contract()
    prompt = prepare_blind_prompt(
        RoleTemplate.from_file(TEMPLATE_PATH), contract, _artifact(), SectionBudgets()
    )
    assert "ИНСТРУКЦИИ ЗАПУСКАЮЩЕМУ" not in prompt.text
    assert "LeadTech" in prompt.text  # the ceiling item, quoted to the reader verbatim
    assert "### S-1 (текст) — Заголовок" in prompt.text
    assert prompt.categories.version == derive_categories(contract.public).version


def test_an_operator_flag_moves_the_contract_hash():
    """Critic finding `contract-flags-omitted-from-digest`: the flags decide which categories
    the blind reader is given, so an edit to one changed what he is asked to look for while
    the contract's hash stood still — and the hash exists so that exactly that cannot hide."""
    first = AudienceContract(public=_public(), private=_private())
    flipped = AudienceContract(
        public=_public(
            flags={
                OperatorFlag.PURPOSE_INCLUDES_EVALUATION: True,
                OperatorFlag.LANGUAGE_NOT_NATIVE: False,
            }
        ),
        private=_private(),
    )
    assert first.sha256 != flipped.sha256
    assert first.public_sha256 != flipped.public_sha256


def test_the_whole_contract_hash_is_built_from_the_public_one():
    """One serialisation, not two: the earlier pair dropped the flags in both copies at once."""
    contract = AudienceContract(public=_public(), private=_private())
    assert contract.canonical_text().startswith(contract.canonical_public_text())


def test_an_identifier_has_exactly_one_spelling():
    """Critic finding `numeric-id-aliases`: C-1 and C-01 are two strings and one integer, so
    the duplicate checks (text) let both through while the ledger (number) folded them into
    one entry — two supposedly permanent identifiers becoming the same identifier."""
    text = PUBLIC_TEXT.replace(
        "- C-2 (зачем читает) — Понять, стоит ли ввязываться.",
        "- C-2 (зачем читает) — Понять, стоит ли ввязываться.\n"
        "- C-01 (что знает) — Тот же номер другим написанием.",
    )
    with pytest.raises(ContractError) as refusal:
        parse_public_contract(text)
    assert any("не канонически" in reason for reason in refusal.value.reasons)
