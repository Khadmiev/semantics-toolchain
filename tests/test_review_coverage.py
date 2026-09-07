# SPDX-License-Identifier: Apache-2.0
"""Coverage manifest tool — pure-core unit tests (B.6 T1-4 / Q-3).

The manifest is the review's coverage DENOMINATOR, so the properties under test are the
ones that make it trustworthy rather than merely present: every element heading parses (a
silently skipped heading is an element missing from the denominator while the manifest
still looks complete), row ids are content-addressed so the same surface keeps its id
across versions, the blind edge is exactly one ordinary row, and an oversized manifest
REFUSES instead of truncating.

Git-backed derivation is exercised against a real throwaway repository — the tool's whole
claim is that it reads the ref pair mechanically, and a mocked git would test the mock.
"""

import subprocess
from pathlib import Path

import pytest

from assistant_memory.review import coverage


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repository with one commit of history to diff against."""
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    (root / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "pkg" / "core.py").write_text(
        "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n", encoding="utf-8"
    )
    (root / "src" / "pkg" / "user.py").write_text(
        "from pkg.core import alpha\n\n\ndef use():\n    return alpha()\n", encoding="utf-8"
    )
    (root / "tests" / "test_core.py").write_text(
        "from pkg import core\n\n\ndef test_alpha():\n    assert core.alpha() == 1\n",
        encoding="utf-8",
    )
    (root / "doc.md").write_text("# Title\n\ntext\n\n## Section A\n\nmore\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _commit_change(repo: Path, path: str, text: str) -> tuple[str, str]:
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / path).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return base, _git(repo, "rev-parse", "HEAD").strip()


# --- element parsing: the invariant that matters most ----------------------


def _engaged(manifest) -> set[str]:
    """The repository paths the manifest says the spec engages.

    Spec mode's denominator is SEMANTIC (operator ruling 2026-07-22): a path or symbol the
    spec body names no longer gets a row of its own, because where a decision will later sit
    in the tree is the code review's question. What still proves the body was parsed and its
    mentions resolved is the scope-divergence row each engaged-but-undeclared path produces —
    so that is what these tests assert, at file grain.
    """
    return {
        r["locator"].split("divergence:undeclared:", 1)[1]
        for r in manifest["rows"]
        if r["kind"] == "divergence" and r["locator"].startswith("divergence:undeclared:")
    }


def test_every_h3_heading_must_parse_into_an_element_id():
    """A heading the pattern skips is an element ABSENT from the denominator while the
    manifest still presents itself as complete — the exact failure that dropped fourteen
    elements from an earlier bundle builder."""
    with pytest.raises(coverage.ManifestError) as exc:
        coverage.spec_elements("### P-1 — fine\n\n### a heading with no id\n")
    assert "stable element id" in str(exc.value)


def test_digit_bearing_id_prefixes_parse():
    """`T1-*` / `T2-*` are the ids an alphabetic-prefix-only pattern silently lost."""
    ids = coverage.spec_elements(
        "### S-1 — scope\n### T1-4 — manifest\n### T2-3 — triage\n### Q-5 — settled\n"
    )
    assert ids == ["S-1", "T1-4", "T2-3", "Q-5"]


def test_duplicate_element_ids_refused():
    with pytest.raises(coverage.ManifestError):
        coverage.spec_elements("### P-1 — one\n### P-1 — two\n")


def test_struck_element_heading_still_parses():
    """A superseded element is struck, never renumbered — so its id must still be read."""
    assert coverage.spec_elements("### ~~R-1~~ — superseded\n") == ["R-1"]


# --- row identity ----------------------------------------------------------


def test_row_id_is_content_addressed_not_positional():
    """Two manifests must agree on the id of any row still denoting the same thing — that
    is what makes 'a finding landed on a row previously marked clean' well defined."""
    first = coverage.row_id_for("code", "src/pkg/core.py::alpha")
    second = coverage.row_id_for("code", "src/pkg/core.py::alpha")
    assert first == second
    assert first != coverage.row_id_for("code", "src/pkg/core.py::beta")
    # kind participates: an element `X-1` and a code path `X-1` are different rows
    assert coverage.row_id_for("element", "X-1") != coverage.row_id_for("code", "X-1")


def test_blind_edge_row_id_is_the_fixed_literal():
    assert coverage.row_id_for("blind_edge", "anything") == "hunt-by-name"


# --- code mode -------------------------------------------------------------


# --- the blind edge --------------------------------------------------------


# --- spec mode -------------------------------------------------------------


def test_spec_mode_reach_comes_from_the_body_not_the_declared_scope(repo: Path):
    """A curated scope that forgets a file would produce a manifest omitting exactly that
    file — so reach rows are PARSED out of the spec body instead."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec lives here\n")
    body = (
        "### S-1 — scope\n\nThis change rewrites `src/pkg/user.py` and the `alpha` helper.\n"
    )
    manifest = coverage.build_manifest(
        mode="spec",
        base=base,
        commit=commit,
        repo_root=repo,
        spec_markdown=body,
        declared_scope=["doc.md"],
    )
    assert "S-1" in [r["locator"] for r in manifest["rows"]]  # the element row
    engaged = _engaged(manifest)
    assert "src/pkg/user.py" in engaged  # mentioned path, never declared
    assert "src/pkg/core.py" in engaged  # mentioned symbol, resolved to its definition


