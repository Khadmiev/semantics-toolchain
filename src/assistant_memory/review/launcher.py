# SPDX-License-Identifier: Apache-2.0
"""Server-side launch-script rendering (B.9 C-3 / C-9) — the server renders, the client
copies verbatim and checks the hash.

Until B.9 this module was a local CLI: the agent assembled launches "by place", and the
launch debugging recurred every session (the quoting incidents, the stale alpha resolved
from PATH). That CLI is retired. The rendering logic now lives BEHIND the server endpoint
``GET /reviews/{id}/launcher``; this module is the server's rendering library, and the
rendered artifact is specified by the C-9 checklist:

1. Composition preserved from B.7 H-4: the script starts the stdlib-only supervisor,
   which starts the watcher; ping channels are baked in from the review's resolved
   configuration at render time.
2. Self-identifying by deterministic inputs only: the header names the profile version,
   the protocol template version, the review id and the expected host pair — no render
   time, no in-band hash.
3. Deterministic render, hash out of band: same (profile version, protocol template
   version, review snapshot) -> same bytes; the sha256 travels BESIDE the script.
4. Absolute everything: every executable and file path comes absolute from the profile;
   the script performs no PATH resolution and sets its environment explicitly.
5. No secret values: tokens are referenced by file path only.
6. Target-shell quoting with refusal: a value that cannot be quoted safely for the
   profile's recorded shell is refused at render time, never emitted mis-split.
7. The startup gate is the host check plus the same probes, a named refusal BEFORE any
   pass is spent; the same probes keep running before every pass (C-7).
8. The critic invocation line carries the frozen decisions: the profile's read-only
   sandbox form (D-6) with the model and effort frozen in the review snapshot (D-2).
9. Failure is named, never improvised around: no fallback launch path lives in a
   generated artifact — a fallback is a state decision (C-6) made by a human.
"""

import hashlib
import re
import shlex

from .errors import InstrumentValidationError
from .sandbox import codex_cmd_is_read_only, tokenize_codex_cmd

#: The version of the protocol-side template joined to the machine profile at render
#: time (C-3). Bumped with every change to the rendering below — the script header names
#: it, so a running or copied script traces back to its render inputs.
LAUNCH_TEMPLATE_VERSION = "B.14.1"

#: Relative (to the repo root) locations the service joins absolute at render time.
SUPERVISOR_REL = "scripts/review_supervisor.py"
CRITIC_PROMPT_REL = "docs/prompts/review_orchestration/critic.md"
#: B.11 Part E/F: the two end-of-cycle passes. Joined here for the SAME reason the graph
#: probe URL is baked from the profile — a launch flag that has to be remembered by hand is
#: a launch flag that gets forgotten, and this project has already measured that once (the
#: omitted probe URL that failed a live launch). A watcher started without these schedules
#: nothing for a review that declared the map role, says so once, and the operator finds out
#: by not receiving a document they were promised.
MAP_PROMPT_REL = "docs/prompts/review_orchestration/semantic_map.md"
RECONCILIATION_PROMPT_REL = "docs/prompts/review_orchestration/reconciliation.md"

#: Channels the watcher cannot deliver (agent-session capabilities); the supervisor's
#: crash ping must go to a channel the sidecar can actually reach. Mirrors
#: ``watcher.AGENT_DELIVERED_CHANNELS`` — this module deliberately does not import the
#: watcher (the render must not drag process machinery), and the mirror is asserted by a
#: test so the two cannot drift silently.
DEFAULT_AGENT_DELIVERED_CHANNELS = ("push",)


def resolve_ping_channels(
    resolved_ping: dict, agent_channels=DEFAULT_AGENT_DELIVERED_CHANNELS
) -> tuple[str, str]:
    """(crash-ping channel, fallback channel) for the supervisor.

    The supervisor's ping fires when the watcher is dead — which is precisely when the
    development side may be gone too, so it must not be addressed to a channel only that
    side can deliver. When the resolved primary is agent-delivered, the supervisor gets
    the fallback for both.
    """
    primary, fallback = resolved_ping.get("primary"), resolved_ping.get("fallback")
    if primary in agent_channels:
        return fallback, fallback
    return primary, fallback


