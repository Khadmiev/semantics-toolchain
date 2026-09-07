# SPDX-License-Identifier: Apache-2.0
"""Harness refusals: each names its cause and its next action.

Principle 3 of the spec (docs/design/2026-09-01_review_harness_spec.md): what
costs a day of diagnosis is not the refusal itself but a line like "The
directory name is invalid" that suggests nothing. So a harness refusal has no
constructor without a cause and a next action — the type does not let you
economise on them.
"""

from __future__ import annotations


class HarnessError(Exception):
    """A harness refusal. Mandatory parts: what happened, why, what to do.

    ``cause`` — why it happened, as far as it is known; when it is not known, that
    is what it says ("the cause was not established: ..."), but the field is never
    empty.
    ``next_action`` — a concrete action that unblocks the human or the agent.
    """

    def __init__(self, what: str, *, cause: str, next_action: str) -> None:
        if not what.strip() or not cause.strip() or not next_action.strip():
            # A refusal without a cause or a next action is itself a defect of the
            # harness (the refusals section of the spec). We catch it in the constructor.
            raise ValueError(
                "HarnessError needs a non-empty what, cause and next_action: "
                "a refusal with no cause and no next action is forbidden by the spec"
            )
        self.what = what.strip()
        self.cause = cause.strip()
        self.next_action = next_action.strip()
        super().__init__(self.render())

    def render(self) -> str:
        return (
            f"{self.what}\n"
            f"  Cause: {self.cause}\n"
            f"  What to do: {self.next_action}"
        )


def read_text_contract(path, what: str) -> str:
    """Read a text file under the refusal contract.

    A disk error AND a broken encoding both become a harness refusal with a cause
    and a next action, rather than a raw OSError/UnicodeDecodeError reaching the
    CLI.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HarnessError(
            f"Could not read {what}: {path}",
            cause=f"a disk error or a broken encoding: {exc}",
            next_action="check the file, its permissions and the disk; repeat the step",
        ) from exc


def read_bytes_contract(path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HarnessError(
            f"Could not read {what}: {path}",
            cause=f"a disk error: {exc}",
            next_action="check the file, its permissions and the disk; repeat the step",
        ) from exc
