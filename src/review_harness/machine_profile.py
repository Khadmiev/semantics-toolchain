# SPDX-License-Identifier: Apache-2.0
"""The machine profile — the knowledge of "how to start the critic and development here".

Principle 2 of the spec: knowledge about launching lives on the launch path, and the
agent has a first-class way to put it there (``save``). The precedent the principle
was born from: the knowledge went into feedback in the graph, and the next agent
tripped over the same opaque line.

The profile lives in ``.review_harness/machine_profile.json`` at the root of the
repository — material of the author's own development process, outside git (the
reader rule).

Thresholds: the machine profile owns the parameters of the ENVIRONMENT only — the
window in which death is recognised, grounded in observed stalls. The time to notify
the operator lives in his profile, not here (the operator's decision of 2026-09-01:
the parameters are sequential, not competing).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .errors import HarnessError

PROFILE_DIRNAME = ".review_harness"
PROFILE_FILENAME = "machine_profile.json"


@dataclass
class MachineProfile:
    """The launch knowledge of one machine."""

    # The critic's command: an argv template; {launch_note} is substituted by the
    # path of the accompanying note, {model} by the model (an explicit review input).
    critic_argv: list[str]
    # The development command (a headless call of the development session).
    dev_argv: list[str]
    # The window in which death is recognised, in seconds. Silence longer than the
    # window is real death (the operator's reading, recorded 2026-09-01).
    detection_window_sec: int
    # What grounds the window: the longest OBSERVED stall of this environment, in
    # seconds. A window with no grounding is the "practically infinite threshold" of
    # the finding in round 1 of the second run; so a window is valid only when it is
    # not shorter than the observed stall and records where the observation came from.
    observed_max_gap_sec: int
    observed_gap_note: str
    # Break-glass for the critic's read-only guard: True switches off the structural
    # check and the header attestation. The single way past the guard (the old
    # contour's break-glass, ported); it is announced loudly on every launch. In a
    # production profile it must be False.
    allow_unsafe_critic_sandbox: bool = False
    # The environment roles are started in: a profile is "command, ENVIRONMENT,
    # sandbox" (the spec). It is laid over the harness's environment when a role
    # starts — CODEX_HOME and proxies are not inherited from a random shell silently.
    env: dict[str, str] = field(default_factory=dict)
    # Free notes about the environment (paths, sandbox, console encoding).
    notes: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        # Types are checked AT RUNTIME rather than by annotations: a dataclass does not
        # check types itself, and the string "false" in JSON instead of a boolean
        # break-glass would be truthy in a condition — opening a writing critic to an
        # operator who had written a plain textual false.
        # The class: ALL fields of the profile, not one.
        checks = (
            ("critic_argv", self.critic_argv, list),
            ("dev_argv", self.dev_argv, list),
            ("detection_window_sec", self.detection_window_sec, int),
            ("observed_max_gap_sec", self.observed_max_gap_sec, int),
            ("observed_gap_note", self.observed_gap_note, str),
            ("allow_unsafe_critic_sandbox", self.allow_unsafe_critic_sandbox, bool),
            ("env", self.env, dict),
            ("notes", self.notes, dict),
        )
        for name, value, expected in checks:
            ok = isinstance(value, expected) and not (
                expected is int and isinstance(value, bool)
            )
            if ok and expected is list:
                ok = all(isinstance(x, str) for x in value)
            if ok and name == "env":
                ok = all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in value.items()
                )
            if not ok:
                raise HarnessError(
                    f"Profile field {name} has the wrong type",
                    cause=(
                        f"{expected.__name__} was expected, got "
                        f"{type(value).__name__} ({value!r}); the string \"false\" "
                        "is not a boolean false"
                    ),
                    next_action=f"correct {name} in the profile JSON to an honest {expected.__name__}",
                )
        # Check point 1 (D-6 of the old contour, ported): the guard stands at the WRITE
        # of the profile, not only at the launch — configuration cannot silently reopen a
        # closed decision about the critic being read-only.
        from .readonly_guard import assert_critic_read_only

        assert_critic_read_only(
            self.critic_argv, allow_unsafe=self.allow_unsafe_critic_sandbox
        )
        # The substitution markers are mandatory: a critic command without {model}
        # would mean the explicitly chosen model silently never reached the tool and
        # the pass was saved under a false model; a development command without
        # {run_dir}/{round} would mean development never got the round's paths.
        # Silently substituting a marker that is absent is itself a silent
        # failure (principle 1).
        for role, argv, markers in (
            ("critic", self.critic_argv, ("{model}",)),
            ("development", self.dev_argv, ("{run_dir}", "{round}")),
        ):
            joined = " ".join(argv)
            missing = [m for m in markers if m not in joined]
            if missing:
                raise HarnessError(
                    f"The {role} command carries no markers {', '.join(missing)}",
                    cause=(
                        "without the marker the value never reaches the tool: the model "
                        "or the round's paths are silently replaced by the tool's defaults"
                    ),
                    next_action=f"add the markers {', '.join(missing)} to the {role} command",
                )
        # The accompanying note is delivered either as content through stdin ("-" in
        # argv) or as a path through the marker (break-glass roles) — one of the two must
        # be present, otherwise the critic gets no instruction.
        if "-" not in self.critic_argv and "{launch_note}" not in " ".join(self.critic_argv):
            raise HarnessError(
                "The critic command receives no accompanying note",
                cause="there is neither a positional \"-\" (stdin) nor a {launch_note} marker",
                next_action="add \"-\" (the standard codex route) or {launch_note} to the critic command",
            )
        if self.detection_window_sec <= 0 or self.observed_max_gap_sec < 0:
            raise HarnessError(
                "The liveness thresholds are out of range",
                cause=(
                    f"window {self.detection_window_sec}s, observed stall "
                    f"{self.observed_max_gap_sec}s — a negative or zero window "
                    "would kill a living role at the very first check for silence"
                ),
                next_action="set a positive window and a non-negative observed stall",
            )
        if self.detection_window_sec < self.observed_max_gap_sec:
            raise HarnessError(
                "The recognition window is shorter than the observed stall of the environment",
                cause=(
                    f"window {self.detection_window_sec}s < observed stall "
                    f"{self.observed_max_gap_sec}s ({self.observed_gap_note}) — "
                    "such a window will declare living stalls dead"
                ),
                next_action="raise the window to the observed stall, or refresh the observation",
            )
        if not self.observed_gap_note.strip():
            raise HarnessError(
                "The observed stall has no record of where it came from",
                cause="the recognition window must be grounded in facts of the environment, not in a choice",
                next_action="write into observed_gap_note where and when the stall was observed",
            )

    def check_executables(self) -> None:
        """A runnable freshness check: the profile's commands really are startable.

        Principle 1 of the spec: a check that cannot run fails loudly instead of
        passing quietly. The precedent: the old contour's freshness check could not run
        against the API and "passed", masking blindness. Here the check is a real
        lookup of the executable; if argv is empty there is nothing to check, and that
        is a refusal rather than a skip.
        """
        for role, argv in (("critic", self.critic_argv), ("development", self.dev_argv)):
            if not argv:
                raise HarnessError(
                    f"The machine profile carries no {role} command",
                    cause=(
                        "an empty command cannot be checked for runnability — "
                        "the check would be a silent one"
                    ),
                    next_action=f"fill in the {role} command in {PROFILE_DIRNAME}/{PROFILE_FILENAME}",
                )
            exe = argv[0]
            path_ok = Path(exe).is_file() and os.access(exe, os.X_OK)
            # The lookup runs in THE environment the role really starts with: a profile env
            # can replace PATH, and "the shell sees codex" guarantees nothing (a finding of
            # round 10).
            effective_path = {**os.environ, **self.env}.get("PATH", os.defpath)
            if shutil.which(exe, path=effective_path) is None and not path_ok:
                raise HarnessError(
                    f"The executable of the {role} command was not found: {exe}",
                    cause="the profile is stale or the machine changed (a move, an update, PATH)",
                    next_action=(
                        "correct the path in the profile or install the tool; "
                        "then put what you found out back into the profile, not into someone else's report"
                    ),
                )


def profile_path(repo_root: Path) -> Path:
    return Path(repo_root) / PROFILE_DIRNAME / PROFILE_FILENAME


def load(repo_root: Path) -> MachineProfile:
    path = profile_path(repo_root)
    if not path.exists():
        raise HarnessError(
            f"The machine profile was not found: {path}",
            cause="the harness has not run on this machine yet, or the profile was not carried over",
            next_action=(
                "create the profile (review-harness profile-init) and fill in the launch commands; "
                "knowledge about launching lives on the launch path, not in someone else's reports"
            ),
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        profile = MachineProfile(**raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        # A partially written or hand-edited profile is no reason for a raw traceback:
        # the refusal names the file and the repair.
        raise HarnessError(
            f"The machine profile is corrupted or incomplete: {path}",
            cause=f"the file does not read as a profile: {exc}",
            next_action=(
                "correct the JSON after the profile-init pattern, or create the profile anew; "
                "fields: critic_argv, dev_argv, detection_window_sec, "
                "observed_max_gap_sec, observed_gap_note"
            ),
        ) from exc
    profile.validate()
    return profile


def save(repo_root: Path, profile: MachineProfile) -> Path:
    """The first-class way to put launch knowledge on the launch path."""
    profile.validate()
    path = profile_path(repo_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessError(
            f"Could not create the profile directory: {path.parent}",
            cause=f"a disk error: {exc}",
            next_action="check the permissions and the free space",
        ) from exc
    try:
        path.write_text(
            json.dumps(profile.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        raise HarnessError(
            f"Could not write the machine profile: {path}",
            cause=f"a disk error: {exc}",
            next_action="check the permissions and the free space, then repeat the save",
        ) from exc
    return path
