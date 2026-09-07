# SPDX-License-Identifier: Apache-2.0
"""The single sanctioned launcher: the only path by which a blind run may start.

"Isolation is verified for this reading" is a claim about a specific launch, and it holds
only for launches that went through here — that is what makes the launch numbers adjacent
and the profile fingerprints comparable. A run started around this module moves no counter
and stays invisible; the spec states that as the LIMIT of the guarantee rather than a hole
it pretends to cover, with the rule that follows from it: any reason to suspect such a run
means the result is not credited and the reading is re-run.

The model invocation is INJECTED, exactly as the critic watcher injects its own. That is
not only for tests: it keeps the choice of provider command in the operator's configuration
where the declared build horizon (another operator, another machine) requires it to be.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from assistant_memory.audience.blind_prompt import AssemblyRefused
from assistant_memory.audience.journal import LaunchJournal
from assistant_memory.audience.profile import LaunchProfile
from assistant_memory.audience.records import SPEND_UNMEASURED, RunKind, RunOutcome, RunRecord
from assistant_memory.audience.transcript import (
    extract_runner_version,
    extract_spend,
    extract_tool_calls,
    header_violations,
    parse_header,
)

#: A model invocation: given the prompt, return the transcript exactly as the provider
#: produced it. Everything the record says about a run is derived from this text.
Invoke = Callable[[str], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BlindLauncher:
    """Runs a model in the blind profile and produces the record for that run.

    It produces the record; it does not post it. Sending is a thin adapter's job, which is
    what lets this be exercised without a live review and keeps the genre's transport
    decision out of the runner.
    """

    profile: LaunchProfile
    journal: LaunchJournal
    invoke: Invoke
    transcripts_dir: Path
    now: Callable[[], datetime] = _utc_now
    new_id: Callable[[], str] = lambda: uuid.uuid4().hex[:12]

    def run(
        self,
        prompt: str,
        *,
        kind: RunKind,
        iteration: int,
        artifact_seq: int,
        model_version: str,
        **kind_fields: object,
    ) -> RunRecord:
        """Take the lock, consume a launch number, invoke, store the transcript, record it.

        The transcript is STORED, not merely hashed: the right to check what was carried
        over into the channel is worth nothing without a readable transcript, and the hash
        only proves that the file at that path is the one the record means.
        """
        # BEFORE THE LOCK AND BEFORE THE NUMBER. The model's version is what makes the
        # canary's verdict apply to the run that follows it, so a blank one is not a launch
        # with a missing label — it is a launch that cannot be bound to anything. Refusing
        # here rather than at crediting also keeps the counter still: a number consumed by a
        # run that can never be credited would push the next honest pair apart.
        if not model_version.strip():
            raise ValueError(
                "версия модели пуста — канарейка и чтение связываются именно ею, и пустая "
                "совпала бы с пустой, выдав неопознанный инструмент за тот же самый"
            )
        self.journal.guard_placement(self.profile.settings_dir)
        fingerprint = self.profile.fingerprint()

        with self.journal.launch(
            fingerprint.digest, settings_dir=self.profile.settings_dir
        ) as entry:
            started_at = entry.started_at
            outcome, reason = RunOutcome.HAPPENED, None
            try:
                transcript = self.invoke(prompt)
            except Exception as exc:  # a failed invocation is a recorded run, not a gap
                transcript = f"<прогон не дал стенограммы: {type(exc).__name__}: {exc}>"
                outcome, reason = RunOutcome.ANNULLED, f"вызов модели не состоялся: {exc}"
            finished_at = self.now()

        run_id = self.new_id()
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcripts_dir / f"{kind.name.lower()}_{run_id}.log"
        # BYTES, NOT TEXT, AND THE SAME BYTES ARE HASHED. `write_text` translates newlines on
        # Windows, so the file on disk was NOT what the record's hash was taken over — and the
        # gate re-reads the file and compares. Every honest pair would have failed crediting
        # with "a different transcript lies at that path", on the machine this runs on. The
        # defect was invisible while nothing ever wrote a transcript and read it back: the
        # tests supplied their own bytes with matching hashes, and the live check predates the
        # re-derivation. One source of truth now: what is written is what is hashed.
        stored = transcript.encode("utf-8")
        path.write_bytes(stored)

        tool_calls = extract_tool_calls(transcript)
        header = parse_header(transcript)
        if outcome is RunOutcome.HAPPENED:
            # The header first: it is the provider's own statement about the launch, and it
            # settles the isolation question that prose-scanning only guesses at.
            problems = header_violations(header)
            if tool_calls:
                problems.append(
                    f"стенограмма содержит выполненные вызовы инструментов ({len(tool_calls)})"
                )
            if problems:
                outcome = RunOutcome.ANNULLED
                reason = "изоляция чтения не подтверждена: " + "; ".join(problems)

        return RunRecord(
            id=run_id,
            kind=kind,
            iteration=iteration,
            artifact_seq=artifact_seq,
            profile_digest=fingerprint.digest,
            launch_number=entry.number,
            started_at=started_at,
            finished_at=finished_at,
            transcript_path=str(path),
            transcript_sha256=hashlib.sha256(stored).hexdigest(),
            tool_calls=tool_calls,
            model=self.profile.model,
            model_version=model_version,
            spend=extract_spend(transcript),
            outcome=outcome,
            runner_version=extract_runner_version(transcript),
            sandbox_mode=header.get("sandbox"),
            approval_mode=header.get("approval"),
            outcome_reason=reason,
            **kind_fields,  # type: ignore[arg-type]
        )


    def record_refusal(
        self,
        refusal: AssemblyRefused,
        *,
        kind: RunKind,
        iteration: int,
        artifact_seq: int,
        model_version: str,
        **kind_fields: object,
    ) -> RunRecord:
        """A record for an attempt that never launched — assembly refused.

        Every attempt is recorded, the refused ones included: otherwise they vanish and the
        series of measurements shows only the successes. The journal is NOT touched — no
        launch happened, and consuming a number here would push the next honest pair apart
        and break a binding with nothing wrong with it.

        The refusal report is stored as this record's transcript rather than left in a log
        line. For a run that produced no model output, the report IS the evidence of what
        happened, and the record's transcript field is where evidence lives.
        """
        run_id = self.new_id()
        at = self.now()
        report = refusal.report()
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = self.transcripts_dir / f"{kind.name.lower()}_{run_id}_отказ.log"
        path.write_text(report, encoding="utf-8")

        return RunRecord(
            id=run_id,
            kind=kind,
            iteration=iteration,
            artifact_seq=artifact_seq,
            profile_digest=self.profile.fingerprint().digest,
            launch_number=None,
            started_at=at,
            finished_at=at,
            transcript_path=str(path),
            transcript_sha256=hashlib.sha256(report.encode("utf-8")).hexdigest(),
            tool_calls=(),
            model=self.profile.model,
            model_version=model_version,
            spend=SPEND_UNMEASURED,
            outcome=RunOutcome.NOT_STARTED,
            outcome_reason="отказ сборки слепого промпта: " + "; ".join(refusal.reasons),
            **kind_fields,  # type: ignore[arg-type]
        )


def canary_verdict(answer: str, markers: list[str]) -> tuple[bool, list[str]]:
    """Clean or not, and which declared markers were hit.

    A marker is a SUFFICIENT condition, not an exhaustive one. The answer can give the
    project away without using any word thought of in advance, so this returns the machine
    half only: development is still required to read the answer whole and annul on any
    recognition, recording the reason in its own words. Where the two criteria disagree,
    the resolution is annulment.
    """
    lowered = answer.lower()
    hit = [marker for marker in markers if marker.lower() in lowered]
    return (not hit), hit
