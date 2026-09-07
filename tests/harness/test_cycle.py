# SPDX-License-Identifier: Apache-2.0
"""Running the cycle: gates → critic → immediate write → development.

The roles are imitated by real subprocesses (python scripts) — the cycle is run
whole, with no mocks of the subprocess mechanics: a layer nobody runs looks like it
works.
"""

import sys
import textwrap
import time

import pytest

from review_harness import cycle, machine_profile
from review_harness.errors import HarnessError
from review_harness.gates import Gate
from review_harness.machine_profile import MachineProfile
from review_harness.runcat import RunCatalog
from review_harness.subproc import run_watched


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A miniature repository: machine profile, operator-profile cache, run catalogue."""
    # The child python roles print Cyrillic into a pipe; without this the Windows
    # python takes cp1252 and dies — a test of the roles, not of the harness.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    root = tmp_path / "repo"
    cache = root / "docs" / "review" / "operator_profile.md"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        "# Operator profile (compiled)\nversion: test-v1\nnotify-within: 60s\n",
        encoding="utf-8",
    )
    return root


def _gate(run_dir, fork_answer="готовое"):
    """Gates: the entry one is confirmed, the forks get a prepared answer."""

    def ask(question):
        return "yes" if "Entry gate" in question else fork_answer

    return Gate(RunCatalog(run_dir), ask_fn=ask, say_fn=lambda _: None)


def _install_profile(repo, tmp_path, dev_body: str, critic_body: str | None = None):
    dev = tmp_path / "dev_agent.py"
    dev.write_text(textwrap.dedent(dev_body), encoding="utf-8")
    critic_body = critic_body or "print('проход: находок нет')"
    critic = tmp_path / "critic_agent.py"
    critic.write_text(textwrap.dedent(critic_body), encoding="utf-8")
    profile = MachineProfile(
        critic_argv=[sys.executable, str(critic), "{launch_note}", "{model}"],
        dev_argv=[sys.executable, str(dev), "{run_dir}", "{round}"],
        detection_window_sec=300,
        observed_max_gap_sec=1,
        observed_gap_note="тестовая среда",
        allow_unsafe_critic_sandbox=True,  # python roles, not codex
    )
    machine_profile.save(repo, profile)


def _create_run(repo):
    (repo / "x.py").write_text("print('артефакт')", encoding="utf-8")
    run_dir = repo / "docs" / "review" / "test_run"
    RunCatalog.create(
        run_dir,
        artifact_paths=["x.py"],
        critic_model="gpt-5.6-sol",
        description_text="# описание",
        launch_note_text="# запуск критика",
    )
    return run_dir


# Complete round metadata is a contract of write_pass: a pass without the
# snapshot of the judged version and the delivery version does not exist.
META = {
    "operator_profile_version": "test-v1",
    "git_head": "h",
    "artifact_snapshot": {
        "x.py": "h",
        "__task_description__": "h",
        "__launch_note__": "h",
    },
}


DEV_OK = """
    import pathlib, sys
    run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
    # Разработка обязана видеть уже записанный проход — немедленная запись.
    assert (run_dir / f"round_{rnd:02d}_pass.md").exists()
    (run_dir / f"round_{rnd:02d}_outcomes.md").write_text("исходы", encoding="utf-8")
