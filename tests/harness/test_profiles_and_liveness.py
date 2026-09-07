# SPDX-License-Identifier: Apache-2.0
"""The machine profile, the launcher, liveness, the operator-profile raw material."""

import sys

import pytest

from review_harness import launcher, liveness, machine_profile
from review_harness.errors import HarnessError
from review_harness.machine_profile import MachineProfile
from review_harness.profile_sync import ExchangePair, PairBuffer, refresh_cache


def _profile(**overrides):
    base = dict(
        critic_argv=[sys.executable, "-c", "print('x')", "{launch_note}", "{model}"],
        dev_argv=[sys.executable, "-c", "print('x')", "{run_dir}", "{round}"],
        detection_window_sec=300,
        observed_max_gap_sec=120,
        observed_gap_note="laptop network stalls, observed 2026-08-31",
        # The test roles are python scripts rather than codex: the read-only guard
        # is bypassed by an explicit break-glass (itself checked separately).
        allow_unsafe_critic_sandbox=True,
    )
    base.update(overrides)
    return MachineProfile(**base)


class TestMachineProfile:
    def test_window_below_observed_gap_is_refused(self):
        # A window shorter than the observed stall would declare living stalls dead —
        # exactly the error of the old thresholds (3 polls on a torn network).
        with pytest.raises(HarnessError, match="observed stall"):
            _profile(detection_window_sec=60).validate()

    def test_window_needs_grounding_note(self):
        # A window with no record of where the observation came from is an ungrounded
        # choice: the "practically infinite threshold" of the finding in round 2.
        with pytest.raises(HarnessError, match="grounded|observed_gap_note"):
            _profile(observed_gap_note="  ").validate()

    def test_substitution_markers_are_mandatory(self):
        # Finding 1 of round 2: a critic command without {model} would silently save
        # the pass under a false model — the markers are mandatory.
        with pytest.raises(HarnessError, match=r"\{model\}"):
            _profile(critic_argv=[sys.executable, "-c", "x", "{launch_note}"]).validate()
        with pytest.raises(HarnessError, match=r"\{run_dir\}"):
            _profile(dev_argv=[sys.executable, "-c", "x", "{round}"]).validate()

    def test_negative_windows_are_refused(self):
        # Round 6: a window of -1 is "valid" by the old comparison, yet it kills a
        # living role at the very first check for silence.
        with pytest.raises(HarnessError, match="range"):
            _profile(detection_window_sec=-1, observed_max_gap_sec=-1).validate()
        with pytest.raises(HarnessError, match="range"):
            _profile(detection_window_sec=0, observed_max_gap_sec=0).validate()

    def test_directory_is_not_an_executable(self, tmp_path):
        # Round 6: the path exists but it is a directory — profile-check must not
        # declare such a profile runnable.
        d = tmp_path / "adir"
        d.mkdir()
        p = _profile(critic_argv=[str(d), "{launch_note}", "{model}"])
        with pytest.raises(HarnessError, match="not found"):
            p.check_executables()

    def test_missing_executable_fails_with_next_action(self):
        p = _profile(critic_argv=["definitely-not-a-real-tool-9000"])
        with pytest.raises(HarnessError) as exc:
            p.check_executables()
        assert "What to do" in exc.value.render()

    def test_load_missing_profile_names_the_fix(self, tmp_path):
        with pytest.raises(HarnessError, match="profile-init|launch path"):
            machine_profile.load(tmp_path)

    def test_save_then_load_roundtrip(self, tmp_path):
        machine_profile.save(tmp_path, _profile())
        loaded = machine_profile.load(tmp_path)
        assert loaded.detection_window_sec == 300


