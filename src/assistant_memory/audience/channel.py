# SPDX-License-Identifier: Apache-2.0
"""The genre's phases on the review channel — closed schemas, both directions.

WHY THIS EXISTS AT ALL. Every mechanism of this genre produced values that never left the
process: run records, the canary's marker list, the mechanical report, the blind findings.
The code made them and nothing sent them, so an audience review could not actually be run —
the gap sat in the working notes as "a thin adapter, later" through four stages.

WHAT RIDES WHERE. Nothing new is invented on the wire: the genre's phases travel as ordinary
``notice`` messages carrying a ``phase``, exactly like the watcher's own phases do. A new
message kind would mean a server change and a migration for something the server does not
need to understand — it stores and orders; the meaning is ours.

THE SCHEMAS ARE CLOSED IN BOTH DIRECTIONS, and that is the whole point of the module. An
unknown field is a refusal, not a shrug, because silence must mean refusal rather than
consent — the rule this genre has been bitten by more than any other. Closed on the way OUT
catches a field added after review; closed on the way IN catches a message written by hand,
by an older version, or by something that merely looks like this genre.

WHAT IS DELIBERATELY NOT HERE: transport. This module builds and validates payloads and
takes the sending as a callable. Keeping the I/O out is what lets the ordering be tested
without a live review — and the ordering is where this genre's mistakes live.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from assistant_memory.audience.claim_map import CLAIM_MAP_PHASE as _CLAIM_MAP_PHASE
from assistant_memory.audience.claim_map import to_payload as claim_map_message  # noqa: F401
from assistant_memory.audience.records import RunKind, RunOutcome, RunRecord

#: The marker list, published BEFORE the canary launches. Its own phase because the ORDER is
#: the evidence: a criterion chosen after the answer is known proves nothing about the answer.
CANARY_MARKERS_PHASE = "canary_markers"
#: The canary's ANSWER, published as its own phase (the spec's `canary`). The verdict that
#: decides anything lives inside the run record — but the record carries only the answer's
#: HASH, and a hash of an unpublished text binds nothing: the record's word about its own
#: answer would be the only witness. Publishing the answer itself makes the verdict
#: re-checkable by a side with no files: hash the body, compare, re-scan for markers.
CANARY_PHASE = "canary"
#: One model run, as recorded at the moment it happened.
RUN_RECORD_PHASE = "run_record"
#: The mechanical layer's measurement. Informational — it blocks nothing, at any threshold.
MECH_REPORT_PHASE = "mech_report"
#: The claim map as published: the ACTING status of each row, never the recorded one. The
#: phase name and the ROW schema are owned by the claim-map module and imported rather than
#: restated — this module briefly held a second definition of both, which is the two-copies
#: shape whose whole problem is that the copies agree until the day they do not.
CLAIM_MAP_PHASE = _CLAIM_MAP_PHASE
#: What the blind reader found on one version of the artefact.
BLIND_FINDINGS_PHASE = "blind_findings"
#: What the strategic reader found — convergence-significant when the role is on.
STRATEGIC_FINDINGS_PHASE = "strategic_findings"
#: What the machine comb found — convergence-significant when the role is on.
MACHINE_FINDINGS_PHASE = "machine_findings"
#: The two declarations only the operator can make: did the retelling match the intended
#: takeaway, and did the goal move this round.
INTENT_TAKEAWAY_PHASE = "intent_takeaway"

AUDIENCE_PHASES: tuple[str, ...] = (
    CANARY_MARKERS_PHASE,
    CANARY_PHASE,
    RUN_RECORD_PHASE,
    MECH_REPORT_PHASE,
    CLAIM_MAP_PHASE,
    BLIND_FINDINGS_PHASE,
    STRATEGIC_FINDINGS_PHASE,
    MACHINE_FINDINGS_PHASE,
    INTENT_TAKEAWAY_PHASE,
)

#: B.8 D-4: the annulment of a BROKEN genre message — deliberately NOT an audience phase.
#: It is channel bookkeeping, not genre data: one hand-posted message under a reserved
#: phase once made the whole channel unreplayable (incident 2c18e50e), and recovery meant
#: editing data by hand. Append-only is honest only if annulment lives in the channel
#: where the audit can see it. RETRACTABILITY HAS A MACHINE PREDICATE: a message is
#: retractable exactly when it FAILS the channel's own genre-record validation (the same
#: validation whose failure invalidates the replay); retracting a VALID message, or a
#: target that does not exist, is itself a schema error. Only the role that authored the
#: target may retract it.
RETRACTION_PHASE = "phase_retraction"


class ChannelError(ValueError):
    """A payload that will not be sent or will not be trusted, with EVERY reason."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


