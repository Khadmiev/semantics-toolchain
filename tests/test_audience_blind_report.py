# SPDX-License-Identifier: Apache-2.0
"""The blind report as a closed schema: valid or refused, with the identity untouched.

The class this closes (v2): both manual reserves of review f0c0b685 were regex over
markdown headings — findings existed, the parser did not see them, and emptiness was
indistinguishable from cleanliness. Under the schema, "did not parse" has no silent form.
What the tests also hold still is the OTHER half of the spec: the identity formula and the
published item's field list did not change by one character, so dispositions given before
the format change keep covering the same findings after it.
"""

import json

from assistant_memory.audience.blind_report import (
    ITEM_FIELDS,
    extract_findings,
    finding_identity,
    item_refusals,
    render_blind_report,
    report_refusals,
)
from assistant_memory.audience.contract import (
    ContractItem,
    OperatorFlag,
    PublicContract,
    PublicField,
)
from assistant_memory.audience.report_schema import (
    SECTION_TABLE,
    SectionBudgets,
    derive_sections,
    render_schema_block,
)
from assistant_memory.audience.structured import parse_structured_answer, word_count


def _public(flag_overrides=None) -> PublicContract:
    flags = {
        OperatorFlag.PURPOSE_INCLUDES_EVALUATION: False,
        OperatorFlag.LANGUAGE_NOT_NATIVE: False,
        OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING: False,
    }
    flags.update(flag_overrides or {})
    return PublicContract(
        version=1,
        items=(
            ContractItem("C-2", PublicField.KNOWS, "Знает статистику."),
            ContractItem("C-4", PublicField.PURPOSE, "Понять, стоит ли ввязываться."),
        ),
        flags=flags,
    )


SECTIONS = derive_sections(_public())
BUDGETS = SectionBudgets(retelling_floor_words=3)
KWARGS = dict(
    sections=SECTIONS,
    budgets=BUDGETS,
    categories=("НЕПОНЯТНО", "ПЕРЕГРУЖЕНО"),
    section_ids=("S-1", "S-2"),
    public_items=("C-2", "C-4"),
)


def _report(**over) -> dict:
    base = {
        "пересказ": "Текст про двух читателей: незнающий видит непонятное, знающий — неправду.",
        "таблица_по_разделам": [
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": [0, 1]},
        ],
        "замечания": [
            {
                "раздел": "S-2",
                "категория": "НЕПОНЯТНО",
                "текст": "термин «леджер» вводится без объяснения",
                "пункт_контракта": "C-2",
            },
            {
                "раздел": "S-2",
                "категория": "ПЕРЕГРУЖЕНО",
                "текст": "три новых понятия в одном абзаце",
                "пункт_контракта": "C-4",
            },
        ],
        "вопросы": [{"текст": "Зачем второй читатель?", "ответ": "отсутствует"}],
        "впечатление": "Ровно.",
        "что_ещё_заметил": "",
    }
    base.update(over)
    return base


# --- the boundary: exactly one JSON object ------------------------------------------------


def test_a_bare_object_and_a_fenced_object_both_parse():
    body = json.dumps(_report(), ensure_ascii=False)
    for answer in (body, f"```json\n{body}\n```", f"```\n{body}\n```"):
        obj, reasons = parse_structured_answer(answer)
        assert reasons == [] and obj == _report()


def test_substantive_text_outside_the_object_is_a_refusal():
    """An aside living next to the object is a finding the schema cannot see — the exact
    silent form this format buries."""
    body = json.dumps(_report(), ensure_ascii=False)
    _, reasons = parse_structured_answer(body + "\nИ ещё одно замечание прозой.")
    assert any("вне JSON-объекта" in r for r in reasons)


def test_a_non_object_top_level_is_a_refusal():
    _, reasons = parse_structured_answer('["список", "не", "объект"]')
    assert any("а контракт требует объект" in r for r in reasons)


def test_prose_that_is_not_json_is_a_refusal_with_the_reason():
    _, reasons = parse_structured_answer("ЧТО Я ПОНЯЛ — текст про двух читателей.")
    assert any("не разбирается как JSON" in r for r in reasons)


def test_word_count_collapses_whitespace():
    assert word_count("  два   слова \n и   ещё\tдва  ") == 5


# --- the closed schema, both directions ----------------------------------------------------


def test_a_conforming_report_is_valid():
    assert report_refusals(_report(), **KWARGS) == []


def test_an_unknown_section_is_a_refusal():
    report = _report(советы="добавить примеров")
    assert any("вне выведенного набора" in r for r in report_refusals(report, **KWARGS))