"""


def test_full_round_writes_pass_then_outcomes(repo, tmp_path):
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)

    assert cycle.one_round(repo, run_dir, gate=_gate(run_dir)) == 1
    assert "находок нет" in (run_dir / "round_01_pass.md").read_text(encoding="utf-8")
    # The operator profile's version is recorded in the round's metadata.
    meta = (run_dir / "round_01_pass_meta.json").read_text(encoding="utf-8")
    assert "test-v1" in meta
    # Recoverability: the next round is visible from the files alone.
    assert RunCatalog(run_dir).restore().current_round == 2


def test_entry_gate_blocks_critic_until_confirmed(repo, tmp_path):
    # Finding 2 of round 1: a first pass without the operator confirming the
    # description would repeat the class of failure the gate was written for.
    marker = tmp_path / "critic_ran.marker"
    _install_profile(
        repo,
        tmp_path,
        DEV_OK,
        critic_body=f"""
        import pathlib
        pathlib.Path({str(marker)!r}).write_text("ran")
        print("проход")
        """,
    )
    run_dir = _create_run(repo)
    refusing = Gate(RunCatalog(run_dir), ask_fn=lambda _: "нет", say_fn=lambda _: None)

    with pytest.raises(HarnessError, match="gate"):
        cycle.one_round(repo, run_dir, gate=refusing)
    assert not marker.exists()  # the critic was never started
    # The operator's refusal is recorded verbatim in the gates.
    assert "нет" in (run_dir / "gates.md").read_text(encoding="utf-8")


def test_rerun_after_dev_crash_does_not_replay_critic(repo, tmp_path):
    # Finding 1 of round 1: a restart of an unfinished round must continue the
    # analysis from the recorded pass rather than replay the critic over it.
    marker = tmp_path / "critic_runs.txt"
    flag = tmp_path / "second_attempt.flag"
    _install_profile(
        repo,
        tmp_path,
        f"""
        import pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        flag = pathlib.Path({str(flag)!r})
        if not flag.exists():
            flag.write_text("x")  # первая попытка «обрывается» без исходов
        else:
            (run_dir / f"round_{{rnd:02d}}_outcomes.md").write_text("исходы", encoding="utf-8")
        """,
        critic_body=f"""
        import pathlib
        p = pathlib.Path({str(marker)!r})
        p.write_text(p.read_text() + "run\\n" if p.exists() else "run\\n")
        print("единственный проход критика")
        """,
    )
    run_dir = _create_run(repo)

    with pytest.raises(HarnessError, match="outcomes"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    saved = (run_dir / "round_01_pass.md").read_text(encoding="utf-8")

    assert cycle.one_round(repo, run_dir, gate=_gate(run_dir)) == 1
    # The critic ran exactly once; the pass was not overwritten.
    assert marker.read_text().count("run") == 1
    assert (run_dir / "round_01_pass.md").read_text(encoding="utf-8") == saved


def test_gate_requests_are_scoped_to_their_round(repo, tmp_path):
    # Finding 3 of round 1: the fork files are per round — round 1's answer cannot
    # be read by development as round 2's answer.
    _install_profile(
        repo,
        tmp_path,
        """
        import json, pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        answers = run_dir / f"round_{rnd:02d}_gate_answers.json"
        if not answers.exists():
            (run_dir / f"round_{rnd:02d}_gate_requests.json").write_text(json.dumps(
                [{"question": f"Развилка круга {rnd}?", "kind": "развилка"}]
            ), encoding="utf-8")
        else:
            got = json.loads(answers.read_text(encoding="utf-8"))
            (run_dir / f"round_{rnd:02d}_outcomes.md").write_text(
                "исходы с ответом: " + got[0]["answer"], encoding="utf-8")
        """,
    )
    run_dir = _create_run(repo)

    cycle.one_round(repo, run_dir, gate=_gate(run_dir, fork_answer="ответ-1"))
    cycle.one_round(repo, run_dir, gate=_gate(run_dir, fork_answer="ответ-2"))

    # Every round got ITS own answer; the previous round's did not carry over.
    r1 = (run_dir / "round_01_outcomes.md").read_text(encoding="utf-8")
    r2 = (run_dir / "round_02_outcomes.md").read_text(encoding="utf-8")
    assert "ответ-1" in r1 and "ответ-2" in r2 and "ответ-1" not in r2
    gates = (run_dir / "gates.md").read_text(encoding="utf-8")
    assert "Развилка круга 1?" in gates and "Развилка круга 2?" in gates


def test_dev_without_outcomes_fails_pointing_at_saved_pass(repo, tmp_path):
    _install_profile(repo, tmp_path, "pass\n")
    run_dir = _create_run(repo)

    with pytest.raises(HarnessError) as exc:
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    rendered = exc.value.render()
    assert "intact" in rendered and "What to do" in rendered
    assert (run_dir / "round_01_pass.md").exists()


def test_hung_development_is_recognized_dead(repo, tmp_path):
    # The harness has no role whose death goes unrecognised: hung development is
    # killed on the machine's window, and the recorded critic pass is intact.
    _install_profile(repo, tmp_path, "import time\ntime.sleep(60)\n")
    prof = machine_profile.load(repo)
    prof.detection_window_sec = 2
    prof.observed_max_gap_sec = 1
    machine_profile.save(repo, prof)
    run_dir = _create_run(repo)

    with pytest.raises(HarnessError, match="dead") as exc:
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    # Round 12: a real Windows path — the first kill is confirmed, and the
    # safety-net second kill does not declare it unconfirmed.
    assert "NOT confirmed" not in exc.value.render()
    assert (run_dir / "round_01_pass.md").exists()


def test_empty_critic_pass_is_a_loud_failure(repo, tmp_path):
    _install_profile(repo, tmp_path, "pass\n", critic_body="pass\n")
    run_dir = _create_run(repo)
    with pytest.raises(HarnessError, match="empty"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))


def test_pending_fork_is_served_before_development(repo, tmp_path):
    # Finding 3 of round 2: a fork left hanging by the previous session is
    # answered BEFORE development gets control again — otherwise it could drive on
    # unsettled semantics. Development here FAILS if it is started with no answers
    # while questions were hanging.
    _install_profile(
        repo,
        tmp_path,
        """
        import json, pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        answers = run_dir / f"round_{rnd:02d}_gate_answers.json"
        assert answers.exists(), "разработка получила управление до ответа оператора"
        got = json.loads(answers.read_text(encoding="utf-8"))
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text(
            "исходы: " + got[0]["answer"], encoding="utf-8")
        """,
    )
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)  # the previous session: there is a pass,
    (run_dir / "round_01_gate_requests.json").write_text(  # the fork is hanging.
        '[{"question": "Висящий вопрос?", "kind": "развилка"}]', encoding="utf-8"
    )

    cycle.one_round(repo, run_dir, gate=_gate(run_dir, fork_answer="решение"))
    assert "решение" in (run_dir / "round_01_outcomes.md").read_text(encoding="utf-8")


def test_answered_requests_are_not_asked_again(repo, tmp_path):
    # A crash strictly between writing the answers and deleting the questions: the
    # answers are durable — the operator is not asked an answered question again.
    _install_profile(
        repo,
        tmp_path,
        """
        import json, pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        got = json.loads((run_dir / f"round_{rnd:02d}_gate_answers.json")
                         .read_text(encoding="utf-8"))
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text(
            "исходы: " + got[0]["answer"], encoding="utf-8")
        """,
    )
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)
    (run_dir / "round_01_gate_requests.json").write_text(
        '[{"question": "Уже отвечено?", "kind": "развилка"}]', encoding="utf-8"
    )
    (run_dir / "round_01_gate_answers.json").write_text(
        '[{"question": "Уже отвечено?", "answer": "прежний ответ"}]', encoding="utf-8"
    )
    fork_calls = []

    def ask(question):
        if "Entry gate" in question:
            return "да"
        fork_calls.append(question)
        return "новый ответ"

    gate = Gate(RunCatalog(run_dir), ask_fn=ask, say_fn=lambda _: None)
    cycle.one_round(repo, run_dir, gate=gate)
    assert fork_calls == []  # the question was not repeated
    assert "прежний ответ" in (run_dir / "round_01_outcomes.md").read_text(
        encoding="utf-8"
    )


def test_profile_version_change_on_resume_is_recorded(repo, tmp_path):
    # Finding 8 of round 2: the profile changed between the pass and the
    # continuation — the catalogue must tell the truth about both halves.
    _install_profile(repo, tmp_path, "pass\n")
    run_dir = _create_run(repo)
    said = []
    gate = Gate(
        RunCatalog(run_dir),
        ask_fn=lambda q: "да",
        say_fn=said.append,
    )
    with pytest.raises(HarnessError):  # development with no outcomes — a crash
        cycle.one_round(repo, run_dir, gate=gate)

    cache = repo / "docs" / "review" / "operator_profile.md"
    cache.write_text(
        "# Operator profile (compiled)\nversion: test-v2\nnotify-within: 60s\n",
        encoding="utf-8",
    )
    with pytest.raises(HarnessError):
        cycle.one_round(repo, run_dir, gate=gate)
    meta = (run_dir / "round_01_pass_meta.json").read_text(encoding="utf-8")
    assert "test-v1" in meta and "test-v2" in meta


def test_round_100_is_not_a_ceiling(repo, tmp_path):
    # Finding 12 of round 2: a two-digit template must not become a ceiling.
    # The history is continuous (round 6), so we live through all hundred rounds.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    for n in range(1, 101):
        cat.write_pass(n, f"проход круга {n}", extra_meta=META)
        cat.write_outcomes(n, "исходы")
        cat.mark_round_completed(n)
    state = RunCatalog(run_dir).restore()
    assert 100 in state.rounds_with_pass
    assert state.current_round == 101


def test_missing_middle_round_breaks_restore(repo, tmp_path):
    # Round 6: a lost intermediate round is damage to the history of decisions,
    # not a reason to carry on quietly from the highest number.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    for n in (1, 2, 3):
        cat.write_pass(n, f"проход {n}", extra_meta=META)
        cat.write_outcomes(n, "исходы")
        cat.mark_round_completed(n)
    for suffix in ("pass.md", "pass_meta.json", "outcomes.md"):
        (run_dir / f"round_02_{suffix}").unlink()
    with pytest.raises(HarnessError, match="has holes"):
        RunCatalog(run_dir).restore()


def test_truncated_pass_breaks_restore(repo, tmp_path):
    # Round 6: a pass truncated to nothing is not "an existing pass".
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.write_pass(1, "проход", extra_meta=META)
    cat.write_outcomes(1, "исходы")
    cat.mark_round_completed(1)
    (run_dir / "round_01_pass.md").write_text("", encoding="utf-8")
    with pytest.raises(HarnessError, match="empty"):
        RunCatalog(run_dir).restore()


def test_entry_gate_accepts_expanded_affirmative(repo, tmp_path):
    # Round 6: the entry gate reads the operator's word with the same parser —
    # «Да, подтверждаю описание» does not turn into a non-confirmation.
    # The parser's boundary (cold review, 2026-09-07): consent with a tail is a
    # reservation, the word is asked again ONCE; a plain «да» then passes the gate.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    asked: list[str] = []

    def ask(question):
        asked.append(question)
        if "plain yes or no" in question:
            return "да"
        if "Entry gate" in question:
            return "Да, подтверждаю описание"
        return "готовое"

    gate = Gate(RunCatalog(run_dir), ask_fn=ask, say_fn=lambda _: None)
    assert cycle.one_round(repo, run_dir, gate=gate) == 1
    assert sum("plain yes or no" in q for q in asked) == 1


def test_unmarked_outcomes_do_not_complete_the_round(repo, tmp_path):
    # Round 14: non-empty outcomes from development WITHOUT the harness marker
    # mean the round is still running: a crash before the operator question was
    # served must not close the round and skip the gate. A restart finishes it.
    _install_profile(
        repo,
        tmp_path,
        """
        import json, pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        answers = run_dir / f"round_{rnd:02d}_gate_answers.json"
        out = "исходы"
        if answers.exists():
            got = json.loads(answers.read_text(encoding="utf-8"))
            out = "исходы с ответом: " + got[0]["answer"]
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text(out, encoding="utf-8")
        """,
    )
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)
    (run_dir / "round_01_outcomes.md").write_text("исходы до обрыва", encoding="utf-8")
    (run_dir / "round_01_gate_requests.json").write_text(
        '[{"question": "Недоставленный вопрос?", "kind": "развилка"}]',
        encoding="utf-8",
    )

    state = RunCatalog(run_dir).restore()
    assert state.resume_round == 1  # the round is NOT complete without the marker

    asked = []

    def ask(question):
        if "Entry gate" in question:
            return "да"
        asked.append(question)
        return "доставленный ответ"

    gate = Gate(RunCatalog(run_dir), ask_fn=ask, say_fn=lambda _: None)
    assert cycle.one_round(repo, run_dir, gate=gate) == 1
    assert any("Недоставленный вопрос" in q for q in asked)
    assert RunCatalog(run_dir).round_completed(1)
    assert "доставленный ответ" in (run_dir / "round_01_outcomes.md").read_text(
        encoding="utf-8"
    )


def test_entry_journal_is_repaired_from_state(repo, tmp_path):
    # Round 14: a journal write that failed after the flag is repaired from state.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("Да, подтверждаю", "Подтверждаешь описание?")
    assert not (run_dir / "gates.md").exists()
    cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    text = (run_dir / "gates.md").read_text(encoding="utf-8")
    assert "Да, подтверждаю" in text and "Подтверждаешь описание?" in text


def test_unlink_failure_is_contractual(tmp_path, monkeypatch):
    # Round 14 (debt 13): a deletion error is a refusal with a cause, not a traceback.
    from pathlib import Path as _P

    from review_harness.cycle import _unlink_contract

    target = tmp_path / "f.json"
    target.write_text("x", encoding="utf-8")

    def broken_unlink(self, missing_ok=False):
        raise PermissionError("нет прав")

    monkeypatch.setattr(_P, "unlink", broken_unlink)
    with pytest.raises(HarnessError, match="delete"):
        _unlink_contract(target, "файл")


def test_launcher_probe_timeout_is_contractual(tmp_path, monkeypatch):
    # Round 14 (debt 13): a hung probe is "not delivered", not a traceback.
    import subprocess as sp

    from review_harness import launcher

    if sys.platform != "win32":
        pytest.skip("cmd-путь")
    path = launcher.write_launcher(tmp_path / "l.cmd", ["echo x"], windows=True)

    def hang(*a, **kw):
        raise sp.TimeoutExpired(cmd="cmd", timeout=30)

    monkeypatch.setattr(launcher.subprocess, "run", hang)
    with pytest.raises(HarnessError, match="hung"):
        launcher.probe(path, windows=True)


def test_resume_failure_names_unconfirmed_kill(tmp_path, monkeypatch):
    # Round 14 (debt 13): a failure to resume plus an unconfirmed finish-off — both
    # facts are named in the refusal.
    if sys.platform != "win32":
        pytest.skip("windows-путь")
    from review_harness import subproc as sp_mod

    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setattr(sp_mod, "_resume_process", lambda pid: False)
    monkeypatch.setattr(sp_mod._ProcessTree, "kill", lambda self: False)
    with pytest.raises(HarnessError, match="NOT confirmed"):
        run_watched(
            [sys.executable, "-c", "print('x')"],
            role="роль",
            stream_path=tmp_path / "s.log",
            detection_window_sec=300,
        )


def test_partial_batch_is_still_pending_on_restore(repo, tmp_path):
    # Round 9: a partly answered batch is a hanging state, not a closed one.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)
    (run_dir / "round_01_gate_requests.json").write_text(
        '[{"question": "Первый?"}, {"question": "Второй?"}]', encoding="utf-8"
    )
    (run_dir / "round_01_gate_answers.json").write_text(
        '[{"question": "Первый?", "answer": "ответ"}]', encoding="utf-8"
    )
    state = RunCatalog(run_dir).restore()
    assert state.pending_gate_rounds == [1]


def test_profile_env_reaches_the_role(repo, tmp_path):
    # Round 9: the environment is part of the machine profile; the role gets it
    # rather than a random shell's environment.
    _install_profile(
        repo,
        tmp_path,
        """
        import os, pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text(
            "исходы env=" + os.environ.get("RH_TEST_ENV", "<нет>"), encoding="utf-8")
        """,
    )
    prof = machine_profile.load(repo)
    prof.env = {"RH_TEST_ENV": "из-профиля"}
    machine_profile.save(repo, prof)
    run_dir = _create_run(repo)
    cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    assert "из-профиля" in (run_dir / "round_01_outcomes.md").read_text(encoding="utf-8")


def test_partial_answer_batch_resumes_without_reasking(repo, tmp_path):
    # A finding of round 3: a crash on the second question of a batch must not
    # repeat the first — every answer is durable at once, and the rest is asked.
    _install_profile(
        repo,
        tmp_path,
        """
        import json, pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        got = json.loads((run_dir / f"round_{rnd:02d}_gate_answers.json")
                         .read_text(encoding="utf-8"))
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text(
            "исходы: " + "; ".join(a["answer"] for a in got), encoding="utf-8")
        """,
    )
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)
    (run_dir / "round_01_gate_requests.json").write_text(
        '[{"question": "Первый?", "kind": "развилка"},'
        ' {"question": "Второй?", "kind": "развилка"}]',
        encoding="utf-8",
    )
    # The previous session managed a durable write of the first answer.
    (run_dir / "round_01_gate_answers.json").write_text(
        '[{"question": "Первый?", "answer": "ответ-один"}]', encoding="utf-8"
    )
    asked = []

    def ask(question):
        if "Entry gate" in question:
            return "да"
        asked.append(question)
        return "ответ-два"

    gate = Gate(RunCatalog(run_dir), ask_fn=ask, say_fn=lambda _: None)
    cycle.one_round(repo, run_dir, gate=gate)
    assert len(asked) == 1 and "Второй?" in asked[0]  # the first was not repeated
    out = (run_dir / "round_01_outcomes.md").read_text(encoding="utf-8")
    assert "ответ-один" in out and "ответ-два" in out


def test_empty_outcomes_are_not_a_review(repo, tmp_path):
    # A finding of round 3: an empty outcomes file is an imitation of analysis.
    _install_profile(
        repo,
        tmp_path,
        """
        import pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text("", encoding="utf-8")
        """,
    )
    run_dir = _create_run(repo)
    with pytest.raises(HarnessError, match="empty|outcomes"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))


def test_missing_round_meta_on_resume_is_loud(repo, tmp_path):
    # A finding of round 3: continuing the analysis with no metadata loses the
    # identity of the judged version; a silent {} is forbidden.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)
    (run_dir / "round_01_pass_meta.json").unlink()
    with pytest.raises(HarnessError, match="metadata"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))


def test_privacy_is_enforced_on_every_write(repo, tmp_path):
    # A finding of round 3: the protection from git works beyond creation — a
    # catalogue that became tracked between rounds rejects the write.
    import subprocess

    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # There is no ignore rule — the directory became tracked.
    with pytest.raises(HarnessError, match="tracked"):
        RunCatalog(run_dir).write_pass(5, "поздний проход", extra_meta=META)
    (repo / ".gitignore").write_text("docs/review/\n", encoding="utf-8")
    RunCatalog(run_dir).write_pass(5, "поздний проход", extra_meta=META)  # allowed again


def test_privacy_checked_before_any_stream(repo, tmp_path):
    # Round 4, finding 2: if the catalogue stopped being ignored between
    # rounds, the refusal must happen BEFORE the stream — the critic's partial
    # output must not get the chance to land in a publishable tree.
    import subprocess

    marker = tmp_path / "critic_ran.marker"
    _install_profile(
        repo,
        tmp_path,
        DEV_OK,
        critic_body=f"""
        import pathlib
        pathlib.Path({str(marker)!r}).write_text("ran")
        print("приватный проход")
        """,
    )
    run_dir = _create_run(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)  # no ignore

    with pytest.raises(HarnessError, match="tracked"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    assert not marker.exists()  # the critic never started
    assert not (run_dir / "round_01_pass.partial").exists()  # there was no stream.


def test_stream_open_failure_actually_kills_the_process(tmp_path, monkeypatch):
    # Round 11: a refusal to open the stream must leave a DEAD process —
    # checked by the pulse, not by the exception alone.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    heartbeat = tmp_path / "hb.txt"
    role = tmp_path / "role.py"
    role.write_text(
        textwrap.dedent(
            f"""
            import pathlib, time
            p = pathlib.Path({str(heartbeat)!r})
            for i in range(120):
                p.write_text(str(i))
                time.sleep(0.25)
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessError, match="stream"):
        run_watched(
            [sys.executable, str(role)],
            role="роль",
            stream_path=tmp_path / "нет_каталога" / "s.log",
            detection_window_sec=300,
        )
    time.sleep(1.5)
    b1 = heartbeat.read_text() if heartbeat.exists() else ""
    time.sleep(1.5)
    b2 = heartbeat.read_text() if heartbeat.exists() else ""
    assert b1 == b2  # the process is dead, the pulse has stopped