#: Fields every genre phase carries. ``artifact_seq`` is the address of the version this is
#: about: the round number moves for reasons of its own (a round that ends at the operator
#: does not move it), so the version is what identifies "which text is this about".
_COMMON: tuple[str, ...] = ("phase", "artifact_seq")

#: Per phase: what must be there, and what may be. Closed: anything else is refused.
_REQUIRED: dict[str, tuple[str, ...]] = {
    CANARY_MARKERS_PHASE: ("markers",),
    CANARY_PHASE: ("answer",),
    RUN_RECORD_PHASE: ("record",),
    MECH_REPORT_PHASE: ("thresholds_digest", "parts", "blocks_convergence"),
    CLAIM_MAP_PHASE: ("rows",),
    # `iteration` is REQUIRED here, not optional: the convergence rule speaks in rounds, a
    # carry is exactly the case where version and round part ways, and a phase that does not
    # say which round it stands for leaves the carry unbound to the cycle it claims to serve.
    # `contract_public_items` is the declared universe of anchors: the channel gate holds no
    # contract, so the phase carries the checkable commitment — every finding's anchor must
    # resolve into it, and the phase-integrity rule pins it per record like the findings.
    BLIND_FINDINGS_PHASE: (
        "findings", "run_record_id", "reader_digest", "iteration", "contract_public_items",
    ),
    # The v2 role phases (the same construction as the blind one): `role_digest` is the
    # fingerprint the assessment stands for — fresh must match its record, a carry must
    # match the record it carries from. The strategic phase additionally carries the
    # declared item universe of BOTH contract parts, so the channel gate can check anchor
    # resolvability without holding the contract.
    STRATEGIC_FINDINGS_PHASE: (
        "findings", "run_record_id", "role_digest", "iteration", "contract_items",
    ),
    MACHINE_FINDINGS_PHASE: ("findings", "run_record_id", "role_digest", "iteration"),
    # The operator's declarations carry the ROUND they are about, both compared texts (the
    # intended takeaway VERBATIM — the declaration is about a comparison, and a comparison
    # whose sides are off the record cannot be checked later), and the date the operator
    # declared. The goal-motion signal is relative by nature, so its anchor to the PREVIOUS
    # declaration rides in the optional pair below — optional only because a first round has
    # no predecessor; where one exists, the gate demands the pair and checks the hash.
    INTENT_TAKEAWAY_PHASE: (
        "retelling_matches", "goal_moved", "reader_retelling",
        "intended_takeaway", "iteration", "declared_at",
    ),
}
_OPTIONAL: dict[str, tuple[str, ...]] = {
    CANARY_MARKERS_PHASE: ("note",),
    CANARY_PHASE: (),
    RUN_RECORD_PHASE: (),
    # `retelling_diff` (AG-10): the word diff of the synthesis sections between the current
    # version's credited report and the previous version's — computable only once the
    # current run is credited, so it rides on a LATER re-post of this phase. Informational
    # like the whole layer: it marks, the operator judges.
    MECH_REPORT_PHASE: ("candidates", "retelling_diff"),
    # `iteration` rides on the claim map because its owner puts it there: the map is a
    # statement about a round as well as a version, and the round is what the convergence
    # rule speaks in.
    CLAIM_MAP_PHASE: ("iteration",),
    BLIND_FINDINGS_PHASE: ("carried_from_artifact_seq", "carried_from_iteration"),
    STRATEGIC_FINDINGS_PHASE: ("carried_from_artifact_seq", "carried_from_iteration"),
    MACHINE_FINDINGS_PHASE: ("carried_from_artifact_seq", "carried_from_iteration"),
    INTENT_TAKEAWAY_PHASE: (
        "operator_note", "previous_intent_artifact_seq", "previous_takeaway_sha256",
    ),
}


#: Per phase: fields whose SEMANTIC emptiness is a refusal — a blank string, an empty list.
#: Checked in `_schema_reasons` so the rule holds on BOTH paths, build and read: the
#: builders refused these from the start, but a builder's refusal binds nobody who writes
#: the wire by hand or through a drifted version — and "present but empty" is an unfilled
#: field in the shape of a filled one, the exact form the records schema already refuses.
_NON_EMPTY: dict[str, tuple[str, ...]] = {
    CANARY_MARKERS_PHASE: ("markers",),
    CANARY_PHASE: ("answer",),
    MECH_REPORT_PHASE: ("thresholds_digest",),
    BLIND_FINDINGS_PHASE: ("run_record_id", "reader_digest", "contract_public_items"),
    STRATEGIC_FINDINGS_PHASE: ("run_record_id", "role_digest", "contract_items"),
    MACHINE_FINDINGS_PHASE: ("run_record_id", "role_digest"),
    INTENT_TAKEAWAY_PHASE: ("reader_retelling", "intended_takeaway", "declared_at"),
}


