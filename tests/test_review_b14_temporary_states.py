# SPDX-License-Identifier: Apache-2.0
"""B.14 Part D: the register of declared known-temporary states.

Spec docs/design/2026-08-28_review_parallelism_and_question_denominator_spec.md, Part D.
The register is declared at review CREATION beside the threat frame (D-2), frozen into
the instrument snapshot with each entry anchored to the subject ref it was declared
against, delivered to the critic beside the frame (D-3), and audited on the wire by two
named carriers: `credited_temporary_entries` on the pass-ending critic status and
`contests_declaration` on a finding — the dissent route that classifies the finding as
a judgment entry at the round gate. Creation-only: there is no restatement channel.
"""

import httpx
import pytest
import pytest_asyncio

from assistant_memory.auth import issue_credential
from assistant_memory.db import get_session
from assistant_memory.main import app
from assistant_memory.models.identity import Account, User
from assistant_memory.review import round_gate as rg
from assistant_memory.review import watcher
from assistant_memory.review.errors import (
    InvalidMessagePayloadError,
    ReviewCreationRefusedError,
)
from assistant_memory.review.repository import append_message, create_review
from tests.instrument_helpers import seed_instruments

SUBJECT_REF = "a" * 40


def _entry(entry_id="stub-1", **over):
    base = {
        "entry_id": entry_id,
        "target": {
            "quote": "the public repository address will appear here",
            "locator": "docs/article.md::Verification",
        },
        "why": "the public repo does not exist until publication (Task 5ee3bc8f)",
        "closes": "release gate R5.5 — publication replaces the stub",
    }
    base.update(over)
    return base


async def _create(session, *, register=None, subject_ref=SUBJECT_REF, slug="tmpstates"):
    instrument = await seed_instruments(session)
    if register is not None:
        instrument = {**instrument, "temporary_states": register}
        if subject_ref is not None:
            instrument = {**instrument, "subject_ref": subject_ref}
    # these tests post no coverage manifest, and say so (B.11 A-1)
    return await create_review(
        session, slug=slug, mode="spec", instrument=instrument,
        config={"coverage": {"in_play": False}},
    )


# --- D-2: the freeze at creation ------------------------------------------


async def test_register_freezes_with_the_subject_anchor(session):
    issued = await _create(session, register=[_entry(), _entry("stub-2")])
    frozen = issued.review.config["instrument"]["temporary_states"]
    assert [e["entry_id"] for e in frozen] == ["stub-1", "stub-2"]
    for e in frozen:
        # the anchor: the subject state the entry was declared against, stamped at freeze
        assert e["declared_at"] == SUBJECT_REF
        assert e["target"]["quote"] and e["target"]["locator"]
        assert e["why"] and e["closes"]


async def test_empty_or_absent_register_freezes_as_empty(session):
    no_key = await _create(session, register=None, slug="nokey")
    assert no_key.review.config["instrument"]["temporary_states"] == []
    empty = await _create(session, register=[], subject_ref=None, slug="empty")
    assert empty.review.config["instrument"]["temporary_states"] == []


async def test_register_refusals_each_name_their_ground(session):
    cases = [
        # a register carrying duplicate ids is refused (D-2)
        ([_entry(), _entry()], SUBJECT_REF, "duplicate entry_id"),
        # an entry that cannot name its closing event is an open question, not a
        # temporary state
        ([_entry(closes="")], SUBJECT_REF, "closing event"),
        # the quote IS the binding coordinate — no quote, no declaration
        ([_entry(target={"quote": "", "locator": "x"})], SUBJECT_REF, "quoted span"),
        ([_entry(target={"quote": "q"})], SUBJECT_REF, "locator"),
        ([_entry(why=" ")], SUBJECT_REF, "why"),
        # entries without the anchor input: nothing to record `declared_at` from
        ([_entry()], None, "subject_ref"),
        ([_entry()], "has whitespace", "subject_ref"),
        ("not-a-list", SUBJECT_REF, "must be a list"),
    ]
    for register, subject_ref, match in cases:
        with pytest.raises(ReviewCreationRefusedError, match=match):
            await _create(session, register=register, subject_ref=subject_ref)


