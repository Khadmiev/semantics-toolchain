# SPDX-License-Identifier: Apache-2.0
"""Server-side launch-script rendering (B.9 C-3 / C-9).

Under B.7 the launcher was a local CLI, and this file tested its generation step. B.9
retires the CLI: the server renders the script from the frozen profile version + the
protocol template + the review snapshot, and the client copies it verbatim against an
out-of-band hash. The claims worth their own tests carried over (channels from the
review's OWN configuration, a supervisor channel the sidecar can actually reach, the two
quoting incidents) and the C-9 checklist adds the new ones: determinism, the startup
gate, the frozen critic invocation line, refusal over improvisation.
"""

import hashlib
import os
import subprocess

import pytest

from assistant_memory.review import launcher, watcher
from assistant_memory.review.errors import InstrumentValidationError

from tests.instrument_helpers import make_content

_SNAPSHOT = {
    "host": {"hostname": "TESTBOX", "username": "tester"},
    "critic": {"engine": "codex", "model": "gpt-5.2-codex", "effort": "high"},
}


def _render(content_over=None, snapshot=None, ping=None, **kw):
    args = dict(
        review_id="0123456789abcdef",
        snapshot=snapshot or _SNAPSHOT,
        profile_key={"hostname": "TESTBOX", "username": "tester", "engine": "codex"},
        version_id="a" * 32,
        version_n=3,
        content=make_content(**(content_over or {})),
        resolved_ping=ping or {"primary": "prod_bot", "fallback": "second_bot"},
    )
    args.update(kw)
    return launcher.render_launch_script(**args)


# --- channel resolution (B.7 H-4, preserved by C-9 clause 1) --------------


def test_channels_come_from_the_reviews_own_configuration():
    assert launcher.resolve_ping_channels(
        {"primary": "prod_bot", "fallback": "second_bot"}
    ) == ("prod_bot", "second_bot")


def test_the_supervisor_is_never_given_a_channel_only_the_agent_can_deliver():
    """The crash ping fires when the watcher is dead — which is exactly when the development
    side may be gone too. Addressing it to the agent-delivered push channel would be a ping
    into a mailbox nobody is holding."""
    assert launcher.resolve_ping_channels(
        {"primary": "push", "fallback": "prod_bot"}
    ) == ("prod_bot", "prod_bot")


def test_agent_channel_mirror_does_not_drift():
    """launcher.py deliberately does not import the watcher; the mirrored constant is
    asserted here so the two cannot drift silently."""
    assert launcher.DEFAULT_AGENT_DELIVERED_CHANNELS == watcher.AGENT_DELIVERED_CHANNELS


# --- the C-9 checklist ----------------------------------------------------


def test_render_is_deterministic_and_hash_is_out_of_band():
    """Clause 2+3: same inputs -> same bytes; no render time, no in-band digest — the
    digest is computed over exactly the emitted bytes and travels beside them."""
    s1, h1 = _render()
    s2, h2 = _render()
    assert (s1, h1) == (s2, h2)
    assert h1 == hashlib.sha256(s1.encode("utf-8")).hexdigest()
    assert h1 not in s1, "an in-band digest is self-referential (clause 2)"


def test_header_names_the_render_inputs():
    script, _ = _render()
    assert "a" * 32 in script  # profile version id
    assert launcher.LAUNCH_TEMPLATE_VERSION in script
    assert "0123456789abcdef" in script
    assert "TESTBOX" in script and "tester" in script


def test_startup_gate_checks_host_pair_and_runs_probes_before_the_supervisor():
    """Clause 7: the gate is the host check plus the SAME probes, a named refusal before
    any pass is spent — a wrong host is a MISLAUNCH, a failed probe a devaluation signal."""
    script, _ = _render()
    gate_part, _, run_part = script.partition("review_supervisor.py")
    assert '"%COMPUTERNAME%"=="TESTBOX"' in gate_part
    assert '"%USERNAME%"=="tester"' in gate_part
    assert "C:/bin/codex.exe --version" in gate_part  # the profile's probe, in the gate
    assert "MISLAUNCH" in script and "devaluation" in gate_part
    # ...and the same probes ride the watcher line for every subsequent pass (C-7).
    assert "--profile-probe" in run_part