def _semantically_empty(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple)):
        return not [v for v in value if not _semantically_empty(v)]
    return False


#: Per phase: fields that must be STRICT booleans. Truthiness is not a type: a "yes" or an
#: empty string arriving through wire drift would be read as a decision the operator never
#: made — and two of these fields ARE the operator's convergence decisions.
_BOOLEAN: dict[str, tuple[str, ...]] = {
    MECH_REPORT_PHASE: ("blocks_convergence",),
    INTENT_TAKEAWAY_PHASE: ("retelling_matches", "goal_moved"),
}


def _schema_reasons(payload: Mapping[str, Any]) -> list[str]:
    phase = payload.get("phase")
    if phase not in _REQUIRED:
        return [
            f"фаза {phase!r} не принадлежит аудиторному жанру — известны: "
            f"{', '.join(AUDIENCE_PHASES)}"
        ]
    allowed = set(_COMMON) | set(_REQUIRED[phase]) | set(_OPTIONAL[phase])
    reasons = [
        f"{phase}: обязательное поле «{name}» отсутствует"
        for name in (*_COMMON, *_REQUIRED[phase])
        if payload.get(name) is None
    ]
    # UNKNOWN FIELDS ARE REFUSED, not ignored. A field nobody expected is either a version
    # skew or a message from something that only looks like this genre, and reading around
    # it would mean acting on a payload we do not understand.
    reasons += [
        f"{phase}: поле «{name}» вне закрытого перечня "
        f"({', '.join(sorted(allowed))})"
        for name in sorted(set(payload) - allowed)
    ]
    reasons += [
        f"{phase}: поле «{name}» содержательно пусто — заполненная форма пустого поля"
        for name in _NON_EMPTY.get(str(phase), ())
        if payload.get(name) is not None and _semantically_empty(payload.get(name))
    ]
    reasons += [
        f"{phase}: поле «{name}» обязано быть строгим булевым, а несёт "
        f"{payload.get(name)!r} — истинность по совпадению типа, не по правдоподобию"
        for name in _BOOLEAN.get(str(phase), ())
        if payload.get(name) is not None and not isinstance(payload.get(name), bool)
    ]
    return reasons


def validate(payload: Mapping[str, Any]) -> list[str]:
    """Why this payload is not a well-formed genre phase (empty list = it is)."""
    return _schema_reasons(payload)


def _checked(payload: dict) -> dict:
    reasons = _schema_reasons(payload)
    if reasons:
        raise ChannelError(reasons)
    return payload


def _record_payload(record: RunRecord) -> dict:
    """A run record on the wire — every field, hashes included, raw values never.

    Serialised field by field rather than by dumping the dataclass: a mass serialisation is
    exactly how a private structure reaches a channel it was never meant to reach, and this
    genre has a whole layer devoted to that failure.
    """
    return {
        "id": record.id,
        "kind": record.kind.value,
        "iteration": record.iteration,
        "artifact_seq": record.artifact_seq,
        "profile_digest": record.profile_digest,
        "launch_number": record.launch_number,
        "started_at": record.started_at.isoformat(),
        "finished_at": record.finished_at.isoformat(),
        "transcript_path": record.transcript_path,
        "transcript_sha256": record.transcript_sha256,
        "tool_calls": list(record.tool_calls),
        "model": record.model,
        "model_version": record.model_version,
        "spend": record.spend,
        "outcome": record.outcome.value,
        "runner_version": record.runner_version,
        "sandbox_mode": record.sandbox_mode,
        "approval_mode": record.approval_mode,
        "outcome_reason": record.outcome_reason,
        "supersedes": record.supersedes,
        "mech_thresholds_digest": record.mech_thresholds_digest,
        "canary_verdict_clean": record.canary_verdict_clean,
        "markers_message_seq": record.markers_message_seq,
        "answer_message_seq": record.answer_message_seq,
        "answer_sha256": record.answer_sha256,
        "canary_record_id": record.canary_record_id,
        "prompt_digest": record.prompt_digest,
        "reader_digest": record.reader_digest,
        "contract_version": record.contract_version,
        "contract_sha256": record.contract_sha256,
        "category_set_version": record.category_set_version,
        "report_schema_version": record.report_schema_version,
        "section_budgets": record.section_budgets,
        "render_path": record.render_path,
        "render_sha256": record.render_sha256,
        "annulled_record_id": record.annulled_record_id,
        "role_digest": record.role_digest,
        "source_version": record.source_version,
        "source_sha256": record.source_sha256,
        "build_env_id": record.build_env_id,
        # B.8 F-3: the degraded-launch triple, written by the tract
        "grounds_mode": record.grounds_mode,
        "failed_ground": record.failed_ground,
        "unchecked_class": record.unchecked_class,
    }


