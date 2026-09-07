# SPDX-License-Identifier: Apache-2.0
"""Genre mode: what runs on an iteration, and what has to hold before convergence.

TWO READERS PER ITERATION, and the informed one reads every round. What is guaranteed is
not a fresh blind reading each round but the EXISTENCE of a live blind assessment of the
current version: the blind pass runs when the reader fingerprint moved, and otherwise the
previous result is carried forward with the carry declared and the link checkable.

The hole this closes was measured on the pilot: version 11 was the one to be shown, version
3 was the last one read blind, and the eight informed passes in between were fixing the
packaging of evidence — the kind of edit that improves checkability and damages
intelligibility, and the only reader who can notice that is the one who does not know.

THE AXES ARE SPLIT, and this module is the reason it matters. The review's ``mode`` answers
one question — what shape the artefact was carried in (a diff, or a text bundle) — and the
server needs it for that. The GENRE answers a different one: what pass plan and what
denominator apply. One field cannot answer both without one of the answers being a value
nobody chose; so pass planning reads the genre and never the mode.

NOTHING HERE TOUCHES THE CHANNEL. Facts that come from the channel are arguments, exactly as
they are for the crediting check — which keeps the whole plan exercisable without a live
review, and keeps transport decisions out of the planner.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime

from assistant_memory.audience.claim_map import ClaimRow, blocking_rows
from assistant_memory.audience.records import (
    CreditVerdict,
    RunKind,
    RunOutcome,
    RunRecord,
    TranscriptReader,
    carried_forward,
    credit_blind_reading,
    runner_transfer_reasons,
)
from assistant_memory.review.genres import (
    AUDIENCE_GENRE,
    COLD_VERDICT_FIRST_KEY,
    config_refusals,
    genre_of,
)

#: The names live on the review side (``review.genres``) so that the genre package and the
#: coverage builder can agree on them without either importing the other.
__all__ = [
    "AUDIENCE_GENRE",
    "COLD_VERDICT_FIRST_KEY",
    "BlindAction",
    "BlindDecision",
    "IterationPlan",
    "blind_assessment_gate",
    "config_refusals",
    "convergence_gate",
    "genre_of",
    "latest_credited_blind",
    "plan_blind_pass",
    "plan_iteration",
]


class BlindAction:
    RUN = "запустить"
    CARRY = "перенести"
    BLOCKED = "заблокировано"


@dataclass(frozen=True)
class BlindDecision:
    action: str
    reasons: list[str] = field(default_factory=list)
    #: The record a carry stands on — never a retelling of it, only its identifier.
    carry_from: str | None = None
    carry_iteration: int | None = None


def latest_credited_blind(
    records: Iterable[RunRecord], *, credited_ids: Collection[str]
) -> RunRecord | None:
    """The newest CREDITED blind run, by launch number.

    ``credited_ids`` is REQUIRED, and this is the whole point of the argument. Being
    credited is a property of the linked PAIR plus facts that live in the channel — a clean
    canary, adjacent launch numbers, equal profile fingerprints, a readable transcript — and
    this module deliberately has none of that. An earlier version filtered on kind, outcome
    and launch number alone and called the result "credited": a reading whose canary was
    dirty, whose profile had moved, or whose transcript was gone could then become the source
    of a carry and open the next version's convergence gate. The name asserted a check that
    was never run.

    So the caller passes the ids the crediting check actually blessed. An empty set means
    nothing may be carried — fail-closed, and the honest answer when crediting has not been
    established.

    By launch number and not by iteration: the launch counter is the one ordering a
    carried-forward record cannot confuse, since a carry moves the round without moving the
    counter.
    """
    blessed = set(credited_ids)
    candidates = [
        record
        for record in records
        if record.kind is RunKind.BLIND
        and record.outcome is RunOutcome.HAPPENED
        and record.launch_number is not None
        and record.id in blessed
    ]
    return max(candidates, default=None, key=lambda r: r.launch_number or 0)


def plan_blind_pass(
    *,
    current_reader_digest: str,
    iteration: int,
    artifact_seq: int,
    previous_records: Iterable[RunRecord] = (),
    credited_ids: Collection[str] = (),
    live_runner_version: str | None = None,
) -> BlindDecision:
    """Run the blind pass, or carry the previous result — and say which, with reasons.

    A carry is legitimate exactly when the reader fingerprint has not moved: that is what
    "nothing the blind reader sees has changed" means, and it is checkable rather than
    asserted. A moved private goal does NOT annul a carry — the blind reader never saw the
    goal — but development still recomputes the comparison of his retelling against the new
    goal, and any divergence is a finding of the CURRENT round.

    THE RUNNER CONDITION ROUTES TO A FRESH RUN, NOT TO A BLOCK. A diverged (or unprobed)
    live runner version is not a pathological carry — it is the environment saying
    "re-measure": the lawful answer is a new run, and only the carry-integrity failures
    (backwards iteration, wrong kind) block the round.
    """
    previous = latest_credited_blind(previous_records, credited_ids=credited_ids)
    if previous is None:
        return BlindDecision(
            action=BlindAction.RUN,
            reasons=["слепой оценки этой версии ещё нет — прогон обязателен"],
        )
    if previous.reader_digest != current_reader_digest:
        return BlindDecision(
            action=BlindAction.RUN,
            reasons=[
                "читательский отпечаток изменился — то, что видит слепой читатель, "
                "сдвинулось, и прошлый результат текущей версии не описывает"
            ],
        )
    runner_reasons = runner_transfer_reasons(previous, live_runner_version)
    if runner_reasons:
        return BlindDecision(action=BlindAction.RUN, reasons=runner_reasons)
    carry = carried_forward(
        previous,
        iteration=iteration,
        artifact_seq=artifact_seq,
        current_reader_digest=current_reader_digest,
        live_runner_version=live_runner_version,
    )
    if not carry.credited:
        return BlindDecision(action=BlindAction.BLOCKED, reasons=carry.reasons)
    return BlindDecision(
        action=BlindAction.CARRY,
        reasons=[f"читательский отпечаток не менялся с круга {previous.iteration}"],
        carry_from=previous.id,
        carry_iteration=previous.iteration,
    )


@dataclass(frozen=True)
class IterationPlan:
    """What this round runs. Both passes belong to the round; they do not depend on each other."""

    iteration: int
    artifact_seq: int
    informed: bool
    blind: BlindDecision

    @property
    def blocked(self) -> bool:
        return self.blind.action == BlindAction.BLOCKED


def plan_iteration(
    *,
    iteration: int,
    artifact_seq: int,
    current_reader_digest: str,
    previous_records: Iterable[RunRecord] = (),
    credited_ids: Collection[str] = (),
    live_runner_version: str | None = None,
) -> IterationPlan:
    """One iteration = both assessments of the SAME version; inside it the passes are independent.

    The informed pass reads every round unconditionally — it sees the graph, the repository
    and the claim map, and every one of those can move while the reader's text stands still.
    """
    return IterationPlan(
        iteration=iteration,
        artifact_seq=artifact_seq,
        informed=True,
        blind=plan_blind_pass(
            current_reader_digest=current_reader_digest,
            iteration=iteration,
            artifact_seq=artifact_seq,
            previous_records=previous_records,
            credited_ids=credited_ids,
            live_runner_version=live_runner_version,
        ),
    )


def blind_assessment_gate(
    *,
    artifact_seq: int,
    iteration: int,
    current_reader_digest: str,
    blind: RunRecord | None,
    canary: RunRecord | None,
    all_blind_records: Iterable[RunRecord] = (),  # noqa: ARG001 — forwarded, see below
    markers_published_at: datetime | None = None,
    observed_answer_sha256: str | None = None,
    read_transcript: TranscriptReader | None = None,
    carried_from: RunRecord | None = None,
    carry_iteration: int | None = None,
    credited_ids: Collection[str] = (),
    live_runner_version: str | None = None,
) -> CreditVerdict:
    """Is there a live blind assessment of THIS version? Convergence is not declared without one.

    Two legitimate shapes, and both are checked rather than declared: a credited run of this
    version, or a carry whose source record is named, whose reader fingerprint equals the
    current one, and which is marked as a carry. An old record without the carry mark is not
    "an acceptable old one" — it is a missing current one.

    ``iteration`` is the CURRENT round and is required rather than derived. An earlier draft
    synthesised it as "the carried round plus one", which made the forward-progress check
    inside the carry pass unconditionally — a check that can never fail is not a check.
    """
    if carried_from is not None:
        # THE CARRY MUST STAND ON A CREDITED READING, here as well as in planning. The
        # planning path was fixed one round earlier and this one was not: the class was
        # swept along the axis "names that assert a check" and not along "every path that
        # can produce a carry", so the sibling survived. Without this, a HAPPENED record
        # with a matching reader digest opened convergence with its canary, its profile
        # binding and its transcript never examined.
        if carried_from.id not in set(credited_ids):
            return CreditVerdict(
                credited=False,
                reasons=[
                    f"перенос опирается на запись {carried_from.id}, не прошедшую проверку "
                    "засчитываемости пары — переносить можно только засчитанное чтение"
                ],
            )
        verdict = carried_forward(
            carried_from,
            iteration=iteration,
            artifact_seq=artifact_seq,
            current_reader_digest=current_reader_digest,
            live_runner_version=live_runner_version,
        )
        if carry_iteration is None:
            verdict.reasons.append(
                "перенос не называет исходный круг — без него связь версии с кругом "
                "проверить нечем"
            )
            return CreditVerdict(credited=False, reasons=verdict.reasons)
        if carry_iteration != carried_from.iteration:
            verdict.reasons.append(
                f"перенос называет круг {carry_iteration}, а исходная запись прогона "
                f"относится к кругу {carried_from.iteration}"
            )
            return CreditVerdict(credited=False, reasons=verdict.reasons)
        return verdict

    if blind is None or canary is None:
        return CreditVerdict(
            credited=False,
            reasons=[
                "слепой оценки текущей версии нет: ни засчитанного прогона, ни переноса"
            ],
        )
    verdict = credit_blind_reading(
        blind,
        canary,
        all_blind_records=all_blind_records,
        current_reader_digest=current_reader_digest,
        markers_published_at=markers_published_at,
        observed_answer_sha256=observed_answer_sha256,
        read_transcript=read_transcript,
    )
    if blind.artifact_seq != artifact_seq:  # noqa: SIM102 — kept flat for the reason list
        verdict = CreditVerdict(
            credited=False,
            reasons=[
                *verdict.reasons,
                f"прогон относится к версии {blind.artifact_seq}, а гейт спрашивает про "
                f"{artifact_seq}",
            ],
        )
    return verdict


def convergence_gate(
    *,
    artifact_seq: int,
    iteration: int,
    current_reader_digest: str,
    claim_rows: Iterable[ClaimRow] = (),
    informed_clean: bool = False,
    **blind_kwargs,
) -> CreditVerdict:
    """Everything that must hold before convergence may be declared, with every reason.

    Two of the four inputs live here and are checkable: a live blind assessment of THIS
    version, and every claim-map row terminal. The other two — that the reader's retelling
    matched the intended takeaway, and that the goal did not move — are the OPERATOR's
    declarations about his own intent, and no other side has access to them. They are not
    faked here with a machine substitute; the caller passes what the operator declared, and
    absence counts as "not met".
    """
    reasons: list[str] = []
    if not informed_clean:
        reasons.append("зрячий проход последней итерации не чист")

    blind = blind_assessment_gate(
        artifact_seq=artifact_seq,
        iteration=iteration,
        current_reader_digest=current_reader_digest,
        **blind_kwargs,
    )
    reasons += blind.reasons
    reasons += blocking_rows(claim_rows)
    return CreditVerdict(credited=not reasons, reasons=reasons)
