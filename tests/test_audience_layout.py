# SPDX-License-Identifier: Apache-2.0
"""The review's description on disk, and reading the state back from the channel.

Two properties are asserted, and both are about the same thing — that nothing this genre
refuses to guess can arrive by accident:

- a declaration that is ABSENT is refused, and refused by name, while a declaration that is
  deliberately EMPTY is accepted. Those two are the same bytes to a careless reader and
  opposite facts to this genre;
- a run record survives the round trip through the channel unchanged, because the driver
  reads the state from the channel instead of remembering it, and a lossy trip would make
  the state quietly different from the evidence.
"""

from datetime import UTC, datetime, timedelta

import pytest

from assistant_memory.audience import channel
from assistant_memory.audience.layout import LayoutRefused, load_declarations, load_review
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord

T0 = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)

PUBLIC = """# Контракт, публичная часть

Версия: 1

Флаги оператора:
- цель чтения включает оценку: нет
- язык не родной аудитории: нет
- цель чтения включает пересказ наверх: нет

Пункты:
- C-1 (что знает) — Знает статистику.
- C-2 (зачем читает) — Понять, стоит ли ввязываться.
"""

PRIVATE = """# Контракт, приватная часть

Версия: 1

Флаги оператора:
- цель включает запрос к аудитории: нет

Пункты:
- C-9 (желаемый вынос) — Должна унести, что всё под контролем.
"""

ARTIFACT = """# Колода

Части:
- слайды: документ

### S-1 (слайды) — Что это
Текст.
"""

TEMPLATE = (
    "<!-- служебное -->\nЧитаешь впервые.\n"
    "{КОНТРАКТ}\n{КАТЕГОРИИ}\n{СХЕМА_ОТВЕТА}\n{АРТЕФАКТ}\n"
)

DECLARATIONS = """
[reader]
model = "gpt-x"
model_version = "2026-08"

[audience]
canary_markers = ["assistant_memory", "леджер"]
known_terms = []
introduced_terms = ["читатель"]

[audience.budgets]
"слайды" = 12.0

[machine]
settings_dir = "~/.codex_blind"
work_root = "/tmp/blind-work"
runner_binary = "/usr/bin/codex"
"""


def _review(tmp_path, declarations: str = DECLARATIONS, **files):
    root = tmp_path / "review"
    root.mkdir(exist_ok=True)
    contents = {
        "contract_public.md": PUBLIC,
        "contract_private.md": PRIVATE,
        "artifact.md": ARTIFACT,
        "blind_reader.md": TEMPLATE,
        "review.toml": declarations,
    }
    contents.update(files)
    for name, text in contents.items():
        if text is not None:
            (root / name).write_text(text, encoding="utf-8")
    return root


def test_a_complete_directory_assembles(tmp_path):
    review = load_review(_review(tmp_path))
    assert review.contract.public.version == 1
    assert review.declarations.model == "gpt-x"
    assert review.declarations.budgets == {"слайды": 12.0}
    assert "читатель" in review.declarations.inventory.introduced


def test_a_missing_file_is_refused_by_name(tmp_path):
    root = _review(tmp_path, **{"contract_private.md": None})
    with pytest.raises(LayoutRefused) as refusal:
        load_review(root)
    assert any("contract_private.md" in r for r in refusal.value.reasons)


def test_an_empty_known_list_is_a_declaration_but_an_absent_one_is_not(tmp_path):
    """The distinction the whole mechanical layer rests on: "this audience knows none of it"
    is an answer; "nobody said" is not, and a report over an undeclared inventory is
    indistinguishable from a clean one."""
    without = DECLARATIONS.replace("known_terms = []\n", "")
    with pytest.raises(LayoutRefused) as refusal:
        load_declarations(_review(tmp_path, declarations=without) / "review.toml")
    assert any("known_terms" in r for r in refusal.value.reasons)

    ok = load_declarations(_review(tmp_path, declarations=DECLARATIONS) / "review.toml")
    assert ok.inventory.known == frozenset()