#: Characters that make a CMD token need quoting. `"` is absent deliberately: an embedded
#: double quote cannot be quoted safely in a batch file, so a value containing one is
#: REFUSED rather than emitted in a form that would silently mis-split.
_CMD_NEEDS_QUOTES = set(' \t&|<>^()')


def quote_for(value: str, *, windows: bool) -> str:
    """Quote one token for the shell that will actually read it (C-9 clause 6).

    The first version used POSIX escaping inside a Windows batch template, which CMD does
    not treat as grouping at all — so any path with a space in it split into pieces and the
    generated launcher simply did not run (critic finding `launcher-shell-quoting`). Two
    shells, two rules, and the caller says which one it is emitting.
    """
    if not windows:
        return shlex.quote(value)
    if '"' in value:
        raise InstrumentValidationError(
            f"cannot safely quote {value!r} for a CMD launcher: an embedded double quote "
            "has no reliable escape in a batch file. Move the value somewhere a path can "
            "live without one (refused at render, C-9 clause 6)"
        )
    # QUOTES DO NOT STOP CMD FROM EXPANDING `%NAME%` — that happens before the argument is
    # ever handed over, inside double quotes as well, so a literal path containing percent
    # signs would silently arrive as something else (or as nothing). `%%` is the batch-file
    # escape for a literal percent, and this file IS a batch file.
    value = value.replace("%", "%%")
    return f'"{value}"' if any(c in _CMD_NEEDS_QUOTES for c in value) else value


def _join(root: str, *parts: str, windows: bool) -> str:
    """Join below an absolute profile path without touching the filesystem (the render
    runs on the server; the path is for the profile's host)."""
    sep = "\\" if windows else "/"
    cleaned = [root.rstrip("/\\")]
    cleaned += [p.strip("/\\").replace("/", sep) for p in parts]
    return sep.join(cleaned)


def compose_critic_cmd(content: dict, *, model: str, effort: str | None) -> str:
    """The critic invocation line, carrying the frozen decisions (C-9 clause 8).

    The profile's sandbox form (validated read-only at version write, D-6) is extended
    with the model and effort FROZEN in the review snapshot (D-2) — and the composed line
    is re-validated: a script whose invocation line disagrees with the read-only contract
    is a render defect, not a tolerable drift.
    """
    base = content.get("codex_cmd")
    if not isinstance(base, str) or not base.strip():
        raise InstrumentValidationError("profile content has no `codex_cmd` to render")
    tokens = shlex.split(base, posix=True)
    extra = ["-m", model]
    if effort is not None:
        extra += ["-c", f"model_reasoning_effort={effort}"]
    # The stdin marker / prompt-file placeholder stays LAST: flags are inserted before
    # it, so the composed line never depends on the CLI tolerating flags after the
    # positional prompt argument.
    if tokens and tokens[-1] in ("-", "{prompt_file}"):
        tokens = tokens[:-1] + extra + tokens[-1:]
    else:
        tokens += extra
    # Canonical POSIX serialization for EVERY token — the same contract the parser on
    # both sides (shlex) reads back. The earlier conditional quoting (space / double
    # quote only) let an apostrophe through unquoted, and the composed line stopped
    # parsing (round 3, finding composed-critic-command-loses-apostrophe-tokenization).
    composed = " ".join(shlex.quote(t) for t in tokens)
    if not codex_cmd_is_read_only(composed):
        raise InstrumentValidationError(
            "render defect: the composed critic invocation does not validate read-only "
            f"(D-6/C-9 clause 8): {composed!r}"
        )
    return composed


