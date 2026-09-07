# SPDX-License-Identifier: Apache-2.0
"""B.10 Part B: the referential spec subject — resolution, its consumers, the seam.

The resolution helper is exercised against a REAL git repository (a mocked git would
test the mock, same doctrine as the coverage suite). The three consumers each get the
half they own: the watcher's pre-pass semantic-validity check (a failure is
environmental, the pass unspent), the coverage tool's resolved-byte fingerprinting,
and the audience seam — the preparation CLI writing artifact + provenance, and the
runner's channel-anchored preflight refusing a stale or substituted pair.
"""

import json
import subprocess
from pathlib import Path

import pytest

from assistant_memory.review import coverage, resolve, watcher
from assistant_memory.audience.run import provenance_problems


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


SPEC_V1 = "# Spec\n\n### E-1 — the rule *(tier: fact)*\n\nBody v1.\n"
SPEC_V2 = "# Spec\n\n### E-1 — the rule *(tier: fact)*\n\nBody v2, changed.\n"


@pytest.fixture
def repo(tmp_path: Path):
    """A real repository with two commits of a spec document; returns (root, base,
    commit) — the full 40-hex pair a referential subject carries."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    spec = root / "docs" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(SPEC_V1, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "spec v1")
    base = _git(root, "rev-parse", "HEAD")
    spec.write_text(SPEC_V2, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "spec v2")
    commit = _git(root, "rev-parse", "HEAD")
    return root, base, commit


# --- the helper itself ------------------------------------------------------


def test_resolve_serves_the_file_at_the_commit(repo):
    root, base, commit = repo
    assert resolve.resolve_spec_text(root, commit, "docs/spec.md") == SPEC_V2
    # the pair is immutable: the base still serves ITS bytes, unchanged by later work
    assert resolve.resolve_spec_text(root, base, "docs/spec.md") == SPEC_V1


def test_resolution_replays_to_identical_bytes(repo):
    """A-1's replay claim: a recorded full-oid pair resolves to identical bytes
    later — that immutability is the whole reason the strict form exists."""
    root, _base, commit = repo
    first = resolve.text_digest(resolve.resolve_spec_text(root, commit, "docs/spec.md"))
    again = resolve.text_digest(resolve.resolve_spec_text(root, commit, "docs/spec.md"))
    assert first == again


def test_resolution_failures_are_named_never_empty(repo):
    root, _base, commit = repo
    # a nonexistent id
    with pytest.raises(resolve.ResolutionRefused) as refusal:
        resolve.resolve_spec_text(root, "f" * 40, "docs/spec.md")
    assert any("names no object" in r for r in refusal.value.reasons)
    # a non-commit object id (the blob of the spec file)
    blob = _git(root, "rev-parse", f"{commit}:docs/spec.md")
    with pytest.raises(resolve.ResolutionRefused) as refusal:
        resolve.resolve_spec_text(root, blob, "docs/spec.md")
    assert any("not a commit" in r for r in refusal.value.reasons)
    # a path absent at the commit
    with pytest.raises(resolve.ResolutionRefused) as refusal:
        resolve.resolve_spec_text(root, commit, "docs/absent.md")
    assert any("does not resolve" in r for r in refusal.value.reasons)


def test_pair_problems_verifies_commits_and_diffability(repo):
    root, base, commit = repo
    assert resolve.pair_problems(root, base, commit) == []
    assert any("names no object" in r for r in resolve.pair_problems(root, "f" * 40, commit))
    blob = _git(root, "rev-parse", f"{commit}:docs/spec.md")
    assert any("not a commit" in r for r in resolve.pair_problems(root, base, blob))


# --- the watcher's pre-pass boundary (B-2) ----------------------------------


def _ref_artifact(seq, base, commit, path="docs/spec.md"):
    return {"seq": seq, "role": "development", "kind": "artifact",
            "payload": {"mode": "spec",
                        "artifact_ref": {"base": base, "commit": commit},
                        "path": path}}


def test_watcher_refuses_a_referential_subject_without_a_repo(repo):
    _root, base, commit = repo
    fault = watcher.subject_resolution_fault([_ref_artifact(1, base, commit)], 1, None)
    assert fault is not None and "--cwd" in fault


def test_watcher_passes_a_resolvable_subject_and_names_an_unresolvable_one(repo):
    root, base, commit = repo
    ok = watcher.subject_resolution_fault([_ref_artifact(1, base, commit)], 1, str(root))
    assert ok is None
    fault = watcher.subject_resolution_fault(
        [_ref_artifact(1, base, commit, path="docs/absent.md")], 1, str(root)
    )
    assert fault is not None and "pass unspent" in fault
    # code mode: a semantically invalid pair is caught at the same boundary
    bad_code = {"seq": 1, "role": "development", "kind": "artifact",
                "payload": {"mode": "code",
                            "artifact_ref": {"base": "f" * 40, "commit": commit}}}
    fault = watcher.subject_resolution_fault([bad_code], 1, str(root))
    assert fault is not None and "names no object" in fault


def test_watcher_verifies_the_base_of_a_referential_spec_subject(repo):
    """B-2 requires BOTH ids of the pair to resolve to commit objects, even though the
    spec text resolves from `commit` alone (finding
    b10-spec-base-semantic-validation-missing): an absent or non-commit base must be
    a named environmental refusal at the watcher, never a later coverage failure."""
    root, _base, commit = repo
    # an absent base: the id names no object
    absent = _ref_artifact(1, "f" * 40, commit)
    fault = watcher.subject_resolution_fault([absent], 1, str(root))
    assert fault is not None and "base" in fault and "names no object" in fault
    # a non-commit base: the blob of the spec file
    blob = _git(root, "rev-parse", f"{commit}:docs/spec.md")
    fault = watcher.subject_resolution_fault([_ref_artifact(1, blob, commit)], 1, str(root))
    assert fault is not None and "not a commit" in fault


def test_preparation_cli_refuses_a_bad_base(tmp_path, repo, monkeypatch):
    """The class comb's second instance: the preparation CLI is a resolution-boundary
    consumer too and refuses an unverifiable base before writing anything."""
    root, _base, commit = repo
    blob = _git(root, "rev-parse", f"{commit}:docs/spec.md")
    rc, run_dir = _prepare_run_dir(
        tmp_path, repo, monkeypatch, _ref_artifact(7, blob, commit)
    )
    assert rc == 2
    assert not (run_dir / "artifact.md").exists()


def test_watcher_form_checks_only_without_repo_for_code(repo):
    """Without a repository the watcher keeps the service's own posture for CODE
    subjects: form only, no invented verification."""
    _root, base, commit = repo
    code = {"seq": 1, "role": "development", "kind": "artifact",
            "payload": {"mode": "code", "artifact_ref": {"base": base, "commit": commit}}}
    assert watcher.subject_resolution_fault([code], 1, None) is None


# --- the coverage tool: fingerprint from RESOLVED bytes (B-2) ---------------


def test_coverage_cli_resolves_the_referential_subject(repo, capsys):
    root, base, commit = repo
    rc = coverage.main([
        "--mode", "spec", "--base", base, "--commit", commit,
        "--repo-root", str(root), "--spec-path", "docs/spec.md",
    ])
    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    # the digest is derived from the RESOLVED bytes — identical to what the inline
    # form of the same body would have produced (manifest identity is form-agnostic)
    assert manifest["inputs"]["spec_markdown"] == coverage._digest(SPEC_V2)
    assert any(r["locator"] == "E-1" for r in manifest["rows"])


def test_coverage_cli_refuses_both_subject_forms(repo, tmp_path):
    root, base, commit = repo
    local = tmp_path / "local_spec.md"
    local.write_text(SPEC_V2, encoding="utf-8")
    rc = coverage.main([
        "--mode", "spec", "--base", base, "--commit", commit,
        "--repo-root", str(root), "--spec-path", "docs/spec.md",
        "--spec-file", str(local),
    ])
    assert rc == 2


def test_coverage_cli_environmental_refusal_on_unresolvable_subject(repo, capsys):
    root, base, commit = repo
    rc = coverage.main([
        "--mode", "spec", "--base", base, "--commit", commit,
        "--repo-root", str(root), "--spec-path", "docs/absent.md",
    ])
    assert rc == 2
    assert "environmental refusal" in capsys.readouterr().err
    # the semantic pair check guards BOTH modes at this boundary
    rc = coverage.main([
        "--mode", "code", "--base", "f" * 40, "--commit", commit,
        "--repo-root", str(root),
    ])
    assert rc == 2


# --- the audience seam (B-4): preparation CLI + runner preflight ------------


def _prepare_run_dir(tmp_path, repo, monkeypatch, payload_message):
    """Run the preparation CLI against a fake channel read; returns the run dir."""
    root, _base, _commit = repo
    run_dir = tmp_path / "run"
    token_file = tmp_path / "token"
    token_file.write_text("t", encoding="utf-8")
    monkeypatch.setattr(
        resolve, "_fetch_artifact_message", lambda *a, **k: payload_message
    )
    rc = resolve.main([
        "--base-url", "http://x", "--review-id", "rev-1", "--token-file",
        str(token_file), "--artifact-seq", "7", "--repo-root", str(root),
        "--run-dir", str(run_dir),
    ])
    return rc, run_dir


def test_preparation_cli_writes_artifact_and_provenance(tmp_path, repo, monkeypatch):
    _root, base, commit = repo
    rc, run_dir = _prepare_run_dir(
        tmp_path, repo, monkeypatch, _ref_artifact(7, base, commit)
    )
    assert rc == 0
    assert (run_dir / "artifact.md").read_text(encoding="utf-8") == SPEC_V2
    record = json.loads((run_dir / resolve.PROVENANCE_FILENAME).read_text("utf-8"))
    assert record == {
        "review_id": "rev-1", "artifact_seq": 7, "commit": commit,
        "path": "docs/spec.md", "digest": resolve.text_digest(SPEC_V2),
    }


def test_preparation_cli_refuses_an_inline_artifact(tmp_path, repo, monkeypatch):
    inline = {"seq": 7, "kind": "artifact",
              "payload": {"mode": "spec", "bundle": {"spec_markdown": "# deck\n"}}}
    rc, run_dir = _prepare_run_dir(tmp_path, repo, monkeypatch, inline)
    assert rc == 2
    assert not (run_dir / "artifact.md").exists()


def _preflight(run_dir, artifact_text, messages, seq=7):
    return provenance_problems(
        review_dir=run_dir, artifact_text=artifact_text,
        review_id="rev-1", artifact_seq=seq, messages=messages,
    )


def test_preflight_accepts_the_cli_prepared_directory(tmp_path, repo, monkeypatch):
    """B-4's positive half at integration level: the CLI's output feeds a counted
    run — the preflight over the exact directory the CLI wrote finds nothing."""
    _root, base, commit = repo
    _rc, run_dir = _prepare_run_dir(
        tmp_path, repo, monkeypatch, _ref_artifact(7, base, commit)
    )
    text = (run_dir / "artifact.md").read_text(encoding="utf-8")
    assert _preflight(run_dir, text, [_ref_artifact(7, base, commit)]) == []


def test_preflight_refuses_a_consistent_but_wrong_pair(tmp_path, repo, monkeypatch):
    """B-4's REQUIRED negative: both the artifact file AND the provenance record are
    replaced — a preparation run against a wrong repository state records an honest
    digest of the wrong bytes. The pair is self-consistent; only the comparison
    against the CHANNEL's declared ref catches it."""
    root, base, commit = repo
    _rc, run_dir = _prepare_run_dir(
        tmp_path, repo, monkeypatch, _ref_artifact(7, base, commit)
    )
    # substitute BOTH: the file (v1 bytes) and a record honestly describing them
    # (prepared from the stale base commit)
    (run_dir / "artifact.md").write_text(SPEC_V1, encoding="utf-8")
    (run_dir / resolve.PROVENANCE_FILENAME).write_text(json.dumps({
        "review_id": "rev-1", "artifact_seq": 7, "commit": base,
        "path": "docs/spec.md", "digest": resolve.text_digest(SPEC_V1),
    }), encoding="utf-8")
    problems = _preflight(run_dir, SPEC_V1, [_ref_artifact(7, base, commit)])
    assert problems and any("расходится с каналом" in p for p in problems)


