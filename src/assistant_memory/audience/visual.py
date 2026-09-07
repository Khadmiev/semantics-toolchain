# SPDX-License-Identifier: Apache-2.0
"""The visual pass: what only the assembled render can show.

WHO RUNS THIS, AND WHY IT IS NOT THE BLIND READER. In this genre the two model roles swap
between phases, by operator decision 2026-08-05. While the MEANING is being built, one side
writes the artefact and the other criticises it; once the meaning is settled, the finished
text goes to the side that BUILDS THE VISUAL, and the first side becomes the critic of that
visual. So the visual pass is a SEEING pass: to tell a picture that carries a fact nobody
recorded from an ordinary illustration, the reader has to hold the claim map and the
contract's ceiling of rights. None of the blindness machinery applies to it — no isolated
profile, no canary, no creditable pair — and pretending otherwise would be theatre.

What stays from the rest of the genre is the part that matters: the side that produces the
visual does not sign it off. That is the same rule as everywhere else here.

WHAT IT LOOKS FOR. Two of the unconditional floor's classes exist in a form the text passes
physically cannot see:

- a picture introduces a material fact that no claim-map row covers — the chart says "it
  doubled" and the text never said so, therefore nobody ever verified it;
- real data reaches the audience as an image — a screenshot of a live dashboard breaches
  the declared ceiling of rights while every line of the text stays inside it.

Plus two lighter ones: does the picture belong to its section's subject, and is the style
one style.

WHAT IS MECHANICAL HERE AND WHAT IS NOT. The pass itself is judgement — a model looks at
pictures. What this module does is everything AROUND that judgement which a machine can
actually hold: the render is addressed section by section so a finding has an address; the
pass is keyed to the exact bytes it looked at, so a finding can never outlive the picture it
was raised against; and the difference between "fixed in the visual layer" and "the source
moved" is DERIVED from the two source digests rather than taken on trust — because that
difference decides whether the blind reading has to happen again, and the side reporting it
is the side that would rather it did not.

WHAT THE BUILD IDENTITY IS FOR. The same source rendered on another machine is a different
render: fonts substitute, lines re-wrap, a slide overflows. So the build environment is part
of the record. It is DECLARED by the side that built it and the machine checks its presence
and shape, never its truth — the honest limit, stated rather than dressed up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from assistant_memory.audience.ledger import LedgerError, canonical_number
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord
from assistant_memory.audience.sections import ReaderArtifact


class VisualRefused(ValueError):
    """The pass will not run, with every reason rather than the first."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class VisualCategory(StrEnum):
    """The closed list. Two unconditional-floor classes first, then the two lighter ones.

    Closed for the same reason the blind reader's categories are: a finding with nowhere to
    land drains into the vaguest available shelf, and both its weight and its routing are
    lost. The first two are not style opinions — they are the floor, in visual form.
    """

    OWNERLESS_CLAIM = "БЕСХОЗНЫЙ КЛЕЙМ НА КАРТИНКЕ"
    CEILING_BREACH = "ВЫХОД ЗА ПОТОЛОК ПРАВ"
    OFF_SUBJECT = "КАРТИНКА НЕ ПРО ТО"
    STYLE_BREAK = "РАЗНОБОЙ В СТИЛЕ"


#: The two classes a visual finding may never be waived away as taste. Kept as data rather
#: than as prose so the gate can check it instead of a reviewer remembering it.
FLOOR_CATEGORIES: frozenset[VisualCategory] = frozenset(
    {VisualCategory.OWNERLESS_CLAIM, VisualCategory.CEILING_BREACH}
)


@dataclass(frozen=True)
class RenderedSlide:
    """One rendered section: which section it is, where the file is, and what is in it."""

    section_id: str
    path: str
    sha256: str

    @property
    def number(self) -> int:
        return canonical_number("S", self.section_id)