def test_unconfirmed_kill_is_named_in_the_refusal(tmp_path, monkeypatch):
    # Round 11: when finishing off the tree was not confirmed, the refusal must
    # say so out loud instead of reporting "stopped".
    from review_harness import subproc as sp_mod

    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    monkeypatch.setattr(sp_mod._ProcessTree, "kill", lambda self: False)

    def failing_privacy():
        raise HarnessError("The catalogue is tracked", cause="тест", next_action="тест")

    with pytest.raises(HarnessError, match="NOT confirmed"):
        run_watched(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            role="роль",
            stream_path=tmp_path / "s.log",
            detection_window_sec=300,
            privacy_check=failing_privacy,
        )


def test_snapshot_distinguishes_name_content_boundary(repo, tmp_path):
    # Round 18: a file "a" holding "bc" and a file "ab" holding "c" are DIFFERENT
    # states; a rename with an edit of the beginning must not look unchanged.
    from review_harness.cycle import _judged_snapshot

    art = repo / "art"
    run_dir = _create_run(repo)
    art.mkdir()
    (art / "a").write_bytes(b"bc")
    snap1 = _judged_snapshot(repo, ["art"], run_dir)["art"]
    (art / "a").unlink()
    (art / "ab").write_bytes(b"c")
    snap2 = _judged_snapshot(repo, ["art"], run_dir)["art"]
    assert snap1 != snap2


