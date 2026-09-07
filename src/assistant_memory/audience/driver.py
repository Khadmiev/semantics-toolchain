# SPDX-License-Identifier: Apache-2.0
"""One round of an audience review, in the order the order has to be.

WHAT THIS CLOSES. Six stages built mechanisms and nothing ran them. The sequence — what
happens before what, and what refuses before anything expensive starts — lived in one
person's head and in a throwaway script outside the repository. That is the thing this
module is: the order, owned by the product.

THE ORDER, AND WHY EACH POSITION IS LOAD-BEARING.

1. Every refusal that can be found without spending anything is found FIRST. A prompt that
   would leak, an artefact the coverage builder cannot see, a service that does not say what
   genre this review is — each of those costs nothing to catch now and costs a launch, a
   journal number and an operator's evening to catch later.

2. The mechanical layer runs BEFORE any model, because it is free and deterministic. It
   blocks nothing at any threshold; it publishes and stands aside.

3. The blind pass runs only if the reader fingerprint MOVED. Otherwise the previous result
   is carried, saying out loud which round it came from — "read again and found the same" and
   "not read again" are different facts about this version, and a carry that hides which one
   it is turns a saved run into a false one.

4. The marker list is published BEFORE the canary launches, and the canary's record names the
   message it arrived in. The order IS the evidence: a criterion chosen once the answer is
   known proves nothing about the answer.

5. The canary runs before the reading, adjacent to it in the launch counter, in the same
   arranged isolation. Any gap breaks the binding — and the binding, not the three separate
   facts, is what makes a clean canary mean anything for THIS reading.

WHAT THE DRIVER MAY NOT DO, kept as a list because it would be so easy to drift into:
it does not declare convergence (the critic declares, the server validates); it does not put
a terminal status on a claim-map row (only a seeing pass or the operator may); it does not
run the seeing pass (that is the critic through the ordinary orchestration); and it does not
supply the operator's two declarations, whose absence counts as "not done".
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from assistant_memory.audience import channel
from assistant_memory.audience.blind_prompt import (
    AssembledPrompt,
    AssemblyRefused,
    RoleTemplate,
    prepare_blind_prompt,
)
from assistant_memory.audience.blind_report import (
    extract_findings,
    findings_signature,
    render_blind_report,
    report_refusals,
)
from assistant_memory.audience.contract import AudienceContract
from assistant_memory.audience.dedup import mark_findings
from assistant_memory.audience.isolation import ArrangedIsolation
from assistant_memory.audience.launcher import BlindLauncher, canary_verdict
from assistant_memory.audience.machine_comb import (
    extract_findings as extract_machine_findings,
)
from assistant_memory.audience.machine_comb import (
    render_machine_report,
)
from assistant_memory.audience.machine_comb import (
    report_refusals as machine_report_refusals,
)
from assistant_memory.audience.mechanical import (
    MechanicalRefused,
    MechanicalThresholds,
    TermInventory,
    measure,
    retelling_diff,
)
from assistant_memory.audience.planner import BlindAction, plan_blind_pass
from assistant_memory.audience.records import (
    RunKind,
    RunOutcome,
    RunRecord,
    carried_role_forward,
    credit_blind_reading,
    credit_role_run,
)
from assistant_memory.audience.report_schema import SectionBudgets
from assistant_memory.audience.structured import annulment_is_deterministic
from assistant_memory.audience.role_prompts import (
    LANGUAGE_FIELDS,
    MachinePrompt,
    StrategicPrompt,
    prepare_machine_prompt,
    prepare_strategic_prompt,
    role_fingerprint,
)
from assistant_memory.audience.sections import (
    ArtifactError,
    ReaderArtifact,
    parse_reader_artifact,
    reader_fingerprint,
)
from assistant_memory.audience.strategic import (
    extract_findings as extract_strategic_findings,
)
from assistant_memory.audience.strategic import (
    inclusion_refusals as strategic_inclusion_refusals,
)
from assistant_memory.audience.strategic import (
    render_strategic_report,
)
from assistant_memory.audience.strategic import (
    report_refusals as strategic_report_refusals,
)
from assistant_memory.audience.structured import parse_structured_answer
from assistant_memory.audience.transcript import extract_answer
from assistant_memory.review.genres import (
    AUDIENCE_GENRE,
    MACHINE_COMB_KEY,
    STRATEGIC_READER_KEY,
    config_refusals,
    genre_of,
)


class RoundRefused(RuntimeError):
    """This round will not proceed, with EVERY reason rather than the first.

    Every reason because the operator re-running a round wants the whole list, not the first
    tripwire followed by another run that trips the second.
    """

    def __init__(self, reasons: Sequence[str], *, needs_operator: bool = False) -> None:
        self.reasons = list(reasons)
        self.needs_operator = needs_operator
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class RoundInputs:
    """Everything one round needs, all of it declared rather than discovered.

    Declared because every one of these has a failure mode where guessing looks like working:
    an inferred term inventory reports clean over nothing, an assumed budget compares an
    estimate with an invention, and a marker list assembled on the fly is a criterion chosen
    after the fact.
    """

    artifact_seq: int
    iteration: int
    artifact_text: str
    contract: AudienceContract
    template: RoleTemplate
    inventory: TermInventory
    budgets: Mapping[str, float]
    markers: Sequence[str]
    model: str
    model_version: str
    thresholds: MechanicalThresholds = MechanicalThresholds()
    #: Word budgets of the report's sections — declared, because the reader is TOLD them and
    #: the record carries them: comparability of two runs is an equality of numbers.
    report_budgets: SectionBudgets = SectionBudgets()
    #: The v2 role templates. Required exactly when the review config enables the role —
    #: preflight refuses an enabled role with no template rather than skipping it silently.
    strategic_template: RoleTemplate | None = None
    machine_template: RoleTemplate | None = None
    #: The LIVE runner version, probed by the caller from the configured binary (operator
    #: decision, review b5fd70de: environment is probed, never fingerprinted). Every
    #: transfer gate holds it against the source record's own ``runner_version``;
    #: ``None`` fails closed — no probe, no transfer, fresh runs instead.
    live_runner_version: str | None = None


@dataclass(frozen=True)
class _RoleSpec:
    """Everything one v2 role pass needs, closed over its assembled prompt.

    A value object rather than two near-identical methods: the strategic and machine
    passes share one lifecycle completely, and two slightly different automatons are the
    known road to divergence — the differences ride here as data.
    """

    kind: RunKind
    phase_name: str
    digest: str
    prompt_text: str
    prompt_digest: str
    category_set_version: str
    record_extra: Mapping[str, object]
    validate: Callable[[Mapping], list[str]]
    extract: Callable[[Mapping], list[dict]]
    render: Callable[[Mapping], str]
    build_phase: Callable[..., dict]


@dataclass
class RoleResult:
    """One v2 role's outcome within the round — the operator-facing summary of the pass."""

    record: RunRecord | None = None
    credited: bool = False
    reasons: tuple[str, ...] = ()
    carried_from_iteration: int | None = None
    needs_operator: bool = False


