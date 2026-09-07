# SPDX-License-Identifier: Apache-2.0
"""Starting the critic and receiving its pass.

The section of the spec on starting the critic. The critic is an external LLM
session started by a command from the machine profile; the model is an explicit
argument. The output is streamed into a partial file of the run catalogue (what
was received does not live in the harness's memory alone), and silence longer
than the recognition window is death (see subproc).
"""

from __future__ import annotations

import re
from pathlib import Path

from . import readonly_guard
from .errors import HarnessError
from .machine_profile import MachineProfile
from .subproc import run_watched

# The model is an identifier, not a command: a value able to carry flags or
# separators must not reach argv (injection through a marker's value).
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def build_argv(profile: MachineProfile, *, model: str, launch_note: Path) -> list[str]:
    """Substitute the values into the critic's command template.

    The guard checks the TEMPLATE (markers in their places) rather than the
    substituted argv: substitution is deterministic and changes exactly two markers,
    so re-checking the result would reject our own legitimate command (the failed
    launch of round 4). The values are still checked AS values — the model must be an
    identifier, the accompanying note an existing file.
    """
    if not _MODEL_RE.match(model):
        raise HarnessError(
            f"The model value is not an identifier: {model!r}",
            cause="the model is substituted into the critic argv — flags and separators are forbidden in the value",
            next_action="pass a model name of the form gpt-5.6-sol",
        )
    if not launch_note.exists():
        raise HarnessError(
            f"The critic's accompanying note was not found: {launch_note}",
            cause="the path of a file that does not exist is substituted into argv",
            next_action="check the run catalogue: the file launch_critic.md is mandatory",
        )
    return [
        part.replace("{model}", model).replace("{launch_note}", str(launch_note))
        for part in profile.critic_argv
    ]


def run_critic(
    profile: MachineProfile,
    *,
    model: str,
    launch_note: Path,
    partial_path: Path,
    detection_window_sec: int,
    privacy_check=None,
) -> str:
    """Start the critic and return its pass in full."""
    # Check point 2: the guard stands at the launch itself as well (whatever way the
    # profile reached the disk) — but what it checks is the profile's TEMPLATE:
    # value substitution comes after and validates the values itself.
    readonly_guard.assert_critic_read_only(
        profile.critic_argv, allow_unsafe=profile.allow_unsafe_critic_sandbox
    )
    argv = build_argv(profile, model=model, launch_note=launch_note)
    # The accompanying note is delivered as CONTENT through stdin (the positional
    # "-"): for codex exec a positional argument is the text of the instruction, and
    # a file path is not an instruction (a finding of round 5). A template with
    # {launch_note} in argv stays legitimate only under break-glass (test roles).
    from .errors import read_text_contract

    input_text = (
        read_text_contract(launch_note, "the critic's accompanying note") if "-" in argv else None
    )
    if input_text is not None and any(
        line.strip() == "codex" for line in input_text.splitlines()
    ):
        # A separate "codex" line inside the accompanying note is forbidden BY
        # CONSTRUCTION (round 19): codex prints the prompt echo before the answer
        # marker, and in the echo such a line is indistinguishable from it — the parser
        # would take the rest of the echo for a real pass while the answer was empty.
        raise HarnessError(
            "The critic's accompanying note carries a separate line \"codex\"",
            cause="in the prompt echo it is indistinguishable from the codex exec answer marker",
            next_action=(
                "rephrase the note so that the word codex does not stand "
                "on a line of its own"
            ),
        )
    out, err = run_watched(
        argv,
        role="the critic",
        stream_path=partial_path,
        detection_window_sec=detection_window_sec,
        input_text=input_text,
        privacy_check=privacy_check,
        env_overrides=profile.env,
    )
    # Transcripts are glued STDERR-FIRST: the codex header is printed to stderr
    # (ready knowledge of the project — isolation.py of the audience genre);
    # attestation on stdout alone would annul every honest pass.
    text = err + out
    if not profile.allow_unsafe_critic_sandbox:
        # Layer 2: attestation from the tool's own header — a pass whose header says
        # something other than read-only, or says nothing, is annulled before the write.
        readonly_guard.assert_attested_read_only(text, stream_hint=str(partial_path))
        # Emptiness is measured on the substantive part — the text AFTER the "codex"
        # marker of the real output format: the header and the prompt echo are not an
        # answer (checked against the live probe.log).
        _, content = readonly_guard.parse_codex_output(text)
    else:
        content = text
    if not content.strip():
        raise HarnessError(
            "The critic finished successfully, but the pass is empty",
            cause=(
                "an empty pass is not \"zero findings\" but an absence of text; "
                "a live pass does not look like this"
            ),
            next_action="check the critic command in the machine profile: the output may not have gone to stdout",
        )
    return text