def test_split_multibyte_char_is_not_corruption(tmp_path, monkeypatch):
    # Round 18: a Cyrillic character cut in half by a read boundary is valid
    # output: neither false raw evidence nor a refusal.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    script = tmp_path / "split.py"
    nl = chr(10)
    script.write_text(
        "import sys, time" + nl
        + "sys.stdout.buffer.write(bytes([0xD1]))" + nl
        + "sys.stdout.buffer.flush()" + nl
        + "time.sleep(1.5)" + nl
        + "sys.stdout.buffer.write(bytes([0x8F]))" + nl
        + "sys.stdout.buffer.flush()" + nl,
        encoding="utf-8",
    )
    out, _ = run_watched(
        [sys.executable, str(script)],
        role="роль",
        stream_path=tmp_path / "s.log",
        detection_window_sec=300,
    )
    assert out == "я"  # the character was assembled correctly
    assert not (tmp_path / "s.log.stdout.raw").exists()  # there is no false evidence.
    assert not (tmp_path / "s.log.stderr.raw").exists()


def test_raw_evidence_is_per_stream(tmp_path, monkeypatch):
    # Round 19: .raw is per channel — the exact bytes of EACH stream in its own
    # order; mixing them by the scheduler is unrecoverable.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    script = tmp_path / "both.py"
    nl = chr(10)
    script.write_text(
        "import sys" + nl
        + "sys.stderr.write('диагностика')" + nl
        + "sys.stderr.flush()" + nl
        + "sys.stdout.buffer.write(bytes([0xFF]))" + nl
        + "sys.stdout.buffer.flush()" + nl,
        encoding="utf-8",
    )
    run_watched(
        [sys.executable, str(script)],
        role="роль",
        stream_path=tmp_path / "s.log",
        detection_window_sec=300,
    )
    assert (tmp_path / "s.log.stdout.raw").read_bytes() == bytes([0xFF])
    assert (tmp_path / "s.log.stderr.raw").read_bytes() == "диагностика".encode()


