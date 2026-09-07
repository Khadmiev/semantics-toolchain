# SPDX-License-Identifier: Apache-2.0
"""Review genres — the axis that is NOT the mode.

Two independent questions were being answered by one field, and that is the shape of defect
this project has already recorded once: when one lever drives two independent things, one of
them ends up with a value nobody chose.

- ``mode`` (``spec`` / ``code``) says what SHAPE the artifact arrived in — a diff, or a text
  bundle. The server stores and validates it, behind a database constraint, and it is meant
  to stay exactly that dumb.
- ``genre`` says what KIND of review this is: what pass plan applies and what the coverage
  denominator is made of. It is declared in the review's free-form config, which the server
  does not interpret.

An audience review therefore has ``mode="spec"`` — its artifact really is carried as a text
bundle — and ``genre="audience"``. Neither statement is a workaround for the other.

This module holds the names only, so that both sides can agree on them without the genre
package pulling in the coverage builder or the coverage builder pulling in the genre.
"""

#: Audience review: a human-readable artifact read by a blind reader and an informed one.
AUDIENCE_GENRE = "audience"

#: The review-config key that must SAY the cold-verdict-first pass is not assigned in this
#: genre. Silence is not acceptable: a missing two-phase split is indistinguishable from a
#: forgotten requirement, and half a year later it reads as a defect rather than a decision.
COLD_VERDICT_FIRST_KEY = "cold_verdict_first"

#: The two optional reading roles of the audience circle, declared by the SAME device as
#: the cold pass: an explicit boolean in the review config, never a default read out of
#: silence. When a role is off, its convergence condition is absent — and that absence has
#: to be a stated decision, or half a year later it reads as a forgotten requirement.
#: The strategic reader is the operator's gate decision (recommended on when a desired
#: takeaway is declared); the machine comb is recommended ON for every audience review —
#: but both are still written down here explicitly.
STRATEGIC_READER_KEY = "strategic_reader"
MACHINE_COMB_KEY = "machine_comb"

KNOWN_GENRES = (AUDIENCE_GENRE,)


def genre_of(config: dict | None) -> str | None:
    return (config or {}).get("genre")


def config_refusals(config: dict | None) -> list[str]:
    """Why a pass cannot be planned for this review at all (empty = it can).

    Refusals rather than defaults, in both directions. An unknown genre is not silently
    treated as "no genre": that would run a review under a pass plan its author did not
    choose. And in the audience genre the absence of the cold-pass declaration is a refusal,
    not an assumption — the point of declaring it is that a reader half a year from now can
    tell a decision from an omission.
    """
    genre = genre_of(config)
    if genre is None:
        return []
    if genre not in KNOWN_GENRES:
        return [f"unknown review genre {genre!r} (known: {', '.join(KNOWN_GENRES)})"]
    if genre != AUDIENCE_GENRE:
        return []
    declared = (config or {}).get(COLD_VERDICT_FIRST_KEY)
    if declared is None:
        return [
            f"the review config does not declare `{COLD_VERDICT_FIRST_KEY}`: in the audience genre "
            "the cold pass is NOT assigned, and that has to be stated rather than omitted"
        ]
    if declared is not False:
        return [
            f"`{COLD_VERDICT_FIRST_KEY}` is {declared!r}: in the audience genre the cold "
            "pass is not assigned — the blind pass plays its role"
        ]
    reasons = []
    for key in (STRATEGIC_READER_KEY, MACHINE_COMB_KEY):
        value = (config or {}).get(key)
        if not isinstance(value, bool):
            reasons.append(
                f"the review config does not declare `{key}` as a strict boolean "
                f"(carries {value!r}): when a role is off its convergence condition is "
                "absent, and that absence must be a stated decision, not silence"
            )
    return reasons


# --- B.11: two more settings resolved once and frozen at creation ------------------

#: The review-config key holding coverage settings, and the flag inside it (B.11 A-1).
#: "Is a coverage denominator in play for this review" was previously INFERRED, in three
#: separate places, from whether the channel already held a manifest — a premise that is
#: false for the first version of every review, because a manifest references an artifact
#: (`artifact_seq`) and therefore can never precede one.
COVERAGE_KEY = "coverage"
COVERAGE_IN_PLAY_KEY = "in_play"

