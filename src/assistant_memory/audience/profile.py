# SPDX-License-Identifier: Apache-2.0
"""The blind reader's launch profile, and its fingerprint by CONTENTS.

Isolation cannot be asked for, only arranged: a separate settings directory holding
nothing but the authorisation and the model, an empty working directory outside every
repository, a read-only sandbox. This module models that profile and, more importantly,
fingerprints it in the one way that is worth anything.

WHY CONTENTS AND NOT THE PATH. An earlier draft hashed the path to the settings directory.
The path survives having the settings swapped underneath it, and it survives a
re-authorisation onto a different account — so equality of paths proved exactly nothing
about the state the canary measured. Hashing the contents makes both of those visible,
and it removes the need for a separate "account context" field: the account lives inside
the profile as its authorisation file.

STATED CONSEQUENCE, not hidden: a token refresh between the canary and the reading also
changes the fingerprint and breaks the binding, so the run is not credited and is re-run.
Refusing in the direction of not-counted is the deliberate choice.

RAW VALUES ARE NEVER RECORDED — only hashes. The settings directory holds credentials;
a fingerprint that leaked them would defeat the isolation it exists to prove.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

#: Read files in chunks: a settings directory is small, but nothing here should assume it.
_CHUNK = 1 << 16

#: What COUNTS as the profile's configuration — an allowlist, not a denylist.
#:
#: Measured, not assumed. A single run rewrites 5266 files in a temp directory inside the
#: profile, refreshes 54 system-skill files, 7 plugin-catalogue files and the model cache.
#: A denylist over a directory the tool repopulates on every launch is unmaintainable in the
#: worst way: a new subdirectory appears, the fingerprint silently stops matching, and the
#: binding refuses every honest pair — a check that always refuses teaches people to skip it.
#:
#: So the question the fingerprint answers is stated narrowly and honestly: is this the same
#: profile as the operator CONFIGURED it — same credentials, same model settings? Everything
#: the tool leaves lying around is outside that question.
#:
#: What it costs, stated plainly rather than discovered later: a plugin or skill appearing in
#: the profile does NOT change the fingerprint. That is the intended reading under the
#: declared horizon — whether the reader carries knowledge is what the canary asks, and it
#: asks the model rather than the directory. Widen this list, never the denylist, if that
#: judgement ever changes.
DEFAULT_CONFIG_GLOBS: tuple[str, ...] = (
    "auth.json",
    "config.toml",
    "instructions.md",
    "AGENTS.md",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ProfileFingerprint:
    """What was hashed, and the digest — kept together so a record can be explained.

    ``files`` is the per-file evidence (name → digest). It is carried alongside the
    single ``digest`` because a bare hash answers "did it change" and nothing else: when
    a pair fails to bind, the operator needs to see WHICH file moved, and re-deriving that
    after the fact is impossible once the directory has changed again.
    """

    digest: str
    files: tuple[tuple[str, str], ...]
    model: str
    extensions: tuple[str, ...]
    #: What counted as configuration, carried so a record explains itself: "these two
    #: fingerprints match" means little without knowing what was compared.
    covered: tuple[str, ...] = ()

    def differs_from(self, other: ProfileFingerprint) -> list[str]:
        """Human-readable reasons this fingerprint is not the other one (empty = equal)."""
        if self.digest == other.digest:
            return []
        reasons: list[str] = []
        if self.model != other.model:
            reasons.append(f"модель: {self.model} против {other.model}")
        if self.extensions != other.extensions:
            reasons.append(
                f"расширения: {list(self.extensions)} против {list(other.extensions)}"
            )
        mine, theirs = dict(self.files), dict(other.files)
        for name in sorted(set(mine) | set(theirs)):
            if name not in theirs:
                reasons.append(f"файл только слева: {name}")
            elif name not in mine:
                reasons.append(f"файл только справа: {name}")
            elif mine[name] != theirs[name]:
                reasons.append(f"содержимое изменилось: {name}")
        return reasons or ["отпечатки различны, но пофайлово совпадают — расходится состав"]


@dataclass(frozen=True)
class LaunchProfile:
    """A configurable launch profile — never a hardcoded path.

    Configurability is not a convenience: the declared build horizon is publication, and
    another operator runs this on their own machine, so a wired-in path is inadmissible.
    """

    settings_dir: Path
    model: str
    extensions: tuple[str, ...] = ()
    config_globs: tuple[str, ...] = field(default=DEFAULT_CONFIG_GLOBS)

    def is_configuration(self, relative: str) -> bool:
        """Is this file part of what the operator configured, as opposed to tool debris?"""
        return any(fnmatch.fnmatch(relative, glob) for glob in self.config_globs)

    def fingerprint(self) -> ProfileFingerprint:
        """Hash the CONFIGURATION in the settings directory, plus the model and extensions.

        Every configuration file participates, recursively and in sorted order, so the
        digest does not depend on filesystem enumeration order. What a run writes into the
        profile is skipped by ``state_globs`` — see the note there for why, and for what
        that costs.
        """
        root = self.settings_dir
        if not root.is_dir():
            raise FileNotFoundError(f"каталог настроек профиля не найден: {root}")

        files: list[tuple[str, str]] = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            relative = path.relative_to(root).as_posix()
            if not self.is_configuration(relative):
                continue
            files.append((relative, _sha256_file(path)))
        if not files:
            raise FileNotFoundError(
                f"в каталоге {root} нет ни одного файла конфигурации из {self.config_globs} — "
                "отпечаток по пустому множеству совпал бы с любым другим таким же"
            )

        material = "\n".join(
            [f"model={self.model}", "extensions=" + ",".join(self.extensions)]
            + [f"{name}={digest}" for name, digest in files]
        )
        return ProfileFingerprint(
            digest=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            files=tuple(files),
            model=self.model,
            extensions=tuple(self.extensions),
            covered=tuple(self.config_globs),
        )
