# SPDX-License-Identifier: Apache-2.0
"""Where one audience review lives on disk, and what it must declare to run.

WHY A LAYOUT AT ALL. Everything this genre refuses to guess has to come from somewhere: the
contract in two parts, the reader artefact, the marker list, the term inventories, the
reader's budget, which model is doing the reading. Scattered across command-line flags they
would be re-typed every round and would drift between rounds; held in the driver they would
be invented. So they are declared once, in one directory, and the driver reads them.

WHAT IS DECLARED VERSUS WHAT IS MACHINE-LOCAL. Two different lifetimes live here and mixing
them is how a review becomes unrunnable on anyone else's machine. The review's own facts —
contract, artefact, markers, inventories, budgets — belong to the review and travel with the
repository. The paths — where the blind profile's settings are, where the runner binary is,
where the empty working directory goes — belong to the MACHINE, and the declared build
horizon says other operators run this too, so they are settings and never constants.

REFUSALS NAME THE FILE. A missing declaration is reported as "this file, this field" rather
than as a stack trace, because the person who has to fix it is the operator, and the genre's
own rule applies to its own inputs: silence must not be able to look like consent.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from assistant_memory.audience.blind_prompt import RoleTemplate
from assistant_memory.audience.contract import AudienceContract, load_contract
from assistant_memory.audience.mechanical import MechanicalThresholds, TermInventory
from assistant_memory.audience.report_schema import SectionBudgets


class LayoutRefused(ValueError):
    """The review cannot be assembled from disk, with every reason at once."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


#: The file names are fixed rather than configurable. One less thing to declare, and a
#: directory whose shape is the same everywhere can be recognised by a person at a glance.
CONTRACT_PUBLIC = "contract_public.md"
CONTRACT_PRIVATE = "contract_private.md"
ARTIFACT = "artifact.md"
DECLARATIONS = "review.toml"
TEMPLATE = "blind_reader.md"
#: The v2 role templates — OPTIONAL files: the review config decides whether a role runs,
#: and the driver refuses an enabled role whose template is absent (never silently skips).
STRATEGIC_TEMPLATE = "strategic_reader.md"
MACHINE_TEMPLATE = "machine_comb.md"
RUNS_DIR = "runs"
JOURNAL = "journal.jsonl"


@dataclass(frozen=True)
class MachineSettings:
    """Paths that belong to this machine and to no review."""

    settings_dir: Path
    work_root: Path
    runner_binary: Path


@dataclass(frozen=True)
class Declarations:
    """What the review declares because the genre refuses to infer it."""

    model: str
    model_version: str
    markers: tuple[str, ...]
    inventory: TermInventory
    budgets: Mapping[str, float]
    thresholds: MechanicalThresholds
    #: Word budgets of the blind report's sections — knobs like the mechanical thresholds:
    #: the values live here, ride into the prompt and the run record, and two runs are
    #: comparable exactly when the numbers match.
    report_budgets: SectionBudgets
    machine: MachineSettings


@dataclass(frozen=True)
class ReviewLayout:
    """One audience review's directory."""

    root: Path

    @property
    def contract_public(self) -> Path:
        return self.root / CONTRACT_PUBLIC

    @property
    def contract_private(self) -> Path:
        return self.root / CONTRACT_PRIVATE

    @property
    def artifact(self) -> Path:
        return self.root / ARTIFACT

    @property
    def declarations(self) -> Path:
        return self.root / DECLARATIONS

    @property
    def template(self) -> Path:
        return self.root / TEMPLATE

    @property
    def strategic_template(self) -> Path:
        return self.root / STRATEGIC_TEMPLATE

    @property
    def machine_template(self) -> Path:
        return self.root / MACHINE_TEMPLATE

    @property
    def runs_dir(self) -> Path:
        return self.root / RUNS_DIR

    @property
    def journal(self) -> Path:
        # OUTSIDE the profile's settings directory by construction — a journal inside it
        # would both leak a log of project runs to the blind reader and move the very
        # fingerprint it exists to protect, mid-run.
        return self.root / JOURNAL

    def missing(self) -> list[str]:
        return [
            f"нет файла {path.name} в каталоге ревью ({self.root})"
            for path in (
                self.contract_public,
                self.contract_private,
                self.artifact,
                self.declarations,
                self.template,
            )
            if not path.exists()
        ]


def _require(table: Mapping[str, object], key: str, where: str, reasons: list[str]):
    value = table.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        reasons.append(f"{DECLARATIONS}: не объявлено «{key}» в разделе [{where}]")
    return value


