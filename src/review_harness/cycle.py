# SPDX-License-Identifier: Apache-2.0
"""Running the cycle: delivering a pass to development and resuming its session.

The roles section of the spec: an automated launch of the critic with the results
carried by hand does not count as a cycle. One call of ``one_round`` makes a round:

1. it checks the entry gate (the first round's description was confirmed by the
   operator BEFORE the critic's first pass; without that confirmation the critic
   is not started);
2. it checks the launch knowledge (is it runnable) and names the thresholds;
3. it starts the critic and writes his pass into the catalogue IMMEDIATELY — or,
   when the catalogue holds a pass with no outcomes (a crash of the previous
   session), it CONTINUES from that pass without replaying the critic: a round's
   pass is a fact;
4. it resumes development (the headless command from the machine profile);
5. development's questions to the operator go through the gates; the files of
   questions and answers are per round, so that one round's answer is never read
   as another's; answers are written durably BEFORE the questions are deleted.

The cycle has no verdicts: the critic recommends stopping, the operator decides.
"""

from __future__ import annotations

import hashlib
import hashlib as _hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import critic, liveness, machine_profile
from .errors import HarnessError, read_bytes_contract, read_text_contract
from .gates import Gate
from .runcat import RunCatalog, _write_durably
from .subproc import run_watched

_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def stream_dir(run_dir: Path) -> Path:
    """The role's stream directory — OUTSIDE the repository by construction.

    The streams live in the system temp and reach the run catalogue only by a durable
    write: the middle of a role does not litter the repository tree, and a crash
    leaves no half-written files in the catalogue (the semantics of the ignore rule —
    hygiene, the operator's decision in round 9).
    """
    tag = _hashlib.sha256(str(Path(run_dir).resolve()).encode()).hexdigest()[:16]
    d = Path(tempfile.gettempdir()) / "review_harness_streams" / tag
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HarnessError(
            f"Could not create the stream directory: {d}",
            cause=f"a disk error: {exc}",
            next_action="check the system temp (free space, permissions)",
        ) from exc
    return d


