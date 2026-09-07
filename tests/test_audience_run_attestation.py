# SPDX-License-Identifier: Apache-2.0
"""Step 1 of the audience genre: profile, canary and run record bind, or nothing counts.

The tests are organised around the failure the binding exists to prevent — a clean canary
in one launch silently counting as isolation for a different reading — so most of them
assert that a plausible-looking pair is REFUSED, and say in the assertion which reason.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from assistant_memory.audience import (
    LaunchJournal,
    LaunchProfile,
    RunKind,
    RunOutcome,
    RunRecord,
    credit_blind_reading,
)
from assistant_memory.audience.journal import JournalLocked
from assistant_memory.audience.launcher import (
    BlindLauncher,
    canary_verdict,
    extract_runner_version,
    extract_spend,
    extract_tool_calls,
    header_violations,
    parse_header,
)
from assistant_memory.audience.records import (
    SPEND_UNMEASURED,
    carried_forward,
    unique_canary_violations,
)

REPO = Path(__file__).resolve().parents[1]

# The audience pilot's records are the author's working material and do not travel with
# the distribution (operator ruling 2026-09-03). The tests that read them say so instead
# of failing in every clone; everything that does not need them keeps running.
PILOT_RECORDS = pytest.mark.skipif(
    not (REPO / "docs" / "pilots" / "audience_review_2026-08").is_dir(),
    reason="the audience pilot's records are not part of this distribution",
)

T0 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


def _settings(tmp_path, *, auth: str = "token-a", model: str = "gpt-x"):
    directory = tmp_path / "codex_blind"
    directory.mkdir(exist_ok=True)
    (directory / "auth.json").write_text(auth, encoding="utf-8")
    return LaunchProfile(settings_dir=directory, model=model)


# --- profile: fingerprint by CONTENTS, never by path -------------------------


def test_fingerprint_follows_contents_not_path(tmp_path):
    profile = _settings(tmp_path)
    before = profile.fingerprint()

    # the path is untouched; only what is inside it changes — this is exactly the case a
    # path hash could not see (swapped settings, or re-authorisation onto another account)
    (profile.settings_dir / "auth.json").write_text("token-b", encoding="utf-8")
    after = profile.fingerprint()

    assert before.digest != after.digest
    assert profile.settings_dir == profile.settings_dir
    assert any("auth.json" in reason for reason in after.differs_from(before))


def test_fingerprint_covers_model_and_extensions(tmp_path):
    base = _settings(tmp_path)
    other_model = LaunchProfile(settings_dir=base.settings_dir, model="gpt-y")
    with_ext = LaunchProfile(
        settings_dir=base.settings_dir, model=base.model, extensions=("mail",)
    )

    assert base.fingerprint().digest != other_model.fingerprint().digest
    assert base.fingerprint().digest != with_ext.fingerprint().digest
    assert "модель" in " ".join(other_model.fingerprint().differs_from(base.fingerprint()))


def test_a_run_writing_its_own_state_does_not_break_the_binding(tmp_path):
    """The canary writes its session and touches the state db BEFORE the reading runs.

    A fingerprint over everything would therefore differ between the canary and the reading
    it is meant to bind to, and every honest pair would be refused — a check that always
    refuses teaches people to skip it. Found by inventorying the real profile, not by
    reasoning: codex populates a fresh settings directory itself within seconds.
    """
    profile = _settings(tmp_path)
    before = profile.fingerprint()

    # measured on the real profile: ONE run rewrites 5266 files in a temp directory inside
    # it, refreshes 54 system-skill files, 7 plugin-catalogue files and the model cache
    for relative in (
        "sessions/2026/08/rollout-1.jsonl", ".tmp/a/b/c.bin", "skills/.system/x/SKILL.md",
        "plugins/cache/remote/y.json", "models_cache.json", "state_5.sqlite",
    ):
        target = profile.settings_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("мусор инструмента", encoding="utf-8")

    assert profile.fingerprint().digest == before.digest
    # and what WAS compared travels with the fingerprint: "they match" means little without it
    assert "auth.json" in profile.fingerprint().covered


def test_a_profile_with_no_configuration_at_all_is_refused(tmp_path):
    """An empty fingerprint would equal every other empty one — silently binding strangers."""
    empty = tmp_path / "hollow"
    empty.mkdir()
    (empty / "models_cache.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="файла конфигурации"):
        LaunchProfile(settings_dir=empty, model="gpt-x").fingerprint()


def test_configuration_changes_still_break_the_binding(tmp_path):
    """The exclusion must not swallow what it exists to catch."""
    profile = _settings(tmp_path)
    before = profile.fingerprint()
    (profile.settings_dir / "auth.json").write_text("другая учётка", encoding="utf-8")
    assert profile.fingerprint().digest != before.digest

    # and the settings file too — but NOT a plugin appearing, which is the stated cost of
    # an allowlist and the operator's ruling: the canary asks the model, not the directory
    (profile.settings_dir / "config.toml").write_text("model = 'z'", encoding="utf-8")
    with_config = profile.fingerprint().digest
    plugins = profile.settings_dir / "plugins" / "gmail"
    plugins.mkdir(parents=True)
    (plugins / "plugin.json").write_text("{}", encoding="utf-8")
    assert profile.fingerprint().digest == with_config


def test_fingerprint_never_carries_raw_values(tmp_path):
    """The settings directory holds credentials; the fingerprint must leak none of them."""
    profile = _settings(tmp_path, auth="super-secret-token")
    printed = repr(profile.fingerprint())
    assert "super-secret-token" not in printed


def test_fingerprint_is_stable_and_order_independent(tmp_path):
    profile = _settings(tmp_path)
    (profile.settings_dir / "b.json").write_text("b", encoding="utf-8")
    (profile.settings_dir / "a.json").write_text("a", encoding="utf-8")
    assert profile.fingerprint().digest == profile.fingerprint().digest


def test_missing_settings_directory_is_loud(tmp_path):
    profile = LaunchProfile(settings_dir=tmp_path / "nope", model="gpt-x")
    with pytest.raises(FileNotFoundError):
        profile.fingerprint()


# --- journal: numbering, exclusivity, placement ------------------------------


def _journal(tmp_path, *, clock=None):
    ticks = iter(clock or [T0 + timedelta(minutes=i) for i in range(20)])
    return LaunchJournal(tmp_path / "launches" / "cold.jsonl", now=lambda: next(ticks))


def test_launch_numbers_are_monotonic_across_processes(tmp_path):
    journal = _journal(tmp_path)
    with journal.launch("digest") as first:
        pass
    with journal.launch("digest") as second:
        pass
    # a fresh object over the same file continues the sequence: the number lives on disk,
    # not in memory, or a restart would silently reset the very counter that detects gaps
    assert (first.number, second.number) == (1, 2)
    assert LaunchJournal(journal.path).next_number() == 3


def test_entry_is_written_when_the_run_starts_not_when_it_succeeds(tmp_path):
    """A crashed launch must still consume its number, or a crash becomes invisible."""
    journal = _journal(tmp_path)
    with pytest.raises(RuntimeError, match="прогон упал"):
        with journal.launch("digest"):
            raise RuntimeError("прогон упал")
    assert [entry.number for entry in journal.entries()] == [1]
    assert not journal.lock_path.exists()  # lock released even on the failure path


def test_second_launch_is_refused_while_the_first_holds_the_lock(tmp_path):
    journal = _journal(tmp_path)
    with journal.launch("digest"):
        with pytest.raises(JournalLocked) as excinfo:
            with LaunchJournal(journal.path).launch("digest"):
                pass
    # the refusal names the holder — an automatic steal would turn "another launch is
    # running" into "another launch was running, probably"
    assert "pid=" in str(excinfo.value)


@pytest.mark.parametrize("inside", ["cold.jsonl", "nested/cold.jsonl"])
def test_journal_inside_the_settings_directory_is_refused(tmp_path, inside):
    profile = _settings(tmp_path)
    journal = LaunchJournal(profile.settings_dir / inside)
    with pytest.raises(ValueError) as excinfo:
        journal.guard_placement(profile.settings_dir)
    message = str(excinfo.value)
    assert "отпечаток профиля" in message and "слепому читателю" in message


def test_launch_guards_placement_when_given_the_settings_dir(tmp_path):
    profile = _settings(tmp_path)
    journal = LaunchJournal(profile.settings_dir / "cold.jsonl")
    with pytest.raises(ValueError):
        with journal.launch("digest", settings_dir=profile.settings_dir):
            pass


# --- records: the closed schema per kind -------------------------------------


def _canary(**over):
    base = dict(
        id="canary-1", kind=RunKind.CANARY, iteration=3, artifact_seq=42,
        profile_digest="P", launch_number=7,
        started_at=T0, finished_at=T0 + timedelta(minutes=1),
        transcript_path="runs/canary-1.log",
        transcript_sha256="a255b7359b24d0a22ed4a763284dfc167be95b25a7beb20cc7167f256b8ab63f",
        tool_calls=(), model="gpt-x", model_version="2026-07", spend=1200,
        outcome=RunOutcome.HAPPENED, runner_version="0.145.0",
        sandbox_mode="read-only", approval_mode="never",
        canary_verdict_clean=True, markers_message_seq=10,
        answer_message_seq=11, answer_sha256="ans",
    )
    return RunRecord(**{**base, **over})


def _blind(**over):
    base = dict(
        id="blind-1", kind=RunKind.BLIND, iteration=3, artifact_seq=42,
        profile_digest="P", launch_number=8,
        started_at=T0 + timedelta(minutes=2), finished_at=T0 + timedelta(minutes=9),
        transcript_path="runs/blind-1.log",
        transcript_sha256="badcff29aff01b44fa9566d9deb6d582a14659e7381328589803a728d2d2503a",
        tool_calls=(), model="gpt-x", model_version="2026-07", spend=14376,
        outcome=RunOutcome.HAPPENED, runner_version="0.145.0",
        sandbox_mode="read-only", approval_mode="never",
        canary_record_id="canary-1", prompt_digest="pr", reader_digest="R",
        contract_version=4, contract_sha256="c4", category_set_version="cs1",
        report_schema_version="ss1", section_budgets='{"пол": 250}',
        render_path="runs/blind-1_report.md", render_sha256="r1",
    )
    return RunRecord(**{**base, **over})


#: A transcript reader for the fixtures: every fixture path holds the bytes whose sha256 is
#: the record's recorded hash. Injected exactly as production injects it — the check must be
#: PASSED, never defaulted, so that "not run" can never read as "passed".
_HEADER = "\n".join(
    [
        "OpenAI Codex v0.145.0",
        "--------",
        "workdir: C:/x",
        "model: gpt-x",
        "approval: never",
        "sandbox: read-only",
        "--------",
        "",
    ]
)
_TRANSCRIPTS = {
    "runs/canary-1.log": (_HEADER + "Сохранённых сведений нет.\n").encode(),
    "runs/blind-1.log": (_HEADER + "ЧТО Я ПОНЯЛ: ...\n").encode(),
}


def _read_transcript(path):
    return _TRANSCRIPTS.get(path)


def _credit(blind=None, canary=None, **over):
    blind = blind or _blind()
    kwargs = dict(
        current_reader_digest="R", markers_published_at=T0 - timedelta(minutes=1),
        observed_answer_sha256="ans", read_transcript=_read_transcript,
        # The universe is REQUIRED now: an omitted list is "the walk was not done", not
        # "no rivals". The minimum honest universe is the reading under examination.
        all_blind_records=[blind],
    )
    return credit_blind_reading(blind, canary or _canary(), **{**kwargs, **over})


@pytest.mark.parametrize(
    "kind,dropped",
    [
        (RunKind.CANARY, "canary_verdict_clean"),
        (RunKind.BLIND, "reader_digest"),
        (RunKind.VISUAL, "render_sha256"),
    ],
)
def test_each_kind_has_its_own_required_fields(kind, dropped):
    common = dict(
        id="x", kind=kind, iteration=1, artifact_seq=1, profile_digest="P",
        launch_number=1, started_at=T0, finished_at=T0,
        transcript_path="p", transcript_sha256="h", tool_calls=(),
        model="m", model_version="v", spend=SPEND_UNMEASURED, outcome=RunOutcome.HAPPENED,
        runner_version="0.145.0", sandbox_mode="read-only", approval_mode="never",
    )
    filled = {
        RunKind.CANARY: dict(canary_verdict_clean=True, markers_message_seq=1,
                             answer_message_seq=2, answer_sha256="a"),
        RunKind.BLIND: dict(canary_record_id="c", prompt_digest="p", reader_digest="r",
                            contract_version=1, contract_sha256="h", category_set_version="v",
                            report_schema_version="ss", section_budgets="{}",
                            render_path="rp", render_sha256="r"),
        RunKind.VISUAL: dict(source_version=1, source_sha256="s", render_sha256="r",
                             build_env_id="e"),
    }[kind]
    assert not RunRecord(**common, **filled).missing_fields()
    assert RunRecord(**common, **{**filled, dropped: None}).missing_fields() == [dropped]


def test_visual_record_is_complete_without_the_canary_fields():
    """The discriminator earns its keep here: a flat schema would reject a legitimate one."""
    visual = RunRecord(
        id="v", kind=RunKind.VISUAL, iteration=1, artifact_seq=1, profile_digest="P",
        launch_number=1, started_at=T0, finished_at=T0, transcript_path="p",
        transcript_sha256="h", tool_calls=(), model="m", model_version="v",
        runner_version="0.145.0", sandbox_mode="read-only", approval_mode="never",
        spend=SPEND_UNMEASURED, outcome=RunOutcome.HAPPENED,
        source_version=11, source_sha256="s", render_sha256="r", build_env_id="env",
    )
    assert visual.missing_fields() == []


# --- records: the creditability check ----------------------------------------


def test_a_properly_bound_pair_is_credited():
    assert _credit().credited


@pytest.mark.parametrize(
    "case,expected",
    [
        (dict(canary=_canary(canary_verdict_clean=False)), "вердикт канарейки не чист"),
        (dict(blind=_blind(tool_calls=("shell",))), "выполнял команды"),
        (dict(blind=_blind(outcome=RunOutcome.ANNULLED)), "исход слепого прогона"),
        (dict(blind=_blind(launch_number=9)), "не соседние"),
        (dict(blind=_blind(profile_digest="OTHER")), "отпечатки профиля"),
        (dict(blind=_blind(artifact_seq=43)), "разным версиям артефакта"),
        (dict(blind=_blind(canary_record_id="canary-9")), "ссылается не на эту"),
        (dict(current_reader_digest="OTHER"), "читательский отпечаток не совпадает"),
        (dict(observed_answer_sha256="different"), "не к тому ответу"),
        (dict(markers_published_at=T0 + timedelta(minutes=1)),
         "список маркеров не опубликован строго до запуска"),
        (dict(markers_published_at=None), "список маркеров не опубликован строго до запуска"),
        # A TIE is refused too: at equal timestamps the order of the two events is unproven,
        # and an unproven order is exactly what the pre-launch condition exists to refuse.
        (dict(markers_published_at=T0), "список маркеров не опубликован строго до запуска"),
        (dict(blind=_blind(started_at=T0 - timedelta(minutes=5))),
         "канарейка завершилась не раньше"),
        (dict(blind=_blind(started_at=T0 + timedelta(minutes=1))),
         "канарейка завершилась не раньше"),
    ],
)
def test_each_broken_binding_refuses_and_says_why(case, expected):
    verdict = _credit(**case)
    assert not verdict.credited
    assert any(expected in reason for reason in verdict.reasons), verdict.reasons


def test_one_canary_serves_exactly_one_reading():
    rival = _blind(id="blind-2")
    verdict = _credit(all_blind_records=[_blind(), rival])
    assert not verdict.credited
    assert any("одна канарейка" in reason for reason in verdict.reasons)


def test_every_reason_is_collected_not_just_the_first():
    """An operator re-running a pass wants the whole list, not one tripwire at a time."""
    verdict = _credit(
        blind=_blind(profile_digest="OTHER", launch_number=9, tool_calls=("shell",)),
        canary=_canary(canary_verdict_clean=False),
    )
    assert not verdict.credited
    assert len(verdict.reasons) >= 4


def test_bulk_uniqueness_check_finds_shared_canaries():
    records = [_canary(), _blind(), _blind(id="blind-2"), _blind(id="blind-3",
                                                                canary_record_id="canary-2")]
    assert unique_canary_violations(records) == {"canary-1": ["blind-1", "blind-2"]}


# --- carrying a previous reading forward --------------------------------------


def test_carry_forward_is_legitimate_while_the_reader_fingerprint_holds():
    verdict = carried_forward(
        _blind(), iteration=4, artifact_seq=50, current_reader_digest="R",
        live_runner_version="0.145.0",
    )
    assert verdict.credited


@pytest.mark.parametrize(
    "case,expected",
    [
        (dict(current_reader_digest="CHANGED"), "читательский отпечаток изменился"),
        (dict(artifact_seq=42), "перенос вперёд невозможен"),
        (dict(iteration=3), "перенос вперёд невозможен"),
        # AG-30: the environment is probed, never assumed — a diverged live runner
        # version forbids the transfer, and an unprobed one is not read as unchanged
        (dict(live_runner_version="0.146.0"), "версия запускающего изменилась"),
        (dict(live_runner_version=None), "не установлена"),
    ],
)
def test_carry_forward_refusals(case, expected):
    kwargs = dict(
        iteration=4, artifact_seq=50, current_reader_digest="R",
        live_runner_version="0.145.0",
    )
    verdict = carried_forward(_blind(), **{**kwargs, **case})
    assert not verdict.credited
    assert any(expected in reason for reason in verdict.reasons), verdict.reasons


def test_an_annulled_run_must_say_why():
    """«Аннулирован» with no reason is indistinguishable from «никто не потрудился объяснить»."""
    annulled = _blind(outcome=RunOutcome.ANNULLED)
    assert "outcome_reason" in annulled.missing_fields()
    explained = _blind(outcome=RunOutcome.ANNULLED, outcome_reason="грязная канарейка")
    assert "outcome_reason" not in explained.missing_fields()


def test_a_happened_run_needs_no_reason():
    assert "outcome_reason" not in _blind().missing_fields()


# --- the launcher: what it derives from a transcript --------------------------


#: The shape the provider really prints, copied from the pilot's transcript.
REAL_HEADER = (
    "OpenAI Codex v0.145.0\n--------\nworkdir: C:\\tmp\\cold_run\nmodel: gpt-5.6-terra\n"
    "provider: openai\napproval: never\nsandbox: read-only\nreasoning effort: high\n"
    "session id: 019fc95b\n--------\nuser\n"
)


@PILOT_RECORDS
def test_derivation_matches_the_real_pilot_transcript():
    """Golden check against the transcript of the live blind run kept in the repo.

    The header parse and the spend format were written FROM this file rather than guessed,
    and this test is what keeps them tied to it: the provider's two-line `tokens used` /
    `14,376` shape is exactly the kind of detail an invented regex gets subtly wrong.
    """
    transcript = Path(
        "docs/pilots/audience_review_2026-08/cold_round4_run.log"
    ).read_text(encoding="utf-8")
    header = parse_header(transcript)
    assert header["sandbox"] == "read-only"
    assert header["approval"] == "never"
    assert header["model"] == "gpt-5.6-terra"
    assert header_violations(header) == []
    assert extract_spend(transcript) == 14376
    assert extract_tool_calls(transcript) == ()


@pytest.mark.parametrize(
    "header,expected",
    [
        ({"sandbox": "read-only", "approval": "never"}, []),
        ({"sandbox": "workspace-write", "approval": "never"}, ["sandbox"]),
        ({"sandbox": "read-only", "approval": "on-request"}, ["approval"]),
        ({"sandbox": "read-only"}, ["approval"]),
        ({}, ["sandbox", "approval"]),
    ],
)
def test_header_is_the_primary_isolation_evidence(header, expected):
    """A missing field is a violation, not a pass: unstated mode is unconfirmed mode."""
    problems = header_violations(header)
    assert len(problems) == len(expected)
    for key in expected:
        assert any(key in problem for problem in problems)


def test_launcher_records_a_clean_run(tmp_path):
    profile = _settings(tmp_path)
    journal = _journal(tmp_path)
    launcher = BlindLauncher(
        profile=profile, journal=journal,
        invoke=lambda prompt: REAL_HEADER + "я прочитал текст\ntokens used\n14,376\n",
        transcripts_dir=tmp_path / "runs",
        now=lambda: T0 + timedelta(minutes=5), new_id=lambda: "run-1",
    )
    record = launcher.run(
        "промпт", kind=RunKind.BLIND, iteration=3, artifact_seq=42, model_version="2026-07",
        canary_record_id="c", prompt_digest="p", reader_digest="R",
        contract_version=1, contract_sha256="h", category_set_version="v",
        report_schema_version="ss", section_budgets="{}",
    )
    assert record.outcome is RunOutcome.HAPPENED
    assert record.spend == 14376
    assert record.tool_calls == ()
    assert record.launch_number == 1
    # the transcript is STORED whole — header included, because the header IS the evidence
    stored = Path(record.transcript_path).read_text(encoding="utf-8")
    assert "sandbox: read-only" in stored and "я прочитал текст" in stored
    # what the launcher's record still owes is exactly the render pair: it is built AFTER
    # validation by the driver, and the record stays incomplete until it exists
    assert record.missing_fields() == ["render_path", "render_sha256"]


def test_a_launch_whose_header_is_not_read_only_is_annulled(tmp_path):
    """The provider says what the launch was; if it says the wrong thing, nothing else matters."""
    launcher = BlindLauncher(
        profile=_settings(tmp_path), journal=_journal(tmp_path),
        invoke=lambda prompt: REAL_HEADER.replace("read-only", "workspace-write") + "текст",
        transcripts_dir=tmp_path / "runs", now=lambda: T0, new_id=lambda: "run-5",
    )
    record = launcher.run(
        "промпт", kind=RunKind.CANARY, iteration=1, artifact_seq=1, model_version="v",
        canary_verdict_clean=True, markers_message_seq=1,
        answer_message_seq=2, answer_sha256="a",
    )
    assert record.outcome is RunOutcome.ANNULLED
    assert "workspace-write" in record.outcome_reason


def test_a_transcript_without_a_header_is_annulled(tmp_path):
    """Unstated launch mode is unconfirmed launch mode — silence never counts as read-only."""
    launcher = BlindLauncher(
        profile=_settings(tmp_path), journal=_journal(tmp_path),
        invoke=lambda prompt: "просто текст без шапки",
        transcripts_dir=tmp_path / "runs", now=lambda: T0, new_id=lambda: "run-6",
    )
    record = launcher.run(
        "промпт", kind=RunKind.CANARY, iteration=1, artifact_seq=1, model_version="v",
        canary_verdict_clean=True, markers_message_seq=1,
        answer_message_seq=2, answer_sha256="a",
    )
    assert record.outcome is RunOutcome.ANNULLED
    assert "режим запуска не подтверждён" in record.outcome_reason


def test_executed_commands_annul_the_run(tmp_path):
    """Belt-and-braces over the header: a tool call in the transcript annuls too."""
    launcher = BlindLauncher(
        profile=_settings(tmp_path), journal=_journal(tmp_path),
        invoke=lambda prompt: REAL_HEADER + "exec ls /repo\nи дальше текст",
        transcripts_dir=tmp_path / "runs", now=lambda: T0, new_id=lambda: "run-2",
    )
    record = launcher.run(
        "промпт", kind=RunKind.BLIND, iteration=1, artifact_seq=1, model_version="v",
        canary_record_id="c", prompt_digest="p", reader_digest="R",
        contract_version=1, contract_sha256="h", category_set_version="v",
    )
    assert record.outcome is RunOutcome.ANNULLED
    assert "выполненные вызовы инструментов" in record.outcome_reason


def test_a_failed_invocation_is_a_recorded_run_not_a_gap(tmp_path):
    """A launch that never answered must still consume its number and leave a record."""
    journal = _journal(tmp_path)

    def boom(prompt):
        raise RuntimeError("квота кончилась")

    launcher = BlindLauncher(
        profile=_settings(tmp_path), journal=journal, invoke=boom,
        transcripts_dir=tmp_path / "runs", now=lambda: T0, new_id=lambda: "run-3",
    )
    record = launcher.run(
        "промпт", kind=RunKind.CANARY, iteration=1, artifact_seq=1, model_version="v",
        canary_verdict_clean=False, markers_message_seq=1,
        answer_message_seq=2, answer_sha256="a",
    )
    assert record.outcome is RunOutcome.ANNULLED
    assert "квота кончилась" in record.outcome_reason
    assert [e.number for e in journal.entries()] == [1]


def test_spend_says_not_measured_rather_than_guessing(tmp_path):
    launcher = BlindLauncher(
        profile=_settings(tmp_path), journal=_journal(tmp_path),
        invoke=lambda prompt: "ответ без строки о расходе",
        transcripts_dir=tmp_path / "runs", now=lambda: T0, new_id=lambda: "run-4",
    )
    record = launcher.run(
        "промпт", kind=RunKind.CANARY, iteration=1, artifact_seq=1, model_version="v",
        canary_verdict_clean=True, markers_message_seq=1,
        answer_message_seq=2, answer_sha256="a",
    )
    assert record.spend == SPEND_UNMEASURED


@pytest.mark.parametrize(
    "answer,clean,hit",
    [
        ("сохранённых сведений у меня нет", True, []),
        ("помню файлы про assistant_memory и колоду", False, ["assistant_memory"]),
    ],
)
def test_canary_verdict_over_the_declared_markers(answer, clean, hit):
    verdict, hits = canary_verdict(answer, ["assistant_memory", "аудиторное ревью"])
    assert (verdict, hits) == (clean, hit)


def test_an_annulled_run_is_never_carried_forward():
    verdict = carried_forward(
        _blind(outcome=RunOutcome.ANNULLED), iteration=4, artifact_seq=50,
        current_reader_digest="R",
    )
    assert not verdict.credited
    assert any("незасчитанный" in reason for reason in verdict.reasons)



# --- the instrument must be the same in both runs, and the transcript must back them up ---
#
# Critic finding `runner-version-not-bound` (review 163a5ef0) plus the two siblings the class
# sweep turned up: the record ASSERTED that runs on different runners are not comparable and
# nothing checked it; the spec says a record pointing at an unavailable transcript is not
# credited and nothing checked that either.


def test_a_launched_record_without_a_runner_version_is_incomplete():
    assert "runner_version" in _blind(runner_version=None).missing_fields()
    assert "runner_version" in _canary(runner_version=None).missing_fields()


def test_a_refused_assembly_still_needs_no_runner_version():
    """A run that never launched has no runner by definition — the relaxation stays."""
    record = _blind(
        outcome=RunOutcome.NOT_STARTED, outcome_reason="отказ сборки", runner_version=None
    )
    assert record.missing_fields() == []


def test_a_pair_on_different_runners_is_not_credited():
    verdict = _credit(blind=_blind(runner_version="0.146.0"))
    assert not verdict.credited
    assert any("разных версиях запускающего" in r for r in verdict.reasons)


def test_a_pair_on_different_model_versions_is_not_credited():
    verdict = _credit(blind=_blind(model_version="2026-08"))
    assert not verdict.credited
    assert any("разных версиях модели" in r for r in verdict.reasons)


@pytest.mark.parametrize("blank", ["", "   "])
def test_two_blank_model_versions_do_not_count_as_the_same_instrument(blank):
    """Critic finding `empty-model-version-accepted`: the schema asked only `is None`, so a
    blank model version passed it and then SATISFIED the identity check by matching the other
    blank — an unidentified instrument credited as "the same one"."""
    verdict = _credit(blind=_blind(model_version=blank), canary=_canary(model_version=blank))
    assert not verdict.credited
    assert any("model_version" in r for r in verdict.reasons)


@pytest.mark.parametrize(
    "field", ["id", "profile_digest", "transcript_path", "transcript_sha256", "model",
              "model_version", "runner_version", "reader_digest", "prompt_digest"]
)
def test_a_blank_required_field_counts_as_missing(field):
    """The class, not the one field the finding named: every required string the gate only
    ever COMPARES could be blanked, and two blanks always agree. Checked once, at the schema."""
    assert field in _blind(**{field: "  "}).missing_fields()


def test_a_blank_model_version_is_refused_before_the_counter_moves(tmp_path):
    """Refused at the launch boundary, not at crediting: a number consumed by a run that can
    never be credited would push the next honest pair apart."""
    profile, journal = _settings(tmp_path), LaunchJournal(tmp_path / "journal.jsonl")
    launcher = BlindLauncher(
        profile=profile, journal=journal, invoke=lambda prompt: _HEADER + "ответ",
        transcripts_dir=tmp_path / "runs",
    )
    with pytest.raises(ValueError, match="версия модели пуста"):
        launcher.run(
            "промпт", kind=RunKind.CANARY, iteration=1, artifact_seq=1, model_version="  ",
            canary_verdict_clean=True, markers_message_seq=1, answer_message_seq=2,
            answer_sha256="a",
        )
    assert journal.next_number() == 1


def test_the_stored_transcript_is_byte_for_byte_what_the_record_hashed(tmp_path):
    """Found by the assembly, not by review: `write_text` translates newlines on Windows, so
    the bytes on disk were not the bytes the hash was taken over — and the gate re-reads the
    file and compares. Every honest pair on this machine would have failed crediting with "a
    different transcript lies at that path". Invisible while nothing wrote a transcript and
    read it back: the fixtures supply their own bytes with matching hashes."""
    launcher = BlindLauncher(
        profile=_settings(tmp_path),
        journal=LaunchJournal(tmp_path / "journal.jsonl"),
        invoke=lambda prompt: _HEADER + "строка\nвторая строка\n",
        transcripts_dir=tmp_path / "runs",
    )
    record = launcher.run(
        "промпт", kind=RunKind.CANARY, iteration=1, artifact_seq=1, model_version="v",
        canary_verdict_clean=True, markers_message_seq=1, answer_message_seq=2,
        answer_sha256="a",
    )
    on_disk = Path(record.transcript_path).read_bytes()
    assert hashlib.sha256(on_disk).hexdigest() == record.transcript_sha256


def test_an_unreadable_transcript_is_not_credited():
    verdict = _credit(read_transcript=lambda path: None)
    assert not verdict.credited
    assert any("недоступную стенограмму" in r for r in verdict.reasons)


def test_a_transcript_that_is_not_the_recorded_one_is_not_credited():
    verdict = _credit(read_transcript=lambda path: b"something else entirely")
    assert not verdict.credited
    assert any("лежит не та стенограмма" in r for r in verdict.reasons)


def test_an_unrun_transcript_check_is_a_refusal_not_a_pass():
    """"Not checked" must never be indistinguishable from "checked and fine"."""
    verdict = _credit(read_transcript=None)
    assert not verdict.credited
    assert any("не проверялась" in r for r in verdict.reasons)


def test_the_launch_number_is_read_under_the_lock(tmp_path):
    """Critic finding `launch-number-race`: read before the lock, two launchers could see the
    same "next" number and the second would append a duplicate after the first released —
    destroying the uniqueness the counter exists to prove. Asserted by consuming a number
    from INSIDE another launcher's critical section, which is what a racing process does."""
    journal = LaunchJournal(tmp_path / "launches.jsonl")
    other = LaunchJournal(tmp_path / "launches.jsonl")

    with journal.launch("P") as first:
        pass
    with journal.launch("P") as second:
        # a second launcher reads the journal while the first has already committed
        assert other.next_number() == 3
    assert (first.number, second.number) == (1, 2)
    numbers = [e.number for e in journal.entries()]
    assert numbers == sorted(set(numbers)) == [1, 2]