# --- D-3: delivery beside the threat frame --------------------------------


@pytest_asyncio.fixture
async def api(session):
    user = User(label="op", is_owner=True)
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label="personal")
    session.add(acc)
    await session.flush()
    await issue_credential(session, account_id=acc.id, label="bootstrap")

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_threat_frame_serves_the_register_beside_the_frame(api, session):
    issued = await _create(session, register=[_entry()])
    r = await api.get(
        f"/reviews/{issued.review.id}/threat-frame",
        headers={"Authorization": f"Bearer {issued.dev_token}"},
    )
    assert r.status_code == 200
    served = r.json()["temporary_states"]
    assert [e["entry_id"] for e in served] == ["stub-1"]
    assert served[0]["declared_at"] == SUBJECT_REF


def test_temporary_states_block_renders_only_when_declared():
    """An ordinary review pays no prompt tax; a declared register rides beside the
    frame with the three-case discipline and the dissent route in the block itself."""
    assert watcher.temporary_states_block(None) == ""
    assert watcher.temporary_states_block({"temporary_states": []}) == ""
    frame = {"temporary_states": [
        {"entry_id": "stub-1", "target": {"quote": "THE QUOTED SPAN", "locator": "l"},
         "why": "w", "closes": "c", "declared_at": SUBJECT_REF},
    ]}
    block = watcher.temporary_states_block(frame)
    assert "THE QUOTED SPAN" in block
    assert "NOT raised" in block
    assert "contests_declaration" in block
    assert "credited_temporary_entries" in block
    # ...and build_prompt carries the block next to the frame
    prompt = watcher.build_prompt(
        "CRITIC PROMPT", [], "CONTRACT", projection_path="p.jsonl", frame=frame
    )
    assert "DECLARED KNOWN-TEMPORARY STATES" in prompt


# --- D-3: the two wire carriers -------------------------------------------


async def _review_with_register(session, slug="wire"):
    issued = await _create(session, register=[_entry()], slug=slug)
    rid = issued.review.id
    await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "# s\n\n### E-1 — x\n"}},
    )
    return rid


async def test_contests_declaration_is_validated_against_the_register(session):
    rid = await _review_with_register(session)
    msg = await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": 1, "items": [
            {"id": "f-ok", "finding_type": "drift", "contests_declaration": "stub-1"},
            {"id": "f-unknown", "finding_type": "drift", "contests_declaration": "ghost"},
            {"id": "f-bad", "finding_type": "drift", "contests_declaration": 7},
            {"id": "f-plain", "finding_type": "drift"},
        ]},
    )
    kept = [f["id"] for f in msg.payload["items"]]
    # partial acceptance (B.8 A-3): the malformed contests reject their ITEMS, the
    # valid contest and the plain finding land
    assert kept == ["f-ok", "f-plain"]


async def test_credited_temporary_entries_ride_a_valid_status(session):
    rid = await _review_with_register(session, slug="credit")
    await append_message(
        session, review_id=rid, role="critic", kind="findings",
        payload={"artifact_seq": 1, "items": []},
    )
    msg = await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={
            "value": "needs_human", "artifact_seq": 1,
            "model": "gpt-5.2-codex", "effort": "high",  # the B.9 D-2 snapshot stamp
            "credited_temporary_entries": [{
                "entry_id": "stub-1", "case": "tracked",
                "evidence": {
                    "quote": "the public repository address will appear here",
                    "read_ref": "r1",
                    "tracking": "anchor..current shows no change touching the span",
                },
            }],
        },
    )
    assert msg.payload["credited_temporary_entries"][0]["entry_id"] == "stub-1"