def build_watcher_cmd(
    *,
    windows: bool,
    python: str,
    base_url: str,
    review_id: str,
    token_file: str,
    critic_prompt: str,
    map_prompt: str,
    reconciliation_prompt: str,
    codex_cmd: str,
    cwd: str,
    probes: list[dict],
    projection_root: str,
    extra: list[str] = (),
) -> str:
    parts = [
        python, "-m", "assistant_memory.review.watcher",
        "--base-url", base_url,
        "--review-id", review_id,
        "--token-file", token_file,
        "--critic-prompt", critic_prompt,
        # B.11 E-10/F-1: the end-of-cycle passes. Always passed; whether either runs is
        # the REVIEW's frozen declaration, read from its config, never a launch flag.
        "--map-prompt", map_prompt,
        "--reconciliation-prompt", reconciliation_prompt,
        "--codex-cmd", codex_cmd,
        "--cwd", cwd,
        # B.8 F-1 ground fail-closed: a spec review without the graph probe refuses its
        # seeing pass, so the probe URL is baked from the profile's base_url — never
        # remembered by hand (the measured omission that failed a live launch).
        "--graph-probe-url", f"{base_url.rstrip('/')}/health/db",
        # B.9 A-1/A-4: the profile's out-of-tree projection root, baked from machine
        # content — the watcher renders the critic's file projection under it.
        "--projection-root", projection_root,
        *extra,
    ]
    # C-7/C-9 clause 7: the SAME probes the startup gate ran keep running before every
    # pass — the startup gate is not a second probe set.
    for probe in probes:
        parts += ["--profile-probe", f"{probe['name']}::{probe['cmd']}"]
    return " ".join(quote_for(p, windows=windows) for p in parts)


_CMD_TEMPLATE = """@echo off
setlocal DisableDelayedExpansion
rem Delayed expansion is DISABLED for the whole script body (round 5, finding
rem cmd-delayed-expansion-not-refused): under cmd /V:ON a profile value containing
rem !NAME! would otherwise expand and the executed argv could diverge from the proven
rem profile. With it off, `!` is an ordinary character - nothing extra is refused.
rem GENERATED by the review service - copy verbatim, verify the sidecar hash, run.
rem Never edit: improvisation over a proven launch is forbidden (B.9 C-6); a broken
rem environment is a devaluation observation, not a reason to patch this file.
rem profile_version_id: {version_id}
rem profile_version: {version_n}
rem proven_tool_versions: {tool_versions}
rem protocol_template: {template_version}
rem review_id: {review_id}
rem expected_host: {hostname}
rem expected_user: {username}
rem --- startup gate (C-9 clause 7): host pair + the profile's probes, BEFORE any pass --
if /i not "%COMPUTERNAME%"=="{hostname}" goto :mislaunch
if /i not "%USERNAME%"=="{username}" goto :mislaunch
rem --- bootstrap stage one FIRST (round 9, finding b14-profile-probes-follow-subject-cwd):
rem the machine-scoped probes below run anchored to the MACHINERY root, never to
rem whatever directory the operator launched from.
cd /d {machinery_root}
if errorlevel 1 (
    echo [launch-refusal] cannot enter machinery_root {machinery_root} - the machine-scoped probes below would run against a foreign tree and their result would be ascribed to the profile; fix the machinery checkout or the profile ^(Task dea66085^) 1>&2
    exit /b 5
)
{probe_lines}
rem --- subject preflight (B.14 A-4): per-review, render-side - distinct from the
rem machine-scoped probes above. A failure here names the review's subject, never
rem the profile: it devalues nothing.
call git -C {subject_root} rev-parse --git-dir >nul 2>&1
if errorlevel 1 (
    echo [launch-refusal] subject preflight failed - the review's subject_root is not a usable git worktree: fix the subject checkout or the review's declared subject_root; this never devalues the profile ^(B.14 A-4^) 1>&2
    exit /b 4
)
rem --- bootstrap stage two (B.14 A-4): import base and the supervisor invocation
rem derive from machinery_root (the cd happened before the probes); subject_root
rem reaches the launch only as the watcher's subject-cwd argument.
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
{python} {supervisor} --base-url {base_url} --review-id {review_id_q} --token-file {token_file} --ping-channel {ping_channel} --fallback-channel {fallback_channel} -- {watcher_cmd}
exit /b %ERRORLEVEL%
:mislaunch
echo [launch-refusal] rendered for {hostname}\\{username}, running as %COMPUTERNAME%\\%USERNAME% - a MISLAUNCH (launched outside its environment, C-6): record a mislaunch observation; this never devalues the source profile 1>&2
exit /b 2
"""