def test_a_missing_section_is_a_refusal():
    report = _report()
    del report["вопросы"]
    assert any("«вопросы» отсутствует" in r for r in report_refusals(report, **KWARGS))


def test_conditional_sections_are_present_exactly_when_derived():
    """The two refusals are the top-level closure itself — no special case to drift."""
    # not derived (flag off) but present → unknown field
    undeserved = _report(готовность_и_уверенность="всё готово")
    assert any(
        "вне выведенного набора" in r for r in report_refusals(undeserved, **KWARGS)
    )
    # derived (flag on) but absent → missing required
    flagged = derive_sections(_public({OperatorFlag.PURPOSE_INCLUDES_EVALUATION: True}))
    reasons = report_refusals(_report(), **{**KWARGS, "sections": flagged})
    assert any("«готовность_и_уверенность» отсутствует" in r for r in reasons)
    # derived and present, non-empty → valid
    ok = _report(готовность_и_уверенность="Готово всё, кроме финала; уверен умеренно.")
    assert report_refusals(ok, **{**KWARGS, "sections": flagged}) == []


def test_the_retelling_budget_refuses_in_both_directions():
    short = _report(пересказ="Мало.")
    assert any("при поле бюджета" in r for r in report_refusals(short, **KWARGS))
    long = _report(пересказ="слово " * 900)
    assert any("при потолке бюджета" in r for r in report_refusals(long, **KWARGS))


def test_semantically_empty_required_strings_are_refused_and_the_valve_may_be_empty():
    empty = _report(впечатление="   ")
    assert any("семантически пуста" in r for r in report_refusals(empty, **KWARGS))
    assert report_refusals(_report(что_ещё_заметил=""), **KWARGS) == []


def test_a_clean_reading_is_a_valid_report():
    clean = _report(
        замечания=[],
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": []},
        ],
        вопросы=[],
    )
    assert report_refusals(clean, **KWARGS) == []


# --- the denominator and the mutual checks -------------------------------------------------


def test_a_missing_denominator_row_is_a_refusal():
    report = _report(таблица_по_разделам=[{"раздел": "S-2", "находки": [0, 1]}])
    assert any("нет строк для разделов" in r for r in report_refusals(report, **KWARGS))


def test_an_unknown_or_duplicated_row_is_a_refusal():
    report = _report(
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": [0, 1]},
            {"раздел": "S-2", "находки": []},
            {"раздел": "S-9", "находки": []},
        ]
    )
    reasons = report_refusals(report, **KWARGS)
    assert any("дважды" in r for r in reasons)
    assert any("которых нет в этой версии" in r for r in reasons)


def test_an_index_that_does_not_exist_is_a_refusal():
    report = _report(
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": [0, 1, 7]},
        ]
    )
    assert any("индекс 7 не существует" in r for r in report_refusals(report, **KWARGS))


def test_a_finding_listed_in_no_row_or_two_rows_is_a_refusal():
    orphan = _report(
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": [0]},
        ]
    )
    assert any("не перечислены ни в одной" in r for r in report_refusals(orphan, **KWARGS))
    twice = _report(
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": [0]},
            {"раздел": "S-2", "находки": [0, 1]},
        ]
    )
    reasons = report_refusals(twice, **KWARGS)
    # index 0 is a finding of S-2 listed under S-1 — both the address mismatch and the
    # two-rows rule fire, and both reasons ride out
    assert any("перечислено в строке раздела" in r for r in reasons)
    assert any("более чем в одной строке" in r for r in reasons)


def test_a_finding_with_an_unknown_category_section_or_anchor_is_refused():
    report = _report(
        замечания=[
            {
                "раздел": "S-9",
                "категория": "СОВЕТ",
                "текст": "лучше переписать",
                "пункт_контракта": "C-7",
            },
        ],
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": []},
        ],
    )
    reasons = report_refusals(report, **KWARGS)
    assert any("S-9" in r and "не существует" in r for r in reasons)
    assert any("вне выведенного набора этой версии" in r for r in reasons)
    assert any("не разрешается в публичную часть" in r for r in reasons)


def test_a_verbatim_duplicate_finding_is_refused_at_the_boundary():
    """One essence must not live in two copies — the phase schema would refuse the doubled
    id later, but the boundary is where the reader can still be told."""
    report = _report(
        замечания=[_report()["замечания"][0], dict(_report()["замечания"][0])],
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": [0, 1]},
        ],
    )
    assert any("одна сущность в двух копиях" in r for r in report_refusals(report, **KWARGS))


def test_a_question_with_an_answer_outside_the_closed_list_is_refused():
    report = _report(вопросы=[{"текст": "Где ответ?", "ответ": "наверное есть"}])
    assert any("закрытый перечень" in r for r in report_refusals(report, **KWARGS))


