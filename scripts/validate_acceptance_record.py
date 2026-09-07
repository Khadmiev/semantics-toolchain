# SPDX-License-Identifier: Apache-2.0
"""Validate an acceptance run record before it crosses the VM boundary (B.13 D-1).

Four checks, all refusals:

1. **The allowlist IS the schema** (docs/acceptance/record.schema.json,
   additionalProperties=false throughout): a field the schema does not enumerate is
   refused. The schema also encodes the SUCCESS conditions (review f46a31d2, finding
   acceptance-schema-accepts-incomplete-failed-run): the update walk is required, the
   step and day-after lists are non-empty, the final clean rerun passed, and every
   compliance attestation is true — a failed or empty run is not a valid record.
   Date-time fields are checked for real (a format checker is registered; annotations
   alone check nothing).
2. **The exclusion set**, checked against every string VALUE in the record: hostnames
   and machine names, network addresses, tunnel domains, account emails, database
   names and identifiers, secret values, and raw command output must not appear.
   Pattern checks catch the mechanically detectable classes (addresses, emails,
   URLs/domains, secret-looking strings, multi-line raw output).
3. **The per-run forbidden-identifier list** (--forbidden-file): names only the run
   knows — the host and machine names and database names/identifiers from the run's
   environment map (D-2). The composing agent derives the list from the map INSIDE the
   VM before validation; any record string containing a listed identifier is refused.
   This is the enforcement for the name classes the static patterns cannot recognize
   (finding acceptance-exclusions-miss-named-identifiers). An EMPTY effective list is
   itself a refusal — an environment map always names at least the host and the
   database, so an empty derived list means the derivation step was skipped, and
   accepting it would silently reduce the name classes to nothing (finding
   acceptance-exclusion-inputs-can-be-empty-or-miss-secrets). With no flag at all
   those classes rest on the composing agent's own care, and the validator says so.

   **What the mechanical checks deliberately do NOT cover** (the claim, narrowed to
   what is enforceable): an arbitrary BARE SECRET VALUE — a session secret or OAuth
   secret pasted without any recognizable shape — is caught by no static pattern and
   is deliberately given no exact-value input: a plaintext file of live secrets to
   grep against would itself be the exfiltration hazard this contract exists to
   prevent. That residual class rests on the composing agent and on the runbook's
   B-4 rule that secrets never pass through the agent's hands at all — a record
   containing one is a B-4 breach upstream of validation. The note printed on every
   successful run names this delegation, always.
4. **The record digest** (compliance.control_4.record_digest) is verified, not merely
   printed. The digest SUBJECT is canonical and non-self-referential (finding
   acceptance-record-digest-self-reference): sha256 over the canonical JSON encoding
   (UTF-8, sorted keys, separators ",", ":") of the record with the record_digest
   value replaced by the empty string. An empty value is reported with the expected
   digest to write; a non-matching value is a refusal. "none" is legal only for a
   host-composed record (no export exists; the schema enforces the pairing).

Exit 0 = the record may ride the sanctioned export door (D-2 rule 4). Any other exit
is a NAMED REFUSAL: the run is red, the record is fixed inside the VM and
re-validated; an unvalidated file never crosses.

Usage:
    python scripts/validate_acceptance_record.py docs/acceptance/<run>.json \
        [--forbidden-file <one identifier per line, # comments>]
"""

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "acceptance" / "record.schema.json"