def canary_markers_message(artifact_seq: int, markers: Sequence[str], *, note: str = "") -> dict:
    """The marker list. Published BEFORE the canary runs — that order is the whole evidence.

    A non-empty list is required: an empty criterion would make every canary clean, and the
    verdict would keep its shape while meaning nothing.
    """
    cleaned = [marker.strip() for marker in markers if marker.strip()]
    if not cleaned:
        raise ChannelError(
            [
                "список маркеров пуст — критерий, которому удовлетворяет любой ответ, "
                "делает вердикт канарейки бессмысленным, не меняя его вида"
            ]
        )
    payload = {
        "phase": CANARY_MARKERS_PHASE,
        "artifact_seq": artifact_seq,
        "markers": sorted(set(cleaned)),
    }
    if note:
        payload["note"] = note
    return _checked(payload)


def canary_answer_message(artifact_seq: int, answer: str) -> dict:
    """The canary's answer, verbatim. Published BEFORE the record that hashes it.

    A blank answer is refused: it would be an unfilled field in the shape of a filled one,
    and a verdict over nothing keeps its form while meaning nothing.
    """
    if not answer.strip():
        raise ChannelError(
            [
                "ответ канарейки пуст — вердикт над пустотой сохраняет форму, "
                "ничего не значя"
            ]
        )
    return _checked(
        {"phase": CANARY_PHASE, "artifact_seq": artifact_seq, "answer": answer}
    )


def run_record_message(record: RunRecord) -> dict:
    """One run, as recorded. Refused outright while the record is incomplete by its schema."""
    missing = record.missing_fields()
    if missing:
        raise ChannelError(
            [f"запись прогона неполна, нет полей: {', '.join(missing)}"]
        )
    return _checked(
        {
            "phase": RUN_RECORD_PHASE,
            "artifact_seq": record.artifact_seq,
            "record": _record_payload(record),
        }
    )


def mech_report_message(
    artifact_seq: int, report, *, retelling_diff: Mapping[str, Any] | None = None
) -> dict:
    """The mechanical layer's numbers. ``blocks_convergence`` travels as a stated false.

    Stated rather than left to be inferred: the layer's standing rests on being unable to
    block, and a reader of this message should not have to know the spec to be sure of it.
    ``retelling_diff`` is the layer's post-crediting signal (AG-10) and rides on a later
    re-post of the phase — the diff needs the current version's CREDITED report, which does
    not exist when the layer first runs.
    """
    body = report.as_message()
    payload = {
        "phase": MECH_REPORT_PHASE,
        "artifact_seq": artifact_seq,
        "thresholds_digest": body["thresholds_digest"],
        "parts": body["parts"],
        "blocks_convergence": False,
        "candidates": list(report.candidates),
    }
    if retelling_diff is not None:
        payload["retelling_diff"] = dict(retelling_diff)
    return _checked(payload)


#: The claim map's payload builder is the claim map's own (`claim_map.to_payload`, re-exported
#: above). It is not wrapped here: the acting status is RECOMPUTED from each row's current
#: fields by the module that owns the automaton, and a wrapper would be a second place where
#: someone could one day answer the same question differently.


def blind_findings_message(
    artifact_seq: int,
    findings: Sequence[Mapping[str, Any]],
    *,
    run_record_id: str,
    reader_digest: str,
    iteration: int,
    contract_public_items: Sequence[str],
    carried_from_artifact_seq: int | None = None,
    carried_from_iteration: int | None = None,
) -> dict:
    """What the blind reader found — bound to the run that produced it.

    ``run_record_id`` is required and has no default: findings whose run cannot be named are
    findings whose isolation cannot be checked, and they would still look like findings. A
    CARRIED assessment says so out loud and names the round it came from, because "read
    again and found the same" and "not read again" are different facts about this version.

    ``reader_digest`` is the fingerprint of what the reader SEES on this version, and it is
    required for the same reason on both paths: fresh, it must match the record's own
    fingerprint; carried, it must match the record carried FROM — an unmoved fingerprint is
    the definition of a lawful carry, and a phase that does not say which fingerprint it
    stands for leaves the convergence gate nothing to hold the carry against.
    """
    if not run_record_id.strip():
        raise ChannelError(
            [
                "находки не названы прогоном — без ссылки на запись нельзя проверить "
                "изоляцию чтения, которое их произвело"
            ]
        )
    if not reader_digest.strip():
        raise ChannelError(
            [
                "фаза слепой оценки без читательского отпечатка — гейту сходимости нечем "
                "проверить, что оценка относится к тому, что читатель видит сейчас"
            ]
        )
    # The ITEMS are typed and closed, not an arbitrary list: an item nobody can address is
    # an item nobody can dispose, and the convergence rule counts dispositions. The anchor
    # universe rides with the phase and every anchor must resolve into it.
    from assistant_memory.audience.blind_report import items_refusals

    public = [str(i) for i in contract_public_items]
    problems = items_refusals(list(findings))
    if not public:
        problems.append(
            "перечень публичных пунктов контракта пуст — якорям находок не во что "
            "разрешаться"
        )
    problems += [
        f"находка {item.get('id')} ссылается на пункт {item.get('contract_item')!r} вне "
        "объявленного перечня публичных пунктов"
        for item in findings
        if isinstance(item, Mapping) and str(item.get("contract_item")) not in public
    ]
    if problems:
        raise ChannelError(problems)
    payload: dict[str, Any] = {
        "phase": BLIND_FINDINGS_PHASE,
        "artifact_seq": artifact_seq,
        "findings": list(findings),
        "run_record_id": run_record_id,
        "reader_digest": reader_digest,
        "iteration": iteration,
        "contract_public_items": sorted(set(public)),
    }
    if carried_from_artifact_seq is not None:
        payload["carried_from_artifact_seq"] = carried_from_artifact_seq
    if carried_from_iteration is not None:
        payload["carried_from_iteration"] = carried_from_iteration
    return _checked(payload)