async def test_credit_record_refusals(session):
    rid = await _review_with_register(session, slug="creditbad")
    good = {
        "entry_id": "stub-1", "case": "tracked",
        "evidence": {"quote": "q", "read_ref": "r1", "tracking": "t"},
    }
    cases = [
        # an unknown id cannot be credited into a creation-only register
        ([{**good, "entry_id": "ghost"}], "names no entry"),
        # one object per credited entry
        ([good, good], "duplicate credits"),
        # case 3 credits nothing — only the two evidence-backed cases exist
        ([{**good, "case": "undecidable"}], "case"),
        ([{**good, "evidence": {"quote": "q", "read_ref": "r1"}}], "tracking"),
        ("not-a-list", "must be a list"),
    ]
    for block, match in cases:
        with pytest.raises(InvalidMessagePayloadError, match=match):
            await append_message(
                session, review_id=rid, role="critic", kind="status",
                payload={
                    "value": "needs_human", "artifact_seq": 1,
                    "credited_temporary_entries": block,
                },
            )


# --- D-3: the contest is a judgment entry at the round gate ----------------


def test_contesting_finding_parks_as_a_judgment_entry():
    """The declaration was the operator's settlement, so only the operator unsettles
    it — whatever the proposal for the finding reads, it waits at the gate."""
    messages = [
        {"seq": 1, "role": "development", "kind": "artifact", "payload": {"mode": "spec"}},
        {"seq": 2, "role": "critic", "kind": "findings", "payload": {
            "artifact_seq": 1, "items": [
                {"id": "t1", "finding_type": "drift", "contests_declaration": "stub-1"},
            ],
        }},
        {"seq": 3, "role": "development", "kind": "proposals", "payload": {
            "entries": [{
                "finding_id": "t1",
                "context": "the declared stub, contested",
                "proposed_outcome": "fix",
                "plan": "do the thing",
                "class_closure_claim": {"kind": "cell", "scope": "this one call site"},
                "reason": "because",
            }],
        }},
    ]
    state = rg.compute(messages)
    entry = state.round.entries["t1"]
    assert entry.contests_declaration
    assert entry.judgment(state.mode)
    assert not entry.settled
    assert "t1" in state.round.waiting(state.mode)


# --- the credit is bound to the run's read log (finding
# b14-temporary-credit-evidence-unbound) --------------------------------------


def _credit_verdict(read_ref="r1", quote="the stub line", log=True):
    return {
        "read_log": ([{"id": "r1", "ground": "repo", "locator": "docs/a.md",
                       "version": "abc", "range": [1, 2]}] if log else []),
        "status": {"value": "needs_iteration", "artifact_seq": 1,
                   "credited_temporary_entries": [{
                       "entry_id": "stub-1", "case": "tracked",
                       "evidence": {"quote": quote, "read_ref": read_ref,
                                    "tracking": "anchor..current: no touch"}}]},
    }


def test_a_credit_citing_no_real_read_is_stripped():
    verdict = _credit_verdict(read_ref="ghost")
    stripped = watcher.verify_credit_evidence(verdict, lambda e: "the stub line here")
    assert [d["entry_id"] for d in stripped] == ["stub-1"]
    assert "credited_temporary_entries" not in verdict["status"]


def test_a_credit_whose_quote_the_read_did_not_serve_is_stripped():
    verdict = _credit_verdict(quote="never served text")
    stripped = watcher.verify_credit_evidence(verdict, lambda e: "the stub line here")
    assert stripped and "does not appear" in stripped[0]["reason"]
    assert "credited_temporary_entries" not in verdict["status"]


def test_a_proven_credit_is_kept_and_marked():
    # Round 12 (b14-temporary-credit-inline-current-version-unbound): with no
    # artifact anchor handed in, the kept credit's claim is NARROWED — the full
    # content_reread_by_watcher marking now requires the anchored path (see the
    # round-12 tests below).
    verdict = _credit_verdict()
    stripped = watcher.verify_credit_evidence(verdict, lambda e: "...the stub line...")
    assert stripped == []
    kept = verdict["status"]["credited_temporary_entries"]
    assert kept[0]["evidence_verification"].startswith("content_reread_by_watcher")
    assert "version-unbound" in kept[0]["evidence_verification"]


