# SPDX-License-Identifier: Apache-2.0
"""Run records: one per model run, linked by reference, and the creditability check.

ONE RECORD PER RUN, NO EXCEPTIONS. The genre has three kinds of model run — canary, blind
and visual — and each produces its own immutable record. No record retells another's
contents: they are joined by reference, and the check resolves the reference and compares
the two records itself. A retold copy drifts from its original exactly the way two copies
of one rule drift, and the spec has been bitten by that twice.

WHAT THE CHECK IS FOR. A complete record with a dirty canary is still a complete record.
Checking the FORM and calling it done would pass a substantively worthless run with a
beautifully filled-in record — which is the failure the whole binding exists to prevent.
So ``credit_blind_reading`` checks substance: outcome, verdict, tool calls, the resolved
link, equal profiles, adjacent launch numbers, ordering in time, and that the reading was
of the CURRENT version.

WHAT IS DELIBERATELY NOT HERE. No I/O and no channel access: everything the check needs
that comes from the review channel (when the marker list was published, what the answer
hashed to) is passed in. That keeps the mechanism testable without a live review, and it
keeps the decision "the genre rides inside existing message kinds" out of the runner.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class RunKind(StrEnum):
    CANARY = "канареечный"
    BLIND = "слепой"
    VISUAL = "визуальный"
    #: The two v2 roles. Fresh one-off sessions with structured answers; NO canary and NO
    #: isolation fields by construction — knowledge isolation is not claimed for them (the
    #: strategic role holds the whole contract on purpose, the machine one only text), the
    #: same boundary as the visual pass.
    STRATEGIC = "стратегический"
    MACHINE = "машинный"


class RunOutcome(StrEnum):
    HAPPENED = "состоялся"
    ANNULLED = "аннулирован"
    #: The run produced no creditable result — two shapes, ONE state by the structured-answer
    #: contract (its own words: an annulled run EQUALS a record with this outcome): assembly
    #: refused before anything was launched, or the answer was refused by schema validation
    #: after a launch. Either way the record carries the full reason list, and a retry is a
    #: NEW run whose record names this one.
    NOT_STARTED = "не состоялся"


#: What the spend field carries when the provider reported no number. An estimate in its
#: place would be indistinguishable from a measurement when read back, and would corrupt
#: the one series the journal is kept for.
SPEND_UNMEASURED = "не измерено"


def _is_blank(value: object) -> bool:
    """A string of nothing but space is an unfilled field wearing the shape of a filled one."""
    return isinstance(value, str) and not value.strip()


@dataclass(frozen=True)
class RunRecord:
    """One model run. Immutable: a correction is a NEW record naming the one it replaces."""

    # --- common to all three kinds ---
    id: str
    kind: RunKind
    iteration: int
    artifact_seq: int
    profile_digest: str
    #: ``None`` only when nothing was launched — assembly refused before the model was
    #: called. The counter must NOT move for such an attempt: a consumed number would make
    #: the next honest pair non-adjacent and break a binding that has nothing wrong with it.
    launch_number: int | None
    started_at: datetime
    finished_at: datetime
    transcript_path: str
    transcript_sha256: str
    tool_calls: tuple[str, ...]
    model: str
    model_version: str
    spend: int | str
    outcome: RunOutcome
    #: The runner (CLI) version, read from the transcript. Recorded beside the model version
    #: because it decides what a run can do AT ALL: a stale binary refuses the configured
    #: model outright, and two runs on different runners are not comparable even when
    #: everything else about the profile matches. Learned live — this box carries three
    #: codex installs and a bare `codex` resolves to the oldest.
    runner_version: str | None = None
    #: The PROVIDER's own words about the launch, copied from the transcript header.
    #:
    #: Recorded as fields rather than left implicit in ``outcome`` because that is the
    #: difference between a checkable fact and trust. The launcher annuls a run whose header
    #: is wrong, so ``HAPPENED`` transitively implied a clean header — but the pair check
    #: could not SEE it, and a record is a claim by the side being checked. Now the gate
    #: reads the sandbox and approval modes itself.
    #:
    #: WHAT THIS DOES AND DOES NOT ESTABLISH — corrected 2026-08-04 after the claim was
    #: overstated for three review rounds. `read-only` is the sandbox policy applied WHEN
    #: model-generated shell commands are executed (`codex exec --help`); it forbids writing,
    #: not executing. So the header establishes that nothing was WRITTEN. It does not
    #: establish that nothing ran, and neither does the tool-call list, which proves presence
    #: and never absence.
    #:
    #: That gap is not a hole to be plugged: by operator decision (2026-08-04) absolute
    #: blindness is not the goal. The blind reader is a coarse approximation of one specific
    #: human whose language level, past questions and edits are already known, so some
    #: context leaking is accepted, and the residual channels are closed by CHECKING the
    #: transcript rather than by proving a negative. The check is therefore as coarse as the
    #: approximation it serves, and saying so is the fix.
    sandbox_mode: str | None = None
    approval_mode: str | None = None
    #: Required whenever the outcome is not ``HAPPENED``: an annulled run without a reason
    #: is indistinguishable from one nobody bothered to explain, and the journal of runs
    #: exists precisely so that the failures stay legible later.
    outcome_reason: str | None = None
    #: Identity of the mechanical layer's thresholds in force for this run (step 5). NOT in
    #: the required list, and deliberately so: the mechanical layer cannot block convergence
    #: at any threshold value, so letting its absence make a run uncreditable would hand it
    #: exactly the power the spec denies it. Its absence is not read as agreement either —
    #: comparing two runs on it treats a missing digest as "not comparable", never as "same".
    mech_thresholds_digest: str | None = None
    supersedes: str | None = None

    # --- canary only ---
    canary_verdict_clean: bool | None = None
    markers_message_seq: int | None = None
    answer_message_seq: int | None = None
    answer_sha256: str | None = None

    # --- blind only ---
    canary_record_id: str | None = None
    prompt_digest: str | None = None
    reader_digest: str | None = None
    contract_version: int | None = None
    contract_sha256: str | None = None
    category_set_version: str | None = None
    #: Version of the derived report SECTION set — the same device as the category set
    #: version: reports produced under different section sets are not comparable, so the
    #: identity of the derivation rides in the record.
    report_schema_version: str | None = None
    #: The section budgets IN FORCE for this run, as one canonical string of the values —
    #: comparability of two runs is checked by equality of these numbers, not by a diff of
    #: prose (the same device as the mechanical thresholds).
    section_budgets: str | None = None
    #: The deterministic render of the validated report — built after validation and BEFORE
    #: crediting, so "a credited run without a deliverable file" is unrepresentable: the
    #: crediting gate demands the pair with the rest of the record. The hash lives in
    #: ``render_sha256`` below — the field is SHARED with the visual kind (each kind's
    #: required list claims it for its own render), the path is the blind kind's own.
    render_path: str | None = None
    #: Set on a RETRY after a validation annulment, naming the annulled record. Absent on a
    #: primary run, REQUIRED of a retry: the crediting gate resolves it to a record with the
    #: outcome «не состоялся» of the same kind, the same artifact version and an EQUAL role
    #: fingerprint — a retry is lawful only while the inputs stand still.
    annulled_record_id: str | None = None
    #: B.11 C-1: WHY this run was annulled, in the one dimension the retry clause needs —
    #: `transient` (a mangled or truncated answer: a repeat is an independent event and is
    #: worth spending) or `deterministic` (a value outside a CLOSED vocabulary, or any other
    #: reason an identical prompt reproduces by construction: the repeat reproduces the
    #: error, so it is NOT spent and the role's gate escalates at once). Absent on a run
    #: that was not annulled. Classified structurally at the boundary — see
    #: `structured.annulment_is_deterministic` — never by matching the reason's prose.
    annulment_class: str | None = None

    # --- strategic / machine only ---
    #: The role's own fingerprint — the launch gate of its pass (normalized section text +
    #: assembled prompt sans artifact + model with version). The strategic one covers the
    #: FULL contract through the prompt; the machine one covers the language fields and the
    #: fixed category table. Plays the part the reader fingerprint plays for the blind
    #: kind: equal fingerprint = lawful transfer, and the retry rule binds on it.
    role_digest: str | None = None

    # --- visual only ---
    source_version: int | None = None
    source_sha256: str | None = None
    render_sha256: str | None = None
    build_env_id: str | None = None

    # --- B.8 F-3: the DEGRADED launch, recorded by the TRACT (never by the model) ---
    #: ``grounds_inline`` when a declared ground failed its probe and a live operator
    #: `grounds_override` sent the run degraded; None for an ordinary run. A degraded
    #: report and a full one used to be indistinguishable except to whoever opened the
    #: log; naming the mode and the unchecked class turns degradation into a NUMBER over
    #: time.
    grounds_mode: str | None = None
    failed_ground: str | None = None
    unchecked_class: str | None = None

    def missing_fields(self) -> list[str]:
        """Fields required for this kind that are absent — the closed schema, checked.

        A closed list of fields is what lets the gate verify completeness by comparison
        instead of by reading prose; the discriminator is what lets a legitimate visual
        record through without the canary fields, which a single flat schema would reject.

        A run that never started is the one deliberate relaxation: it has no launch number,
        no prompt digest and no transcript of a model's words, because none of those exist
        for an attempt that was refused before the call. Demanding them would leave an
        honest refusal permanently "incomplete" and push whoever files it to invent values.
        Nothing is weakened by this — crediting requires the outcome to be «состоялся», so
        a not-started record is refused on substance well before its form is examined.
        """
        if self.outcome is RunOutcome.NOT_STARTED:
            return [] if self.outcome_reason else ["outcome_reason"]

        # The runner is required for anything that actually LAUNCHED. It decides what a run
        # can do at all — a stale binary refuses the configured model outright — and the
        # record already claims that two runs on different runners are not comparable. A
        # claim like that has to be a field the gate can demand, or it is prose.
        # The MODEL's version sits beside the runner's for the same reason and was missing
        # from this list: the pair check compares the two records' model versions to prove
        # that the canary measured the instrument that then read, and a comparison is only
        # as good as the schema behind it.
        common_required = ("model_version", "runner_version")

        # THE ISOLATION FIELDS ARE DEMANDED OF THE PAIR THAT CLAIMS ISOLATION, AND OF NOTHING
        # ELSE. They are the provider's own words about the launch, and they exist to back
        # the canary/reading binding. The visual pass makes no such claim — it is a SEEING
        # pass by construction, holding the claim map and the contract on purpose — so
        # requiring them of it would borrow the authority of a mechanism it does not use, and
        # would push whoever files a visual record into inventing two values nobody checks.
        isolating = (RunKind.CANARY, RunKind.BLIND)
        if self.kind in isolating:
            common_required += ("sandbox_mode", "approval_mode")

        required: dict[RunKind, tuple[str, ...]] = {
            RunKind.CANARY: (
                "canary_verdict_clean", "markers_message_seq",
                "answer_message_seq", "answer_sha256",
            ),
            RunKind.BLIND: (
                "canary_record_id", "prompt_digest", "reader_digest",
                "contract_version", "contract_sha256", "category_set_version",
                "report_schema_version", "section_budgets",
            ),
            RunKind.VISUAL: (
                "source_version", "source_sha256", "render_sha256", "build_env_id",
            ),
            # The v2 roles: the common part + the prompt digest + the ROLE's own
            # fingerprint + the category-set version; the strategic record additionally
            # binds the contract VERSION WITH THE FULL-TEXT HASH, because that role reads
            # the private part and a transfer across a changed takeaway must be
            # unrepresentable. No canary and no isolation fields — see the kind's note.
            RunKind.STRATEGIC: (
                "prompt_digest", "role_digest", "category_set_version",
                "contract_version", "contract_sha256",
            ),
            RunKind.MACHINE: ("prompt_digest", "role_digest", "category_set_version"),
        }
        # A BLANK STRING IS NOT A VALUE, and until now only ``is None`` was asked. An empty
        # ``model_version`` passed the schema and then SATISFIED the instrument-identity
        # check by matching the other record's empty one: an unidentified instrument credited
        # as "the same instrument". The same shape was reachable through every other required
        # string that the gate only compares — the reader fingerprint, the answer hash, the
        # canary's id — because two blanks always agree. So the rule is applied ONCE, over
        # every field a launched run must carry, instead of at the field the finding named:
        # a comparison is worth no more than the schema behind it.
        identity_required = (
            "id", "profile_digest", "transcript_path", "transcript_sha256", "model"
        )
        missing = [
            name
            for name in (*identity_required, *common_required, *required[self.kind])
            if getattr(self, name) is None or _is_blank(getattr(self, name))
        ]
        # The launch number is demanded of the same two kinds and for the same reason: the
        # counter proves that no unrecorded run slipped between a canary and its reading. The
        # visual pass does not launch into the blind profile at all, so it consumes no number,
        # and demanding one would make it look like part of a binding it has no place in.
        if self.kind in isolating and self.launch_number is None:
            missing.append("launch_number")
        # THE RENDER IS DEMANDED OF A RUN THAT HELD, AND OF NOTHING ELSE. It is built after
        # successful validation, so an annulled run legitimately has none — but a HAPPENED
        # record without it is a run claiming a result with no deliverable behind it, and
        # requiring the fields here is what turns "the operator gets a readable file" from
        # a promise into a property the crediting gate checks.
        structured_kinds = (RunKind.BLIND, RunKind.STRATEGIC, RunKind.MACHINE)
        if self.kind in structured_kinds and self.outcome is RunOutcome.HAPPENED:
            missing += [
                name
                for name in ("render_path", "render_sha256")
                if getattr(self, name) is None or _is_blank(getattr(self, name))
            ]
        if self.outcome is not RunOutcome.HAPPENED and not self.outcome_reason:
            missing.append("outcome_reason")
        # B.8 F-3: a record that declares a degraded launch must say the whole of it —
        # which ground failed and which class of checks the mode could not perform. Half
        # a disclosure is the silent degradation this field set exists to end.
        if self.grounds_mode is not None:
            missing += [
                name
                for name in ("failed_ground", "unchecked_class")
                if getattr(self, name) is None or _is_blank(getattr(self, name))
            ]
        return missing


@dataclass(frozen=True)
class CreditVerdict:
    """Whether the blind reading counts, and — when it does not — every reason why.

    Every failing reason is collected rather than short-circuited: an operator re-running
    a blind pass wants the whole list, not the first tripwire followed by another run that
    trips the second.
    """

    credited: bool
    reasons: list[str] = field(default_factory=list)


#: Reads a stored transcript by path. Returns ``None`` when it cannot be read at all.
#: INJECTED rather than imported so this module keeps its no-I/O property, and REQUIRED
#: rather than optional so that "the check did not run" can never look like "the check
#: passed" — the same shape as the coverage builder emitting an explicit `unchecked` row.
TranscriptReader = Callable[[str], bytes | None]


def transcript_reasons(
    record: RunRecord, what: str, read: TranscriptReader | None
) -> list[str]:
    """Ways the stored transcript does not back this record up — INCLUDING its own fields.

    The hash alone proves only that the file at that path is the one the record names. It
    proves nothing about whether the record's fields describe that file, and the fields are
    written by the side being checked. So the facts are RE-DERIVED here from the verified
    bytes and compared: the provider's header, the runner version, the executed calls. A
    record claiming `read-only` over a transcript headed `danger-full-access` used to be
    credited; now the gate reads the transcript itself and the record is a cache of it.

    The spec's own words for the first half: a record pointing at an unavailable transcript
    is not credited, because the right to check what was carried into the channel is not a
    right without a readable transcript — it is a formula.
    """
    if read is None:
        return [
            f"{what} запись: читаемость стенограммы не проверялась — проверка требует "
            "доступа к файлам и обязана передаваться явно"
        ]
    data = read(record.transcript_path)
    if data is None:
        return [
            f"{what} запись ссылается на недоступную стенограмму: {record.transcript_path}"
        ]
    actual = hashlib.sha256(data).hexdigest()
    if actual != record.transcript_sha256:
        return [
            f"{what} запись: по пути {record.transcript_path} лежит не та стенограмма "
            f"(хэш {actual[:16]}… против записанного {record.transcript_sha256[:16]}…)"
        ]

    from assistant_memory.audience import transcript as tr  # circular at module level

    text = data.decode("utf-8", errors="replace")
    header = tr.parse_header(text)
    derived = {
        "sandbox_mode": header.get("sandbox"),
        "approval_mode": header.get("approval"),
        "runner_version": tr.extract_runner_version(text),
    }
    problems = [
        f"{what} запись: поле {name} = {getattr(record, name)!r}, а сама стенограмма "
        f"говорит {value!r} — запись не описывает то, на что ссылается"
        for name, value in derived.items()
        if getattr(record, name) != value
    ]
    calls = tr.extract_tool_calls(text)
    if calls and not record.tool_calls:
        problems.append(
            f"{what} запись объявляет пустой перечень вызовов, а в стенограмме их "
            f"{len(calls)} — изоляция нарушена, и запись это скрывает"
        )
    return problems


def retry_link_reasons(
    record: RunRecord,
    universe: Sequence[RunRecord],
    *,
    what: str,
    fingerprint_field: str = "reader_digest",
) -> list[str]:
    """Every way this run's annulled-run link fails the retry rule (empty = it holds).

    ONE copy for the development gate and the channel gate — the rule is the structured
    answer contract's: a link resolves into a record with the outcome «не состоялся», of
    the same kind and artifact version, with an EQUAL role fingerprint; and where such an
    annulled twin exists, the link is mandatory, because a retry that does not name its
    annulment is a failure quietly buried by a fresh success. ``fingerprint_field`` names
    the role's own fingerprint (the blind role's is the reader fingerprint).
    """
    reasons: list[str] = []
    fingerprint = getattr(record, fingerprint_field)
    link = str(record.annulled_record_id or "").strip()
    if link:
        target = next((r for r in universe if r.id == link), None)
        if target is None:
            return [
                f"{what} запись ссылается на аннулированную запись {link!r}, которой нет "
                "среди переданных — ссылка повтора обязана разрешаться в улику"
            ]
        if target.kind is not record.kind:
            reasons.append(
                f"{what} запись: ссылка повтора ведёт на запись другого вида "
                f"({target.kind}) — повтор наследует ровно свой вид"
            )
        if target.outcome is not RunOutcome.NOT_STARTED:
            reasons.append(
                f"{what} запись: ссылка повтора ведёт на запись с исходом "
                f"«{target.outcome}», а повтор законен только после аннулированного "
                f"валидацией прогона (исход «{RunOutcome.NOT_STARTED}»)"
            )
        if target.artifact_seq != record.artifact_seq:
            reasons.append(
                f"{what} запись: повтор и аннулированная запись относятся к разным версиям "
                f"артефакта ({record.artifact_seq} и {target.artifact_seq})"
            )
        if getattr(target, fingerprint_field) != fingerprint:
            reasons.append(
                f"{what} запись: отпечаток роли повтора не равен отпечатку аннулированной "
                "записи — повтор законен только при неизменных входах"
            )
        return reasons
    twins = [
        r.id
        for r in universe
        if r.id != record.id
        and r.kind is record.kind
        and r.outcome is RunOutcome.NOT_STARTED
        and r.artifact_seq == record.artifact_seq
        and getattr(r, fingerprint_field) == fingerprint
    ]
    if twins:
        reasons.append(
            f"{what} запись не ссылается на аннулированную запись той же версии с равным "
            f"отпечатком ({', '.join(sorted(twins))}) — запись повтора ОБЯЗАНА ссылаться "
            "на аннулированную, иначе отказ похоронен свежим успехом"
        )
    return reasons


def credit_blind_reading(
    blind: RunRecord,
    canary: RunRecord,
    *,
    all_blind_records: Iterable[RunRecord],
    current_reader_digest: str,
    markers_published_at: datetime | None,
    observed_answer_sha256: str | None,
    read_transcript: TranscriptReader | None = None,
) -> CreditVerdict:
    """Is this blind reading credited as the current version's assessment?

    The channel-derived facts are arguments, not lookups: ``markers_published_at`` is when
    the marker list appeared in the channel, and ``observed_answer_sha256`` is the hash of
    the canary answer as published there.

    ``markers_published_at`` is compared with the canary's START, not with the answer
    message. Both the marker list and the answer are published by development AFTER the run
    returned, so their order in the channel says nothing about when the criterion was
    chosen; the only moment the list must precede is the launch. The limit of that check is
    stated rather than hidden: the launch happens outside the channel, so the comparison
    rests on the start time development recorded. Under the declared threat model that is
    enough — it catches "forgot to declare the list before launching", which is the failure
    that actually happens here.
    """
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    require(blind.kind is RunKind.BLIND, f"запись чтения не слепого вида: {blind.kind}")
    require(canary.kind is RunKind.CANARY, f"связанная запись не канареечная: {canary.kind}")

    for record, what in ((blind, "слепая"), (canary, "канареечная")):
        missing = record.missing_fields()
        require(not missing, f"{what} запись неполна, нет полей: {', '.join(missing)}")
        require(
            not record.tool_calls,
            f"{what} запись: прогон выполнял команды ({len(record.tool_calls)}), "
            "изоляция нарушена",
        )
        reasons += transcript_reasons(record, what, read_transcript)

    # THE INSTRUMENT MUST BE THE SAME ONE IN BOTH RUNS. The canary measures the channel for
    # the reader that is about to read; measured on one runner or model version and read on
    # another, its verdict is about a different instrument. The configured model name is
    # already inside the profile fingerprint; the runner binary and the model's version are
    # not, and this is where they are bound.
    # THE PROVIDER'S OWN STATEMENT ABOUT THE LAUNCH, checked here rather than trusted
    # through the outcome field. What the isolation claim rests on is this, not the derived
    # tool-call list: that list proves presence and never absence, because an unrecognised
    # execution shape yields an empty list indistinguishable from a clean run.
    for record, what in ((blind, "слепая"), (canary, "канареечная")):
        for name, expected in (("sandbox_mode", "read-only"), ("approval_mode", "never")):
            actual = getattr(record, name)
            require(
                actual == expected,
                f"{what} запись: {name} = {actual!r}, а изоляция требует {expected!r} — "
                "это слова самого провайдера о запуске, и на них держится заявление",
            )

    require(
        blind.runner_version == canary.runner_version,
        f"канарейка и чтение шли на разных версиях запускающего "
        f"({canary.runner_version} против {blind.runner_version}) — "
        "вердикт канарейки относится к другому инструменту",
    )
    require(
        blind.model_version == canary.model_version,
        f"канарейка и чтение шли на разных версиях модели "
        f"({canary.model_version} против {blind.model_version})",
    )

    require(
        blind.outcome is RunOutcome.HAPPENED,
        f"исход слепого прогона — {blind.outcome}, а не «{RunOutcome.HAPPENED}»",
    )
    require(canary.canary_verdict_clean is True, "вердикт канарейки не чист")
    require(
        blind.canary_record_id == canary.id,
        "слепая запись ссылается не на эту канареечную "
        f"({blind.canary_record_id} против {canary.id})",
    )

    # THE UNIVERSE IS REQUIRED, with no default. "One canary serves exactly one reading" is
    # checked by walking the channel's references, so an empty list is not "no rivals" — it is
    # "the walk was not done", and the two are indistinguishable in the verdict. Left
    # defaulted, a caller who simply forgot the argument got `credited=True` on a pair the
    # full list refuses. Fail-closed: the caller must at minimum hand over the reading itself.
    universe = list(all_blind_records)
    require(
        any(other.id == blind.id for other in universe),
        "перечень слепых записей не содержит проверяемой — уникальность канарейки "
        "проверяется перебором ссылок, и пустой перебор не означает «соперников нет»",
    )
    rival = [
        other.id
        for other in universe
        if other.id != blind.id and other.canary_record_id == canary.id
    ]
    require(
        not rival,
        "на эту канарейку уже ссылается другое чтение "
        f"({', '.join(rival)}) — одна канарейка обслуживает ровно одно чтение",
    )

    # THE RETRY NAMES ITS ANNULMENT, OR IT IS NOT A RETRY. A run refused by schema
    # validation is annulled into a record with the outcome «не состоялся» (one state, two
    # names), and the one automatic retry must LINK to it: the link is what keeps the
    # annulment and its retry one legible story instead of a failure quietly buried by a
    # fresh success. Both directions are checked over the same universe the rival walk
    # uses: a link must resolve to an annulled record of this version with an EQUAL reader
    # fingerprint (a retry is lawful only while the inputs stand still), and a reading
    # launched beside an unnamed annulled twin of the same version and fingerprint is a
    # retry pretending to be a first attempt.
    reasons += retry_link_reasons(blind, universe, what="слепая")

    require(
        blind.profile_digest == canary.profile_digest,
        "отпечатки профиля у канарейки и чтения различны — между ними менялись настройки "
        "или учётная запись",
    )
    require(
        blind.artifact_seq == canary.artifact_seq,
        f"записи относятся к разным версиям артефакта ({canary.artifact_seq} и "
        f"{blind.artifact_seq})",
    )
    require(
        blind.launch_number is not None
        and canary.launch_number is not None
        and blind.launch_number == canary.launch_number + 1,
        f"номера запусков не соседние ({canary.launch_number} и {blind.launch_number}) — "
        "между канарейкой и чтением был ещё один прогон",
    )
    require(
        canary.finished_at < blind.started_at,
        "канарейка завершилась не раньше начала чтения",
    )
    # STRICTLY before, not "no later than": at equal timestamps the order of the two events
    # is unproven, and an unproven order is exactly what this check exists to refuse.
    require(
        markers_published_at is not None and markers_published_at < canary.started_at,
        "список маркеров не опубликован строго до запуска канарейки — критерий мог быть "
        "выбран после того, как ответ стал известен",
    )
    require(
        observed_answer_sha256 is not None
        and observed_answer_sha256 == canary.answer_sha256,
        "вердикт канарейки относится не к тому ответу, что опубликован в канале",
    )
    require(
        blind.reader_digest == current_reader_digest,
        "читательский отпечаток не совпадает с текущей версией — прочитано что-то другое",
    )

    return CreditVerdict(credited=not reasons, reasons=reasons)


def runner_transfer_reasons(
    previous: RunRecord, live_runner_version: str | None
) -> list[str]:
    """Why the LIVE environment forbids transferring this record's result (empty = it
    does not) — ONE copy for all three transferable roles.

    Operator decision (review b5fd70de, round 1): fingerprints answer "did the role's
    inputs change" and deliberately exclude the runner; the runner is ENVIRONMENT, and
    environment is probed. A transfer is lawful only while the live-probed runner version
    equals the version recorded by the source run — an in-place CLI update moves no
    fingerprint, so without this probe it would silently carry a result measured through
    an instrument that no longer exists. Fail-closed: an unprobed environment
    (``None``) refuses the transfer rather than assuming stillness; the channel-side
    convergence gate cannot probe a live binary, so this condition is development's,
    like the transcript re-reads.
    """
    if live_runner_version is None:
        return [
            "живая версия запускающего не установлена (проба не выполнялась или не "
            "прошла) — перенос требует пробы среды, непробованная среда не считается "
            "неизменной"
        ]
    if previous.runner_version != live_runner_version:
        return [
            f"версия запускающего изменилась ({previous.runner_version} в исходной "
            f"записи против {live_runner_version} живой) — перенос незаконен, результат "
            "перемеряется свежим прогоном"
        ]
    return []


def carried_forward(
    previous: RunRecord,
    *,
    iteration: int,
    artifact_seq: int,
    current_reader_digest: str,
    live_runner_version: str | None = None,
) -> CreditVerdict:
    """May a previous blind result stand as this round's assessment without a new run?

    Legitimate exactly when the reader fingerprint is unchanged — that is what "nothing the
    blind reader sees has moved" means — AND the live runner version equals the source
    record's (see ``runner_transfer_reasons``; the default ``None`` fails closed). The
    carried record keeps its OWN round and version; it is the phase that carries the
    current ones, which is why this returns a verdict about the carry rather than
    rewriting the record.
    """
    reasons: list[str] = []
    if previous.kind is not RunKind.BLIND:
        reasons.append(f"переносится запись не слепого вида: {previous.kind}")
    if previous.outcome is not RunOutcome.HAPPENED:
        reasons.append(f"переносится незасчитанный прогон (исход {previous.outcome})")
    if previous.reader_digest != current_reader_digest:
        reasons.append(
            "читательский отпечаток изменился — перенос незаконен, нужен новый прогон"
        )
    reasons += runner_transfer_reasons(previous, live_runner_version)
    if artifact_seq <= previous.artifact_seq:
        reasons.append(
            f"перенос вперёд невозможен: исходная версия {previous.artifact_seq}, "
            f"текущая {artifact_seq}"
        )
    if iteration <= previous.iteration:
        reasons.append(
            f"перенос вперёд невозможен: исходный круг {previous.iteration}, "
            f"текущий {iteration}"
        )
    return CreditVerdict(credited=not reasons, reasons=reasons)


def credit_role_run(
    record: RunRecord,
    *,
    all_records: Iterable[RunRecord],
    current_role_digest: str,
) -> CreditVerdict:
    """Is this strategic/machine run credited as the current version's pass?

    Deliberately smaller than the blind gate — the roles claim no knowledge isolation, so
    there is no canary, no adjacency and no provider attestation to check. What remains is
    exactly what the structured-answer contract demands: a complete record (render fields
    included — they are required of a HAPPENED record by schema), the outcome «состоялся»,
    the ROLE fingerprint equal to the current one, and the retry rule over the same
    universe the phase's readers see.
    """
    reasons: list[str] = []
    if record.kind not in (RunKind.STRATEGIC, RunKind.MACHINE):
        reasons.append(f"запись не стратегического и не машинного вида: {record.kind}")
    missing = record.missing_fields()
    if missing:
        reasons.append(f"запись неполна, нет полей: {', '.join(missing)}")
    if record.outcome is not RunOutcome.HAPPENED:
        reasons.append(
            f"исход прогона — «{record.outcome}», а не «{RunOutcome.HAPPENED}» — "
            "аннулированный прогон не результат"
        )
    if record.role_digest != current_role_digest:
        reasons.append(
            "отпечаток роли не совпадает с текущим — прогон читал другие входы"
        )
    universe = list(all_records)
    if not any(other.id == record.id for other in universe):
        reasons.append(
            "перечень записей не содержит проверяемой — правило повтора проверяется "
            "перебором, и пустой перебор не означает «повторов нет»"
        )
    reasons += retry_link_reasons(
        record, universe, what=str(record.kind.value), fingerprint_field="role_digest"
    )
    return CreditVerdict(credited=not reasons, reasons=reasons)


def carried_role_forward(
    previous: RunRecord,
    *,
    kind: RunKind,
    iteration: int,
    artifact_seq: int,
    current_role_digest: str,
    live_runner_version: str | None = None,
) -> CreditVerdict:
    """May a previous strategic/machine result stand for this round without a new run?

    The same shape as the blind carry: lawful exactly when the ROLE fingerprint is
    unchanged — and for the strategic role the fingerprint covers the full contract, so a
    changed private part invalidates the transfer by construction (the stated difference
    between the roles) — AND the live runner version equals the source record's (see
    ``runner_transfer_reasons``; the default ``None`` fails closed).
    """
    reasons: list[str] = []
    if previous.kind is not kind:
        reasons.append(f"переносится запись не того вида: {previous.kind} (нужен {kind})")
    if previous.outcome is not RunOutcome.HAPPENED:
        reasons.append(f"переносится незасчитанный прогон (исход {previous.outcome})")
    if previous.role_digest != current_role_digest:
        reasons.append("отпечаток роли изменился — перенос незаконен, нужен новый прогон")
    reasons += runner_transfer_reasons(previous, live_runner_version)
    if artifact_seq <= previous.artifact_seq:
        reasons.append(
            f"перенос вперёд невозможен: исходная версия {previous.artifact_seq}, "
            f"текущая {artifact_seq}"
        )
    if iteration <= previous.iteration:
        reasons.append(
            f"перенос вперёд невозможен: исходный круг {previous.iteration}, "
            f"текущий {iteration}"
        )
    return CreditVerdict(credited=not reasons, reasons=reasons)


def unique_canary_violations(records: Sequence[RunRecord]) -> dict[str, list[str]]:
    """Canary ids referenced by more than one blind record — the rule, checked in bulk.

    Kept separate from the pair check because it is a property of the whole set: the pair
    check can only see the rivals it is handed, and a caller holding every record should be
    able to ask the question directly rather than by looping.
    """
    by_canary: dict[str, list[str]] = {}
    for record in records:
        if record.kind is RunKind.BLIND and record.canary_record_id:
            by_canary.setdefault(record.canary_record_id, []).append(record.id)
    return {canary: blinds for canary, blinds in by_canary.items() if len(blinds) > 1}
