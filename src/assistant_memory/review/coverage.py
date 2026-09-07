# SPDX-License-Identifier: Apache-2.0
"""Coverage manifest — the EXTERNAL denominator for the critic's reading (B.6, T1-3/T1-4).

A self-generated coverage check inherits the checker's own blind spot: "did I look
everywhere?" cannot surface a region the model never knew to look at. So coverage is
measured against a manifest derived **mechanically from the artifact's ref pair**, by a
fixed tool — this module — that is not the reviewing model.

The externality is carried by **verifiability, not by trusted authorship**: whoever
invokes it (development alongside the artifact, or the watcher), the critic can re-run
the same tool against the same ref pair and compare — a mismatch between the posted
manifest and the tool's output is a finding. ``manifest_id`` is a pure function of the
declared inputs, which is what makes that comparison mechanical.

**How far that reproducibility actually goes, stated rather than implied.** The dividing line
is NOT the mode — it is whether an input came from the ref pair, and one non-ref input exists
in both modes. The **high-stakes list is handed to the tool in code mode too**: it marks rows
load-bearing and it participates in ``manifest_id``. So code mode is reproducible from the ref
pair only **modulo the declared high-stakes list** — a re-run that omits it over a manifest
that declared one differs legitimately, and reading that difference as a derivation mismatch
would manufacture a finding against an honest counterpart. Saying "in code mode everything
comes from the ref pair", as this docstring once did, is what set that trap.

In spec mode two further inputs do not come from the ref pair — the spec body and the declared
scope — so a re-run after any of the three has moved will legitimately differ. All non-ref
inputs are therefore **fingerprinted into the payload and into ``manifest_id``**: the
divergence becomes *attributable* (the digests say which input changed) instead of an
unexplained mismatch between two honest parties. Fingerprints make a difference legible; they
do not make a manifest reproducible from the ref pair alone, and this module does not claim
they do.

What it produces, in both modes:

- **code mode** — the QUESTION denominator (B.14 Part B): two verdict axes (change +
  seam) per semantic BLOCK sliced by the development LLM, validated mechanically
  against the changed-line universe of the ref pair — every changed line must be
  referenced by at least one block, a reference outside the diff is refused, and the
  slicing itself is contestable by the critic, who receives it together with the full
  diff. The former place-derivation (changed symbols, importers, tests, configs — the
  B.7-measured 275-row maps) is retired; the derivation machinery survives as the
  completeness instrument;
- **spec mode** — a SEMANTIC denominator, not a physical one (operator ruling
  2026-07-22): the element-id set parsed from the spec's own headers, the declared-scope
  divergence row(s), and the blind-edge row — **no physical reach rows**. What a spec
  review measures is whether every DECISION was considered; where a decision will later
  sit in the tree is the code review's question, asked when the code exists. The ruling's
  ground, measured live (review b0ef1b30): 43 of 98 physical rows were reachable only
  through the spec body — the tool was expanding the author's own list and asking the
  critic to report against it. The repository paths and code symbols the spec body NAMES
  still feed the DIVERGENCE check — the one comparison of two independently-authored
  lists — but produce no rows of their own.

  What that parsing actually reads, stated at the precision the code delivers rather
  than one notch above it: **code symbols come from BACKTICKED code spans only** — an
  unbackticked prose mention of a function is not resolved. Paths are recognised more
  widely (with or without backticks, with or without a directory component, and
  extensionless tracked files by name). An unresolved prose mention belongs to the
  blind-edge row like any other thing this tool cannot see, and a surface absent from
  BOTH the ref pair and the spec body remains ordinary critic judgement on the
  blind-edge row. Code mode is different: there the ref pair carries the real change,
  and physical reach rows are derived from the diff.

Every manifest therefore carries exactly ONE ``blind_edge`` row, id ``hunt-by-name``, as an
ORDINARY row: it takes a verdict like any other, so the one row that declares what the
manifest cannot see is never the part nobody has to answer for.
"""

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from assistant_memory.review.genres import AUDIENCE_GENRE

# Bumped whenever the derivation changes in a way that alters the rows a given ref pair
# produces. It is part of `manifest_id`, so a version skew between the manifest development
# posted and the tool the critic re-runs shows up as a mismatch instead of a silent
# disagreement about what "the same manifest" means.
# "2" is the B.14 question denominator (Part B): in CODE mode the rows are two verdict
# axes per development-sliced semantic BLOCK, not parser-derived places. The version
# participates in `manifest_id`, so a v1 manifest can never masquerade as a v2 one when
# the critic re-runs the tool.
TOOL_VERSION = "2"
#: The pre-B.14 derivation. A review created before B.14 finishes under its own
#: coverage contract (spec constraint 3), INCLUDING the right to post NEW manifests
#: for its later artifact versions in the form it has always posted — so the POST
#: validator needs the legacy version by name, not just the current one (round 10,
#: finding b14-legacy-contract-not-carried-through-critic-surfaces).
TOOL_VERSION_LEGACY = "1"

GRANULARITIES = ("file", "symbol")
ROW_KINDS = ("code", "markdown", "element", "divergence", "blind_edge", "block")

#: The two verdict axes of a code-mode block (B.14 B-3): the CHANGE itself, and the
#: SEAM — how the change meets the settled code around it. Two rows per block,
#: ``row_id = <block_id>::<axis>``, each taking one verdict with its own evidence.
BLOCK_AXES = ("change", "seam")

#: The machine-readable head of a downgrade reason written by the watcher's content
#: re-read when the EVIDENCE, not the reading, is what failed (B.11 B-1/B-4). It is a
#: classification the server reads back: an unreached-row escalation whose whole group
#: carries it asks the operator for an instrument remedy rather than for a ruling on
#: whether the rows are reviewable at all. The separator is part of the contract so that
#: a reason can be split back into its class and its detail.
INSTRUMENT_FAILURE_REASON = "instrument_failure"
INSTRUMENT_FAILURE_PREFIX = INSTRUMENT_FAILURE_REASON + " — "

#: The blind edge is a FIXED literal id, present exactly once in every manifest, both modes.
BLIND_EDGE_ROW_ID = "hunt-by-name"

#: Extensions treated as configuration (read by the spec-body bare-name scan).
_CONFIG_SUFFIXES = (".toml", ".ini", ".cfg", ".yaml", ".yml", ".json")


def is_config_path(path: str) -> bool:
    """Is this path a configuration surface?

    Expressed as a PREDICATE rather than a suffix list, because a suffix list cannot
    describe dotfile-style config at all: ``.env`` has no extension in the usual sense, and
    ``.env.local`` ends in ``.local``. Two rounds of the B.6 review lost dotenv paths to that
    representation — first the requirement of a directory component, then the suffix test —
    so the shape is fixed here once, and every caller asks this function instead of
    re-deciding what "config" means. Since B.14 its one caller is the spec-body bare-name
    scan (the code-mode config-reach rows dissolved into the per-block seam axis).
    """
    name = path.rsplit("/", 1)[-1]
    return name.endswith(_CONFIG_SUFFIXES) or name == ".env" or name.startswith(".env.")