_CMD_PROBE_TEMPLATE = """call {cmd} >nul 2>&1
if errorlevel 1 (
    echo [launch-refusal] probe {name} failed on profile version {version_n} - the environment diverged from the proven state: record a devaluation observation and walk the preparation path ^(C-6^) 1>&2
    exit /b 3
)"""

_POSIX_TEMPLATE = """#!/bin/sh
# GENERATED by the review service - copy verbatim, verify the sidecar hash, run.
# Never edit: improvisation over a proven launch is forbidden (B.9 C-6); a broken
# environment is a devaluation observation, not a reason to patch this file.
# profile_version_id: {version_id}
# profile_version: {version_n}
# proven_tool_versions: {tool_versions}
# protocol_template: {template_version}
# review_id: {review_id}
# expected_host: {hostname}
# expected_user: {username}
# --- startup gate (C-9 clause 7): host pair + the profile's probes, BEFORE any pass ---
if [ "$(hostname)" != {hostname_q} ] || [ "$(id -un)" != {username_q} ]; then
    echo "[launch-refusal] rendered for {hostname}/{username}, running as $(hostname)/$(id -un) - a MISLAUNCH (launched outside its environment, C-6): record a mislaunch observation; this never devalues the source profile" >&2
    exit 2
fi
# --- bootstrap stage one FIRST (round 9, finding b14-profile-probes-follow-subject-cwd):
# the machine-scoped probes below run anchored to the MACHINERY root, never to
# whatever directory the operator launched from.
cd {machinery_root} || exit 1
{probe_lines}
# --- subject preflight (B.14 A-4): per-review, render-side - distinct from the
# machine-scoped probes above. A failure names the review's subject, never the
# profile: it devalues nothing.
if ! git -C {subject_root} rev-parse --git-dir >/dev/null 2>&1; then
    echo "[launch-refusal] subject preflight failed - the review's subject_root is not a usable git worktree: fix the subject checkout or the review's declared subject_root; this never devalues the profile (B.14 A-4)" >&2
    exit 4
fi
# --- bootstrap stage two (B.14 A-4): import base and the supervisor invocation
# derive from machinery_root (the cd happened before the probes); subject_root
# reaches the launch only as the watcher's subject-cwd argument.
export PYTHONPATH=src
export PYTHONIOENCODING=utf-8
exec {python} {supervisor} --base-url {base_url} --review-id {review_id_q} --token-file {token_file} --ping-channel {ping_channel} --fallback-channel {fallback_channel} -- {watcher_cmd}
"""

_POSIX_PROBE_TEMPLATE = """if ! {cmd} >/dev/null 2>&1; then
    echo "[launch-refusal] probe {name} failed on profile version {version_n} - the environment diverged from the proven state: record a devaluation observation and walk the preparation path (C-6)" >&2
    exit 3
fi"""


