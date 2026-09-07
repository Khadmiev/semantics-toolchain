# SPDX-License-Identifier: Apache-2.0
"""Delivery of the launcher in an executable form.

The section of the spec on starting the critic. The precedent class "delivered
unexecutable": LF line endings in a cmd script for Windows (twice, hole
4d10aea3), an unescaped bracket in a cmd block (B.9-3). Hence three rules:

* line endings are chosen for the target platform rather than inherited from
  whoever wrote the file;
* the "bracket in cmd" class is closed TWICE: structurally — the generated
  launcher wraps its body in no bracketed block at all, so a lone ``)`` is
  harmless; and statically — a body line with an unescaped bracket is rejected at
  generation. Dynamically cmd does not catch this class: a live probe showed
  (2026-09-01) that cmd silently tolerates a stray ``)`` (rc=0), so "parse the
  body with a probe" is an unrunnable check for cmd and we do not promise it
  (principle 1 — honest static analysis beats a quiet probe);
* for sh the body is parsed for real: ``sh -n`` parses the whole file;
* the file's executability is checked AT DELIVERY by the real interpreter — the
  check is delivery's finish line, not a hope.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import HarnessError

PROBE_ENV = "REVIEW_HARNESS_PROBE"


def _has_unescaped_paren(line: str) -> bool:
    """A bracket with no caret before it is a candidate for breaking cmd's flow."""
    for i, ch in enumerate(line):
        if ch in "()" and (i == 0 or line[i - 1] != "^"):
            return True
    return False


def write_launcher(path: Path, body_lines: list[str], *, windows: bool) -> Path:
    """Write the launcher with the target platform's line endings and a probe branch.

    The body always sits in the part the interpreter parses: a probe run with the
    environment variable set does not execute the body, but the interpreter is
    obliged to parse it — syntax and escaping are checked by a real parse rather
    than by hope.
    """
    if windows:
        newline = "\r\n"
        for lineno, line in enumerate(body_lines, 1):
            if _has_unescaped_paren(line):
                raise HarnessError(
                    f"Line {lineno} of the launcher body carries an unescaped bracket",
                    cause=(
                        f"line: {line!r}; cmd silently tolerates a stray bracket and breaks the "
                        "flow instead of erroring — the \"delivered unexecutable\" class "
                        "(precedent B.9-3) is not caught by a dynamic probe"
                    ),
                    next_action="escape the brackets with a caret (^) or rewrite the line",
                )
        # The body lives at the top level, with NO bracketed blocks of ours: a lone
        # «)» standing on its own has no block it could close.
        lines = [
            "@echo off",
            f"if defined {PROBE_ENV} exit /b 0",
            *body_lines,
        ]
    else:
        newline = "\n"
        lines = [
            "#!/bin/sh",
            f'if [ -n "${{{PROBE_ENV}:-}}" ]; then exit 0; fi',
            *body_lines,
        ]
    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(newline.join(lines) + newline)
        if not windows:
            os.chmod(path, 0o755)
    except OSError as exc:
        raise HarnessError(
            f"Could not write the launcher: {path}",
            cause=f"a disk error: {exc}",
            next_action="check the permissions and the free space; the launcher is not delivered",
        ) from exc
    return path


def _run(cmd: list[str], env: dict, what: str, path: Path) -> None:
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise HarnessError(
            f"The probe of launcher {path} hung for more than 30 seconds ({what})",
            cause="the interpreter did not finish the probe run — the launcher is not delivered",
            next_action="find out what is blocking the interpreter and repeat the delivery",
        ) from exc
    except OSError as exc:
        raise HarnessError(
            f"Launcher {path} could not be started for a probe",
            cause=f"the OS refused to start it ({what}): {exc}",
            next_action="check the file permissions and the interpreter; the launcher is not delivered",
        ) from exc
    if result.returncode != 0:
        raise HarnessError(
            f"The probe of launcher {path} failed ({what}, code {result.returncode})",
            cause=(
                "the interpreter could not parse or execute the file — the \"delivered "
                "unexecutable\" class (line endings, syntax, escaping). "
                f"stderr: {result.stderr.strip()[:400] or '<empty>'}"
            ),
            next_action=(
                "the launcher is not delivered; fix the generation and repeat — "
                "do not run the payload"
            ),
        )


def probe(path: Path, *, windows: bool) -> None:
    """Check the delivered launcher's executability with the real interpreter.

    Windows: a probe run with an environment variable — a smoke check of the file
    (line endings, the header); the body is protected from the bracket class by
    the static check at generation. POSIX: first ``sh -n`` (a real parse of the
    whole file without executing it), then a probe run.
    """
    env = dict(os.environ)
    env[PROBE_ENV] = "1"
    if windows:
        _run(["cmd", "/c", str(path)], env, "a probe run", path)
    else:
        _run(["sh", "-n", str(path)], env, "a syntax parse", path)
        _run([str(path)], env, "a probe run", path)