#: Path-ish tokens worth resolving when parsed out of a spec body or a source file. Two
#: shapes, because requiring a directory component silently loses a whole category the
#: manifest CLAIMS to cover: a change that reads `pyproject.toml`, `settings.yaml` or
#: `.env` from the repository root names them with no slash at all, and the blind-edge row
#: does not cover that — it declares dynamic reach, not a gap in static path detection.
#: Both shapes are filtered by existence at the commit, which is what keeps the barer
#: pattern from turning prose into rows.
_PATHLIKE = re.compile(
    r"(?<![\w./-])((?:[\w.-]+/)+(?:[\w.-]+\.(?:py|md|toml|ini|cfg|yaml|yml|json|sql|txt)"
    r"|\.env(?:\.[\w-]+)?))"
)
_BARE_FILE = re.compile(
    r"(?<![\w./-])((?:[\w-]+\.(?:py|md|toml|ini|cfg|yaml|yml|json|sql)|\.env(?:\.[\w-]+)?))"
    r"(?![\w/])"
)
#: Which bare names are worth resolving depends on WHERE the text came from, not on the
#: extension alone. A spec body is prose written about the change, so a bare `doc.md`,
#: `conftest.py` or `.env` in it is almost always a genuine reference. A Python source file
#: is not prose: bare `.py` names appear throughout its docstrings and messages, and
#: resolving those would add a row per mention. So the source-scan is narrowed to the
#: category the reach obligation names explicitly — the config a change reads.