# --- the isolation claim rests on the provider's header, not on a derived list ------------
#
# Critic finding `tool-call-scan-incomplete`. Answered by MEASUREMENT rather than by widening
# a regex against shapes nobody had seen: a real executing run was captured and kept as
# evidence, and the fix moved the load-bearing check to the header the provider itself prints.

MEASURED_EXECUTING_RUN = (
    REPO / "docs" / "pilots" / "audience_review_2026-08" / "measured" / "executed_tool_call.log"
)


@PILOT_RECORDS
def test_the_measured_executing_run_is_recognised_as_executing():
    """Calibration against a REAL transcript of a run that executed commands."""
    text = MEASURED_EXECUTING_RUN.read_text(encoding="utf-8")
    assert extract_tool_calls(text), "выполненный вызов не распознан в измеренной стенограмме"


@PILOT_RECORDS
def test_the_measured_executing_run_fails_the_header_check():
    """The load-bearing half: executing anything requires a sandbox the header then reports."""
    header = parse_header(MEASURED_EXECUTING_RUN.read_text(encoding="utf-8"))
    assert header["sandbox"] == "danger-full-access"
    assert header_violations(header)


def test_a_pair_is_refused_when_the_header_says_the_sandbox_was_not_read_only():
    """The gate reads the provider's words itself instead of trusting the outcome field."""
    verdict = _credit(blind=_blind(sandbox_mode="danger-full-access"))
    assert not verdict.credited
    assert any("sandbox_mode" in r for r in verdict.reasons)