def load_declarations(path: Path) -> Declarations:
    """Read the declarations file, refusing with every missing field named."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as problem:
        raise LayoutRefused([f"{path} не читается как TOML: {problem}"]) from None

    reasons: list[str] = []
    reader = raw.get("reader") or {}
    audience = raw.get("audience") or {}
    machine = raw.get("machine") or {}

    model = _require(reader, "model", "reader", reasons)
    model_version = _require(reader, "model_version", "reader", reasons)
    markers = audience.get("canary_markers") or []
    if not [m for m in markers if str(m).strip()]:
        reasons.append(
            f"{DECLARATIONS}: пустой «canary_markers» в разделе [audience] — критерий, "
            "которому удовлетворяет любой ответ, не отличает чистый прогон от грязного"
        )
    # An EMPTY known list is a legitimate declaration; an ABSENT one is not. The difference
    # is the whole reason these are declared rather than inferred, so it is checked here.
    if "known_terms" not in audience:
        reasons.append(
            f"{DECLARATIONS}: нет «known_terms» в разделе [audience] — пустой список это "
            "объявление («аудитория не знает ничего»), а отсутствие ключа объявлением не "
            "является, и отчёт по невыведенному инвентарю неотличим от чистого"
        )
    if "introduced_terms" not in audience:
        reasons.append(f"{DECLARATIONS}: нет «introduced_terms» в разделе [audience]")

    settings_dir = _require(machine, "settings_dir", "machine", reasons)
    work_root = _require(machine, "work_root", "machine", reasons)
    runner_binary = _require(machine, "runner_binary", "machine", reasons)

    if reasons:
        raise LayoutRefused(reasons)

    thresholds_table = raw.get("thresholds") or {}
    unknown = sorted(set(thresholds_table) - set(MechanicalThresholds().__dict__))
    if unknown:
        raise LayoutRefused(
            [
                f"{DECLARATIONS}: в разделе [thresholds] неизвестные ключи: "
                f"{', '.join(unknown)} — порог, которого нет, молча не применяется"
            ]
        )

    report_table = raw.get("report_budgets") or {}
    unknown = sorted(set(report_table) - set(SectionBudgets().__dict__))
    if unknown:
        raise LayoutRefused(
            [
                f"{DECLARATIONS}: в разделе [report_budgets] неизвестные ключи: "
                f"{', '.join(unknown)} — бюджет, которого нет, молча не применяется"
            ]
        )
    report_budgets = SectionBudgets(**report_table)
    broken = report_budgets.refusals()
    if broken:
        raise LayoutRefused([f"{DECLARATIONS}: [report_budgets]: {reason}" for reason in broken])

    return Declarations(
        model=str(model),
        model_version=str(model_version),
        markers=tuple(str(m) for m in markers if str(m).strip()),
        inventory=TermInventory(
            known=frozenset(str(t) for t in audience.get("known_terms", [])),
            introduced=frozenset(str(t) for t in audience.get("introduced_terms", [])),
        ),
        budgets={str(k): float(v) for k, v in (audience.get("budgets") or {}).items()},
        thresholds=MechanicalThresholds(**thresholds_table),
        report_budgets=report_budgets,
        machine=MachineSettings(
            settings_dir=Path(str(settings_dir)).expanduser(),
            work_root=Path(str(work_root)).expanduser(),
            runner_binary=Path(str(runner_binary)).expanduser(),
        ),
    )


@dataclass(frozen=True)
class LoadedReview:
    """Everything one round needs, read from one directory."""

    layout: ReviewLayout
    contract: AudienceContract
    template: RoleTemplate
    artifact_text: str
    declarations: Declarations
    #: The v2 role templates — present when their files are; whether a role RUNS is the
    #: review config's declaration, and the driver holds the two against each other.
    strategic_template: RoleTemplate | None = None
    machine_template: RoleTemplate | None = None


def load_review(root: Path) -> LoadedReview:
    """Assemble a review from its directory, or refuse naming every missing piece."""
    from assistant_memory.audience.role_prompts import MACHINE_SLOTS, STRATEGIC_SLOTS

    layout = ReviewLayout(root=root)
    missing = layout.missing()
    if missing:
        raise LayoutRefused(missing)
    contract = load_contract(layout.contract_public, layout.contract_private)
    strategic_template = (
        RoleTemplate.from_file(layout.strategic_template, slots=STRATEGIC_SLOTS)
        if layout.strategic_template.exists()
        else None
    )
    machine_template = (
        RoleTemplate.from_file(layout.machine_template, slots=MACHINE_SLOTS)
        if layout.machine_template.exists()
        else None
    )
    return LoadedReview(
        layout=layout,
        contract=contract,
        template=RoleTemplate.from_text(layout.template.read_text(encoding="utf-8")),
        artifact_text=layout.artifact.read_text(encoding="utf-8"),
        declarations=load_declarations(layout.declarations),
        strategic_template=strategic_template,
        machine_template=machine_template,
    )