def test_without_a_reread_tract_the_credit_is_kept_form_only():
    verdict = _credit_verdict()
    stripped = watcher.verify_credit_evidence(verdict, None)
    assert stripped == []
    kept = verdict["status"]["credited_temporary_entries"]
    assert "form_and_coordinates_only" in kept[0]["evidence_verification"]


async def test_subject_ref_must_be_a_full_git_id(session):
    """The anchor is what every credit tracks from — a garbage anchor is
    unrepresentable now (full 40-hex, as `git rev-parse` emits it)."""
    for bad in ("short123", "deadbeef", "g" * 40):
        with pytest.raises(ReviewCreationRefusedError, match="40-hex"):
            await _create(session, register=[_entry()], subject_ref=bad, slug="badref")


def test_a_credit_of_a_foreign_quote_is_stripped_against_the_frozen_register():
    """The reopen's teeth: proving SOME read happened must not credit the entry — the
    cited read has to contain the entry's FROZEN quote (identity of the occurrence)."""
    register = [{"entry_id": "stub-1",
                 "target": {"quote": "the DECLARED span", "locator": "l"},
                 "why": "w", "closes": "c", "declared_at": "a" * 40}]
    verdict = _credit_verdict(quote="unrelated text the read serves")
    stripped = watcher.verify_credit_evidence(
        verdict, lambda e: "unrelated text the read serves, nothing else",
        register=register,
    )
    assert stripped and "FROZEN quote" in stripped[0]["reason"]
    assert "credited_temporary_entries" not in verdict["status"]


def test_a_credit_containing_the_frozen_quote_passes_the_register_check():
    register = [{"entry_id": "stub-1",
                 "target": {"quote": "the stub line", "locator": "l"},
                 "why": "w", "closes": "c", "declared_at": "a" * 40}]
    verdict = _credit_verdict()
    stripped = watcher.verify_credit_evidence(
        verdict, lambda e: "context, the stub line, context", register=register,
    )
    assert stripped == []


def test_a_credit_whose_anchor_does_not_resolve_is_stripped(tmp_path):
    """A random 40-hex anchor stops being creditable: the watcher resolves the entry's
    declared_at in the subject repository it sits in."""
    import subprocess as sp
    repo = tmp_path / "subj"
    repo.mkdir()
    sp.run(["git", "init", "-q", str(repo)], check=True)
    register = [{"entry_id": "stub-1",
                 "target": {"quote": "the stub line", "locator": "l"},
                 "why": "w", "closes": "c", "declared_at": "a" * 40}]
    verdict = _credit_verdict()
    stripped = watcher.verify_credit_evidence(
        verdict, lambda e: "the stub line", register=register,
        subject_repo=str(repo),
    )
    assert stripped and "does not resolve" in stripped[0]["reason"]


def test_a_credit_reading_a_stale_version_is_stripped():
    """Round 11, finding b14-temporary-credit-read-not-current: D-3's presence test is
    against the CURRENT artifact version — a quote surviving in an old snapshot (the
    declaration anchor, any stale commit) credits nothing."""
    verdict = _credit_verdict()  # read_log entry names version "abc"
    stripped = watcher.verify_credit_evidence(
        verdict, lambda e: "the stub line",
        artifact_commit="b" * 40,
    )
    assert stripped and "CURRENT version" in stripped[0]["reason"]
    assert "credited_temporary_entries" not in verdict["status"]


