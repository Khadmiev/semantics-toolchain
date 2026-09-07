# SPDX-License-Identifier: Apache-2.0
"""Refusals and the run catalogue: the load-bearing rules of transport.

What is checked here is not conveniences but the promises of the spec: a refusal
never comes without a cause and a next action; a pass is written immediately and
survives a crash; the state of the cycle is restored from the files alone.
"""

import pytest

from review_harness.errors import HarnessError
from review_harness.runcat import RunCatalog

# Complete round metadata is a contract of write_pass: a pass without the
# snapshot of the judged version and the delivery version does not exist.
META = {
    "operator_profile_version": "test-v1",
    "git_head": "h",
    "artifact_snapshot": {
        "src/thing.py": "h",
        "__task_description__": "h",
        "__launch_note__": "h",
    },
}


def _make_run(tmp_path):
    return RunCatalog.create(
        tmp_path / "run",
        artifact_paths=["src/thing.py"],
        critic_model="gpt-5.6-sol",
        description_text="# a description",
        launch_note_text="# a launch",
    )


class TestHarnessError:
    def test_requires_cause_and_next_action(self):
        with pytest.raises(ValueError):
            HarnessError("it broke", cause="", next_action="fix it")
        with pytest.raises(ValueError):
            HarnessError("it broke", cause="because", next_action="  ")

    def test_render_names_cause_and_action(self):
        err = HarnessError("Refusal X", cause="cause Y", next_action="action Z")
        text = err.render()
        assert "cause Y" in text and "action Z" in text