@dataclass
class RoundOutcome:
    """What this round actually did — enough for the operator to see it without the channel."""

    artifact_seq: int
    iteration: int
    reader_digest: str
    blind_action: BlindAction
    reasons: tuple[str, ...] = ()
    mech_candidates: tuple[str, ...] = ()
    canary_record: RunRecord | None = None
    blind_record: RunRecord | None = None
    credited: bool = False
    credit_reasons: tuple[str, ...] = ()
    carried_from_iteration: int | None = None
    #: Files the run left in a directory where nothing should have been written. Evidence,
    #: carried up rather than swept: see the isolation module.
    residue: tuple[str, ...] = ()
    posted_seqs: dict[str, int] = field(default_factory=dict)
    #: True when the round ended on a question only the operator may answer — a second
    #: validation refusal in a row: a model that cannot hold the schema is a property of the
    #: instrument, not a quota expense, and the operator must learn of it.
    needs_operator: bool = False
    #: The v2 role passes, by kind value («стратегический» / «машинный») — present exactly
    #: for the roles the review config enables.
    role_results: dict[str, RoleResult] = field(default_factory=dict)


@dataclass
class AudienceRound:
    """Drives one round. Every collaborator is injected, and that is not ceremony.

    The order is what this module exists to protect, and an order can only be tested if the
    things it orders can be replaced: a channel by a list, a model by a function. Everything
    that talks to the world arrives from outside.
    """

    post: channel.PostMessage
    fetch: channel.FetchMessages
    launcher: BlindLauncher
    isolation: ArrangedIsolation
    review_config: Mapping[str, object] | None
    transcripts_dir: Path
    previous_records: Sequence[RunRecord] = ()
    credited_ids: Sequence[str] = ()

    # --- 1. everything that can refuse for free ------------------------------------------

    def preflight(
        self, inputs: RoundInputs
    ) -> tuple[
        ReaderArtifact, AssembledPrompt, StrategicPrompt | None, MachinePrompt | None
    ]:
        """Refusals first, spending later. Raises with every reason at once."""
        reasons: list[str] = []
        needs_operator = False

        # The genre is read from the service, never assumed: a review whose genre cannot be
        # read is not "an ordinary review", it is a review nobody has identified. Silence
        # here used to select the ordinary protocol, which is the same defect in reverse.
        reasons += config_refusals(dict(self.review_config or {}) or None)
        if genre_of(dict(self.review_config or {})) != AUDIENCE_GENRE:
            reasons.append(
                "жанр ревью не аудиторный (или конфигурация не прочитана) — этот проход "
                "назначается только аудиторному жанру, и молчание здесь однажды уже "
                "выбирало обычный протокол"
            )

        artifact: ReaderArtifact | None = None
        try:
            artifact = parse_reader_artifact(inputs.artifact_text)
        except ArtifactError as refusal:
            reasons += refusal.reasons

        prompt: AssembledPrompt | None = None
        if artifact is not None:
            try:
                prompt = prepare_blind_prompt(
                    inputs.template, inputs.contract, artifact, inputs.report_budgets
                )
            except AssemblyRefused as refusal:
                reasons += refusal.reasons
                needs_operator = needs_operator or refusal.needs_operator

        if not [marker for marker in inputs.markers if marker.strip()]:
            reasons.append(
                "список маркеров канарейки пуст — критерий, которому удовлетворяет любой "
                "ответ, не отличает чистый прогон от грязного"
            )

        # THE v2 ROLES — enabled by the review config's EXPLICIT booleans (validated by
        # `config_refusals` above), each with its admissibility checked before anything is
        # spent. An enabled strategic role without a declared takeaway is a configuration
        # refusal with a named reason, never a silent switch-off.
        config = dict(self.review_config or {})
        strategic_on = config.get(STRATEGIC_READER_KEY) is True
        machine_on = config.get(MACHINE_COMB_KEY) is True
        reasons += strategic_inclusion_refusals(strategic_on, inputs.contract)
        strategic_prompt: StrategicPrompt | None = None
        machine_prompt: MachinePrompt | None = None
        if strategic_on:
            if inputs.strategic_template is None:
                reasons.append(
                    "стратегический читатель включён, а его шаблон не передан — роль без "
                    "шаблона не запускается, и молчание тут не выключение"
                )
            elif artifact is not None:
                try:
                    strategic_prompt = prepare_strategic_prompt(
                        inputs.strategic_template, inputs.contract, artifact
                    )
                except AssemblyRefused as refusal:
                    reasons += refusal.reasons
        if machine_on:
            if inputs.machine_template is None:
                reasons.append(
                    "прочёс машинности включён, а его шаблон не передан — роль без "
                    "шаблона не запускается, и молчание тут не выключение"
                )
            elif artifact is not None:
                language_items = tuple(
                    item
                    for item in inputs.contract.public.items
                    if item.field in LANGUAGE_FIELDS
                )
                try:
                    machine_prompt = prepare_machine_prompt(
                        inputs.machine_template, language_items, artifact
                    )
                except AssemblyRefused as refusal:
                    reasons += refusal.reasons

        self.launcher.journal.guard_placement(self.launcher.profile.settings_dir)

        if reasons or artifact is None or prompt is None:
            raise RoundRefused(reasons, needs_operator=needs_operator)
        return artifact, prompt, strategic_prompt, machine_prompt

    # --- 2. the free measurement ----------------------------------------------------------

    def run_mechanical(self, inputs: RoundInputs, artifact: ReaderArtifact) -> tuple[str, ...]:
        """Measure and publish. A refusal here stops the round: it means the layer was asked
        to report over an inventory nobody declared, and a clean report over nothing is worse
        than no report."""
        report = measure(
            artifact,
            inventory=inputs.inventory,
            budgets=inputs.budgets,
            thresholds=inputs.thresholds,
        )
        # Kept for the layer's POST-CREDITING signal: the retelling diff (AG-10) rides on a
        # later re-post of this phase, and re-posting must restate the same measurement.
        self._mech_report = report
        payload = channel.mech_report_message(inputs.artifact_seq, report)
        seq = channel.post_phase(self.post, payload)
        self._posted["mech_report"] = seq
        return report.candidates

    # --- 3-5. the blind pass, or the carry -------------------------------------------------

    def run(self, inputs: RoundInputs) -> RoundOutcome:
        """One whole round. See the module note for why the steps are in this order."""
        self._posted: dict[str, int] = {}
        artifact, prompt, strategic_prompt, machine_prompt = self.preflight(inputs)

        digest = reader_fingerprint(
            artifact,
            instructions_digest=prompt.instructions_digest,
            model=inputs.model,
            model_version=inputs.model_version,
        )

        try:
            candidates = self.run_mechanical(inputs, artifact)
        except MechanicalRefused as refusal:
            raise RoundRefused(refusal.reasons, needs_operator=True) from None

        decision = plan_blind_pass(
            current_reader_digest=digest,
            iteration=inputs.iteration,
            artifact_seq=inputs.artifact_seq,
            previous_records=self.previous_records,
            credited_ids=self.credited_ids,
            live_runner_version=inputs.live_runner_version,
        )
        outcome = RoundOutcome(
            artifact_seq=inputs.artifact_seq,
            iteration=inputs.iteration,
            reader_digest=digest,
            blind_action=decision.action,
            reasons=tuple(decision.reasons),
            mech_candidates=candidates,
            posted_seqs=dict(self._posted),
        )
        if decision.action is BlindAction.CARRY:
            outcome = self._carry(inputs, decision, outcome)
        else:
            outcome = self._launch(inputs, prompt, digest, outcome)
        # THE v2 ROLE PASSES run after the blind assessment stands (credited or lawfully
        # carried) — they are the circle's later passes, and a round whose blind half
        # failed is going back for repair anyway; spending the role runs on it would buy
        # results of a version about to move.
        if outcome.credited:
            if strategic_prompt is not None:
                self._role_pass(
                    inputs, outcome, self._strategic_spec(inputs, strategic_prompt, artifact)
                )
            if machine_prompt is not None:
                self._role_pass(
                    inputs, outcome, self._machine_spec(inputs, machine_prompt, artifact)
                )
        return outcome

    def _carry(self, inputs: RoundInputs, decision, outcome: RoundOutcome) -> RoundOutcome:
        """Publish the carried assessment, naming the round it actually came from.

        Named rather than silently re-dated: the whole value of a carry is that it can be
        checked, and a carry that presents itself as a fresh reading is indistinguishable
        from one — which is the failure this genre found twice, in planning and in the gate.
        """
        # The decision carries the source's IDENTIFIER, never a retelling of the record — so
        # the version it was read on is looked up here rather than restated, and a carry can
        # be checked against the channel instead of believed.
        source = next(
            (r for r in self.previous_records if r.id == decision.carry_from), None
        )
        if source is None:
            raise RoundRefused(
                [
                    f"перенос ссылается на запись {decision.carry_from!r}, которой нет среди "
                    "переданных — перенос без проверяемого исходника это заявление, а не факт"
                ]
            )
        # A carry rides WITH the source's findings list, not with an empty one: "read again
        # and found the same" carries what was found. The list is looked up on the channel
        # by the record it names — copied from the phase, never retold from memory.
        # THE CARRY COPIES ITS SOURCE PHASE, OR THERE IS NO CARRY. A source-less "carry"
        # used to publish an empty list and report success — the gate would later refuse
        # it, but the driver and the CLI had already told the operator a lawful carry
        # happened, and a success message that a later layer quietly contradicts is the
        # silence-looks-like-consent shape again. An unreadable channel refuses the same
        # way: a carry that cannot see its source cannot claim to restate it.
        try:
            source_phases = [
                phase
                for phase in channel.read_phases(self.fetch(0))
                if phase.phase == channel.BLIND_FINDINGS_PHASE
                and phase.payload.get("run_record_id") == source.id
            ]
        except channel.ChannelError as broken:
            raise RoundRefused(
                [f"перенос невозможен: канал не читается ({'; '.join(broken.reasons)})"]
            ) from None
        if not source_phases:
            raise RoundRefused(
                [
                    f"перенос невозможен: в канале нет исходной фазы слепой оценки для "
                    f"записи {source.id} — переносить нечего, и публиковать пустоту под "
                    "видом переноса нельзя"
                ]
            )
        signatures = {findings_signature(p.payload) for p in source_phases}
        if len(signatures) > 1:
            raise RoundRefused(
                [
                    f"перенос невозможен: исходные фазы записи {source.id} противоречат "
                    "друг другу составом находок"
                ]
            )
        latest = source_phases[-1]
        carried_findings = list(latest.payload.get("findings") or [])
        carried_universe = list(
            latest.payload.get("contract_public_items")
            or [item.id for item in inputs.contract.public.items]
        )
        seq = channel.post_phase(
            self.post,
            channel.blind_findings_message(
                inputs.artifact_seq,
                findings=carried_findings,
                run_record_id=source.id,
                reader_digest=outcome.reader_digest,
                iteration=inputs.iteration,
                contract_public_items=carried_universe,
                carried_from_artifact_seq=source.artifact_seq,
                carried_from_iteration=source.iteration,
            ),
        )
        outcome.posted_seqs["blind_findings"] = seq
        outcome.carried_from_iteration = source.iteration
        outcome.credited = True
        return outcome

    def _launch(
        self, inputs: RoundInputs, prompt: AssembledPrompt, digest: str, outcome: RoundOutcome
    ) -> RoundOutcome:
        markers_seq = channel.post_phase(
            self.post,
            channel.canary_markers_message(inputs.artifact_seq, list(inputs.markers)),
        )
        outcome.posted_seqs["canary_markers"] = markers_seq

        removed = self.isolation.prepare()
        outcome.reasons += (
            f"профиль очищен перед прогоном, удалено записей: {len(removed)}",
        )
        try:
            # THE STRUCTURED-ANSWER CONTRACT'S ORDER, and the loop is its retry clause: one
            # automatic retry as a NEW run with its own canary pair and its own record,
            # linked to the annulled one; a second refusal in a row goes to the operator.
            annulled_id: str | None = None
            round_blinds: list[RunRecord] = []
            for attempt in (1, 2):
                canary = self._run_canary(inputs, markers_seq)
                outcome.canary_record = canary
                outcome.posted_seqs["canary_record"] = channel.post_phase(
                    self.post, channel.run_record_message(canary)
                )
                if canary.outcome is not RunOutcome.HAPPENED or not canary.canary_verdict_clean:
                    # A dirty canary stops the round HERE. The reading is not launched: it
                    # would consume the next number and could never be credited, pushing the
                    # following honest pair apart for nothing.
                    outcome.credit_reasons = (
                        "канарейка не чиста — слепое чтение не запускается: "
                        f"{canary.outcome_reason or 'вердикт грязный'}",
                    )
                    return outcome

                blind = self._run_blind(inputs, prompt, digest, canary, annulled_id)
                validated = None
                diff = None
                refusals: list[str] = []
                if blind.outcome is RunOutcome.HAPPENED:
                    # THE ORDER IS FIXED AND CYCLE-FREE: validation → render → record →
                    # crediting. The answer is validated at the boundary, BEFORE the record
                    # is published; the render is built only from a valid object; the record
                    # then carries the render's path and hash, and the crediting gate checks
                    # the record whole — so "credited without a deliverable" cannot exist.
                    validated, refusals = self._validated_report(inputs, prompt, blind)
                    if refusals:
                        # An answer refused by validation ANNULS the run: one state, two
                        # names — the record carries «не состоялся» with every reason.
                        # B.11 C-1: and the CLASS of the annulment, decided here at the
                        # boundary where the refusals still carry their type.
                        blind = RunRecord(
                            **{
                                **blind.__dict__,
                                "outcome": RunOutcome.NOT_STARTED,
                                "outcome_reason": (
                                    "ответ слепого читателя не прошёл валидацию схемы: "
                                    + "; ".join(refusals)
                                ),
                                "annulment_class": (
                                    "deterministic"
                                    if annulment_is_deterministic(refusals)
                                    else "transient"
                                ),
                            }
                        )
                    else:
                        # AG-10: the between-versions signal of the mechanical layer — an
                        # INPUT to the render (purity kept), posted to the channel after
                        # crediting. Absent on a first version, and absent when the
                        # previous credited report cannot be re-read: an informational
                        # signal is honestly absent, never guessed.
                        previous_report = self._previous_validated_report(inputs)
                        diff = (
                            retelling_diff(previous_report, validated)
                            if previous_report is not None
                            else None
                        )
                        rendered = render_blind_report(
                            validated, prompt.sections, retelling_diff=diff
                        ).encode("utf-8")
                        render_path = self.transcripts_dir / f"blind_{blind.id}_report.md"
                        render_path.parent.mkdir(parents=True, exist_ok=True)
                        render_path.write_bytes(rendered)
                        blind = RunRecord(
                            **{
                                **blind.__dict__,
                                "render_path": str(render_path),
                                "render_sha256": hashlib.sha256(rendered).hexdigest(),
                            }
                        )
                outcome.blind_record = blind
                round_blinds.append(blind)
                outcome.posted_seqs["blind_record"] = channel.post_phase(
                    self.post, channel.run_record_message(blind)
                )

                if blind.outcome is RunOutcome.NOT_STARTED:
                    # B.11 C-1: the same rule as the v2 roles below. A repeat that cannot
                    # help is not spent — the identical prompt reproduces a closed-vocabulary
                    # error by construction, so the operator hears about it now instead of
                    # after a second run says the same thing.
                    if blind.annulment_class == "deterministic":
                        outcome.needs_operator = True
                        outcome.credit_reasons = (
                            "ответ аннулирован по значению вне закрытого перечня — "
                            "повтор с тем же промптом воспроизведёт ту же ошибку по "
                            "построению, автоматический прогон НЕ тратится; решение "
                            "за оператором",
                            *refusals,
                        )
                        return outcome
                    if attempt == 1:
                        annulled_id = blind.id
                        outcome.reasons += (
                            f"ответ слепого аннулирован валидацией (запись {blind.id}) — "
                            "автоматический повтор одним новым прогоном со своей "
                            "канареечной парой",
                        )
                        continue
                    # A SECOND refusal in a row is a property of the instrument, and the
                    # operator must learn of it — not a quota to keep spending.
                    outcome.needs_operator = True
                    outcome.credit_reasons = (
                        "второй отказ валидации подряд — систематический отказ модели "
                        "держать схему это свойство инструмента, решение за оператором",
                        *refusals,
                    )
                    return outcome

                verdict = credit_blind_reading(
                    blind,
                    canary,
                    all_blind_records=[*self.previous_records, *round_blinds],
                    current_reader_digest=digest,
                    markers_published_at=self._markers_time(markers_seq),
                    observed_answer_sha256=canary.answer_sha256,
                    read_transcript=self._read_transcript,
                )
                outcome.credited = verdict.credited
                outcome.credit_reasons = tuple(verdict.reasons)
                if verdict.credited and diff is not None:
                    # The layer's own phase, re-posted with the same measurement plus the
                    # diff — informational, blocking nothing at any value.
                    outcome.posted_seqs["mech_report_diff"] = channel.post_phase(
                        self.post,
                        channel.mech_report_message(
                            inputs.artifact_seq, self._mech_report, retelling_diff=diff
                        ),
                    )
                if verdict.credited:
                    # WHAT THE READER SAID IS EXTRACTED FROM THE VALIDATED OBJECT of the
                    # CREDITED run — the findings phase may reference nothing else. The ids
                    # are derived by the unchanged identity formula: the format change moved
                    # the finding's transport, never its essence.
                    public_ids = [item.id for item in inputs.contract.public.items]
                    items = extract_findings(
                        validated,
                        contract_version=blind.contract_version,
                        contract_sha256=str(blind.contract_sha256),
                    )
                    outcome.posted_seqs["blind_findings"] = channel.post_phase(
                        self.post,
                        channel.blind_findings_message(
                            inputs.artifact_seq,
                            findings=items,
                            run_record_id=blind.id,
                            reader_digest=digest,
                            iteration=inputs.iteration,
                            contract_public_items=public_ids,
                        ),
                    )
                return outcome
            return outcome  # unreachable: both attempts return from inside the loop
        finally:
            area = self.isolation.finish()
            outcome.residue = area.residue
            if area.residue:
                outcome.reasons += (
                    f"в рабочем каталоге появились файлы ({len(area.residue)}): "
                    "писать туда никто не должен был, каталог сохранён как улика",
                )

    def _validated_report(
        self, inputs: RoundInputs, prompt: AssembledPrompt, blind: RunRecord
    ):
        """The validated report object of this run, or every reason there is none.

        Three gates in file order — the answer is findable in the transcript, it is exactly
        one JSON object, the object holds the derived schema — and the reasons accumulate
        into the annulled record, where whoever re-runs the round reads the whole list.
        """
        transcript_text = Path(blind.transcript_path).read_text(encoding="utf-8")
        answer, reasons = extract_answer(transcript_text)
        if reasons:
            return None, reasons
        report, reasons = parse_structured_answer(answer)
        if reasons:
            return None, reasons
        reasons = report_refusals(
            report,
            sections=prompt.sections,
            budgets=inputs.report_budgets,
            categories=prompt.categories.names,
            section_ids=parse_reader_artifact(inputs.artifact_text).section_ids(),
            public_items=[item.id for item in inputs.contract.public.items],
        )
        return (report, []) if not reasons else (None, reasons)

    def _run_canary(self, inputs: RoundInputs, markers_seq: int) -> RunRecord:
        record = self.launcher.run(
            CANARY_PROMPT,
            kind=RunKind.CANARY,
            iteration=inputs.iteration,
            artifact_seq=inputs.artifact_seq,
            model_version=inputs.model_version,
            canary_verdict_clean=None,
            markers_message_seq=markers_seq,
            answer_message_seq=markers_seq,
            answer_sha256="",
        )
        answer = Path(record.transcript_path).read_text(encoding="utf-8")
        clean, hit = canary_verdict(answer, list(inputs.markers))
        # THE ANSWER ITSELF IS PUBLISHED, and BEFORE the record that hashes it. The record
        # carries only the hash, and a hash of an unpublished text binds nothing — the
        # record would be the only witness to its own answer. With the answer on the
        # channel, a side holding no files can hash the body, compare with the record, and
        # re-scan it against the published markers.
        answer_seq = channel.post_phase(
            self.post, channel.canary_answer_message(inputs.artifact_seq, answer)
        )
        self._posted["canary_answer"] = answer_seq
        return RunRecord(
            **{
                **record.__dict__,
                "canary_verdict_clean": clean,
                "answer_message_seq": answer_seq,
                "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                "outcome": record.outcome if clean else RunOutcome.ANNULLED,
                "outcome_reason": record.outcome_reason
                or (None if clean else f"канарейка задела маркеры: {', '.join(hit)}"),
            }
        )

    def _run_blind(
        self,
        inputs: RoundInputs,
        prompt: AssembledPrompt,
        digest: str,
        canary: RunRecord,
        annulled_record_id: str | None = None,
    ) -> RunRecord:
        return self.launcher.run(
            prompt.text,
            kind=RunKind.BLIND,
            iteration=inputs.iteration,
            artifact_seq=inputs.artifact_seq,
            model_version=inputs.model_version,
            canary_record_id=canary.id,
            prompt_digest=prompt.instructions_digest,
            reader_digest=digest,
            contract_version=prompt.contract_version,
            contract_sha256=prompt.contract_sha256,
            category_set_version=prompt.categories.version,
            report_schema_version=prompt.sections.version,
            section_budgets=prompt.section_budgets,
            annulled_record_id=annulled_record_id,
        )

    # --- the v2 role passes ----------------------------------------------------------------

    def _strategic_spec(
        self, inputs: RoundInputs, prompt: StrategicPrompt, artifact: ReaderArtifact
    ) -> _RoleSpec:
        section_ids = artifact.section_ids()
        section_texts = {s.id: s.render() for s in artifact.sections}
        contract_items = [
            item.id
            for item in (*inputs.contract.public.items, *inputs.contract.private.items)
        ]
        return _RoleSpec(
            kind=RunKind.STRATEGIC,
            phase_name=channel.STRATEGIC_FINDINGS_PHASE,
            digest=role_fingerprint(
                "strategic",
                artifact,
                instructions_digest=prompt.instructions_digest,
                model=inputs.model,
                model_version=inputs.model_version,
            ),
            prompt_text=prompt.text,
            prompt_digest=prompt.instructions_digest,
            category_set_version=prompt.categories.version,
            record_extra={
                "contract_version": prompt.contract_version,
                "contract_sha256": prompt.contract_sha256,
            },
            validate=lambda obj: strategic_report_refusals(
                obj,
                categories=prompt.categories.names,
                section_ids=section_ids,
                section_texts=section_texts,
                contract_items=contract_items,
            ),
            extract=lambda obj: extract_strategic_findings(
                obj, contract_sha256=prompt.contract_sha256
            ),
            render=render_strategic_report,
            build_phase=lambda **kw: channel.strategic_findings_message(
                contract_items=contract_items,
                contract_sha256=prompt.contract_sha256,
                **kw,
            ),
        )

    def _machine_spec(
        self, inputs: RoundInputs, prompt: MachinePrompt, artifact: ReaderArtifact
    ) -> _RoleSpec:
        section_ids = artifact.section_ids()
        section_texts = {s.id: s.render() for s in artifact.sections}
        return _RoleSpec(
            kind=RunKind.MACHINE,
            phase_name=channel.MACHINE_FINDINGS_PHASE,
            digest=role_fingerprint(
                "machine",
                artifact,
                instructions_digest=prompt.instructions_digest,
                model=inputs.model,
                model_version=inputs.model_version,
            ),
            prompt_text=prompt.text,
            prompt_digest=prompt.instructions_digest,
            category_set_version=prompt.table_version,
            record_extra={},
            validate=lambda obj: machine_report_refusals(
                obj, section_ids=section_ids, section_texts=section_texts
            ),
            extract=lambda obj: extract_machine_findings(
                obj, table_version=prompt.table_version
            ),
            render=render_machine_report,
            build_phase=lambda **kw: channel.machine_findings_message(
                table_version=prompt.table_version, **kw
            ),
        )

    def _dispositions(self) -> list[Mapping]:
        return [
            m.get("payload") or {}
            for m in self.fetch(0)
            if m.get("kind") == "disposition"
        ]

    def _role_pass(
        self, inputs: RoundInputs, outcome: RoundOutcome, spec: _RoleSpec
    ) -> None:
        """One v2 role's pass: transfer when the role fingerprint stands, otherwise a fresh
        run under the structured-answer contract (validation at the boundary, annulment
        with reasons, one linked retry, render before crediting).

        A failed pass records its reasons on the outcome and posts nothing — it does not
        abort the round (the blind assessment already stands), but the convergence gate
        will hold the review until the enabled role's phase is in order, so nothing here
        can be quietly skipped.
        """
        result = RoleResult()
        outcome.role_results[spec.kind.value] = result
        label = spec.kind.name.lower()
        try:
            phases = channel.read_phases(self.fetch(0))
        except channel.ChannelError as broken:
            result.reasons = (f"канал не читается: {'; '.join(broken.reasons)}",)
            return
        published = channel.published_records(phases)
        same_kind = [r for r in published if r.kind is spec.kind]

        # -- the transfer (AR-15 by the role fingerprint) -----------------------------------
        transferable = [
            record
            for record in same_kind
            if record.role_digest == spec.digest
            and credit_role_run(
                record, all_records=same_kind, current_role_digest=spec.digest
            ).credited
            and carried_role_forward(
                record,
                kind=spec.kind,
                iteration=inputs.iteration,
                artifact_seq=inputs.artifact_seq,
                current_role_digest=spec.digest,
                live_runner_version=inputs.live_runner_version,
            ).credited
        ]
        if transferable:
            source = max(transferable, key=lambda r: r.launch_number or 0)
            source_phases = [
                p
                for p in phases
                if p.phase == spec.phase_name
                and p.payload.get("run_record_id") == source.id
            ]
            if not source_phases:
                result.reasons = (
                    f"перенос невозможен: в канале нет исходной фазы для записи "
                    f"{source.id} — переносить нечего, публиковать пустоту под видом "
                    "переноса нельзя",
                )
                return
            latest = source_phases[-1]
            seq = channel.post_phase(
                self.post,
                spec.build_phase(
                    artifact_seq=inputs.artifact_seq,
                    findings=list(latest.payload.get("findings") or []),
                    run_record_id=source.id,
                    role_digest=spec.digest,
                    iteration=inputs.iteration,
                    carried_from_artifact_seq=source.artifact_seq,
                    carried_from_iteration=source.iteration,
                ),
            )
            outcome.posted_seqs[f"{label}_findings"] = seq
            result.record = source
            result.credited = True
            result.carried_from_iteration = source.iteration
            result.reasons = (
                f"отпечаток роли не менялся с круга {source.iteration} — результат "
                "перенесён",
            )
            return

        # -- the fresh run, with the one linked retry ---------------------------------------
        annulled_id: str | None = None
        round_records: list[RunRecord] = []
        for attempt in (1, 2):
            removed = self.isolation.prepare()
            try:
                record = self.launcher.run(
                    spec.prompt_text,
                    kind=spec.kind,
                    iteration=inputs.iteration,
                    artifact_seq=inputs.artifact_seq,
                    model_version=inputs.model_version,
                    prompt_digest=spec.prompt_digest,
                    role_digest=spec.digest,
                    category_set_version=spec.category_set_version,
                    annulled_record_id=annulled_id,
                    **spec.record_extra,
                )
            finally:
                area = self.isolation.finish()
                if area.residue:
                    outcome.residue += area.residue
            del removed
            validated = None
            refusals: list[str] = []
            if record.outcome is RunOutcome.HAPPENED:
                transcript_text = Path(record.transcript_path).read_text(encoding="utf-8")
                answer, refusals = extract_answer(transcript_text)
                if not refusals:
                    validated, refusals = parse_structured_answer(answer)
                if not refusals:
                    refusals = spec.validate(validated)
                    if refusals:
                        validated = None
                if refusals:
                    # B.11 C-1: classify BEFORE the retry is spent. The marker rides on the
                    # refusal itself (a `str` subclass), so nothing about this shows in the
                    # operator-facing reason and nothing is matched against prose.
                    deterministic = annulment_is_deterministic(refusals)
                    record = RunRecord(
                        **{
                            **record.__dict__,
                            "outcome": RunOutcome.NOT_STARTED,
                            "outcome_reason": (
                                "ответ роли не прошёл валидацию схемы: "
                                + "; ".join(refusals)
                            ),
                            "annulment_class": (
                                "deterministic" if deterministic else "transient"
                            ),
                        }
                    )
                else:
                    rendered = spec.render(validated).encode("utf-8")
                    render_path = (
                        self.transcripts_dir / f"{spec.kind.name.lower()}_{record.id}_report.md"
                    )
                    render_path.parent.mkdir(parents=True, exist_ok=True)
                    render_path.write_bytes(rendered)
                    record = RunRecord(
                        **{
                            **record.__dict__,
                            "render_path": str(render_path),
                            "render_sha256": hashlib.sha256(rendered).hexdigest(),
                        }
                    )
            result.record = record
            round_records.append(record)
            outcome.posted_seqs[f"{label}_record"] = channel.post_phase(
                self.post, channel.run_record_message(record)
            )

            if record.outcome is RunOutcome.NOT_STARTED:
                # B.11 C-1: A REPEAT THAT CANNOT HELP IS NOT SPENT. The retry clause rests
                # on a silent assumption that a validation failure is an independent random
                # event; against a deterministic model error the repeat reproduces the
                # error and buys nothing. Measured in round 5 of audience review 66651b62
                # (2026-08-22): the machine-comb role was annulled twice with the same
                # cause — the category «ШАМП» is not in the table, whose real member is
                # «ШТАМП» — and both the original run and the automatic retry made it.
                #
                # WHAT THE GENRE DID RIGHT SURVIVES THIS FIX, and that is why the change is
                # this small: it did not swallow the invalid answer, it did not credit the
                # role, and after the second annulment it escalated instead of going round
                # again. C-1 removes only the wasted second run, not the strictness.
                if record.annulment_class == "deterministic":
                    result.needs_operator = True
                    outcome.needs_operator = True
                    result.reasons = (
                        f"ответ роли «{spec.kind.value}» аннулирован по значению вне "
                        f"закрытого перечня (запись {record.id}) — повтор с тем же "
                        "промптом воспроизведёт ту же ошибку по построению, поэтому "
                        "автоматический прогон НЕ тратится; решение за оператором: "
                        + "; ".join(round_records[-1].outcome_reason.split("; ")[:6]),
                    )
                    outcome.reasons += result.reasons
                    break
                if attempt == 1:
                    annulled_id = record.id
                    outcome.reasons += (
                        f"ответ роли «{spec.kind.value}» аннулирован валидацией "
                        f"(запись {record.id}) — автоматический повтор одним новым прогоном",
                    )
                    continue
                result.needs_operator = True
                outcome.needs_operator = True
                result.reasons = (
                    "второй отказ валидации подряд — свойство инструмента, решение за "
                    "оператором",
                    *refusals,
                )
                return

            verdict = credit_role_run(
                record,
                all_records=[*same_kind, *round_records],
                current_role_digest=spec.digest,
            )
            result.credited = verdict.credited
            result.reasons = tuple(verdict.reasons)
            if not verdict.credited or validated is None:
                return
            items = spec.extract(validated)
            disposed = {
                str(d.get("finding_id")): str(d.get("outcome"))
                for d in self._dispositions()
                if d.get("outcome") in ("fixed", "waived")
            }
            prior_open = [
                item
                for phase in phases
                if phase.phase == spec.phase_name
                for item in phase.payload.get("findings") or []
                if isinstance(item, Mapping) and str(item.get("id")) not in disposed
            ]
            marked = mark_findings(items, disposed=disposed, prior_open=prior_open)
            outcome.posted_seqs[f"{label}_findings"] = channel.post_phase(
                self.post,
                spec.build_phase(
                    artifact_seq=inputs.artifact_seq,
                    findings=marked,
                    run_record_id=record.id,
                    role_digest=spec.digest,
                    iteration=inputs.iteration,
                    carried_from_artifact_seq=None,
                    carried_from_iteration=None,
                ),
            )
            return

    def _previous_validated_report(self, inputs: RoundInputs):
        """The previous VERSION's credited report object, or None when there is none.

        The source is the latest CREDITED blind record of an earlier artifact version —
        re-read from its stored transcript, never retold from memory. ``None`` covers both
        honest absences the same way, because the signal is informational: a first version
        has no predecessor, and a predecessor whose transcript no longer validates (a
        pre-schema record, a missing file) has no comparable report to offer.
        """
        blessed = set(self.credited_ids)
        candidates = [
            record
            for record in self.previous_records
            if record.kind is RunKind.BLIND
            and record.id in blessed
            and record.artifact_seq < inputs.artifact_seq
        ]
        if not candidates:
            return None
        source = max(candidates, key=lambda record: record.launch_number or 0)
        try:
            text = Path(source.transcript_path).read_text(encoding="utf-8")
        except OSError:
            return None
        answer, problems = extract_answer(text)
        if problems:
            return None
        report, problems = parse_structured_answer(answer)
        return None if problems else report

    # --- helpers that touch the world ------------------------------------------------------

    def _read_transcript(self, path: str) -> bytes | None:
        try:
            return Path(path).read_bytes()
        except OSError:
            return None

    def _markers_time(self, markers_seq: int):
        """When the channel says the marker list arrived — read back, not remembered.

        Read back because the comparison is with the canary's start, and the time development
        believes it posted at is exactly the value that must not be trusted here.
        """
        for message in self.fetch(markers_seq - 1):
            if int(message.get("seq", -1)) == markers_seq:
                created = message.get("created_at")
                if created:
                    return datetime.fromisoformat(str(created))
        return None


#: The canary's question, kept deliberately NEUTRAL. A leading frame ("do you remember our
#: project?") makes a model with no memory produce something plausible, and the check starts
#: raising false alarms — which is how a check gets switched off.
CANARY_PROMPT = (
    "Есть ли у тебя какие-нибудь сохранённые сведения о задачах, над которыми ты работал "
    "раньше? Если есть — перечисли их коротко. Если нет — так и скажи."
)