def test_invalid_bytes_produce_raw_evidence(tmp_path, monkeypatch):
    # Round 11: non-UTF-8 output leaves a raw file with the EXACT bytes.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    script = tmp_path / "binout.py"
    nl = chr(10)
    script.write_text(
        "import sys" + nl
        + "sys.stdout.buffer.write(b'text-" + chr(92) + "xff-tail')" + nl
        + "sys.stdout.buffer.flush()" + nl,
        encoding="utf-8",
    )
    out, _ = run_watched(
        [sys.executable, str(script)],
        role="роль",
        stream_path=tmp_path / "s.log",
        detection_window_sec=300,
    )
    raw = (tmp_path / "s.log.stdout.raw").read_bytes()
    assert raw == b"text-" + bytes([0xFF]) + b"-tail"  # the exact bytes, 0xFF included
    # Round 19: the evidence is per channel — the clean stream is empty, not mixed.
    assert (tmp_path / "s.log.stderr.raw").read_bytes() == b""
    assert (chr(92) + "xff") in out  # the textual form carries a visible escape


def test_clarification_prompt_is_journaled_verbatim(tmp_path):
    # Round 11: a final «да» after a clarification is journalled with the text of
    # THE CLARIFICATION, which is what the operator actually answered.
    cat = RunCatalog.create(
        tmp_path / "run",
        artifact_paths=["a"],
        critic_model="m",
        description_text="d",
        launch_note_text="l",
    )
    answers = iter(["хм", "да"])
    gate = Gate(cat, ask_fn=lambda _: next(answers), say_fn=lambda _: None)
    verdict, verbatim, prompt = gate.ask_yes_no_verbatim(
        "Подтверждаешь?", kind="входные ворота"
    )
    assert verdict is True and verbatim == "да"
    # Round 13: state comes first — the final answer is journalled by the CALLER
    # after its own durable write, with the actual text of the question.
    assert "did not recognise the answer" in prompt
    cat.mark_entry_confirmed(verbatim, prompt)
    cat.append_gate_record(prompt, verbatim, kind="входные ворота")
    text = (cat.run_dir / "gates.md").read_text(encoding="utf-8")
    assert "хм" in text  # the exchange in between was journalled immediately
    assert "did not recognise the answer" in text
    # The final answer is recorded exactly once, and no exchanges are invented.
    assert text.count(": да") == 1


