# SPDX-License-Identifier: Apache-2.0
"""The critic read-only sandbox guard — ONE implementation, two consumers.

The critic must NEVER write (Incident db5b243d, July 2026). The watcher refuses to
launch an unsafe ``--codex-cmd`` at startup, and since B.9 Part C the same check runs at
profile-version WRITE time (D-6): the sandbox invocation form stored inside a launch
profile is validated against the read-only contract before the version is ever accepted,
so the configuration surface cannot quietly re-open a closed security decision. Moved
here out of ``watcher.py`` so the repository side can validate without importing the
watcher's process machinery; the watcher re-imports these names.
"""

import shlex


def tokenize_codex_cmd(codex_cmd: str) -> list[str] | None:
    """Shlex-split the critic command into argv, or None if it will not parse. The ONE
    tokenization shared by the read-only guard and the invoker, so what the guard validates
    is exactly what runs — no shell re-parse can reintroduce a divergence between them."""
    try:
        return shlex.split(codex_cmd, posix=True)
    except ValueError:
        return None


def codex_cmd_is_read_only(codex_cmd: str) -> bool:
    """True iff the template is a recognized ``codex exec`` invocation pinned read-only.

    The critic must NEVER write (Incident db5b243d); read-only is what the harness uses to
    deny every critic write — filesystem and graph/plugin MCP write-tools alike, independent
    of ``approval_policy`` (verified 2026-07-20). This is a STRUCTURAL check, not a substring
    match (finding critic-readonly-substring-wrapper-bypass: a wrapper or quoted argument that
    merely CONTAINS ``-s read-only`` must not pass). The command must: tokenize; invoke the
    ``codex`` binary with the ``exec`` subcommand; carry no danger/bypass escape; and pin the
    sandbox as ADJACENT tokens — ``-s read-only`` / ``--sandbox read-only`` / their ``=`` forms,
    or the config form ``-c``/``--config`` ``sandbox_mode=read-only`` (space- or ``=``-joined;
    ``resume`` uses this since it has no ``-s``). EVERY sandbox-setting directive must say
    read-only: a command that ALSO sets a conflicting non-read-only value (a later/duplicate
    ``-s`` or ``sandbox_mode=``, which could override the safe pin) is refused, not just the
    ones with no safe pin at all. The invoker runs the validated argv with ``shell=False``
    (see ``codex_invoker``), so no shell expands or chains anything at execution; the shell
    control-character rejection here (``;`` ``&`` ``|`` ``<`` ``>`` a backtick or ``$(``, a
    newline) is retained as DEFENSE-IN-DEPTH — it keeps this guard correct if execution ever
    regressed to a shell, and fails such templates loudly at the gate instead of as puzzling
    codex arg errors. Anything else — an unrecognized wrapper, a stray ``read-only`` inside a
    quoted argument, a non-``exec`` subcommand, an unparseable string — is refused (errs
    closed); the explicit ``--allow-unsafe-sandbox`` break-glass is the only way past."""
    # Reject shell control characters outright. This is now DEFENSE-IN-DEPTH: codex_invoker
    # runs the tokenized argv with shell=False, so `;`, `&`, `|`, redirection, substitution or
    # a newline already reach codex as literal args and cannot chain/expand (findings
    # critic-readonly-shell-metachar-bypass / -shell-expansion-bypass). Kept so the guard stays
    # correct even if execution ever regressed to a shell, and so such commands fail loudly at
    # the gate rather than as puzzling codex arg errors. None appear in a legitimate template.
    if any(ch in codex_cmd for ch in ";&|<>`\n\r") or "$(" in codex_cmd:
        return False
    tokens = tokenize_codex_cmd(codex_cmd)
    if tokens is None:
        return False  # unbalanced quotes etc. -> err closed
    if len(tokens) < 2:
        return False
    low = [t.lower() for t in tokens]
    exe = low[0].replace("\\", "/").rsplit("/", 1)[-1]
    if exe not in ("codex", "codex.exe") or low[1] != "exec":
        return False
    if any("danger-full-access" in t or "dangerously-bypass" in t for t in low):
        return False
    # Collect EVERY sandbox-setting directive and require that they ALL say read-only. Checking
    # for the mere PRESENCE of one safe pin is not enough (finding
    # critic-readonly-conflicting-sandbox-override): a later or duplicate sandbox arg with a
    # different value could override the safe one, so any non-read-only sandbox value is
    # refused even beside a read-only pin. Both config spellings are covered — `-c` and its
    # long form `--config`, space and `=` joined (finding critic-readonly-config-long-form-override).
    config_flags = ("-c", "--config")
    sandbox_values: list[str] = []
    i = 0
    while i < len(low):
        t, nxt = low[i], (low[i + 1] if i + 1 < len(low) else "")
        if t in ("-s", "--sandbox"):
            sandbox_values.append(nxt)
            i += 2
        elif t.startswith("-s=") or t.startswith("--sandbox="):
            sandbox_values.append(t.split("=", 1)[1])
            i += 1
        elif t in config_flags and nxt.startswith("sandbox_mode="):
            sandbox_values.append(nxt.split("=", 1)[1])
            i += 2
        elif t.startswith(("-c=", "--config=")) and t.split("=", 1)[1].startswith("sandbox_mode="):
            sandbox_values.append(t.split("sandbox_mode=", 1)[1])
            i += 1
        else:
            i += 1
    return bool(sandbox_values) and all(v == "read-only" for v in sandbox_values)