def test_critic_invocation_carries_the_frozen_model_and_effort():
    """Clause 8: the invocation line is where the recorded decisions become real."""
    script, _ = _render()
    assert "-m gpt-5.2-codex" in script
    assert "model_reasoning_effort=high" in script
    # the stdin marker stays last — flags are inserted before it
    assert "read-only -m gpt-5.2-codex -c model_reasoning_effort=high -" in script


def test_composed_invocation_survives_an_apostrophe_in_the_executable_path():
    """Round 3: conditional quoting let an apostrophe through unquoted and the composed
    line stopped parsing — every token is now canonically shlex-serialized."""
    composed = launcher.compose_critic_cmd(
        {"codex_cmd": "'C:/O'\\''Reilly/codex.exe' exec -s read-only -"},
        model="gpt-5.2-codex", effort="high",
    )
    from assistant_memory.review.sandbox import tokenize_codex_cmd

    tokens = tokenize_codex_cmd(composed)
    assert tokens is not None and tokens[0] == "C:/O'Reilly/codex.exe"
    assert tokens[1] == "exec" and "-m" in tokens


def test_composed_invocation_is_revalidated_read_only():
    """Clause 8 + D-6: a base command whose composition breaks the read-only contract is
    a render defect, refused — not a tolerable drift."""
    with pytest.raises(InstrumentValidationError, match="read-only"):
        launcher.compose_critic_cmd(
            {"codex_cmd": "codex exec -s danger-full-access -"},
            model="m", effort=None,
        )


def test_no_secret_values_and_absolute_paths():
    """Clauses 4+5: tokens by file path only; every path comes absolute from the profile."""
    script, _ = _render()
    assert "critic.token" in script  # referenced by path...
    assert "Bearer" not in script  # ...never inlined
    assert "C:\\proj\\repo\\.review_tokens\\review_01234567\\critic.token" in script
    assert "scripts" in script and "C:\\proj\\repo\\scripts\\review_supervisor.py" in script


def test_unknown_engine_refuses_render_with_the_preparation_route():
    """Clause 9 / C-6: no fallback launch path lives in a generated artifact."""
    with pytest.raises(InstrumentValidationError, match="preparation path"):
        _render(profile_key={"hostname": "TESTBOX", "username": "tester", "engine": "mystery"})


def test_graph_probe_url_is_baked_from_the_profile():
    """B.8 F-1 fail-closed ground: the measured live failure was a launch assembled by
    hand that forgot --graph-probe-url; the render bakes it from the profile's base_url."""
    script, _ = _render()
    assert "--graph-probe-url http://localhost:8000/health/db" in script


def test_posix_render_quotes_and_gates():
    script, digest = _render(
        content_over={
            "shell": "posix",
            "machinery_root": "/proj/a b",
            "python": "/usr/bin/python3",
            "engine_binary": "/usr/local/bin/codex",
            "token_dir": "/proj/tokens",
            "projection_root": "/proj/projections",
        }
    )
    assert script.startswith("#!/bin/sh")
    assert "'/proj/a b'" in script, "POSIX quoting is single quotes"
    assert '"$(hostname)"' in script and '"$(id -un)"' in script
    assert digest == hashlib.sha256(script.encode("utf-8")).hexdigest()


# --- the B.14 machinery/subject split (A-3..A-5) --------------------------


def test_template_version_names_the_b14_split():
    assert launcher.LAUNCH_TEMPLATE_VERSION == "B.14.1"


def test_subject_root_rides_the_snapshot_into_preflight_and_watcher_cwd():
    """A-4/A-5: the subject root's ONLY carriers into the launch are the render-side
    subject preflight and the watcher's --cwd argument; bootstrap stage one (cd,
    PYTHONPATH, supervisor and prompt paths) stays entirely on the machinery root."""
    script, _ = _render(snapshot={**_SNAPSHOT, "subject_root": "D:\\subject repo"})
    assert 'call git -C "D:\\subject repo" rev-parse --git-dir' in script
    assert "exit /b 4" in script
    assert '--cwd "D:\\subject repo"' in script
    assert "cd /d C:\\proj\\repo" in script  # machinery, not subject
    assert "C:\\proj\\repo\\scripts\\review_supervisor.py" in script
    # the preflight refusal names the FIELD, never devalues the profile — and the
    # machine-scoped probes are anchored to the machinery root BEFORE they run
    # (round 9, finding b14-profile-probes-follow-subject-cwd)
    pre = script.partition("set PYTHONPATH")[0]
    assert "subject_root" in pre and "never devalues the profile" in pre
    assert script.index("cd /d C:") < script.index("call C:/bin/codex.exe --version")