def test_buffer_rewrite_failure_is_contractual(tmp_path, monkeypatch):
    # Round 11: a failed rewrite of the outbox is a refusal with a cause, not a traceback.
    from review_harness import profile_sync as ps
    from review_harness.profile_sync import ExchangePair, PairBuffer

    buf = PairBuffer(tmp_path)
    for i in range(2):
        buf.append(ExchangePair(f"ф{i}", f"с{i}", "", ""))

    def broken_rewrite_body(self, tmp, text):
        raise PermissionError("нет прав")

    monkeypatch.setattr(ps.PairBuffer, "_rewrite_body", broken_rewrite_body)

    class OkGraph:
        def send_pair(self, pair): pass
        def fetch_profile(self): return ("v", "x")

    with pytest.raises(HarnessError, match="rewrite the outbox"):
        buf.flush(OkGraph())


def test_env_value_types_are_validated(repo, tmp_path):
    # Round 11: a non-string env value is a validation refusal, not a TypeError
    # out of Popen.
    _install_profile(repo, tmp_path, DEV_OK)
    prof = machine_profile.load(repo)
    prof.env = {"HTTPS_PROXY": 42}  # type: ignore[dict-item]
    with pytest.raises(HarnessError, match="env"):
        prof.validate()


def test_profile_path_is_checked_in_profile_env(repo, tmp_path):
    # Round 11 (inherited from 10): the profile PATH wins — a command visible to
    # the shell but absent from the profile PATH does not pass the check.
    _install_profile(repo, tmp_path, DEV_OK)
    prof = machine_profile.load(repo)
    prof.critic_argv = ["python", "{launch_note}", "{model}"]  # a name, not a path
    prof.env = {"PATH": str(tmp_path / "пусто")}
    with pytest.raises(HarnessError, match="not found"):
        prof.check_executables()


