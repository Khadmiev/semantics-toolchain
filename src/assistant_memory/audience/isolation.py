# SPDX-License-Identifier: Apache-2.0
"""Arranging the blind reader's isolation — the part that was never in the product.

WHAT WAS WRONG. The profile module opens by saying isolation "cannot be asked for, only
arranged: a separate settings directory, an empty working directory outside every
repository, read-only". Every word of that was true of the ONE hand-run script that lived
outside the repository, and of nothing in the package: the model call was an injected
callable and nobody owned the arrangement. An assertion in prose with no mechanism — the
exact class this genre's review spent twelve rounds removing, sitting in the module that
states the claim.

So the arrangement lives here, and it is arranged rather than checked afterwards.

THREE THINGS, AND ONLY THE FIRST IS OBVIOUS.

1. A working directory that is EMPTY and OUTSIDE every repository. `read-only` forbids
   writing, not reading: a run whose working directory sits inside the project has the
   project at hand. This is about what is reachable, not about what was executed.

2. The profile's TRANSIENT state is wiped before each run, and this is not tidiness. The
   tool leaves sessions, history and caches in its own settings directory, so the second
   blind reading of one deck would carry the first one's session — and the whole point of
   the blind pass is that it does not know what the previous reading concluded. This is the
   third leak channel, the one the canary exists to detect, and wiping it locally is the
   half we control. What the provider remembers on ITS side is not touched by this and is
   not claimed to be: that is what the canary asks the model about.

3. The cleanup REFUSES TO DELETE EVIDENCE. A working directory that is not empty after a run
   means something wrote where nothing should have, and a cleaner that erases that erases a
   signal. It is kept, and it is named in the result.

WHAT IS NOT HERE, deliberately: any check that tries to prove the reader read nothing. That
proof does not exist (a scan shows presence, never absence), the operator ruled twice that
the genre does not need it, and adding it here would be the same prop under the same
unprovable guarantee. Arrangement, not proof.

THE ACCOUNT CHANNEL IS OUTSIDE THIS CLAIM, ruled by the operator 2026-08-05 after a live
probe. The runner re-syncs the ACCOUNT's remote skills and plugins into the profile on
every launch — the probe found project-named material delivered into the blind profile
that way — and no local hygiene can reach that channel: it is keyed to ``auth.json``, not
to files, and wiping the directories only makes the runner download them again. So the
claim this module makes stops at the machine's edge: what the profile DIRECTORY carries is
arranged and wiped; what the account carries is held back by the PROMPT's instruction
alone ("you may not use knowledge beyond what is below"), and that is a request, not an
arrangement. Named here so nobody reads profile isolation as account isolation; the
structural fix, if the genre ever needs it, is a separate clean account for the blind
profile — a deployment decision, not a mechanism to build here.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class IsolationRefused(RuntimeError):
    """The blind run will not be launched, with every reason rather than the first."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class WorkArea:
    """The empty directory a blind run is launched in, and what was found there after."""

    path: Path
    #: Files that appeared during the run. Empty on the happy path; non-empty is evidence.
    residue: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.residue


def repository_roots(start: Path) -> tuple[Path, ...]:
    """Every repository root at or above ``start`` — what the working directory must avoid."""
    roots = []
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            roots.append(candidate)
    return tuple(roots)


def prepare_work_area(root: Path, *, forbidden: Sequence[Path] = ()) -> WorkArea:
    """Make (or empty) the working directory, refusing anything inside a repository.

    Created empty rather than merely required to be empty: "the caller will pass a clean
    directory" is precisely the kind of promise that was keeping this genre's isolation
    alive in a script nobody reviewed.
    """
    resolved = root.resolve()
    reasons = []
    for repo in (*forbidden, *repository_roots(resolved)):
        repo = repo.resolve()
        if resolved == repo or repo in resolved.parents:
            reasons.append(
                f"рабочий каталог {resolved} лежит внутри репозитория {repo} — "
                "режим «только чтение» запрещает запись, а не чтение, и проект оказался бы "
                "у слепого читателя под рукой"
            )
    if reasons:
        raise IsolationRefused(reasons)

    if resolved.exists():
        for entry in resolved.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    else:
        resolved.mkdir(parents=True)
    return WorkArea(path=resolved)


def collect_residue(area: WorkArea) -> WorkArea:
    """What the run left in its working directory. Nothing is deleted here — see the note."""
    residue = tuple(sorted(str(p.relative_to(area.path)) for p in area.path.rglob("*")))
    return WorkArea(path=area.path, residue=residue)


def release_work_area(area: WorkArea) -> WorkArea:
    """Remove the working directory ONLY if the run left it empty.

    A non-empty directory is not rubbish to sweep: something wrote where the sandbox forbids
    writing, and that is worth more than the disk it occupies. It stays, and the caller is
    told, so the anomaly reaches the record instead of the recycle bin.
    """
    checked = collect_residue(area)
    if checked.clean:
        shutil.rmtree(checked.path, ignore_errors=True)
    return checked


#: Files whose presence is the profile's CONFIGURATION rather than its leavings. Imported
#: from the profile rather than restated: the set the fingerprint is taken over and the set
#: the cleaning preserves have to be the same set, or cleaning would move the fingerprint and
#: break the binding it exists to protect.
def _configuration_names(config_globs: Sequence[str]) -> set[str]:
    return {name.lower() for name in config_globs}