#: The review-config key declaring the semantic-map role (B.11 E-2), on the SAME device as
#: the audience genre's optional reading roles above: an explicit boolean in the config,
#: resolved once and frozen, never re-read out of silence later.
SEMANTIC_MAP_KEY = "semantic_map"


def coverage_in_play(config: dict | None, *, any_manifest: bool) -> bool:
    """Is a coverage denominator in play for this review? (B.11 A-1)

    ONE predicate, asked by the server where it refuses a critic's evidence and by the
    watcher where it schedules a pass. It used to exist three times over, and each copy
    answered "not in play" whenever the channel held no manifest AT ALL — which is the
    state every review is in before its first manifest lands. Two measured outcomes, both
    bad: a phantom `coverage-manifest-missing` finding (three times across two reviews),
    or, worse, passes running with no denominator in complete silence (twice in the review
    of this very spec) with nothing on the channel recording that they were unmeasured.

    ``any_manifest`` is the ROLLOUT hatch and nothing more. A review created by this code
    carries the resolved flag in its frozen config, so the answer comes from the config
    and the argument is not consulted. A review created BEFORE this slice carries no such
    key, and for it the old inference is kept deliberately: an in-flight review that never
    had coverage in play must not start being refused mid-channel by a rule its author
    never agreed to. Same shape as the protocol and artifact-contract stamps.
    """
    declared = ((config or {}).get(COVERAGE_KEY) or {}).get(COVERAGE_IN_PLAY_KEY)
    if declared is None:
        return any_manifest
    return bool(declared)


def semantic_map_owed(config: dict | None) -> bool:
    """Does this review owe a semantic map at the end of its cycle? (B.11 E-2, E-10)

    Reads the frozen declaration only. A review created before this slice declares
    nothing and owes nothing — the map is a capability its cycle never promised.
    """
    return (config or {}).get(SEMANTIC_MAP_KEY) is True


def freeze_role_settings(config: dict | None, mode: str) -> dict:
    """Resolve the two B.11 settings for freezing at creation, or raise ``ValueError``.

    Defaults exist and are applied HERE, once, so that what lands in the stored config is
    always an explicit value: coverage is in play for both modes; the map is owed by
    `code` reviews and not by `spec` ones (G-1 — a map of a spec would be a model reading
    a text against a model's summary of the same text). An explicit declaration overrides
    either, and must be a strict boolean: `False` and "the key is missing" are the same
    string in a config file and very different facts, so the difference between them is
    resolved at creation rather than argued about later.
    """
    resolved: dict = {}
    declared_coverage = (config or {}).get(COVERAGE_KEY)
    if declared_coverage is None:
        in_play: bool = True
    elif not isinstance(declared_coverage, dict):
        raise ValueError(
            f"review config `{COVERAGE_KEY}` must be an object carrying "
            f"`{COVERAGE_IN_PLAY_KEY}`, got {declared_coverage!r}"
        )
    else:
        value = declared_coverage.get(COVERAGE_IN_PLAY_KEY, True)
        if not isinstance(value, bool):
            raise ValueError(
                f"review config `{COVERAGE_KEY}.{COVERAGE_IN_PLAY_KEY}` must be a strict "
                f"boolean, got {value!r} — whether a review has a coverage denominator at "
                "all is not a thing to leave to how a truthy value reads"
            )
        in_play = value
    resolved[COVERAGE_KEY] = {**(declared_coverage or {}), COVERAGE_IN_PLAY_KEY: in_play}

    declared_map = (config or {}).get(SEMANTIC_MAP_KEY)
    if declared_map is None:
        resolved[SEMANTIC_MAP_KEY] = mode == "code"
    elif not isinstance(declared_map, bool):
        raise ValueError(
            f"review config `{SEMANTIC_MAP_KEY}` must be a strict boolean, got "
            f"{declared_map!r} — when the role is off, its absence has to be a stated "
            "decision rather than a silence"
        )
    else:
        resolved[SEMANTIC_MAP_KEY] = declared_map
    return resolved