def test_privacy_refusal_kills_the_whole_tree(tmp_path, monkeypatch):
    # Round 8, finding 2: a privacy refusal must leave a DEAD tree behind it
    # rather than return control over a living role and its helper.
    # The proof is the DEVNULL child's pulse, which must stop.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    heartbeat = tmp_path / "hb.txt"
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(
            f"""
            import pathlib, time
            p = pathlib.Path({str(heartbeat)!r})
            for i in range(120):
                p.write_text(str(i))
                time.sleep(0.25)
            """
        ),
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, {str(child)!r}],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("работаю")
            sys.stdout.flush()
            time.sleep(60)
            """
        ),
        encoding="utf-8",
    )

    def failing_privacy():
        raise HarnessError(
            "The run catalogue is tracked by git",
            cause="тестовая проверка",
            next_action="тест",
        )

    with pytest.raises(HarnessError, match="tracked"):
        run_watched(
            [sys.executable, str(parent)],
            role="роль-с-помощником",
            stream_path=tmp_path / "s.log",
            detection_window_sec=300,
            privacy_check=failing_privacy,
        )
    time.sleep(1.5)
    beat1 = heartbeat.read_text() if heartbeat.exists() else ""
    time.sleep(1.5)
    beat2 = heartbeat.read_text() if heartbeat.exists() else ""
    assert beat1 == beat2  # the tree is dead: the helper's pulse has stopped


def test_gitignore_removal_mid_role_stops_the_stream(repo, tmp_path):
    # Round 7, finding 1: .gitignore is deleted IN THE MIDDLE of a role (an
    # ordinary edit by development) — the stream stops within the watch tick and
    # the role is killed, rather than "a leak discovered after it finished".
    import subprocess

    gitignore = repo / ".gitignore"
    _install_profile(
        repo,
        tmp_path,
        DEV_OK,
        critic_body=f"""
        import pathlib, sys, time
        pathlib.Path({str(gitignore)!r}).unlink()  # роль правит .gitignore
        sys.stdout.write("начал")
        sys.stdout.flush()
        for _ in range(30):
            time.sleep(1)
            sys.stdout.write(".")
            sys.stdout.flush()
        """,
    )
    run_dir = _create_run(repo)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    gitignore.write_text("docs/review/" + chr(10), encoding="utf-8")

    import time as _t

    start = _t.monotonic()
    with pytest.raises(HarnessError, match="tracked"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))
    assert _t.monotonic() - start < 20  # killed by the tick, not lived out


def test_description_change_triggers_next_round(repo, tmp_path):
    # Round 4, finding 8: the task description is judged by the critic — refining
    # it means a new version and a new full pass, exactly like an artifact edit.
    marker = tmp_path / "critic_runs.txt"
    _install_profile(
        repo,
        tmp_path,
        """
        import pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        if rnd == 1:
            (run_dir / "task_description.md").write_text(
                "# уточнённое описание", encoding="utf-8")
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text("исходы", encoding="utf-8")
        """,
        critic_body=f"""
        import pathlib
        p = pathlib.Path({str(marker)!r})
        p.write_text((p.read_text() if p.exists() else "") + "run\\n")
        print("проход")
        """,
    )
    run_dir = _create_run(repo)
    gate = Gate(RunCatalog(run_dir), ask_fn=lambda q: "да", say_fn=lambda _: None)
    assert cycle.run_cycle(repo, run_dir, gate=gate) == 2
    assert marker.read_text().count("run") == 2


def test_corrupt_gate_answer_elements_fail_loudly(repo, tmp_path):
    # Round 4, finding 7: an item with no question/answer is a refusal with a
    # cause, not a raw KeyError.
    _install_profile(repo, tmp_path, DEV_OK)
    run_dir = _create_run(repo)
    cat = RunCatalog(run_dir)
    cat.mark_entry_confirmed("да", "Входные ворота (тест)")
    cat.write_pass(1, "проход", extra_meta=META)
    (run_dir / "round_01_gate_requests.json").write_text(
        '[{"question": "Вопрос?", "kind": "развилка"}]', encoding="utf-8"
    )
    (run_dir / "round_01_gate_answers.json").write_text(
        '[{"answer": "да"}]', encoding="utf-8"  # no question
    )
    with pytest.raises(HarnessError, match="corrupted"):
        cycle.one_round(repo, run_dir, gate=_gate(run_dir))


def test_auto_cycle_runs_next_round_on_artifact_change(repo, tmp_path):
    # A finding of round 3 (automatic running): an edit of the artifact is the
    # next full pass with no operator; with no edit the cycle stops.
    marker = tmp_path / "critic_runs.txt"
    _install_profile(
        repo,
        tmp_path,
        """
        import pathlib, sys
        run_dir, rnd = pathlib.Path(sys.argv[1]), int(sys.argv[2])
        artifact = run_dir.parent.parent.parent / "x.py"
        if rnd == 1:
            artifact.write_text("правка круга 1", encoding="utf-8")
        (run_dir / f"round_{rnd:02d}_outcomes.md").write_text("исходы", encoding="utf-8")
        """,
        critic_body=f"""
        import pathlib
        p = pathlib.Path({str(marker)!r})
        p.write_text((p.read_text() if p.exists() else "") + "run\\n")
        print("проход")
        """,
    )
    run_dir = _create_run(repo)
    said = []
    gate = Gate(RunCatalog(run_dir), ask_fn=lambda q: "да", say_fn=said.append)

    last = cycle.run_cycle(repo, run_dir, gate=gate)
    # Round 1: development edits the artifact → round 2 runs automatically;
    # round 2: no edits → the cycle stops by itself.
    assert last == 2
    assert marker.read_text().count("run") == 2
    assert any("waits for the operator" in s for s in said)


def test_devnull_child_is_killed_with_the_role(tmp_path, monkeypatch):
    # Round 5: a helper on DEVNULL pipes is invisible to output watching — only
    # the structural guarantee kills it (Job Object / process group). The child
    # writes a pulse to a file; after the role ends the pulse must stop.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    heartbeat = tmp_path / "heartbeat.txt"
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(
            f"""
            import pathlib, time
            p = pathlib.Path({str(heartbeat)!r})
            for i in range(120):
                p.write_text(str(i))
                time.sleep(0.25)
            """
        ),
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys
            subprocess.Popen([sys.executable, {str(child)!r}],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('DONE')
            """
        ),
        encoding="utf-8",
    )
    out, _ = run_watched(
        [sys.executable, str(parent)],
        role="спаунер-devnull",
        stream_path=tmp_path / "stream.log",
        detection_window_sec=300,
    )
    assert "DONE" in out
    time.sleep(1.5)
    beat1 = heartbeat.read_text() if heartbeat.exists() else ""
    time.sleep(1.5)
    beat2 = heartbeat.read_text() if heartbeat.exists() else ""
    assert beat1 == beat2  # the pulse stopped: the child died together with the role


