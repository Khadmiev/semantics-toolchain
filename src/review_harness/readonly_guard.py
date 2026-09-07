# SPDX-License-Identifier: Apache-2.0
"""The critic's read-only guard: a port of the old contour's hardened guard.

A ready solution of the project (the ready-solution rule, the operator's act of
2026-09-01): not a reinvention but a port of `assistant_memory/review/sandbox.py`
— a guard born of incident db5b243d (a writing critic, July 2026) and hardened by
four discovered bypasses (a subshell wrapper, shell metacharacters, a conflicting
duplicate flag, the long config form).

The rules are closed by default: ONLY a recognised ``codex exec`` invocation is
let through, and only when EVERY sandbox directive in it says read-only.
Anything unrecognised is refused. The single way past is an explicit break-glass
in the machine profile (``allow_unsafe_critic_sandbox``), and it is announced
loudly on every launch.

The second layer is attestation from the tool's own output: codex prints a header
for its run; a pass whose header says something other than read-only OR says
nothing is annulled — silence does not count as read-only (a rule ported from the
audience genre).
"""

from __future__ import annotations

import re

from .errors import HarnessError

_SANDBOX_LINE_RE = re.compile(r"^\s*sandbox:\s*(\S+)\s*$")


def parse_codex_output(text: str) -> tuple[str | None, str]:
    """Parse the output of codex exec by its REAL structure.

    The live format (checked against docs/audience/account_channel_probe/probe.log):
    a version line, a header block between dashed separator lines
    (workdir/model/provider/approval/sandbox/...), then a "user" section echoing the
    prompt and — after a separate "codex" line — the answer itself.

    Returns (the sandbox value from the header, or None; the substantive answer).
    Only the text after the "codex" marker counts as the answer: the prompt echo and
    the header are not an answer — a codex that printed a header and an echo but not
    one line of answer gave an EMPTY pass. Quotations of "sandbox: ..." inside the
    answer are left alone: the header is searched for only up to the second
    separator.
    """
    lines = text.splitlines()
    sep_idx = [
        i
        for i, line in enumerate(lines[:80])
        if len(line.strip()) >= 4 and set(line.strip()) == {"-"}
    ]
    header_end = sep_idx[1] + 1 if len(sep_idx) >= 2 else min(len(lines), 15)
    sandbox: str | None = None
    for line in lines[:header_end]:
        m = _SANDBOX_LINE_RE.match(line)
        if m:
            sandbox = m.group(1)
    for i in range(header_end, len(lines)):
        if lines[i].strip() == "codex":
            return sandbox, "\n".join(lines[i + 1 :])
    return sandbox, ""


def critic_argv_is_read_only(argv: list[str]) -> bool:
    """True only when argv is a codex exec assembled ENTIRELY from the allow-list.

    The lesson of three review rounds: a deny-list of flags loses to every codex
    update (--approve-for-me granted workspace-write past -s; -o/--output-last-message
    writes a file under an honest read-only sandbox). The class is closed by
    inversion: only what our own launch needs is permitted — any token outside the
    list, including flags of future versions, means refusal. The check is structural
    and covers the same tokens that are executed (the profile stores an argv list;
    there is no shell).

    The permitted form: codex exec, exactly one read-only sandbox pin
    (-s/--sandbox/-c sandbox_mode=), exactly one -m/--model whose value is strictly
    "{model}" (otherwise the explicitly chosen model is silently replaced by the
    user's config), optionally -C/--cd with a directory, and exactly one positional
    argument "-" — the prompt arrives as content through stdin.
    """
    if any(any(ch in t for ch in ";&|<>`\n\r") or "$(" in t for t in argv):
        # Defence in depth from the original: a legitimate template needs no
        # metacharacters; we fail loudly at the gate, not as a riddle in codex arguments.
        return False
    if len(argv) < 2:
        return False
    exe = argv[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
    if exe not in ("codex", "codex.exe") or argv[1].lower() != "exec":
        return False
    sandbox_pins = 0
    model_pins = 0
    positionals: list[str] = []
    i = 2
    while i < len(argv):
        t = argv[i]
        low = t.lower()
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if low in ("-s", "--sandbox"):
            if nxt != "read-only":
                return False
            sandbox_pins += 1
            i += 2
        elif low in ("-s=read-only", "--sandbox=read-only"):
            sandbox_pins += 1
            i += 1
        elif t == "-c" or low == "--config":  # case matters: -c is not -C
            if nxt != "sandbox_mode=read-only":
                return False
            sandbox_pins += 1
            i += 2
        elif low in ("-m", "--model"):
            if nxt != "{model}":
                return False
            model_pins += 1
            i += 2
        elif t == "-C" or low == "--cd":  # the working directory
            if nxt is None:
                return False
            i += 2
        elif t.startswith("-") and t != "-":
            return False  # an unknown flag — refused by construction
        else:
            positionals.append(t)
            i += 1
    # The prompt has one source, stdin ("-"): for codex exec a positional argument
    # IS the text of the instruction, and a file path is not an instruction — the
    # accompanying note is delivered as content through stdin, deterministically.
    return (
        sandbox_pins == 1
        and model_pins == 1
        and positionals == ["-"]
    )


def assert_critic_read_only(critic_argv: list[str], *, allow_unsafe: bool) -> None:
    """Refuse to start a critic without a structural read-only guarantee."""
    if allow_unsafe:
        return  # break-glass: announced loudly by the calling code
    if not critic_argv_is_read_only(critic_argv):
        raise HarnessError(
            "The critic command gives no structural read-only guarantee",
            cause=(
                "the critic never writes (the operator's decision; incident db5b243d) — "
                "only a recognised codex exec is let through, and only when every "
                f"sandbox directive says read-only; received: {critic_argv!r}"
            ),
            next_action=(
                "pin the critic command read-only (codex exec -s read-only ...) in the machine "
                "profile; break-glass is allow_unsafe_critic_sandbox alone, with a loud "
                "announcement on every launch"
            ),
        )


def assert_attested_read_only(pass_text: str, *, stream_hint: str) -> None:
    """Annul a pass whose header says something other than read-only, or says nothing.

    Silence does not count as read-only: a mode that was not declared is a mode that
    was not confirmed (a rule ported from the audience genre).
    """
    sandbox, _ = parse_codex_output(pass_text)
    if sandbox is None:
        raise HarnessError(
            "The critic pass is annulled: the tool did not attest its sandbox",
            cause=(
                "the output header carries no \"sandbox: ...\" line — silence does not count "
                "as read-only"
            ),
            next_action=(
                f"the received text is intact in {stream_hint}; check the codex version and call "
                "(the launch header must be printed) and restart the round"
            ),
        )
    if sandbox.lower() != "read-only":
        raise HarnessError(
            "The critic pass is annulled: by attestation the sandbox is not read-only",
            cause=f"the tool header declared sandbox: {sandbox}",
            next_action=(
                f"the text is intact in {stream_hint} but is not a pass; pin the critic "
                "command read-only and restart the round"
            ),
        )