def test_preflight_refuses_a_stale_artifact_file(tmp_path, repo, monkeypatch):
    """The file alone substituted: the record still matches the channel, the file no
    longer matches the record."""
    _root, base, commit = repo
    _rc, run_dir = _prepare_run_dir(
        tmp_path, repo, monkeypatch, _ref_artifact(7, base, commit)
    )
    (run_dir / "artifact.md").write_text(SPEC_V1, encoding="utf-8")
    problems = _preflight(run_dir, SPEC_V1, [_ref_artifact(7, base, commit)])
    assert problems and any("подменён или устарел" in p for p in problems)


def test_preflight_requires_the_record_for_a_referential_subject(tmp_path, repo, monkeypatch):
    _root, base, commit = repo
    run_dir = tmp_path / "manual"
    run_dir.mkdir()
    (run_dir / "artifact.md").write_text(SPEC_V2, encoding="utf-8")
    problems = _preflight(run_dir, SPEC_V2, [_ref_artifact(7, base, commit)])
    assert problems and any("записи происхождения" in p for p in problems)


def test_preflight_refuses_a_valid_json_non_object_record(tmp_path, repo, monkeypatch):
    """A provenance record of [] or null is valid JSON and used to crash the preflight
    with AttributeError instead of refusing by name (finding
    b10-provenance-valid-json-not-object-refusal) — the refusal path must survive any
    record shape."""
    _root, base, commit = repo
    _rc, run_dir = _prepare_run_dir(
        tmp_path, repo, monkeypatch, _ref_artifact(7, base, commit)
    )
    text = (run_dir / "artifact.md").read_text(encoding="utf-8")
    for bad in ("[]", "null", '"string"'):
        (run_dir / resolve.PROVENANCE_FILENAME).write_text(bad, encoding="utf-8")
        problems = _preflight(run_dir, text, [_ref_artifact(7, base, commit)])
        assert problems and any("не объект" in p for p in problems), bad


def test_preflight_checks_an_inline_subject_by_digest(tmp_path):
    run_dir = tmp_path / "inline"
    run_dir.mkdir()
    inline_msg = {"seq": 7, "kind": "artifact",
                  "payload": {"mode": "spec", "bundle": {"spec_markdown": "# deck\n"}}}
    assert _preflight(run_dir, "# deck\n", [inline_msg]) == []
    problems = _preflight(run_dir, "# other deck\n", [inline_msg])
    assert problems and any("не совпадает" in p for p in problems)


def test_preflight_refuses_when_the_channel_has_no_such_artifact(tmp_path):
    run_dir = tmp_path / "empty"
    run_dir.mkdir()
    problems = _preflight(run_dir, "x", [], seq=9)
    assert problems and any("не содержит артефакта" in p for p in problems)