def test_posix_subject_preflight_and_watcher_cwd():
    script, _ = _render(
        snapshot={**_SNAPSHOT, "subject_root": "/subj/repo"},
        content_over={
            "shell": "posix",
            "machinery_root": "/proj/a b",
            "python": "/usr/bin/python3",
            "engine_binary": "/usr/local/bin/codex",
            "token_dir": "/proj/tokens",
            "projection_root": "/proj/projections",
        },
    )
    assert "git -C /subj/repo rev-parse --git-dir" in script
    assert "exit 4" in script
    assert "--cwd /subj/repo" in script
    assert "cd '/proj/a b' || exit 1" in script  # machinery, not subject


def test_snapshot_without_subject_root_falls_back_to_machinery():
    """Spec constraint 3: a review created before B.14 carries no subject_root in its
    frozen snapshot and finishes under its own contract — subject = machinery,
    exactly the old behavior."""
    script, _ = _render()  # _SNAPSHOT predates the split
    assert "--cwd C:\\proj\\repo" in script
    assert "git -C C:\\proj\\repo rev-parse --git-dir" in script


def test_legacy_stored_repo_root_still_renders():
    """A-4: proven stored versions are immutable; one carrying the pre-split key is
    honored at render — its value was always the machinery half of the old meaning."""
    legacy = make_content()
    legacy["repo_root"] = legacy.pop("machinery_root")
    script, _ = _render(content=legacy)
    assert "cd /d C:\\proj\\repo" in script
    assert "--cwd C:\\proj\\repo" in script


# --- quoting (the two measured incidents) ---------------------------------


def test_a_value_cmd_cannot_quote_is_refused_rather_than_mangled():
    with pytest.raises(InstrumentValidationError) as e:
        launcher.quote_for('C:/we"ird', windows=True)
    assert "no reliable escape" in str(e.value)


def test_percent_signs_survive_cmd_expansion():
    """Quotes do not stop CMD from expanding `%NAME%` — that happens before the argument is
    handed over, inside double quotes too. A literal path with percent signs would arrive as
    something else entirely (critic finding `launcher-cmd-percent-expansion`)."""
    quoted = launcher.quote_for("C:/work/%AM_REVIEW_PYTHON%/token", windows=True)
    assert "%%AM_REVIEW_PYTHON%%" in quoted, "a literal percent is escaped as %% in a batch file"
    # POSIX has no such expansion — `%` is an ordinary character there, so the value is
    # passed through untouched. Escaping it "just in case" would corrupt it in the other
    # direction.
    assert launcher.quote_for("/a/%VAR%/b", windows=False) == "/a/%VAR%/b"


def test_paths_with_spaces_survive_each_shell():
    """The first version escaped POSIX-style inside a Windows batch template, which CMD does
    not treat as grouping — so any path with a space split into pieces and the generated
    launcher simply did not run (critic finding `launcher-shell-quoting`)."""
    spacey = r"C:\Program Files\Py\python.exe"
    script, _ = _render(content_over={"python": spacey})
    assert f'"{spacey}"' in script


