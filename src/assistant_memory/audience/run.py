# SPDX-License-Identifier: Apache-2.0
"""Run one round of an audience review, out of the box.

This is what a skill invokes and what the operator can invoke by hand: one command, one
review directory, one round. Everything it needs beyond the directory is the address of the
review on the service, because the STATE IS READ FROM THE CHANNEL rather than remembered —
which runs happened, which pair was credited, which version of the artefact is current. A
driver that trusted a local file would lose its evidence the moment the process did, and the
whole genre exists so that the evidence outlives the run.

WHAT IT PRINTS. Enough for the person who ran it to see what happened without opening the
channel: what refused and why, what the free layer measured, whether the blind pass ran or
was carried, whether the pair was credited and — when it was not — every reason.

EXIT CODES, because a skill has to be able to tell these apart:
  0  the round completed (credited, or legitimately carried)
  1  the round refused before spending anything, or the pair was not credited
  2  the round needs the OPERATOR — a leak screen hit, or an inventory nobody declared
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from assistant_memory.audience import channel
from assistant_memory.audience.driver import AudienceRound, RoundInputs, RoundRefused
from assistant_memory.audience.isolation import (
    ArrangedIsolation,
    BlindRunner,
    IsolationRefused,
    ProfileHygiene,
    probe_runner_version,
)
from assistant_memory.audience.journal import LaunchJournal
from assistant_memory.audience.launcher import BlindLauncher
from assistant_memory.audience.layout import LayoutRefused, load_review
from assistant_memory.audience.planner import BlindAction
from assistant_memory.audience.profile import DEFAULT_CONFIG_GLOBS, LaunchProfile
from assistant_memory.audience.records import RunKind, RunRecord, credit_blind_reading
from assistant_memory.review import resolve as subject_resolve


def _print(*lines: str) -> None:
    for line in lines:
        print(line, flush=True)


def provenance_problems(
    *,
    review_dir: Path,
    artifact_text: str,
    review_id: str,
    artifact_seq: int,
    messages: list[dict],
) -> list[str]:
    """B.10 B-4, the reader half: the runner's provenance preflight, ANCHORED IN THE
    CHANNEL, run before ``artifact_text`` is accepted.

    The run directory is prepared by the development session (an agent duty — no code
    component reads the channel and writes the directory), so the runner fetches the
    artifact message for its own review id and artifact seq and compares: for a
    REFERENTIAL subject, the provenance record's commit and path against the message's
    declared ref and path, plus the local artifact file's digest against the record;
    for an INLINE subject, the local file's digest against the digest of the message's
    inline text. A record and file that live in one directory travel together and
    prove only each other — the realistic failure is accident, not attack (a
    preparation run against a wrong repository state records an honest digest of the
    wrong bytes), and only the channel comparison catches it. A missing, mismatching,
    or channel-contradicted record is a named environmental refusal; the run is not
    counted. The reading path itself and ``load_review`` are unchanged.
    """
    artifact_message = next(
        (
            m
            for m in messages
            if m.get("kind") == "artifact" and m.get("seq") == artifact_seq
        ),
        None,
    )
    if artifact_message is None:
        return [
            f"канал ревью не содержит артефакта с номером {artifact_seq} — "
            "проверить происхождение локального файла не по чему"
        ]
    payload = artifact_message.get("payload") or {}
    local_digest = subject_resolve.text_digest(artifact_text)

    if subject_resolve.is_referential_spec_subject(payload):
        record_path = review_dir / subject_resolve.PROVENANCE_FILENAME
        if not record_path.exists():
            return [
                f"артефакт версии {artifact_seq} — ссылочный, а записи происхождения "
                f"({subject_resolve.PROVENANCE_FILENAME}) в каталоге ревью нет: каталог "
                "готовится CLI-инструментом разрешения, не руками"
            ]
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as problem:
            return [f"запись происхождения не читается: {problem}"]
        # A valid-JSON non-object ([] / null) must take the SAME named refusal as an
        # unreadable record — a crash in the refusal path is the silent-death class
        # the preflight exists to prevent (finding
        # b10-provenance-valid-json-not-object-refusal).
        if not isinstance(record, dict):
            return [
                "запись происхождения — корректный JSON, но не объект "
                f"(тип {type(record).__name__}); каталог готовится CLI-инструментом "
                "разрешения, не руками"
            ]
        declared_ref = payload.get("artifact_ref") or {}
        problems = []
        for field, declared in (
            ("review_id", review_id),
            ("artifact_seq", artifact_seq),
            ("commit", declared_ref.get("commit")),
            ("path", payload.get("path")),
        ):
            if record.get(field) != declared:
                problems.append(
                    f"запись происхождения расходится с каналом: {field} в записи = "
                    f"{record.get(field)!r}, канал объявляет {declared!r}"
                )
        if record.get("digest") != local_digest:
            problems.append(
                "локальный artifact.md не совпадает с записью происхождения по "
                f"отпечатку (файл {local_digest[:16]}…, запись "
                f"{str(record.get('digest'))[:16]}…) — файл подменён или устарел"
            )
        return problems

    inline = (payload.get("bundle") or {}).get("spec_markdown")
    if isinstance(inline, str) and inline.strip():
        if subject_resolve.text_digest(inline) != local_digest:
            return [
                f"локальный artifact.md не совпадает с текстом артефакта версии "
                f"{artifact_seq} на канале (отпечатки {local_digest[:16]}… против "
                f"{subject_resolve.text_digest(inline)[:16]}…) — каталог собран не из "
                "этой версии"
            ]
        return []

    return [
        f"артефакт версии {artifact_seq} на канале не несёт ни инлайн-текста, ни "
        "ссылочного предмета — сверять происхождение не с чем"
    ]


def recredit(records, *, read_transcript, current_reader_digest: str) -> set[str]:
    """Which published blind readings still stand, recomputed rather than remembered.

    Recomputed because "credited" is not a fact about the past that can be stored — it is a
    property of the pair as it is NOW: a transcript that has since gone missing, or a record
    whose fields no longer describe the file they point at, must stop counting. Storing the
    verdict would make it survive its own evidence.
    """
    by_id = {record.id: record for record in records}
    credited: set[str] = set()
    blinds = [r for r in records if r.kind is RunKind.BLIND]
    for blind in blinds:
        canary = by_id.get(blind.canary_record_id or "")
        if canary is None:
            continue
        verdict = credit_blind_reading(
            blind,
            canary,
            all_blind_records=blinds,
            current_reader_digest=blind.reader_digest or "",
            markers_published_at=canary.started_at,
            observed_answer_sha256=canary.answer_sha256,
            read_transcript=read_transcript,
        )
        if verdict.credited:
            credited.add(blind.id)
    return credited


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Один круг аудиторного ревью: механический слой, канарейка, слепое чтение."
    )
    ap.add_argument("--review-dir", required=True, help="каталог с контрактом и артефактом")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--review-id", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--artifact-seq", type=int, required=True,
                    help="версия артефакта, к которой относится этот круг")
    ap.add_argument("--iteration", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    try:
        review = load_review(Path(args.review_dir))
    except LayoutRefused as refusal:
        _print("КРУГ НЕ ЗАПУЩЕН — каталог ревью неполон:", *(f"  · {r}" for r in refusal.reasons))
        return 2

    import httpx  # imported here so the pure modules stay importable without the transport

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{args.base_url.rstrip('/')}/reviews/{args.review_id}"

    def post(route: str, body: dict) -> dict:
        response = httpx.post(f"{base}/{route}", json=body, headers=headers, timeout=args.timeout)
        response.raise_for_status()
        return response.json()

    def fetch(after: int = 0):
        response = httpx.get(
            f"{base}/messages", params={"after": after, "wait": 0},
            headers=headers, timeout=args.timeout,
        )
        response.raise_for_status()
        body = response.json()
        return body.get("messages", body)

    snapshot = httpx.get(base, headers=headers, timeout=args.timeout)
    snapshot.raise_for_status()
    config = snapshot.json().get("config")

    messages = fetch(0)

    # B.10 B-4: the provenance preflight — the channel, not the run directory, is the
    # anchor. A refused preflight costs nothing and counts nothing.
    provenance_faults = provenance_problems(
        review_dir=Path(args.review_dir),
        artifact_text=review.artifact_text,
        review_id=args.review_id,
        artifact_seq=args.artifact_seq,
        messages=messages,
    )
    if provenance_faults:
        _print(
            "КРУГ НЕ ЗАПУЩЕН — происхождение артефакта не подтверждено каналом "
            "(средовой отказ, прогон не засчитан):",
            *(f"  · {r}" for r in provenance_faults),
        )
        return 1

    phases = channel.read_phases(messages)
    published: tuple[RunRecord, ...] = channel.published_records(phases)

    def read_transcript(path: str) -> bytes | None:
        try:
            return Path(path).read_bytes()
        except OSError:
            return None

    credited = recredit(published, read_transcript=read_transcript, current_reader_digest="")

    declarations = review.declarations
    profile = LaunchProfile(
        settings_dir=declarations.machine.settings_dir, model=declarations.model
    )
    isolation = ArrangedIsolation(
        runner=BlindRunner(
            binary=declarations.machine.runner_binary,
            settings_dir=declarations.machine.settings_dir,
            model=declarations.model,
        ),
        work_root=declarations.machine.work_root,
        hygiene=ProfileHygiene(
            settings_dir=declarations.machine.settings_dir, config_globs=DEFAULT_CONFIG_GLOBS
        ),
    )
    launcher = BlindLauncher(
        profile=profile,
        journal=LaunchJournal(review.layout.journal),
        invoke=isolation.invoke,
        transcripts_dir=review.layout.runs_dir,
    )
    round_ = AudienceRound(
        post=post,
        fetch=fetch,
        launcher=launcher,
        isolation=isolation,
        review_config=config,
        transcripts_dir=review.layout.runs_dir,
        previous_records=published,
        credited_ids=tuple(credited),
    )
    inputs = RoundInputs(
        artifact_seq=args.artifact_seq,
        iteration=args.iteration,
        artifact_text=review.artifact_text,
        contract=review.contract,
        template=review.template,
        inventory=declarations.inventory,
        budgets=declarations.budgets,
        markers=declarations.markers,
        model=declarations.model,
        model_version=declarations.model_version,
        thresholds=declarations.thresholds,
        report_budgets=declarations.report_budgets,
        strategic_template=review.strategic_template,
        machine_template=review.machine_template,
        # The genre's own environment probe: transfers are lawful only while the live
        # runner version equals the source record's; a failed probe reads as "unknown
        # environment" and forces fresh runs rather than assuming stillness.
        live_runner_version=probe_runner_version(declarations.machine.runner_binary),
    )

    try:
        outcome = round_.run(inputs)
    except RoundRefused as refusal:
        _print("КРУГ НЕ ЗАПУЩЕН:", *(f"  · {r}" for r in refusal.reasons))
        if refusal.needs_operator:
            _print("Это вопрос к оператору, а не дефект: решение за ним.")
            return 2
        return 1
    except IsolationRefused as refusal:
        _print("ИЗОЛЯЦИЯ НЕ УСТРОЕНА:", *(f"  · {r}" for r in refusal.reasons))
        return 1

    _print(f"Читательский отпечаток: {outcome.reader_digest[:16]}…")
    _print(
        "Живая версия запускающего: "
        + (inputs.live_runner_version or "НЕ УСТАНОВЛЕНА — переносы запрещены")
    )
    if outcome.mech_candidates:
        _print("Механический слой — кандидаты (ничего не блокируют):")
        _print(*(f"  · {c}" for c in outcome.mech_candidates))
    else:
        _print("Механический слой: кандидатов нет.")
    _print(*(f"  · {r}" for r in outcome.reasons))

    def _print_roles() -> None:
        for role, result in outcome.role_results.items():
            if result.carried_from_iteration is not None:
                _print(f"Роль «{role}»: результат перенесён с круга "
                       f"{result.carried_from_iteration}.")
            elif result.credited:
                _print(f"Роль «{role}»: прогон засчитан.")
                if result.record is not None and result.record.render_path:
                    _print(f"  Рендер отчёта для оператора: {result.record.render_path}")
            else:
                _print(f"Роль «{role}» НЕ засчитана:", *(f"  · {r}" for r in result.reasons))

    if outcome.blind_action is BlindAction.CARRY:
        _print(f"Слепая оценка ПЕРЕНЕСЕНА с круга {outcome.carried_from_iteration}.")
        _print_roles()
        return 2 if outcome.needs_operator else 0

    if outcome.credited:
        _print("Пара канарейка/чтение ЗАСЧИТАНА.")
        if outcome.blind_record is not None and outcome.blind_record.render_path:
            _print(f"Рендер отчёта для оператора: {outcome.blind_record.render_path}")
        _print_roles()
        if outcome.needs_operator:
            _print("Одна из ролей упёрлась в вопрос к оператору — см. выше.")
            return 2
        return 0

    if outcome.needs_operator:
        _print(
            "ВТОРОЙ ОТКАЗ ВАЛИДАЦИИ ПОДРЯД — вопрос к оператору, а не расход квоты:",
            *(f"  · {r}" for r in outcome.credit_reasons),
        )
        return 2

    _print("Пара НЕ засчитана:", *(f"  · {r}" for r in outcome.credit_reasons))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
