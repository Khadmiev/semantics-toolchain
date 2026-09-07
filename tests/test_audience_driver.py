# SPDX-License-Identifier: Apache-2.0
"""The assembly: one round of an audience review, and the order it happens in.

The order is the subject here, so the tests replace the world and watch the sequence: the
channel is a list, the model is a function. What is asserted is what the six stages could
never assert, because nothing ran them:

- everything that can refuse for free refuses BEFORE a launch consumes a journal number;
- the marker list reaches the channel BEFORE the canary starts;
- a dirty canary stops the round instead of burning the next number on a reading that could
  never be credited;
- a carry says which round it came from, so "read again" and "not read again" stay different;
- the isolation is ARRANGED by the product, and what the run left behind is kept, not swept.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant_memory.audience import channel
from assistant_memory.audience.blind_prompt import RoleTemplate
from assistant_memory.audience.contract import (
    AudienceContract,
    ContractItem,
    OperatorFlag,
    PrivateContract,
    PrivateField,
    PrivateFlag,
    PublicContract,
    PublicField,
)
from assistant_memory.audience.driver import (
    AudienceRound,
    RoundInputs,
    RoundRefused,
)
from assistant_memory.audience.isolation import ArrangedIsolation, BlindRunner, ProfileHygiene
from assistant_memory.audience.journal import LaunchJournal
from assistant_memory.audience.launcher import BlindLauncher
from assistant_memory.audience.mechanical import TermInventory
from assistant_memory.audience.planner import BlindAction
from assistant_memory.audience.profile import DEFAULT_CONFIG_GLOBS, LaunchProfile
from assistant_memory.audience.records import RunKind, RunOutcome
from assistant_memory.audience.report_schema import SectionBudgets

T0 = datetime(2026, 8, 5, 9, 0, tzinfo=UTC)

ARTIFACT = """# Колода

Части:
- слайды: документ

### S-1 (слайды) — Что это
Механизм читает текст двумя читателями.