@pytest.mark.skipif(os.name != "nt", reason="executes the rendered cmd gate; Windows-only")
def test_cmd_gate_is_actually_passable_and_a_failed_probe_still_prints(tmp_path):
    """Round-trip EXECUTION of the rendered cmd startup gate (found live on the first-ever
    run of a rendered launcher, review bb5f3494): an unescaped `)` inside the probe block's
    refusal echo closed the `if errorlevel 1 ( ... )` block early, so `exit /b 3` ran
    UNCONDITIONALLY — every probe-passing launch exited 3 with no output at all. Text-level
    assertions cannot see cmd's parser; only running the gate can."""
    live = {
        "hostname": os.environ["COMPUTERNAME"],
        "username": os.environ["USERNAME"],
    }
    # B.14: the gate now ends with the subject preflight, so the live run needs a real
    # git worktree as the subject — this checkout itself is one.
    this_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snapshot = {"host": dict(live), "critic": _SNAPSHOT["critic"], "subject_root": this_repo}
    key = {**live, "engine": "codex"}

    def gate_only(probe_cmd, snap=None):
        script, _ = _render(
            snapshot=snap or snapshot,
            profile_key=key,
            content_over={
                # a REAL machinery root: since Task dea66085 the gate refuses a
                # missing one before the probes (this used to pass silently from
                # the wrong cwd - the exact hole the refusal closes)
                "machinery_root": this_repo,
                "probes": [{"name": "gate_probe", "cmd": probe_cmd}],
            },
        )
        gate, seam, _ = script.partition("set PYTHONPATH")
        assert seam, "the template no longer carries the `set PYTHONPATH` seam this test cuts at"
        return gate + "echo GATE-PASSED\r\nexit /b 0\r\n"

    # The stand-in probes mirror the real shape (an absolute forward-slash exe path plus
    # plain arguments, as the profile records them). `cmd.exe /c exit 0` is NOT usable
    # here: under `call` a forward-slash cmd.exe path itself trips errorlevel — a
    # different cmd quirk that would fail this test against a correct template.
    passing = tmp_path / "gate_pass.cmd"
    passing.write_text(gate_only("C:/Windows/System32/where.exe cmd.exe"), encoding="utf-8")
    done = subprocess.run(["cmd", "/c", str(passing)], capture_output=True, text=True)
    assert done.returncode == 0, f"gate refused a passing probe: {done.stdout}{done.stderr}"
    assert "GATE-PASSED" in done.stdout

    failing = tmp_path / "gate_fail.cmd"
    failing.write_text(gate_only("C:/Windows/System32/where.exe no-such-binary-zzz"), encoding="utf-8")
    done = subprocess.run(["cmd", "/c", str(failing)], capture_output=True, text=True)
    assert done.returncode == 3
    assert "GATE-PASSED" not in done.stdout
    # The refusal names itself even though its text carries parentheses (escaped ^( ^)).
    assert "probe gate_probe failed" in (done.stdout + done.stderr)

    # B.14 A-4, live: probes pass, but the SUBJECT is not a git worktree — the gate
    # refuses with exit 4 and a message that names the review's subject, not the profile.
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    bad_subject = tmp_path / "gate_bad_subject.cmd"
    bad_subject.write_text(
        gate_only(
            "C:/Windows/System32/where.exe cmd.exe",
            snap={**snapshot, "subject_root": str(not_a_repo)},
        ),
        encoding="utf-8",
    )
    done = subprocess.run(["cmd", "/c", str(bad_subject)], capture_output=True, text=True)
    assert done.returncode == 4, f"subject preflight did not refuse: {done.stdout}{done.stderr}"
    assert "GATE-PASSED" not in done.stdout
    assert "subject preflight failed" in (done.stdout + done.stderr)


def test_cmd_gate_refuses_a_missing_machinery_root(tmp_path):
    """Task dea66085 (semantic map of review 9ceebe2d): `cd /d` in cmd does NOT stop
    the script on failure — without an errorlevel check the machine probes would run
    against a foreign tree and be ascribed to the profile. The POSIX branch always
    had `|| exit 1`; this pins the cmd twin, live."""
    live = {
        "hostname": os.environ["COMPUTERNAME"],
        "username": os.environ["USERNAME"],
    }
    this_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    snapshot = {"host": dict(live), "critic": _SNAPSHOT["critic"], "subject_root": this_repo}
    script, _ = _render(
        snapshot=snapshot,
        profile_key={**live, "engine": "codex"},
        content_over={
            "machinery_root": str(tmp_path / "no-such-machinery"),
            "probes": [{"name": "gate_probe", "cmd": "C:/Windows/System32/where.exe cmd.exe"}],
        },
    )
    gate, seam, _rest = script.partition("set PYTHONPATH")
    assert seam
    path = tmp_path / "gate_no_machinery.cmd"
    path.write_text(gate + "echo GATE-PASSED\r\nexit /b 0\r\n", encoding="utf-8")
    done = subprocess.run(["cmd", "/c", str(path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 5, f"expected the machinery-root refusal: {done.stdout}{done.stderr}"
    assert "GATE-PASSED" not in done.stdout
    assert "cannot enter machinery_root" in (done.stdout + done.stderr)