def test_spec_mode_reports_scope_divergence_both_ways(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec",
        base=base,
        commit=commit,
        repo_root=repo,
        spec_markdown="### S-1 — scope\n\nTouches `src/pkg/user.py`.\n",
        declared_scope=["tests/test_core.py"],
    )
    divergences = [r["locator"] for r in manifest["rows"] if r["kind"] == "divergence"]
    assert "divergence:undeclared:src/pkg/user.py" in divergences
    assert "divergence:unengaged:tests/test_core.py" in divergences


def test_missing_declared_scope_becomes_a_row_not_a_silent_skip(repo: Path):
    """An unrun check that leaves no trace is indistinguishable from a check that passed."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec",
        base=base,
        commit=commit,
        repo_root=repo,
        spec_markdown="### S-1 — scope\n\nnothing named\n",
        declared_scope=None,
    )
    assert any(
        r["locator"] == "divergence:unchecked:no-declared-scope-supplied"
        for r in manifest["rows"]
    )


def test_spec_mode_requires_the_spec_body(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nx\n")
    with pytest.raises(coverage.ManifestError):
        coverage.build_manifest(mode="spec", base=base, commit=commit, repo_root=repo)


# --- refusals --------------------------------------------------------------


# --- verifiability ---------------------------------------------------------


# --- high-stakes marking ---------------------------------------------------


SPEC_WITH_TIERS = """# A spec

### A-1 — An operator decision *(tier: operator_decision)*

Statement.

### A-2 — A mixed tier *(tier: operator_decision for the right; llm_judgment for naming)*

Statement.

### A-3 — Ruled in by the operator in words *(tier: llm_judgment; scope-in by operator ruling)*

Statement.

### A-4 — Plain judgment *(tier: llm_judgment)*

Statement.

### A-5 — Watcher liveness: crash surfacing *(tier: llm_judgment)*