class TestRunCatalog:
    def test_model_is_mandatory_input(self, tmp_path):
        # The choice of the critic's model is an explicit review input; no silent default.
        with pytest.raises(HarnessError, match="model"):
            RunCatalog.create(
                tmp_path / "run",
                artifact_paths=["a"],
                critic_model="  ",
                description_text="d",
                launch_note_text="l",
            )

    def test_create_refuses_nonempty_dir(self, tmp_path):
        run = tmp_path / "run"
        run.mkdir()
        (run / "junk.txt").write_text("x")
        with pytest.raises(HarnessError, match="not empty"):
            RunCatalog.create(
                run,
                artifact_paths=["a"],
                critic_model="m",
                description_text="d",
                launch_note_text="l",
            )

    def test_pass_written_verbatim_byte_for_byte(self, tmp_path):
        # Verbatim = exact equality: no harness headers and no trimmed edges — the
        # pass file reads as the critic's result, not as a document the harness
        # laid out.
        cat = _make_run(tmp_path)
        original = "\n  a pass with leading spaces\nand a tail\n\n"
        path = cat.write_pass(1, original, extra_meta=META)
        assert path.read_text(encoding="utf-8") == original

    def test_pass_meta_must_be_complete(self, tmp_path):
        # Round 4: the metadata file existing is not completeness; a pass with no
        # snapshot of the judged version must not exist.
        cat = _make_run(tmp_path)
        with pytest.raises(HarnessError, match="incomplete"):
            cat.write_pass(1, "pass")  # without extra_meta
        cat.write_pass(1, "pass", extra_meta=META)
        # Trimming the metadata after the fact is caught on reading and on restore.
        meta_path = cat.run_dir / "round_01_pass_meta.json"
        meta_path.write_text('{"received_at": "t"}', encoding="utf-8")
        with pytest.raises(HarnessError, match="incomplete"):
            cat.read_round_meta(1)
        with pytest.raises(HarnessError, match="incomplete"):
            RunCatalog(cat.run_dir).restore()

    def test_outcomes_without_pass_break_restore(self, tmp_path):
        # Round 4: an orphaned outcomes file would pass an analysis that never
        # happened off as one that did — the invariant is checked, not assumed.
        cat = _make_run(tmp_path)
        (cat.run_dir / "round_02_outcomes.md").write_text("old outcomes", encoding="utf-8")
        with pytest.raises(HarnessError, match="with no pass"):
            RunCatalog(cat.run_dir).restore()

    def test_pass_never_overwritten(self, tmp_path):
        # A round's pass is a fact: a restart after a crash continues the analysis
        # rather than replaying the critic over the record (finding 1 of round 1).
        cat = _make_run(tmp_path)
        cat.write_pass(1, "the original pass", extra_meta=META)
        with pytest.raises(HarnessError, match="already written"):
            cat.write_pass(1, "a new pass", extra_meta=META)
        assert "the original pass" in (cat.run_dir / "round_01_pass.md").read_text(
            encoding="utf-8"
        )

    def test_empty_description_is_refused(self, tmp_path):
        # An empty description would give the critic a clean pass against an intent
        # that does not exist (finding 2 of round 1).
        with pytest.raises(HarnessError, match="description"):
            RunCatalog.create(
                tmp_path / "run",
                artifact_paths=["a"],
                critic_model="m",
                description_text="   \n",
                launch_note_text="l",
            )

    def test_entry_gate_flag_restores(self, tmp_path):
        cat = _make_run(tmp_path)
        assert RunCatalog(cat.run_dir).restore().entry_gate_confirmed is False
        cat.mark_entry_confirmed("yes", "Entry gate (test)")
        assert RunCatalog(cat.run_dir).restore().entry_gate_confirmed is True

    def test_corrupt_meta_fails_with_next_action(self, tmp_path):
        # Corrupted JSON is a harness refusal, not a raw traceback
        # (finding 6 of round 1).
        cat = _make_run(tmp_path)
        (cat.run_dir / "run.json").write_text("{truncated", encoding="utf-8")
        with pytest.raises(HarnessError, match="What to do|corrupted"):
            RunCatalog(cat.run_dir).restore()

    def test_typed_meta_refuses_string_false(self, tmp_path):
        # Finding 5 of round 2: the string "false" through bool() would become True
        # and open the first pass without the operator's confirmation.
        cat = _make_run(tmp_path)
        meta = (cat.run_dir / "run.json").read_text(encoding="utf-8")
        (cat.run_dir / "run.json").write_text(
            meta.replace("false", '"false"'), encoding="utf-8"
        )
        with pytest.raises(HarnessError, match="corrupted"):
            RunCatalog(cat.run_dir).restore()

    def test_string_false_completed_marker_is_refused(self, tmp_path):
        # Round 15: a string "false" in the completion marker is truthy in a
        # condition and would walk past the gate — the type is checked.
        import json as _json

        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass", extra_meta=META)
        meta_path = cat.run_dir / "round_01_pass_meta.json"
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        meta["completed"] = "false"
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(HarnessError, match="the wrong type"):
            RunCatalog(cat.run_dir).restore()

    def test_completed_rounds_must_form_prefix(self, tmp_path):
        # Round 15: "round 1 is not complete, round 2 is" is corruption, not a reason
        # to analyse an old round on top of later history.
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass 1", extra_meta=META)
        cat.write_outcomes(1, "outcomes 1")
        cat.write_pass(2, "pass 2", extra_meta=META)
        cat.write_outcomes(2, "outcomes 2")
        cat.mark_round_completed(2)  # the second only!
        with pytest.raises(HarnessError, match="prefix"):
            RunCatalog(cat.run_dir).restore()

    def test_completed_without_outcomes_is_corruption(self, tmp_path):
        # Round 16: completed=true with the outcomes gone is corruption of a completed
        # history, not a repeat analysis over one that already happened.
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass", extra_meta=META)
        cat.write_outcomes(1, "outcomes")
        cat.mark_round_completed(1)
        (cat.run_dir / "round_01_outcomes.md").unlink()
        with pytest.raises(HarnessError, match="outcomes are missing"):
            RunCatalog(cat.run_dir).restore()

    def test_pending_request_on_completed_round_is_corruption(self, tmp_path):
        # Round 16: a closed round asks no questions — a hanging question on a
        # completed round is not swallowed silently.
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass", extra_meta=META)
        cat.write_outcomes(1, "outcomes")
        cat.mark_round_completed(1)
        (cat.run_dir / "round_01_gate_requests.json").write_text(
            '[{"question": "A late question?"}]', encoding="utf-8"
        )
        with pytest.raises(HarnessError, match="Completed rounds"):
            RunCatalog(cat.run_dir).restore()

    def test_gate_files_outside_history_are_corruption(self, tmp_path):
        # Round 20: a question from round 99 while round 1 is running is never served
        # — the operator's question would hang undelivered while the cycle moved on;
        # the same holds for orphaned answers and for "round 0".
        cat = _make_run(tmp_path)
        (cat.run_dir / "round_99_gate_requests.json").write_text(
            '[{"question": "An undeliverable question?"}]', encoding="utf-8"
        )
        with pytest.raises(HarnessError, match="outside the run"):
            RunCatalog(cat.run_dir).restore()
        (cat.run_dir / "round_99_gate_requests.json").unlink()
        (cat.run_dir / "round_99_gate_answers.json").write_text(
            '[{"question": "q", "answer": "a"}]', encoding="utf-8"
        )
        with pytest.raises(HarnessError, match="outside the run"):
            RunCatalog(cat.run_dir).restore()

    def test_answers_before_any_pass_are_corruption(self, tmp_path):
        # Round 21 (a descendant of round 20): the measure is the pass, not the
        # number. Answers of the "current" round in a fresh run with not a single
        # pass are a substitution for the operator's word, not a legal state.
        cat = _make_run(tmp_path)
        (cat.run_dir / "round_01_gate_answers.json").write_text(
            '[{"question": "q", "answer": "a phantom answer"}]', encoding="utf-8"
        )
        with pytest.raises(HarnessError, match="outside the run"):
            RunCatalog(cat.run_dir).restore()

    def test_orphan_answers_are_parsed_structurally(self, tmp_path):
        # Round 21: answers with no questions file (which is the normal state after a
        # batch has been served) are parsed structurally at recovery rather than when
        # development reads them; valid answers stay legal.
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass", extra_meta=META)
        (cat.run_dir / "round_01_gate_answers.json").write_text(
            '[{"question": "q"}]', encoding="utf-8"  # no answer
        )
        with pytest.raises(HarnessError, match="answers file of round 1 is corrupted"):
            RunCatalog(cat.run_dir).restore()
        (cat.run_dir / "round_01_gate_answers.json").write_text(
            '[{"question": "q", "answer": "a"}]', encoding="utf-8"
        )
        RunCatalog(cat.run_dir).restore()  # valid orphans — legal

    def test_two_unfinished_rounds_are_corruption(self, tmp_path):
        # Round 19: only the current round can be incomplete — losing the markers of
        # two rounds does not send the earlier one to a repeat analysis over later
        # history, it fails loudly.
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass 1", extra_meta=META)
        cat.write_outcomes(1, "outcomes 1")
        cat.write_pass(2, "pass 2", extra_meta=META)
        cat.write_outcomes(2, "outcomes 2")
        # the completed markers were never set (corruption of both rounds' metadata)
        with pytest.raises(HarnessError, match="round is incomplete"):
            RunCatalog(cat.run_dir).restore()

    def test_confirmed_flag_requires_evidence(self, tmp_path):
        # Round 17: a flag without the verbatim answer and the question does not
        # exist — neither on the write nor when restoring a partly corrupted run.json.
        import json as _json

        cat = _make_run(tmp_path)
        with pytest.raises(HarnessError, match="with no verbatim"):
            cat.mark_entry_confirmed("", "")
        cat.mark_entry_confirmed("yes", "Do you confirm?")
        meta_path = cat.run_dir / "run.json"
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["entry_gate_answer"]
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(HarnessError, match="corrupted"):
            RunCatalog(cat.run_dir).restore()

    def test_entry_repair_fields_are_typed(self, tmp_path):
        # Round 16: a list instead of the verbatim answer does not turn into its
        # string form in the journal — the types of the repaired fields are checked.
        import json as _json

        cat = _make_run(tmp_path)
        meta_path = cat.run_dir / "run.json"
        meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        meta["entry_gate_confirmed"] = True
        meta["entry_gate_answer"] = ["yes"]
        meta_path.write_text(_json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(HarnessError, match="corrupted"):
            RunCatalog(cat.run_dir).restore()

    def test_journal_repair_matches_exact_pair(self, tmp_path):
        # Round 15: a final «да» inside an intermediate «да?» does not count as
        # recorded — the repair compares the exact lines of the pair.
        cat = _make_run(tmp_path)
        cat.append_gate_record("Do you confirm?", "yes?", kind="the entry gate")
        cat.mark_entry_confirmed("yes", "Please answer yes or no.")
        cat.entry_journal_repair()
        text = (cat.run_dir / "gates.md").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert "The operator's answer (verbatim): yes" in lines  # the exact line
        assert "The operator's answer (verbatim): yes?" in lines  # and the intermediate one is intact.

    def test_junk_files_do_not_crash_restore(self, tmp_path):
        cat = _make_run(tmp_path)
        (cat.run_dir / "junk_outcomes.md").write_text("junk", encoding="utf-8")
        (cat.run_dir / "round_xx_gate_requests.json").write_text("[]", encoding="utf-8")
        state = RunCatalog(cat.run_dir).restore()  # does not fail
        assert state.rounds_with_outcomes == []

    def test_missing_description_fails_restore(self, tmp_path):
        cat = _make_run(tmp_path)
        (cat.run_dir / "task_description.md").unlink()
        with pytest.raises(HarnessError, match="task_description"):
            RunCatalog(cat.run_dir).restore()

    def test_check_ignore_hard_failure_is_not_treated_as_tracked(self, tmp_path, monkeypatch):
        # Finding 11 of round 2: code 128 (dubious ownership) means "the check did not
        # run", not "the directory is tracked"; what needs repair is git access, not
        # .gitignore.
        import subprocess as sp

        from review_harness import runcat as rc

        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)

        real_run = sp.run

        def fake_run(cmd, **kw):
            if cmd[:3] == ["git", "-C", str(repo)]:
                return sp.CompletedProcess(cmd, 128, stdout="", stderr="fatal: dubious ownership")
            return real_run(cmd, **kw)

        monkeypatch.setattr(rc.subprocess, "run", fake_run)
        with pytest.raises(HarnessError, match="safe.directory|did not run"):
            rc.assert_outside_git(repo / "runs" / "r1")

    def test_outcomes_require_pass_first(self, tmp_path):
        # Outcomes before the pass would mean analysing text that was never written —
        # exactly the loss window of finding 3 of the second run.
        cat = _make_run(tmp_path)
        with pytest.raises(HarnessError, match="before"):
            cat.write_outcomes(1, "outcomes")

    def test_restore_from_files_only(self, tmp_path):
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass 1", extra_meta=META)
        cat.write_outcomes(1, "outcomes 1")
        cat.mark_round_completed(1)
        cat.write_pass(2, "pass 2", extra_meta=META)
        # A new object is a "new session": no memory, only the files.
        state = RunCatalog(cat.run_dir).restore()
        assert state.critic_model == "gpt-5.6-sol"
        assert state.rounds_with_pass == [1, 2]
        assert state.rounds_with_outcomes == [1]
        # Round 2 received a pass but was not analysed — the current one is still 2.
        assert state.current_round == 2

    def test_restore_after_full_round_advances(self, tmp_path):
        cat = _make_run(tmp_path)
        cat.write_pass(1, "pass", extra_meta=META)
        cat.write_outcomes(1, "outcomes")
        cat.mark_round_completed(1)
        assert RunCatalog(cat.run_dir).restore().current_round == 2

    def test_restore_on_foreign_dir_fails_loudly(self, tmp_path):
        foreign = tmp_path / "not_a_run"
        foreign.mkdir()
        with pytest.raises(HarnessError, match="What to do|is not"):
            RunCatalog(foreign).restore()

    def test_run_dir_inside_git_must_be_ignored(self, tmp_path):
        # The operator's decision "the run catalogue is outside git" is enforced by the
        # harness, not by the caller's discipline (finding 12 of round 1).
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        tracked = dict(
            artifact_paths=["a"],
            critic_model="m",
            description_text="d",
            launch_note_text="l",
        )
        with pytest.raises(HarnessError, match="tracked by git"):
            RunCatalog.create(repo / "runs" / "r1", **tracked)
        (repo / ".gitignore").write_text("runs/\n", encoding="utf-8")
        RunCatalog.create(repo / "runs" / "r1", **tracked)  # ignored — allowed

    def test_gate_record_is_durable_and_verbatim(self, tmp_path):
        cat = _make_run(tmp_path)
        cat.append_gate_record("Use the ready one?", "Yes, the ready one", kind="a fork")
        cat.append_gate_record("Do we finalise?", "Финализации принимаю", kind="a confirmation")
        text = (cat.run_dir / "gates.md").read_text(encoding="utf-8")
        assert "Yes, the ready one" in text and "Финализации принимаю" in text
