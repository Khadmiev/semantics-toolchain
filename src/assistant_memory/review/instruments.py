# SPDX-License-Identifier: Apache-2.0
"""B.9 Parts C+D — launch profiles, engine model lists, frozen review instruments.

Spec docs/design/2026-08-16_review_loop_next_spec.md. Three stores and one gate:

- **Launch profiles** (C-1..C-7): the machine part of a critic launch, keyed
  host × engine where host = OS hostname + OS username pair. Immutable versions with
  probe evidence; the active version is a pointer; devaluation is a durable state
  entered by observation (never by the agent's opinion), and a launch outside the
  profile's own environment is a ``mislaunch`` record that never devalues.
- **Engine model lists** (D-4): *what to choose from*, apart from *how to launch*.
  Entries carry the engine-local invocation alias, the provider's OWN model id, the
  operator-entered provider/family lineage facts, and the supported effort domain.
- **Instrument defaults** (D-5): a two-level PAIR map (global + per-genre overrides) in
  the same immutable-versions-plus-pointer shape; every version is an explicit operator
  act (verbatim quote + a graph Decision ref validated to RESOLVE in-process).
- **The creation gate** (C-5/D-1/D-3): ``freeze_instruments`` resolves the requested (or
  defaulted) instruments, refuses with a ROUTE when the ground is missing, computes the
  graduated independence step, enforces the self-check waiver, and returns the snapshot
  that ``create_review`` freezes into ``review.config`` — nothing changes it mid-review.

Transaction contract as everywhere in this package: functions ``flush``, never
``commit``; the caller owns the transaction.
"""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import Node
from ..models.review import (
    PROFILE_OBSERVATIONS,
    EngineModel,
    InstrumentDefault,
    InstrumentDefaultVersion,
    LaunchProfile,
    LaunchProfileBypass,
    LaunchProfileObservation,
    LaunchProfileVersion,
)
from .errors import InstrumentValidationError, ReviewCreationRefusedError
from .genres import KNOWN_GENRES, genre_of
from .sandbox import codex_cmd_is_read_only, tokenize_codex_cmd

#: The graduated independence scale (D-3), strongest first. There is no fourth value:
#: a same-provider pair is AT MOST ``same_family`` (shared training organization), and
#: an ambiguous/unfilled lineage resolves conservatively to ``same_family`` with the
#: ``conservative`` marker — family never blocks creation, it only colours the step.
INDEPENDENCE_STEPS = ("different_provider", "same_family", "self_check")

#: The instrument-default parent is a per-deployment SINGLETON; its birth is made
#: idempotent by a fixed id (round 5, finding default-singleton-first-write-race): two
#: concurrent first-writers both target this one row via ON CONFLICT DO NOTHING, one
#: wins, both then lock the same row. A legacy parent with another id is honored first.
INSTRUMENT_DEFAULT_SINGLETON_ID = uuid.UUID("b9000000-0000-4000-8000-000000000001")

#: Common machine-content keys every profile version must carry (C-1). All paths are
#: ABSOLUTE (C-9 clause 4 — the rendered script performs no PATH resolution; the stale
#: alpha resolved from PATH is the measured incident). ``base_url`` is where THIS
#: deployment's service is reachable from that host; ``token_dir`` is where per-review
#: token files live; ``projection_root`` is the out-of-tree root for channel projections
#: (A-4 — stored now so a proven profile does not need re-proving when Part A lands).
#:
#: B.14 A-3/A-4: ``machinery_root`` REPLACES the two-role ``repo_root`` — it names only
#: the Assistant Memory checkout whose supervisor and prompt files this machine launches
#: critics with. The subject under review is NOT profile content: it rides each review
#: (``subject_root`` in the instrument snapshot). A stored pre-split version carrying
#: ``repo_root`` is honored at render as the machinery root (`machinery_root_of`);
#: NEW content carrying the old key is refused (`_validate_profile_content`).
PROFILE_CONTENT_PATH_KEYS = ("machinery_root", "python", "engine_binary", "token_dir", "projection_root")
PROFILE_CONTENT_REQUIRED = (*PROFILE_CONTENT_PATH_KEYS, "base_url", "shell", "probes")

#: The pre-B.14 two-role key, readable in STORED versions only (constraint: proven
#: versions are immutable and their machinery half is the half the probes proved).
LEGACY_MACHINERY_KEY = "repo_root"


def machinery_root_of(content: dict) -> str:
    """The machinery root of a profile version's content, legacy honored (B.14 A-4).

    A version stored before the split carries ``repo_root``; its value is read as the
    machinery root — that was always the half of its meaning the machine actually
    proved (probes run against the machinery environment; no stored probe evidence
    ever touched a subject). New versions carry ``machinery_root`` only.
    """
    root = content.get("machinery_root") or content.get(LEGACY_MACHINERY_KEY)
    if not isinstance(root, str) or not root.strip():
        raise InstrumentValidationError(
            "profile version content carries neither `machinery_root` nor the legacy "
            "`repo_root` — no machinery root to launch from (B.14 A-3)"
        )
    return root

#: The shell that will actually read the rendered script (C-9 clause 6) — a recorded
#: host fact, never guessed from where the server happens to run.
PROFILE_SHELLS = ("cmd", "posix")

#: The anchor form of a temporary-state register (B.14 D-2): a full git object id.
_FULL_HEX_REF = re.compile(r"[0-9a-f]{40}")

_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _is_absolute(path: str) -> bool:
    return path.startswith("/") or path.startswith("\\\\") or bool(_WINDOWS_ABS.match(path))