class TestLauncher:
    def test_windows_launcher_has_crlf(self, tmp_path):
        # Precedent 4d10aea3: LF line endings broke the cmd launcher twice.
        path = launcher.write_launcher(
            tmp_path / "run.cmd", ["echo hello"], windows=True
        )
        raw = path.read_bytes()
        assert b"\r\n" in raw
        assert b"\n" not in raw.replace(b"\r\n", b"")

    @pytest.mark.skipif(sys.platform != "win32", reason="the cmd probe is Windows-only")
    def test_probe_passes_for_deliverable_launcher(self, tmp_path):
        path = launcher.write_launcher(
            tmp_path / "ok.cmd", ["echo payload"], windows=True
        )
        launcher.probe(path, windows=True)  # does not raise — the delivery happened

    @pytest.mark.skipif(sys.platform != "win32", reason="the cmd probe is Windows-only")
    def test_probe_fails_for_broken_launcher(self, tmp_path):
        # A launcher with no probe branch that fails unconditionally is "delivered
        # unexecutable": the probe must catch that before the payload.
        broken = tmp_path / "broken.cmd"
        broken.write_bytes(b"@echo off\r\nexit /b 2\r\n")
        with pytest.raises(HarnessError, match="not delivered"):
            launcher.probe(broken, windows=True)

    def test_generation_refuses_unescaped_paren(self, tmp_path):
        # Finding 4 of round 1 (the bracket class of B.9-3): cmd silently tolerates a
        # stray ")" — a dynamic probe does not catch the class (live check of
        # 2026-09-01), so it is closed statically at generation.
        with pytest.raises(HarnessError, match="bracket"):
            launcher.write_launcher(tmp_path / "p.cmd", ["echo done)"], windows=True)
        # An escaped bracket is legitimate.
        launcher.write_launcher(tmp_path / "ok.cmd", ["echo done^)"], windows=True)

    def test_payload_runs_in_normal_mode(self, tmp_path):
        # The probe branch does not steal the payload: without the probe variable the
        # body executes and its output is visible.
        import subprocess

        if sys.platform != "win32":
            pytest.skip("a check of the cmd path")
        path = launcher.write_launcher(
            tmp_path / "payload.cmd", ["echo PAYLOAD_RAN"], windows=True
        )
        launcher.probe(path, windows=True)
        result = subprocess.run(
            ["cmd", "/c", str(path)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0 and "PAYLOAD_RAN" in result.stdout


class TestLiveness:
    def test_notify_param_parsed_from_operator_profile(self):
        assert liveness.operator_notify_within_sec("...\nnotify-within: 300s\n...") == 300

    def test_missing_notify_param_fails_loudly(self):
        # There is no default baked in: the thresholds are parameters of the profiles.
        with pytest.raises(HarnessError, match="remember_preference|profile"):
            liveness.operator_notify_within_sec("# a profile with no thresholds")

    def test_announcement_names_the_sum(self):
        said = []
        liveness.announce_window(300, 60, said.append)
        assert "360" in said[0]  # the whole time until the operator knows is the sum


class TestPairBuffer:
    def test_half_pair_is_refused(self, tmp_path):
        with pytest.raises(HarnessError, match="whole"):
            PairBuffer(tmp_path).append(
                ExchangePair(agent_phrasing="", operator_signal="unfold it",
                             expansion="", operator_reaction="")
            )

    def test_append_is_durable_and_pending_restores(self, tmp_path):
        buf = PairBuffer(tmp_path)
        buf.append(ExchangePair("the wording", "unfold it", "unfolding", "ah, so it is about..."))
        again = PairBuffer(tmp_path)  # a new "session" — the files and nothing else
        assert [p.operator_signal for p in again.pending()] == ["unfold it"]

    def test_flush_partial_failure_keeps_remainder(self, tmp_path):
        buf = PairBuffer(tmp_path)
        for i in range(3):
            buf.append(ExchangePair(f"w{i}", f"s{i}", "", ""))

        class FlakyGraph:
            def __init__(self):
                self.sent = []

            def send_pair(self, pair):
                if len(self.sent) == 1:
                    raise ConnectionError("stall")
                self.sent.append(pair.agent_phrasing)

            def fetch_profile(self):
                raise NotImplementedError

        graph = FlakyGraph()
        with pytest.raises(HarnessError, match="not lost"):
            buf.flush(graph)
        # The first is gone, the other two are intact in the outbox — the send goes on.
        assert graph.sent == ["w0"]
        assert len(buf.pending()) == 2

    def test_flush_success_empties_buffer(self, tmp_path):
        buf = PairBuffer(tmp_path)
        buf.append(ExchangePair("w", "s", "", ""))

        class OkGraph:
            def send_pair(self, pair): pass
            def fetch_profile(self): return ("v1", "# Operator profile\nversion: v1\n")

        assert buf.flush(OkGraph()) == 1
        assert buf.pending() == []


class TestBufferPrivacy:
    def test_buffer_inside_git_must_be_ignored(self, tmp_path):
        # Finding 9 of round 2: the outbox is the same class of data as the run
        # catalogues; the operator's decision "outside git by construction" is enforced
        # by the same check.
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        with pytest.raises(HarnessError, match="tracked by git"):
            PairBuffer(repo / "state")
        (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
        PairBuffer(repo / "state")  # ignored — allowed


class TestOperatorWordParsing:
    def test_expanded_affirmatives_are_recognized(self):
        from review_harness.gates import _read_yes_no

        # Finding 10 of round 2: an expanded word from the operator must not turn
        # into a quiet cancellation.
        assert _read_yes_no("Финализацию принимаю") is True
        assert _read_yes_no("да, финализируй") is True
        assert _read_yes_no("подтверждаю") is True
        assert _read_yes_no("нет") is False
        # Negation dominates the affirmative words standing next to it.
        assert _read_yes_no("точно нет") is False
        assert _read_yes_no("принимаю, но не сейчас") is False
        assert _read_yes_no("хм") is None
        # A finding of round 3: morphological negation — an affirmative stem inside a
        # word carrying «не» must read as a refusal.
        assert _read_yes_no("несогласен") is False
        assert _read_yes_no("неподтверждаю") is False
        assert _read_yes_no("непринимаю") is False
        # Round 4: the «не» prefix does not swallow ordinary adverbs — negation holds
        # only over affirmative stems.
        assert _read_yes_no("Да, немедленно финализируй") is True
        assert _read_yes_no("непременно согласен") is True
        # Round 5: idioms of agreement carrying «не» are a Russian "yes", not a refusal.
        assert _read_yes_no("Да, не возражаю") is True
        assert _read_yes_no("Не против") is True
        assert _read_yes_no("не возражаю") is True

    def test_unrecognized_answer_reasks_instead_of_cancelling(self, tmp_path):
        from review_harness.gates import Gate
        from review_harness.runcat import RunCatalog

        cat = RunCatalog.create(
            tmp_path / "run",
            artifact_paths=["a"],
            critic_model="m",
            description_text="d",
            launch_note_text="l",
        )
        answers = iter(["ну такое", "Финализацию принимаю"])
        gate = Gate(cat, ask_fn=lambda _: next(answers), say_fn=lambda _: None)
        assert gate.confirm_unconditional("Финализируем") is True
        # Both exchanges are recorded verbatim.
        text = (cat.run_dir / "gates.md").read_text(encoding="utf-8")
        assert "ну такое" in text and "Финализацию принимаю" in text


class TestProfileCache:
    class Graph:
        def __init__(self, version):
            self.version = version

        def send_pair(self, pair): pass

        def fetch_profile(self):
            return (self.version, f"# Operator profile (compiled)\nversion: {self.version}\n")

    def test_cache_written_when_version_changes(self, tmp_path):
        cache = tmp_path / "operator_profile.md"
        assert refresh_cache(cache, self.Graph("aaa")) is True
        assert refresh_cache(cache, self.Graph("aaa")) is False  # the same version
        assert refresh_cache(cache, self.Graph("bbb")) is True

    def test_version_compared_by_equality_not_substring(self, tmp_path):
        # Finding 11 of round 1: a substring search counted «aaa» as already written
        # when the old one was «baaa» — content addressing requires equality.
        cache = tmp_path / "operator_profile.md"
        assert refresh_cache(cache, self.Graph("baaa")) is True
        assert refresh_cache(cache, self.Graph("aaa")) is True  # NOT suppressed

    def test_reaction_without_expansion_is_refused(self, tmp_path):
        # Finding 9 of round 1: the operator's reaction is meaningful only relative
        # to the unfolding.
        with pytest.raises(HarnessError, match="unfold"):
            PairBuffer(tmp_path).append(
                ExchangePair("w", "unfold it", expansion="", operator_reaction="ага")
            )

    def test_flush_removes_each_sent_pair_durably(self, tmp_path):
        # Finding 10 of round 1: a crash between successful sends must not lead to
        # resending what was already delivered — the outbox shrinks item by item and
        # the duplicate window is narrowed to one pair.
        buf = PairBuffer(tmp_path)
        for i in range(3):
            buf.append(ExchangePair(f"w{i}", f"s{i}", "", ""))

        states = []

        class SpyGraph:
            def send_pair(self, pair):
                states.append(len(PairBuffer(tmp_path).pending()))

            def fetch_profile(self):
                raise NotImplementedError

        assert buf.flush(SpyGraph()) == 3
        # Before pair N was sent the outbox still held 3-N+1 pairs: every successful
        # send removed its own immediately.
        assert states == [3, 2, 1]

    def test_corrupt_buffer_line_fails_with_next_action(self, tmp_path):
        buf = PairBuffer(tmp_path)
        buf.append(ExchangePair("w", "s", "", ""))
        with open(buf.path, "a", encoding="utf-8") as fh:
            fh.write("{a truncated line\n")
        with pytest.raises(HarnessError, match="line 2"):
            buf.pending()

    def test_expansion_without_reaction_is_refused(self, tmp_path):
        # Round 9: an unfolding with no reaction is half a pair; "no reaction came"
        # is recorded in explicit words rather than as emptiness.
        with pytest.raises(HarnessError, match="no operator reaction"):
            PairBuffer(tmp_path).append(
                ExchangePair("w", "unfold it", expansion="an explanation",
                             operator_reaction="")
            )
        PairBuffer(tmp_path).append(
            ExchangePair("w", "unfold it", expansion="an explanation",
                         operator_reaction="(no reaction came)")
        )

    def test_restored_pair_is_validated_like_appended(self, tmp_path):
        # A finding of round 3: the outbox must not send on to the graph what append
        # would have rejected — a restored pair goes through the same validation.
        import json

        buf = PairBuffer(tmp_path)
        buf.append(ExchangePair("w", "s", "", ""))
        bad = {"agent_phrasing": "w", "operator_signal": "",  # an empty signal
               "expansion": "", "operator_reaction": "", "signal_reading": None,
               "captured_at": "t"}
        with open(buf.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(bad, ensure_ascii=False) + "\n")
        with pytest.raises(HarnessError, match="line 2"):
            buf.pending()