def _render_tool_versions(probe_evidence: dict | None) -> str:
    """The proven tool versions as ONE header-safe line (B.12 D-2).

    Sorted, so the render stays deterministic — clause 3 of the contract is that the same
    inputs produce the same bytes, and a dict's iteration order is not an input.

    A version stored before D-2 carries none, and the line says exactly that rather than
    printing an empty value: "not recorded" is a fact about the profile a reader can act
    on, while a blank reads as a rendering bug.
    """
    versions = (probe_evidence or {}).get("tool_versions")
    if not isinstance(versions, dict) or not versions:
        return "(not recorded — this profile version predates B.12 D-2)"
    # THE RENDERER DOES NOT TRUST THE STORE (finding
    # `b12-tool-version-contract-incomplete`, round 1; the NAME in round 2; the GRAMMAR in
    # round 3, `b12-tool-version-shell-metachar-still-unsafe`). The write path now refuses
    # names outside the closed tool set and values outside the shell-inert charset, but
    # this function turns stored bytes into script text, and "the other side already
    # checked" is how a guarantee stops being one: a profile version written before those
    # checks, or by anything else, still renders here. The gate is the CONSUMER'S grammar,
    # not printability — rounds 1-2 checked "one printable line", and `&` is printable
    # while separating commands even on a CMD rem line. Both halves of the pair land on
    # the same header line, so both get the same charset gate; a half outside it is
    # replaced by a named refusal, and no line of the script comes from the store.
    _SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
    _SAFE_VALUE = re.compile(r"^[A-Za-z0-9._+-]+$")

    # fullmatch, not match: `$` also matches before a terminal LF, and a value of
    # "0.147\n" would push everything after it onto a new executable script line
    # (finding `b12-shell-charset-final-lf-gap`, sol round 4).
    def _one_line(k: object, v: object) -> str:
        if not isinstance(k, str) or not _SAFE_NAME.fullmatch(k):
            return "<unrenderable tool name: outside [A-Za-z0-9._-]>"
        if not isinstance(v, str) or not _SAFE_VALUE.fullmatch(v):
            return f"{k}=<unrenderable: outside the shell-inert charset [A-Za-z0-9._+-]>"
        return f"{k}={v}"

    return ", ".join(_one_line(k, versions[k]) for k in sorted(versions, key=str))


