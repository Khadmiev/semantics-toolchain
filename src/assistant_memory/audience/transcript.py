# SPDX-License-Identifier: Apache-2.0
"""Reading the provider's transcript — the one place that knows its shape.

SEPARATE FROM BOTH SIDES ON PURPOSE. The launcher derives these facts when a run finishes;
the crediting gate must RE-DERIVE them from the stored transcript rather than trust the
record, because a record is a claim by the side being checked and a hash only proves the
file is the one the record names — not that the record's fields describe it. Both therefore
import from here, and neither imports the other.
"""

from __future__ import annotations

import re

from assistant_memory.audience.records import SPEND_UNMEASURED

#: The provider prints a header block delimited by dashed lines, `key: value` inside. Its
#: contents are the strongest isolation evidence available, because the PROVIDER wrote
#: them, not us: `sandbox: read-only` and `approval: never` are facts about the launch that
#: no amount of prose-scanning could establish. Verified against the pilot's real
#: transcript (`docs/pilots/audience_review_2026-08/cold_round4_run.log`).
_HEADER_FENCE = "--------"

#: What the header must say for a blind run to be isolated at all.
REQUIRED_HEADER = {"sandbox": "read-only", "approval": "never"}

#: The provider's own spend, printed as two lines: `tokens used` then `14,376`. Absent →
#: the record says "not measured"; an estimate in its place would be indistinguishable
#: from a measurement when read back later.
_SPEND = re.compile(r"^tokens used\s*$\s*^([\d,\s]+)$", re.IGNORECASE | re.MULTILINE)

#: The runner's own version, printed as the transcript's first line. It belongs in the
#: record for the same reason the model version does: it decides what a run can do at all.
#: Found the hard way — this box carries THREE codex installs (0.130 on PATH, 0.145 pinned
#: by the critic's wrapper, 0.146 alongside), and a bare `codex` resolves to the oldest,
#: which refuses the configured model outright. The profile's path is configuration by
#: spec; the binary is configuration for exactly the same reason and was not treated so.
#:
#: ANCHORED TO THE VERY START, not to any line. Searched line-wise it could be satisfied by
#: a banner appearing inside the model's own answer, or inside an artefact the reader quoted
#: — and this repository now contains a transcript with exactly such a banner, kept as
#: evidence, so a deck about this project could quote it. A field claimed to be the
#: provider's own words has to come from where the provider writes them.
_RUNNER_VERSION = re.compile(r"\AOpenAI Codex v(\S+)")


#: What the RUNNER's own diagnostics look like, and the only thing allowed to precede its
#: banner. Measured 2026-08-05 on the first live run of the assembly:
#:
#:   2026-08-05T12:07:01.692597Z ERROR codex_models_manager::manager: failed to refresh …
#:
#: A SHAPE and not a list of known messages: a denylist of texts goes stale the first time
#: the runner adds one, and it goes stale silently. Anything else before the banner — prose,
#: a quoted artefact, a lookalike header — means the transcript is not the runner's own
#: opening, and there is no header. That is the finding this anchor was raised for, and
#: widening it to "skip whatever comes first" reopened it for exactly one test run.
_DIAGNOSTIC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z\s+(ERROR|WARN|WARNING|INFO|DEBUG|TRACE)\s+\S+"
)


def extract_runner_version(transcript: str) -> str | None:
    """The runner's version — from ITS banner, wherever the runner put it.

    Read through the same anchor as the header rather than from line one: the runner prints
    diagnostics before its banner (measured on the first live run), and a version that the
    record has and the transcript "does not" makes the gate refuse every honest pair.
    """
    lines = transcript.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or _DIAGNOSTIC.match(stripped):
            continue
        match = _RUNNER_VERSION.match(stripped)
        # The FIRST non-diagnostic line decides: if it is not the banner, the banner does not
        # open this transcript, and a version quoted further down is somebody's text — the
        # same rule as the header, for the same reason.
        return match.group(1) if match else None
    return None

#: NOW CALIBRATED AGAINST A REAL EXECUTING RUN, not against imagination. Measured
#: 2026-08-04 (evidence: docs/pilots/audience_review_2026-08/measured/executed_tool_call.log):
#: the provider prints the verb on ITS OWN LINE and the command on the next, then a result
#: line — `exec` / `"…powershell.exe" -Command '…' in D:\…` / ` succeeded in 200ms:`. The
#: pattern below matches that because ``\s+`` spans the newline; the result markers are
#: added as a second, independent shape so recognition does not hang on one of them.
#:
#: WHAT THIS SCAN IS, stated at its real strength (corrected 2026-08-04 after three review
#: rounds spent overstating it). It proves PRESENCE and never absence: it recognises the
#: shapes that have been OBSERVED, and an unobserved shape yields an empty list
#: indistinguishable from a clean run. The header does not close that gap either —
#: `read-only` is the policy applied WHEN model-generated commands execute (`codex exec
#: --help`), so it forbids writing, not executing.
#:
#: Absence is therefore NOT PROVABLE here, and by operator decision (2026-08-04) it does not
#: need to be. The blind reader is a coarse approximation of one specific human — his
#: language level, his past questions and his edits are already known — so full precision is
#: not the goal, some context leaking is accepted, and the residual channels are closed by
#: CHECKING the transcript rather than by proving a negative. Widening this pattern against
#: shapes nobody has seen would be guessing dressed as rigour, and building a proof of
#: absence would be defending a claim the design never made.
#:
#: So: a hit annuls the run. A miss establishes nothing, and nothing downstream is allowed to
#: read it as if it did.
_TOOL_CALL = re.compile(
    r"^(?:exec|shell|apply_patch|update_plan)\s+\S.*$|^\s*(?:succeeded|failed|exited) in \S+",
    re.MULTILINE,
)


