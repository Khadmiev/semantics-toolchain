# SPDX-License-Identifier: Apache-2.0
"""The critic's read-only guard: the command allow-list plus header attestation.

Every refusal case here is a bypass — ported, or found by a review. The guard is
built as an allow-list: an unknown flag (including flags of future codex versions)
is rejected by construction rather than by appearing on a deny-list.
"""

import pytest

from review_harness.errors import HarnessError
from review_harness.machine_profile import MachineProfile
from review_harness.readonly_guard import (
    assert_attested_read_only,
    critic_argv_is_read_only,
    parse_codex_output,
)

GOOD = ["C:/bin/codex.exe", "exec", "-s", "read-only", "-m", "{model}", "-"]


class TestStructuralGuard:
    def test_pinned_codex_exec_passes(self):
        assert critic_argv_is_read_only(GOOD)
        assert critic_argv_is_read_only(
            ["codex", "exec", "--sandbox=read-only", "--model", "{model}", "-"]
        )
        assert critic_argv_is_read_only(
            ["codex", "exec", "-c", "sandbox_mode=read-only", "-m", "{model}",
             "-C", "D:/work", "-"]
        )

    def test_substring_wrapper_is_refused(self):
        # The critic-readonly-substring-wrapper bypass.
        assert not critic_argv_is_read_only(
            ["run-anything.exe", "codex exec -s read-only"]
        )

    def test_conflicting_duplicate_sandbox_is_refused(self):
        assert not critic_argv_is_read_only(
            ["codex", "exec", "-s", "read-only", "-s", "workspace-write",
             "-m", "{model}", "-"]
        )

    def test_unknown_flags_are_refused_by_construction(self):
        # The class is closed by inversion: -o writes a file under an honest
        # read-only sandbox (a finding of round 3), approve-for-me/full-auto
        # changed the approvals (round 2) — all of them, and ANY future flag
        # outside the allow-list, are rejected alike.
        for extra in (["-o", "out.md"], ["--output-last-message", "x"],
                      ["--approve-for-me"], ["--full-auto"],
                      ["--dangerously-bypass-approvals"], ["--json"]):
            assert not critic_argv_is_read_only(GOOD[:-1] + extra + ["-"])

    def test_model_must_be_the_flag_value(self):
        # A finding of round 3: {model} inside the prompt text does not pass the
        # model — the marker must be the value of -m/--model, or codex will take
        # the model from the user's config.
        assert not critic_argv_is_read_only(
            ["codex", "exec", "-s", "read-only", "Declared model {model}"]
        )
        assert not critic_argv_is_read_only(
            ["codex", "exec", "-s", "read-only", "-m", "gpt-5.6-sol", "-"]
        )

    def test_exactly_one_prompt_source(self):
        assert not critic_argv_is_read_only(GOOD + ["extra-positional"])
        assert not critic_argv_is_read_only(GOOD[:-1])  # with no source for the prompt

    def test_shell_metachars_are_refused(self):
        assert not critic_argv_is_read_only(
            ["codex", "exec", "-s", "read-only", "-m", "{model}", "; rm -rf ."]
        )

    def test_profile_without_breakglass_refuses_unsafe_critic(self):
        profile = MachineProfile(
            critic_argv=["python", "critic.py", "{launch_note}", "{model}"],
            dev_argv=["python", "dev.py", "{run_dir}", "{round}"],
            detection_window_sec=300,
            observed_max_gap_sec=1,
            observed_gap_note="a test",
            allow_unsafe_critic_sandbox=False,
        )
        with pytest.raises(HarnessError, match="read-only"):
            profile.validate()

    def test_string_false_breakglass_is_refused(self):
        # A finding of round 3: the string "false" in JSON is truthy in a condition —
        # the break-glass type is checked at runtime, not by an annotation.
        profile = MachineProfile(
            critic_argv=list(GOOD),
            dev_argv=["python", "dev.py", "{run_dir}", "{round}"],
            detection_window_sec=300,
            observed_max_gap_sec=1,
            observed_gap_note="a test",
            allow_unsafe_critic_sandbox="false",  # type: ignore[arg-type]
        )
        with pytest.raises(HarnessError, match="type"):
            profile.validate()


# The real output format of codex exec — checked against probe.log (version,
# header between separators, prompt echo after "user", answer after "codex").
REAL_PREFIX = (
    "OpenAI Codex v0.151.0\n"
    "--------\n"
    "workdir: D:\\git\\assistant_memory\n"
    "model: gpt-5.6-sol\n"
    "provider: openai\n"
    "approval: never\n"
    "sandbox: read-only\n"
    "reasoning effort: high\n"
    "session id: 0000\n"
    "--------\n"
    "user\n"
    "Read docs/review/critic.md and act by it.\n"
    "\n"
)