def _role_findings_payload(
    phase: str,
    artifact_seq: int,
    findings: Sequence[Mapping[str, Any]],
    *,
    run_record_id: str,
    role_digest: str,
    iteration: int,
    item_problems: list[str],
    carried_from_artifact_seq: int | None,
    carried_from_iteration: int | None,
    extra: dict | None = None,
) -> dict:
    """The shared half of the two role-findings builders — one construction, two phases."""
    problems = list(item_problems)
    if not run_record_id.strip():
        problems.append(
            "находки не названы прогоном — без ссылки на запись их засчитываемость "
            "нечем проверить"
        )
    if not role_digest.strip():
        problems.append(
            "фаза без отпечатка роли — гейту нечем проверить, что оценка относится к "
            "текущим входам роли"
        )
    if problems:
        raise ChannelError(problems)
    payload: dict[str, Any] = {
        "phase": phase,
        "artifact_seq": artifact_seq,
        "findings": list(findings),
        "run_record_id": run_record_id,
        "role_digest": role_digest,
        "iteration": iteration,
        **(extra or {}),
    }
    if carried_from_artifact_seq is not None:
        payload["carried_from_artifact_seq"] = carried_from_artifact_seq
    if carried_from_iteration is not None:
        payload["carried_from_iteration"] = carried_from_iteration
    return _checked(payload)


def strategic_findings_message(
    artifact_seq: int,
    findings: Sequence[Mapping[str, Any]],
    *,
    run_record_id: str,
    role_digest: str,
    iteration: int,
    contract_items: Sequence[str],
    contract_sha256: str,
    carried_from_artifact_seq: int | None = None,
    carried_from_iteration: int | None = None,
) -> dict:
    """What the strategic reader found — bound to its run and to the declared anchor
    universe of BOTH contract parts (the gate holds no contract, so the phase carries the
    checkable commitment, exactly as the blind phase carries the public items)."""
    from assistant_memory.audience.strategic import phase_items_refusals

    universe = sorted({str(i) for i in contract_items})
    problems = phase_items_refusals(list(findings), contract_sha256=contract_sha256)
    if not universe:
        problems.append(
            "перечень объявленных пунктов контракта пуст — якорям находок не во что "
            "разрешаться"
        )
    problems += [
        f"стратегическая находка {item.get('id')} ссылается на пункт "
        f"{item.get('пункт_контракта')!r} вне объявленного перечня"
        for item in findings
        if isinstance(item, Mapping)
        and str(item.get("пункт_контракта")) not in set(universe)
    ]
    return _role_findings_payload(
        STRATEGIC_FINDINGS_PHASE, artifact_seq, findings,
        run_record_id=run_record_id, role_digest=role_digest, iteration=iteration,
        item_problems=problems,
        carried_from_artifact_seq=carried_from_artifact_seq,
        carried_from_iteration=carried_from_iteration,
        extra={"contract_items": universe},
    )


def machine_findings_message(
    artifact_seq: int,
    findings: Sequence[Mapping[str, Any]],
    *,
    run_record_id: str,
    role_digest: str,
    iteration: int,
    table_version: str,
    carried_from_artifact_seq: int | None = None,
    carried_from_iteration: int | None = None,
) -> dict:
    """What the machine comb found — bound to its run; no contract anchor by construction
    (the role never sees the contract)."""
    from assistant_memory.audience.machine_comb import phase_items_refusals

    return _role_findings_payload(
        MACHINE_FINDINGS_PHASE, artifact_seq, findings,
        run_record_id=run_record_id, role_digest=role_digest, iteration=iteration,
        item_problems=phase_items_refusals(list(findings), table_version=table_version),
        carried_from_artifact_seq=carried_from_artifact_seq,
        carried_from_iteration=carried_from_iteration,
    )