def test_stream_open_failure_kills_the_launched_process(tmp_path, monkeypatch):
    # Round 5: a failed opening of the stream file does not leave an already
    # started process unwatched — the tree is killed and the refusal is loud.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    with pytest.raises(HarnessError, match="stream"):
        run_watched(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            role="роль",
            stream_path=tmp_path / "нет_такого_каталога" / "s.log",
            detection_window_sec=300,
        )


def test_stderr_is_streamed_to_partial_too(tmp_path, monkeypatch):
    # Round 6: the refusal promises "the received part of the output is intact in
    # partial" — the header and stderr diagnostics must be on disk, not in memory.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    script = tmp_path / "err_then_hang.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, time
            sys.stderr.write("sandbox: read-only -- диагностика")
            sys.stderr.flush()
            time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessError, match="dead"):
        run_watched(
            [sys.executable, str(script)],
            role="висящий",
            stream_path=tmp_path / "stream.log",
            detection_window_sec=3,
        )
    assert "диагностика" in (tmp_path / "stream.log").read_text(encoding="utf-8")


def test_orphan_child_after_parent_success_is_reaped(tmp_path, monkeypatch):
    # Findings of rounds 3-4: a helper that outlived the parent (on pipes or on
    # DEVNULL, it makes no difference) is finished off STRUCTURALLY: the role
    # lives in a Job Object (Windows) or a process group (POSIX), and ending the
    # role closes the whole tree. The role still ends in an honest success — with
    # no minute of hanging on an orphan and no lost output.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    script = tmp_path / "spawner.py"
    script.write_text(
        textwrap.dedent(
            """
            import subprocess, sys
            # Ребёнок наследует stdout родителя и держит трубу минуту.
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            print("DONE")
            """
        ),
        encoding="utf-8",
    )
    start = time.monotonic()
    out, _ = run_watched(
        [sys.executable, str(script)],
        role="спаунер",
        stream_path=tmp_path / "stream.log",
        detection_window_sec=300,
    )
    elapsed = time.monotonic() - start
    assert "DONE" in out
    assert elapsed < 45  # the tree was finished off, nobody waited a minute for the orphan


def test_streaming_without_newlines_counts_as_alive(tmp_path, monkeypatch):
    # Finding 5 of round 1: an LLM client streams tokens with no newline — the
    # sign of life is ANY bytes, not finished lines. The role writes a dot a
    # second with no newline for longer than the window; line reading would kill it.
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    script = tmp_path / "streamer.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys, time
            for _ in range(6):
                sys.stdout.write(".")
                sys.stdout.flush()
                time.sleep(1)
            sys.stdout.write("КОНЕЦ")
            """
        ),
        encoding="utf-8",
    )
    start = time.monotonic()
    out, _ = run_watched(
        [sys.executable, str(script)],
        role="стример",
        stream_path=tmp_path / "stream.log",
        detection_window_sec=3,
        # 6 seconds of dots against a 3s window: alive only if bytes count as life.
    )
    assert out.endswith("КОНЕЦ")
    assert time.monotonic() - start >= 5