@dataclass(frozen=True)
class RenderManifest:
    """What was rendered, from what, and in what environment — declared by the builder.

    ``source_version`` and ``source_sha256`` are BOTH carried for the reason the contract
    carries both: a number is a label the same side sets that edits the text, so an edit
    without a version bump would pass unnoticed, and the digest is what notices it.
    """

    source_version: int
    source_sha256: str
    slides: tuple[RenderedSlide, ...]
    #: Builder version plus the fonts it had, with their hashes — declared, not derived.
    #: The machine checks that it is there and non-blank; its truth rests on the builder.
    build_env_id: str

    def render_sha256(self) -> str:
        """Identity of the whole render: the slide digests, in section order.

        Over the DIGESTS rather than the files: the pass is keyed to what it looked at, and
        this identity has to be computable from the manifest alone — by the side that did
        not build the render and may not have the image files at hand.
        """
        ordered = sorted(self.slides, key=lambda slide: slide.number)
        payload = "\n".join(f"{slide.section_id}:{slide.sha256}" for slide in ordered)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _blank(value: object) -> bool:
    return isinstance(value, str) and not value.strip()


def manifest_refusals(manifest: RenderManifest, artifact: ReaderArtifact) -> list[str]:
    """Why this render may not be reviewed (empty list = it may). Pure: changes nothing."""
    reasons: list[str] = []
    if _blank(manifest.source_sha256):
        reasons.append("не объявлен хэш исходника — рендер не привязан ни к какой версии текста")
    if _blank(manifest.build_env_id):
        reasons.append(
            "не объявлена среда сборки — тот же исходник на другой машине даёт другую "
            "вёрстку, и находка по этому рендеру не воспроизводится"
        )
    if manifest.source_version <= 0:
        reasons.append(f"версия исходника не положительна: {manifest.source_version}")

    seen: set[str] = set()
    for slide in manifest.slides:
        try:
            canonical_number("S", slide.section_id)
        except LedgerError as refusal:
            reasons += refusal.reasons
            continue
        if slide.section_id in seen:
            reasons.append(f"{slide.section_id}: в рендере два изображения на один раздел")
        seen.add(slide.section_id)
        if _blank(slide.sha256):
            reasons.append(f"{slide.section_id}: у изображения не объявлен хэш")
        if _blank(slide.path):
            reasons.append(f"{slide.section_id}: у изображения не объявлен путь")

    declared = set(artifact.section_ids())
    # BOTH directions, and neither is a formality. A section with no picture is a section
    # nobody looked at, and the pass would report clean over it; a picture with no section is
    # a slide the reader will see and the text passes never covered.
    for missing in sorted(declared - seen):
        reasons.append(f"{missing}: раздел есть в артефакте, а изображения для него нет")
    for extra in sorted(seen - declared):
        reasons.append(
            f"{extra}: изображение есть, а такого раздела в артефакте нет — этот слайд "
            "не проходил ни одной текстовой проверки"
        )
    return reasons


@dataclass(frozen=True)
class VisualFinding:
    """One finding, bound to the exact render it was raised against."""

    category: VisualCategory
    section_id: str
    what: str
    #: The render this was seen on. A finding never outlives the picture it was raised
    #: against: the fix IS a new render, so carrying it forward would report a defect
    #: against bytes nobody has looked at.
    render_sha256: str

    @property
    def is_floor(self) -> bool:
        return self.category in FLOOR_CATEGORIES


def stale_findings(
    findings: Sequence[VisualFinding], manifest: RenderManifest
) -> tuple[VisualFinding, ...]:
    """Findings raised against a DIFFERENT render than the one in hand.

    Returned rather than silently dropped: "this was fixed" and "this was raised against a
    picture that no longer exists" are different statements, and only the second one needs
    a fresh look before it can be closed.
    """
    current = manifest.render_sha256()
    return tuple(finding for finding in findings if finding.render_sha256 != current)