def _require_str(container: dict, key: str, *, what: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InstrumentValidationError(f"{what}: `{key}` must be a non-empty string")
    return value


#: The charset a recorded tool VERSION must match. The consumer of the stored value is a
#: SHELL, so the gate is the shell's grammar, not printability (finding
#: `b12-tool-version-shell-metachar-still-unsafe`, sol round 3: rounds 1 and 2 required
#: "one printable line", and `0.147 & echo unexpected` is one printable line — in CMD,
#: `&` separates commands even on a `rem` line). Letters, digits, dot, underscore, plus,
#: hyphen: enough for every real version string, and containing no CMD or POSIX
#: metacharacter. `+` is the one addition over `_IDENT_RE` (semver build metadata).
_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


def _is_nonempty_version(value: object) -> bool:
    """A recorded tool version: one shell-inert token, taken from the tool (B.12 D-2).

    The charset gate is part of the contract, not a tidiness preference (findings
    `b12-tool-version-contract-incomplete`, round 1, and
    `b12-tool-version-shell-metachar-still-unsafe`, round 3). A recorded version is printed
    into the generated launch script's header, and the launch script is the one artifact
    this loop forbids anyone to hand-edit — so text arriving there through the store walks
    around the very rule that protects it. Rounds 1-2 validated "one printable line", which
    is a property of TEXT; the consumer is a SHELL, and `&`, `%`, `|` are printable. The
    gate is now the closed charset `_VERSION_RE`, which no shell reads as syntax.

    No adversary is postulated — the recorded threat model has no third parties and the value
    is written under the operator's own deployment credential. This is in the model as
    corruption of one's own data, and the check costs one line, so the case for it does not
    rest on an attacker who does not exist.
    """
    # fullmatch, not match: in Python `$` also matches just before a terminal LF, so
    # `match` accepted "0.147\n" against a grammar declared as one shell-inert token —
    # and the renderer would have put the next pair on a new executable line (finding
    # `b12-shell-charset-final-lf-gap`, sol round 4).
    return isinstance(value, str) and bool(_VERSION_RE.fullmatch(value))


#: Identifiers that end up INSIDE the rendered script's text not through the quoter —
#: the host pair and probe names (finding rendered-profile-values-are-shell-injectable,
#: round 2). A whitelist rather than per-context escaping: a CMD comparison inside
#: quotes has no reliable escape at all, and legitimate hostnames/usernames/probe names
#: fit the list. Enforced at every write site and re-checked at render.
_IDENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_script_identifier(value: str, *, what: str) -> str:
    # fullmatch: `$` + .match() lets a terminal LF through (see _is_nonempty_version).
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise InstrumentValidationError(
            f"{what}: {value!r} must match [A-Za-z0-9._-]+ — this value is rendered into "
            "the launch script's text, where a quote, `&` or `$( )` would break out of "
            "the shell line it sits in; refusal beats an injectable script"
        )
    return value


def _norm_exe(path: str) -> str:
    """Executable-path equality form: forward slashes, case-folded (Windows paths are
    case-insensitive, and the profile's commands are written forward-slash by contract)."""
    return path.replace("\\", "/").lower()


def _validate_command_tokens(
    cmd: str, *, what: str, engine_binary: str | None = None
) -> list[str]:
    """One profile command, validated for BOTH its executors (critic findings
    engine-binary-not-bound + probe-shell-semantics-diverge, round 1).

    A profile command line is interpreted twice — as a line of the rendered startup
    gate (a shell) and as a shell=False argv in the watcher — so everything that could
    make the two disagree is refused: shell operators, `%`/`$` expansions (the gate
    would expand them, the argv would not), and backslashes (the shared tokenization is
    POSIX shlex, which eats them — write Windows paths with forward slashes). The first
    token must be an ABSOLUTE executable path (C-9 clause 4: no PATH resolution
    anywhere — the stale-alpha incident); when ``engine_binary`` is given, it must EQUAL
    the profile's recorded binary, which is what binds the recorded path to its one use
    site instead of leaving it a dead requirement.
    """
    if any(ch in cmd for ch in ";&|<>`\n\r") or "$(" in cmd:
        raise InstrumentValidationError(
            f"{what}: `cmd` must be a plain argv command — shell operators are refused "
            "because the command runs both in the rendered startup gate and as a "
            "shell=False argv before every pass (C-7/C-9)"
        )
    if "%" in cmd or "$" in cmd:
        raise InstrumentValidationError(
            f"{what}: `%` and `$` are refused — the startup gate's shell would expand "
            "them while the watcher's argv keeps them literal, so the 'same' command "
            "would run with different semantics in its two executors"
        )
    if "\\" in cmd:
        raise InstrumentValidationError(
            f"{what}: backslashes are refused — commands are POSIX-tokenized in both "
            "executors, which strips backslashes; write Windows paths with forward "
            "slashes"
        )
    tokens = tokenize_codex_cmd(cmd)
    if not tokens:
        raise InstrumentValidationError(f"{what}: `cmd` does not parse as a command")
    if engine_binary is not None:
        if _norm_exe(tokens[0]) != _norm_exe(engine_binary):
            raise InstrumentValidationError(
                f"{what}: the command's executable {tokens[0]!r} must EQUAL the "
                f"profile's recorded `engine_binary` {engine_binary!r} — the absolute "
                "binary path is only a guard where the launch actually uses it "
                "(the PATH-resolved stale-alpha incident, C-1)"
            )
    elif not _is_absolute(tokens[0]):
        raise InstrumentValidationError(
            f"{what}: the command's executable {tokens[0]!r} must be an ABSOLUTE path — "
            "the rendered script and the watcher perform no PATH resolution (C-9)"
        )
    return tokens


# --- launch profiles (Part C) --------------------------------------------


async def get_profile(
    session: AsyncSession, *, hostname: str, username: str, engine: str
) -> LaunchProfile | None:
    return await session.scalar(
        select(LaunchProfile).where(
            LaunchProfile.hostname == hostname,
            LaunchProfile.username == username,
            LaunchProfile.engine == engine,
        )
    )


async def upsert_profile(
    session: AsyncSession, *, hostname: str, username: str, engine: str
) -> LaunchProfile:
    """The profile identity row for a host × engine pair (C-1); created bare — content
    arrives only as proven versions."""
    for name, value in (("hostname", hostname), ("username", username), ("engine", engine)):
        if not isinstance(value, str) or not value.strip():
            raise InstrumentValidationError(f"profile key: `{name}` must be a non-empty string")
    validate_script_identifier(hostname, what="profile key `hostname`")
    validate_script_identifier(username, what="profile key `username`")
    # The engine name reaches the same rendered script (the proven-tool-versions header
    # line and the closed tool set both carry it), and it was the one profile-key value
    # accepted as any non-empty string (finding
    # `b12-tool-version-shell-metachar-still-unsafe`, sol round 3).
    validate_script_identifier(engine, what="profile key `engine`")
    # An honest atomic upsert (round 5, finding unique-identity-create-race): two
    # concurrent bootstraps of the same key both land on the one row instead of racing
    # a read-then-insert into the unique constraint.
    await session.execute(
        pg_insert(LaunchProfile)
        .values(id=uuid.uuid4(), hostname=hostname, username=username, engine=engine)
        .on_conflict_do_nothing(index_elements=["hostname", "username", "engine"])
    )
    return await get_profile(session, hostname=hostname, username=username, engine=engine)


def _validate_profile_content(engine: str, content: dict) -> None:
    """The C-1 machine-content contract, checked at version WRITE time.

    Engine-specific over a common part. For the codex engine the stored sandbox
    invocation is validated against the read-only contract HERE (D-6): a configuration
    surface that can quietly re-open a closed security decision is how closed decisions
    get reopened.
    """
    if not isinstance(content, dict):
        raise InstrumentValidationError("profile version: `content` must be an object")
    # B.14 A-4: the old two-role key is refused on NEW content — with or without the new
    # key beside it — so the "machinery + subject in one field" reading cannot re-enter
    # through habit. Stored pre-split versions are honored at render (`machinery_root_of`).
    if LEGACY_MACHINERY_KEY in content:
        raise InstrumentValidationError(
            "profile version content: `repo_root` is retired by the B.14 split — the "
            "profile describes the MACHINE (`machinery_root`: where the loop's supervisor "
            "and prompts live); the subject under review rides each review as "
            "`subject_root` at creation, never the profile"
        )
    for key in PROFILE_CONTENT_REQUIRED:
        if key not in content:
            raise InstrumentValidationError(
                f"profile version content: missing required key `{key}` "
                f"(required: {', '.join(PROFILE_CONTENT_REQUIRED)})"
            )
    for key in PROFILE_CONTENT_PATH_KEYS:
        value = _require_str(content, key, what="profile version content")
        if not _is_absolute(value):
            raise InstrumentValidationError(
                f"profile version content: `{key}` must be an ABSOLUTE path (got {value!r}) — "
                "the rendered script performs no PATH resolution (C-9)"
            )
    _require_str(content, "base_url", what="profile version content")
    shell = content.get("shell")
    if shell not in PROFILE_SHELLS:
        raise InstrumentValidationError(
            f"profile version content: `shell` must be one of {PROFILE_SHELLS} — the shell "
            "that will read the rendered script is a recorded host fact (C-9)"
        )
    probes = content.get("probes")
    if not isinstance(probes, list) or not probes:
        raise InstrumentValidationError(
            "profile version content: `probes` must be a non-empty list of {name, cmd} — "
            "a version with nothing to probe can never carry evidence of operability (C-2)"
        )
    seen: set[str] = set()
    for probe in probes:
        if not isinstance(probe, dict):
            raise InstrumentValidationError("profile version content: each probe must be an object")
        name = _require_str(probe, "name", what="profile probe")
        validate_script_identifier(name, what="profile probe name")
        cmd = _require_str(probe, "cmd", what="profile probe")
        _validate_command_tokens(cmd, what=f"profile probe {name!r}")
        if name in seen:
            raise InstrumentValidationError(f"profile version content: duplicate probe name {name!r}")
        seen.add(name)
    if engine == "codex":
        codex_cmd = _require_str(content, "codex_cmd", what="codex profile content")
        # The recorded absolute binary is bound to its ONE use site: the critic command
        # must invoke exactly `engine_binary` (finding engine-binary-not-bound).
        _validate_command_tokens(
            codex_cmd, what="codex profile content", engine_binary=content["engine_binary"]
        )
        if not codex_cmd_is_read_only(codex_cmd):
            raise InstrumentValidationError(
                "codex profile content: `codex_cmd` is not a recognized `codex exec` "
                "invocation pinned read-only — the critic sandbox decision is not reopened "
                "by a profile (D-6)"
            )


#: B.12 D-2 — where the proven TOOL VERSIONS live inside the probe evidence: an object
#: mapping a tool's name to the version string observed when its probes passed. The engine
#: is required; the interpreter may be recorded beside it and is compared the same way.
#:
#: THE SET IS CLOSED (finding `b12-tool-version-name-contract-still-unsafe`, sol round 2):
#: the engine and the interpreter, exactly the two tools whose absolute paths the profile
#: content already records (`engine_binary`, `python`). The freshness check runs a tool at
#: its RECORDED ABSOLUTE PATH, so a recorded version for a tool with no recorded path is a
#: comparison that cannot be performed — the prepare-repair skill used to authorise such
#: entries anyway, which left the contract contradicting the store. Closing the set removes
#: the contradiction without building a per-tool path registry; if arbitrary tools are ever
#: wanted, that registry is the honest price.
#:
#: WHY THIS EXISTS AT ALL. "Tool version divergence" is a declared ground for devaluing a
#: launch profile, and the freshness check is instructed to compare live tool versions
#: "against the ones recorded in the active version" — but nothing recorded any. The store
#: kept pass/fail per probe and nothing else, so the ground existed in the prose of the
#: rule and could never fire. The stake is not hypothetical: a Codex update from 0.145 to
#: 0.147 broke the sandbox once and was diagnosed by hand over a session.
#:
#: The alternative — deleting the ground and recording why — was put to the operator as the
#: narrower claim on 2026-08-25 and NOT chosen (Task `70a9bf47`, path A).
#:
#: No migration: `probe_evidence` is an existing nullable JSONB column, which is why D-2
#: says "no migration" as a statement rather than a hope.
PROBE_TOOL_VERSIONS_KEY = "tool_versions"

#: The one recordable tool beside the engine: the interpreter, whose absolute path the
#: profile content records under the same name (`PROFILE_CONTENT_PATH_KEYS`).
PROBE_TOOL_INTERPRETER = "python"


def _validate_probe_evidence(profile: LaunchProfile, content: dict, evidence: dict) -> None:
    """C-2: a version records WHEN and ON WHICH HOST its probes passed, and the pair must
    be the profile's own — preparation ends with a run of the same probes every pass will
    later use, in the environment the profile names, not with "it works for me".

    B.12 D-2 adds the third fact: WHICH TOOL VERSION they passed on. Without it the
    freshness check cannot perform the comparison its own rule names, and a profile keeps
    reading as proven across a tool upgrade that changed the behaviour it proved.
    """
    if not isinstance(evidence, dict):
        raise InstrumentValidationError("profile version: `probe_evidence` must be an object")
    hostname = _require_str(evidence, "hostname", what="probe evidence")
    username = _require_str(evidence, "username", what="probe evidence")
    _require_str(evidence, "at", what="probe evidence")
    if (hostname, username) != (profile.hostname, profile.username):
        raise InstrumentValidationError(
            f"probe evidence names host {hostname}/{username}, but the profile's environment "
            f"is {profile.hostname}/{profile.username} — a version is proven only by probes "
            "run in its own environment (C-2)"
        )
    results = evidence.get("results")
    if not isinstance(results, list):
        raise InstrumentValidationError("probe evidence: `results` must be a list")
    passed = {
        r.get("name"): bool(r.get("passed"))
        for r in results
        if isinstance(r, dict) and isinstance(r.get("name"), str)
    }
    for probe in content["probes"]:
        if not passed.get(probe["name"]):
            raise InstrumentValidationError(
                f"probe evidence: probe {probe['name']!r} has no passing result — a version "
                "is stored only proven (C-2/C-6: probes pass + operator accepts)"
            )
    # B.12 D-2: and WHICH TOOL VERSION they passed on. Required rather than optional,
    # because an optional field is exactly what "tool version divergence" was before this:
    # a ground named by a rule and carried by nothing.
    versions = evidence.get(PROBE_TOOL_VERSIONS_KEY)
    if not isinstance(versions, dict) or not versions:
        raise InstrumentValidationError(
            f"probe evidence: `{PROBE_TOOL_VERSIONS_KEY}` must be a non-empty object "
            "mapping a tool name to the version string its probes passed on (at least the "
            f"engine, {profile.engine!r}). Without it the freshness check cannot compare "
            "live versions against the proven ones, and 'tool version divergence' stays a "
            "ground that can never fire — which is how a Codex 0.145→0.147 upgrade broke "
            "the sandbox under a profile that still read as proven"
        )
    # BOTH HALVES of every stored pair reach the rendered script, so both halves carry the
    # same guarantee (finding `b12-tool-version-name-contract-still-unsafe`: the value was
    # sanitised and the NAME beside it was interpolated unchecked). The name check here is
    # the closed set — the engine and the interpreter, the two tools whose absolute paths
    # the profile records, which is also what makes every accepted name one printable line.
    allowed_tools = {profile.engine, PROBE_TOOL_INTERPRETER}
    for name, value in versions.items():
        if name not in allowed_tools:
            raise InstrumentValidationError(
                f"probe evidence: `{PROBE_TOOL_VERSIONS_KEY}` entry {name!r} is outside the "
                f"closed tool set {sorted(allowed_tools)} — the freshness check runs a tool "
                "at its RECORDED ABSOLUTE PATH, and only the engine and the interpreter "
                "have one, so a version for anything else is a comparison that can never "
                "be performed"
            )
        if not _is_nonempty_version(value):
            raise InstrumentValidationError(
                f"probe evidence: `{PROBE_TOOL_VERSIONS_KEY}` entry {name!r} must be a "
                "non-empty version string matching [A-Za-z0-9._+-]+, observed rather "
                "than assumed — the value is compared verbatim at the freshness check "
                "and is printed into the rendered launch script, whose reader is a "
                "SHELL: `&` is printable and separates commands even on a CMD rem line, "
                "so the gate is the shell-inert charset, not printability"
            )
    if profile.engine not in versions:
        raise InstrumentValidationError(
            f"probe evidence: `{PROBE_TOOL_VERSIONS_KEY}` must include the engine "
            f"{profile.engine!r} — it is the tool whose divergence the ground was written "
            f"for. Recorded: {sorted(versions)}"
        )


async def add_profile_version(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    content: dict,
    probe_evidence: dict,
) -> LaunchProfileVersion:
    """Store a new immutable, proven version and move the active pointer to it (C-2/C-6).

    Storing IS the acceptance act — the management endpoint is reachable only under the
    deployment bootstrap credential, and the version must arrive with complete passing
    probe evidence from the profile's own host pair. Prior versions remain; rollback is
    ``activate_profile_version`` on an earlier one.
    """
    # Lock the parent row for the whole allocate-and-insert: two concurrent management
    # requests for the same profile serialize here instead of racing max+1 into a raw
    # integrity error (round 4, finding non-atomic-version-allocation — same pattern as
    # the message-seq FOR UPDATE in append_message).
    profile = await session.get(LaunchProfile, profile_id, with_for_update=True)
    if profile is None:
        raise InstrumentValidationError(f"no launch profile {profile_id}")
    _validate_profile_content(profile.engine, content)
    _validate_probe_evidence(profile, content, probe_evidence)
    last = await session.scalar(
        select(LaunchProfileVersion.version)
        .where(LaunchProfileVersion.profile_id == profile_id)
        .order_by(LaunchProfileVersion.version.desc())
        .limit(1)
    )
    row = LaunchProfileVersion(
        profile_id=profile_id,
        version=(last or 0) + 1,
        content=content,
        probe_evidence=probe_evidence,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # The lock makes this unreachable in the normal path; translated anyway so a
        # residual conflict is an actionable refusal, never a bare 500.
        raise InstrumentValidationError(
            "version allocation conflicted with a concurrent management request — retry"
        ) from None
    profile.active_version_id = row.id
    await session.flush()
    return row


async def activate_profile_version(
    session: AsyncSession, *, profile_id: uuid.UUID, version_id: uuid.UUID
) -> LaunchProfile:
    """Rollback = move the pointer to a prior version (C-2). A devalued version is not a
    rollback target: what was observed broken stays broken until a NEW proven version."""
    profile = await session.get(LaunchProfile, profile_id)
    if profile is None:
        raise InstrumentValidationError(f"no launch profile {profile_id}")
    version = await session.get(LaunchProfileVersion, version_id)
    if version is None or version.profile_id != profile_id:
        raise InstrumentValidationError("version does not belong to this profile")
    if version.devalued_at is not None:
        raise InstrumentValidationError(
            f"version {version.version} is devalued — the exit from the devalued state is a "
            "new proven version, not reactivating the one observed broken (C-6)"
        )
    profile.active_version_id = version.id
    await session.flush()
    return profile


async def record_profile_observation(
    session: AsyncSession,
    *,
    profile_id: uuid.UUID,
    version_id: uuid.UUID,
    kind: str,
    observation: str,
    observed_hostname: str,
    observed_username: str,
    evidence: dict | None = None,
) -> LaunchProfileObservation:
    """The durable server state behind C-6: devaluation by observation, host-bound.

    A ``devaluation`` is accepted only when the observed pair equals the profile key's
    pair, and it transitions the named version to devalued in the store. A refusal from a
    launcher run OUTSIDE its environment is a ``mislaunch`` — recorded as such and never
    devaluing the source profile; posting it with the profile's own pair is refused as
    contradictory. The poster is the development agent session under the deployment
    credential (the management entry, C-4) — scripts and leaves post nothing themselves.
    """
    profile = await session.get(LaunchProfile, profile_id)
    if profile is None:
        raise InstrumentValidationError(f"no launch profile {profile_id}")
    version = await session.get(LaunchProfileVersion, version_id)
    if version is None or version.profile_id != profile_id:
        raise InstrumentValidationError("version does not belong to this profile")
    if observation not in PROFILE_OBSERVATIONS:
        raise InstrumentValidationError(
            f"unknown observation {observation!r} (expected one of {PROFILE_OBSERVATIONS})"
        )
    for name, value in (("observed_hostname", observed_hostname), ("observed_username", observed_username)):
        if not isinstance(value, str) or not value.strip():
            raise InstrumentValidationError(f"observation: `{name}` must be a non-empty string")
    pair_matches = (observed_hostname, observed_username) == (profile.hostname, profile.username)
    if kind == "devaluation" and not pair_matches:
        raise InstrumentValidationError(
            f"a devaluation must be observed in the profile's own environment "
            f"({profile.hostname}/{profile.username}); an observation from "
            f"{observed_hostname}/{observed_username} is a MISLAUNCH — record it as such, "
            "it never devalues the source profile (C-6)"
        )
    if kind == "mislaunch" and pair_matches:
        raise InstrumentValidationError(
            "a mislaunch is a launch OUTSIDE the profile's environment; this observation "
            "names the profile's own pair — if the gate refused here, that is a devaluation "
            "observation (C-6)"
        )
    if kind not in ("devaluation", "mislaunch"):
        raise InstrumentValidationError(f"unknown observation kind {kind!r}")
    row = LaunchProfileObservation(
        profile_id=profile_id,
        version_id=version_id,
        kind=kind,
        observation=observation,
        observed_hostname=observed_hostname,
        observed_username=observed_username,
        evidence=evidence,
    )
    session.add(row)
    if kind == "devaluation" and version.devalued_at is None:
        version.devalued_at = datetime.now(UTC)
    await session.flush()
    return row


async def add_bypass(
    session: AsyncSession, *, profile_id: uuid.UUID, probe_ref: str | None, trigger_text: str
) -> LaunchProfileBypass:
    """Register a bypass against the probe that justifies it (C-7) — "this bypass stands
    while probe X fails", never "revisit later". ``probe_ref`` is nullable for the honest
    reservation (some bypasses have no probe); the trigger is then recorded in words and
    the check stays human — but the record still says what to check."""
    profile = await session.get(LaunchProfile, profile_id)
    if profile is None:
        raise InstrumentValidationError(f"no launch profile {profile_id}")
    if not isinstance(trigger_text, str) or not trigger_text.strip():
        raise InstrumentValidationError("bypass: `trigger_text` must be a non-empty string")
    if probe_ref is not None:
        if not isinstance(probe_ref, str) or not probe_ref.strip():
            raise InstrumentValidationError("bypass: `probe_ref` must be a non-empty string or null")
        # The ref must RESOLVE against the active version's probes (finding
        # bypass-probe-ref-unvalidated, round 1): a bypass hanging on a probe that does
        # not exist will never be named by a real probe-flip notice — forever silent,
        # indistinguishable from no mechanism. Historical bypasses whose probe vanishes
        # in a LATER version are untouched: they were valid when recorded.
        if profile.active_version_id is None:
            raise InstrumentValidationError(
                "bypass: the profile has no active version to resolve `probe_ref` "
                "against — record the bypass without a probe (trigger in words), or "
                "prove a version first"
            )
        version = await session.get(LaunchProfileVersion, profile.active_version_id)
        known = [p["name"] for p in (version.content.get("probes") or [])]
        if probe_ref not in known:
            raise InstrumentValidationError(
                f"bypass: `probe_ref` {probe_ref!r} names no probe of the active profile "
                f"version (known: {', '.join(known)}) — a flip notice could never find it"
            )
    row = LaunchProfileBypass(profile_id=profile_id, probe_ref=probe_ref, trigger_text=trigger_text)
    session.add(row)
    await session.flush()
    return row


async def close_bypass(session: AsyncSession, *, bypass_id: uuid.UUID) -> LaunchProfileBypass:
    row = await session.get(LaunchProfileBypass, bypass_id)
    if row is None:
        raise InstrumentValidationError(f"no bypass {bypass_id}")
    if row.removed_at is None:
        row.removed_at = datetime.now(UTC)
        await session.flush()
    return row


async def open_bypasses(session: AsyncSession, profile_id: uuid.UUID) -> list[LaunchProfileBypass]:
    rows = await session.scalars(
        select(LaunchProfileBypass)
        .where(LaunchProfileBypass.profile_id == profile_id, LaunchProfileBypass.removed_at.is_(None))
        .order_by(LaunchProfileBypass.created_at)
    )
    return list(rows)


async def list_profiles(session: AsyncSession) -> list[dict]:
    profiles = (await session.scalars(select(LaunchProfile).order_by(LaunchProfile.created_at))).all()
    out = []
    for profile in profiles:
        versions = (
            await session.scalars(
                select(LaunchProfileVersion)
                .where(LaunchProfileVersion.profile_id == profile.id)
                .order_by(LaunchProfileVersion.version)
            )
        ).all()
        out.append(
            {
                **profile_key_dict(profile),
                "versions": [
                    {
                        "id": str(v.id),
                        "version": v.version,
                        "devalued_at": v.devalued_at.isoformat() if v.devalued_at else None,
                        "probe_evidence": v.probe_evidence,
                        "created_at": v.created_at.isoformat() if v.created_at else None,
                    }
                    for v in versions
                ],
            }
        )
    return out


def profile_key_dict(profile: LaunchProfile) -> dict:
    return {
        "id": str(profile.id),
        "hostname": profile.hostname,
        "username": profile.username,
        "engine": profile.engine,
        "active_version_id": str(profile.active_version_id) if profile.active_version_id else None,
    }


# --- engine model lists (D-4) --------------------------------------------


def _validate_verification(verification) -> None:
    """The D-4 verification-run record, validated for its STATED meaning (finding
    verified-model-evidence-unstructured, round 1): the engine accepted the identifier,
    the run's effort mapped through, the sandbox held — plus when and on which host
    pair. A verified entry is the admission gate to the critic role; admission on "any
    non-empty dict" was a strong word on a weak check. A failed run is not verification:
    a false confirmation is refused, not recorded."""
    if not isinstance(verification, dict):
        raise InstrumentValidationError(
            "model entry: a `verified` entry must carry its verification-run record (D-4)"
        )
    for key in ("at", "hostname", "username"):
        _require_str(verification, key, what="verification record")
    for key in ("engine_accepted", "sandbox_held"):
        if verification.get(key) is not True:
            raise InstrumentValidationError(
                f"verification record: `{key}` must be explicitly true — a failed or "
                "unattested run is not verification (D-4: the engine accepted the "
                "identifier, the effort mapped through, the sandbox held)"
            )
    if "effort_checked" not in verification:
        raise InstrumentValidationError(
            "verification record: `effort_checked` is required — the effort value the "
            "run actually used, or null for an engine with no effort knob (D-4)"
        )
    effort = verification["effort_checked"]
    if effort is not None and (not isinstance(effort, str) or not effort.strip()):
        raise InstrumentValidationError(
            "verification record: `effort_checked` must be a non-empty string or null"
        )


def _validate_effort_domain(effort_domain) -> None:
    if effort_domain is None:
        return
    if not isinstance(effort_domain, list) or not all(
        isinstance(v, str) and v.strip() for v in effort_domain
    ):
        raise InstrumentValidationError(
            "model entry: `effort_domain` must be null or a list of non-empty strings — "
            "the effort values the engine's interface accepts, an operator-entered "
            "interface fact (D-4)"
        )
    if len(set(effort_domain)) != len(effort_domain):
        raise InstrumentValidationError("model entry: `effort_domain` has duplicate values")


async def add_engine_model(
    session: AsyncSession,
    *,
    engine: str,
    invocation_alias: str,
    provider: str,
    entry_class: str,
    provider_model_id: str | None = None,
    family: str | None = None,
    effort_domain: list | None = None,
    verification: dict | None = None,
) -> EngineModel:
    """One list entry (D-4): a cheap append, never a new profile version.

    ``verified`` requires the verification-run record (the engine accepted the
    identifier, the run's effort mapped through, the sandbox held); ``facts_only``
    exists so a development instrument can resolve for D-3 and is never
    critic-selectable.
    """
    for name, value in (("engine", engine), ("invocation_alias", invocation_alias), ("provider", provider)):
        if not isinstance(value, str) or not value.strip():
            raise InstrumentValidationError(f"model entry: `{name}` must be a non-empty string")
    if entry_class == "verified":
        _validate_verification(verification)
    elif entry_class != "facts_only":
        raise InstrumentValidationError(
            f"model entry: unknown class {entry_class!r} (expected verified | facts_only)"
        )
    _validate_effort_domain(effort_domain)
    # Atomic insert (round 5, finding unique-identity-create-race): a concurrent twin
    # no-ops on the unique (engine, alias) index and gets the SAME named refusal the
    # read-then-insert used to give — without the race window into a raw 500.
    inserted = await session.scalar(
        pg_insert(EngineModel)
        .values(
            id=uuid.uuid4(),
            engine=engine,
            invocation_alias=invocation_alias,
            provider=provider,
            provider_model_id=provider_model_id,
            family=family,
            effort_domain=effort_domain,
            entry_class=entry_class,
            verification=verification,
        )
        .on_conflict_do_nothing(index_elements=["engine", "invocation_alias"])
        .returning(EngineModel.id)
    )
    if inserted is None:
        raise InstrumentValidationError(
            f"engine {engine!r} already lists {invocation_alias!r} — update the entry instead"
        )
    return await session.get(EngineModel, inserted)


async def update_engine_model(session: AsyncSession, *, model_id: uuid.UUID, **fields) -> EngineModel:
    """Fill or correct the operator-entered facts of an entry (official id, family,
    effort domain), or upgrade ``facts_only`` -> ``verified`` by supplying the run."""
    row = await session.get(EngineModel, model_id)
    if row is None:
        raise InstrumentValidationError(f"no model entry {model_id}")
    allowed = {"provider", "provider_model_id", "family", "effort_domain", "entry_class", "verification"}
    unknown = set(fields) - allowed
    if unknown:
        raise InstrumentValidationError(f"model entry: unknown fields {sorted(unknown)}")
    merged_class = fields.get("entry_class", row.entry_class)
    merged_verification = fields.get("verification", row.verification)
    if merged_class == "verified":
        _validate_verification(merged_verification)
    if merged_class not in ("verified", "facts_only"):
        raise InstrumentValidationError(f"model entry: unknown class {merged_class!r}")
    if "effort_domain" in fields:
        _validate_effort_domain(fields["effort_domain"])
    if "provider" in fields and (not isinstance(fields["provider"], str) or not fields["provider"].strip()):
        raise InstrumentValidationError("model entry: `provider` must be a non-empty string")
    for key, value in fields.items():
        setattr(row, key, value)
    await session.flush()
    return row


async def list_engine_models(session: AsyncSession, engine: str | None = None) -> list[EngineModel]:
    query = select(EngineModel).order_by(EngineModel.engine, EngineModel.invocation_alias)
    if engine is not None:
        query = query.where(EngineModel.engine == engine)
    return list(await session.scalars(query))


def model_entry_dict(row: EngineModel) -> dict:
    return {
        "id": str(row.id),
        "engine": row.engine,
        "invocation_alias": row.invocation_alias,
        "provider": row.provider,
        "provider_model_id": row.provider_model_id,
        "family": row.family,
        "effort_domain": row.effort_domain,
        "entry_class": row.entry_class,
        "verification": row.verification,
    }


# --- instrument defaults (D-5) -------------------------------------------


def _validate_pair(pair, *, where: str) -> None:
    if not isinstance(pair, dict):
        raise InstrumentValidationError(f"default {where}: must be an object")
    development, critic = pair.get("development"), pair.get("critic")
    if not isinstance(development, dict):
        raise InstrumentValidationError(f"default {where}: missing `development` {{engine, model}}")
    if not isinstance(critic, dict):
        raise InstrumentValidationError(f"default {where}: missing `critic` {{engine, model[, effort]}}")
    _require_str(development, "engine", what=f"default {where} development")
    _require_str(development, "model", what=f"default {where} development")
    _require_str(critic, "engine", what=f"default {where} critic")
    _require_str(critic, "model", what=f"default {where} critic")
    if "effort" in critic and critic["effort"] is not None and (
        not isinstance(critic["effort"], str) or not critic["effort"].strip()
    ):
        raise InstrumentValidationError(f"default {where} critic: `effort` must be a string or null")


async def _resolve_decision_ref(session: AsyncSession, decision_ref) -> uuid.UUID:
    """D-5: the reference must RESOLVE — the graph shares the database (E-2's verified
    fact), so existence and type are checked in-process. Content-match is not — a named
    boundary: what the decision says remains the operator's record."""
    try:
        ref = uuid.UUID(str(decision_ref))
    except (ValueError, AttributeError, TypeError):
        raise InstrumentValidationError(
            f"default version: `decision_ref` is not a node id: {decision_ref!r}"
        ) from None
    node = await session.get(Node, ref)
    if node is None or node.deleted_at is not None:
        raise InstrumentValidationError(
            f"default version: no graph node {ref} — record the decision first (D-5)"
        )
    if node.type != "Decision":
        raise InstrumentValidationError(
            f"default version: graph node {ref} has type {node.type!r}, not Decision — "
            "record the decision first (D-5)"
        )
    return ref


async def add_default_version(
    session: AsyncSession,
    *,
    pair_map: dict,
    operator_quote: str,
    decision_ref,
) -> InstrumentDefaultVersion:
    """Store a new default-instrument version and move the pointer (D-5).

    Refused without the operator's verbatim quote, with a non-resolving graph-Decision
    ref, or with a per-genre key unknown to the genre registry — a misspelled override
    must never sit dormant while the global pair silently applies.
    """
    if not isinstance(operator_quote, str) or not operator_quote.strip():
        raise InstrumentValidationError(
            "default version: `operator_quote` (the operator's verbatim words) is mandatory — "
            "a new model never becomes the default by itself (D-5)"
        )
    ref = await _resolve_decision_ref(session, decision_ref)
    if not isinstance(pair_map, dict) or not isinstance(pair_map.get("global"), dict):
        raise InstrumentValidationError(
            'default version: `pair_map` must be {"global": PAIR, "per_genre": {genre: PAIR}}'
        )
    _validate_pair(pair_map["global"], where="global pair")
    per_genre = pair_map.get("per_genre") or {}
    if not isinstance(per_genre, dict):
        raise InstrumentValidationError("default version: `per_genre` must be an object")
    for genre, pair in per_genre.items():
        if genre not in KNOWN_GENRES:
            raise InstrumentValidationError(
                f"default version: override key {genre!r} names no genre in the registry "
                f"(known: {', '.join(KNOWN_GENRES)}) — adding a genre is a code change that "
                "naturally precedes any policy for it (D-5)"
            )
        _validate_pair(pair, where=f"per-genre pair {genre!r}")

    # Parent-row lock for the allocate-and-insert, mirroring add_profile_version
    # (round 4, finding non-atomic-version-allocation). The parent's BIRTH is
    # idempotent via the fixed singleton id (round 5, default-singleton-first-write-race):
    # a first-ever writer inserts it ON CONFLICT DO NOTHING — a concurrent twin blocks
    # on the unique index, no-ops, and both re-select and lock the SAME row.
    parent = await session.scalar(
        select(InstrumentDefault)
        .order_by(InstrumentDefault.created_at)
        .limit(1)
        .with_for_update()
    )
    if parent is None:
        await session.execute(
            pg_insert(InstrumentDefault)
            .values(id=INSTRUMENT_DEFAULT_SINGLETON_ID)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        parent = await session.scalar(
            select(InstrumentDefault)
            .order_by(InstrumentDefault.created_at)
            .limit(1)
            .with_for_update()
        )
    last = await session.scalar(
        select(InstrumentDefaultVersion.version)
        .where(InstrumentDefaultVersion.default_id == parent.id)
        .order_by(InstrumentDefaultVersion.version.desc())
        .limit(1)
    )
    row = InstrumentDefaultVersion(
        default_id=parent.id,
        version=(last or 0) + 1,
        pair_map=pair_map,
        operator_quote=operator_quote,
        decision_ref=ref,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        raise InstrumentValidationError(
            "version allocation conflicted with a concurrent management request — retry"
        ) from None
    parent.active_version_id = row.id
    await session.flush()
    return row


async def activate_default_version(
    session: AsyncSession, *, version_id: uuid.UUID
) -> InstrumentDefault:
    """Rollback is a pointer move; history is kept (D-5)."""
    version = await session.get(InstrumentDefaultVersion, version_id)
    if version is None:
        raise InstrumentValidationError(f"no default version {version_id}")
    parent = await session.get(InstrumentDefault, version.default_id)
    parent.active_version_id = version.id
    await session.flush()
    return parent


async def active_default_pair_map(session: AsyncSession) -> tuple[dict, int] | None:
    """(pair_map, version) of the active default record, or None if none is active."""
    parent = await session.scalar(select(InstrumentDefault).limit(1))
    if parent is None or parent.active_version_id is None:
        return None
    version = await session.get(InstrumentDefaultVersion, parent.active_version_id)
    if version is None:
        return None
    return version.pair_map, version.version


async def get_default(session: AsyncSession) -> dict | None:
    parent = await session.scalar(select(InstrumentDefault).limit(1))
    if parent is None:
        return None
    versions = (
        await session.scalars(
            select(InstrumentDefaultVersion)
            .where(InstrumentDefaultVersion.default_id == parent.id)
            .order_by(InstrumentDefaultVersion.version)
        )
    ).all()
    return {
        "id": str(parent.id),
        "active_version_id": str(parent.active_version_id) if parent.active_version_id else None,
        "versions": [
            {
                "id": str(v.id),
                "version": v.version,
                "pair_map": v.pair_map,
                "operator_quote": v.operator_quote,
                "decision_ref": str(v.decision_ref),
                "created_at": v.created_at.isoformat() if v.created_at else None,
            }
            for v in versions
        ],
    }


# --- independence (D-3) --------------------------------------------------


def independence_step(development: EngineModel, critic: EngineModel) -> dict:
    """The graduated critic-independence level, computed over the two snapshot records'
    provider/family facts (D-3). Identity for the self-check gate is equality of the
    tuple (provider, official provider model id) — engine-local aliases are never
    compared. An ambiguous or unfilled family resolves CONSERVATIVELY: the pair is
    treated as same-family and the step carries the "conservative: ambiguous lineage"
    marker; family never blocks creation. A same-provider pair is at most same_family —
    shared training organization is shared lineage whatever the family strings say.
    """
    if (
        development.provider == critic.provider
        and development.provider_model_id is not None
        and development.provider_model_id == critic.provider_model_id
    ):
        return {
            "step": "self_check",
            "conservative": False,
            "reason": "development and critic are the same model (provider + official id)",
        }
    if development.provider == critic.provider:
        return {
            "step": "same_family",
            "conservative": False,
            "reason": "same provider — shared lineage",
        }
    if not development.family or not critic.family:
        return {
            "step": "same_family",
            "conservative": True,
            "reason": "conservative: ambiguous lineage (family unfilled on at least one entry)",
        }
    if development.family == critic.family:
        return {
            "step": "same_family",
            "conservative": False,
            "reason": f"same family ({critic.family})",
        }
    return {
        "step": "different_provider",
        "conservative": False,
        "reason": f"different providers ({development.provider} vs {critic.provider})",
    }


# --- the creation gate (C-5 / D-1 / D-3 / D-4) ---------------------------


async def _resolve_model_entry(
    session: AsyncSession, *, engine: str, model: str, role: str
) -> EngineModel:
    entry = await session.scalar(
        select(EngineModel).where(
            EngineModel.engine == engine, EngineModel.invocation_alias == model
        )
    )
    if entry is None:
        kind = "a verified entry" if role == "critic" else "an entry (facts_only suffices)"
        raise ReviewCreationRefusedError(
            f"the {role} model {model!r} resolves to no entry on engine {engine!r}'s model "
            f"list — add {kind} first (D-4)"
        )
    if role == "critic" and entry.entry_class != "verified":
        raise ReviewCreationRefusedError(
            f"model {model!r} on engine {engine!r} is a facts-only entry — run its "
            "verification to make it critic-selectable (D-4)"
        )
    if entry.provider_model_id is None:
        raise ReviewCreationRefusedError(
            f"the {role} entry {model!r} on engine {engine!r} carries no official provider "
            "model id — fill in the identity (D-3): the self-check gate compares on the "
            "(provider, official id) tuple and cannot run without it"
        )
    return entry


def _pair_from_default(pair_map: dict, genre: str | None) -> dict:
    """D-5 resolution: two steps and nothing more — the review's genre has an override ->
    it applies; otherwise the global pair."""
    per_genre = pair_map.get("per_genre") or {}
    if genre is not None and genre in per_genre:
        return per_genre[genre]
    return pair_map["global"]


_ELEMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_RANGE_TAIL = re.compile(r"^\d+-\d+$")


def _locator_grammar_problem(locator: str, mode: str | None) -> str | None:
    """The CLOSED per-mode locator grammar (round 13, finding
    b14-temporary-locator-shape-unvalidated-reopened; spec D-2 names the forms).

    code mode: ``path::symbol`` or ``path::start-end`` — the left side must look
    like a repository path (contains '/' or '.'), the tail non-empty. spec mode
    (the audience genre rides on it): a spec ELEMENT id (``T1-4`` shape) or a
    documentary span ``path::section`` (the D-4 canonical case — a stub section of
    an article); a bare code line RANGE means nothing against a document and is
    refused. Arbitrary strings pass neither.
    """
    path, sep, tail = locator.partition("::")
    path_like = bool(path.strip()) and ("/" in path or "." in path)
    if mode == "code":
        if not sep:
            return (
                "code mode locates by `path::symbol` or `path::start-end` — a bare "
                "string (or a spec element id) names nothing in a repository"
            )
        if not path_like or not tail.strip():
            return (
                "code mode locates by `path::symbol` or `path::start-end` — the left "
                "side must be a repository path, the right side non-empty"
            )
        return None
    # spec mode, and the audience genre riding on it
    if not sep:
        if _ELEMENT_ID.match(locator.strip()):
            return None
        return (
            "spec mode locates by a spec ELEMENT id (e.g. `T1-4`) or a documentary "
            "span `path::section` — an arbitrary string names nothing"
        )
    if not path_like or not tail.strip():
        return (
            "spec mode's `path::section` form needs a path-like left side and a "
            "non-empty section"
        )
    if _RANGE_TAIL.match(tail.strip()):
        return (
            "a line range locates code, not a document — a spec-mode span is an "
            "element id or `path::section`"
        )
    return None


def _frozen_temporary_states(instrument: dict, *, mode: str | None = None) -> list[dict]:
    """B.14 D-2 — the register of declared known-temporary states, frozen at creation.

    Optional; an empty register is legal and freezes as ``[]``. Each entry carries an
    immutable ``entry_id`` (unique within the register), a typed ``target`` whose
    BINDING coordinate is the quoted text of the excused span (``quote``) beside an
    auxiliary per-mode ``locator``, ``why`` (the decision the state rests on) and
    ``closes`` (the gate or event that replaces it — a temporary state without a named
    end is an open question, refused). One entry excuses exactly ONE occurrence of its
    quote; a recurring state is declared once per occurrence.

    The ANCHOR: each frozen entry records ``declared_at`` — the subject ref at review
    creation, taken from the creation input ``subject_ref`` (the server never runs git
    against the subject; the declaring side names the state it declared against). The
    register accepts entries at creation ONLY — there is no restatement channel (D-3).
    """
    register = instrument.get("temporary_states")
    if register is None:
        return []
    if not isinstance(register, list):
        raise ReviewCreationRefusedError(
            "instrument: `temporary_states` must be a list of declared-state entries "
            "(B.14 D-2); [] declares none"
        )
    if not register:
        return []
    subject_ref = instrument.get("subject_ref")
    # Full 40-hex, strictly (finding b14-temporary-credit-evidence-unbound): the anchor
    # is what every later credit TRACKS FROM, and an unresolvable string would leave
    # the whole audit trail hanging on nothing. The declaring side reads it from
    # `git rev-parse` anyway; the strict form makes a garbage anchor unrepresentable.
    if not isinstance(subject_ref, str) or not _FULL_HEX_REF.fullmatch(
        subject_ref.strip() if isinstance(subject_ref, str) else ""
    ):
        raise ReviewCreationRefusedError(
            "instrument: `subject_ref` must be the subject's FULL 40-hex git commit id "
            "at review creation (take it from `git rev-parse HEAD`) — it anchors every "
            "temporary-state entry and every later credit tracks from it (B.14 D-2)"
        )
    subject_ref = subject_ref.strip()
    frozen: list[dict] = []
    seen_ids: set[str] = set()
    for i, entry in enumerate(register):
        where = f"temporary_states[{i}]"
        if not isinstance(entry, dict):
            raise ReviewCreationRefusedError(f"instrument: {where} must be an object")
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ReviewCreationRefusedError(
                f"instrument: {where} requires a non-empty string `entry_id`"
            )
        if entry_id in seen_ids:
            raise ReviewCreationRefusedError(
                f"instrument: temporary_states carries duplicate entry_id {entry_id!r} — "
                "each declared occurrence gets its own id (B.14 D-2)"
            )
        seen_ids.add(entry_id)
        target = entry.get("target")
        if not isinstance(target, dict):
            raise ReviewCreationRefusedError(
                f"instrument: {where} requires a `target` object with the quoted span "
                "and its auxiliary locator"
            )
        quote = target.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ReviewCreationRefusedError(
                f"instrument: {where} target carries no quoted span — the quote IS the "
                "binding coordinate of what the declaration excuses (B.14 D-2)"
            )
        locator = target.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            raise ReviewCreationRefusedError(
                f"instrument: {where} target requires a non-empty STRING `locator` "
                "(the per-mode auxiliary coordinate: element id / path::symbol or "
                "range / section id) - got "
                f"{type(locator).__name__} (round 9, locator-shape-unvalidated)"
            )
        # Round 13 (locator-shape-unvalidated REOPENED): the string alone was half
        # the class — the grammar is closed per mode, and a locator of the wrong
        # mode's form is refused with the expected form named.
        grammar_problem = _locator_grammar_problem(locator, mode)
        if grammar_problem is not None:
            raise ReviewCreationRefusedError(
                f"instrument: {where} target locator {locator!r}: {grammar_problem} "
                "(B.14 D-2; round 13)"
            )
        if not isinstance(entry.get("why"), str) or not entry["why"].strip():
            raise ReviewCreationRefusedError(
                f"instrument: {where} requires `why` — the decision the temporary "
                "state rests on"
            )
        if not isinstance(entry.get("closes"), str) or not entry["closes"].strip():
            raise ReviewCreationRefusedError(
                f"instrument: {where} names no closing event (`closes`) — a temporary "
                "state without a named end is not a temporary state but an open "
                "question, and open questions have their own routes (B.14 D-2)"
            )
        frozen.append(
            {
                "entry_id": entry_id,
                "target": {"quote": quote, "locator": locator},
                "why": entry["why"],
                "closes": entry["closes"],
                "declared_at": subject_ref,
            }
        )
    return frozen


async def freeze_instruments(
    session: AsyncSession, *, config: dict | None, instrument: dict | None,
    mode: str | None = None,
) -> dict:
    """Resolve, validate and freeze the review's instruments at creation (D-1).

    Returns the snapshot dict stored under ``config["instrument"]``; nothing changes it
    mid-review — the instrument does not change in the middle of a measurement. Raises
    ``ReviewCreationRefusedError`` with a ROUTE for every missing ground (C-5, D-3, D-4).
    """
    if not isinstance(instrument, dict) or not instrument:
        raise ReviewCreationRefusedError(
            "review creation now names its instruments (B.9 D-1): pass `instrument` with "
            '`host` {hostname, username} and, explicitly or via the recorded default, '
            "`critic` {engine, model, effort} and `development` {engine, model}"
        )
    host = instrument.get("host")
    if not isinstance(host, dict):
        raise ReviewCreationRefusedError(
            "instrument: `host` {hostname, username} is required — the host cannot be "
            "defaulted, it is where the critic will actually run (C-5)"
        )
    hostname = host.get("hostname")
    username = host.get("username")
    for name, value in (("hostname", hostname), ("username", username)):
        if not isinstance(value, str) or not value.strip():
            raise ReviewCreationRefusedError(f"instrument host: `{name}` must be a non-empty string")
        # The frozen pair is rendered into the launch script's startup gate — the same
        # whitelist as at profile write (rendered-profile-values-are-shell-injectable).
        try:
            validate_script_identifier(value, what=f"instrument host `{name}`")
        except InstrumentValidationError as e:
            raise ReviewCreationRefusedError(e.reason) from None

    genre = genre_of(config)
    critic_req = instrument.get("critic")
    development_req = instrument.get("development")
    resolved_from_default: int | None = None
    if critic_req is None or development_req is None:
        stored = await active_default_pair_map(session)
        if stored is None:
            raise ReviewCreationRefusedError(
                "instrument: no explicit choice and no active default-instrument record — "
                "choose explicitly at creation, or record a default through the management "
                "endpoint (operator quote + Decision ref mandatory, D-5)"
            )
        pair_map, default_version = stored
        pair = _pair_from_default(pair_map, genre)
        if critic_req is None:
            critic_req = pair["critic"]
        if development_req is None:
            development_req = pair["development"]
        resolved_from_default = default_version

    for role, req in (("critic", critic_req), ("development", development_req)):
        if not isinstance(req, dict):
            raise ReviewCreationRefusedError(f"instrument: `{role}` must be an object")
        for key in ("engine", "model"):
            if not isinstance(req.get(key), str) or not req[key].strip():
                raise ReviewCreationRefusedError(f"instrument {role}: `{key}` must be a non-empty string")

    # C-5 — the profile gate. Exactly two server-visible cases: absent and known-devalued;
    # freshness beyond the stored proof is held by the other two layers (the
    # verify-freshness leaf before creation, the script's startup gate before any pass).
    profile = await get_profile(
        session, hostname=hostname, username=username, engine=critic_req["engine"]
    )
    if profile is None or profile.active_version_id is None:
        raise ReviewCreationRefusedError(
            f"no active, host-proven launch profile for {hostname}/{username} × "
            f"{critic_req['engine']!r} — walk the preparation path: create the profile, "
            "prove a version with its probes on that host, then create the review (C-5/C-6)"
        )
    active_version = await session.get(LaunchProfileVersion, profile.active_version_id)
    if active_version.devalued_at is not None:
        raise ReviewCreationRefusedError(
            f"the active profile version {active_version.version} for {hostname}/{username} × "
            f"{critic_req['engine']!r} is known-devalued — prepare and prove a new version "
            "before creating reviews on it (C-5/C-6)"
        )

    critic_entry = await _resolve_model_entry(
        session, engine=critic_req["engine"], model=critic_req["model"], role="critic"
    )
    development_entry = await _resolve_model_entry(
        session,
        engine=development_req["engine"],
        model=development_req["model"],
        role="development",
    )

    # D-4 — effort is free WITHIN the entry's recorded domain.
    effort = critic_req.get("effort")
    domain = critic_entry.effort_domain
    if domain is None:
        if effort is not None:
            raise ReviewCreationRefusedError(
                f"model {critic_entry.invocation_alias!r} records no effort domain, but "
                f"effort {effort!r} was requested — record the interface's effort domain on "
                "the entry, or drop the effort (D-4)"
            )
    else:
        if effort is None:
            raise ReviewCreationRefusedError(
                f"model {critic_entry.invocation_alias!r} supports efforts {domain} — choose "
                "one (D-1: effort is free within the entry's domain)"
            )
        if effort not in domain:
            raise ReviewCreationRefusedError(
                f"effort {effort!r} is outside the recorded domain {domain} of model "
                f"{critic_entry.invocation_alias!r} (D-4)"
            )

    independence = independence_step(development_entry, critic_entry)

    # D-3 — the hard self-check gate: full coincidence only as a marked self-check with a
    # TYPED operator waiver, frozen into the snapshot and displayed at the pre-review gate.
    waiver = instrument.get("self_check_waiver")
    if independence["step"] == "self_check":
        if not isinstance(waiver, dict) or not all(
            isinstance(waiver.get(k), str) and waiver[k].strip()
            for k in ("granted_by", "operator_quote")
        ):
            raise ReviewCreationRefusedError(
                "the declared development model equals the chosen critic model — a "
                "self-check runs only with an operator waiver: pass `self_check_waiver` "
                "{granted_by, operator_quote} (D-3)"
            )
        waiver = {"granted_by": waiver["granted_by"], "operator_quote": waiver["operator_quote"]}
    else:
        waiver = None

    development_frozen = {
        "engine": development_req["engine"],
        "model": development_entry.invocation_alias,
        "provider": development_entry.provider,
        "provider_model_id": development_entry.provider_model_id,
        "family": development_entry.family,
    }
    if "ultracode" in development_req:
        development_frozen["ultracode"] = bool(development_req["ultracode"])

    # B-5 freshness seam 1 (finding b9-threat-context-not-a-freshness-input): the
    # operator-confirmed threat context is a CREATION input, frozen with everything
    # else the gate settled — so no pass can ever judge under an absent frame (the
    # pre-review gate confirms exactly these values before creation). Mid-review
    # restatement rides the latest-binds `threat_context` notice, which overrides
    # this initial record in the served frame.
    context = instrument.get("threat_context")
    if not isinstance(context, dict):
        raise ReviewCreationRefusedError(
            "instrument: `threat_context` {threat_model, operating_scale, granted_by} "
            "is required — the operator-confirmed positive threat model and operating "
            "scale from the pre-review gate; without it the critic's standing frame "
            "has no declared model to draw adversaries from (B-5)"
        )
    for key in ("threat_model", "operating_scale", "granted_by"):
        if not isinstance(context.get(key), str) or not context[key].strip():
            raise ReviewCreationRefusedError(
                f"instrument threat_context: `{key}` must be a non-empty string"
            )
    context = {k: context[k] for k in ("threat_model", "operating_scale", "granted_by")}

    # B.14 A-3/A-4 — the SUBJECT root is a per-review creation input, required and
    # explicit for every review including reviews of the machinery repository itself.
    # No defaulting: a default of "the machinery root" would silently re-create the
    # two-role reading the split exists to kill. Validated like profile paths are at
    # profile write — non-empty and ABSOLUTE under the shell of the host's profile
    # (creation already requires a proven profile, so the shell is known here).
    subject_root = instrument.get("subject_root")
    if not isinstance(subject_root, str) or not subject_root.strip():
        raise ReviewCreationRefusedError(
            "instrument: `subject_root` is required (B.14 A-3) — the absolute path, on "
            "the launching host, of the repository under review (the critic's working "
            "directory, where coverage runs git). There is no default: name it even "
            "when it equals the machinery root"
        )
    shell = (active_version.content or {}).get("shell")
    windows_shell = shell == "cmd"
    absolute_for_shell = (
        bool(_WINDOWS_ABS.match(subject_root)) or subject_root.startswith("\\\\")
        if windows_shell
        else subject_root.startswith("/")
    )
    if not absolute_for_shell:
        raise ReviewCreationRefusedError(
            f"instrument: `subject_root` must be an ABSOLUTE path under the host "
            f"profile's shell ({shell!r}); got {subject_root!r} (B.14 A-4)"
        )

    temporary_states = _frozen_temporary_states(instrument, mode=mode)

    return {
        "threat_context": context,
        "subject_root": subject_root,
        "temporary_states": temporary_states,
        "host": {"hostname": hostname, "username": username},
        "critic": {
            "engine": critic_req["engine"],
            "model": critic_entry.invocation_alias,
            "provider": critic_entry.provider,
            "provider_model_id": critic_entry.provider_model_id,
            "family": critic_entry.family,
            "effort": effort,
        },
        "development": development_frozen,
        "profile_id": str(profile.id),
        "profile_version_id": str(active_version.id),
        "profile_version": active_version.version,
        "independence": independence,
        "self_check_waiver": waiver,
        "resolved_from_default": resolved_from_default,
    }