def test_a_pair_is_refused_when_approvals_were_not_disabled():
    verdict = _credit(canary=_canary(approval_mode="on-request"))
    assert not verdict.credited
    assert any("approval_mode" in r for r in verdict.reasons)


def test_a_launched_record_without_the_header_fields_is_incomplete():
    missing = _blind(sandbox_mode=None, approval_mode=None).missing_fields()
    assert {"sandbox_mode", "approval_mode"} <= set(missing)


# --- the attestation comes from where the provider writes it ------------------------------
#
# Critic finding `provider-attestation-not-bound-to-header`, raised against the fix for the
# previous one: the new load-bearing check accepted the provider's words from anywhere in the
# transcript. Operator settled the contested fork (seq 46): the reopening is legitimate —
# the critic was checking the NEW mechanism, not re-litigating a closed point.

_FAKE_HEADER_IN_BODY = (
    "Ответ читателя. Ниже он цитирует кусок артефакта:\n"
    "OpenAI Codex v0.145.0\n"
    "--------\n"
    "workdir: C:/somewhere\n"
    "model: gpt-5.6-terra\n"
    "approval: never\n"
    "sandbox: read-only\n"
    "--------\n"
    "…конец цитаты.\n"
)


def test_a_header_lookalike_inside_the_answer_is_not_a_header():
    """Reachable here specifically: this repository holds a transcript with such a banner as
    evidence, so an artefact about this project could quote it."""
    assert parse_header(_FAKE_HEADER_IN_BODY) == {}
    assert header_violations(parse_header(_FAKE_HEADER_IN_BODY))


