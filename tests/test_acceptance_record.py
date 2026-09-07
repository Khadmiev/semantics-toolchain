# SPDX-License-Identifier: Apache-2.0
"""The acceptance-record validator (B.13 D-1): allowlist schema + success conditions +
exclusion set + per-run forbidden identifiers + the canonical record digest."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from validate_acceptance_record import expected_digest, validate  # noqa: E402

SHA = "a" * 40


def _record_body() -> dict:
    return {
        "source_commit": SHA,
        "variant": "local",
        "composed_by": "in_vm",
        "clean_baseline": "fresh VM image, snapshot id pristine-01",
        "agent": {"name": "codex", "version": "0.147.0"},
        "steps": [
            {"step": "7 bring-up", "outcome": "green"},
            {"step": "10 finishing probe", "outcome": "green"},
        ],
        "failures": [
            {"description": "step 5 verify grep missed a key", "fix_commits": [SHA]}
        ],
        "day_after_walk": [
            {"item": "1 add a project", "walked": True, "outcome": "passed"},
        ],
        "update_fixture": {
            "baseline": {"tag": "fixture-base", "commit": SHA},
            "target": {"tag": "fixture-v2", "commit": SHA},
        },
        "final_clean_rerun": {"passed": True, "note": "clean from snapshot restore"},
        "compliance": {
            "control_1": {
                "environment_map_existed": True,
                "environment_map_timestamp": "2026-09-01T10:00:00Z",
            },
            "control_2": {
                "no_host_commands_attested": True,
                "evidence_redacted": "shell history: zero docker/psql/compose against host",
            },
            "control_3": {
                "vm_only_client_config": True,
                "screaming_name_pattern_match": "*-DISPOSABLE",
                "no_shared_tunnel": True,
                "no_production_credential": True,
            },
            "control_4": {
                "vm_destroyed_at": "2026-09-01T18:00:00Z",
                "export_exercised": True,
                "validator_run": "scripts/validate_acceptance_record.py: passed",
                "record_digest": "",
                "nothing_else_crossed": True,
            },
            "control_5": {"clean_baseline_restored": True},
        },
    }


def _valid_record() -> dict:
    record = _record_body()
    record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
    return record


def _write(tmp_path, record) -> Path:
    p = tmp_path / "record.json"
    p.write_text(json.dumps(record), encoding="utf-8")
    return p


def test_a_valid_record_passes(tmp_path):
    assert validate(_write(tmp_path, _valid_record())) == []


def test_unenumerated_field_is_refused(tmp_path):
    record = _valid_record()
    record["vm_ip_for_reference"] = "just a note"
    problems = validate(_write(tmp_path, record))
    assert any("schema" in p and "vm_ip_for_reference" in p for p in problems)


def test_missing_compliance_control_is_refused(tmp_path):
    record = _valid_record()
    del record["compliance"]["control_3"]
    problems = validate(_write(tmp_path, record))
    assert any("control_3" in p for p in problems)


def test_excluded_classes_are_refused(tmp_path):
    for note, cls in [
        ("db reachable at 192.0.2.10", "network address"),
        ("owner is owner@example.com", "account email"),
        ("tunnel demo.trycloudflare.com answered", "tunnel domain"),
        ("AM_SESSION_SECRET=abc123def456", "assignment carrying a value"),
        ("output:\nline1\nline2\nline3\n", "raw multi-line command output"),
    ]:
        record = _valid_record()
        record["final_clean_rerun"]["note"] = note
        record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
        problems = validate(_write(tmp_path, record))
        assert any("exclusion" in p for p in problems), f"not refused: {cls}"


def test_commit_ids_are_not_false_positives(tmp_path):
    # A 40-hex sha and a 64-hex digest are identifiers by contract, not secrets.
    record = _valid_record()
    problems = validate(_write(tmp_path, record))
    assert problems == []


# --- success conditions (finding acceptance-schema-accepts-incomplete-failed-run) --


def test_missing_update_fixture_is_refused(tmp_path):
    record = _record_body()
    del record["update_fixture"]
    record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
    problems = validate(_write(tmp_path, record))
    assert any("update_fixture" in p for p in problems)


def test_empty_steps_and_walk_are_refused(tmp_path):
    for field in ("steps", "day_after_walk"):
        record = _record_body()
        record[field] = []
        record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
        problems = validate(_write(tmp_path, record))
        assert any(field in p and "non-empty" in p.lower() or field in p for p in problems), field


def test_failed_clean_rerun_is_refused(tmp_path):
    record = _record_body()
    record["final_clean_rerun"]["passed"] = False
    record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
    problems = validate(_write(tmp_path, record))
    assert any("final_clean_rerun" in p for p in problems)


def test_false_attestations_are_refused(tmp_path):
    for path in [
        ("compliance", "control_1", "environment_map_existed"),
        ("compliance", "control_2", "no_host_commands_attested"),
        ("compliance", "control_3", "no_shared_tunnel"),
        ("compliance", "control_4", "nothing_else_crossed"),
        ("compliance", "control_5", "clean_baseline_restored"),
    ]:
        record = _record_body()
        node = record
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = False
        record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
        problems = validate(_write(tmp_path, record))
        assert any(path[-1] in p for p in problems), f"false {path[-1]} not refused"


def test_malformed_timestamp_is_refused(tmp_path):
    # Malformed, date-only, and timezone-naive values all refuse: the date-time
    # contract means a full timestamp with an offset (review f46a31d2, finding
    # acceptance-success-fields-remain-unconstrained).
    for bad in ("yesterday-ish", "2026-09-01", "2026-09-01T10:00:00"):
        record = _record_body()
        record["compliance"]["control_1"]["environment_map_timestamp"] = bad
        record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
        problems = validate(_write(tmp_path, record))
        assert any("environment_map_timestamp" in p for p in problems), bad


def test_empty_evidence_strings_are_refused(tmp_path):
    record = _record_body()
    record["compliance"]["control_2"]["evidence_redacted"] = ""
    record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
    problems = validate(_write(tmp_path, record))
    assert any("evidence_redacted" in p for p in problems)


def test_empty_forbidden_list_is_a_refusal(tmp_path):
    """An empty (or comment-only) --forbidden-file refuses instead of silently
    reducing the name classes to nothing (review f46a31d2, finding
    acceptance-exclusion-inputs-can-be-empty-or-miss-secrets)."""
    from validate_acceptance_record import main

    record_path = _write(tmp_path, _valid_record())
    empty = tmp_path / "forbidden.txt"
    empty.write_text("# comments only\n", encoding="utf-8")
    assert main([str(record_path), "--forbidden-file", str(empty)]) == 2
    # A real list on the same record still validates.
    real = tmp_path / "forbidden2.txt"
    real.write_text("SOME-VM-NAME\n", encoding="utf-8")
    assert main([str(record_path), "--forbidden-file", str(real)]) == 0


def test_host_composed_record_needs_no_export(tmp_path):
    record = _record_body()
    record["composed_by"] = "host"
    record["compliance"]["control_4"]["export_exercised"] = False
    record["compliance"]["control_4"]["record_digest"] = "none"
    assert validate(_write(tmp_path, record)) == []
    # ...and the in_vm/host pairing is enforced: a host record claiming an export
    # is refused.
    record["compliance"]["control_4"]["export_exercised"] = True
    problems = validate(_write(tmp_path, record))
    assert any("export_exercised" in p for p in problems)


# --- the canonical digest (finding acceptance-record-digest-self-reference) --------


def test_digest_is_verified_not_just_printed(tmp_path):
    record = _record_body()
    record["compliance"]["control_4"]["record_digest"] = "f" * 64
    problems = validate(_write(tmp_path, record))
    assert any("does not match the canonical subject" in p for p in problems)


def test_empty_digest_reports_the_expected_value(tmp_path):
    record = _record_body()  # record_digest is ""
    problems = validate(_write(tmp_path, record))
    want = expected_digest(record)
    assert any(want in p for p in problems)


def test_digest_is_not_self_referential(tmp_path):
    # Writing the expected digest into the record must NOT change what the digest
    # is expected to be — the subject empties the field before hashing.
    record = _record_body()
    want = expected_digest(record)
    record["compliance"]["control_4"]["record_digest"] = want
    assert expected_digest(record) == want
    assert validate(_write(tmp_path, record)) == []


# --- per-run forbidden identifiers (finding acceptance-exclusions-miss-named-identifiers)


def test_forbidden_identifiers_are_refused_when_listed(tmp_path):
    record = _valid_record()
    record["final_clean_rerun"]["note"] = (
        "verified against assistant_memory_test on laptop-01"
    )
    record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
    path = _write(tmp_path, record)
    problems = validate(path, forbidden=["assistant_memory_test", "laptop-01"])
    assert sum("forbidden identifier" in p for p in problems) == 2
    # The documented residual: with no list, bare names pass the mechanical checks.
    assert validate(path) == []


def test_preflight_failures_are_named_refusals(tmp_path):
    """Every nonzero exit is a NAMED refusal — swept by exception axis per read site
    (review f46a31d2, reopened finding
    acceptance-validator-preflight-has-unnamed-failures): invalid UTF-8 in either
    run-supplied file, and a valid non-object JSON record, must refuse, not
    traceback."""
    from validate_acceptance_record import main

    bad_bytes = b"\xff\xfe\x00broken"

    # invalid UTF-8 record
    rec = tmp_path / "rec.json"
    rec.write_bytes(bad_bytes)
    assert main([str(rec)]) == 1  # validate() names it "unreadable record"

    # invalid UTF-8 forbidden file beside a valid record
    good = _write(tmp_path, _valid_record())
    forb = tmp_path / "forb.txt"
    forb.write_bytes(bad_bytes)
    assert main([str(good), "--forbidden-file", str(forb)]) == 2

    # valid non-object JSON records: schema refusal, no AttributeError
    for payload in ("[1, 2, 3]", '"just a string"', "42"):
        p = tmp_path / "nonobject.json"
        p.write_text(payload, encoding="utf-8")
        assert main([str(p)]) == 1


def test_forbidden_matching_is_word_bounded(tmp_path):
    # A listed short name must not fire inside an unrelated longer token.
    record = _valid_record()
    record["final_clean_rerun"]["note"] = "restored from snapshot pristine-01"
    record["compliance"]["control_4"]["record_digest"] = expected_digest(record)
    problems = validate(_write(tmp_path, record), forbidden=["tine"])
    assert problems == []