def intent_takeaway_message(
    artifact_seq: int,
    *,
    iteration: int,
    retelling_matches: bool,
    goal_moved: bool,
    reader_retelling: str,
    intended_takeaway: str,
    declared_at: str,
    operator_note: str = "",
    previous_intent_artifact_seq: int | None = None,
    previous_takeaway_sha256: str | None = None,
) -> dict:
    """The operator's two declarations, bound to the round and to the texts they compare.

    Both texts ride along — the intended takeaway VERBATIM — because the declaration is
    about a comparison, and a comparison whose sides are not on the record cannot be checked
    by the human backstop later. The goal-motion signal is RELATIVE, so it anchors to the
    previous declaration by that phase's version and the hash of its verbatim quote; a first
    round has no predecessor and may omit the pair — where a predecessor exists, the gate
    demands it. Neither declaration has a machine basis and neither is ever defaulted: this
    module can only carry what the operator said.
    """
    reasons = []
    if not intended_takeaway.strip():
        reasons.append(
            "желаемый вынос пуст — объявление о совпадении не с чем будет сверить"
        )
    if not declared_at.strip():
        reasons.append("не названа дата объявления оператора")
    # STRICT TYPE AT THE DOOR, never a coercion: bool("yes") is True, so a cast here would
    # turn an untyped caller's garbage into exactly the convergence-enabling decision the
    # read path refuses — the builder must hold the same line it expects the wire to hold.
    for name, value in (("retelling_matches", retelling_matches), ("goal_moved", goal_moved)):
        if not isinstance(value, bool):
            reasons.append(
                f"объявление «{name}» обязано быть строгим булевым, а несёт {value!r} — "
                "приведение типом сборщика превратило бы мусор в решение"
            )
    if reasons:
        raise ChannelError(reasons)
    payload: dict[str, Any] = {
        "phase": INTENT_TAKEAWAY_PHASE,
        "artifact_seq": artifact_seq,
        "iteration": iteration,
        "retelling_matches": retelling_matches,
        "goal_moved": goal_moved,
        "reader_retelling": reader_retelling,
        "intended_takeaway": intended_takeaway,
        "declared_at": declared_at,
    }
    if operator_note:
        payload["operator_note"] = operator_note
    if previous_intent_artifact_seq is not None:
        payload["previous_intent_artifact_seq"] = previous_intent_artifact_seq
    if previous_takeaway_sha256 is not None:
        payload["previous_takeaway_sha256"] = previous_takeaway_sha256
    return _checked(payload)


# --- reading the channel back --------------------------------------------------------------


@dataclass(frozen=True)
class ChannelPhase:
    """One genre message as read back: its sequence number, its phase, its payload."""

    seq: int
    phase: str
    payload: Mapping[str, Any]
    role: str


def _genre_problems(payload: Mapping[str, Any]) -> list[str]:
    """Every way one genre payload fails the channel's own validation (empty = it holds).

    THE NESTED HALVES ARE CHECKED TOO, not left for a consumer to trip over: a
    top-level-valid phase whose rows or record are garbage is still a payload nobody
    understands, and a gate that CRASHES on it has turned a refusal into an outage.
    The claim map's own deep check is the claim-map module's (it owns the row schema);
    a run record is checked by rebuilding it, which is exactly what every reader does.
    This function is ALSO D-4's machine predicate of retractability: a genre message is
    retractable exactly when this returns non-empty.
    """
    problems = _schema_reasons(payload)
    if not problems and payload.get("phase") == CLAIM_MAP_PHASE:
        from assistant_memory.audience.claim_map import check_payload

        problems = check_payload(payload)
    if not problems and payload.get("phase") == RUN_RECORD_PHASE:
        try:
            record_from_payload(payload["record"])
        except Exception as broken:  # noqa: BLE001 — any parse failure is the refusal
            problems = [f"запись прогона не разбирается: {broken!r}"]
    return problems