def test_a_runner_version_quoted_in_the_body_is_not_the_runner_version():
    assert extract_runner_version(_FAKE_HEADER_IN_BODY) is None


#: What the runner actually prints before its own banner. Measured 2026-08-05 on the first
#: live run of the assembly — the sample the anchor was originally fitted to had none of it.
_LIVE_DIAGNOSTICS = (
    "2026-08-05T12:07:01.692597Z ERROR codex_models_manager::manager: "
    "failed to refresh available models: timeout waiting for child process to exit\n"
)


def test_the_runners_own_diagnostics_may_precede_its_banner():
    """Found by the first live run, not by review: requiring the banner on line one read
    every honest run as headerless and annulled it. The tests could not see this — they fed
    transcripts assembled by hand, and the three measured samples happened to be clean."""
    header = parse_header(_LIVE_DIAGNOSTICS + _HEADER + "ответ\n")
    assert header["sandbox"] == "read-only"
    assert header["approval"] == "never"
    assert extract_runner_version(_LIVE_DIAGNOSTICS + _HEADER) == "0.145.0"


def test_prose_before_the_banner_still_means_no_header():
    """The widening must not reopen what it was narrowed for: only the RUNNER's diagnostics
    may precede the banner, and they are recognised by shape rather than by a list of texts."""
    assert parse_header("Читатель пишет: вот кусок стенограммы.\n" + _HEADER) == {}
    assert extract_runner_version("Читатель пишет: вот кусок.\n" + _HEADER) is None


