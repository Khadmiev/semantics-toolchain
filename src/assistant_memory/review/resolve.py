# SPDX-License-Identifier: Apache-2.0
"""B.10 B-2/B-4: resolution of a referential review subject — the ONE helper.

A referential subject is a git reference: ``artifact_ref`` (full 40-hex ``base`` and
``commit``) plus, in spec mode, a repository-relative ``path`` to the document. Only
components that hold a repository at their working directory resolve bytes; the
service NEVER does (the production app image deliberately ships without ``.git``, and
every server-side check is form-level). This module is that one resolution helper,
with three consumers (B-4's rationale: the duplicated-logic residue Part C fixed for
elision must not be re-created here):

- the **watcher** — semantic validity of the current subject before a pass is spent
  (a failure routes environmentally: pass unspent, reason named on the channel);
- the **coverage tool** — the manifest's ``inputs.spec_markdown`` digest is computed
  from the RESOLVED bytes, so manifest identity is unchanged between subject forms;
- the **audience run-directory preparation** — the standalone CLI below (B-4): it
  takes (review id, artifact seq), fetches that artifact message from the channel's
  read endpoint, verifies the declared ref and path against it, resolves the bytes,
  and writes the run directory's artifact file plus a provenance record beside it.
  The CLI — not the preparing session's memory — supplies the subject.

Failure mode (B-2, copying the launch-profile doctrine): a resolution failure —
commit absent, a non-commit object, an undiffable pair, path absent at the commit —
is a NAMED refusal (``ResolutionRefused``), never a silent empty subject.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

#: The provenance record the CLI writes beside the run directory's artifact file, and
#: the runner's preflight verifies against the CHANNEL (B-4: a record and file that
#: live in one directory travel together and prove only each other — the anchor is
#: the channel, not the directory).
PROVENANCE_FILENAME = "artifact_provenance.json"


class ResolutionRefused(Exception):
    """A referential subject that cannot be resolved, with every reason named."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