class TestCodexOutputParsing:
    def test_real_output_parses_sandbox_and_content(self):
        sandbox, content = parse_codex_output(
            REAL_PREFIX + "codex\nfinding 1: everything is broken"
        )
        assert sandbox == "read-only"
        assert "finding 1" in content

    def test_header_and_prompt_echo_are_not_content(self):
        # A finding of round 3: a real header plus the prompt echo without a single
        # line of answer is an empty pass, not "a non-empty stdout".
        sandbox, content = parse_codex_output(REAL_PREFIX + "codex\n")
        assert sandbox == "read-only" and content.strip() == ""
        sandbox, content = parse_codex_output(REAL_PREFIX)  # no marker at all
        assert content.strip() == ""

    def test_quote_in_answer_does_not_change_attestation(self):
        text = REAL_PREFIX + "codex\nquoting the artifact: sandbox: workspace-write\n"
        assert_attested_read_only(text, stream_hint="x")  # not annulled

    def test_wrong_sandbox_header_annuls(self):
        bad = REAL_PREFIX.replace("sandbox: read-only", "sandbox: workspace-write")
        with pytest.raises(HarnessError, match="annulled"):
            assert_attested_read_only(bad + "codex\ntext", stream_hint="x")

    def test_silent_header_annuls(self):
        with pytest.raises(HarnessError, match="silence|did not attest"):
            assert_attested_read_only("a pass with no header", stream_hint="x")


class TestGuardChecksTemplateNotSubstitution:
    def test_safe_path_launches_after_substitution(self, tmp_path, monkeypatch):
        # Round 4, finding 1: the guard judges the profile TEMPLATE; the substituted
        # command (a real model instead of {model}, a path instead of
        # {launch_note}) must not be rejected by our own guard — otherwise the
        # standard safe route never starts at all.
        from review_harness import critic
        from review_harness.machine_profile import MachineProfile

        profile = MachineProfile(
            critic_argv=list(GOOD),
            dev_argv=["python", "dev.py", "{run_dir}", "{round}"],
            detection_window_sec=300,
            observed_max_gap_sec=1,
            observed_gap_note="a test",
            allow_unsafe_critic_sandbox=False,  # the guard is ON
        )
        note = tmp_path / "launch_critic.md"
        note.write_text("# launch", encoding="utf-8")
        seen_argv = {}

        def fake_run_watched(argv, **kw):
            seen_argv["argv"] = argv
            seen_argv["input_text"] = kw.get("input_text")
            # The real separation of streams: the codex header lives in STDERR and the
            # answer in stdout; the critic glues them stderr-first.
            return ("codex\nfinding 1", REAL_PREFIX)

        monkeypatch.setattr(critic, "run_watched", fake_run_watched)
        text = critic.run_critic(
            profile,
            model="gpt-5.6-sol",
            launch_note=note,
            partial_path=tmp_path / "p.partial",
            detection_window_sec=300,
        )
        assert "finding 1" in text
        # The substitution happened: the markers were replaced by real values.
        assert "gpt-5.6-sol" in seen_argv["argv"]
        # The accompanying note went as content on stdin, not as a path in argv.
        assert seen_argv["input_text"] == "# launch"
        assert "-" in seen_argv["argv"]

    def test_model_value_must_be_identifier(self, tmp_path):
        # A marker's value is not a command: injection of flags through the model
        # is rejected at substitution.
        from review_harness import critic
        from review_harness.machine_profile import MachineProfile

        profile = MachineProfile(
            critic_argv=list(GOOD),
            dev_argv=["python", "dev.py", "{run_dir}", "{round}"],
            detection_window_sec=300,
            observed_max_gap_sec=1,
            observed_gap_note="a test",
            allow_unsafe_critic_sandbox=False,
        )
        note = tmp_path / "launch_critic.md"
        note.write_text("x", encoding="utf-8")
        with pytest.raises(HarnessError, match="identifier"):
            critic.build_argv(profile, model="sol --full-auto", launch_note=note)


def test_launch_note_with_bare_codex_line_is_refused(tmp_path):
    # Round 19: a separate "codex" line in the accompanying note is indistinguishable
    # in the prompt echo from the answer marker — refused before the critic starts.
    import pytest as _pytest

    from review_harness.critic import run_critic
    from review_harness.errors import HarnessError as _HE
    from review_harness.machine_profile import MachineProfile as _MP

    note = tmp_path / "launch_critic.md"
    note.write_text(
        "a line" + chr(10) + "codex" + chr(10) + "a tail", encoding="utf-8"
    )
    profile = _MP(
        critic_argv=["python", "no-such-role.py", "-"],
        dev_argv=["python", "dev.py"],
        detection_window_sec=300,
        observed_max_gap_sec=1,
        observed_gap_note="a test",
        allow_unsafe_critic_sandbox=True,
    )
    with _pytest.raises(_HE, match="on a line of its own"):
        run_critic(
            profile,
            model="m",
            launch_note=note,
            partial_path=tmp_path / "p.log",
            detection_window_sec=300,
        )