@dataclass
class ProfileHygiene:
    """Wipe the blind profile's transient state, keeping exactly its configuration."""

    settings_dir: Path
    config_globs: Sequence[str]
    #: Set by the caller when the operator wants to see what would go before it goes.
    dry_run: bool = False
    removed: list[str] = field(default_factory=list)

    def clean(self) -> list[str]:
        """Remove everything that is not configuration. Returns what was (or would be) removed.

        Everything, not a curated list of known caches: a denylist over a directory the tool
        repopulates on every launch goes stale the first time the tool adds a subdirectory,
        and it goes stale silently — the failure mode is a session file nobody thought to
        name surviving into the next blind reading.
        """
        if not self.settings_dir.exists():
            return []
        keep = _configuration_names(self.config_globs)
        for entry in sorted(self.settings_dir.iterdir()):
            if entry.name.lower() in keep:
                continue
            self.removed.append(entry.name)
            if self.dry_run:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        return list(self.removed)


@dataclass(frozen=True)
class BlindRunner:
    """Launches the model in the arranged isolation and returns its transcript.

    The runner binary is PINNED by absolute path and never resolved through the environment:
    this box carries three installations and a bare name picks the oldest, which refuses the
    configured model outright. Learned the expensive way.

    Both streams are captured together, and that is load-bearing rather than tidy: the
    provider prints its header — the sandbox and approval modes the whole isolation claim
    rests on — to the error stream. Taking only the output stream throws away the evidence.
    """

    binary: Path
    settings_dir: Path
    model: str
    timeout_seconds: int = 1800
    sandbox: str = "read-only"
    approval: str = "never"

    def command(self) -> list[str]:
        # THE FLAGS ARE THE PROVIDER'S, and they were guessed once. `codex exec` has no
        # `--ask-for-approval`: approval is not a flag of the non-interactive form at all,
        # it is stated by the runner in its own header, which is where this genre reads it
        # from anyway. `--skip-git-repo-check` is required rather than tidy: the working
        # directory is deliberately empty and outside every repository, and without this the
        # runner refuses to start there. Found by the first live run, not by review — the
        # tests replaced the runner with a function and could not see its argument parser.
        return [
            str(self.binary),
            "exec",
            "-s", self.sandbox,
            "--skip-git-repo-check",
            "--model", self.model,
            "--color", "never",
            "-",
        ]

    def environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(self.settings_dir)
        return env

    def __call__(self, prompt: str, *, work_area: WorkArea) -> str:
        proc = subprocess.run(  # noqa: S603 — argv form, never a shell string
            self.command(),
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work_area.path,
            env=self.environment(),
            timeout=self.timeout_seconds,
        )
        # STDERR FIRST: the header — the provider's own statement about the launch, and the
        # strongest isolation evidence there is — is printed there, and the header parser
        # requires it at the START of the transcript. Putting stdout first left the header
        # buried mid-file and unparsed, which reads as "no attestation" and annuls the run.
        merged = (proc.stderr or "") + (proc.stdout or "")
        if proc.returncode != 0:
            raise IsolationRefused(
                [f"запуск вернул код {proc.returncode}: {merged[-1500:]}"]
            )
        return merged


#: A version-shaped token in the runner's own `--version` output ("codex-cli 0.147.0").
_VERSION_TOKEN = re.compile(r"\d+\.\d+\.\d+\S*")


def probe_runner_version(binary: Path, *, timeout_seconds: float = 30.0) -> str | None:
    """The LIVE runner version, probed from the configured binary — or None, loudly not
    a value.

    The genre's own environment probe (operator decision, review b5fd70de round 1): the
    launch profiles pin the binary's PATH and prove its operability, but no fingerprint
    anywhere carries the binary's VERSION — an in-place CLI update moves nothing. So
    the environment is PROBED, in the layer that already owns environment truth, and
    the transfer gates hold the probed value against the source record's own
    ``runner_version``. ``None`` (binary missing, non-zero exit, unparsable output) is
    fail-closed at the consumer: no probed version → no transfer.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv form, never a shell string
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    match = _VERSION_TOKEN.search((proc.stdout or "") + (proc.stderr or ""))
    return match.group(0) if match else None


@dataclass
class ArrangedIsolation:
    """One arrangement, reusable for the canary and the reading that follows it.

    ONE arrangement for both on purpose: the pair is credited only if both ran under the same
    profile fingerprint, so re-arranging between them would be re-arranging exactly what the
    binding compares.
    """

    runner: BlindRunner
    work_root: Path
    hygiene: ProfileHygiene
    _area: WorkArea | None = None

    def prepare(self) -> list[str]:
        """Wipe the transient profile state and make the empty working directory."""
        removed = self.hygiene.clean()
        self._area = prepare_work_area(self.work_root)
        return removed

    def invoke(self, prompt: str) -> str:
        if self._area is None:
            raise IsolationRefused(
                ["изоляция не устроена: prepare() не вызывался — запуск без неё бессмыслен"]
            )
        return self.runner(prompt, work_area=self._area)

    def finish(self) -> WorkArea:
        if self._area is None:
            raise IsolationRefused(["изоляция не устраивалась — нечего закрывать"])
        return release_work_area(self._area)


#: What the launcher needs: a callable taking the prompt and returning the transcript.
Invoke = Callable[[str], str]