def _is_spec_bare_candidate(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith((".py", ".md", ".sql")) or is_config_path(path)


#: A backticked code span in a spec body. The pattern deliberately matches the WHOLE span
#: and lets `_mentioned_symbols` take its leading dotted-identifier prefix, rather than
#: enumerating the shapes a mention may take. Enumerating was the losing strategy: bare
#: names, then dotted names, then call notation — each round added one more form while the
#: next was already in the text. A prefix rule covers `append_message`,
#: `Repository.append_message`, `append_message()`, `Repository.append_message(session)`
#: and `rows[0]` with one rule and no list to keep extending.
_BACKTICKED = re.compile(r"`([^`\n]{1,200})`")
#: The leading dotted-identifier of a code span — what actually gets resolved.
_LEADING_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
#: A spec element heading: `### T2-3 — title`. The prefix may contain DIGITS (`T1`, `T2`) —
#: requiring a purely-alphabetic prefix is exactly the bug that silently dropped 14 elements
#: from an earlier bundle builder and left their rationale attached to the wrong ids.
_ELEMENT_HEADING = re.compile(r"^###\s+~*\s*([A-Za-z][A-Za-z0-9]*-\d+)\b")
_ANY_H3 = re.compile(r"^###\s+")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")


class ManifestError(Exception):
    """The manifest could not be derived — never a partial or silently truncated one."""


class ManifestTooLarge(ManifestError):
    """The derived manifest exceeds the caller's row cap (T1-4).

    Raised rather than truncated. Exceeding the cap is a real decision, and the advice
    names only the levers the mode actually has (round 11, finding
    b14-code-cap-error-retains-retired-granularity): in spec mode, narrow the scope or
    step the granularity down; in code mode the grain is retired (B.14 B-7) — narrow
    the scope or slice coarser (fewer, broader blocks). A tool that silently dropped
    the tail would hand back a manifest that looks complete, which is the one failure
    this whole mechanism exists to prevent.
    """

    def __init__(self, row_count: int, cap: int, mode: str | None = None) -> None:
        advice = (
            "narrow the review scope or slice coarser — fewer, broader blocks; the "
            "grain choice is retired in code mode (B.14 B-7)"
            if mode == "code"
            else "narrow the review scope or step the granularity down"
        )
        super().__init__(
            f"manifest has {row_count} rows, over the cap of {cap} — {advice} "
            "(never truncate: T1-4)"
        )
        self.row_count = row_count
        self.cap = cap


# --- git access (deterministic, read-only) --------------------------------


#: Configuration pinned on EVERY git invocation, so the derivation depends on the ref pair
#: and nothing else. The manifest claims to be reproducible, and the critic verifies it by
#: re-running the tool — in a different process, on a machine whose git config this tool
#: does not control. Rename detection changes which paths a diff reports; the diff algorithm
#: changes where hunks fall and therefore which symbols a change is attributed to; an
#: external diff driver or textconv filter can replace the output entirely; `core.quotepath`
#: changes how non-ASCII paths are spelled. Each of those would make two honest re-runs
#: disagree with nothing available to explain it.
#: NOT pinned here: ``diff.external``. Setting it empty makes git try to spawn an empty
#: command and fail outright — which, combined with a tolerant failure path, silently
#: emptied every diff and collapsed the manifest to nothing. External diff drivers are
#: disabled with ``--no-ext-diff`` on the diff invocations instead, where it is a flag
#: rather than a config value that can be empty.
_PINNED_GIT_CONFIG = (
    "-c", "diff.algorithm=myers",
    "-c", "diff.renames=false",
    "-c", "diff.noprefix=false",
    "-c", "core.quotepath=false",
)


def _git(repo_root: str | Path, *args: str, allow_failure: bool = False) -> str:
    """Run a read-only git command at ``repo_root`` and return stdout.

    ``shell=False`` on an argv list: a ref or path containing shell metacharacters reaches
    git as a literal argument. Some queries legitimately find nothing (``git grep`` exits 1
    on no match), so those pass ``allow_failure``.

    Every invocation carries ``_PINNED_GIT_CONFIG`` — see there for why the derivation must
    not inherit the ambient git configuration.
    """
    proc = subprocess.run(
        ["git", *_PINNED_GIT_CONFIG, *args],
        shell=False,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        # `allow_failure` means "this query legitimately finds nothing" — git grep's exit 1.
        # It must NOT swallow a real failure: a broken invocation returning "" looks exactly
        # like an empty result, and that is how a mis-pinned config emptied every diff and
        # collapsed the manifest to nothing without a word. Anything past rc=1 is a fault.
        if allow_failure and proc.returncode == 1:
            return ""
        raise ManifestError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {(proc.stderr or '')[-500:]}"
        )
    return proc.stdout


def _tracked_paths(repo_root: str | Path, commit: str) -> set[str]:
    """Every path tracked at ``commit``.

    One git call instead of one per candidate, which is what makes existence the CHEAP
    check — and existence is the right check. An extension whitelist could never recognise
    `Dockerfile`, `Makefile` or `.dockerignore`, and extending the list one name at a time
    is the losing strategy this module already abandoned once for symbol shapes: the tree
    itself knows what is a repository path, so ask it.
    """
    out = _git(repo_root, "ls-tree", "-r", "--name-only", commit, allow_failure=True)
    return {line.strip() for line in out.splitlines() if line.strip()}


def _read_at(repo_root: str | Path, commit: str, path: str) -> str | None:
    """File content at a commit, or None when the path does not exist there (deleted)."""
    proc = subprocess.run(
        ["git", *_PINNED_GIT_CONFIG, "show", f"{commit}:{path}"],
        shell=False,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def _changed_paths(repo_root: str | Path, base: str, commit: str) -> list[str]:
    """Paths touched between the ref pair, in a stable order."""
    out = _git(repo_root, "diff", "--no-ext-diff", "--name-only", f"{base}..{commit}")
    return sorted({line.strip() for line in out.splitlines() if line.strip()})


def changed_modules(repo_root: str | Path, base: str, commit: str) -> list[str]:
    """The slice's scope, derived MECHANICALLY from the ref pair (B.11 E-4).

    A "module" here is a changed path, which is the only derivation the pair actually
    supports without guessing: anything finer would be this tool's opinion about what a
    module is, and the point of deriving the scope rather than letting the pass choose it
    is that two maps of the same slice stay comparable.

    Public where `_changed_paths` is private because the semantic map is a second consumer
    outside this module's own manifest building — and a second consumer copying a private
    helper is how a predicate ends up existing three times (B.11 A-1).
    """
    return _changed_paths(repo_root, base, commit)


def _changed_line_ranges(
    repo_root: str | Path, base: str, commit: str, path: str, *, side: str = "new"
) -> list[tuple[int, int]]:
    """Line ranges touched in ``path``, on the post-image (``new``) or pre-image (``old``).

    ``-U0`` so a hunk covers only the changed lines themselves; context lines would drag in
    neighbouring symbols and inflate the manifest with rows the change never touched.

    Both sides are derivable because attribution from the post-image ALONE cannot see a
    deletion: a deleted function has no post-image span, so its hunk lands on whatever
    survived next to it and the manifest names the wrong surface — or the whole file — for
    the one edit that most needs its own verdict.
    """
    out = _git(
        repo_root, "diff", "--no-ext-diff", "-U0", f"{base}..{commit}", "--", path,
        allow_failure=True,
    )
    marker = r"\+" if side == "new" else r"-"
    ranges: list[tuple[int, int]] = []
    for line in out.splitlines():
        if not line.startswith("@@"):
            continue
        # `@@ -a,b +c,d @@` — take the side asked for, and only from the hunk header
        header = line.split("@@")[1] if "@@" in line[2:] else line
        m = re.search(rf"{marker}(\d+)(?:,(\d+))?", header)
        if not m:
            continue
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        if count == 0:
            # Nothing on this side: a pure insertion has no old-side lines and a pure
            # deletion has no new-side ones. Anchoring to the neighbouring line is what
            # attributed a deletion to the surviving surface, so this side simply
            # contributes nothing and the OTHER side's pass supplies the real row.
            continue
        ranges.append((start, start + count - 1))
    return ranges


# --- rows ------------------------------------------------------------------


def row_id_for(kind: str, locator: str) -> str:
    """A LOCATOR identity — derived from the kind and locator, never from a position.

    Two manifests built from different artifact versions therefore agree on the id of any
    row that still denotes the same PLACE, which is what makes the late-surfacing
    diagnostic well-defined (a finding landing on a row previously marked reviewed-clean).
    A row whose locator genuinely changed gets a NEW id and is treated as a new row, not as
    a silently renamed old one.

    What this id deliberately does NOT capture is the row's CONTENT: the surface behind a
    stable locator can change between versions while the id stays the same. Id equality
    therefore proves locator equality only — it never proves that a prior reading of the
    row still applies. Carrying a clean verdict forward to a new version requires an
    explicit content check (e.g. an empty ``git diff <v_prev> <v_cur> -- <file>``);
    a row whose surface changed is re-read, whatever its id says.
    """
    if kind == "blind_edge":
        return BLIND_EDGE_ROW_ID
    if kind == "block":
        # B.14 B-3: a block-axis row id IS its locator — the literal
        # ``<block_id>::<axis>`` the spec names, so reports and escalations speak in
        # the slicing's own vocabulary instead of a hash of it.
        return locator
    digest = hashlib.sha256(f"{kind}\x00{locator}".encode()).hexdigest()
    return digest[:12]


def _row(kind: str, locator: str) -> dict:
    if kind not in ROW_KINDS:
        raise ManifestError(f"unknown row kind {kind!r}")
    return {"row_id": row_id_for(kind, locator), "kind": kind, "locator": locator}


def _dedupe(rows: Iterable[dict]) -> list[dict]:
    """One row per identity, first occurrence wins, order preserved.

    The blind edge is asserted separately by ``build_manifest`` so it can never be dropped
    here or duplicated by a derivation path that happens to name it.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        if r["row_id"] in seen:
            continue
        seen.add(r["row_id"])
        out.append(r)
    return out


# --- Python symbol attribution --------------------------------------------


def _symbol_spans(source: str) -> list[tuple[str, int, int]]:
    """(qualified name, first line, last line) for every def/class in a module.

    Decorators are folded into the span: an edit that only touches a decorator is an edit
    to that symbol, and attributing it to the module instead would hide it in a row whose
    verdict says nothing about the function whose behaviour changed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[str, int, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in getattr(node, "body", []):
            if not isinstance(
                child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                continue
            name = f"{prefix}{child.name}"
            start = min(
                [child.lineno] + [d.lineno for d in getattr(child, "decorator_list", [])]
            )
            end = getattr(child, "end_lineno", None) or start
            spans.append((name, start, end))
            if isinstance(child, ast.ClassDef):
                walk(child, f"{name}.")

    walk(tree, "")
    return spans


# --- Markdown sectioning ---------------------------------------------------


def _markdown_sections(source: str) -> list[tuple[str, int, int]]:
    """(heading path, first line, last line) per section, e.g. ``§3 Reach > Checklist``.

    The same split spec mode uses to cut element rows — which is exactly why Markdown is
    covered in v1 rather than deferred: it costs a heading split, not a parser, and the very
    next reviews this machinery gates are Markdown prompt changes.

    Spans are returned alongside the paths so a CHANGED Markdown file can be attributed to
    the sections the diff actually touched, exactly as a changed Python file is attributed
    to the symbols it touched. A section ends where the next heading of any level begins.
    """
    lines = source.splitlines()
    stack: list[str] = []
    starts: list[tuple[str, int]] = []
    for i, line in enumerate(lines, start=1):
        m = _MD_HEADING.match(line)
        if not m:
            continue
        level = len(m.group(1))
        title = " ".join(m.group(2).split())
        if not title:
            continue
        del stack[level - 1 :]
        while len(stack) < level - 1:
            stack.append("(untitled)")
        stack.append(title)
        starts.append((" > ".join(stack), i))
    out: list[tuple[str, int, int]] = []
    for index, (heading, start) in enumerate(starts):
        end = starts[index + 1][1] - 1 if index + 1 < len(starts) else len(lines)
        out.append((heading, start, max(start, end)))
    return out


# --- reach derivation ------------------------------------------------------


def _module_name(path: str) -> str | None:
    """Dotted module path for a file under ``src/``, e.g. ``assistant_memory.review.poll``."""
    if not path.endswith(".py"):
        return None
    parts = Path(path).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _grep_files(
    repo_root: str | Path, commit: str, pattern: str, *globs: str
) -> list[str]:
    """Files at ``commit`` matching an extended-regex pattern (``git grep`` — deterministic).

    Searching the tree AT THE COMMIT, not the working directory, is what makes two runs of
    this tool over the same ref pair produce the same manifest — otherwise the critic's
    re-run would disagree with development's whenever the working tree had moved on.
    """
    args = ["grep", "-l", "-I", "-E", pattern, commit]
    if globs:
        args += ["--", *globs]
    out = _git(repo_root, *args, allow_failure=True)
    files = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # `git grep <rev>` prefixes each path with "<rev>:"
        files.append(line.split(":", 1)[1] if line.startswith(f"{commit}:") else line)
    return sorted(set(files))


def _named_paths(
    repo_root: str | Path, commit: str, text: str, bare_ok
) -> set[str]:
    """Every repository path named in ``text`` that exists at the commit.

    Both path shapes are searched — with and without a directory component — so a
    root-level file named bare is not silently absent from the denominator.
    ``bare_ok`` is a predicate narrowing the slash-less shape to what is worth resolving for
    this text's provenance; existence at the commit is what keeps either shape from turning
    prose into rows.
    """
    found: set[str] = set()
    for match in _PATHLIKE.finditer(text):
        candidate = match.group(1)
        if _read_at(repo_root, commit, candidate) is not None:
            found.add(candidate)
    for match in _BARE_FILE.finditer(text):
        candidate = match.group(1)
        if bare_ok(candidate) and _read_at(repo_root, commit, candidate) is not None:
            found.add(candidate)
    return found


def _heading_element_id(locator: str) -> str | None:
    """The spec element id a markdown row's deepest heading carries, if any.

    A markdown locator is ``path::A > B > C``; the element heading text keeps its id at the
    front (``E-3 — …``), so the deepest segment is what identifies the element.
    """
    _, _, headings = locator.partition("::")
    if not headings:
        return None
    m = _ELEMENT_HEADING.match("### " + headings.rsplit(" > ", 1)[-1])
    return m.group(1) if m else None


def spec_elements(spec_markdown: str) -> list[str]:
    """Stable element ids parsed from the spec's own `###` headings.

    INVARIANT, enforced rather than hoped for: **every** `###` heading must yield an id. A
    heading the pattern silently skips is an element that would be absent from the
    denominator while looking like a complete manifest — the precise failure that dropped
    fourteen elements from an earlier bundle and attached their rationale to strangers.
    """
    ids: list[str] = []
    unparsed: list[str] = []
    for line in spec_markdown.splitlines():
        if not _ANY_H3.match(line):
            continue
        m = _ELEMENT_HEADING.match(line)
        if m:
            ids.append(m.group(1))
        else:
            unparsed.append(line.strip()[:80])
    if unparsed:
        raise ManifestError(
            "every `### ` heading must carry a stable element id (e.g. `### T2-3 — …`); "
            f"unrecognised: {unparsed}"
        )
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ManifestError(
            f"duplicate element ids {duplicates} — ids are the review's addressing scheme "
            "and must be unique"
        )
    return ids


#: The tier annotation a spec element carries in its own heading, e.g.
#: `### G-3 — Gate answer *(tier: operator_decision)*` — including the MIXED forms
#: (`*(tier: operator_decision for existence; llm_judgment for rendering)*`).
_TIER_ANNOTATION = re.compile(r"\*\(\s*tier:(?P<tier>[^)]*)\)\*", re.IGNORECASE)
#: Recovery and termination surfaces. A spec element about how the system comes back, or
#: how it stops, is load-bearing whatever its tier: a cheap `reviewed-clean` on one of
#: those is exactly the claim that costs the most when it is wrong.
_RECOVERY_WORDS = (
    "recovery", "recover", "termination", "terminate", "terminal", "rollback",
    "resume", "restart", "abort", "shutdown", "crash", "fallback", "stall",
)


def derive_high_stakes(spec_markdown: str) -> list[str]:
    """The load-bearing element ids, derived MECHANICALLY from the spec's own headings.

    WHY THIS IS NOT A HAND-MAINTAINED FILE ANY MORE. The list decides where the server
    demands a cited observation behind a `reviewed-clean` verdict — so a stale list quietly
    downgrades exactly the elements that most need the scrutiny. Across B.7's own review the
    hand-written list fell behind the spec FOUR times: an element was added or its tier
    changed, and the list was updated one round later, or not at all. Nothing in the process
    could see the gap, because a list that is merely short still looks like a list.

    The rule is the spec's own: every element the OPERATOR decided (including the mixed
    tiers, where an operator decision sits beside an llm_judgment about rendering — a
    partial operator decision is still one), plus the recovery and termination surfaces. It
    is derived from the same body the manifest's element rows come from, so the two cannot
    disagree, and the derivation is a pure function of that body, so a re-run reproduces it.

    An author may still ADD to the list (`--high-stakes-file`); the derivation is a floor,
    not a ceiling. What it removes is the ability to fall silently below it.
    """
    out: list[str] = []
    for line in spec_markdown.splitlines():
        m = _ELEMENT_HEADING.match(line)
        if not m:
            continue
        element_id = m.group(1)
        tier = _TIER_ANNOTATION.search(line)
        lowered = line.lower()
        # ANY operator involvement declared in the tier counts — `operator_decision`, a
        # mixed tier that carries one beside an llm_judgment, and the prose forms a spec
        # actually uses ("scope-in by operator ruling", "channels operator-decided"). The
        # rule matched on the exact token first and missed two elements whose operator
        # decision was written in words instead: matching the exact spelling is how a
        # mechanical floor acquires the same blind spot the hand-kept list had.
        if tier is not None and "operator" in tier.group("tier").lower():
            out.append(element_id)
        elif any(word in lowered for word in _RECOVERY_WORDS):
            out.append(element_id)
    return out


def _mentioned_paths(repo_root: str | Path, commit: str, body: str) -> list[str]:
    """Repository paths named anywhere in the spec body that exist at the commit.

    Two sources, and the second is what makes the claim true rather than nearly true. The
    regex shapes catch paths written in prose; the TRACKED-PATH scan catches everything
    else a spec can legitimately name — `Dockerfile`, `Makefile`, `.dockerignore` — which no
    extension whitelist can enumerate. Membership in the commit's own tree is both the
    correct test and the one that cannot go out of date.
    """
    found = _named_paths(repo_root, commit, body, _is_spec_bare_candidate)
    tracked = _tracked_paths(repo_root, commit)
    for span in _BACKTICKED.finditer(body):
        candidate = span.group(1).strip()
        if candidate in tracked:
            found.add(candidate)
    # EXTENSIONLESS tracked files — `Dockerfile`, `Makefile`, `LICENSE` — are the one shape
    # the regexes cannot reach even in prose, because they look exactly like words. They are
    # matched as plain tokens against the commit's own tree. Deliberately narrow: only files
    # whose basename has no extension, because those are the reported gap and nothing wider.
    # A repository file named like an ordinary English word would produce a spurious row —
    # accepted, because a spurious row costs one verdict and a missed surface costs coverage,
    # and unknown context is supposed to widen what is looked at, not narrow it.
    extensionless = {p for p in tracked if "." not in p.rsplit("/", 1)[-1]}
    if extensionless:
        # EVERY path sharing the basename, not one of them. Mapping basename -> path over a
        # SET picked an arbitrary winner, which under-covered the other locations and — far
        # worse here — made the manifest depend on set iteration order. `manifest_id` is
        # supposed to be a pure function of the inputs; a non-deterministic row set would
        # have broken the one property the critic's verification rests on.
        by_basename: dict[str, list[str]] = {}
        for path in sorted(extensionless):
            by_basename.setdefault(path.rsplit("/", 1)[-1], []).append(path)
        # trailing punctuation is part of the sentence, not of the name
        tokens = {t.strip(".-,;:") for t in re.findall(r"[\w][\w.-]*", body)}
        for token in tokens:
            # BOTH lookups, never one-or-the-other: a token that is itself a tracked path
            # (`Dockerfile` at the root) is also the basename of every other `Dockerfile` in
            # the tree, and short-circuiting on the exact match dropped all of them.
            if token in extensionless:
                found.add(token)
            found.update(by_basename.get(token, ()))
    return sorted(found)


def _module_prefix_matches(module: str | None, prefix_segments: list[str]) -> bool:
    """Do the qualifier segments left over in front of a symbol name this file's module?

    No leftovers means the mention was symbol-qualified only (`Store.load`) and any module
    is admissible. Leftovers mean the spec named a module too, and a file whose module does
    not end with that path is simply not the one the spec meant.
    """
    if not prefix_segments:
        return True
    if not module:
        return False
    prefix = ".".join(prefix_segments)
    return module == prefix or module.endswith(f".{prefix}")


def _mentioned_symbols(
    repo_root: str | Path, commit: str, body: str
) -> list[tuple[str, str]]:
    """(path, symbol) for backticked identifiers the spec names that resolve to a definition.

    Resolution is by definition site, so a spec that discusses ``compact_replay`` puts the
    file that defines it into the denominator even when the spec never names the file.
    """
    out: set[tuple[str, str]] = set()
    mentioned: set[str] = set()
    for span in _BACKTICKED.finditer(body):
        leading = _LEADING_SYMBOL.match(span.group(1).strip())
        if leading:
            mentioned.add(leading.group(0))
    for dotted in sorted(mentioned):
        leaf = dotted.rsplit(".", 1)[-1]
        if len(leaf) < 4:  # too short to resolve; the blind edge covers these
            continue
        pattern = rf"^[[:space:]]*(async[[:space:]]+)?(def|class)[[:space:]]+{re.escape(leaf)}\b"
        qualifier = dotted.rsplit(".", 1)[0] if "." in dotted else None
        for path in _grep_files(repo_root, commit, pattern, "*.py"):
            # THE QUALIFIER BOTH NAMES AND EXCLUDES. Grepping the leaf alone answers
            # `Store.load()` with every `load` in the repository: over-covering with rows the
            # spec never meant, and unable to name the one it did. So a qualified mention
            # must match this file in one of the two ways a qualifier can be true of it —
            # an enclosing symbol, or the module path — and every segment of the qualifier
            # has to be accounted for by one of them. Otherwise the file is not what the
            # spec named, and it gets no row at all.
            source = _read_at(repo_root, commit, path)
            names = {name for name, _, _ in _symbol_spans(source or "")}
            if qualifier is None:
                out.add((path, leaf))
                continue
            # (a) the qualifier is an enclosing SYMBOL — `Store.load` is defined here.
            # Compared SEGMENT-WISE, not by string suffix: `dotted.endswith("Store.load")`
            # is also true of `OtherStore.load`, which would match a definition the spec
            # never named.
            #
            # And any segments LEFT OVER in front of the symbol are a module path, which
            # must match this file too. Without that, `pkg.store.Store.load()` was satisfied
            # by `Store.load` in any module at all — the qualifier excluded a same-named
            # method on a different class but not the same class in a different place, which
            # is the very distinction a fully-qualified mention exists to draw.
            segments = dotted.split(".")
            module = _module_name(path)
            enclosing = next(
                (
                    n
                    for n in sorted(names)
                    if "." in n
                    and n.split(".") == segments[-len(n.split(".")) :]
                    and n.split(".")[-1] == leaf
                    and _module_prefix_matches(module, segments[: -len(n.split("."))])
                ),
                None,
            )
            if enclosing:
                out.add((path, enclosing))
                continue
            # (b) the qualifier is a MODULE path — `pkg.core.alpha` in src/pkg/core.py,
            # where the symbol itself is module-level and carries no dotted name
            module = _module_name(path)
            if module and (module == qualifier or module.endswith(f".{qualifier}")):
                out.add((path, leaf))
    return sorted(out)


# --- the manifest ----------------------------------------------------------


def build_manifest(
    *,
    mode: str,
    base: str,
    commit: str,
    repo_root: str | Path,
    granularity: str = "symbol",
    spec_markdown: str | None = None,
    declared_scope: list[str] | None = None,
    high_stakes: list[str] | None = None,
    blocks: list | None = None,
    max_rows: int | None = None,
    genre: str | None = None,
) -> dict:
    """Derive the coverage manifest for one artifact version.

    ``mode`` is the review mode (``code`` / ``spec``); ``base``/``commit`` are the artifact's
    ref pair. In spec mode ``spec_markdown`` is the spec body (its headers carry the element
    ids) and ``declared_scope`` is the scope the spec DECLARES — used only as a CHECK
    against the mechanically derived set, never as a source of rows.

    ``genre`` is a SEPARATE axis from ``mode`` and answers a different question. ``mode``
    says what shape the artifact arrived in — a diff, or a text bundle — which is what the
    server stores and validates. ``genre`` says what kind of review this is. An audience
    review arrives as a text bundle, so its mode is ``spec`` and that statement is true
    rather than a workaround; but its denominator is the reader artifact's own ``S-N``
    sections and nothing else. Overloading one field with both questions would give one of
    the two answers a value nobody chose.

    ``high_stakes`` marks rows as load-bearing by **locator or element id**, which makes the
    server require a specific checked observation behind any ``reviewed-clean`` on them. It
    is supplied rather than derived because what is load-bearing is a property of the
    BUNDLE — an element's epistemic tier, a symbol's role in isolation or termination logic
    — and the ref pair does not carry it. Supplying it here rather than hand-editing the
    posted payload keeps the manifest a pure function of declared inputs, so the critic's
    re-run still reproduces it.

    Raises ``ManifestTooLarge`` past ``max_rows`` — never truncates.
    """
    if mode not in ("code", "spec"):
        raise ManifestError(f"unknown mode {mode!r} (expected 'code' or 'spec')")
    if granularity not in GRANULARITIES:
        raise ManifestError(
            f"unknown granularity {granularity!r} (expected one of {GRANULARITIES})"
        )
    if genre is not None and genre != AUDIENCE_GENRE:
        raise ManifestError(f"unknown genre {genre!r}")
    if mode == "spec" and not (spec_markdown or "").strip():
        raise ManifestError("spec mode requires the spec body (its headers carry the ids)")
    # B.14 B-7: the file/symbol GRAIN choice is retired in code mode, not left as dead
    # configuration — the denominator is the slicing, and there is no place grain to pick.
    if mode == "code" and granularity != "symbol":
        raise ManifestError(
            "the granularity (grain) choice is retired in code mode (B.14 B-7) — the "
            "denominator is the development-sliced blocks; do not pass a grain"
        )
    if mode == "spec" and blocks is not None:
        raise ManifestError(
            "`blocks` is the CODE-mode denominator input (B.14 B-1) — the spec-mode "
            "denominator has been semantic since 2026-07-22 and takes no slicing"
        )
    if genre == AUDIENCE_GENRE and mode != "spec":
        raise ManifestError(
            f"an audience artifact is carried as a text bundle, so its mode is 'spec', got {mode!r}"
        )

    rows: list[dict] = []
    changed = _changed_paths(repo_root, base, commit)

    if mode == "spec":
        for element_id in spec_elements(spec_markdown or ""):
            rows.append(_row("element", element_id))

    # --- CODE MODE: the question denominator (B.14 Part B) ---
    #
    # The syntactic place-derivation (Python parser -> def/class rows, markdown header
    # splits, "affected" files grep-found by module name) stopped producing the rows the
    # critic answers: a pass honestly works 5-25 rows against maps of 275-292 (the B.7
    # measurement), and the gap killed four channels (Incident 7ec452ca). The denominator
    # is now the development LLM's semantic slicing — validated mechanically against the
    # changed-line universe (every changed line in >=1 block; a reference outside the
    # diff refused) and contestable semantically by the critic, who holds the full diff
    # (B-4). The reach rows ("every file mentioning the changed module") dissolve into
    # the per-block SEAM axis: where to look to answer it is the critic's judgment (B-5).
    #
    # SPEC MODE IS UNTOUCHED by B.14 (B-1): its denominator has been semantic since the
    # 2026-07-22 ruling — elements, divergence, blind edge.
    frozen_blocks: list[dict] | None = None
    if mode == "code":
        block_rows, frozen_blocks = _code_block_rows(
            repo_root, base, commit, blocks, high_stakes
        )
        rows.extend(block_rows)

    if mode == "spec" and genre != AUDIENCE_GENRE:
        # The paths and symbols the spec names still feed the DIVERGENCE check — the one
        # place where two independently-authored lists meet, and therefore the one part of
        # spec-mode reach that measures anything the author did not decide. They produce no
        # rows of their own.
        #
        # NOT for an audience artifact: divergence asks whether the text engages repository
        # surfaces its declared scope does not name, and a deck has no scope over the
        # repository at all. A backticked filename in a slide would produce an "undeclared"
        # row, and with no scope supplied the whole check contributes one "unchecked" row —
        # noise in a denominator that is supposed to be exactly the sections.
        body = spec_markdown or ""
        known = set(changed) | set(_mentioned_paths(repo_root, commit, body))
        if granularity == "symbol":
            known.update(path for path, _ in _mentioned_symbols(repo_root, commit, body))
        rows.extend(_divergence_rows(sorted(known), declared_scope))

    # --- B.8 C-1: in CODE mode the declared scope is a FOCUS, never a reading boundary.
    #
    # Derived rows OUTSIDE the declared scope STAY in the denominator, each marked
    # `out_of_declared_scope` by name — removing them would let a changed calling surface
    # hide behind the scope statement (finding b8-c1-scope-removes-reach-obligation).
    # They are closed collectively by ONE operator exemption recorded once in the channel
    # (the server's `_scope_exempt_rows`), never argued out one escalation per row; a
    # high-stakes row is never exemptable this way. The derived-vs-declared divergence is
    # additionally reported as one `divergence` row, so the check that RAN leaves a trace
    # (an unrun check that leaves no trace is indistinguishable from a check that passed).
    # The check-not-source discipline is preserved: the tool reports, the operator exempts.
    # Measured cost of the old behaviour (review 59b5ec42): 7 of 118 rows sat in a sibling
    # component the operator had scoped out before the review started, and two not-reached
    # passes then raised six contested forks for one already-made decision.
    if mode == "code" and declared_scope is not None:
        # B.14: with a block denominator there are no derived location rows left to mark
        # `out_of_declared_scope` — the mechanical completeness check already guarantees
        # nothing changed sits outside the slicing. The divergence row survives (B-3) so
        # the scope check that RAN still leaves a trace.
        rows.append(_row("divergence", "divergence:declared-scope-focus"))

    # The blind edge is asserted LAST and unconditionally: exactly one, an ordinary row, so
    # it takes a verdict like every other and can never sit outside the gate.
    rows = _dedupe(rows)
    rows = [r for r in rows if r["kind"] != "blind_edge"]
    rows.append(_row("blind_edge", BLIND_EDGE_ROW_ID))

    # THE MECHANICAL FLOOR (B.7): in spec mode the load-bearing set is derived from the
    # spec body itself and UNIONED with whatever the author supplied. A hand-kept list is
    # the thing that fell behind four times in one review while still looking complete;
    # deriving it means the floor cannot silently drop, and supplying more still works.
    if mode == "spec":
        derived = derive_high_stakes(spec_markdown or "")
        high_stakes = sorted({*(high_stakes or []), *derived})

    # In code mode the declared high-stakes list already fed the block-overlap mapping
    # (B-2, inside `_code_block_rows`); this locator-match path is the spec-mode one.
    if high_stakes and mode == "spec":
        wanted = {h.strip() for h in high_stakes if h.strip()}
        marked = {r["locator"] for r in rows if r["locator"] in wanted}
        for r in rows:
            if r["locator"] in wanted:
                r["high_stakes"] = True
        missing = sorted(wanted - marked)
        if missing:
            # A high-stakes marker naming a row that does not exist is a silent no-op that
            # LOOKS like the row was protected — refuse instead, the same reason the tool
            # refuses to truncate.
            raise ManifestError(
                f"high-stakes locators not present in the manifest: {missing} — a marker on "
                "a nonexistent row protects nothing while appearing to"
            )

    if max_rows is not None and len(rows) > max_rows:
        raise ManifestTooLarge(len(rows), max_rows, mode)

    manifest = {
        "tool_version": TOOL_VERSION,
        "mode": mode,
        "base": base,
        "commit": commit,
        "granularity": granularity,
        # Inputs that are NOT recoverable from the ref pair — the spec body, the declared
        # scope, the high-stakes list — are fingerprinted so a re-run that produces a
        # different manifest is ATTRIBUTABLE rather than mysterious. They change the rows and
        # therefore the id; without their digests a critic re-running the same command over
        # the same base and commit could get a different answer with nothing to point at.
        "inputs": {
            "spec_markdown": _digest(spec_markdown),
            "declared_scope": _digest(declared_scope),
            "high_stakes": _digest(high_stakes),
            # B-8: the slicing is a non-ref input exactly like the high-stakes list —
            # fingerprinted so a re-run that differs is ATTRIBUTABLE (the digests say
            # which input moved), never a manufactured mismatch between honest parties.
            # The digest is over the CANONICAL slicing (block_id/claim/refs — the frozen
            # form minus the computed flag), so the server can re-derive it from the
            # carried `blocks` and refuse a swapped slicing (round 9, finding
            # b14-carried-slicing-not-bound-to-manifest-id).
            "blocks": (
                slicing_digest(frozen_blocks) if frozen_blocks is not None else None
            ),
        },
        "rows": rows,
    }
    if frozen_blocks is not None:
        # B-2: the payload carries the COMPLETE slicing — every block with its prose
        # claim, its references, and its computed flag — beside the ref-pair identity.
        manifest["blocks"] = frozen_blocks
        # B-8 + finding b14-code-high-stakes-input-not-reproducible: the DECLARED
        # high-stakes locator list rides the payload VERBATIM. The block flags are a
        # lossy overlap-projection of it, so without the list itself the critic could
        # not re-derive the manifest (many lists yield the same flags); the digest in
        # `inputs.high_stakes` stays the fingerprint the server verifies it against.
        manifest["high_stakes_declared"] = list(high_stakes or [])
    manifest["manifest_id"] = _manifest_id(manifest)
    return manifest


def _digest(value) -> str | None:
    """A stable fingerprint of a non-ref-pair input, or None when it was not supplied."""
    if value is None:
        return None
    material = value if isinstance(value, str) else "\n".join(sorted(value))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _divergence_rows(derived: list[str], declared: list[str] | None) -> list[dict]:
    """Scope-divergence rows: what the spec engages but never declares, and the reverse.

    A spec whose stated scope does not match the surfaces it actually reaches has a defect
    in its scope statement, worth surfacing regardless of coverage. When no declared scope
    is supplied the check cannot run — and that fact becomes its own row rather than a
    silent absence, because an unrun check that leaves no trace is indistinguishable from a
    check that passed.
    """
    if declared is None:
        return [_row("divergence", "divergence:unchecked:no-declared-scope-supplied")]
    declared_set = {d.strip() for d in declared if d.strip()}
    derived_set = set(derived)
    rows = [
        _row("divergence", f"divergence:undeclared:{p}")
        for p in sorted(derived_set - declared_set)
    ]
    rows += [
        _row("divergence", f"divergence:unengaged:{p}")
        for p in sorted(declared_set - derived_set)
    ]
    return rows


# --- the question denominator (B.14 Part B, code mode) ---------------------


def _changed_line_universe(
    repo_root: str | Path, base: str, commit: str, changed: list[str]
) -> dict[tuple[str, str], set[int]]:
    """Every changed line of the ref pair, as ``(path, side) -> line set``.

    The derivation machinery is kept and REPURPOSED (B-4): from row-generator to
    completeness instrument. This set is what makes it impossible to silently omit a
    piece of the change — every line here must be referenced by at least one block.
    """
    universe: dict[tuple[str, str], set[int]] = {}
    for path in changed:
        for side in ("old", "new"):
            lines: set[int] = set()
            for start, end in _changed_line_ranges(repo_root, base, commit, path, side=side):
                lines.update(range(start, end + 1))
            if lines:
                universe[(path, side)] = lines
    return universe


def _format_lines(lines: set[int]) -> str:
    """Compress a line set to ``12-14, 17`` for refusal messages."""
    out: list[str] = []
    run_start = run_end = None
    for n in sorted(lines):
        if run_start is None:
            run_start = run_end = n
        elif n == run_end + 1:
            run_end = n
        else:
            out.append(str(run_start) if run_start == run_end else f"{run_start}-{run_end}")
            run_start = run_end = n
    if run_start is not None:
        out.append(str(run_start) if run_start == run_end else f"{run_start}-{run_end}")
    return ", ".join(out)


def _resolve_stakes_locator(
    repo_root: str | Path,
    base: str,
    commit: str,
    locator: str,
    universe: dict[tuple[str, str], set[int]],
) -> set[tuple[str, str, int]]:
    """Resolve one declared high-stakes locator into sided changed lines (B-2).

    The grammar introduces nothing new — the three existing row-locator forms, resolved
    by the SAME parsers the derivation already uses: a path resolves to every changed
    line of that file on both sides; ``path::symbol`` to the changed lines within the
    symbol's boundaries on each side where the symbol exists; ``path::heading chain``
    likewise for the Markdown section. The resolved sided line set is the overlap
    operand, in the same coordinates as block references.
    """
    path, sep, tail = locator.partition("::")
    resolved: set[tuple[str, str, int]] = set()
    if not sep:
        for side in ("old", "new"):
            resolved.update((path, side, n) for n in universe.get((path, side), ()))
        return resolved
    for side, ref in (("old", base), ("new", commit)):
        source = _read_at(repo_root, ref, path)
        if source is None:
            continue
        spans = _markdown_sections(source) if path.endswith(".md") else _symbol_spans(source)
        for name, s_start, s_end in spans:
            if name == tail:
                resolved.update(
                    (path, side, n)
                    for n in universe.get((path, side), ())
                    if s_start <= n <= s_end
                )
    return resolved


def _code_block_rows(
    repo_root: str | Path,
    base: str,
    commit: str,
    blocks: list,
    high_stakes: list[str] | None,
) -> tuple[list[dict], list[dict]]:
    """Validate the development-sliced blocks and derive the block-axis rows (B-2..B-4).

    Returns ``(rows, frozen_blocks)``: two rows per block (``<block_id>::change`` and
    ``<block_id>::seam``) and the complete slicing as it rides the manifest payload —
    every block with its prose claim, its references, and its computed ``high_stakes``
    flag. Raises ``ManifestError`` on the per-manifest refusal set of B-4: a malformed
    block; a block reference outside the ref-pair diff; a changed line referenced by no
    block; a declared high-stakes locator that overlaps no block.
    """
    if not isinstance(blocks, list) or not blocks:
        raise ManifestError(
            "code mode requires the slicing: a non-empty `blocks` list of "
            "{block_id, claim, refs} — the denominator is the development LLM's semantic "
            "cut of the change, not a parser's place list (B.14 B-1/B-2)"
        )
    changed = _changed_paths(repo_root, base, commit)
    universe = _changed_line_universe(repo_root, base, commit, changed)

    frozen: list[dict] = []
    seen_ids: set[str] = set()
    ref_lines_by_block: dict[str, set[tuple[str, str, int]]] = {}
    for i, block in enumerate(blocks):
        where = f"blocks[{i}]"
        if not isinstance(block, dict):
            raise ManifestError(f"{where}: each block must be an object")
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id.strip():
            raise ManifestError(f"{where}: `block_id` must be a non-empty string")
        if "::" in block_id:
            raise ManifestError(
                f"{where}: `block_id` {block_id!r} may not contain '::' — it is the "
                "axis separator of the derived row ids"
            )
        if block_id in seen_ids:
            raise ManifestError(
                f"duplicate block_id {block_id!r} — block ids are unique within the manifest"
            )
        seen_ids.add(block_id)
        claim = block.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            raise ManifestError(
                f"{where} ({block_id}): `claim` must name the block's coherent claim in prose"
            )
        refs = block.get("refs")
        if not isinstance(refs, list) or not refs:
            raise ManifestError(
                f"{where} ({block_id}): `refs` must be a non-empty list of changed-line "
                "references {path, side: old|new, start, end}"
            )
        lines: set[tuple[str, str, int]] = set()
        for j, ref in enumerate(refs):
            rwhere = f"{where} ({block_id}) refs[{j}]"
            if not isinstance(ref, dict):
                raise ManifestError(f"{rwhere}: each reference must be an object")
            path = ref.get("path")
            side = ref.get("side")
            start, end = ref.get("start"), ref.get("end")
            if not isinstance(path, str) or not path.strip():
                raise ManifestError(f"{rwhere}: `path` must be a non-empty string")
            if side not in ("old", "new"):
                raise ManifestError(f"{rwhere}: `side` must be 'old' or 'new', got {side!r}")
            for name, v in (("start", start), ("end", end)):
                if type(v) is not int or v < 1:
                    raise ManifestError(f"{rwhere}: `{name}` must be a positive integer")
            if start > end:
                raise ManifestError(f"{rwhere}: `start` must be <= `end`")
            changed_here = universe.get((path, side), set())
            outside = set(range(start, end + 1)) - changed_here
            if outside:
                raise ManifestError(
                    f"{rwhere}: lines {_format_lines(outside)} of {path} ({side}) are "
                    "outside the ref-pair diff — a block references only changed lines "
                    "(B.14 B-4)"
                )
            lines.update((path, side, n) for n in range(start, end + 1))
        ref_lines_by_block[block_id] = lines
        frozen.append({"block_id": block_id, "claim": claim, "refs": refs})

    # MECHANICAL completeness (B-4): every changed line in AT LEAST one block — never
    # "exactly one", which would force a false partition (the mapping is many-to-many).
    covered: set[tuple[str, str, int]] = set()
    for lines in ref_lines_by_block.values():
        covered |= lines
    unassigned_by_place: dict[tuple[str, str], set[int]] = {}
    for (path, side), line_set in universe.items():
        missing = {n for n in line_set if (path, side, n) not in covered}
        if missing:
            unassigned_by_place[(path, side)] = missing
    if unassigned_by_place:
        named = "; ".join(
            f"{path} ({side}): {_format_lines(lines)}"
            for (path, side), lines in sorted(unassigned_by_place.items())
        )
        raise ManifestError(
            f"changed lines referenced by no block — re-slice: {named} (B.14 B-4: the "
            "machine, not the author, guarantees factual completeness)"
        )

    # The declared high-stakes input feeds the flag by the DEFINED overlap mapping
    # (B-2): a declared locator marks EVERY block referencing at least one changed line
    # inside the locator's resolved span; a block is high_stakes exactly when at least
    # one declared locator overlaps it. A declared locator overlapping no block is a
    # refusal — a marker that protects nothing while appearing to.
    stakes_blocks: set[str] = set()
    for locator in {h.strip() for h in (high_stakes or []) if h.strip()}:
        span = _resolve_stakes_locator(repo_root, base, commit, locator, universe)
        overlapped = {bid for bid, lines in ref_lines_by_block.items() if lines & span}
        if not overlapped:
            raise ManifestError(
                f"declared high-stakes locator {locator!r} overlaps no block of the "
                "slicing — it would protect nothing while appearing to (B.14 B-2/B-4)"
            )
        stakes_blocks |= overlapped

    rows: list[dict] = []
    for block in frozen:
        block["high_stakes"] = block["block_id"] in stakes_blocks
        for axis in BLOCK_AXES:
            row = {
                "row_id": f"{block['block_id']}::{axis}",
                "kind": "block",
                "locator": f"{block['block_id']}::{axis}",
            }
            if block["high_stakes"]:
                # BOTH axis rows of a high-stakes block keep the evidence discipline
                # (B-3: the F-2 mechanism, unchanged, now per block-axis).
                row["high_stakes"] = True
            rows.append(row)
    return rows, frozen


def slicing_digest(blocks: list) -> str:
    """The canonical fingerprint of a slicing: block_id/claim/refs, flags excluded.

    One implementation for the builder and the server's verification of the carried
    `blocks` — two copies of a canonicalisation drift, and the drift would read as a
    manufactured mismatch between honest parties."""
    canonical = [
        {"block_id": b.get("block_id"), "claim": b.get("claim"), "refs": b.get("refs")}
        for b in blocks
    ]
    return _digest(json.dumps(canonical, ensure_ascii=False, sort_keys=True))


def manifest_id_for(payload: dict) -> str:
    """The content-addressed id of a manifest PAYLOAD, for the server to verify against.

    The same function the builder uses, exposed so the identity is computed in one place.
    Two implementations of "what this manifest's id is" would drift, and the drift would
    surface as a review that cannot converge for reasons nobody can locate.
    """
    return _manifest_id(payload)


def _manifest_id(manifest: dict) -> str:
    """A pure function of the tool version and the derived content.

    This is what makes the critic's verification mechanical rather than a judgement call:
    re-run the tool on the same ref pair, compare one string.
    """
    material = json.dumps(
        {
            "tool_version": manifest["tool_version"],
            "mode": manifest["mode"],
            "base": manifest["base"],
            "commit": manifest["commit"],
            "granularity": manifest["granularity"],
            "inputs": manifest["inputs"],
            # `high_stakes` participates: it changes what the server demands behind a
            # `reviewed-clean`, so a manifest that quietly dropped the flag must NOT
            # reproduce the same id when the critic re-runs the tool.
            "rows": [
                [r["row_id"], r["kind"], r["locator"], bool(r.get("high_stakes"))]
                for r in manifest["rows"]
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def manifest_message(manifest: dict, artifact_seq: int) -> dict:
    """The ``coverage_manifest`` channel payload for an artifact version."""
    message = {
        "artifact_seq": artifact_seq,
        "manifest_id": manifest["manifest_id"],
        "tool_version": manifest["tool_version"],
        # `mode` travels on the wire because the manifest is mode-specific by construction
        # (element rows in spec mode, symbol reach in code mode) and the server cross-checks
        # it against the artifact's. Omitting it let a spec-mode denominator gate a code
        # artifact, and in a binding that cannot re-run the tool nothing downstream would
        # have noticed: the fallback check compares base and commit, which match.
        "mode": manifest["mode"],
        "base": manifest["base"],
        "commit": manifest["commit"],
        "granularity": manifest["granularity"],
        "inputs": manifest["inputs"],
        "rows": manifest["rows"],
    }
    if manifest.get("blocks") is not None:
        # B.14 B-2/B-4: the critic receives the slicing TOGETHER WITH the diff and may
        # contest the cut itself — so the complete slicing travels on the wire.
        message["blocks"] = manifest["blocks"]
        # ...and so does the declared high-stakes list the flags were computed from —
        # the one other non-ref input a re-derivation needs (it is not recoverable
        # from the flags; the digest in `inputs` lets the server verify the carry).
        message["high_stakes_declared"] = manifest.get("high_stakes_declared", [])
    return message


# --- CLI -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Derive the coverage manifest for a review artifact (B.6 T1-4). Both "
            "development and the critic run THIS tool; a mismatch between the posted "
            "manifest and its output is a finding."
        )
    )
    ap.add_argument("--mode", required=True, choices=("code", "spec"))
    ap.add_argument("--base", required=True, help="the artifact ref pair's base")
    ap.add_argument("--commit", required=True, help="the artifact ref pair's commit")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--granularity", default="symbol", choices=GRANULARITIES)
    ap.add_argument(
        "--spec-file",
        help="spec mode: path to the spec body whose `###` headers carry the element ids",
    )
    ap.add_argument(
        "--spec-path",
        help="spec mode, REFERENTIAL subject (B.10 B-2): repository-relative path of "
             "the spec document, resolved as the file at --commit from --repo-root; "
             "the `inputs.spec_markdown` digest is then derived from the RESOLVED "
             "bytes, so manifest identity is unchanged between subject forms. "
             "Mutually exclusive with --spec-file (exactly one subject form)",
    )
    ap.add_argument(
        "--declared-scope-file",
        help="newline-separated declared-scope paths. Spec mode: what the spec DECLARES it "
             "touches — a check against the derived set (divergence rows), never a source. "
             "Code mode (B.8 C-1): the review's FOCUS, never a reading boundary — derived "
             "rows outside it stay in the denominator marked out_of_declared_scope, closed "
             "collectively by one operator exemption; high-stakes rows are never exemptable",
    )
    ap.add_argument(
        "--high-stakes-file",
        help="newline-separated row locators or spec element ids that are load-bearing — a "
             "`reviewed-clean` on these must cite a specific checked observation. Code mode "
             "(B.14 B-2): the locators feed the block-overlap mapping — a block is "
             "high_stakes exactly when at least one declared locator overlaps it",
    )
    ap.add_argument(
        "--blocks-file",
        help="code mode (B.14 B-1/B-2), REQUIRED there: JSON list of the development-"
             "sliced semantic blocks [{block_id, claim, refs: [{path, side: old|new, "
             "start, end}]}] — the question denominator. Every changed line of the ref "
             "pair must be referenced by at least one block, or the tool refuses",
    )
    ap.add_argument(
        "--genre",
        default=None,
        choices=(AUDIENCE_GENRE,),
        help="review genre — a SEPARATE axis from --mode. An audience review is carried as a "
        "text bundle (mode spec) but its denominator is the reader artifact's own sections "
        "and nothing else.",
    )
    ap.add_argument("--artifact-seq", type=int, help="emit the channel message payload")
    ap.add_argument("--max-rows", type=int, default=None)
    args = ap.parse_args(argv)

    # B.10 B-2: this tool runs where the repository is — it is a resolution boundary.
    # Semantic validity of the ref pair is verified here, BEFORE any derivation, and a
    # failure is a NAMED environmental refusal (exit 2), never a stack trace from a
    # git call six steps later.
    from assistant_memory.review import resolve as subject_resolve

    pair_faults = subject_resolve.pair_problems(args.repo_root, args.base, args.commit)
    if pair_faults:
        print("environmental refusal — the ref pair is not a subject:", file=sys.stderr)
        for reason in pair_faults:
            print(f"  - {reason}", file=sys.stderr)
        return 2

    if args.spec_file and args.spec_path:
        print(
            "manifest error: --spec-file and --spec-path are one subject form each — "
            "pass exactly one (B.10 B-1)",
            file=sys.stderr,
        )
        return 2
    if args.spec_path:
        try:
            spec_markdown = subject_resolve.resolve_spec_text(
                args.repo_root, args.commit, args.spec_path
            )
        except subject_resolve.ResolutionRefused as refusal:
            print(
                "environmental refusal — the referential spec subject does not "
                "resolve:",
                file=sys.stderr,
            )
            for reason in refusal.reasons:
                print(f"  - {reason}", file=sys.stderr)
            return 2
    else:
        spec_markdown = (
            Path(args.spec_file).read_text(encoding="utf-8") if args.spec_file else None
        )

    def _lines(path: str | None) -> list[str] | None:
        if not path:
            return None
        return [
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    declared = _lines(args.declared_scope_file)
    high_stakes = _lines(args.high_stakes_file)
    blocks = None
    if args.blocks_file:
        try:
            blocks = json.loads(Path(args.blocks_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"manifest error: --blocks-file unreadable: {exc}", file=sys.stderr)
            return 2
    try:
        manifest = build_manifest(
            mode=args.mode,
            base=args.base,
            commit=args.commit,
            repo_root=args.repo_root,
            granularity=args.granularity,
            genre=args.genre,
            spec_markdown=spec_markdown,
            declared_scope=declared,
            high_stakes=high_stakes,
            blocks=blocks,
            max_rows=args.max_rows,
        )
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    payload = (
        manifest_message(manifest, args.artifact_seq)
        if args.artifact_seq is not None
        else manifest
    )
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
