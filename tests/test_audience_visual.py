# SPDX-License-Identifier: Apache-2.0
"""Step 6 of the audience genre: the visual pass.

What is asserted here is everything AROUND the judgement, because the judgement itself is a
model looking at pictures and no test can stand in for it:

- the render is addressed section by section, in BOTH directions — a section with no picture
  is a section nobody looked at, a picture with no section is a slide no text pass covered;
- a finding cannot outlive the picture it was raised against;
- "fixed in the visual layer" versus "the source moved" is DERIVED from digests, because the
  side reporting it is the side that would rather not pay for a fresh blind reading;
- the visual record is held to its own required fields and NOT to the isolation fields — it
  is a seeing pass, and borrowed authority is worse than none.
"""

from datetime import UTC, datetime, timedelta

import pytest

from assistant_memory.audience.records import RunKind, RunOutcome
from assistant_memory.audience.sections import parse_reader_artifact
from assistant_memory.audience.visual import (
    FLOOR_CATEGORIES,
    RenderedSlide,
    RenderManifest,
    VisualCategory,
    VisualFinding,
    classify_change,
    manifest_refusals,
    pass_is_due,
    stale_findings,
    visual_record,
)

T0 = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)

ARTIFACT = parse_reader_artifact(
    "# Колода\n\nЧасти:\n- слайды: документ\n\n"
    "### S-1 (слайды) — Первый\nТекст.\n\n"
    "### S-2 (слайды) — Второй\nЕщё текст.\n"
)


def _manifest(**over) -> RenderManifest:
    base = dict(
        source_version=3,
        source_sha256="src-a",
        slides=(
            RenderedSlide("S-1", "render/s1.png", "img-a"),
            RenderedSlide("S-2", "render/s2.png", "img-b"),
        ),
        build_env_id="build_pptx 1.2; fonts: Inter=aa11, Georgia=bb22",
    )
    return RenderManifest(**{**base, **over})


# --- addressing: both directions ---------------------------------------------------------


def test_a_section_without_a_picture_is_a_refusal():
    """Otherwise the pass reports clean over a section nobody ever looked at."""
    manifest = _manifest(slides=(RenderedSlide("S-1", "render/s1.png", "img-a"),))
    reasons = manifest_refusals(manifest, ARTIFACT)
    assert any("S-2" in r and "изображения для него нет" in r for r in reasons)


def test_a_picture_without_a_section_is_a_refusal():
    """A slide the audience will see that passed through no text check at all."""
    manifest = _manifest(
        slides=_manifest().slides + (RenderedSlide("S-9", "render/s9.png", "img-c"),)
    )
    reasons = manifest_refusals(manifest, ARTIFACT)
    assert any("S-9" in r and "не проходил ни одной текстовой проверки" in r for r in reasons)


def test_two_pictures_for_one_section_are_refused():
    manifest = _manifest(
        slides=_manifest().slides + (RenderedSlide("S-1", "render/s1b.png", "img-d"),)
    )
    assert any("два изображения на один раздел" in r for r in manifest_refusals(manifest, ARTIFACT))


def test_a_section_id_written_non_canonically_is_refused():
    """The same one-spelling rule as everywhere: S-1 and S-01 must not be two addresses."""
    manifest = _manifest(
        slides=(
            RenderedSlide("S-01", "render/s1.png", "img-a"),
            RenderedSlide("S-2", "render/s2.png", "img-b"),
        )
    )
    assert any("не канонически" in r for r in manifest_refusals(manifest, ARTIFACT))


@pytest.mark.parametrize("blank", ["", "   "])
def test_an_undeclared_build_environment_is_a_refusal(blank):
    """The same source on another machine is another render — fonts substitute and lines
    re-wrap — so a finding without a build identity does not reproduce."""
    reasons = manifest_refusals(_manifest(build_env_id=blank), ARTIFACT)
    assert any("не объявлена среда сборки" in r for r in reasons)


def test_a_render_without_a_source_digest_is_a_refusal():
    assert any(
        "не привязан ни к какой версии текста" in r
        for r in manifest_refusals(_manifest(source_sha256="  "), ARTIFACT)
    )


def test_a_complete_manifest_passes():
    assert manifest_refusals(_manifest(), ARTIFACT) == []


# --- the pass is keyed to the bytes it looked at -------------------------------------------


def test_the_render_identity_moves_when_any_slide_does():
    before = _manifest()
    after = _manifest(
        slides=(
            RenderedSlide("S-1", "render/s1.png", "img-a"),
            RenderedSlide("S-2", "render/s2.png", "img-CHANGED"),
        )
    )
    assert before.render_sha256() != after.render_sha256()


def test_the_render_identity_does_not_depend_on_slide_order():
    """Two manifests listing the same pictures are the same render — the order in which the
    builder happened to emit them is not a property of what the reader sees."""
    shuffled = _manifest(slides=tuple(reversed(_manifest().slides)))
    assert shuffled.render_sha256() == _manifest().render_sha256()