def test_a_credit_reading_the_current_version_is_kept():
    verdict = _credit_verdict()
    verdict["read_log"][0]["version"] = "b" * 12  # unambiguous prefix of the commit
    stripped = watcher.verify_credit_evidence(
        verdict, lambda e: "the stub line",
        artifact_commit="b" * 40,
    )
    assert stripped == []
    credits = verdict["status"]["credited_temporary_entries"]
    assert credits[0]["evidence_verification"] == "content_reread_by_watcher"


# Round 12, finding b14-temporary-credit-inline-current-version-unbound: the
# unanchored path is asserted in test_a_proven_credit_is_kept_and_marked above.


def test_an_anchored_current_credit_keeps_the_full_claim():
    verdict = _credit_verdict()
    verdict["read_log"][0]["version"] = "b" * 12
    stripped = watcher.verify_credit_evidence(
        verdict, lambda e: "the stub line", artifact_commit="b" * 40,
    )
    assert stripped == []
    credits = verdict["status"]["credited_temporary_entries"]
    assert credits[0]["evidence_verification"] == "content_reread_by_watcher"


@pytest.mark.parametrize("locator, fragment", [
    ("l", "arbitrary string"),                    # bare string, names nothing
    ("just words", "arbitrary string"),
    ("src/x.py::10-20", "line range locates code"),  # code-only form in spec mode
    ("::Verification", "path-like left side"),
    ("docs/a.md::", "path-like left side"),
])
async def test_spec_register_refuses_malformed_and_cross_mode_locators(
    session, locator, fragment
):
    """Round 13, finding b14-temporary-locator-shape-unvalidated-reopened: the
    grammar is CLOSED per mode — spec mode takes an element id or `path::section`,
    never a bare string and never a code line range."""
    with pytest.raises(ReviewCreationRefusedError) as exc:
        await _create(
            session,
            register=[_entry(target={"quote": "q", "locator": locator})],
            slug="grammar",
        )
    assert fragment in str(exc.value)


async def test_spec_register_accepts_element_id_and_documentary_span(session):
    issued = await _create(
        session,
        register=[
            _entry("stub-el", target={"quote": "q1", "locator": "T1-4"}),
            _entry("stub-doc", target={"quote": "q2", "locator": "docs/article.md::Verification"}),
        ],
        slug="grammar-ok",
    )
    frozen = issued.review.config["instrument"]["temporary_states"]
    assert [e["target"]["locator"] for e in frozen] == [
        "T1-4", "docs/article.md::Verification",
    ]


async def test_code_register_refuses_spec_forms_and_bare_strings(session):
    """Cross-mode: a spec element id names nothing in a repository."""
    from assistant_memory.review.repository import create_review as _cr
    from tests.instrument_helpers import seed_instruments as _si

    for locator, fragment in [
        ("T1-4", "path::symbol"),
        ("l", "path::symbol"),
        ("nopath::sym", "repository path"),
    ]:
        instrument = {
            **(await _si(session)),
            "temporary_states": [_entry(target={"quote": "q", "locator": locator})],
            "subject_ref": SUBJECT_REF,
        }
        with pytest.raises(ReviewCreationRefusedError) as exc:
            await _cr(session, slug=f"code-gr-{locator[:4]}", mode="code",
                      instrument=instrument,
                      config={"coverage": {"in_play": False}})
        assert fragment in str(exc.value)


async def test_code_register_accepts_symbol_and_range(session):
    from assistant_memory.review.repository import create_review as _cr
    from tests.instrument_helpers import seed_instruments as _si

    instrument = {
        **(await _si(session)),
        "temporary_states": [
            _entry("stub-sym", target={"quote": "q1", "locator": "src/x.py::alpha"}),
            _entry("stub-rng", target={"quote": "q2", "locator": "src/x.py::10-20"}),
        ],
        "subject_ref": SUBJECT_REF,
    }
    issued = await _cr(session, slug="code-gr-ok", mode="code", instrument=instrument,
                       config={"coverage": {"in_play": False}})
    frozen = issued.review.config["instrument"]["temporary_states"]
    assert len(frozen) == 2