# The exclusion set (D-1), as mechanically detectable patterns.
_EXCLUSIONS: list[tuple[str, re.Pattern]] = [
    ("network address (IPv4)", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("account email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("tunnel domain", re.compile(r"\b[\w-]+\.(?:ngrok(?:-free)?\.(?:app|dev)|trycloudflare\.com|loca\.lt)\b", re.I)),
    ("URL / hostname", re.compile(r"\bhttps?://(?!localhost[:/])[^\s\"']+", re.I)),
    ("ssh target", re.compile(r"\b\w+@[\w.-]+\b")),
    ("secret-looking value", re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,})\b")),
    ("assignment carrying a value", re.compile(r"\w*(?:SECRET|TOKEN|PASSWORD|AUTHTOKEN)\w*\s*[=:]\s*\S+", re.I)),
    ("database DSN", re.compile(r"\b(?:postgres(?:ql)?(?:\+\w+)?|mysql|mongodb)://\S+", re.I)),
    ("raw multi-line command output", re.compile(r"\n.*\n.*\n")),
]

# Fields whose VALUES are identifiers by contract and legitimately match otherwise
# suspicious shapes (a git sha is not an address; a digest is not a secret). The
# per-run forbidden list is NOT subject to this exemption: a listed name is refused
# wherever it appears.
_EXEMPT_KEYS = {"source_commit", "commit", "record_digest", "fix_commits"}

_DIGEST_PATH = ("compliance", "control_4", "record_digest")

# jsonschema treats `format` as an annotation unless a checker is registered; the
# stdlib parse keeps this dependency-free (fromisoformat covers RFC-3339 timestamps).
_FORMATS = jsonschema.FormatChecker()


@_FORMATS.checks("date-time")
def _is_datetime(value) -> bool:
    """A full, timezone-aware RFC-3339 date-time. fromisoformat alone also accepts
    date-only and timezone-naive strings, which is exactly what slipped through
    (finding acceptance-success-fields-remain-unconstrained) — so the parse is
    followed by two explicit demands: a time part exists, and an offset exists."""
    if not isinstance(value, str):
        return True  # type errors are the schema's to report
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return "T" in value.upper() and parsed.tzinfo is not None


def _walk_strings(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _walk_strings(item, f"{path}[{i}]")
    elif isinstance(value, str):
        yield path, value


def expected_digest(record: dict) -> str:
    """The canonical, non-self-referential digest subject: the record with the
    record_digest VALUE replaced by the empty string, encoded as canonical JSON
    (UTF-8, sorted keys, compact separators)."""
    clone = copy.deepcopy(record)
    node = clone
    for key in _DIGEST_PATH[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            break
    if isinstance(node, dict) and _DIGEST_PATH[-1] in node:
        node[_DIGEST_PATH[-1]] = ""
    canonical = json.dumps(clone, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_forbidden(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def validate(record_path: Path, forbidden: list[str] | None = None) -> list[str]:
    """Return the list of refusals (empty = valid)."""
    problems: list[str] = []
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        # The exception surface of a run-supplied read is THREE-axis: the file
        # (OSError), its encoding (UnicodeDecodeError — a ValueError, caught by
        # neither of the other two), and its syntax (JSONDecodeError). The round-5
        # closure listed two of three (review f46a31d2, reopened finding
        # acceptance-validator-preflight-has-unnamed-failures).
        return [f"unreadable record: {exc}"]

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=_FORMATS)
    for err in sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "<root>"
        problems.append(f"schema: {where}: {err.message}")

    forbidden_rx = [
        (ident, re.compile(rf"(?<![\w-]){re.escape(ident)}(?![\w-])", re.I))
        for ident in (forbidden or [])
    ]
    for path, text in _walk_strings(record):
        for ident, rx in forbidden_rx:
            if rx.search(text):
                problems.append(
                    f"forbidden identifier: {path}: value contains {ident!r} from the "
                    "run's forbidden list — redact or rephrase before export"
                )
        leaf = path.rsplit(".", 1)[-1].split("[")[0]
        if leaf in _EXEMPT_KEYS:
            continue
        for name, pattern in _EXCLUSIONS:
            if pattern.search(text):
                problems.append(
                    f"exclusion: {path}: value matches the excluded class "
                    f"'{name}' — redact it (identities and outcomes only)"
                )
                break

    # The digest contract (canonical subject; see expected_digest). Schema errors may
    # coexist — a digest over a malformed record is still checkable and reported.
    # Every access to the PARSED value is type-guarded: a valid non-object record
    # (array/string/number) already carries the schema refusal above, and calling
    # .get on it would replace that named refusal with a bare AttributeError.
    compliance = record.get("compliance") if isinstance(record, dict) else None
    control_4 = compliance.get("control_4") if isinstance(compliance, dict) else None
    declared = control_4.get("record_digest") if isinstance(control_4, dict) else None
    if isinstance(declared, str) and declared not in ("none",):
        want = expected_digest(record)
        if declared == "":
            problems.append(
                f"digest: compliance.control_4.record_digest is empty — write the "
                f"canonical digest: {want}"
            )
        elif declared != want:
            problems.append(
                f"digest: compliance.control_4.record_digest does not match the "
                f"canonical subject (expected {want}) — recompute after the last edit"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("record", help="the acceptance record JSON file")
    ap.add_argument(
        "--forbidden-file",
        help="per-run forbidden identifiers (host/machine/database names from the "
        "run's environment map), one per line; without it those name classes rest on "
        "the composing agent alone",
    )
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    record_path = Path(args.record)
    try:
        forbidden = load_forbidden(Path(args.forbidden_file)) if args.forbidden_file else None
    except (OSError, UnicodeDecodeError) as exc:
        # Every nonzero exit is a NAMED refusal — an unreadable forbidden file must
        # not surface as a bare traceback (finding
        # acceptance-validator-preflight-has-unnamed-failures).
        print(f"REFUSED: cannot read --forbidden-file {args.forbidden_file}: {exc}")
        return 2
    # The EXPORTING case demands the list (finding
    # acceptance-in-vm-validator-allows-missing-forbidden-list): a record composed
    # in the VM is the one sanctioned export, and the runbook makes deriving the
    # denylist from the environment map mandatory before it — a validator that
    # passes without it lets the mandatory step be skipped by forgetting a flag.
    # A host-composed record has no export and keeps the flag optional.
    try:
        parsed = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        parsed = None  # unreadable records are refused by validate() below
    # A valid-JSON NON-OBJECT (array/string/number) is not a record; .get on it
    # would crash with a bare AttributeError instead of a named refusal (finding
    # acceptance-validator-preflight-has-unnamed-failures) — validate()'s schema
    # check names it, so the probe just declines to answer here.
    composed_by = parsed.get("composed_by") if isinstance(parsed, dict) else None
    if composed_by == "in_vm" and forbidden is None:
        print(
            f"REFUSED: {record_path} is an in-VM (exporting) record and no "
            "--forbidden-file was supplied — derive the forbidden list from the "
            "run's environment map (host/machine/database names) and pass it; the "
            "name classes must not rest on a forgettable flag for the one record "
            "that crosses the VM boundary"
        )
        return 2
    if forbidden is not None and not forbidden:
        # An environment map always names at least the host and the database; an
        # empty derived list means the derivation was skipped. Accepting it would
        # silently reduce the name classes to nothing while looking checked.
        print(
            f"REFUSED: {args.forbidden_file} is empty (or comments only) — derive the "
            "forbidden list from the run's environment map (host/machine/database "
            "names) before validating; an empty list is a skipped step, not a clean one"
        )
        return 2
    problems = validate(record_path, forbidden)
    if problems:
        print(f"REFUSED: {record_path} may not cross the VM boundary:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"valid: {record_path}")
    if forbidden is None:
        print(
            "note: no --forbidden-file supplied — host/machine/database NAMES were "
            "checked by no mechanism; the composing agent carries those classes alone"
        )
    print(
        "note: arbitrary bare secret VALUES are checked by no mechanism (no plaintext "
        "secrets list exists by design) — that class rests on the composing agent and "
        "the B-4 rule that secrets never pass through the agent's hands"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