Statement.
"""


def test_load_bearing_elements_are_derived_from_the_spec_itself():
    """B.7: the list decides where a bare `reviewed-clean` is refused, and the hand-kept
    version fell behind the spec four times in one review while still looking complete."""
    derived = coverage.derive_high_stakes(SPEC_WITH_TIERS)
    assert derived == ["A-1", "A-2", "A-3", "A-5"]
    assert "A-4" not in derived, "a plain llm_judgment element is not load-bearing"


def test_derived_load_bearing_list_is_unioned_with_the_supplied_one(repo: Path):
    """The derivation is a FLOOR, not a ceiling: an author may still add to it, and what
    they cannot do any more is silently fall below it."""
    (repo / "spec.md").write_text(SPEC_WITH_TIERS, encoding="utf-8")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo,
        spec_markdown=SPEC_WITH_TIERS, high_stakes=["A-4"],
    )
    flagged = {r["locator"] for r in manifest["rows"] if r.get("high_stakes")}
    assert flagged == {"A-1", "A-2", "A-3", "A-4", "A-5"}


def test_a_spec_manifest_marks_its_operator_decisions_without_being_told(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo,
        spec_markdown=SPEC_WITH_TIERS,
    )
    flagged = {r["locator"] for r in manifest["rows"] if r.get("high_stakes")}
    assert "A-1" in flagged and "A-4" not in flagged


# --- regressions from the B.6 implementation review ------------------------


def test_a_cited_document_is_engaged_without_becoming_section_rows(repo: Path):
    """Citing a document must not cost what CHANGING it costs. Expanding every cited doc
    into its heading tree put three referenced designs into 104 of one review's 278 rows —
    for documents the change did not touch — and that inflation is what made the report
    unanswerable in practice. The citation is still measured, at file grain."""
    base, commit = _commit_change(repo, "src/pkg/core.py", "def alpha():\n    return 3\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nSee `doc.md`.\n", declared_scope=[],
    )
    assert [r for r in manifest["rows"] if r["kind"] == "markdown"] == []
    assert "doc.md" in _engaged(manifest)


def test_input_digests_change_only_with_their_input(repo: Path):
    """A digest that moved with nothing else is what tells two honest parties WHICH input
    diverged — so it must track its own input and nothing else."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    body = "### S-1 — scope\n\nTouches `doc.md`.\n"
    first = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, spec_markdown=body,
        declared_scope=["doc.md"],
    )
    same = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, spec_markdown=body,
        declared_scope=["doc.md"],
    )
    moved = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, spec_markdown=body,
        declared_scope=["doc.md", "src/pkg/core.py"],
    )
    assert first["inputs"] == same["inputs"]
    assert first["manifest_id"] == same["manifest_id"]
    assert moved["inputs"]["declared_scope"] != first["inputs"]["declared_scope"]
    assert moved["inputs"]["spec_markdown"] == first["inputs"]["spec_markdown"]
    assert moved["manifest_id"] != first["manifest_id"]


def test_a_qualified_symbol_mention_resolves_to_its_definition(repo: Path):
    """A spec discussing `Core.alpha` or `pkg.core.alpha` names a code symbol just as
    plainly as one writing the bare name — and the manifest CLAIMS to cover every symbol the
    spec names, so resolving only bare identifiers left those mentions out of the
    denominator."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `pkg.core.alpha` and `Core.alpha`.\n",
        declared_scope=[],
    )
    assert "src/pkg/core.py" in _engaged(manifest)


def test_call_notation_and_subscripts_resolve_by_prefix_rule(repo: Path):
    """F33 then F37 each added one more mention SHAPE while the next was already in the
    text. The rule now takes the leading dotted-identifier of any backticked span, so bare,
    dotted, call and subscript notation are one rule with no list to keep extending."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    for mention in ("`alpha()`", "`core.alpha()`", "`pkg.core.alpha(x, y)`", "`alpha`"):
        manifest = coverage.build_manifest(
            mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
            spec_markdown=f"### S-1 — scope\n\nChanges {mention}.\n",
            declared_scope=[],
        )
        assert "src/pkg/core.py" in _engaged(manifest), mention

    # ...and a qualifier that names nothing real resolves to nothing, rather than falling
    # back to every symbol carrying that leaf (there is no class `Core` anywhere).
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `Core.alpha()`.\n", declared_scope=[],
    )
    assert "src/pkg/core.py" not in _engaged(manifest)


def test_a_backticked_span_that_is_not_a_symbol_yields_nothing(repo: Path):
    """The prefix rule must not turn arbitrary code spans into rows."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nSet `--max-rows 5` and `{\"a\": 1}`.\n",
        declared_scope=[],
    )
    assert _engaged(manifest) == {"doc.md"}  # the spec file itself, nothing invented
    # Asserted on the RESOLVER as well: no code row survives in spec mode, so a row-level
    # check here would pass whatever the prefix rule did — a test that cannot fail.
    body = "### S-1 — scope\n\nSet `--max-rows 5` and `{\"a\": 1}`.\n"
    assert coverage._mentioned_symbols(repo, commit, body) == []


def test_a_qualified_mention_yields_the_qualified_locator(repo: Path):
    """Grepping the leaf alone answers `Store.load()` with every `load` in the repository
    and emits the imprecise `path::load` — over-covering with rows the spec never meant, and
    unable to name the one it did."""
    (repo / "src" / "pkg" / "store.py").write_text(
        "class Store:\n    def load(self):\n        return 1\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add store")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    # Both candidates live in ONE file, so file-grain engagement cannot tell them apart:
    # this distinction is asserted where it still exists, on the resolver itself.
    resolved = coverage._mentioned_symbols(
        repo, commit, "### S-1 — scope\n\nChanges `Store.load()`.\n"
    )
    assert ("src/pkg/store.py", "Store.load") in resolved
    assert ("src/pkg/store.py", "load") not in resolved


def test_an_extensionless_repository_path_is_reached(repo: Path):
    """An extension whitelist can never recognise `Dockerfile` or `Makefile`, and extending
    it one name at a time is the strategy this module already abandoned once. Membership in
    the commit's own tree is the correct test."""
    (repo / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add dockerfile")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nAlso touches `Dockerfile`.\n", declared_scope=[],
    )
    assert "Dockerfile" in _engaged(manifest)