def test_a_finding_does_not_outlive_the_picture_it_was_raised_against():
    """The fix IS a new render, so a carried finding would report a defect against bytes
    nobody has looked at."""
    old = VisualFinding(
        VisualCategory.STYLE_BREAK, "S-1", "другой кегль", "старый-хэш-рендера"
    )
    fresh = VisualFinding(
        VisualCategory.STYLE_BREAK, "S-2", "тот же кегль", _manifest().render_sha256()
    )
    stale = stale_findings([old, fresh], _manifest())
    assert stale == (old,)


def test_the_two_floor_categories_are_not_matters_of_taste():
    assert VisualCategory.OWNERLESS_CLAIM in FLOOR_CATEGORIES
    assert VisualCategory.CEILING_BREACH in FLOOR_CATEGORIES
    assert VisualCategory.STYLE_BREAK not in FLOOR_CATEGORIES
    assert VisualFinding(
        VisualCategory.CEILING_BREACH, "S-1", "живые цифры скриншотом", "r"
    ).is_floor


# --- a visual fix versus a new version of the source ---------------------------------------


def test_a_layout_only_fix_does_not_cost_a_new_blind_reading():
    verdict = classify_change(_manifest(), _manifest(slides=(
        RenderedSlide("S-1", "render/s1.png", "img-NEW"),
        RenderedSlide("S-2", "render/s2.png", "img-b"),
    )))
    assert verdict.render_moved and not verdict.source_moved
    assert verdict.blind_reading_must_be_repeated is False
    assert any("правка визуального слоя" in r for r in verdict.reasons)


def test_a_source_edit_is_derived_and_costs_a_new_blind_reading():
    """Derived from the digests, not asked for: the side reporting "only the layout changed"
    is the side that would rather not pay for another blind pass."""
    verdict = classify_change(_manifest(), _manifest(source_version=4, source_sha256="src-b"))
    assert verdict.source_moved
    assert verdict.blind_reading_must_be_repeated is True
    assert any("новая версия текста" in r for r in verdict.reasons)


def test_a_moved_source_that_kept_its_version_number_is_called_out():
    """The number is a label set by the same side that edits the text; the digest is what
    notices an edit that forgot to bump it."""
    verdict = classify_change(_manifest(), _manifest(source_sha256="src-b"))
    assert any("версия исходника не выросла" in r for r in verdict.reasons)


def test_nothing_moved_means_no_pass_is_needed():
    verdict = classify_change(_manifest(), _manifest())
    assert not verdict.source_moved and not verdict.render_moved
    assert any("новый проход не нужен" in r for r in verdict.reasons)


# --- when the pass runs, and what it records ------------------------------------------------


def _record(**over):
    return visual_record(
        _manifest(),
        run_id="vis-1",
        iteration=2,
        artifact_seq=40,
        profile_digest="P",
        started_at=T0,
        finished_at=T0 + timedelta(minutes=3),
        transcript_path="runs/visual-1.log",
        transcript_sha256="h",
        model="gpt-x",
        model_version="2026-08",
        runner_version="0.145.0",
        spend=900,
        **over,
    )


def test_the_pass_is_due_on_bytes_nobody_has_looked_at_yet():
    assert pass_is_due(None, _manifest()) is True
    assert pass_is_due(_record(), _manifest()) is False
    assert pass_is_due(_record(), _manifest(slides=(
        RenderedSlide("S-1", "render/s1.png", "img-NEW"),
        RenderedSlide("S-2", "render/s2.png", "img-b"),
    ))) is True


def test_the_visual_record_is_complete_without_the_isolation_fields():
    """It is a SEEING pass by construction — it holds the claim map and the contract on
    purpose. Demanding the provider's isolation attestation of it would borrow the authority
    of a mechanism it does not use, and push whoever files it into inventing two values."""
    record = _record()
    assert record.kind is RunKind.VISUAL
    assert record.missing_fields() == []
    assert record.sandbox_mode is None and record.approval_mode is None
    assert record.launch_number is None


def test_the_visual_record_still_demands_its_own_four_fields():
    record = _record()
    assert record.source_sha256 == "src-a"
    assert record.render_sha256 == _manifest().render_sha256()
    assert record.build_env_id.startswith("build_pptx")
    assert record.source_version == 3


def test_the_isolating_pair_still_demands_the_isolation_fields():
    """The relaxation is for the visual kind alone — the binding it does not touch keeps
    every field it rests on."""
    from tests.test_audience_run_attestation import _blind  # the existing fixture

    incomplete = _blind(sandbox_mode=None, approval_mode=None, launch_number=None)
    missing = incomplete.missing_fields()
    assert {"sandbox_mode", "approval_mode", "launch_number"} <= set(missing)


def test_an_annulled_visual_run_still_needs_its_reason():
    record = _record(outcome=RunOutcome.ANNULLED)
    assert "outcome_reason" in record.missing_fields()