def parse_header(transcript: str) -> dict[str, str]:
    """The provider's own header block as key → value (empty when there is no block).

    THE BLOCK IS THE PROVIDER'S BANNER AND THE FENCE IMMEDIATELY AFTER IT, and it must come
    before the model has said anything. Taken from "the first two fences anywhere in the
    text", the block could be supplied by a lookalike inside the model's own answer or inside
    an artefact it quoted, and the record would then carry `sandbox: read-only` that no
    provider ever wrote. That is the failure this is anchored against, and the anchor holds:
    the model's own text cannot precede the runner's banner in a real transcript.

    WHY THE ANCHOR IS NO LONGER "LINE ONE", corrected 2026-08-05 by the first live run of the
    assembly. The real runner prints its own diagnostics to the error stream BEFORE the
    banner ("failed to refresh available models: timeout ..."), so requiring the banner on the
    very first line read every honest run as headerless and annulled it. The three transcripts
    this rule was measured on happened to have no such lines — the sample was clean, and the
    rule was fitted to the sample rather than to the runner.

    So: lines before the banner are the RUNNER's own and are skipped; anything from the model
    is not, and a transcript where a model turn opens before the banner has no header. The
    fence must follow the banner immediately — a gap would put arbitrary text inside the one
    block the isolation claim rests on. Fail-closed throughout: no block means no header,
    which annuls the run rather than crediting it.
    """
    lines = transcript.splitlines()
    banner = next(
        (i for i, line in enumerate(lines) if _RUNNER_VERSION.match(line.strip())), None
    )
    if banner is None or banner + 2 >= len(lines):
        return {}
    # ONLY the runner's own diagnostics may precede its banner. Anything else there means
    # the banner is not the opening of a transcript — it is a lookalike inside somebody's
    # text, which is the whole failure this anchor exists to refuse.
    if any(line.strip() and not _DIAGNOSTIC.match(line.strip()) for line in lines[:banner]):
        return {}
    if lines[banner + 1].strip() != _HEADER_FENCE:
        return {}
    closing = next(
        (
            i
            for i, line in enumerate(lines[banner + 2 :], start=banner + 2)
            if line.strip() == _HEADER_FENCE
        ),
        None,
    )
    if closing is None:
        return {}
    lines = lines[banner:]
    closing -= banner
    header: dict[str, str] = {}
    for line in lines[2:closing]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        name, value = key.strip().lower(), value.strip()
        # A REPEATED KEY IS AMBIGUITY, NOT AN UPDATE. Taking the last value silently, a
        # header saying `sandbox: danger-full-access` and then `sandbox: read-only` parsed as
        # clean. A contradictory attestation is not an attestation, so the whole block is
        # refused — and a refused block means no header at all, which annuls the run.
        if name in header and header[name] != value:
            return {}
        header[name] = value
    return header


def header_violations(header: dict[str, str]) -> list[str]:
    """Ways the launch was NOT what the blind profile requires, per the provider's own words."""
    problems = []
    for key, expected in REQUIRED_HEADER.items():
        actual = header.get(key)
        if actual is None:
            problems.append(f"в шапке стенограммы нет поля «{key}» — режим запуска не подтверждён")
        elif actual != expected:
            problems.append(f"{key}: {actual!r}, а требуется {expected!r}")
    return problems


def extract_answer(transcript: str) -> tuple[str | None, list[str]]:
    """The model's final answer, cut out of the provider's transcript — or why it cannot be.

    Anchored to the OBSERVED shape (the pilot's real transcripts): the runner prints the
    prompt echo under a ``user`` marker line, the answer under a ``codex`` marker line, then
    a ``tokens used`` line with the spend — and repeats the final message after it. So the
    answer is what lies after the LAST ``codex`` marker, up to the spend line. Fail-closed
    with a reason, never a guess: a transcript without the marker is a transcript whose
    answer nobody can point at, and validating the whole transcript instead would hold the
    schema against the prompt echo — which CONTAINS the schema's own example.
    """
    lines = transcript.splitlines()
    markers = [i for i, line in enumerate(lines) if line.strip() == "codex"]
    if not markers:
        return None, [
            "в стенограмме нет секции ответа (маркер «codex») — ответ, на который нельзя "
            "указать, нельзя и валидировать"
        ]
    tail = lines[markers[-1] + 1 :]
    spend = next((i for i, line in enumerate(tail) if line.strip() == "tokens used"), None)
    answer = "\n".join(tail if spend is None else tail[:spend]).strip()
    if not answer:
        return None, ["секция ответа в стенограмме пуста"]
    return answer, []


def extract_tool_calls(transcript: str) -> tuple[str, ...]:
    return tuple(match.group(0).strip() for match in _TOOL_CALL.finditer(transcript))


def extract_spend(transcript: str) -> int | str:
    match = _SPEND.search(transcript)
    if not match:
        return SPEND_UNMEASURED
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else SPEND_UNMEASURED
