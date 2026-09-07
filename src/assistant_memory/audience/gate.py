# SPDX-License-Identifier: Apache-2.0
"""The genre's convergence conditions, read off the channel.

WHY HERE AND NOT ONLY IN THE PLANNER. The planner already computes this gate — from objects,
for development, which holds them. Nobody else does. The party that DECLARES convergence is
the critic, and its declaration was checked against the server's own conditions and nothing
of this genre's: a review could converge with the blind reading missing, the claim map full
of unconfirmed rows and the operator's declarations never made, and every layer would report
itself satisfied. A rule that only the party with the least reason to enforce it can check is
not enforced.

So the conditions are ALSO computable from what the channel shows, by a side that holds no
local files at all. That is what this module does.

WHAT "COMPUTABLE FROM THE CHANNEL" NOW MEANS. The run records travel WHOLE — outcome,
canary verdict, launch numbers, profile and reader fingerprints, the provider's own
sandbox/approval words. A gate that only asked "does a blind record exist" was trusting the
poster for every one of those, and a hand-posted phase could open convergence over an
annulled run, a dirty canary or a reading of some other version. So this gate re-runs, from
the published records alone, every creditability condition the channel can carry: record
completeness, the pair binding to the canary, adjacency of launch numbers, one canary
serving exactly one reading, the marker list preceding the canary's start, and the reader
fingerprint the assessment claims — fresh must match its record, a carry must match the
record it carries from, because an unmoved fingerprint IS the definition of a lawful carry.

THE LIMIT, STATED RATHER THAN DISCOVERED. Three things stay development's side and are not
claimed here: re-reading the transcripts (the critic side holds no files, so record fields
that were re-derived from transcripts are taken as published), re-deriving the CURRENT
version's reader fingerprint (that needs the contract, the template and the artefact — two
of which the critic side must not hold; the fingerprint a phase claims is development's
recorded claim, checkable later, not re-derivable here), and the LIVE runner-version probe
that gates every transfer (probing a binary is running the environment, which this side by
definition does not do — the probe's verdict is enforced where the transfer decision is
made, in the planner and the driver). Attestations and roles are
prompt-maintained, as everywhere in this loop. Development's gate re-derives; this one
checks everything the wire carries — and the wire now carries enough that "present" and
"creditable-as-published" are the same question.

AND THE BOUNDARY OF WHAT ALL OF THIS DEFENDS AGAINST, ruled by the operator (2026-08-05,
the third fixing of one frame): these checks catch ACCIDENT AND DRIFT — an honest bug, a
half-written publication, two views of the state coming apart. They do not defend against
the posting side deliberately forging its own messages, and they are not required to: the
only parties able to post are development and the critic, on the operator's own machines,
and a malicious insider is outside the declared threat model. Do not read consistency
between self-published values as proof against forgery, and do not harden it into one —
the honest answer to "this could be forged consistently" is this paragraph, not another
mechanism.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import datetime

from assistant_memory.audience import channel
from assistant_memory.audience.blind_report import findings_signature, items_refusals
from assistant_memory.audience.claim_map import MAY_SET, TERMINAL, ClaimStatus
from assistant_memory.audience.launcher import canary_verdict
from assistant_memory.audience.machine_comb import (
    phase_items_refusals as machine_items_refusals,
)
from assistant_memory.audience.records import (
    RunKind,
    RunOutcome,
    RunRecord,
    retry_link_reasons,
)
from assistant_memory.audience.strategic import (
    phase_items_refusals as strategic_items_refusals,
)
from assistant_memory.audience.structured import findings_signature_json
from assistant_memory.review.genres import MACHINE_COMB_KEY, STRATEGIC_READER_KEY


def _phase(phases: Sequence[channel.ChannelPhase], name: str, artifact_seq: int):
    """The LAST message of this phase for this version — a phase may legitimately be re-posted."""
    found = [
        p for p in phases if p.phase == name and p.payload.get("artifact_seq") == artifact_seq
    ]
    return found[-1] if found else None


def _pair_refusals(
    blind: RunRecord,
    records: Mapping[str, RunRecord],
    phases: Sequence[channel.ChannelPhase],
    created_at: Mapping[int, datetime],
    record_seqs: Mapping[str, int],
) -> list[str]:
    """Every way this published pair is not creditable AS PUBLISHED (empty = it is).

    Mirrors the channel-computable subset of ``credit_blind_reading``: what that check
    re-derives from transcripts is out of reach here and is named in the module note; all
    the rest rides in the records and is therefore checked rather than believed.
    """
    reasons: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    missing = blind.missing_fields()
    require(not missing, f"слепая запись неполна, нет полей: {', '.join(missing)}")
    require(
        blind.outcome is RunOutcome.HAPPENED,
        f"исход слепого прогона — «{blind.outcome}», а не «{RunOutcome.HAPPENED}» — "
        "аннулированное чтение не оценка",
    )
    require(
        not blind.tool_calls,
        f"слепая запись: прогон выполнял команды ({len(blind.tool_calls)}) — изоляция нарушена",
    )

    canary = records.get(str(blind.canary_record_id or ""))
    if canary is None:
        reasons.append(
            f"канарейка {blind.canary_record_id!r} слепой записи не опубликована в канале — "
            "пара без канарейки не пара"
        )
        return reasons
    require(
        canary.kind is RunKind.CANARY,
        f"связанная запись {canary.id} не канареечного вида: {canary.kind}",
    )
    missing = canary.missing_fields()
    require(not missing, f"канареечная запись неполна, нет полей: {', '.join(missing)}")
    require(
        canary.outcome is RunOutcome.HAPPENED,
        f"исход канарейки — «{canary.outcome}», а не «{RunOutcome.HAPPENED}»",
    )
    require(canary.canary_verdict_clean is True, "вердикт канарейки не чист")
    require(
        not canary.tool_calls,
        f"канареечная запись: прогон выполнял команды ({len(canary.tool_calls)})",
    )

    # The provider's own words about both launches — what the isolation claim rests on.
    for record, what in ((blind, "слепая"), (canary, "канареечная")):
        for name, expected in (("sandbox_mode", "read-only"), ("approval_mode", "never")):
            actual = getattr(record, name)
            require(
                actual == expected,
                f"{what} запись: {name} = {actual!r}, а изоляция требует {expected!r}",
            )

    # The instrument must be the same one in both runs, and the arrangement the same one.
    for name, label in (
        ("profile_digest", "отпечатки профиля"),
        ("runner_version", "версии запускающего"),
        ("model", "модели"),
        ("model_version", "версии модели"),
    ):
        require(
            getattr(blind, name) == getattr(canary, name),
            f"{label} канарейки и чтения расходятся "
            f"({getattr(canary, name)!r} против {getattr(blind, name)!r})",
        )

    require(
        canary.artifact_seq == blind.artifact_seq,
        f"канарейка и чтение записаны на разные версии артефакта "
        f"({canary.artifact_seq} и {blind.artifact_seq}) — измерение чужой версии",
    )
    # Adjacent numbers: an unrecorded run between the two moves the counter and breaks this.
    require(
        canary.launch_number is not None
        and blind.launch_number is not None
        and canary.launch_number + 1 == blind.launch_number,
        f"номера запусков не соседние ({canary.launch_number} и {blind.launch_number}) — "
        "между канарейкой и чтением что-то запускалось",
    )
    require(
        canary.finished_at < blind.started_at,
        "канарейка закончилась не строго раньше старта чтения — измерение шло не перед "
        "чтением",
    )

    # One canary serves exactly one reading — walked over the PUBLISHED records.
    rivals = [
        r.id
        for r in records.values()
        if r.kind is RunKind.BLIND and r.canary_record_id == canary.id and r.id != blind.id
    ]
    require(
        not rivals,
        f"на канарейку {canary.id} ссылается ещё и {', '.join(rivals) or '—'} — "
        "чистота канарейки зачлась бы двум чтениям разом",
    )

    # The retry rule of the structured-answer contract, checked over the PUBLISHED records:
    # one copy of the rule (the records module's), because a channel-side twin of it would
    # drift exactly the way two copies of one list drift.
    reasons += retry_link_reasons(blind, list(records.values()), what="слепая")

    # The marker list must exist in the channel AND precede the canary's start: a criterion
    # chosen once the answer is known proves nothing about the answer.
    markers_seq = canary.markers_message_seq
    marker_phases = [
        p
        for p in phases
        if p.phase == channel.CANARY_MARKERS_PHASE and p.seq == markers_seq
    ]
    if not marker_phases:
        reasons.append(
            f"запись канарейки называет список маркеров сообщением {markers_seq!r}, "
            "которого нет в канале среди фаз списка маркеров"
        )
    else:
        published = created_at.get(int(markers_seq)) if markers_seq is not None else None
        # STRICTLY earlier: at equal timestamps the order is unproven, and an unproven
        # order is what this condition exists to refuse.
        require(
            published is not None and published < canary.started_at,
            "список маркеров опубликован не строго раньше старта канарейки — критерий, "
            "выбранный после ответа, ничего про ответ не доказывает",
        )

    canary_seq = record_seqs.get(canary.id)
    blind_seq = record_seqs.get(blind.id)
    require(
        canary_seq is not None and blind_seq is not None and canary_seq < blind_seq,
        "запись канарейки опубликована не раньше слепой записи",
    )
    # THE VERDICT IS BOUND TO A PUBLISHED ANSWER, not to the record's word about itself. The
    # answer rides the channel as its own phase; the record carries its hash and the message
    # number. So three things are checkable by a side with no files: the reference resolves
    # to a canary-answer phase of this pair's version, the hash of the published body is the
    # hash the record swears by, and a "clean" verdict survives a re-scan of that body
    # against the published markers. What stays development's half is only the second
    # criterion of the verdict — reading the answer whole — and the transcripts.
    answer_phase = next(
        (
            p
            for p in phases
            if p.phase == channel.CANARY_PHASE and p.seq == canary.answer_message_seq
        ),
        None,
    )
    if answer_phase is None:
        reasons.append(
            f"запись канарейки ссылается на сообщение ответа {canary.answer_message_seq!r}, "
            "но фазы ответа канарейки с этим номером в канале нет"
        )
    else:
        require(
            answer_phase.payload.get("artifact_seq") == canary.artifact_seq,
            "фаза ответа канарейки относится к версии "
            f"{answer_phase.payload.get('artifact_seq')!r}, а запись — к {canary.artifact_seq}",
        )
        answer = str(answer_phase.payload.get("answer") or "")
        digest = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        require(
            digest == canary.answer_sha256,
            "хэш опубликованного ответа канарейки не совпадает с хэшем в записи — "
            "вердикт относится к другому ответу",
        )
        if canary.canary_verdict_clean is True and marker_phases:
            markers = [str(m) for m in marker_phases[0].payload.get("markers") or []]
            clean, hit = canary_verdict(answer, markers)
            require(
                clean,
                "опубликованный ответ канарейки задевает опубликованные маркеры "
                f"({', '.join(hit)}), а вердикт записан чистым",
            )
    return reasons


def _phase_integrity_refusals(phases: Sequence[channel.ChannelPhase]) -> list[str]:
    """A LATER publication may never erase an earlier one's adverse evidence.

    The last-phase rule (`_phase` takes the newest) is legitimate only while re-posting
    cannot change what the gate would have seen. So each convergence phase gets the
    integrity rule its own nature dictates: a reading's findings are FIXED by its
    transcript, so two phases naming one record must agree; the operator's declarations
    are immutable within their round, so a differing re-post is a contradiction, not a
    correction; the claim map legitimately EVOLVES within a round — statuses move as the
    informed reader works — but a row, once published, never disappears (its number is
    permanent and a withdrawn row stays, in the withdrawn state).
    """
    reasons: list[str] = []

    by_record: dict[str, tuple] = {}
    for p in phases:
        if p.phase != channel.BLIND_FINDINGS_PHASE:
            continue
        key = str(p.payload.get("run_record_id"))
        # The signature is what the RECORD fixes — the findings IN FULL (id alone would let
        # every field outside the identity material, the contract anchor above all, be
        # rebound under a disposed id) and the fingerprint they were read under. The carry
        # marks are NOT in it: a lawful carry re-publishes the same record's findings with
        # the marks added, and that is a re-statement, not a contradiction.
        signature = (
            findings_signature(p.payload),
            str(p.payload.get("reader_digest") or ""),
            tuple(sorted(str(i) for i in p.payload.get("contract_public_items") or ())),
        )
        if key in by_record and by_record[key] != signature:
            reasons.append(
                f"две фазы слепой оценки записи {key} противоречат друг другу — находки "
                "завершённого прогона зафиксированы его стенограммой, и поздняя публикация "
                "не вправе стереть раннюю"
            )
        by_record.setdefault(key, signature)

    # The role phases obey the same fixture rule as the blind one: a reading's findings are
    # fixed by its transcript, so two phases naming one record must agree — compared by
    # full canonical content plus the fingerprint they stand for.
    for role_phase in (channel.STRATEGIC_FINDINGS_PHASE, channel.MACHINE_FINDINGS_PHASE):
        by_role_record: dict[str, tuple] = {}
        for p in phases:
            if p.phase != role_phase:
                continue
            key = str(p.payload.get("run_record_id"))
            signature = (
                findings_signature_json(p.payload),
                str(p.payload.get("role_digest") or ""),
            )
            if key in by_role_record and by_role_record[key] != signature:
                reasons.append(
                    f"две фазы «{role_phase}» записи {key} противоречат друг другу — "
                    "находки завершённого прогона зафиксированы его стенограммой, и "
                    "поздняя публикация не вправе стереть раннюю"
                )
            by_role_record.setdefault(key, signature)

    takeaways: dict[tuple, Mapping] = {}
    for p in phases:
        if p.phase != channel.INTENT_TAKEAWAY_PHASE:
            continue
        key = (p.payload.get("artifact_seq"), p.payload.get("iteration"))
        if key in takeaways and dict(takeaways[key]) != dict(p.payload):
            reasons.append(
                f"объявления оператора за круг {key[1]!r} версии {key[0]!r} опубликованы "
                "дважды с разным содержимым — объявление неизменяемо в пределах круга, "
                "и поздний повтор не вправе перевернуть ранний сигнал"
            )
        takeaways.setdefault(key, p.payload)

    seen_rows: dict[tuple, set[str]] = {}
    for p in phases:
        if p.phase != channel.CLAIM_MAP_PHASE:
            continue
        key = (p.payload.get("artifact_seq"), p.payload.get("iteration"))
        ids = {
            str(row.get("id"))
            for row in p.payload.get("rows") or []
            if isinstance(row, Mapping)
        }
        vanished = seen_rows.get(key, set()) - ids
        if vanished:
            reasons.append(
                f"поздняя публикация карты за круг {key[1]!r} версии {key[0]!r} потеряла "
                f"строки {', '.join(sorted(vanished))} — статус строки может двигаться, "
                "сама строка не исчезает"
            )
        seen_rows[key] = seen_rows.get(key, set()) | ids

    return reasons


def _claim_row_refusals(row: Mapping) -> list[str]:
    """Ways this PUBLISHED row cannot carry the status it claims (empty = it can).

    The acting status is recomputed by the module that owns the automaton before
    publication — but the gate reads the wire, and the wire can be written by hand. What the
    wire itself carries is re-checked here: the quote hash against the quote, the triple
    against its members, the signer against who MAY set that status, the passport behind a
    simplification. The attestation's authenticity stays prompt-maintained, as stated in the
    module note — but a row whose published fields disagree with each other, or whose signer
    is not entitled to its status, is refused rather than believed.
    """
    reasons: list[str] = []
    row_id = row.get("id", "?")
    try:
        status = ClaimStatus(row.get("status", ClaimStatus.UNCONFIRMED))
    except ValueError:
        return [f"{row_id}: неизвестный статус строки {row.get('status')!r}"]
    if status not in TERMINAL:
        return []

    quote_sha = hashlib.sha256(
        " ".join(str(row.get("quote", "")).split()).encode("utf-8")
    ).hexdigest()
    if row.get("quote_sha256") != quote_sha:
        reasons.append(
            f"{row_id}: опубликованный хэш цитаты не сходится с самой цитатой — "
            "подтверждение относилось бы к другому тексту"
        )
    form = row.get("form") or "—"
    step = row.get("rounding_step") or "—"
    triple = hashlib.sha256(f"{row_id}|{quote_sha}|{form}|{step}".encode()).hexdigest()
    if row.get("triple") != triple:
        reasons.append(
            f"{row_id}: тройка строки не сходится с её же полями — цитата, форма или шаг "
            "менялись после подтверждения"
        )
    entitled = MAY_SET[status].value
    if row.get("status_by") != entitled:
        reasons.append(
            f"{row_id}: статус «{status}» подписан «{row.get('status_by') or '—'}», "
            f"а вправе только «{entitled}»"
        )
    if not str(row.get("status_basis") or "").strip():
        reasons.append(f"{row_id}: терминальный статус без основания подписи")
    if status is ClaimStatus.SIMPLIFIED:
        passport = row.get("passport") or {}
        for field in ("original_fact", "withheld", "permitted_by"):
            if not str(passport.get(field) or "").strip():
                reasons.append(
                    f"{row_id}: «{ClaimStatus.SIMPLIFIED}» без поля паспорта «{field}» — "
                    "паспорт часть предложения, а не украшение"
                )
    return reasons


def _role_phase_refusals(
    phases: Sequence[channel.ChannelPhase],
    records: Mapping[str, RunRecord],
    *,
    artifact_seq: int,
    kind: RunKind,
    phase_name: str,
    role_label: str,
    dispositions: Sequence[Mapping],
) -> list[str]:
    """The enabled role's dispositional condition — the fourth/fifth condition of AR-23,
    built exactly like the third (the blind one), transfer form included.

    What is checkable from the wire is checked rather than believed: the phase's record
    resolves and is creditable AS PUBLISHED (complete, «состоялся», the render pair, the
    retry rule), the fingerprint the phase stands for equals its record's, a carry binds to
    the cycle on both ends and restates its source phase verbatim, every item validates
    against the record's own instrument identity, every strategic anchor resolves into the
    declared universe, and every finding of the current version is terminally disposed.
    The current role fingerprint is NOT re-derivable here (it needs the contract and the
    template) — the phase's claim is development's recorded claim, checkable later, the
    same stated limit as the blind reader's.
    """
    reasons: list[str] = []
    phase = _phase(phases, phase_name, artifact_seq)
    if phase is None:
        return [
            f"роль «{role_label}» включена, а фазы «{phase_name}» текущей версии нет: "
            "ни свежего прогона, ни объявленного переноса — сходимость объявлять не на чем"
        ]
    named = str(phase.payload.get("run_record_id"))
    claimed_digest = str(phase.payload.get("role_digest") or "")
    source = records.get(named)
    if source is None:
        return [
            f"фаза «{phase_name}» ссылается на запись прогона {named!r}, которой нет в "
            "канале — сослаться можно только на опубликованную улику"
        ]
    if source.kind is not kind:
        reasons.append(
            f"фаза «{phase_name}» ссылается на запись вида «{source.kind}», а не "
            f"«{kind.value}»"
        )
        return reasons

    carried = (
        phase.payload.get("carried_from_artifact_seq") is not None
        or phase.payload.get("carried_from_iteration") is not None
    )
    phase_iteration = phase.payload.get("iteration")
    if not carried and source.artifact_seq != artifact_seq:
        reasons.append(
            f"прогон роли «{role_label}» относится к версии {source.artifact_seq}, а круг "
            f"идёт по {artifact_seq}, и перенос не объявлен"
        )
    if not carried and source.iteration != phase_iteration:
        reasons.append(
            f"свежая фаза «{phase_name}» объявлена за круг {phase_iteration!r}, а её "
            f"запись читалась на круге {source.iteration} — перенос не объявлен"
        )
    if carried:
        declared_iter = phase.payload.get("carried_from_iteration")
        declared_seq = phase.payload.get("carried_from_artifact_seq")
        if declared_iter is None or declared_seq is None:
            reasons.append(
                f"перенос фазы «{phase_name}» объявлен наполовину: нужны и исходная "
                "версия, и исходный круг"
            )
        else:
            if declared_seq != source.artifact_seq:
                reasons.append(
                    f"перенос называет версию {declared_seq!r}, а названная запись читалась "
                    f"на версии {source.artifact_seq} — перенос противоречит своей улике"
                )
            if declared_iter != source.iteration:
                reasons.append(
                    f"перенос называет круг-источник {declared_iter!r}, а запись читалась "
                    f"на круге {source.iteration} — перенос противоречит своей улике"
                )
            if not (isinstance(phase_iteration, int) and phase_iteration > declared_iter):
                reasons.append(
                    f"перенос объявлен за круг {phase_iteration!r}, не позже круга-источника "
                    f"{declared_iter!r} — переносить вперёд можно только из прошлого"
                )
        source_phases = [
            p
            for p in phases
            if p.phase == phase_name
            and p.seq < phase.seq
            and p.payload.get("run_record_id") == named
        ]
        if not source_phases:
            reasons.append(
                f"перенос фазы «{phase_name}» не находит в канале исходной фазы для записи "
                f"{named!r} — переносить нечего и сверить перенесённый список не с чем"
            )
        else:
            source_sets = {findings_signature_json(p.payload) for p in source_phases}
            if len(source_sets) > 1:
                reasons.append(
                    f"исходные фазы записи {named!r} противоречат друг другу составом "
                    "находок"
                )
            elif findings_signature_json(phase.payload) != next(iter(source_sets)):
                reasons.append(
                    f"перенесённый список фазы «{phase_name}» не совпадает с исходной "
                    "фазой — это не перенос, а новый список под старой записью"
                )
    if not claimed_digest:
        reasons.append(
            f"фаза «{phase_name}» не несёт отпечатка роли — нечем проверить, что оценка "
            "относится к текущим входам роли"
        )
    elif source.role_digest != claimed_digest:
        reasons.append(
            f"отпечаток роли фазы не совпадает с отпечатком записи, на которую она "
            f"опирается ({claimed_digest[:16]}… против "
            f"{str(source.role_digest)[:16]}…)"
        )

    # Creditability AS PUBLISHED — the record whole, the retry rule over the published set.
    missing = source.missing_fields()
    if missing:
        reasons.append(
            f"запись роли «{role_label}» неполна, нет полей: {', '.join(missing)}"
        )
    if source.outcome is not RunOutcome.HAPPENED:
        reasons.append(
            f"исход прогона роли «{role_label}» — «{source.outcome}», а не "
            f"«{RunOutcome.HAPPENED}» — аннулированный прогон не результат"
        )
    reasons += retry_link_reasons(
        source, list(records.values()), what=str(kind.value), fingerprint_field="role_digest"
    )

    # The items themselves are read, not waved past — against the record's OWN instrument
    # identity (the full-contract hash for the strategic role, the table version for the
    # machine one), so a hand-posted item wearing another instrument refuses.
    raw_items = phase.payload.get("findings") or []
    items = [i for i in raw_items if isinstance(i, Mapping)]
    if len(items) != len(raw_items):
        reasons.append(
            f"элементы фазы «{phase_name}» не являются объектами — то, что нельзя "
            "прочитать, нельзя и диспозировать"
        )
    if kind is RunKind.STRATEGIC:
        reasons += strategic_items_refusals(
            items, contract_sha256=str(source.contract_sha256 or "")
        )
        universe = {str(i) for i in phase.payload.get("contract_items") or ()}
        if not universe:
            reasons.append(
                f"фаза «{phase_name}» не несёт перечня объявленных пунктов контракта — "
                "якорям находок не во что разрешаться"
            )
        else:
            reasons += [
                f"стратегическая находка {item.get('id')} ссылается на пункт "
                f"{item.get('пункт_контракта')!r} вне объявленного перечня"
                for item in items
                if str(item.get("пункт_контракта")) not in universe
            ]
    else:
        reasons += machine_items_refusals(
            items, table_version=str(source.category_set_version or "")
        )

    terminal_ids = {
        str(d.get("finding_id"))
        for d in dispositions
        if d.get("outcome") in ("fixed", "waived")
    }
    for item in items:
        item_id = str(item.get("id"))
        if item_id and item_id not in terminal_ids:
            reasons.append(
                f"находка роли «{role_label}» {item_id} не имеет терминальной "
                "диспозиции — недиспозированная гипотеза это молчание, читаемое как "
                "согласие"
            )
    return reasons


def convergence_refusals(
    phases: Sequence[channel.ChannelPhase],
    *,
    artifact_seq: int,
    created_at: Mapping[int, datetime],
    dispositions: Sequence[Mapping] = (),
    roles: Mapping[str, object] | None = None,
) -> list[str]:
    """Why an audience review may not be declared converged (empty list = the inputs hold).

    Every reason, not the first: whoever has to fix this wants the whole list.
    ``created_at`` maps message seq → channel time; it is required, not optional, because
    the markers-before-canary condition is an ORDERING and a gate that skips an ordering it
    cannot see would be the silence-means-consent shape this genre keeps refusing.
    ``dispositions`` are the review's disposition payloads: the blind findings of the round
    converge only when each is terminally disposed, and that can be checked nowhere else.
    """
    reasons: list[str] = []
    # THE RECORD'S ID IS AN IDENTITY, NOT A KEY TO OVERWRITE. A dict built naively keeps the
    # LAST record per id — so a second, differing record posted under an old id would bury
    # the earlier evidence (an annulled run, a dirty canary) without a trace. A record is
    # immutable: the same id may only ever carry the same bytes. An identical re-post is an
    # honest retry and is tolerated; a differing one is refused, and the review does not
    # proceed on a channel that contradicts itself.
    payloads_by_id: dict[str, dict] = {}
    record_seqs: dict[str, int] = {}
    for p in phases:
        if p.phase != channel.RUN_RECORD_PHASE:
            continue
        payload = dict(p.payload["record"])
        rid = str(payload.get("id"))
        if rid in payloads_by_id and payloads_by_id[rid] != payload:
            reasons.append(
                f"запись прогона {rid} опубликована дважды с разным содержимым — "
                "неизменяемая запись не может отличаться от себя, ранняя улика была бы "
                "молча похоронена поздней"
            )
        payloads_by_id.setdefault(rid, payload)
        record_seqs.setdefault(rid, p.seq)
    records = {
        rid: channel.record_from_payload(payload)
        for rid, payload in payloads_by_id.items()
    }
    reasons += _phase_integrity_refusals(phases)

    blind = _phase(phases, channel.BLIND_FINDINGS_PHASE, artifact_seq)
    if blind is None:
        reasons.append(
            "нет слепой оценки этой версии артефакта: ни свежего чтения, ни объявленного "
            "переноса — сходимость объявлять не на чем"
        )
    else:
        named = blind.payload.get("run_record_id")
        claimed_digest = str(blind.payload.get("reader_digest") or "")
        source = records.get(str(named))
        if source is None:
            reasons.append(
                f"слепые находки ссылаются на запись прогона {named!r}, которой нет в "
                "канале — сослаться можно только на опубликованную улику"
            )
        elif source.kind is not RunKind.BLIND:
            reasons.append(
                f"слепые находки ссылаются на запись вида «{source.kind}», а не на слепую"
            )
        else:
            carried = (
                blind.payload.get("carried_from_artifact_seq") is not None
                or blind.payload.get("carried_from_iteration") is not None
            )
            phase_iteration = blind.payload.get("iteration")
            if not carried and source.artifact_seq != artifact_seq:
                # A fresh reading must be OF this version. A record of an older version
                # without the carry mark is not "an acceptable old one" — it is a missing
                # current one, and the distinction is the whole difference between a saved
                # run and a false one.
                reasons.append(
                    f"слепое чтение относится к версии {source.artifact_seq}, а круг идёт по "
                    f"{artifact_seq}, и перенос не объявлен"
                )
            if not carried and source.iteration != phase_iteration:
                reasons.append(
                    f"свежая слепая оценка объявлена за круг {phase_iteration!r}, а её "
                    f"запись читалась на круге {source.iteration} — прогона в этом круге "
                    "не было, и перенос не объявлен"
                )
            if carried:
                # THE CARRY IS BOUND TO THE CYCLE ON BOTH ENDS, or it is not a carry. The
                # spec's exception is exactly this narrow: the phase itself carries the
                # CURRENT round, names the source round, and the source record must be of
                # that round — a carry missing any of the three is an assessment of some
                # round being passed off as this one's.
                declared_iter = blind.payload.get("carried_from_iteration")
                declared_seq = blind.payload.get("carried_from_artifact_seq")
                if declared_iter is None or declared_seq is None:
                    reasons.append(
                        "перенос объявлен наполовину: нужны и исходная версия, и исходный "
                        "круг — без любого из них связь переноса не проверяется"
                    )
                else:
                    if declared_seq != source.artifact_seq:
                        reasons.append(
                            f"перенос называет версию {declared_seq!r}, а названная запись "
                            f"читалась на версии {source.artifact_seq} — перенос "
                            "противоречит своей улике"
                        )
                    if declared_iter != source.iteration:
                        reasons.append(
                            f"перенос называет круг-источник {declared_iter!r}, а запись "
                            f"читалась на круге {source.iteration} — перенос противоречит "
                            "своей улике"
                        )
                    if not (
                        isinstance(phase_iteration, int) and phase_iteration > declared_iter
                    ):
                        reasons.append(
                            f"перенос объявлен за круг {phase_iteration!r}, не позже "
                            f"круга-источника {declared_iter!r} — переносить вперёд можно "
                            "только из прошлого"
                        )
                # THE CARRIED LIST IS BOUND TO ITS SOURCE PHASE, not restated. A carry
                # whose findings differ from what the source reading published is not a
                # carry — it is a new list wearing an old record; and a carry with no
                # source phase at all has nothing to carry and nothing to check against.
                # Source phases naming one record must agree among themselves: the
                # findings of a completed run are fixed with its transcript, so two
                # contradicting publications are a contradiction, not a correction.
                source_phases = [
                    p
                    for p in phases
                    if p.phase == channel.BLIND_FINDINGS_PHASE
                    and p.seq < blind.seq
                    and p.payload.get("run_record_id") == str(named)
                ]
                if not source_phases:
                    reasons.append(
                        "перенос не находит в канале исходной фазы слепой оценки для "
                        f"записи {named!r} — переносить нечего и сверить перенесённый "
                        "список не с чем"
                    )
                else:
                    source_sets = {findings_signature(p.payload) for p in source_phases}
                    if len(source_sets) > 1:
                        reasons.append(
                            f"исходные фазы слепой оценки записи {named!r} противоречат "
                            "друг другу составом находок — находки завершённого прогона "
                            "зафиксированы его стенограммой и отличаться не могут"
                        )
                    elif findings_signature(blind.payload) != next(iter(source_sets)):
                        reasons.append(
                            "перенесённый список находок не совпадает с исходной фазой — "
                            "это не перенос, а новый список под старой записью (сверяется "
                            "полное каноническое содержимое, не только идентификаторы)"
                        )
            # The fingerprint binding — fresh and carry alike. An unmoved fingerprint IS the
            # definition of a lawful carry, so the phase must SAY which fingerprint it
            # stands for, and that word must match the record it leans on.
            if not claimed_digest:
                reasons.append(
                    "фаза слепой оценки не несёт читательского отпечатка — без него нельзя "
                    "проверить, что оценка (свежая или перенесённая) относится к тому, что "
                    "читатель видит сейчас"
                )
            elif source.reader_digest != claimed_digest:
                reasons.append(
                    "читательский отпечаток фазы не совпадает с отпечатком записи, на "
                    f"которую она опирается ({claimed_digest[:16]}… против "
                    f"{str(source.reader_digest)[:16]}…) — то, что читатель видел тогда, "
                    "не то, за что оценка выдаётся сейчас"
                )
            reasons += _pair_refusals(source, records, phases, created_at, record_seqs)
        # THE ITEMS THEMSELVES ARE READ, NOT WAVED PAST. Convergence counts dispositions,
        # so an item that is malformed, mis-identified, or terminally undisposed blocks —
        # a list the gate never opened would let a live blind finding ride under a
        # converged review. A non-object element is REFUSED, not filtered: filtering is
        # exactly the quiet disappearance this condition exists to stop.
        raw_items = blind.payload.get("findings") or []
        non_objects = [x for x in raw_items if not isinstance(x, Mapping)]
        if non_objects:
            reasons.append(
                f"элементы слепой оценки не являются объектами ({len(non_objects)} шт., "
                f"первый: {str(non_objects[0])[:80]!r}) — то, что нельзя прочитать, "
                "нельзя и диспозировать"
            )
        items = [item for item in raw_items if isinstance(item, Mapping)]
        reasons += items_refusals(items)
        # EVERY ANCHOR RESOLVES INTO THE DECLARED UNIVERSE. The gate holds no contract, so
        # the phase carries the public-item list as its checkable commitment — an anchor of
        # canonical shape that names no declared item is an anchor resolved to nothing, and
        # a finding anchored to nothing is not this contract's finding.
        universe = {str(i) for i in blind.payload.get("contract_public_items") or ()}
        if not universe:
            reasons.append(
                "фаза слепой оценки не несёт перечня публичных пунктов контракта — "
                "якорям находок не во что разрешаться"
            )
        else:
            for item in items:
                if str(item.get("contract_item")) not in universe:
                    reasons.append(
                        f"слепая находка {item.get('id')} ссылается на пункт "
                        f"{item.get('contract_item')!r} вне объявленного перечня публичных "
                        "пунктов — якорь, разрешённый в ничто, не якорь"
                    )
        # THE ITEM'S CONTRACT IS THE READING'S CONTRACT. Every item carries the version and
        # fingerprint of the contract it was read under (they are inside its identity), and
        # the record of the reading carries the same pair — so an item wearing another
        # contract is not this reading's finding. The identity being contract-bound is also
        # what retires stale dispositions structurally: a contract change changes the id,
        # and a disposition given under the old premises simply stops covering.
        if source is not None and source.kind is RunKind.BLIND:
            for item in items:
                if (
                    item.get("contract_version") != source.contract_version
                    or str(item.get("contract_sha256")) != str(source.contract_sha256)
                ):
                    reasons.append(
                        f"слепая находка {item.get('id')} несёт контракт "
                        f"({item.get('contract_version')!r}, "
                        f"{str(item.get('contract_sha256'))[:12]}…), а чтение шло под "
                        f"({source.contract_version!r}, "
                        f"{str(source.contract_sha256)[:12]}…) — находка чужого контракта"
                    )
        terminal_ids = {
            str(d.get("finding_id"))
            for d in dispositions
            if d.get("outcome") in ("fixed", "waived")
        }
        # --- B.8 D-5: a waiver survives a contract version ONLY by explicit carry ---
        #
        # Finding identity keeps `contract_sha256` (deliberately: a contract change
        # changes the premises). The consequence used to be three re-decisions in one
        # round (review f0c0b685) on an edit that touched none of the waivers' premises.
        # The bridge is an EXPLICIT development step — a `waiver_carry` block on a waived
        # disposition — naming the prior waiver, both contract digests and the diff of
        # contract items, asserting the premise is untouched, confirmed by the seeing
        # role (or the operator, when the diff touches the waiver's own item — settled
        # Q-3). Silent auto-carry stays forbidden by construction: no carry message, no
        # coverage. A malformed carry is a refusal, never a silently working one.
        waived_ids = {
            str(d.get("finding_id")) for d in dispositions if d.get("outcome") == "waived"
        }
        for d in dispositions:
            carry = d.get("waiver_carry")
            if carry is None:
                continue
            cid = str(d.get("finding_id"))
            if d.get("outcome") != "waived":
                reasons.append(
                    f"перенос вейвера на находку {cid}: перенос бывает только у вейвера, "
                    f"а диспозиция несёт исход {d.get('outcome')!r}"
                )
                continue
            if not isinstance(carry, Mapping):
                reasons.append(f"перенос вейвера на находку {cid}: блок переноса не объект")
                continue
            for field in (
                "prior_finding_id", "old_contract_sha256", "new_contract_sha256",
                "contract_items_diff",
            ):
                if not str(carry.get(field) or "").strip():
                    reasons.append(
                        f"перенос вейвера на находку {cid}: нет поля «{field}» — перенос "
                        "утверждает нетронутость предпосылки, и утверждение без "
                        "предъявленного диффа пунктов контракта не проверяемо"
                    )
            if carry.get("premise_untouched") is not True:
                reasons.append(
                    f"перенос вейвера на находку {cid}: «premise_untouched» обязан быть "
                    "строгим True — перенос и есть это утверждение"
                )
            if carry.get("confirmed_by") not in ("seeing", "operator"):
                reasons.append(
                    f"перенос вейвера на находку {cid}: «confirmed_by» обязан называть "
                    "подтвердившую сторону (seeing | operator) — молчаливый автоперенос "
                    "запрещён"
                )
            prior = str(carry.get("prior_finding_id") or "")
            if prior and prior not in waived_ids:
                reasons.append(
                    f"перенос вейвера на находку {cid}: прежний вейвер {prior!r} не найден "
                    "среди вейвер-диспозиций — переносить нечего"
                )
            if (
                str(carry.get("old_contract_sha256") or "")
                == str(carry.get("new_contract_sha256") or "")
            ):
                reasons.append(
                    f"перенос вейвера на находку {cid}: контрактные отпечатки совпадают — "
                    "внутри одной версии контракта вейвер и так живёт, переносить нечего"
                )
        # D-5's round-report marking: a RE-FOUND finding — same category, section and
        # normalized text as a WAIVED one from another contract version — is presented as
        # covered-pending-carry, not as fresh: the operator already decided it once.
        def _content_key(item: Mapping) -> tuple:
            return (
                str(item.get("category") or "").strip().lower(),
                str(item.get("section") or "").strip(),
                " ".join(str(item.get("text") or "").split()).lower(),
            )

        prior_waived_content = {
            _content_key(i)
            for ph in phases
            if ph.phase == channel.BLIND_FINDINGS_PHASE and ph is not blind
            for i in ph.payload.get("findings") or []
            if isinstance(i, Mapping) and str(i.get("id")) in waived_ids
        }
        for item in items:
            item_id = str(item.get("id"))
            if item_id and item_id not in terminal_ids:
                if _content_key(item) in prior_waived_content:
                    reasons.append(
                        f"слепая находка {item_id} ({item.get('section')}, "
                        f"{item.get('category')}) — ПОВТОРНАЯ, под живым вейвером прежней "
                        "версии контракта: нужен явный перенос (waiver_carry с диффом "
                        "пунктов и подтверждением зрячего/оператора), не свежий разбор и "
                        "не молчаливое наследование"
                    )
                    continue
                reasons.append(
                    f"слепая находка {item_id} ({item.get('section')}, "
                    f"{item.get('category')}) не имеет терминальной диспозиции — "
                    "сходимость считает диспозиции, а не наличие списка"
                )

    claim_map = _phase(phases, channel.CLAIM_MAP_PHASE, artifact_seq)
    if claim_map is None:
        reasons.append(
            "карта утверждений для этой версии не опубликована — терминальность строк "
            "проверить не по чему"
        )
    else:
        # FAIL-CLOSED ON GARBAGE, NEVER A CRASH: a row that is not an object, or a status
        # outside the automaton, is a refusal with a reason — an exception here would turn
        # the gate's "no" into an outage, which reads as anything but "no". The channel
        # reader already refuses these; the gate guards again because it can be handed
        # phases directly.
        raw_rows = claim_map.payload.get("rows") or []
        non_rows = [r for r in raw_rows if not isinstance(r, Mapping)]
        if non_rows:
            reasons.append(
                f"строки карты не являются объектами ({len(non_rows)} шт., первая: "
                f"{str(non_rows[0])[:80]!r}) — карту, которую нельзя прочитать, нельзя "
                "и проверить"
            )
        rows = [r for r in raw_rows if isinstance(r, Mapping)]

        def _row_status(row: Mapping) -> ClaimStatus | None:
            try:
                return ClaimStatus(row.get("status", ClaimStatus.UNCONFIRMED))
            except ValueError:
                return None

        blocking = [
            f"{row.get('id')} в статусе «{row.get('status')}»"
            for row in rows
            if _row_status(row) not in TERMINAL
        ]
        if blocking:
            reasons.append(
                "строки карты утверждений не в терминальном статусе: " + "; ".join(blocking)
            )
        # The same identity rule as for run records: one id, one row. Two rows under one
        # id — each internally valid — would let a terminal twin stand beside the row it
        # contradicts, and every consumer would pick whichever it read last.
        row_ids = [str(row.get("id")) for row in rows]
        duplicated = sorted({rid for rid in row_ids if row_ids.count(rid) > 1})
        if duplicated:
            reasons.append(
                "идентификаторы строк карты выданы дважды в одной публикации: "
                + ", ".join(duplicated)
            )
        for row in rows:
            reasons += _claim_row_refusals(row)
        # The convergence phases speak for ONE round: a map of one round beside a blind
        # assessment of another is two halves of two different cycles wearing one version.
        map_iteration = claim_map.payload.get("iteration")
        blind_iteration = blind.payload.get("iteration") if blind is not None else None
        if (
            blind is not None
            and map_iteration is not None
            and map_iteration != blind_iteration
        ):
            reasons.append(
                f"карта утверждений объявлена за круг {map_iteration!r}, а слепая оценка — "
                f"за круг {blind_iteration!r}: сходимость собирается из фаз одного круга"
            )

    takeaway = _phase(phases, channel.INTENT_TAKEAWAY_PHASE, artifact_seq)
    if takeaway is None:
        # NOT faked with a machine substitute and NOT defaulted to "fine": these two are the
        # operator's statements about his own intent, no other side has access to them, and
        # absence counts as "not met" precisely so that silence cannot pass for consent.
        reasons.append(
            "оператор не объявил, совпал ли пересказ читателя с желаемым выносом и "
            "двигалась ли цель — эти два утверждения машинного основания не имеют, и "
            "отсутствие считается «не выполнено»"
        )
    else:
        if takeaway.role != "operator":
            # The same prompt-maintained trust level as the rest of the loop — but a
            # development-authored declaration is refused outright: these two words belong
            # to the operator, and the relay posts them under the operator role.
            reasons.append(
                f"объявления о выносе и цели опубликованы ролью «{takeaway.role}», "
                "а делать их вправе только оператор"
            )
        # Second fail-closed line against direct calls: the two decisions are read as
        # TYPES, never as truthiness — "yes" is not a declaration, and neither is "".
        for name in ("retelling_matches", "goal_moved"):
            if not isinstance(takeaway.payload.get(name), bool):
                reasons.append(
                    f"объявление «{name}» не является строгим булевым "
                    f"({takeaway.payload.get(name)!r}) — решение, которого нельзя "
                    "прочитать, не решение"
                )
        if takeaway.payload.get("retelling_matches") is not True:
            reasons.append("оператор объявил, что пересказ читателя с выносом НЕ совпал")
        if takeaway.payload.get("goal_moved") is not False:
            reasons.append("цель двигалась на этом круге — сходимость объявлять рано")
        blind_iteration = blind.payload.get("iteration") if blind is not None else None
        if (
            blind is not None
            and takeaway.payload.get("iteration") != blind_iteration
        ):
            reasons.append(
                f"объявления оператора сделаны за круг {takeaway.payload.get('iteration')!r}, "
                f"а слепая оценка — за круг {blind_iteration!r}: сходимость собирается из "
                "фаз одного круга"
            )
        # THE GOAL-MOTION SIGNAL IS RELATIVE, so "the goal did not move" is checkable only
        # against the declaration it moved (or did not move) FROM. Where an earlier
        # declaration exists on the channel, this one must name it — by version and by the
        # hash of its verbatim quote — and the hash must match. Fail-closed: an unanchored
        # "did not move" beside an existing history is an unproven claim, and unproven
        # reads as "moved".
        # An identical twin is the SAME declaration re-posted, not history to anchor to;
        # a differing same-round twin is already refused by the integrity rule above.
        earlier = [
            p
            for p in phases
            if p.phase == channel.INTENT_TAKEAWAY_PHASE
            and p.seq < takeaway.seq
            and dict(p.payload) != dict(takeaway.payload)
        ]
        if earlier:
            previous = earlier[-1]
            declared_seq = takeaway.payload.get("previous_intent_artifact_seq")
            declared_hash = str(takeaway.payload.get("previous_takeaway_sha256") or "")
            expected_hash = hashlib.sha256(
                str(previous.payload.get("intended_takeaway") or "").encode("utf-8")
            ).hexdigest()
            if declared_seq is None or not declared_hash:
                reasons.append(
                    "в канале есть предыдущее объявление оператора, а текущее не ссылается "
                    "на него (нужны его версия и sha256 дословной цитаты выноса) — "
                    "непривязанное «цель не двигалась» недоказуемо и читается как "
                    "«двигалась»"
                )
            else:
                if declared_seq != previous.payload.get("artifact_seq"):
                    reasons.append(
                        f"сигнал движения цели ссылается на версию {declared_seq!r}, а "
                        "предыдущее объявление в канале сделано на версии "
                        f"{previous.payload.get('artifact_seq')!r}"
                    )
                if declared_hash != expected_hash:
                    reasons.append(
                        "sha256 прежней цитаты выноса не совпадает с опубликованной — "
                        "сигнал движения цели относится к другому тексту"
                    )

    # THE v2 ROLE CONDITIONS — present exactly when the role is ON, and the on/off answer
    # must be an EXPLICIT declaration handed in from the review config (the same device as
    # cold_verdict_first): an absent condition read out of silence is indistinguishable
    # from a forgotten one. Fail-closed on an undeclared roster.
    role_plan = (
        (STRATEGIC_READER_KEY, RunKind.STRATEGIC, channel.STRATEGIC_FINDINGS_PHASE),
        (MACHINE_COMB_KEY, RunKind.MACHINE, channel.MACHINE_FINDINGS_PHASE),
    )
    if roles is None or any(not isinstance(roles.get(key), bool) for key, _, _ in role_plan):
        reasons.append(
            "включённость ролей круга (стратегический читатель, прочёс машинности) не "
            "объявлена гейту строгими булевыми — при выключенной роли её условие "
            "отсутствует, и это записывается явно, а не умалчивается"
        )
    else:
        for key, kind, phase_name in role_plan:
            if roles.get(key) is True:
                reasons += _role_phase_refusals(
                    phases,
                    records,
                    artifact_seq=artifact_seq,
                    kind=kind,
                    phase_name=phase_name,
                    role_label=str(kind.value),
                    dispositions=dispositions,
                )

    return reasons