def test_a_qualifier_that_does_not_match_the_file_yields_no_row(repo: Path):
    """The qualifier both NAMES and EXCLUDES: `Store.load()` must not answer with every
    `load` in the repository, so a file that defines neither `Store.load` nor a matching
    module path gets no row at all."""
    (repo / "src" / "pkg" / "store.py").write_text(
        "class Store:\n    def load(self):\n        return 1\n", encoding="utf-8"
    )
    (repo / "src" / "pkg" / "other.py").write_text(
        "class Other:\n    def load(self):\n        return 2\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two loads")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `Store.load()`.\n", declared_scope=[],
    )
    engaged = _engaged(manifest)
    assert "src/pkg/store.py" in engaged
    assert "src/pkg/other.py" not in engaged


def test_a_module_qualified_mention_still_resolves(repo: Path):
    """The other way a qualifier can be true of a file: it is the module path, and the
    symbol itself is module-level with no dotted name."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `pkg.core.alpha()`.\n", declared_scope=[],
    )
    assert "src/pkg/core.py" in _engaged(manifest)


def test_an_extensionless_path_named_in_prose_is_reached(repo: Path):
    """The one shape the regexes cannot reach even in prose, because it looks like a word."""
    (repo / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add makefile")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nThis change also updates Makefile.\n",
        declared_scope=[],
    )
    assert "Makefile" in _engaged(manifest)


def test_duplicate_extensionless_basenames_all_resolve_deterministically(repo: Path):
    """Mapping basename -> path over a SET picked an arbitrary winner: it under-covered the
    other locations and, far worse, made the row set depend on set iteration order —
    breaking the reproducibility the critic's verification rests on."""
    (repo / "services").mkdir()
    (repo / "Dockerfile").write_text("FROM python\n", encoding="utf-8")
    (repo / "services" / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "two dockerfiles")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    body = "### S-1 — scope\n\nThis change also updates Dockerfile.\n"
    first = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, spec_markdown=body,
        declared_scope=[],
    )
    engaged = _engaged(first)
    assert "Dockerfile" in engaged
    assert "services/Dockerfile" in engaged
    again = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, spec_markdown=body,
        declared_scope=[],
    )
    assert first["manifest_id"] == again["manifest_id"]


def test_a_qualifier_matching_only_as_a_string_suffix_is_rejected(repo: Path):
    """`dotted.endswith("Store.load")` is also true of `OtherStore.load` — a definition the
    spec never named. Segments, not characters."""
    (repo / "src" / "pkg" / "store.py").write_text(
        "class Store:\n    def load(self):\n        return 1\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add store")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `OtherStore.load()`.\n", declared_scope=[],
    )
    assert not any(
        loc.startswith("src/pkg/store.py") for loc in
        [r["locator"] for r in manifest["rows"]]
    )


def test_a_module_qualified_mention_excludes_the_same_class_elsewhere(repo: Path):
    """`pkg.store.Store.load()` was satisfied by `Store.load` in ANY module — the qualifier
    excluded a same-named method on a different class but not the same class in a different
    place, which is the very distinction a fully-qualified mention exists to draw."""
    (repo / "src" / "pkg" / "store.py").write_text(
        "class Store:\n    def load(self):\n        return 1\n", encoding="utf-8"
    )
    (repo / "src" / "pkg" / "mirror.py").write_text(
        "class Store:\n    def load(self):\n        return 2\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "same class twice")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `pkg.store.Store.load()`.\n",
        declared_scope=[],
    )
    engaged = _engaged(manifest)
    assert "src/pkg/store.py" in engaged
    assert "src/pkg/mirror.py" not in engaged