@PILOT_RECORDS
def test_the_real_transcripts_still_parse():
    """Three measured transcripts, all opening the same way — the anchor is a measurement."""
    for name in (
        "cold_round4_run.log",
        "measured/executed_tool_call.log",
        "step1_live_check/blind_fc791294186c.log",
    ):
        path = REPO / "docs" / "pilots" / "audience_review_2026-08" / name
        text = path.read_text(encoding="utf-8")
        header = parse_header(text)
        assert header.get("sandbox"), f"шапка не разобрана: {name}"
        assert extract_runner_version(text) == "0.145.0", f"версия не прочитана: {name}"


# --- the gate re-derives from the verified transcript, it does not trust the record --------
#
# Critic finding `transcript-attestation-not-rederived`: the hash proves the file is the one
# the record names and nothing about whether the record's fields describe it — and the fields
# are written by the side being checked.

_HONEST = (
    "OpenAI Codex v0.145.0\n--------\nworkdir: C:/x\nmodel: gpt-x\n"
    "approval: never\nsandbox: read-only\n--------\nОтвет читателя.\n"
).encode()

_DIRTY_SANDBOX = (
    "OpenAI Codex v0.145.0\n--------\nworkdir: C:/x\nmodel: gpt-x\n"
    "approval: never\nsandbox: danger-full-access\n--------\nОтвет читателя.\n"
).encode()