@dataclass(frozen=True)
class ChangeVerdict:
    """What moved between two renders, and what that costs downstream."""

    source_moved: bool
    render_moved: bool
    #: The expensive consequence, derived rather than declared: a source edit moves what the
    #: blind reader sees, so the blind reading of the previous version stops standing for
    #: this one. Derived here because the side reporting "only the layout changed" is the
    #: side that would rather not pay for a new blind pass.
    blind_reading_must_be_repeated: bool
    reasons: tuple[str, ...]


def classify_change(previous: RenderManifest, current: RenderManifest) -> ChangeVerdict:
    """Compare two renders: a visual-layer fix, or a new version of the source?

    The rule this implements is the genre's: findings are fixed IN THE VISUAL LAYER, and a
    fix that touches the text is a new version of the source. The difference is not a matter
    of intent — it is two digests — so it is computed here instead of being asked for.
    """
    source_moved = previous.source_sha256 != current.source_sha256
    render_moved = previous.render_sha256() != current.render_sha256()
    reasons: list[str] = []
    if source_moved:
        reasons.append(
            "исходник изменился — это новая версия текста, а не правка вёрстки: слепое "
            "чтение прошлой версии больше не стоит за текущую"
        )
        if previous.source_version >= current.source_version:
            reasons.append(
                f"версия исходника не выросла ({previous.source_version} → "
                f"{current.source_version}) при изменившемся тексте — номер ставит та же "
                "сторона, что правит текст, и правка без смены номера прошла бы незаметно"
            )
    if not render_moved and not source_moved:
        reasons.append("ни исходник, ни рендер не двигались — новый проход не нужен")
    if render_moved and not source_moved:
        reasons.append("сменилась только вёрстка — правка визуального слоя")
    return ChangeVerdict(
        source_moved=source_moved,
        render_moved=render_moved,
        blind_reading_must_be_repeated=source_moved,
        reasons=tuple(reasons),
    )


def pass_is_due(previous: RunRecord | None, manifest: RenderManifest) -> bool:
    """Does this render need a visual pass? Yes unless one already ran on these exact bytes.

    Keyed on the render digest and not on the source: two renders of one source differ
    (fonts, a rebuild), and the pass is a statement about pixels.
    """
    if previous is None or previous.kind is not RunKind.VISUAL:
        return True
    return previous.render_sha256 != manifest.render_sha256()


def visual_record(
    manifest: RenderManifest,
    *,
    run_id: str,
    iteration: int,
    artifact_seq: int,
    profile_digest: str,
    started_at,
    finished_at,
    transcript_path: str,
    transcript_sha256: str,
    model: str,
    model_version: str,
    runner_version: str,
    spend,
    outcome: RunOutcome = RunOutcome.HAPPENED,
    outcome_reason: str | None = None,
    launch_number: int | None = None,
) -> RunRecord:
    """The run record for a visual pass — the same closed schema, its own required fields.

    ``launch_number`` is optional here and required for the blind pair, and that asymmetry is
    the point: the counter exists to prove no unrecorded run slipped between a canary and its
    reading. The visual pass makes no such claim, so demanding the number of it would be
    borrowing the authority of a mechanism it does not use.
    """
    return RunRecord(
        id=run_id,
        kind=RunKind.VISUAL,
        iteration=iteration,
        artifact_seq=artifact_seq,
        profile_digest=profile_digest,
        launch_number=launch_number,
        started_at=started_at,
        finished_at=finished_at,
        transcript_path=transcript_path,
        transcript_sha256=transcript_sha256,
        tool_calls=(),
        model=model,
        model_version=model_version,
        spend=spend,
        outcome=outcome,
        runner_version=runner_version,
        outcome_reason=outcome_reason,
        source_version=manifest.source_version,
        source_sha256=manifest.source_sha256,
        render_sha256=manifest.render_sha256(),
        build_env_id=manifest.build_env_id,
    )