def test_a_symbol_only_qualifier_still_admits_any_module(repo: Path):
    """No leftover segments means the spec named a class, not a location — narrowing by
    module there would drop the row the spec actually meant."""
    (repo / "src" / "pkg" / "store.py").write_text(
        "class Store:\n    def load(self):\n        return 1\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "store")
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nspec\n")
    manifest = coverage.build_manifest(
        mode="spec", base=base, commit=commit, repo_root=repo, granularity="symbol",
        spec_markdown="### S-1 — scope\n\nChanges `Store.load()`.\n", declared_scope=[],
    )
    assert "src/pkg/store.py" in _engaged(manifest)


# --- B.8 C-1: declared scope in code mode is a FOCUS, never a reading boundary ------




# --- B.14 Part B: the question denominator (code mode) ----------------------
#
# In code mode the rows are two verdict axes per semantic BLOCK sliced by the
# development LLM; the parser-derived place rows (symbols, importers, configs,
# markdown sections) are retired, and the derivation machinery survives as the
# COMPLETENESS instrument: every changed line must be referenced by at least one
# block, a reference outside the diff is refused, and the slicing is fingerprinted
# so a differing re-run is attributable.


def _minimal_blocks(repo, base, commit, block_id="b1", claim="the change, in one claim"):
    """One block referencing every changed line of the ref pair — the minimal legal
    slicing (the tests below carve refusals out of it)."""
    universe = coverage._changed_line_universe(
        repo, base, commit, coverage._changed_paths(repo, base, commit)
    )
    refs = [
        {"path": path, "side": side, "start": n, "end": n}
        for (path, side), lines in sorted(universe.items())
        for n in sorted(lines)
    ]
    return [{"block_id": block_id, "claim": claim, "refs": refs}]


def _code(repo, base, commit, **kw):
    kw.setdefault("blocks", _minimal_blocks(repo, base, commit))
    return coverage.build_manifest(
        mode="code", base=base, commit=commit, repo_root=repo, **kw
    )


def test_code_mode_requires_the_slicing(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    with pytest.raises(coverage.ManifestError, match="requires the slicing"):
        coverage.build_manifest(mode="code", base=base, commit=commit, repo_root=repo)


def test_grain_choice_is_retired_in_code_mode(repo: Path):
    """B-7: retired in code and prompt text, not left as dead configuration."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    with pytest.raises(coverage.ManifestError, match="retired"):
        _code(repo, base, commit, granularity="file")


def test_spec_mode_takes_no_slicing(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    with pytest.raises(coverage.ManifestError, match="CODE-mode"):
        coverage.build_manifest(
            mode="spec", base=base, commit=commit, repo_root=repo,
            spec_markdown="### S-1 — x\n", blocks=[],
        )


def test_two_axis_rows_per_block_and_the_slicing_rides_the_payload(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    manifest = _code(repo, base, commit)
    block_rows = [r for r in manifest["rows"] if r["kind"] == "block"]
    assert [r["row_id"] for r in block_rows] == ["b1::change", "b1::seam"]
    # the row id IS the literal locator identity — reports speak the slicing's language
    assert all(r["row_id"] == r["locator"] for r in block_rows)
    # the complete slicing rides the payload: claim, refs, computed flag
    assert [b["block_id"] for b in manifest["blocks"]] == ["b1"]
    assert manifest["blocks"][0]["claim"]
    assert manifest["blocks"][0]["refs"]
    assert manifest["blocks"][0]["high_stakes"] is False
    assert manifest["inputs"]["blocks"]  # B-8: the slicing is fingerprinted
    # ...and the wire message carries the slicing too
    payload = coverage.manifest_message(manifest, artifact_seq=3)
    assert payload["blocks"] == manifest["blocks"]
    # the blind edge stays exactly one ordinary row, asserted last
    blind = [r for r in manifest["rows"] if r["kind"] == "blind_edge"]
    assert len(blind) == 1 and manifest["rows"][-1] is blind[0]


def test_every_changed_line_must_land_in_a_block(repo: Path):
    """B-4 mechanical completeness: the machine, not the author, guarantees nothing
    changed was silently omitted — unassigned lines are NAMED in the refusal."""
    base, commit = _commit_change(
        repo, "src/pkg/core.py",
        "def alpha():\n    return 11\n\n\ndef beta():\n    return 22\n",
    )
    blocks = _minimal_blocks(repo, base, commit)
    blocks[0]["refs"] = blocks[0]["refs"][:1]  # drop the rest of the change
    with pytest.raises(coverage.ManifestError, match="referenced by no block"):
        _code(repo, base, commit, blocks=blocks)


def test_deleted_lines_are_part_of_the_universe(repo: Path):
    """A deletion exists only in the pre-image; a slicing covering only the new side
    has silently omitted it — the completeness instrument must see both sides."""
    base, commit = _commit_change(repo, "src/pkg/core.py", "def alpha():\n    return 111\n")
    blocks = _minimal_blocks(repo, base, commit)
    blocks[0]["refs"] = [r for r in blocks[0]["refs"] if r["side"] == "new"]
    assert blocks[0]["refs"], "the modification keeps a new-side ref for the block"
    with pytest.raises(coverage.ManifestError, match=r"\(old\)"):
        _code(repo, base, commit, blocks=blocks)


def test_a_reference_outside_the_diff_is_refused(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    blocks = _minimal_blocks(repo, base, commit)
    blocks[0]["refs"].append({"path": "doc.md", "side": "new", "start": 90, "end": 99})
    with pytest.raises(coverage.ManifestError, match="outside the ref-pair diff"):
        _code(repo, base, commit, blocks=blocks)


def test_the_mapping_is_many_to_many(repo: Path):
    """The operator's explicit correction: one line may legally serve more than one
    semantic block — 'at least one', never 'exactly one'."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    blocks = _minimal_blocks(repo, base, commit)
    twin = {"block_id": "b2", "claim": "the same lines, another angle",
            "refs": [dict(r) for r in blocks[0]["refs"]]}
    manifest = _code(repo, base, commit, blocks=blocks + [twin])
    assert [r["row_id"] for r in manifest["rows"] if r["kind"] == "block"] == [
        "b1::change", "b1::seam", "b2::change", "b2::seam",
    ]


def test_malformed_blocks_are_refused_each_with_its_ground(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    good = _minimal_blocks(repo, base, commit)[0]
    cases = [
        ([good, dict(good)], "duplicate block_id"),
        ([{**good, "block_id": "a::b"}], "axis separator"),
        ([{**good, "claim": " "}], "coherent claim"),
        ([{**good, "refs": []}], "non-empty list"),
        ([{**good, "refs": [{"path": "doc.md", "side": "both", "start": 1, "end": 1}]}],
         "side"),
        ([{**good, "refs": [{"path": "doc.md", "side": "new", "start": 3, "end": 2}]}],
         "start"),
    ]
    for blocks, match in cases:
        with pytest.raises(coverage.ManifestError, match=match):
            _code(repo, base, commit, blocks=blocks)


def test_high_stakes_locators_flag_blocks_by_overlap(repo: Path):
    """B-2: a declared locator (the three existing forms, resolved by the same parsers)
    marks every block referencing a changed line inside its span — and BOTH axis rows of
    a flagged block carry the evidence demand."""
    base, commit = _commit_change(
        repo, "src/pkg/core.py",
        "def alpha():\n    return 11\n\n\ndef beta():\n    return 22\n",
    )
    blocks = _minimal_blocks(repo, base, commit)
    alpha_refs = [r for r in blocks[0]["refs"] if r["start"] <= 2]
    beta_refs = [r for r in blocks[0]["refs"] if r["start"] > 2]
    split = [
        {"block_id": "alpha", "claim": "alpha now returns 11", "refs": alpha_refs},
        {"block_id": "beta", "claim": "beta now returns 22", "refs": beta_refs},
    ]
    manifest = _code(
        repo, base, commit, blocks=split, high_stakes=["src/pkg/core.py::alpha"]
    )
    flags = {b["block_id"]: b["high_stakes"] for b in manifest["blocks"]}
    assert flags == {"alpha": True, "beta": False}
    flagged_rows = {r["row_id"] for r in manifest["rows"] if r.get("high_stakes")}
    assert flagged_rows == {"alpha::change", "alpha::seam"}


def test_a_stakes_locator_overlapping_no_block_is_refused(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    with pytest.raises(coverage.ManifestError, match="overlaps no block"):
        _code(repo, base, commit, high_stakes=["src/pkg/core.py"])


def test_manifest_id_is_stable_and_moves_with_the_slicing(repo: Path):
    """B-8: same inputs -> same id (the critic verifies by re-running); a different
    slicing is a DIFFERENT manifest, attributable via the inputs digest."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    first = _code(repo, base, commit)
    second = _code(repo, base, commit)
    assert first["manifest_id"] == second["manifest_id"]
    renamed = _code(
        repo, base, commit,
        blocks=_minimal_blocks(repo, base, commit, block_id="other"),
    )
    assert renamed["manifest_id"] != first["manifest_id"]
    assert renamed["inputs"]["blocks"] != first["inputs"]["blocks"]


def test_row_cap_is_kept_over_blocks(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    blocks = _minimal_blocks(repo, base, commit)
    for i in range(4):
        blocks.append({**dict(blocks[0]), "block_id": f"extra{i}"})
    with pytest.raises(coverage.ManifestTooLarge) as exc:
        _code(repo, base, commit, blocks=blocks, max_rows=5)
    assert exc.value.cap == 5


def test_message_payload_carries_the_version_anchor(repo: Path):
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nx\n")
    manifest = _code(repo, base, commit)
    payload = coverage.manifest_message(manifest, artifact_seq=7)
    assert payload["artifact_seq"] == 7
    assert payload["manifest_id"] == manifest["manifest_id"]
    assert payload["rows"] == manifest["rows"]


def test_the_tool_output_satisfies_the_server_validator(repo: Path):
    """The seam nobody else checks: the tool BUILDS the manifest and the server
    VALIDATES it; a disagreement would only surface as a 422 mid-review."""
    from assistant_memory.review.errors import InvalidMessagePayloadError
    from assistant_memory.review.repository import _validate_coverage_manifest_payload

    # the tool builds CURRENT-contract manifests; validate under a stamped config
    # (round 10: an unstamped/None config reads as a legacy-contract review)
    stamped = {"coverage_contract": "B.14"}
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    code = _code(repo, base, commit, high_stakes=["doc.md"])
    _validate_coverage_manifest_payload(
        coverage.manifest_message(code, artifact_seq=2), config=stamped
    )
    empty = _code(repo, base, commit, high_stakes=[])
    _validate_coverage_manifest_payload(
        coverage.manifest_message(empty, artifact_seq=2), config=stamped
    )
    # NOT declaring the high-stakes list at all is still refused at the server seam:
    # a forgotten list and a considered "none" must be different payloads.
    undeclared = _code(repo, base, commit)
    with pytest.raises(InvalidMessagePayloadError, match="high_stakes"):
        _validate_coverage_manifest_payload(
            coverage.manifest_message(undeclared, artifact_seq=2), config=stamped
        )


def test_the_declared_stakes_list_rides_the_code_manifest(repo: Path):
    """finding b14-code-high-stakes-input-not-reproducible: the block flags are a lossy
    overlap-projection of the declared list, so the list itself travels verbatim — a
    re-derivation needs it and cannot recover it from the flags."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    manifest = _code(repo, base, commit, high_stakes=["doc.md"])
    assert manifest["high_stakes_declared"] == ["doc.md"]
    payload = coverage.manifest_message(manifest, artifact_seq=3)
    assert payload["high_stakes_declared"] == ["doc.md"]
    empty = _code(repo, base, commit, high_stakes=[])
    assert empty["high_stakes_declared"] == []


def test_over_cap_advice_is_contract_aware(repo: Path):
    """Round 11, finding b14-code-cap-error-retains-retired-granularity: the advice
    names only the levers the mode actually has — code mode has no grain to step."""
    base, commit = _commit_change(repo, "doc.md", "# Title\n\nchanged\n")
    blocks = _minimal_blocks(repo, base, commit)
    for i in range(4):
        blocks.append({**dict(blocks[0]), "block_id": f"extra{i}"})
    with pytest.raises(coverage.ManifestTooLarge) as exc:
        _code(repo, base, commit, blocks=blocks, max_rows=5)
    assert "slice coarser" in str(exc.value)
    assert "granularity" not in str(exc.value)