def _unlink_contract(path: Path, what: str) -> None:
    """Deleting a harness file — under the refusal contract (round 13)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise HarnessError(
            f"Could not delete {what}: {path}",
            cause=f"a disk error: {exc}",
            next_action="check the file permissions; the cycle state is intact — repeat the step",
        ) from exc


def gate_requests_path(run_dir: Path, round_no: int) -> Path:
    return run_dir / f"round_{round_no:02d}_gate_requests.json"


def gate_answers_path(run_dir: Path, round_no: int) -> Path:
    return run_dir / f"round_{round_no:02d}_gate_answers.json"


def _serve_gate_requests(req_path: Path, ans_path: Path, gate: Gate) -> None:
    """Deliver the forks to the operator; every answer is durable IMMEDIATELY.

    The answers file is rewritten after EVERY answer rather than at the end of the
    batch: a crash on the second of two questions must not lead to the first being
    asked again — two different answers from one operator to one question leave the
    cycle without an unambiguous state. On recovery only the unanswered are asked
    (matched by the question). The questions are deleted only after the whole batch.
    """
    try:
        requests = json.loads(read_text_contract(req_path, "the questions to the operator"))
        # A semantically corrupt item (one with no question) is the same class as
        # corrupt JSON. The checks are explicit if/raise rather than assert: under
        # python -O an assert disappears and takes its guarantee with it (round 7).
        if not isinstance(requests, list) or not all(
            isinstance(r, dict) and isinstance(r.get("question"), str) for r in requests
        ):
            raise ValueError("not a list of questions with a question field")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HarnessError(
            f"The file of questions to the operator is corrupted: {req_path}",
            cause=f"it does not read as a list of questions with a question field: {exc}",
            next_action="work out what development produced; the questions file is left as it is",
        ) from exc
    answers: list[dict] = []
    if ans_path.exists():
        try:
            answers = json.loads(read_text_contract(ans_path, "the operator's answers"))
            if not isinstance(answers, list) or not all(
                isinstance(a, dict)
                and isinstance(a.get("question"), str)
                and isinstance(a.get("answer"), str)
                for a in answers
            ):
                raise ValueError("not a list of question/answer records")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise HarnessError(
                f"The answers file is corrupted: {ans_path}",
                cause=f"it does not read as a list of question/answer records: {exc}",
                next_action="restore the file from the gate journal (gates.md) or delete it",
            ) from exc
    answered = {a["question"] for a in answers}
    for req in requests:
        if str(req["question"]) in answered:
            continue
        answer = gate.ask(
            str(req["question"]), kind=str(req.get("kind", "a fork")), record=False
        )
        answers.append({"question": req["question"], "answer": answer})
        # State comes first: the verbatim answer lands in the durable JSON, and only
        # then in the gates.md journal. A crash between the two does not breed a repeat
        # question (the JSON already knows the answer).
        _write_durably(ans_path, json.dumps(answers, ensure_ascii=False, indent=2))
        gate._catalog.append_gate_record(
            str(req["question"]), answer, kind=str(req.get("kind", "a fork"))
        )
    _unlink_contract(req_path, "the file of questions to the operator")


def _run_development(
    profile: machine_profile.MachineProfile, run_dir: Path, round_no: int
) -> None:
    argv = [
        part.replace("{run_dir}", str(run_dir)).replace("{round}", str(round_no))
        for part in profile.dev_argv
    ]
    # Development's silence is watched by the same window as the critic's: the
    # harness has no role whose death goes unrecognised. The critic's pass is
    # already written by this point — a crash of development loses nothing.
    # The returned (stdout, stderr) is unused: development's journal is streamed to
    # a file, and the outcomes arrive as an outcomes file.
    from .runcat import RunCatalog as _RC

    run_watched(
        argv,
        role="development",
        stream_path=stream_dir(run_dir) / f"round_{round_no:02d}_dev.log",
        detection_window_sec=profile.detection_window_sec,
        privacy_check=_RC(run_dir)._ensure_private,
        env_overrides=profile.env,
    )


def _ensure_entry_gate(catalog: RunCatalog, gate: Gate) -> None:
    """The entry gate: the operator confirms the description before the first pass."""
    state = catalog.restore()
    if state.entry_gate_confirmed:
        # The journal is a derivative of state: if its write failed after the flag, the
        # final pair is repaired from run.json (round 14).
        catalog.entry_journal_repair()
        return
    description = read_text_contract(
        catalog.run_dir / "task_description.md", "the task description"
    )
    # The answer is read by the same parser of the operator's word as the other
    # gates: «Да, подтверждаю описание» is agreement rather than a string mismatch
    # (one semantics for answers — thesis 14).
    confirmed, verbatim, actual_prompt = gate.ask_yes_no_verbatim(
        "Entry gate: confirm the task description of the first round "
        f"({catalog.run_dir / 'task_description.md'}, {len(description)} characters). "
        "The critic does not start without a confirmation. Do you confirm? (yes/no)",
        kind="the entry gate",
    )
    if not confirmed:
        # Refusal: there is no state — we journal the final pair and fail; asking again
        # on a restart is legitimate (the gate was not passed).
        catalog.append_gate_record(actual_prompt, verbatim, kind="the entry gate")
        raise HarnessError(
            "The entry gate was not passed: the operator did not confirm the description",
            cause=f"the operator answered no (verbatim: {verbatim!r})",
            next_action="correct the description after the operator's word and start the round again",
        )
    # STATE COMES FIRST: first the durable write of the flag with the verbatim
    # answer (one write — a crash asks nothing again), then the derived journal with
    # the actual text of the question, exactly once (rounds 12-13).
    catalog.mark_entry_confirmed(verbatim, actual_prompt)
    catalog.append_gate_record(actual_prompt, verbatim, kind="the entry gate")


def _operator_profile(repo_root: Path) -> tuple[str, int]:
    """The operator profile cache: (version, notification time)."""
    cache_path = repo_root / "docs" / "review" / "operator_profile.md"
    if not cache_path.exists():
        raise HarnessError(
            "The operator profile cache was not found",
            cause=f"{cache_path} was expected; without the profile there is no delivery and no notification time",
            next_action=(
                "refresh the cache from the graph's canon (get_operator_profile) with a development "
                "session, or put the file there by hand"
            ),
        )
    text = read_text_contract(cache_path, "the operator profile cache")
    m = _VERSION_RE.search(text)
    if m is None:
        # A cache with no version cannot be identified: silently recording "no version"
        # in the round's metadata would be a lie about the delivery the round ran under
        # (principle 1 — loudly, not with a stub).
        raise HarnessError(
            "The operator profile cache carries no version line",
            cause=f"{cache_path} has no \"version: ...\" line — the cache is corrupted or was never compiled",
            next_action="fetch the cache again from the graph's canon (get_operator_profile)",
        )
    return m.group(1), liveness.operator_notify_within_sec(text)


def _judged_snapshot(
    repo_root: Path, artifact_paths: list[str], run_dir: Path
) -> dict[str, str]:
    """Hashes of what IS JUDGED: the artifacts plus the task description. No git head.

    The snapshot defines both recovery ("what the critic saw") and automatic running
    ("was there an edit → is a new pass needed"), so its contents must coincide with
    the subject of judgement (round 4): the task description is judged by the critic,
    so refining it requires a new pass; the git head is not the subject — an unrelated
    commit must neither trigger a needless pass nor mask the absence of an edit. The
    head is written into the metadata separately, as provenance.
    """
    snapshot: dict[str, str] = {}
    for rel in artifact_paths:
        path = (repo_root / rel) if not Path(rel).is_absolute() else Path(rel)
        if not path.exists():
            raise HarnessError(
                f"An artifact of the run was not found: {rel}",
                cause="there is nothing to judge — the path from the run metadata does not exist",
                next_action="correct the artifact paths or the working copy",
            )
        h = hashlib.sha256()
        if path.is_dir():
            # Generated litter is not judged: a __pycache__ from a test run must not look
            # like an edit of the artifact (a false extra pass).
            files = sorted(
                p
                for p in path.rglob("*")
                if p.is_file()
                and "__pycache__" not in p.parts
                and p.suffix != ".pyc"
                and ".git" not in p.parts
            )
        else:
            files = [path]
        for f in files:
            name = str(f.relative_to(path) if path.is_dir() else f.name).encode()
            body = read_bytes_contract(f, f"artifact {rel}")
            # Boundaries are encoded by LENGTHS: a bare concatenation of name and content
            # gave one hash to different states (a file "a" holding "bc" and a file "ab"
            # holding "c" are the same bytes), and a rename together with an edit of the
            # beginning skipped the mandatory new pass (round 18).
            h.update(len(name).to_bytes(8, "big"))
            h.update(name)
            h.update(len(body).to_bytes(8, "big"))
            h.update(body)
        snapshot[rel] = h.hexdigest()
    # The critic's input in full: the task description AND the accompanying note — a
    # corrected instruction to the critic means a new input version and a new pass.
    for key, name in (
        ("__task_description__", "task_description.md"),
        ("__launch_note__", "launch_critic.md"),
    ):
        snapshot[key] = hashlib.sha256(
            read_bytes_contract(run_dir / name, name)
        ).hexdigest()
    return snapshot


def _git_head(repo_root: Path) -> str:
    """The repository head — or a loud refusal: a stub instead of provenance
    would make the metadata "complete" while the version is unrecoverable
    (principle 1).

    A legitimate absence of a repository is not a git error: a run outside a working
    copy honestly records "outside git" (the artifact snapshot remains full
    provenance); git errors with a repository that DOES exist are a loud refusal.
    """
    if next((p for p in [repo_root, *repo_root.parents] if (p / ".git").exists()), None) is None:
        return "outside-git"
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(
            "Could not obtain the repository's git head",
            cause=f"git is unavailable: {exc}",
            next_action="fix git on this machine — a round's provenance must be recorded",
        ) from exc
    if head.returncode != 0 or not head.stdout.strip():
        raise HarnessError(
            "git rev-parse HEAD returned no head",
            cause=f"code {head.returncode}: {(head.stderr or '').strip()[:200]}",
            next_action="check the state of the repository (HEAD, safe.directory) and repeat",
        )
    return head.stdout.strip()


def one_round(repo_root: Path, run_dir: Path, *, gate: Gate | None = None) -> int:
    """Run one round. Returns the number of the round that was run."""
    catalog = RunCatalog(run_dir)
    state = catalog.restore()
    profile = machine_profile.load(repo_root)
    profile.check_executables()
    gate = gate or Gate(catalog)

    # The catalogue's privacy is checked BEFORE the first stream: the critic's
    # partial output and development's journal are written directly, and if the
    # catalogue stopped being ignored between rounds, private text would land in a
    # publishable tree ahead of any refusal (round 4).
    catalog._ensure_private()

    _ensure_entry_gate(catalog, gate)

    profile_version, notify_sec = _operator_profile(repo_root)
    liveness.announce_window(profile.detection_window_sec, notify_sec, gate._say)
    if profile.allow_unsafe_critic_sandbox:
        # Break-glass is never quiet: every launch says so out loud.
        gate._say(
            "[ATTENTION] The machine profile has switched off the critic's read-only guard "
            "(allow_unsafe_critic_sandbox) — this launch has no guarantee that the critic "
            "does not write. A production run must not look like this."
        )

    resume = catalog.restore().resume_round
    if resume is not None:
        # A crash of the previous session: the pass was received, the outcomes are
        # missing. The critic is not replayed — analysis continues from the recorded text.
        round_no = resume
        gate._say(
            f"[cycle] Continuing round {round_no} from the recorded pass — "
            "the critic is not replayed."
        )
        # The round's metadata must exist (it is written before the pass); its absence
        # is a loud refusal rather than an analysis made blind.
        meta = catalog.read_round_meta(round_no)
        meta_path = run_dir / f"round_{round_no:02d}_pass_meta.json"
        # The profile version may have changed between the pass and the continuation:
        # the catalogue must tell the truth about BOTH halves of the round.
        recorded = meta["operator_profile_version"]
        if recorded != profile_version:
            meta["operator_profile_version_at_resume"] = profile_version
            _write_durably(meta_path, json.dumps(meta, ensure_ascii=False))
            gate._say(
                f"[cycle] The operator profile changed after the pass "
                f"({recorded} → {profile_version}); recorded in the round metadata."
            )
        # The artifact may have changed after the pass (development managed to edit
        # before the crash) — we compare the snapshot and say so out loud. The metadata
        # is complete by contract (read_round_meta checked it).
        recorded_snap = meta["artifact_snapshot"]
        current_snap = _judged_snapshot(repo_root, state.artifact_paths, run_dir)
        drifted = [k for k in recorded_snap if current_snap.get(k) != recorded_snap[k]]
        if drifted:
            gate._say(
                "[cycle] The artifact changed after the critic pass: "
                f"{', '.join(drifted)}. The analysis judges the recorded pass; "
                "the edits go into the next round."
            )
    else:
        round_no = catalog.restore().current_round
        snapshot = _judged_snapshot(repo_root, state.artifact_paths, run_dir)
        partial = stream_dir(run_dir) / f"round_{round_no:02d}_pass.partial"
        pass_text = critic.run_critic(
            profile,
            model=state.critic_model,
            launch_note=run_dir / "launch_critic.md",
            partial_path=partial,
            detection_window_sec=profile.detection_window_sec,
            privacy_check=catalog._ensure_private,
        )
        # The pass and its metadata (the delivery version, the snapshot of the artifact
        # version that was judged) are one state: the metadata lands before the pass
        # inside write_pass, and a crash between them is impossible by construction.
        catalog.write_pass(
            round_no,
            pass_text,
            extra_meta={
                "operator_profile_version": profile_version,
                "artifact_snapshot": snapshot,
                # Provenance, not the subject of judgement: the git head takes no part in the
                # comparison of snapshots (an unrelated commit is not an edit).
                "git_head": _git_head(repo_root),
            },
        )
        _unlink_contract(partial, "the partial pass file")

    req_path = gate_requests_path(run_dir, round_no)
    ans_path = gate_answers_path(run_dir, round_no)

    if req_path.exists():
        # A fork left hanging by the previous session: the operator answers BEFORE
        # development gets control again. A partly answered batch (a crash in the middle
        # of the questions) continues from the unanswered; a fully answered one (a crash
        # before the questions were deleted) asks nothing.
        _serve_gate_requests(req_path, ans_path, gate)

    _run_development(profile, run_dir, round_no)

    if req_path.exists():
        if ans_path.exists():
            raise HarnessError(
                f"Development filed questions after the answers of round {round_no} were written",
                cause="the contract is one batch of forks per round; a repeat filing is ambiguous",
                next_action="work out development's logic; the question and answer files are intact",
            )
        _serve_gate_requests(req_path, ans_path, gate)
        # A hanging semantic decision blocks movement on its own question until the
        # operator answers — the analysis is finished only now.
        _run_development(profile, run_dir, round_no)
        if req_path.exists():
            # The second development session filed questions AFTER the answers, together
            # with the outcomes — the round is not accepted (a finding of round 9).
            raise HarnessError(
                f"Development filed questions after the answers of round {round_no} (again)",
                cause="the contract is one batch of forks per round",
                next_action="work out development's logic; the files are intact",
            )

    # Development's journal moves from temp into the catalogue by a durable write
    # (with a privacy check) — the streams never touched the repository.
    dev_log_tmp = stream_dir(run_dir) / f"round_{round_no:02d}_dev.log"
    if dev_log_tmp.exists():
        _write_durably(
            run_dir / f"round_{round_no:02d}_dev.log",
            read_text_contract(dev_log_tmp, "the development journal"),
        )
        _unlink_contract(dev_log_tmp, "the temporary development journal")

    # Privacy is rechecked AFTER development: it may have edited .gitignore in the
    # course of ordinary work, and accepting outcomes written into a catalogue that
    # had become publishable would legitimise a leak silently.
    catalog._ensure_private()

    outcomes = run_dir / f"round_{round_no:02d}_outcomes.md"
    try:
        outcomes_body = outcomes.read_text(encoding="utf-8") if outcomes.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise HarnessError(
            f"Could not read the outcomes of round {round_no}",
            cause=f"a disk read error on {outcomes}: {exc}",
            next_action="check the permissions and the disk; the outcomes file is intact — repeat the step",
        ) from exc
    if outcomes_body.strip():
        # The outcomes were written by a headless role with a plain write_text: we
        # rewrite them through the durable API (fsync plus the privacy check) — the
        # cycle's state must not depend on another process's file cache (round 8).
        _write_durably(outcomes, outcomes_body)
        # A round is complete only after the HARNESS marks it so — after all of its
        # checks (hanging questions served, a repeat filing rejected, the outcomes
        # made durable again). A crash before the mark leaves the round running, and
        # a restart carries it through (round 14).
        catalog.mark_round_completed(round_no)
    if not outcomes_body.strip():
        # An empty outcomes file is an imitation of analysis, not analysis: not one
        # finding got an outcome, so the round does not count as complete.
        raise HarnessError(
            f"Development left no outcomes for round {round_no}"
            + ("" if not outcomes.exists() else " (the outcomes file is empty)"),
            cause="the development session ended with no substantive outcomes — no analysis happened",
            next_action=(
                f"the critic pass is intact ({run_dir / f'round_{round_no:02d}_pass.md'}); "
                "restart the round — the analysis continues from it, the critic is not replayed"
            ),
        )
    return round_no


def run_cycle(repo_root: Path, run_dir: Path, *, gate: Gate | None = None) -> int:
    """Run the cycle automatically: an edit of the artifact is a new round, by itself.

    A mechanical execution of the pair's rule "any edit = a new version = a new full
    pass", with no verdicts: after a round the harness compares the artifact with the
    snapshot the critic saw. Changed — development made edits, and the next pass
    starts WITHOUT the operator (the spec: the operator is needed only at the gates
    and the forks). Unchanged — there were no edits (a clean pass, or a disagreement
    with no edits): the cycle stops and waits for the operator's word; stopping is
    recommended by the critic in his pass and decided by the operator — the harness
    merely stops turning.

    Returns the number of the last round that was run.
    """
    catalog = RunCatalog(run_dir)
    gate = gate or Gate(catalog)
    while True:
        round_no = one_round(repo_root, run_dir, gate=gate)
        state = catalog.restore()
        recorded = catalog.read_round_meta(round_no)["artifact_snapshot"]
        current = _judged_snapshot(repo_root, state.artifact_paths, run_dir)
        if recorded == current:
            gate._say(
                f"[cycle] The artifact did not change after the analysis of round {round_no} — "
                "there were no edits, so no next pass is needed. The cycle waits for the operator's "
                "word (the verdict and the recommendation are in the critic pass)."
            )
            return round_no
        gate._say(
            f"[cycle] The artifact changed after the analysis of round {round_no} — "
            "an edit means a new version: starting the next full pass."
        )
