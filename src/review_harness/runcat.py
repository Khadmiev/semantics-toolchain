# SPDX-License-Identifier: Apache-2.0
"""The run catalogue — the carrier of a review cycle.

The section of the spec on transporting rounds: the task description with its
deltas, the records of the rounds (the critic's pass verbatim plus the outcomes),
the records of the operator's decisions, the accompanying launch note. The
catalogue lives on disk and does not enter git (the reader rule, the operator's
decision of 2026-09-01) — durability here means "the file is written and flushed
to disk", not "committed".

Two load-bearing rules:

* the critic's pass is written IMMEDIATELY on receipt, before any analysis — a
  crash of the development session between receipt and analysis loses no findings;
* from the catalogue a new session of any role restores the state of the cycle
  with nothing handed over by word of mouth (principle 2 of the spec).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import HarnessError


def assert_outside_git(run_dir: Path) -> None:
    """The run catalogue must be outside git — and the harness checks that.

    The semantics are the operator's decision (the pivot of round 9): the git ignore
    on the catalogue is HYGIENE for the repository tree ("so the repo does not get
    littered"), not defence; the publication threat is closed a level higher — the
    working repository is not published at all (R1.1, a separate clean repository).
    The check stays: a catalogue inside the repository is accepted only when git
    ignores it, so that a machine process litters neither the tree nor the status;
    being unable to check is a loud refusal rather than a quiet skip (principle 1).
    """
    git_root = next(
        (p for p in [run_dir, *run_dir.parents] if (p / ".git").exists()), None
    )
    if git_root is None:
        return  # outside a repository there is nothing to publish
    try:
        result = subprocess.run(
            ["git", "-C", str(git_root), "check-ignore", "-q", str(run_dir)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(
            "Could not check whether git ignores the directory",
            cause=f"git is unavailable: {exc}",
            next_action="fix git on PATH, or create the directory outside the repository",
        ) from exc
    # The codes differ: 1 means "not ignored" (the only code that means the path is
    # tracked); anything else non-zero means the check DID NOT RUN (dubious
    # ownership, corruption) and must not be passed off as "tracked" — that would be
    # somebody else's failure with somebody else's repair (the precedent of
    # 2026-08-31: a linked copy plus safe.directory).
    if result.returncode not in (0, 1):
        raise HarnessError(
            "The git check-ignore check did not run",
            cause=(
                f"git returned code {result.returncode}: "
                f"{(result.stderr or '').strip()[:300] or '<no message>'}"
            ),
            next_action=(
                "fix git's access to the copy (a common cause is dubious ownership: "
                "git config --global --add safe.directory <path>) and repeat; "
                "or create the directory outside the repository"
            ),
        )
    if result.returncode == 1:
        raise HarnessError(
            f"The run catalogue {run_dir} is tracked by git",
            cause=(
                "the machine side of the review does not litter the repository tree (the "
                "operator's decision: the ignore is hygiene; publication is closed by R1.1)"
            ),
            next_action=(
                "choose a path under an ignored rule (docs/review/*/) "
                "or add the directory to .gitignore before creating the run"
            ),
        )

# Two digits are the minimum of formatting, not a ceiling: round 100 is written
# round_100_* and must be recognised, or a two-digit template would become a de
# facto max_rounds, which the process does not have by the operator's decision.
_ROUND_PASS_RE = re.compile(r"^round_(\d{2,})_pass\.md$")
_ROUND_OUTCOMES_RE = re.compile(r"^round_(\d{2,})_outcomes\.md$")
_ROUND_REQUESTS_RE = re.compile(r"^round_(\d{2,})_gate_requests\.json$")
_ROUND_ANSWERS_RE = re.compile(r"^round_(\d{2,})_gate_answers\.json$")

# The catalogue's file names are the contract of recoverability: they are known
# to the code and to a human who opens the directory by hand.
DESCRIPTION = "task_description.md"
LAUNCH_NOTE = "launch_critic.md"
META = "run.json"
GATES = "gates.md"


def _write_durably(path: Path, text: str) -> None:
    """Write and FLUSH to disk — privately, and failing under the contract.

    An ordinary ``write_text`` leaves the data in the OS cache; a machine crash
    before the flush loses a record the spec declared fireproof. Privacy is checked
    HERE, at the point of writing — no branch of the code can write a durable file
    past the check (the .gitignore rule may have narrowed mid-round). Disk errors (no
    space, permissions) become a harness refusal with a cause and an action rather
    than a raw traceback (principle 3).
    """
    assert_outside_git(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            # A rename is durable only after an fsync of the DIRECTORY: without
            # it a sudden power cut can bring the old file back (a finding of
            # round 9). On Windows there is no equivalent — there the replace is
            # journalled by NTFS.
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    except OSError as exc:
        raise HarnessError(
            f"Could not durably write {path}",
            cause=f"a disk error: {exc}",
            next_action="check the free space and the permissions on the run catalogue, repeat the step",
        ) from exc


@dataclass
class RunState:
    """The state of the cycle, restored from the catalogue."""

    run_dir: Path
    artifact_paths: list[str]
    critic_model: str
    rounds_with_pass: list[int]
    rounds_with_outcomes: list[int]
    # The entry gate: the first round's description was confirmed by the operator.
    entry_gate_confirmed: bool
    # Rounds with questions to the operator still hanging (question files on disk).
    pending_gate_rounds: list[int]

    @property
    def resume_round(self) -> int | None:
        """A round with a pass received and no outcomes — analysis continues from it.

        This is precisely the "continue from the files": after a crash of development
        the critic is not replayed, and analysis starts from the recorded pass.
        """
        waiting = sorted(set(self.rounds_with_pass) - set(self.rounds_with_outcomes))
        return waiting[0] if waiting else None

    @property
    def current_round(self) -> int:
        """The number of the round that is running now.

        The number is kept for recoverability and for the trend line; it is not a
        regulator (the subtraction section of the spec) — there is no ceiling here and
        there must not be one.
        """
        return max(self.rounds_with_pass, default=0) + (
            0 if self._pass_awaits_outcomes() else 1
        )

    def _pass_awaits_outcomes(self) -> bool:
        return bool(set(self.rounds_with_pass) - set(self.rounds_with_outcomes))


class RunCatalog:
    """One review run: creation, immediate writing, recovery."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)

    # -- creation -----------------------------------------------------------

    @classmethod
    def create(
        cls,
        run_dir: Path,
        *,
        artifact_paths: list[str],
        critic_model: str,
        description_text: str,
        launch_note_text: str,
    ) -> RunCatalog:
        run_dir = Path(run_dir)
        try:
            nonempty = run_dir.exists() and any(run_dir.iterdir())
        except OSError as exc:
            raise HarnessError(
                f"Could not inspect the run catalogue: {run_dir}",
                cause=f"a disk error: {exc}",
                next_action="check the permissions on the directory",
            ) from exc
        if nonempty:
            raise HarnessError(
                f"The run catalogue {run_dir} already exists and is not empty",
                cause="creating over a live run would wipe its records",
                next_action="continue the existing run (restore), or choose another directory",
            )
        if not critic_model.strip():
            # The choice of the critic's model is an explicit input of every review (the
            # roles section of the spec): there is no silent default.
            raise HarnessError(
                "No critic model was given",
                cause="the choice of model is an explicit review input; defaults are forbidden by the spec",
                next_action="pass critic_model explicitly (gpt-5.6-sol, for example)",
            )
        if not description_text.strip():
            # An empty description would let the critic honestly produce a clean pass
            # against an intent that does not exist — exactly the class of failure the
            # entry gate was written into the spec for.
            raise HarnessError(
                "The task description is empty",
                cause="with no description the critic has nothing to check the artifact against — the pass would be a fiction",
                next_action="pass a non-empty description, grounded in the operator's intent",
            )
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HarnessError(
                f"Could not create the run catalogue: {run_dir}",
                cause=f"a disk error: {exc}",
                next_action="check the permissions and the free space",
            ) from exc
        assert_outside_git(run_dir)
        _write_durably(run_dir / DESCRIPTION, description_text)
        _write_durably(run_dir / LAUNCH_NOTE, launch_note_text)
        meta = {
            "artifact_paths": artifact_paths,
            "critic_model": critic_model,
            "created_at": datetime.now(UTC).isoformat(),
            # The entry gate: the critic's first pass is legitimate only after the operator
            # has confirmed the description (mark_entry_confirmed).
            "entry_gate_confirmed": False,
        }
        _write_durably(run_dir / META, json.dumps(meta, ensure_ascii=False, indent=2))
        return cls(run_dir)

    def entry_journal_repair(self, gates_name: str = GATES) -> None:
        """Repair the entry-gate journal from the state (idempotent).

        State comes first; if the journal write failed after the flag was written
        (round 14), the final pair is restored from run.json at the next entry — the
        journal does not stay incomplete forever.
        """
        meta = self._load_meta()
        answer = meta.get("entry_gate_answer") or ""
        prompt = meta.get("entry_gate_prompt") or "Entry gate"
        if not meta.get("entry_gate_confirmed") or not answer:
            return
        path = self.run_dir / gates_name
        existing = self._read_text(path, "the gate journal") if path.exists() else ""
        # The match is BY THE EXACT recorded line, not by substring: a final «да» was
        # found inside an intermediate «да?» and the repair stayed silent (round 15).
        # Both lines of the pair are checked: the question and the answer.
        lines = set(existing.splitlines())
        has_answer = f"The operator's answer (verbatim): {answer}" in lines
        has_prompt = f"Question: {prompt}" in lines
        if not (has_answer and has_prompt):
            self.append_gate_record(prompt, answer, kind="the entry gate")

    def mark_entry_confirmed(self, answer_verbatim: str, prompt: str) -> None:
        """Mark the entry gate with ONE durable write carrying the verbatim answer.

        The flag and the operator's verbatim answer live in one file: a crash cannot
        leave "there is an answer but no state" — the window for a repeated question
        is closed by construction (a finding of round 8). The gates.md journal is
        secondary and is appended after the state.
        """
        # A flag without its evidence does not exist (round 17): confirmation of the
        # entry gate must carry the operator's verbatim answer and the actual text of
        # the question — otherwise recovery would skip the critic without a single
        # recorded word from the operator.
        if not answer_verbatim.strip() or not prompt.strip():
            raise HarnessError(
                "The entry gate was confirmed with no verbatim answer or no question",
                cause="a flag with no evidence is a way past the gate on recovery",
                next_action="pass the operator's verbatim answer and the actual question",
            )
        meta = self._load_meta()
        meta["entry_gate_confirmed"] = True
        meta["entry_gate_answer"] = answer_verbatim
        meta["entry_gate_prompt"] = prompt
        _write_durably(self.run_dir / META, json.dumps(meta, ensure_ascii=False, indent=2))

    # -- immediate writing ---------------------------------------------------

    def write_pass(
        self, round_no: int, pass_text: str, *, extra_meta: dict | None = None
    ) -> Path:
        """Write the critic's pass VERBATIM — the moment it is received.

        Verbatim means byte for byte: the pass file carries neither a header nor a
        harness timestamp — the metadata goes to the neighbouring
        ``*_pass_meta.json``. The metadata (including the snapshot of the judged
        version from ``extra_meta``) lands BEFORE the pass: recovery is triggered by
        the pass existing, so a pass without metadata must not exist at any moment — a
        crash between the two writes gives "metadata with no pass" (harmless: the
        round is replayed) but never the other way round. An existing pass is never
        overwritten: a round's pass is a fact, not a draft.
        """
        self._ensure_private()
        if not pass_text.strip():
            raise HarnessError(
                f"The pass of round {round_no} is empty",
                cause="empty text is not a pass; writing it would legitimise a fiction",
                next_action="work out what the critic produced; emptiness is not written",
            )
        path = self.run_dir / f"round_{round_no:02d}_pass.md"
        if path.exists():
            raise HarnessError(
                f"The pass of round {round_no} is already written",
                cause=(
                    "overwriting would destroy the pass that was received — "
                    "after a crash the analysis continues, the critic is not replayed"
                ),
                next_action=(
                    f"the analysis continues from {path}; a new critic pass "
                    "is legitimate only as a new round"
                ),
            )
        meta = {"received_at": datetime.now(UTC).isoformat(), **(extra_meta or {})}
        # Completeness of the metadata is a contract of the write, not luck on reading:
        # a pass without the judged-version snapshot and the delivery version cannot exist.
        self._require_complete_meta(meta, context=f"writing the pass of round {round_no}")
        _write_durably(
            self.run_dir / f"round_{round_no:02d}_pass_meta.json",
            json.dumps(meta, ensure_ascii=False),
        )
        _write_durably(path, pass_text)
        return path

    def _require_complete_meta(self, meta: dict, *, context: str) -> None:
        snapshot = meta.get("artifact_snapshot")
        version = meta.get("operator_profile_version")
        head = meta.get("git_head")
        completed = meta.get("completed", False)
        if not isinstance(completed, bool):
            raise HarnessError(
                f"The round completion marker has the wrong type ({context})",
                cause=(
                    f"a boolean completed was expected, got "
                    f"{type(completed).__name__} ({completed!r}); a string "
                    "\"false\" is truthy in a condition and would walk past the gate (round 15)"
                ),
                next_action="correct completed in the round metadata to an honest true/false",
            )
        # Completeness is measured against the SUBJECT of the run: the snapshot must
        # cover every artifact from run.json plus the description and the accompanying
        # note — one fictional key does not describe the version that was judged.
        expected = set(self._load_meta()["artifact_paths"]) | {
            "__task_description__",
            "__launch_note__",
        }
        ok = (
            isinstance(version, str)
            and version.strip()
            and isinstance(head, str)
            and head.strip()
            # An error stub is not provenance; a legitimate "outside git" is an explicit
            # value for a run outside a working copy.
            and not head.startswith("<")
            and isinstance(snapshot, dict)
            and set(snapshot) == expected
            and all(isinstance(k, str) and isinstance(v, str) for k, v in snapshot.items())
        )
        if not ok:
            raise HarnessError(
                f"The round metadata is incomplete ({context})",
                cause=(
                    "operator_profile_version, git_head and an artifact_snapshot covering "
                    "exactly the run's artifacts plus the description plus the accompanying "
                    "note are mandatory — otherwise \"which version was judged\" is "
                    "unrecoverable and the analysis would be made blind"
                ),
                next_action=(
                    "when writing, pass complete metadata; when reading, restore the "
                    "metadata file from a backup or replay the round"
                ),
            )

    def read_round_meta(self, round_no: int) -> dict:
        """The round's metadata — loudly: a pass with no metadata is a corrupt state.

        A silent ``{}`` would mean "we continue the analysis knowing neither the
        artifact version nor the profile version" — precisely the loss of the judged
        version's identity.
        """
        meta_path = self.run_dir / f"round_{round_no:02d}_pass_meta.json"
        if not meta_path.exists():
            raise HarnessError(
                f"The metadata of round {round_no} is missing",
                cause=(
                    f"{meta_path} is not there; a pass with no metadata loses the identity "
                    "of the judged version (metadata is written before the pass and must exist)"
                ),
                next_action=(
                    "restore the metadata file from a backup; if there is none, delete the "
                    "round's pass and replay the round"
                ),
            )
        try:
            meta = json.loads(self._read_text(meta_path, f"the metadata of round {round_no}"))
            if not isinstance(meta, dict):
                raise ValueError("the metadata is not an object")
            # The file existing is not completeness: {"received_at": ...} would allow
            # analysis made blind (round 4). The mandatory fields are checked here.
            self._require_complete_meta(meta, context=f"reading the metadata of round {round_no}")
            return meta
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessError(
                f"The metadata of round {round_no} is missing or corrupted",
                cause=(
                    f"{meta_path}: {exc}; a pass with no metadata loses the identity "
                    "of the judged version (metadata is written before the pass and must exist)"
                ),
                next_action=(
                    "restore the metadata file from a backup; if there is none, delete the "
                    "round's pass and replay the round"
                ),
            ) from exc

    def mark_round_completed(self, round_no: int) -> None:
        """Declare a round complete — with the HARNESS's marker in the round metadata.

        The outcomes file is written by headless development with an ordinary write;
        by itself it is not completion (round 14): a crash of the harness between the
        outcomes and its own checks (hanging questions to the operator, the durability
        rewrite) would leave an undelivered question, while a restart would count the
        round closed and walk past the gate. Completion exists only after this durable
        mark.
        """
        meta = self.read_round_meta(round_no)
        meta["completed"] = True
        _write_durably(
            self.run_dir / f"round_{round_no:02d}_pass_meta.json",
            json.dumps(meta, ensure_ascii=False),
        )

    def round_completed(self, round_no: int) -> bool:
        return bool(self.read_round_meta(round_no).get("completed"))

    def write_outcomes(self, round_no: int, outcomes_text: str) -> Path:
        self._ensure_private()
        if not outcomes_text.strip():
            raise HarnessError(
                f"The outcomes of round {round_no} are empty",
                cause="an empty outcomes file is not an analysis that happened but an imitation of one",
                next_action="pass the outcomes item by item, or do not complete the round",
            )
        pass_path = self.run_dir / f"round_{round_no:02d}_pass.md"
        if not pass_path.exists():
            raise HarnessError(
                f"The outcomes of round {round_no} are being written before its pass",
                cause="the critic pass must land in the catalogue first (the immediate write)",
                next_action="call write_pass with the critic's text first, then the outcomes",
            )
        path = self.run_dir / f"round_{round_no:02d}_outcomes.md"
        _write_durably(path, outcomes_text)
        return path

    def _ensure_private(self) -> None:
        """That the directory is ignored is checked on every write.

        The semantics are hygiene of the repository tree (the operator's decision,
        round 9), not defence: machine writes must not litter the status and the tree.
        """
        assert_outside_git(self.run_dir)

    def append_gate_record(self, question: str, answer: str, *, kind: str) -> None:
        """Append a gate record: the question to the operator and his answer, durably."""
        self._ensure_private()
        path = self.run_dir / GATES
        stamp = datetime.now(UTC).isoformat()
        entry = (
            f"\n## {stamp} — {kind}\n\n"
            f"Question: {question}\n\n"
            f"The operator's answer (verbatim): {answer}\n"
        )
        from .errors import read_text_contract

        existing = (
            read_text_contract(path, "the gate journal") if path.exists() else "# The run's gates\n"
        )
        _write_durably(path, existing + entry)

    # -- recovery ------------------------------------------------------------

    def _read_text(self, path: Path, what: str) -> str:
        """Reading a catalogue file — under the contract (disk AND encoding)."""
        from .errors import read_text_contract

        return read_text_contract(path, what)

    def _load_meta(self) -> dict:
        meta_path = self.run_dir / META
        if not meta_path.exists():
            raise HarnessError(
                f"{self.run_dir} has no {META} file",
                cause="the directory is not a harness run, or was not created by one",
                next_action="check the path; a new run is made through create, not by hand",
            )
        try:
            meta = json.loads(self._read_text(meta_path, "the run metadata"))
            # Types are checked, not merely the presence of keys: the string "false" instead
            # of a boolean entry_gate_confirmed would become True through bool() and would
            # open the first pass without the operator's confirmation.
            if not (
                isinstance(meta.get("artifact_paths"), list)
                and all(isinstance(p, str) for p in meta["artifact_paths"])
                and isinstance(meta.get("critic_model"), str)
                and isinstance(meta.get("entry_gate_confirmed", False), bool)
                and isinstance(meta.get("entry_gate_answer", ""), str)
                and isinstance(meta.get("entry_gate_prompt", ""), str)
                # A confirmed flag must carry both fields of evidence: a partial corruption of
                # run.json that left the flag without the answer is damage, not a gate that was
                # passed (round 17).
                and (
                    not meta.get("entry_gate_confirmed", False)
                    or (
                        str(meta.get("entry_gate_answer", "")).strip()
                        and str(meta.get("entry_gate_prompt", "")).strip()
                    )
                )
            ):
                raise TypeError("the metadata fields have the wrong types")
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # A raw traceback instead of a refusal is exactly the way round the promise of
            # "a cause plus a next action"; a corrupted file is no exception.
            raise HarnessError(
                f"The run metadata is corrupted: {meta_path}",
                cause=f"the file does not read as a harness run: {exc}",
                next_action=(
                    "restore run.json from a backup or create the run anew; "
                    "the round records are intact and portable either way"
                ),
            ) from exc
        return meta

    def restore(self) -> RunState:
        """Restore the state of the cycle from the files — nothing handed over by word."""
        meta = self._load_meta()
        for required in (DESCRIPTION, LAUNCH_NOTE):
            if not (self.run_dir / required).exists():
                raise HarnessError(
                    f"The run catalogue has no mandatory file {required}",
                    cause="the catalogue is incomplete — the description and the note are part of the contract",
                    next_action="restore the file from a backup or create the run anew",
                )
        rounds_with_pass: list[int] = []
        rounds_with_outcomes: list[int] = []
        requests: list[int] = []
        answers: list[int] = []
        # Names are parsed by the contract's regular expressions: a foreign file
        # (junk_outcomes.md) is not ours and is skipped silently rather than felling the
        # recovery with a raw ValueError.
        try:
            children = list(self.run_dir.iterdir())
        except OSError as exc:
            raise HarnessError(
                f"Could not read the run catalogue: {self.run_dir}",
                cause=f"a disk error: {exc}",
                next_action="check the permissions on the directory; the records are intact",
            ) from exc
        for child in children:
            for regex, bucket in (
                (_ROUND_PASS_RE, rounds_with_pass),
                (_ROUND_OUTCOMES_RE, rounds_with_outcomes),
                (_ROUND_REQUESTS_RE, requests),
                (_ROUND_ANSWERS_RE, answers),
            ):
                m = regex.match(child.name)
                if m:
                    bucket.append(int(m.group(1)))
                    break
        # The catalogue's invariants are checked, not assumed: outcomes with no pass are
        # a corrupt state (a foreign or orphaned outcomes file would later pass itself
        # off as the result of a fresh analysis), and every pass must have metadata and
        # that metadata must be complete — otherwise "which version was judged" is
        # already lost, and that has to be said now.
        for done in rounds_with_outcomes:
            body = self._read_text(
                self.run_dir / f"round_{done:02d}_outcomes.md",
                f"the outcomes of round {done}",
            )
            if not body.strip():
                raise HarnessError(
                    f"The outcomes file of round {done} is empty",
                    cause=(
                        "empty outcomes are not an analysis that happened; the active path does "
                        "not write them, so the file is corrupted or forged"
                    ),
                    next_action="restore the outcomes from a backup, or delete the file and replay the analysis",
                )
        orphan_outcomes = sorted(set(rounds_with_outcomes) - set(rounds_with_pass))
        if orphan_outcomes:
            raise HarnessError(
                f"The catalogue holds outcomes with no pass: rounds {orphan_outcomes}",
                cause=(
                    "an outcomes file exists for a round whose pass does not — "
                    "it would pass an analysis that never happened off as one that did"
                ),
                next_action="remove the orphaned outcomes files, or restore the passes from a backup",
            )
        # Continuity of the history: the rounds run 1..N with no holes — a round lost in
        # the middle would hide part of the history of decisions.
        if sorted(rounds_with_pass) != list(range(1, len(rounds_with_pass) + 1)):
            raise HarnessError(
                f"The history of rounds has holes: there are passes {sorted(rounds_with_pass)}",
                cause="the files of a middle round are lost — part of the history of decisions is gone",
                next_action="restore the missing round's files from a backup; carrying on quietly is not allowed",
            )
        # Completed rounds form a PREFIX of the history: outcomes for round 2 with no
        # outcomes for round 1 are impossible on a healthy active path — that is damage,
        # not "continue the analysis of the first round after the second".
        if sorted(rounds_with_outcomes) != list(range(1, len(rounds_with_outcomes) + 1)):
            raise HarnessError(
                f"The outcomes do not form a prefix of the history: {sorted(rounds_with_outcomes)}",
                cause="the active path completes rounds in order — such a set is damaged",
                next_action="restore the missing outcomes from a backup; carrying on quietly is not allowed",
            )
        for done in rounds_with_pass:
            self.read_round_meta(done)
            body = self._read_text(
                self.run_dir / f"round_{done:02d}_pass.md",
                f"the pass of round {done}",
            )
            if not body.strip():
                raise HarnessError(
                    f"The pass file of round {done} is empty",
                    cause="writing an empty pass is forbidden — the file is truncated or corrupted",
                    next_action="restore the pass from a backup, or delete the round and replay it",
                )
        # A round's completeness is decided by the HARNESS's marker, not by the outcomes
        # file from development (round 14): non-empty outcomes with no marker mean the
        # round is still running (a crash before the harness's checks) and has to be
        # carried through rather than counted as closed.
        # The completion marker is checked against the CONSEQUENCES of completion
        # (round 16): a completed round must have non-empty outcomes — completed=true
        # with the outcomes file gone is corruption of a completed history, not "the
        # round suddenly became incomplete" with a repeat analysis laid over one that
        # already happened.
        for r in rounds_with_pass:
            if self.round_completed(r) and r not in rounds_with_outcomes:
                raise HarnessError(
                    f"Round {r} is marked complete, but its outcomes are missing",
                    cause=(
                        "the outcomes file of a completed round is lost or corrupted "
                        "apart from its metadata — a completed history is damaged"
                    ),
                    next_action=(
                        "restore the outcomes file from a backup; replaying an analysis that "
                        "already happened, quietly, is not allowed"
                    ),
                )
        completed = [r for r in rounds_with_outcomes if self.round_completed(r)]
        # The prefix of the history is measured over COMPLETED rounds: "round 1 is
        # not complete, round 2 is" is a state impossible on a healthy path, and a
        # repeat analysis of an old round on top of later history is forbidden
        # (round 15).
        if sorted(completed) != list(range(1, len(completed) + 1)):
            raise HarnessError(
                f"The completed rounds do not form a prefix of the history: {sorted(completed)}",
                cause="the active path completes rounds in order — the state is damaged",
                next_action="restore the missing round's metadata from a backup; carrying on quietly is not allowed",
            )
        # Only ONE round can be incomplete — the current one (round 19): two rounds with
        # no completion marker are impossible on a healthy path, and accepting them
        # would mean sending an early round to a repeat analysis ON TOP of later history
        # that already happened (overwriting its outcomes and repeating its side
        # effects).
        if len(rounds_with_pass) > len(completed) + 1:
            raise HarnessError(
                f"More than one round is incomplete: {len(completed)} completed, "
                f"while there are passes up to round {len(rounds_with_pass)}",
                cause=(
                    "the completion markers of more than one round are lost — the "
                    "metadata is damaged, this is not \"the cycle stopped\""
                ),
                next_action=(
                    "restore the metadata of the completed rounds from a backup; replaying "
                    "an early round over later history, quietly, is not allowed"
                ),
            )
        rounds_with_outcomes = completed
        # A hanging fork means questions whose answers are MISSING or INCOMPLETE: a
        # partly answered batch (a crash in the middle of the questions) is still a
        # hanging state, not a closed one (a finding of round 9).
        pending: list[int] = []
        for rnd in requests:
            if rnd not in answers:
                pending.append(rnd)
                continue
            try:
                reqs = json.loads(
                    self._read_text(
                        self.run_dir / f"round_{rnd:02d}_gate_requests.json",
                        f"the questions of round {rnd}",
                    )
                )
                ans = json.loads(
                    self._read_text(
                        self.run_dir / f"round_{rnd:02d}_gate_answers.json",
                        f"the answers of round {rnd}",
                    )
                )
                if not isinstance(reqs, list) or not isinstance(ans, list):
                    raise ValueError("not lists")
            except (json.JSONDecodeError, ValueError) as exc:
                raise HarnessError(
                    f"The gate files of round {rnd} are corrupted",
                    cause=f"they do not read as lists of questions/answers: {exc}",
                    next_action="restore the gate files from a backup or from the gates.md journal",
                ) from exc
            # Completeness is measured by the IDENTITY of the questions, not by their count:
            # two copies of the answer to the first question do not close the second (a
            # finding of round 10). The structure of the items is checked along the way.
            if not all(
                isinstance(r, dict) and isinstance(r.get("question"), str)
                for r in reqs
            ) or not all(
                isinstance(a, dict)
                and isinstance(a.get("question"), str)
                and isinstance(a.get("answer"), str)
                for a in ans
            ):
                raise HarnessError(
                    f"The gate files of round {rnd} are structurally corrupted",
                    cause="items with no question/answer",
                    next_action="restore the gate files from the gates.md journal",
                )
            answered_q = {a["question"] for a in ans}
            if any(r["question"] not in answered_q for r in reqs):
                pending.append(rnd)
        pending = sorted(pending)
        # A hanging question on a COMPLETED round breaks the contract "one batch of
        # forks per round": a closed round asks no new questions, and moving quietly
        # on to the next round without serving it is not allowed
        # (round 16).
        stale = sorted(set(pending) & set(completed))
        if stale:
            raise HarnessError(
                f"Completed rounds have acquired hanging questions: {stale}",
                cause="a closed round asks no questions — the gate files are corrupted or forged",
                next_action="work out the question files of those rounds; the state is intact",
            )
        # Gate files live only inside the history (round 20), and the measure is the
        # PASS rather than the number (round 21): development files questions after the
        # round's pass and answers are born of questions — a gate file for a round whose
        # pass does not exist is impossible on a healthy path. An answer "out of nowhere"
        # in a fresh run would be read by development as the operator's word belonging to
        # no question that was ever asked.
        passes = set(rounds_with_pass)
        alien = sorted({r for r in requests + answers if r not in passes})
        if alien:
            raise HarnessError(
                f"The gate files belong to rounds outside the run's history: {alien}",
                cause=(
                    f"there are passes for rounds {sorted(passes) or '—'}; a round with no "
                    "pass has no gates — the files are corrupted, forged or "
                    "named wrongly"
                ),
                next_action="remove the foreign gate files, or restore the catalogue from a backup",
            )
        # Answers are parsed structurally and WITHOUT the questions file (round 21):
        # after a batch has been served the questions are deleted and the answers remain
        # — their corruption must be found by recovery rather than at the moment
        # development reads a broken file as the operator's word.
        for rnd in answers:
            if rnd in requests:
                continue  # parsed together with the questions above
            try:
                ans = json.loads(
                    self._read_text(
                        self.run_dir / f"round_{rnd:02d}_gate_answers.json",
                        f"the answers of round {rnd}",
                    )
                )
                if not isinstance(ans, list) or not all(
                    isinstance(a, dict)
                    and isinstance(a.get("question"), str)
                    and isinstance(a.get("answer"), str)
                    for a in ans
                ):
                    raise ValueError("not a list of question/answer records")
            except (json.JSONDecodeError, ValueError) as exc:
                raise HarnessError(
                    f"The answers file of round {rnd} is corrupted",
                    cause=f"it does not read as a list of question/answer records: {exc}",
                    next_action="restore the file from the gates.md journal or from a backup",
                ) from exc
        return RunState(
            run_dir=self.run_dir,
            artifact_paths=list(meta["artifact_paths"]),
            critic_model=str(meta["critic_model"]),
            rounds_with_pass=sorted(rounds_with_pass),
            rounds_with_outcomes=sorted(rounds_with_outcomes),
            entry_gate_confirmed=meta.get("entry_gate_confirmed", False),
            pending_gate_rounds=pending,
        )