### S-2 (слайды) — Зачем
Незнающий видит непонятное, знающий видит неправду.
"""

TEMPLATE = RoleTemplate.from_text(
    "<!-- инструкции запускающему: не едут читателю -->\n"
    "Ты читаешь текст впервые.\n"
    "{КОНТРАКТ}\n"
    "{КАТЕГОРИИ}\n"
    "{СХЕМА_ОТВЕТА}\n"
    "{АРТЕФАКТ}\n"
)

CONFIG = {
    "genre": "audience", "cold_verdict_first": False,
    "strategic_reader": False, "machine_comb": False,
}
MARKERS = ["assistant_memory", "аудиторное ревью", "леджер"]


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
                OperatorFlag.PURPOSE_INCLUDES_UPWARD_RETELLING: False,
            },
        ),
        private=PrivateContract(
            version=1,
            items=(
                ContractItem(
                    "C-9", PrivateField.TAKEAWAY, "Должна унести, что всё под контролем."
                ),
            ),
            flags={PrivateFlag.GOAL_INCLUDES_AUDIENCE_REQUEST: False},
        ),
    )


def _inputs(**over) -> RoundInputs:
    base = dict(
        artifact_seq=10,
        iteration=1,
        artifact_text=ARTIFACT,
        contract=_contract(),
        template=TEMPLATE,
        inventory=TermInventory(known=frozenset(), introduced=frozenset({"читатель"})),
        budgets={},
        markers=MARKERS,
        model="gpt-x",
        model_version="2026-08",
        # A test-sized floor: the production default (250 words) is a knob, and what the
        # tests exercise is the mechanism, not the calibration hypothesis.
        report_budgets=SectionBudgets(retelling_floor_words=3),
        # AG-30: transfers require the probed live runner version; the fake transcripts
        # carry the 0.145.0 banner, so this is the matching environment.
        live_runner_version="0.145.0",
    )
    return RoundInputs(**{**base, **over})


class FakeChannel:
    """The channel as a list — which is exactly enough to watch an order."""

    def __init__(self):
        self.messages = []

    def post(self, route, body):
        seq = len(self.messages) + 1
        self.messages.append(
            {
                "seq": seq,
                "role": body["role"],
                "kind": body["kind"],
                "payload": body["payload"],
                "created_at": (T0 - timedelta(minutes=5)).isoformat(),
            }
        )
        return {"seq": seq}

    def fetch(self, after=0):
        return [m for m in self.messages if m["seq"] > after]

    def phases(self):
        return [m["payload"].get("phase") for m in self.messages if m["kind"] == "notice"]


# The conforming reader answers ONE JSON object against the derived schema — an empty
# findings list with a full denominator is a legitimate clean reading.
BLIND_OK = """```json
{
  "пересказ": "Механизм читает артефакт двумя читателями: незнающий ловит непонятное.",
  "таблица_по_разделам": [
    {"раздел": "S-1", "находки": []},
    {"раздел": "S-2", "находки": []}
  ],
  "замечания": [],
  "вопросы": [],
  "впечатление": "Ровно и понятно.",
  "что_ещё_заметил": ""
}
```"""

#: A conforming answer CARRYING a finding — for the extraction path.
BLIND_WITH_FINDING = """```json
{
  "пересказ": "Механизм читает текст двумя читателями: незнающий видит непонятное.",
  "таблица_по_разделам": [
    {"раздел": "S-1", "находки": [0]},
    {"раздел": "S-2", "находки": []}
  ],
  "замечания": [
    {"раздел": "S-1", "категория": "НЕПОНЯТНО",
     "текст": "Слово «механизм» не объяснено.", "пункт_контракта": "C-1"}
  ],
  "вопросы": [{"текст": "Зачем два читателя?", "ответ": "в артефакте"}],
  "впечатление": "Местами живо.",
  "что_ещё_заметил": ""
}
```"""

HEADER = "\n".join(
    [
        "OpenAI Codex v0.145.0",
        "--------",
        "workdir: C:/tmp/blind",
        "model: gpt-x",
        "approval: never",
        "sandbox: read-only",
        "--------",
        "",
    ]
)


def _round(tmp_path, *, answers=None, config=CONFIG, records=(), credited=()):
    settings = tmp_path / "codex_blind"
    settings.mkdir(exist_ok=True)
    (settings / "auth.json").write_text("token", encoding="utf-8")
    profile = LaunchProfile(settings_dir=settings, model="gpt-x")
    journal = LaunchJournal(tmp_path / "journal.jsonl")
    fake = FakeChannel()

    said = list(answers or ["Сохранённых сведений нет.", BLIND_OK])
    calls = {"n": 0, "prompts": []}

    def invoke(prompt: str) -> str:
        calls["prompts"].append(prompt)
        answer = said[min(calls["n"], len(said) - 1)]
        calls["n"] += 1
        # The provider's real shape: prompt echo under `user`, the answer under `codex`,
        # the spend line after — the answer extractor is anchored to exactly this.
        return HEADER + "user\n(промпт)\ncodex\n" + answer + "\ntokens used\n14,376\n"

    isolation = ArrangedIsolation(
        runner=BlindRunner(binary=Path("codex"), settings_dir=settings, model="gpt-x"),
        work_root=tmp_path / "work",
        hygiene=ProfileHygiene(settings_dir=settings, config_globs=DEFAULT_CONFIG_GLOBS),
    )
    launcher = BlindLauncher(
        profile=profile, journal=journal, invoke=invoke, transcripts_dir=tmp_path / "runs"
    )
    driver = AudienceRound(
        post=fake.post,
        fetch=fake.fetch,
        launcher=launcher,
        isolation=isolation,
        review_config=config,
        transcripts_dir=tmp_path / "runs",
        previous_records=records,
        credited_ids=credited,
    )
    return driver, fake, journal, calls


# --- refusals come first, and they cost nothing --------------------------------------------


def test_a_service_that_does_not_say_the_genre_stops_the_round(tmp_path):
    """The failure this repeats: silence used to select the ordinary protocol. A review whose
    genre cannot be read is not an ordinary review — it is a review nobody has identified."""
    driver, fake, journal, calls = _round(tmp_path, config=None)
    with pytest.raises(RoundRefused) as refusal:
        driver.run(_inputs())
    assert any("жанр ревью не аудиторный" in r for r in refusal.value.reasons)
    assert calls["n"] == 0
    assert journal.next_number() == 1  # nothing was spent


def test_an_artifact_at_the_wrong_heading_level_stops_before_any_launch(tmp_path):
    """The measured failure: the pilot deck was marked up one level up and the coverage
    builder saw zero sections — an artefact accepted in silence with an empty denominator."""
    driver, fake, journal, calls = _round(tmp_path)
    broken = ARTIFACT.replace("### S-1", "## S-1")
    with pytest.raises(RoundRefused) as refusal:
        driver.run(_inputs(artifact_text=broken))
    assert any("не третьего уровня" in r for r in refusal.value.reasons)
    assert calls["n"] == 0
    assert journal.next_number() == 1


def test_a_prompt_carrying_a_private_value_stops_and_goes_to_the_operator(tmp_path):
    """Not filtered — asked. It is either a leak or a phrasing that is actually public and
    belongs in the public part, and development does not get to decide which."""
    driver, fake, journal, calls = _round(tmp_path)
    leaking = ARTIFACT + "\n### S-3 (слайды) — Вынос\nДолжна унести, что всё под контролем.\n"
    with pytest.raises(RoundRefused) as refusal:
        driver.run(_inputs(artifact_text=leaking))
    assert refusal.value.needs_operator
    assert calls["n"] == 0


def test_an_empty_marker_list_stops_the_round(tmp_path):
    """A criterion every answer satisfies keeps the verdict's shape and loses its meaning."""
    driver, fake, journal, calls = _round(tmp_path)
    with pytest.raises(RoundRefused) as refusal:
        driver.run(_inputs(markers=[]))
    assert any("список маркеров" in r for r in refusal.value.reasons)