def render_launch_script(
    *,
    review_id: str,
    snapshot: dict,
    profile_key: dict,
    version_id: str,
    version_n: int,
    content: dict,
    resolved_ping: dict,
    probe_evidence: dict | None = None,
    agent_channels=DEFAULT_AGENT_DELIVERED_CHANNELS,
) -> tuple[str, str]:
    """Render the launch script for one review; returns ``(script, sha256_hex)``.

    Deterministic over (profile version, protocol template version, review snapshot +
    its resolved channel configuration): no timestamps, no in-band digest (C-9 clauses
    2-3). The digest is computed over exactly the emitted bytes and returned BESIDE the
    script; the endpoint hands both to the client, which stores the hash as a sidecar.
    """
    engine = profile_key["engine"]
    if engine != "codex":
        raise InstrumentValidationError(
            f"no render form is implemented for engine {engine!r} yet — connecting a new "
            "engine walks the preparation path (C-6): its profile shape, model list and "
            "render form arrive together as a reviewed change"
        )
    # Render-side re-check of the write-time whitelist (defense in depth): these values
    # sit in shell text OUTSIDE the quoter, where a quote or `$( )` would break out —
    # a violation here is a render defect, refused, never emitted (round 2, finding
    # rendered-profile-values-are-shell-injectable).
    from .instruments import validate_script_identifier

    validate_script_identifier(profile_key["hostname"], what="render host `hostname`")
    validate_script_identifier(profile_key["username"], what="render host `username`")
    for p in content["probes"]:
        validate_script_identifier(p["name"], what="render probe name")
    windows = content["shell"] == "cmd"
    critic = snapshot["critic"]
    critic_cmd = compose_critic_cmd(content, model=critic["model"], effort=critic.get("effort"))

    # B.14 A-4/A-5 — the two roots, each with its exhaustive consumer list. From the
    # MACHINERY root (legacy `repo_root` honored on stored pre-split versions): the
    # supervisor path, the three prompt paths, and bootstrap stage one (cd, PYTHONPATH,
    # supervisor invocation — in the templates). From the SUBJECT root: the watcher's
    # subject-cwd argument (its ONLY carrier into the launch) and the render-side
    # subject preflight in the templates. A review created before B.14 carries no
    # subject_root in its snapshot and finishes under its own contract (spec
    # constraint 3): subject = machinery, exactly the old behavior.
    from .instruments import machinery_root_of

    machinery_root = machinery_root_of(content)
    subject_root = snapshot.get("subject_root") or machinery_root
    python = content["python"]
    token_file = _join(
        content["token_dir"], f"review_{review_id[:8]}", "critic.token", windows=windows
    )
    supervisor = _join(machinery_root, SUPERVISOR_REL, windows=windows)
    critic_prompt = _join(machinery_root, CRITIC_PROMPT_REL, windows=windows)
    map_prompt = _join(machinery_root, MAP_PROMPT_REL, windows=windows)
    reconciliation_prompt = _join(machinery_root, RECONCILIATION_PROMPT_REL, windows=windows)
    ping_channel, fallback_channel = resolve_ping_channels(resolved_ping, agent_channels)

    watcher_cmd = build_watcher_cmd(
        windows=windows,
        python=python,
        base_url=content["base_url"],
        review_id=review_id,
        token_file=token_file,
        critic_prompt=critic_prompt,
        map_prompt=map_prompt,
        reconciliation_prompt=reconciliation_prompt,
        codex_cmd=critic_cmd,
        cwd=subject_root,
        probes=content["probes"],
        projection_root=content["projection_root"],
    )

    probe_template = _CMD_PROBE_TEMPLATE if windows else _POSIX_PROBE_TEMPLATE
    # The probe is NOT interpolated raw: it is tokenized with the SAME tokenization the
    # watcher uses and re-quoted for the target shell, so the startup-gate line and the
    # per-pass argv are identical by construction (finding probe-shell-semantics-diverge;
    # validation at profile write already refused expansions and shell operators).
    def _probe_line(p: dict) -> str:
        tokens = tokenize_codex_cmd(p["cmd"])
        if not tokens:
            raise InstrumentValidationError(
                f"render defect: probe {p['name']!r} does not tokenize — the profile "
                "version should have refused it at write time"
            )
        requoted = " ".join(quote_for(t, windows=windows) for t in tokens)
        return probe_template.format(cmd=requoted, name=p["name"], version_n=version_n)

    probe_lines = "\n".join(_probe_line(p) for p in content["probes"])
    template = _CMD_TEMPLATE if windows else _POSIX_TEMPLATE
    q = lambda v: quote_for(v, windows=windows)  # noqa: E731 - one-line shell quoting
    script = template.format(
        version_id=version_id,
        version_n=version_n,
        # B.12 D-2: the script NAMES the tool versions its probes were proven on. Clause 2
        # of the render contract is that the artifact is self-identifying by deterministic
        # inputs only — and until now the one input that decides whether the proof still
        # holds was missing from it. A running script that says «proven on codex 0.145»
        # while the box has 0.147 is the difference between a puzzle and a fact; the
        # measured incident (0.145→0.147 silently breaking the sandbox) cost a session.
        # Rendered rather than checked here: the comparison is the freshness check's, which
        # runs BEFORE a review is created, and duplicating it in the startup gate would be
        # a second implementation of one rule.
        tool_versions=_render_tool_versions(probe_evidence),
        template_version=LAUNCH_TEMPLATE_VERSION,
        review_id=review_id,
        review_id_q=q(review_id),
        hostname=profile_key["hostname"],
        username=profile_key["username"],
        hostname_q=q(profile_key["hostname"]) if not windows else "",
        username_q=q(profile_key["username"]) if not windows else "",
        probe_lines=probe_lines,
        machinery_root=q(machinery_root),
        subject_root=q(subject_root),
        python=q(python),
        supervisor=q(supervisor),
        base_url=q(content["base_url"]),
        token_file=q(token_file),
        ping_channel=q(ping_channel),
        fallback_channel=q(fallback_channel),
        watcher_cmd=watcher_cmd,
    )
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    return script, digest