def text_digest(text: str) -> str:
    """The provenance digest of a resolved subject: full sha256 over UTF-8 bytes.

    Full, not the manifest machinery's 16-hex truncation: the record exists to bind
    one file to one channel message, and truncating buys nothing here.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo_root: str | Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], shell=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", cwd=str(repo_root), timeout=60,
    )
    # stdout is returned RAW: for `git show` it IS the resolved subject, and stripping
    # it would silently alter the bytes the digest is derived from. Callers that read
    # a one-line answer (`cat-file -t`) strip for themselves.
    return proc.returncode, proc.stdout or "", (proc.stderr or "").strip()


def commit_problems(repo_root: str | Path, oid, *, name: str) -> list[str]:
    """A 40-hex form is not yet a subject (B-2): the id may name no local object, or a
    non-commit. Verified here, at the boundary that holds a repository."""
    if not isinstance(oid, str) or not oid.strip():
        return [f"{name}: no object id supplied"]
    code, out, err = _git(repo_root, "cat-file", "-t", oid.strip())
    if code != 0:
        return [f"{name}: {oid} names no object in this repository ({err or 'unknown'})"]
    if out.strip() != "commit":
        return [f"{name}: {oid} is a {out.strip()} object, not a commit"]
    return []


def pair_problems(repo_root: str | Path, base, commit) -> list[str]:
    """Code-mode semantic validity: both ids are commits and the pair yields a
    derivable diff."""
    problems = commit_problems(repo_root, base, name="base")
    problems += commit_problems(repo_root, commit, name="commit")
    if problems:
        return problems
    code, _out, err = _git(repo_root, "diff", "--stat", f"{base}..{commit}")
    if code != 0:
        problems.append(f"the pair {base}..{commit} yields no derivable diff ({err})")
    return problems


def resolve_spec_text(repo_root: str | Path, commit, path) -> str:
    """The referential spec subject's bytes: the file at ``path`` at ``commit``.

    Raises ``ResolutionRefused`` with every problem named — commit absent or not a
    commit, path absent at that commit — never returns an empty stand-in.
    """
    problems = commit_problems(repo_root, commit, name="commit")
    if not problems:
        if not isinstance(path, str) or not path.strip():
            problems.append("path: no repository-relative path supplied")
        else:
            code, out, err = _git(
                repo_root, "show", f"{commit}:{path.strip().replace(chr(92), '/')}"
            )
            if code != 0:
                problems.append(
                    f"path {path!r} does not resolve at commit {commit} ({err})"
                )
            else:
                return out
    raise ResolutionRefused(problems)


def is_referential_spec_subject(payload: dict) -> bool:
    """Does this artifact payload carry the referential spec form (B-1)?

    Keyed on the DECLARED fields, not on the bundle's absence: the wire refuses a
    message carrying both forms, so on an accepted channel the two are exclusive.
    """
    ref = payload.get("artifact_ref") or {}
    return (
        payload.get("mode") == "spec"
        and not payload.get("intent_summary")
        and isinstance(ref, dict)
        and bool(ref.get("commit"))
        and bool(payload.get("path"))
    )


def subject_problems(repo_root: str | Path, payload: dict) -> list[str]:
    """Semantic validity of an artifact message's subject, both modes (B-2).

    For a referential spec subject: commit is a commit and the path resolves at it.
    For a code subject with a usable ref: both ids are commits and the pair is
    diffable. Anything else (an inline bundle, a legacy inline diff without a usable
    ref) has no reference to verify — no problems, nothing invented.
    """
    ref = payload.get("artifact_ref") or {}
    if is_referential_spec_subject(payload):
        # BOTH ids of the pair must resolve to commit objects (B-2), even though the
        # spec text itself resolves from `commit` alone: `base` participates in
        # manifest identity and diff derivation, and skipping it here let an absent
        # or non-commit base spend nothing at this boundary only to fail later at
        # coverage derivation (finding b10-spec-base-semantic-validation-missing).
        problems = commit_problems(repo_root, ref.get("base"), name="base")
        try:
            resolve_spec_text(repo_root, ref.get("commit"), payload.get("path"))
        except ResolutionRefused as refusal:
            problems += refusal.reasons
        return problems
    if payload.get("mode") == "code" and ref.get("base") and ref.get("commit"):
        return pair_problems(repo_root, ref.get("base"), ref.get("commit"))
    return []


# --- the standalone CLI: audience run-directory preparation (B-4) ----------------


def _fetch_artifact_message(base_url: str, review_id: str, token: str,
                            artifact_seq: int, timeout: float) -> dict:
    import httpx

    url = f"{base_url.rstrip('/')}/reviews/{review_id}/messages"
    headers = {"Authorization": f"Bearer {token}"}
    messages: list[dict] = []
    after = 0
    with httpx.Client(timeout=timeout) as client:
        while True:
            resp = client.get(url, params={"after": after, "wait": 0}, headers=headers)
            resp.raise_for_status()
            batch = resp.json().get("messages") or []
            if not batch:
                break
            messages.extend(batch)
            after = max(m["seq"] for m in batch)
    for m in messages:
        if m.get("seq") == artifact_seq and m.get("kind") == "artifact":
            return m
    raise ResolutionRefused(
        [f"the channel has no artifact message at seq {artifact_seq}"]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "B.10 B-4: prepare an audience run directory's artifact from a REFERENTIAL "
            "spec subject. Fetches the artifact message from the channel, verifies the "
            "declared ref and path against it, resolves the bytes from the repository, "
            "and writes the artifact file plus its provenance record. The CLI — not "
            "the preparing session's memory — supplies the subject."
        )
    )
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--review-id", required=True)
    ap.add_argument("--token-file", required=True,
                    help="file holding a token whose credential can read the review")
    ap.add_argument("--artifact-seq", type=int, required=True)
    ap.add_argument("--repo-root", default=".",
                    help="the repository the referential subject resolves against")
    ap.add_argument("--run-dir", required=True,
                    help="the audience run directory: artifact.md and the provenance "
                         "record are written here")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    try:
        message = _fetch_artifact_message(
            args.base_url, args.review_id, token, args.artifact_seq, args.timeout
        )
        payload = message.get("payload") or {}
        if not is_referential_spec_subject(payload):
            raise ResolutionRefused(
                [
                    f"artifact seq {args.artifact_seq} does not carry a referential "
                    "spec subject — an inline artifact is prepared as before (its text "
                    "is in the message); this CLI resolves references only"
                ]
            )
        ref = payload["artifact_ref"]
        # The same both-ids rule as the watcher's preflight (B-2; class comb of
        # finding b10-spec-base-semantic-validation-missing): this CLI is a
        # resolution-boundary consumer too, and a run directory prepared over an
        # unverified base would carry the defect into the audience run.
        base_problems = commit_problems(args.repo_root, ref.get("base"), name="base")
        if base_problems:
            raise ResolutionRefused(base_problems)
        text = resolve_spec_text(args.repo_root, ref["commit"], payload["path"])
    except ResolutionRefused as refusal:
        print("run directory NOT prepared — environmental refusal:", file=sys.stderr)
        for reason in refusal.reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 2

    # ONE path contract shared by preparer and reader: the audience layout fixes the
    # artifact file name by design, and this CLI writes exactly what `load_review`
    # reads. A configurable name here prepared directories the runner's provenance
    # preflight then rejected (finding b10-audience-artifact-name-unusable).
    from assistant_memory.audience.layout import ARTIFACT

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = run_dir / ARTIFACT
    artifact_path.write_text(text, encoding="utf-8")
    record = {
        "review_id": args.review_id,
        "artifact_seq": args.artifact_seq,
        "commit": ref["commit"],
        "path": payload["path"],
        "digest": text_digest(text),
    }
    (run_dir / PROVENANCE_FILENAME).write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"wrote {artifact_path} ({len(text)} chars) and {PROVENANCE_FILENAME}")
    print(json.dumps(record, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