_EXECUTED = (
    b"OpenAI Codex v0.145.0\n--------\nworkdir: C:/x\nmodel: gpt-x\n"
    b"approval: never\nsandbox: read-only\n--------\n"
    b"exec\n\"powershell.exe\" -Command 'git log' in C:/x\n succeeded in 40ms:\n"
)


def _with_transcript(blob: bytes):
    import hashlib as _h

    digest = _h.sha256(blob).hexdigest()
    blind = _blind(transcript_path="runs/x.log", transcript_sha256=digest)
    canary = _canary(transcript_path="runs/y.log", transcript_sha256=_h.sha256(_HONEST).hexdigest())
    reader = {"runs/x.log": blob, "runs/y.log": _HONEST}.get
    return _credit(blind=blind, canary=canary, read_transcript=reader)


def test_a_record_claiming_read_only_over_a_dirty_transcript_is_refused():
    """The exact case: the hash matches, the record says read-only, the transcript does not."""
    verdict = _with_transcript(_DIRTY_SANDBOX)
    assert not verdict.credited
    assert any("сама стенограмма говорит" in r for r in verdict.reasons)


def test_a_record_hiding_executed_calls_is_refused():
    verdict = _with_transcript(_EXECUTED)
    assert not verdict.credited
    assert any("скрывает" in r for r in verdict.reasons)