def _retraction_problems(
    payload: Mapping[str, Any], seq, by_seq: Mapping[int, Mapping[str, Any]], role: str
) -> list[str]:
    """Why one ``phase_retraction`` notice is unfounded (empty = it annuls its target)."""
    problems: list[str] = []
    target_seq = payload.get("target_seq")
    if not (isinstance(payload.get("reason"), str) and payload["reason"].strip()):
        problems.append("ретракция обязана нести непустую причину «reason»")
    if not isinstance(target_seq, int) or target_seq not in by_seq:
        problems.append(
            f"ретракция называет target_seq {target_seq!r}, которого нет на канале — "
            "аннулировать нечего, сама ретракция и есть ошибка схемы"
        )
        return problems
    if isinstance(seq, int) and seq <= target_seq:
        problems.append(
            f"ретракция (сообщение {seq}) не может предшествовать своей цели "
            f"({target_seq}) — аннулирование это позднейшая запись"
        )
    target = by_seq[target_seq]
    if str(target.get("role", "")) != role:
        problems.append(
            f"ретракцию сообщения {target_seq} может подать только роль-автор цели "
            f"({target.get('role')!r}), а подала {role!r}"
        )
    target_payload = target.get("payload") or {}
    is_broken_genre = (
        target.get("kind") == "notice"
        and target_payload.get("phase") in AUDIENCE_PHASES
        and bool(_genre_problems(target_payload))
    )
    if not is_broken_genre:
        problems.append(
            f"цель ретракции (сообщение {target_seq}) проходит собственную валидацию "
            "жанровой записи — валидное сообщение не отменяется никем; отменяемо ровно "
            "то, что валидацию не проходит"
        )
    return problems


def read_phases(messages: Sequence[Mapping[str, Any]]) -> tuple[ChannelPhase, ...]:
    """Genre phases from a raw message list, in channel order. Malformed ones are REFUSED.

    Refused rather than skipped: a payload that fails its own schema is not noise to route
    around — it is either a version skew or something imitating this genre, and quietly
    ignoring it would let a review proceed on a channel nobody understands.

    B.8 D-4 — THE ONE ANNULMENT PATH: a ``notice`` of phase ``phase_retraction``
    (``{target_seq, reason}``) posted LATER by the target's own authoring role excludes a
    BROKEN genre message from the replay instead of letting it invalidate the whole
    channel forever (incident 2c18e50e: one hand-posted message under a reserved phase
    bricked every later read, and recovery meant editing data by hand). The retraction
    notice itself stays on the channel — the exclusion is explicitly on the record, where
    the audit can see it. An UNFOUNDED retraction — target missing, target VALID, wrong
    role, no reason — is itself a refusal: annulment must never become a way to erase
    valid history.
    """
    by_seq: dict[int, Mapping[str, Any]] = {
        int(m["seq"]): m for m in messages if isinstance(m.get("seq"), int)
    }
    retracted: set[int] = set()
    reasons: list[str] = []
    for message in messages:
        if message.get("kind") != "notice":
            continue
        payload = message.get("payload") or {}
        if payload.get("phase") != RETRACTION_PHASE:
            continue
        problems = _retraction_problems(
            payload, message.get("seq"), by_seq, str(message.get("role", ""))
        )
        if problems:
            reasons += [f"сообщение {message.get('seq')}: {p}" for p in problems]
        else:
            retracted.add(int(payload["target_seq"]))
    out: list[ChannelPhase] = []
    for message in messages:
        if message.get("kind") != "notice":
            continue
        payload = message.get("payload") or {}
        if payload.get("phase") not in AUDIENCE_PHASES:
            continue
        problems = _genre_problems(payload)
        if problems:
            if message.get("seq") in retracted:
                # D-4: annulled by its author's later retraction — excluded from the
                # replay; the retraction notice on the channel is the explicit record.
                continue
            reasons += [f"сообщение {message.get('seq')}: {problem}" for problem in problems]
            continue
        out.append(
            ChannelPhase(
                seq=int(message["seq"]),
                phase=str(payload["phase"]),
                payload=payload,
                role=str(message.get("role", "")),
            )
        )
    if reasons:
        raise ChannelError(reasons)
    return tuple(out)


def phase_retraction_message(
    messages: Sequence[Mapping[str, Any]], *, target_seq: int, reason: str, role: str
) -> dict:
    """Build one retraction body, validated against the replay BEFORE it is posted.

    The author-side half of D-4's two-sided validation: `read_phases` refuses an unfounded
    retraction wherever it came from; this builder refuses to CREATE one, so the ordinary
    path fails at the author with the reasons in hand. Returns the full message body
    (role + kind + payload) — the retraction is not a genre phase and must not go through
    ``post_phase``'s genre schema.
    """
    by_seq = {int(m["seq"]): m for m in messages if isinstance(m.get("seq"), int)}
    payload = {"phase": RETRACTION_PHASE, "target_seq": target_seq, "reason": reason}
    next_seq = max(by_seq, default=0) + 1
    problems = _retraction_problems(payload, next_seq, by_seq, role)
    if problems:
        raise ChannelError(problems)
    return {"role": role, "kind": "notice", "payload": payload}