# --- extraction: the identity did not move -------------------------------------------------


def test_extraction_derives_ids_by_the_unchanged_formula():
    items = extract_findings(_report(), contract_version=1, contract_sha256="c1")
    assert [i["section"] for i in items] == ["S-2", "S-2"]
    assert items[0]["category"] == "НЕПОНЯТНО"
    assert items[0]["contract_item"] == "C-2"
    assert items[0]["id"] == finding_identity(
        "НЕПОНЯТНО", "S-2", "термин «леджер» вводится без объяснения", "c1"
    )
    assert set(items[0]) == set(ITEM_FIELDS)
    assert item_refusals(items[0]) == []


def test_the_identity_is_the_dedup_key_and_survives_whitespace():
    a = finding_identity("непонятно", "S-2", "термин  «леджер»   без объяснения", "c1")
    b = finding_identity("НЕПОНЯТНО", "S-2", "термин «леджер» без объяснения", "c1")
    assert a == b


def test_a_contract_change_changes_the_identity():
    """A finding is a claim about how a DESCRIBED reader met the text: change the
    description and the same words are a different finding, owed its own review."""
    a = finding_identity("непонятно", "S-2", "термин без объяснения", "c1")
    b = finding_identity("непонятно", "S-2", "термин без объяснения", "c2")
    assert a != b


def test_an_item_with_a_free_floating_id_is_refused():
    item = dict(extract_findings(_report(), contract_version=1, contract_sha256="c1")[0])
    item["id"] = "B-anything"
    assert any("не выведен из его же содержимого" in r for r in item_refusals(item))


def test_item_fields_did_not_change_with_the_format():
    """Said as a set literal on purpose: the spec pins the list by name, and a drift here
    would re-seed the dedup registry through a technical re-format."""
    assert ITEM_FIELDS == {
        "id", "section", "category", "text", "contract_item",
        "contract_version", "contract_sha256",
    }


# --- the derivation and its structural limits ----------------------------------------------


def test_sections_switch_on_by_operator_flags_and_move_the_version():
    base = derive_sections(_public())
    assert "передать_наверх" not in base.names and "готовность_и_уверенность" not in base.names
    upward = derive_sections(
        _public({OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING: True})
    )
    assert "передать_наверх" in upward.names
    assert upward.version != base.version


def test_every_condition_names_a_public_flag_or_is_unconditional():
    """The first structural limit: a section derived from a private field is an echo
    manufactured by structure. Checked against the registry, not against prose."""
    public_vocabulary = {f.value for f in OperatorFlag} | {f.value for f in PublicField}
    for rule in SECTION_TABLE:
        assert rule.condition == "всегда" or any(
            name in rule.condition for name in public_vocabulary
        ), rule.condition


def test_the_free_valve_is_exactly_one():
    """The second structural limit: every unlimited text field is a road back to free-form
    markdown."""
    assert [r.name for r in SECTION_TABLE if "клапан" in r.description] == ["что_ещё_заметил"]


def test_the_schema_block_offers_exactly_the_derived_sections_with_the_budgets():
    block = render_schema_block(SECTIONS, BUDGETS)
    assert '"пересказ"' in block and '"что_ещё_заметил"' in block
    assert '"передать_наверх"' not in block
    assert "не меньше 3 и не больше 800 слов" in block
    assert "РОВНО ОДИН JSON-объект" in block


def test_budget_refusals_catch_an_unsatisfiable_configuration():
    broken = SectionBudgets(retelling_floor_words=900, retelling_ceiling_words=800)
    assert any("выше потолка" in r for r in broken.refusals())
    assert any("не положителен" in r for r in SectionBudgets(impression_ceiling_words=0).refusals())


# --- the render: deterministic, and a derivative of the object -----------------------------


def test_the_render_is_deterministic_and_readable():
    first = render_blind_report(_report(), SECTIONS)
    second = render_blind_report(_report(), SECTIONS)
    assert first == second
    assert "## Пересказ" in first
    assert "| S-1 | чисто |" in first
    assert "| S-2 | замечания: №1, №2 |" in first
    assert "1. [S-2] НЕПОНЯТНО — термин «леджер» вводится без объяснения — C-2" in first
    assert "— ответ: отсутствует" in first


def test_the_render_of_a_clean_reading_says_clean_out_loud():
    clean = _report(
        замечания=[],
        таблица_по_разделам=[
            {"раздел": "S-1", "находки": []},
            {"раздел": "S-2", "находки": []},
        ],
        вопросы=[],
    )
    text = render_blind_report(clean, SECTIONS)
    assert "Замечаний нет — чистое чтение." in text
    assert "Вопросов нет." in text