def test_an_empty_marker_list_is_refused_at_the_declaration(tmp_path):
    empty = DECLARATIONS.replace(
        'canary_markers = ["assistant_memory", "леджер"]', "canary_markers = []"
    )
    with pytest.raises(LayoutRefused) as refusal:
        load_declarations(_review(tmp_path, declarations=empty) / "review.toml")
    assert any("canary_markers" in r for r in refusal.value.reasons)


def test_an_unknown_threshold_key_is_refused_rather_than_ignored(tmp_path):
    """A threshold that does not exist would simply not apply, and the report would look as
    if it had — the quiet failure this genre keeps finding."""
    text = DECLARATIONS + '\n[thresholds]\nmade_up_threshold = 5\n'
    with pytest.raises(LayoutRefused) as refusal:
        load_declarations(_review(tmp_path, declarations=text) / "review.toml")
    assert any("неизвестные ключи" in r for r in refusal.value.reasons)


def test_machine_paths_are_settings_and_not_constants(tmp_path):
    """The declared build horizon says other operators run this; a hard-coded path would be a
    machine of ours baked into their review."""
    review = load_review(_review(tmp_path))
    assert str(review.declarations.machine.runner_binary).endswith("codex")
    assert review.layout.journal.parent == review.layout.root  # never inside the profile


# --- the state is READ from the channel, not remembered ---------------------------------------


def _record() -> RunRecord:
    return RunRecord(
        id="blind-1", kind=RunKind.BLIND, iteration=3, artifact_seq=42, profile_digest="P",
        launch_number=8, started_at=T0, finished_at=T0 + timedelta(minutes=7),
        transcript_path="runs/blind-1.log", transcript_sha256="h", tool_calls=(),
        model="gpt-x", model_version="2026-08", spend=14376, outcome=RunOutcome.HAPPENED,
        runner_version="0.145.0", sandbox_mode="read-only", approval_mode="never",
        canary_record_id="canary-1", prompt_digest="pr", reader_digest="R",
        contract_version=4, contract_sha256="c4", category_set_version="cs1",
        report_schema_version="ss1", section_budgets='{"пол": 250}',
        render_path="runs/blind-1_report.md", render_sha256="r1",
        annulled_record_id=None,
        mech_thresholds_digest="th",
    )


def test_a_record_survives_the_round_trip_through_the_channel():
    """Lossy would mean the state the driver plans on is quietly different from the evidence
    the channel holds — and the evidence is the thing that is supposed to outlive the run."""
    payload = channel.run_record_message(_record())["record"]
    assert channel.record_from_payload(payload) == _record()


def test_published_records_come_back_in_channel_order():
    messages = [
        {"seq": 1, "kind": "notice", "role": "development",
         "payload": channel.run_record_message(_record())},
        {"seq": 2, "kind": "artifact", "role": "development", "payload": {"mode": "spec"}},
    ]
    phases = channel.read_phases(messages)
    assert [r.id for r in channel.published_records(phases)] == ["blind-1"]


def test_report_budget_knobs_load_and_unknown_or_broken_ones_are_refused(tmp_path):
    """The budgets are knobs like the thresholds: a budget that does not exist must not
    silently not apply, and one that cannot be satisfied must not wait for a live run."""
    text = DECLARATIONS + "\n[report_budgets]\nretelling_floor_words = 300\n"
    loaded = load_declarations(_review(tmp_path, declarations=text) / "review.toml")
    assert loaded.report_budgets.retelling_floor_words == 300

    unknown = DECLARATIONS + "\n[report_budgets]\nmade_up_budget = 5\n"
    with pytest.raises(LayoutRefused) as refusal:
        load_declarations(_review(tmp_path, declarations=unknown) / "review.toml")
    assert any("неизвестные ключи" in r for r in refusal.value.reasons)

    broken = DECLARATIONS + "\n[report_budgets]\nretelling_floor_words = 900\n"
    with pytest.raises(LayoutRefused) as refusal:
        load_declarations(_review(tmp_path, declarations=broken) / "review.toml")
    assert any("выше потолка" in r for r in refusal.value.reasons)