def record_from_payload(payload: Mapping[str, Any]) -> RunRecord:
    """Rebuild a run record from what the channel carries — the state is READ, not remembered.

    Read back rather than kept in a local file because the channel is the authority: a
    driver that trusts its own memory of which runs happened is a driver whose evidence
    disappears when the process does, and this genre's whole point is that the evidence
    outlives the run.
    """
    from datetime import datetime as _dt

    return RunRecord(
        id=payload["id"],
        kind=RunKind(payload["kind"]),
        iteration=payload["iteration"],
        artifact_seq=payload["artifact_seq"],
        profile_digest=payload["profile_digest"],
        launch_number=payload.get("launch_number"),
        started_at=_dt.fromisoformat(payload["started_at"]),
        finished_at=_dt.fromisoformat(payload["finished_at"]),
        transcript_path=payload["transcript_path"],
        transcript_sha256=payload["transcript_sha256"],
        tool_calls=tuple(payload.get("tool_calls") or ()),
        model=payload["model"],
        model_version=payload["model_version"],
        spend=payload["spend"],
        outcome=RunOutcome(payload["outcome"]),
        runner_version=payload.get("runner_version"),
        sandbox_mode=payload.get("sandbox_mode"),
        approval_mode=payload.get("approval_mode"),
        outcome_reason=payload.get("outcome_reason"),
        supersedes=payload.get("supersedes"),
        mech_thresholds_digest=payload.get("mech_thresholds_digest"),
        grounds_mode=payload.get("grounds_mode"),
        failed_ground=payload.get("failed_ground"),
        unchecked_class=payload.get("unchecked_class"),
        canary_verdict_clean=payload.get("canary_verdict_clean"),
        markers_message_seq=payload.get("markers_message_seq"),
        answer_message_seq=payload.get("answer_message_seq"),
        answer_sha256=payload.get("answer_sha256"),
        canary_record_id=payload.get("canary_record_id"),
        prompt_digest=payload.get("prompt_digest"),
        reader_digest=payload.get("reader_digest"),
        contract_version=payload.get("contract_version"),
        contract_sha256=payload.get("contract_sha256"),
        category_set_version=payload.get("category_set_version"),
        report_schema_version=payload.get("report_schema_version"),
        section_budgets=payload.get("section_budgets"),
        render_path=payload.get("render_path"),
        render_sha256=payload.get("render_sha256"),
        annulled_record_id=payload.get("annulled_record_id"),
        role_digest=payload.get("role_digest"),
        source_version=payload.get("source_version"),
        source_sha256=payload.get("source_sha256"),
        build_env_id=payload.get("build_env_id"),
    )


def published_records(phases: Sequence[ChannelPhase]) -> tuple[RunRecord, ...]:
    """Every run record on the channel, oldest first."""
    return tuple(
        record_from_payload(phase.payload["record"])
        for phase in phases
        if phase.phase == RUN_RECORD_PHASE
    )


def markers_published_at(
    phases: Sequence[ChannelPhase], artifact_seq: int, *, created_at: Mapping[int, datetime]
) -> datetime | None:
    """When the marker list for this version appeared in the channel — the FIRST time.

    The first and not the last: a second publication after the canary has run cannot make the
    criterion pre-declared, and taking the latest would let exactly that pass.
    """
    times = [
        created_at[phase.seq]
        for phase in phases
        if phase.phase == CANARY_MARKERS_PHASE
        and phase.payload.get("artifact_seq") == artifact_seq
        and phase.seq in created_at
    ]
    return min(times) if times else None


def latest_record(
    phases: Sequence[ChannelPhase], kind: RunKind, artifact_seq: int | None = None
) -> Mapping[str, Any] | None:
    """The newest published record of one kind (optionally for one version of the artefact)."""
    found = [
        phase.payload["record"]
        for phase in phases
        if phase.phase == RUN_RECORD_PHASE
        and phase.payload["record"].get("kind") == kind.value
        and (artifact_seq is None or phase.payload.get("artifact_seq") == artifact_seq)
    ]
    return found[-1] if found else None


#: How the driver reaches the channel. A callable rather than a client object: the ordering
#: this module exists to protect is testable only if sending can be replaced by a list.
PostMessage = Callable[[str, dict], Mapping[str, Any]]
FetchMessages = Callable[[int], Sequence[Mapping[str, Any]]]


def post_phase(post: PostMessage, payload: Mapping[str, Any], *, role: str = "development") -> int:
    """Send one genre phase and return the sequence number the channel gave it.

    The number is returned rather than dropped because the genre binds by it: a canary record
    names the message its marker list arrived in, and a binding to "the list, somewhere in
    the channel" would be a binding to nothing.
    """
    reasons = _schema_reasons(payload)
    if reasons:
        raise ChannelError(reasons)
    sent = post("messages", {"role": role, "kind": "notice", "payload": dict(payload)})
    return int(sent["seq"])