def test_a_record_that_matches_its_transcript_passes():
    assert _with_transcript(_HONEST).credited


def test_a_contradictory_header_is_not_an_attestation():
    """Critic finding `duplicate-provider-header-fields-accepted`: taking the last value
    silently, a header claiming both sandboxes parsed as clean."""
    contradictory = (
        "OpenAI Codex v0.145.0\n--------\nworkdir: C:/x\n"
        "sandbox: danger-full-access\nsandbox: read-only\napproval: never\n--------\n"
    )
    assert parse_header(contradictory) == {}
    assert header_violations(parse_header(contradictory))


def test_a_repeated_key_with_the_same_value_is_not_a_contradiction():
    benign = (
        "OpenAI Codex v0.145.0\n--------\nworkdir: C:/x\n"
        "sandbox: read-only\nsandbox: read-only\napproval: never\n--------\n"
    )
    assert parse_header(benign)["sandbox"] == "read-only"


def test_the_scan_is_a_coarse_check_and_says_so():
    """Operator decision 2026-08-04: absolute blindness is not the goal — the blind reader is
    a coarse approximation of one known human, so residual channels are CHECKED rather than
    proven absent. The code states its own strength; this test holds it to that."""
    from assistant_memory.audience import transcript as tr

    text = tr.__doc__ or ""
    source = (REPO / "src" / "assistant_memory" / "audience" / "transcript.py").read_text(
        encoding="utf-8"
    )
    assert "PRESENCE" in source and "never absence" in source
    assert "forbids writing, not executing" in source
    assert text


def test_an_omitted_record_universe_is_not_no_rivals():
    """Critic finding `canary-uniqueness-unchecked-on-omitted-universe`: the rule is checked
    by WALKING the channel's references, so an empty list means the walk was not done — and
    a caller who simply forgot the argument used to get `credited=True` on a pair the full
    list refuses."""
    verdict = _credit(all_blind_records=[])
    assert not verdict.credited
    assert any("пустой перебор" in r for r in verdict.reasons)


def test_a_rival_reading_on_the_same_canary_is_still_caught():
    blind = _blind()
    rival = _blind(id="blind-2", launch_number=9)
    verdict = _credit(blind=blind, all_blind_records=[blind, rival])
    assert not verdict.credited
    assert any("уже ссылается другое чтение" in r for r in verdict.reasons)