# --- the order, which is the whole subject --------------------------------------------------


def test_the_marker_list_reaches_the_channel_before_the_canary_runs(tmp_path):
    """The order IS the evidence: a criterion chosen once the answer is known proves nothing
    about the answer."""
    driver, fake, journal, calls = _round(tmp_path)
    outcome = driver.run(_inputs())
    phases = fake.phases()
    assert phases.index(channel.CANARY_MARKERS_PHASE) < phases.index(channel.RUN_RECORD_PHASE)
    assert outcome.canary_record is not None
    assert outcome.canary_record.markers_message_seq == 2  # after mech_report, before the run


def test_the_free_measurement_runs_before_any_model(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    driver.run(_inputs())
    assert fake.phases()[0] == channel.MECH_REPORT_PHASE


def test_a_whole_round_credits_the_pair_and_publishes_both_records(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    outcome = driver.run(_inputs())
    assert outcome.blind_action is BlindAction.RUN
    assert outcome.credited, outcome.credit_reasons
    kinds = [
        m["payload"]["record"]["kind"]
        for m in fake.messages
        if m["payload"].get("phase") == channel.RUN_RECORD_PHASE
    ]
    assert kinds == [RunKind.CANARY.value, RunKind.BLIND.value]
    assert channel.BLIND_FINDINGS_PHASE in fake.phases()
    assert journal.next_number() == 3  # exactly two launches, adjacent


def test_the_reading_gets_the_assembled_prompt_and_the_canary_a_neutral_question(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    driver.run(_inputs())
    canary_prompt, blind_prompt = calls["prompts"]
    assert "сохранённые сведения" in canary_prompt
    assert "Знает статистику" not in canary_prompt  # the canary is not told about the project
    assert "Механизм читает текст двумя читателями" in blind_prompt


# --- a dirty canary stops the round ----------------------------------------------------------


def test_a_dirty_canary_stops_before_the_reading_burns_the_next_number(tmp_path):
    """A reading launched after a dirty canary could never be credited, and the number it ate
    would push the next honest pair apart."""
    driver, fake, journal, calls = _round(
        tmp_path, answers=["Да, я работал над проектом assistant_memory."]
    )
    outcome = driver.run(_inputs())
    assert outcome.canary_record.canary_verdict_clean is False
    assert outcome.canary_record.outcome is RunOutcome.ANNULLED
    assert outcome.blind_record is None
    assert not outcome.credited
    assert calls["n"] == 1  # the reading was never launched
    assert journal.next_number() == 2  # exactly one number consumed


# --- the carry says which round it came from --------------------------------------------------


def test_an_unmoved_fingerprint_carries_the_previous_reading_and_names_its_round(tmp_path):
    """"Read again and found the same" and "not read again" are different facts about this
    version; a carry that hides which one it is turns a saved run into a false one."""
    driver, fake, journal, calls = _round(tmp_path)
    first = driver.run(_inputs())
    assert first.credited

    again, fake2, journal2, calls2 = _round(
        tmp_path,
        records=[first.blind_record],
        credited=[first.blind_record.id],
    )
    # the carry restates its SOURCE PHASE, so the channel history rides into the new round
    fake2.messages = list(fake.messages)
    outcome = again.run(_inputs(artifact_seq=11, iteration=2))
    assert outcome.blind_action is BlindAction.CARRY
    assert outcome.carried_from_iteration == 1
    assert calls2["n"] == 0  # no model was spent
    carried = [
        m["payload"]
        for m in fake2.messages
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE
    ][-1]
    assert carried["carried_from_iteration"] == 1
    assert carried["run_record_id"] == first.blind_record.id


def test_a_source_less_carry_refuses_instead_of_reporting_success(tmp_path):
    """The critic's counterexample: an empty channel beside a creditable record used to
    publish an empty carry and report credited — a success a later layer would quietly
    contradict."""
    driver, fake, journal, calls = _round(tmp_path)
    first = driver.run(_inputs())

    again, fake2, journal2, calls2 = _round(
        tmp_path, records=[first.blind_record], credited=[first.blind_record.id]
    )
    with pytest.raises(RoundRefused) as refusal:
        again.run(_inputs(artifact_seq=11, iteration=2))
    assert any("нет исходной фазы" in r for r in refusal.value.reasons)
    assert not [
        m for m in fake2.messages
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE
    ]


def test_a_moved_artifact_forces_a_fresh_reading(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    first = driver.run(_inputs())
    again, fake2, journal2, calls2 = _round(
        tmp_path, records=[first.blind_record], credited=[first.blind_record.id]
    )
    outcome = again.run(
        _inputs(artifact_seq=11, iteration=2, artifact_text=ARTIFACT + "\nДописано.\n")
    )
    assert outcome.blind_action is BlindAction.RUN
    assert calls2["n"] == 2


# --- the isolation is arranged by the product, not by a script ----------------------------------


def test_the_profile_is_wiped_of_everything_that_is_not_configuration(tmp_path):
    """Not tidiness: the previous reading's session sitting in the profile would make the next
    reading of the same deck not blind to the first one."""
    driver, fake, journal, calls = _round(tmp_path)
    settings = tmp_path / "codex_blind"
    (settings / "sessions").mkdir()
    (settings / "sessions" / "prior.json").write_text("что читали в прошлый раз", encoding="utf-8")
    (settings / "history.jsonl").write_text("...", encoding="utf-8")

    driver.run(_inputs())

    assert not (settings / "sessions").exists()
    assert not (settings / "history.jsonl").exists()
    assert (settings / "auth.json").exists()  # configuration survives, or the run cannot start


def test_wiping_the_profile_does_not_move_its_fingerprint(tmp_path):
    """It must not: the fingerprint binds the canary to the reading, and a cleaning that moved
    it would break the binding it exists to protect."""
    driver, fake, journal, calls = _round(tmp_path)
    settings = tmp_path / "codex_blind"
    profile = LaunchProfile(settings_dir=settings, model="gpt-x")
    before = profile.fingerprint().digest
    (settings / "cache").mkdir()
    (settings / "cache" / "junk").write_text("x", encoding="utf-8")
    driver.isolation.hygiene.clean()
    assert profile.fingerprint().digest == before


def test_the_working_directory_is_refused_inside_a_repository(tmp_path):
    """`read-only` forbids writing, not reading: a working directory inside the project puts
    the project within the blind reader's reach."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    driver, fake, journal, calls = _round(tmp_path)
    driver.isolation.work_root = repo / "work"
    with pytest.raises(Exception) as refusal:
        driver.run(_inputs())
    assert "лежит внутри репозитория" in str(refusal.value)


def test_files_left_in_the_working_directory_are_kept_as_evidence(tmp_path):
    """Something wrote where the sandbox forbids writing. A cleaner that erases that erases a
    signal — so the directory stays and the anomaly is carried up."""
    driver, fake, journal, calls = _round(tmp_path)

    original = driver.isolation.runner

    def writing_runner(prompt, *, work_area):
        (work_area.path / "нежданный.txt").write_text("я тут был", encoding="utf-8")
        # the ANSWER conforms to the schema; the anomaly is the file
        return HEADER + "user\n(промпт)\ncodex\n" + BLIND_OK + "\ntokens used\n1\n"

    driver.launcher.invoke = lambda prompt: writing_runner(
        prompt, work_area=driver.isolation._area
    )
    outcome = driver.run(_inputs())
    assert outcome.residue == ("нежданный.txt",)
    assert (driver.isolation.work_root / "нежданный.txt").exists()
    assert any("сохранён как улика" in r for r in outcome.reasons)
    assert original is driver.isolation.runner


# --- the channel schemas are closed in both directions ------------------------------------------


def test_an_unknown_field_in_a_phase_is_refused_on_the_way_out():
    with pytest.raises(channel.ChannelError) as refusal:
        channel.post_phase(
            lambda route, body: {"seq": 1},
            {"phase": channel.MECH_REPORT_PHASE, "artifact_seq": 1, "выдумка": 1},
        )
    assert any("вне закрытого перечня" in r for r in refusal.value.reasons)


def test_a_malformed_phase_on_the_way_in_is_refused_not_skipped():
    """Skipping would let a review proceed on a channel nobody understands."""
    with pytest.raises(channel.ChannelError):
        channel.read_phases(
            [{"seq": 1, "kind": "notice", "payload": {"phase": channel.CLAIM_MAP_PHASE}}]
        )


def test_findings_must_name_the_run_that_produced_them():
    with pytest.raises(channel.ChannelError) as refusal:
        channel.blind_findings_message(
            1, findings=[], run_record_id="  ", reader_digest="d", iteration=1,
            contract_public_items=["C-1"],
        )
    assert any("нельзя проверить изоляцию" in r for r in refusal.value.reasons)


def test_a_record_is_not_published_while_it_is_incomplete():
    from tests.test_audience_run_attestation import _blind

    with pytest.raises(channel.ChannelError) as refusal:
        channel.run_record_message(_blind(reader_digest=None))
    assert any("неполна" in r for r in refusal.value.reasons)


def test_the_published_record_carries_hashes_and_never_raw_values():
    from tests.test_audience_run_attestation import _blind

    payload = channel.run_record_message(_blind())["record"]
    assert payload["transcript_sha256"]
    assert set(payload) >= {"profile_digest", "reader_digest", "contract_sha256"}
    assert "prompt_text" not in payload and "answer" not in payload


def test_the_canary_answer_hash_is_the_hash_of_what_was_actually_said(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    outcome = driver.run(_inputs())
    stored = Path(outcome.canary_record.transcript_path).read_text(encoding="utf-8")
    assert outcome.canary_record.answer_sha256 == hashlib.sha256(
        stored.encode("utf-8")
    ).hexdigest()


# --- the structured-answer contract: validation, annulment, the one retry -------------------


def test_a_credited_round_writes_the_render_and_records_its_path_and_hash(tmp_path):
    """The order is validation → render → record → crediting: a credited run without a
    deliverable file is unrepresentable, because the gate demands the fields with the rest
    of the record."""
    driver, fake, journal, calls = _round(tmp_path)
    outcome = driver.run(_inputs())
    assert outcome.credited, outcome.credit_reasons
    record = outcome.blind_record
    assert record.render_path and record.render_sha256
    rendered = Path(record.render_path).read_bytes()
    assert hashlib.sha256(rendered).hexdigest() == record.render_sha256
    text = rendered.decode("utf-8")
    assert "## Пересказ" in text and "| S-1 | чисто |" in text
    assert record.report_schema_version and record.section_budgets


def test_findings_are_extracted_from_the_validated_object_with_derived_ids(tmp_path):
    from assistant_memory.audience.blind_report import finding_identity

    driver, fake, journal, calls = _round(
        tmp_path, answers=["Сохранённых сведений нет.", BLIND_WITH_FINDING]
    )
    outcome = driver.run(_inputs())
    assert outcome.credited, outcome.credit_reasons
    published = [
        m["payload"]
        for m in fake.messages
        if m["payload"].get("phase") == channel.BLIND_FINDINGS_PHASE
    ][-1]
    (item,) = published["findings"]
    assert item["section"] == "S-1" and item["contract_item"] == "C-1"
    assert item["id"] == finding_identity(
        "НЕПОНЯТНО", "S-1", "Слово «механизм» не объяснено.", item["contract_sha256"]
    )


def test_an_invalid_answer_annuls_the_run_and_one_retry_credits_with_a_link(tmp_path):
    """The retry clause verbatim: the annulled run's record carries «не состоялся» with the
    reasons, the retry is a NEW run with its own canary pair, and its record NAMES the
    annulled one."""
    driver, fake, journal, calls = _round(
        tmp_path,
        answers=[
            "Сохранённых сведений нет.",
            "Это просто текст, а не JSON-объект.",
            "Сохранённых сведений нет.",
            BLIND_OK,
        ],
    )
    outcome = driver.run(_inputs())
    assert outcome.credited, outcome.credit_reasons
    blinds = [
        m["payload"]["record"]
        for m in fake.messages
        if m["payload"].get("phase") == channel.RUN_RECORD_PHASE
        and m["payload"]["record"]["kind"] == RunKind.BLIND.value
    ]
    assert len(blinds) == 2
    annulled, retried = blinds
    assert annulled["outcome"] == RunOutcome.NOT_STARTED.value
    assert "не прошёл валидацию схемы" in annulled["outcome_reason"]
    assert retried["outcome"] == RunOutcome.HAPPENED.value
    assert retried["annulled_record_id"] == annulled["id"]
    assert calls["n"] == 4  # two canaries, two readings
    assert journal.next_number() == 5


def test_a_second_validation_refusal_in_a_row_goes_to_the_operator(tmp_path):
    """A model that cannot hold the schema is a property of the instrument — the operator
    must learn of it, not pay for a loop."""
    driver, fake, journal, calls = _round(
        tmp_path,
        answers=[
            "Сохранённых сведений нет.",
            "Не JSON.",
            "Сохранённых сведений нет.",
            '{"пересказ": "и всё"}',
        ],
    )
    outcome = driver.run(_inputs())
    assert not outcome.credited
    assert outcome.needs_operator
    assert any("второй отказ валидации подряд" in r for r in outcome.credit_reasons)
    blinds = [
        m["payload"]["record"]
        for m in fake.messages
        if m["payload"].get("phase") == channel.RUN_RECORD_PHASE
        and m["payload"]["record"]["kind"] == RunKind.BLIND.value
    ]
    assert [b["outcome"] for b in blinds] == [RunOutcome.NOT_STARTED.value] * 2


def test_the_blind_prompt_carries_the_derived_schema_block(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    driver.run(_inputs())
    _, blind_prompt = calls["prompts"]
    assert "РОВНО ОДИН JSON-объект" in blind_prompt
    assert '"пересказ"' in blind_prompt and '"что_ещё_заметил"' in blind_prompt
    # flags are off, so the conditional sections are not offered to the reader
    assert "передать_наверх" not in blind_prompt


def test_a_second_version_gets_the_retelling_diff_signal(tmp_path):
    """AG-10: the schema made same-named sections comparable, and the mechanical layer
    diffs the synthesis fields of two credited reports — into the phase re-post and into
    the render the operator opens. Informational: it blocks nothing at any value."""
    driver, fake, journal, calls = _round(tmp_path)
    first = driver.run(_inputs())
    assert first.credited

    moved = BLIND_OK.replace(
        "незнающий ловит непонятное", "незнающий ловит непонятное мгновенно"
    )
    again, fake2, journal2, calls2 = _round(
        tmp_path,
        answers=["Сохранённых сведений нет.", moved],
        records=[first.blind_record],
        credited=[first.blind_record.id],
    )
    outcome = again.run(
        _inputs(artifact_seq=11, iteration=2, artifact_text=ARTIFACT + "\nДописано.\n")
    )
    assert outcome.credited, outcome.credit_reasons

    diffed = [
        m["payload"]
        for m in fake2.messages
        if m["payload"].get("phase") == channel.MECH_REPORT_PHASE
        and "retelling_diff" in m["payload"]
    ]
    assert diffed, "дифф пересказа не опубликован в фазе механического слоя"
    diff = diffed[-1]["retelling_diff"]["пересказ"]
    # the edit replaced the final word-with-period by two words: one out, two in
    assert diff["добавлено_слов"] == 2 and diff["убрано_слов"] == 1
    assert diffed[-1]["blocks_convergence"] is False
    render = Path(outcome.blind_record.render_path).read_text(encoding="utf-8")
    assert "Дифф синтеза с прошлой версии" in render


def test_the_first_version_has_no_diff_and_says_nothing(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    outcome = driver.run(_inputs())
    assert outcome.credited
    assert not [
        m for m in fake.messages if "retelling_diff" in m["payload"]
    ]
    render = Path(outcome.blind_record.render_path).read_text(encoding="utf-8")
    assert "Дифф синтеза" not in render


# --- the v2 role passes ---------------------------------------------------------------------

from assistant_memory.audience.role_prompts import MACHINE_SLOTS, STRATEGIC_SLOTS  # noqa: E402

CONFIG_ROLES = {
    "genre": "audience", "cold_verdict_first": False,
    "strategic_reader": True, "machine_comb": True,
}

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

STRATEGIC_OK = (
    '{"итог": "Вынос достигается: текст ведёт читателя к мысли, что всё под контролем.", '
    '"находки": []}'
)
MACHINE_OK = '{"находки": []}'


def _role_inputs(**over):
    return _inputs(
        strategic_template=STRATEGIC_TEMPLATE, machine_template=MACHINE_TEMPLATE, **over
    )


def test_a_roles_on_round_runs_all_four_passes_and_credits_them(tmp_path):
    driver, fake, journal, calls = _round(
        tmp_path,
        config=CONFIG_ROLES,
        answers=["Сохранённых сведений нет.", BLIND_OK, STRATEGIC_OK, MACHINE_OK],
    )
    outcome = driver.run(_role_inputs())
    assert outcome.credited, outcome.credit_reasons
    assert calls["n"] == 4  # canary, blind, strategic, machine
    strategic = outcome.role_results[RunKind.STRATEGIC.value]
    machine = outcome.role_results[RunKind.MACHINE.value]
    assert strategic.credited, strategic.reasons
    assert machine.credited, machine.reasons
    assert Path(strategic.record.render_path).exists()
    assert Path(machine.record.render_path).exists()
    phases = fake.phases()
    assert channel.STRATEGIC_FINDINGS_PHASE in phases
    assert channel.MACHINE_FINDINGS_PHASE in phases
    strategic_payload = [
        m["payload"] for m in fake.messages
        if m["payload"].get("phase") == channel.STRATEGIC_FINDINGS_PHASE
    ][-1]
    assert strategic_payload["role_digest"] == strategic.record.role_digest
    assert "C-9" in strategic_payload["contract_items"]  # both parts declared


def test_an_unmoved_role_fingerprint_transfers_the_result_without_a_run(tmp_path):
    driver, fake, journal, calls = _round(
        tmp_path,
        config=CONFIG_ROLES,
        answers=["Сохранённых сведений нет.", BLIND_OK, STRATEGIC_OK, MACHINE_OK],
    )
    first = driver.run(_role_inputs())
    assert first.credited

    again, fake2, journal2, calls2 = _round(
        tmp_path,
        config=CONFIG_ROLES,
        records=[
            first.blind_record,
            first.role_results[RunKind.STRATEGIC.value].record,
            first.role_results[RunKind.MACHINE.value].record,
        ],
        credited=[first.blind_record.id],
    )
    fake2.messages = list(fake.messages)
    outcome = again.run(_role_inputs(artifact_seq=11, iteration=2))
    assert calls2["n"] == 0  # nothing was spent: the blind carried, the roles transferred
    strategic = outcome.role_results[RunKind.STRATEGIC.value]
    assert strategic.credited and strategic.carried_from_iteration == 1
    carried = [
        m["payload"] for m in fake2.messages
        if m["payload"].get("phase") == channel.STRATEGIC_FINDINGS_PHASE
    ][-1]
    assert carried["carried_from_iteration"] == 1


def test_a_twice_invalid_role_answer_goes_to_the_operator_with_linked_records(tmp_path):
    driver, fake, journal, calls = _round(
        tmp_path,
        config=CONFIG_ROLES,
        answers=[
            "Сохранённых сведений нет.", BLIND_OK,
            "Это не JSON.", '{"итог": "и всё"}',  # two strategic refusals in a row
            MACHINE_OK,
        ],
    )
    outcome = driver.run(_role_inputs())
    assert outcome.credited  # the blind half stands
    strategic = outcome.role_results[RunKind.STRATEGIC.value]
    assert strategic.needs_operator and outcome.needs_operator
    records = [
        m["payload"]["record"] for m in fake.messages
        if m["payload"].get("phase") == channel.RUN_RECORD_PHASE
        and m["payload"]["record"]["kind"] == RunKind.STRATEGIC.value
    ]
    assert [r["outcome"] for r in records] == [RunOutcome.NOT_STARTED.value] * 2
    assert records[1]["annulled_record_id"] == records[0]["id"]
    # the machine pass still ran and credited — one role's failure hides nothing
    assert outcome.role_results[RunKind.MACHINE.value].credited


def test_an_enabled_strategic_role_without_a_takeaway_refuses_the_round(tmp_path):
    from assistant_memory.audience.contract import PrivateContract, PrivateField, PrivateFlag

    driver, fake, journal, calls = _round(tmp_path, config=CONFIG_ROLES)
    contract = _contract()
    no_takeaway = AudienceContract(
        public=contract.public,
        private=PrivateContract(
            version=1,
            items=(ContractItem("C-9", PrivateField.DELIVERY, "Метафоры разрешены."),),
            flags={PrivateFlag.GOAL_INCLUDES_AUDIENCE_REQUEST: False},
        ),
    )
    with pytest.raises(RoundRefused) as refusal:
        driver.run(_role_inputs(contract=no_takeaway))
    assert any("отказ конфигурации" in r for r in refusal.value.reasons)
    assert calls["n"] == 0


def test_the_convergence_gate_holds_the_enabled_roles_and_frees_the_disabled_ones(tmp_path):
    from datetime import datetime

    from assistant_memory.audience import gate

    driver, fake, journal, calls = _round(
        tmp_path,
        config=CONFIG_ROLES,
        answers=["Сохранённых сведений нет.", BLIND_OK, STRATEGIC_OK, MACHINE_OK],
    )
    driver.run(_role_inputs())
    phases = channel.read_phases(fake.messages)
    created_at = {
        m["seq"]: datetime.fromisoformat(m["created_at"]) for m in fake.messages
    }
    on = gate.convergence_refusals(
        phases, artifact_seq=10, created_at=created_at,
        roles={"strategic_reader": True, "machine_comb": True},
    )
    # the role conditions are satisfied (clean passes, nothing to dispose); the remaining
    # refusals are the round's other conditions (claim map, operator declarations)
    assert not any("strategic_findings" in r or "machine_findings" in r for r in on)
    off = gate.convergence_refusals(
        phases, artifact_seq=10, created_at=created_at,
        roles={"strategic_reader": False, "machine_comb": False},
    )
    assert not any("роль" in r for r in off)
    undeclared = gate.convergence_refusals(
        phases, artifact_seq=10, created_at=created_at, roles=None,
    )
    assert any("включённость ролей" in r for r in undeclared)


# --- AG-30: the environment probe gates every transfer --------------------------------------


def test_a_changed_runner_version_forces_fresh_runs_instead_of_transfers(tmp_path):
    """The operator's layer choice: fingerprints answer "did the role's inputs change",
    the probe answers "is the environment still the proven one" — a CLI updated in place
    moves no fingerprint, so the probe is what forces the re-measure."""
    driver, fake, journal, calls = _round(
        tmp_path,
        config=CONFIG_ROLES,
        answers=["Сохранённых сведений нет.", BLIND_OK, STRATEGIC_OK, MACHINE_OK],
    )
    first = driver.run(_role_inputs())
    assert first.credited

    again, fake2, journal2, calls2 = _round(
        tmp_path,
        config=CONFIG_ROLES,
        answers=["Сохранённых сведений нет.", BLIND_OK, STRATEGIC_OK, MACHINE_OK],
        records=[
            first.blind_record,
            first.role_results[RunKind.STRATEGIC.value].record,
            first.role_results[RunKind.MACHINE.value].record,
        ],
        credited=[first.blind_record.id],
    )
    fake2.messages = list(fake.messages)
    outcome = again.run(
        _role_inputs(artifact_seq=11, iteration=2, live_runner_version="0.146.0")
    )
    assert outcome.blind_action is BlindAction.RUN
    assert any("версия запускающего изменилась" in r for r in outcome.reasons)
    assert outcome.credited, outcome.credit_reasons
    assert calls2["n"] == 4  # everything re-measured: canary, blind, both roles
    assert outcome.role_results[RunKind.STRATEGIC.value].carried_from_iteration is None


def test_an_unprobed_environment_also_refuses_transfers(tmp_path):
    driver, fake, journal, calls = _round(tmp_path)
    first = driver.run(_inputs())
    again, fake2, journal2, calls2 = _round(
        tmp_path, records=[first.blind_record], credited=[first.blind_record.id]
    )
    fake2.messages = list(fake.messages)
    outcome = again.run(
        _inputs(artifact_seq=11, iteration=2, live_runner_version=None)
    )
    assert outcome.blind_action is BlindAction.RUN
    assert any("не установлена" in r for r in outcome.reasons)
