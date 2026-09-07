# SPDX-License-Identifier: Apache-2.0
"""The harness command line.

The v1 channels: launching and the gates are the terminal (a working decision,
named to the operator in the plan of 2026-09-01). The commands are deliberately
few — the harness breeds no formats and issues no verdicts (the subtraction
section of the spec).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import cycle, machine_profile
from .errors import HarnessError, read_text_contract
from .runcat import RunCatalog


def _repo_root() -> Path:
    here = Path.cwd()
    for candidate in [here, *here.parents]:
        if (candidate / ".git").exists():
            return candidate
    raise HarnessError(
        "The repository root was not found",
        cause="the harness is started from a working copy, and the current directory is not one",
        next_action="go to the project repository and repeat",
    )


def main(argv: list[str] | None = None) -> int:
    # The Windows console defaults to cp1252: the harness's Russian output (its
    # whole language) would die with UnicodeEncodeError on the very first message.
    # Found by the first live deploy probe on 2026-09-02 — exactly the layer that
    # nobody had ever run. Errors are not swallowed: an unprintable byte is
    # replaced rather than killing a command whose meaning is already done.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass  # a non-standard sink (a test, a pipe) — print as is
    parser = argparse.ArgumentParser(prog="review-harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create a run catalogue")
    p_create.add_argument("run_dir")
    p_create.add_argument("--model", required=True, help="the critic model — an explicit input of the review")
    p_create.add_argument("--artifact", action="append", required=True, dest="artifacts")
    p_create.add_argument("--description", required=True, help="the task description file")
    p_create.add_argument("--launch-note", required=True, help="the file of the critic's accompanying note")

    p_round = sub.add_parser("round", help="run one round")
    p_round.add_argument("run_dir")

    p_cycle = sub.add_parser(
        "cycle",
        help="run the cycle automatically: an edit of the artifact is the next round",
    )
    p_cycle.add_argument("run_dir")

    p_status = sub.add_parser("status", help="the state of the run, from its catalogue")
    p_status.add_argument("run_dir")

    sub.add_parser("profile-check", help="a runnable check of the machine profile")

    sub.add_parser(
        "profile-init",
        help="create a machine-profile stub on the launch path (fill it in by hand)",
    )

    args = parser.parse_args(argv)
    try:
        if args.cmd == "create":
            RunCatalog.create(
                Path(args.run_dir),
                artifact_paths=args.artifacts,
                critic_model=args.model,
                description_text=read_text_contract(
                    Path(args.description), "the description file"
                ),
                launch_note_text=read_text_contract(
                    Path(args.launch_note), "the accompanying note file"
                ),
            )
            print(f"Run created: {args.run_dir}")
        elif args.cmd == "round":
            n = cycle.one_round(_repo_root(), Path(args.run_dir))
            print(f"Round {n} is done; the pass and the outcomes are in the run catalogue.")
        elif args.cmd == "cycle":
            n = cycle.run_cycle(_repo_root(), Path(args.run_dir))
            print(
                f"The cycle stopped after round {n}: the artifact did not change — "
                "the decision is the operator's (the critic pass is in the catalogue)."
            )
        elif args.cmd == "status":
            state = RunCatalog(Path(args.run_dir)).restore()
            print(
                f"Run {state.run_dir}\n"
                f"  Critic model: {state.critic_model}\n"
                f"  Artifacts: {', '.join(state.artifact_paths)}\n"
                f"  Rounds with a pass: {state.rounds_with_pass}\n"
                f"  Rounds with outcomes: {state.rounds_with_outcomes}\n"
                f"  Current round: {state.current_round}"
            )
        elif args.cmd == "profile-check":
            profile = machine_profile.load(_repo_root())
            profile.check_executables()
            print("The machine profile is runnable: the critic and development commands were found.")
        elif args.cmd == "profile-init":
            root = _repo_root()
            path = machine_profile.profile_path(root)
            if path.exists():
                raise HarnessError(
                    f"The machine profile already exists: {path}",
                    cause="a stub laid over a live profile would erase the launch knowledge",
                    next_action="edit the existing file; profile-init is for an empty place only",
                )
            # The stub goes straight into the SAFE allow-list form: codex exec, a
            # read-only pin, the model as the value of -m, the prompt through stdin "-".
            # The old form with {launch_note} did not pass our own guard.
            template = machine_profile.MachineProfile(
                critic_argv=["codex", "exec", "-s", "read-only", "-m", "{model}", "-"],
                dev_argv=["<the development executable>", "{run_dir}", "{round}"],
                detection_window_sec=300,
                observed_max_gap_sec=300,
                observed_gap_note="<where and when the longest stall of this environment was observed>",
            )
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(template.__dict__, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise HarnessError(
                    f"Could not write the profile stub: {path}",
                    cause=f"a disk error: {exc}",
                    next_action="check the permissions and the free space",
                ) from exc
            print(
                f"Profile stub created: {path}\n"
                "Fill in the commands and the observed stall, then run profile-check."
            )
    except HarnessError as err:
        print(err.render(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
