# SPDX-License-Identifier: Apache-2.0
"""B.9 Parts C+D — launch profiles, engine model lists, frozen instruments, the D-2 stamp.

The spec's claims under test, each at the layer that holds it:

- C-1/C-2: a profile is host-pair × engine; versions are immutable, stored only PROVEN
  (complete passing probe evidence from the profile's own pair), the active version is a
  pointer, rollback exists, a devalued version is not a rollback target.
- C-6: devaluation is a durable observation, host-bound; a foreign-environment refusal
  is a mislaunch and never devalues.
- C-7: a bypass is registered against the probe that justifies it.
- D-4: two entry classes; a verified run makes a model critic-selectable; the effort
  domain is a recorded interface fact.
- D-5: a default version demands the operator quote + a RESOLVING graph Decision ref;
  an override key must name a registered genre; resolution is override-else-global.
- C-5/D-1/D-3: creation refuses with a ROUTE for every missing ground; the snapshot is
  frozen into config; independence is graduated, conservative on ambiguous lineage; a
  self-check runs only under a typed operator waiver.
- D-2: every critic status must stamp model+effort EQUAL to the snapshot.
"""

import hashlib

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from assistant_memory.auth import issue_credential
from assistant_memory.db import get_session
from assistant_memory.main import app
from assistant_memory.models.graph import Node
from assistant_memory.models.identity import Account, User
from assistant_memory.review import instruments
from assistant_memory.review.errors import (
    InstrumentValidationError,
    InvalidMessagePayloadError,
    ReviewCreationRefusedError,
)
from assistant_memory.review.repository import append_message, create_review
from assistant_memory.review.watcher import result_messages
from tests.instrument_helpers import (
    TEST_HOSTNAME,
    TEST_USERNAME,
    make_content,
    make_evidence,
    make_verification,
    seed_instruments,
)


# --- profiles (C-1 / C-2 / C-6) -------------------------------------------


async def _profile(session):
    return await instruments.upsert_profile(
        session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex"
    )


async def test_profile_version_requires_complete_passing_evidence_from_own_pair(session):
    profile = await _profile(session)
    with pytest.raises(InstrumentValidationError, match="own environment"):
        await instruments.add_profile_version(
            session, profile_id=profile.id, content=make_content(),
            probe_evidence=make_evidence(hostname="OTHERBOX"),
        )
    with pytest.raises(InstrumentValidationError, match="no passing result"):
        await instruments.add_profile_version(
            session, profile_id=profile.id, content=make_content(),
            probe_evidence={**make_evidence(), "results": [
                {"name": "engine_version", "passed": False}
            ]},
        )


async def test_profile_content_paths_must_be_absolute(session):
    profile = await _profile(session)
    with pytest.raises(InstrumentValidationError, match="ABSOLUTE"):
        await instruments.add_profile_version(
            session, profile_id=profile.id,
            content=make_content(machinery_root="proj/repo"),
            probe_evidence=make_evidence(),
        )


async def test_new_profile_content_refuses_the_retired_repo_root_key(session):
    """B.14 A-4: the old two-role key cannot re-enter through habit — refused on NEW
    content whether it comes alone or beside the new key. Stored pre-split versions
    are honored at render instead (`machinery_root_of`)."""
    profile = await _profile(session)
    with pytest.raises(InstrumentValidationError, match="retired"):
        await instruments.add_profile_version(
            session, profile_id=profile.id,
            content=make_content(repo_root="C:\\proj\\repo"),  # beside machinery_root
            probe_evidence=make_evidence(),
        )
    legacy_only = make_content()
    legacy_only["repo_root"] = legacy_only.pop("machinery_root")
    with pytest.raises(InstrumentValidationError, match="retired"):
        await instruments.add_profile_version(
            session, profile_id=profile.id, content=legacy_only,
            probe_evidence=make_evidence(),
        )


def test_machinery_root_of_reads_legacy_stored_versions():
    """B.14 A-4: a version stored before the split carries `repo_root`; its value was
    always the machinery half of the old meaning (the probes proved the machinery
    environment, never a subject) — so render reads it as the machinery root."""
    assert instruments.machinery_root_of({"machinery_root": "C:\\m"}) == "C:\\m"
    assert instruments.machinery_root_of({"repo_root": "C:\\legacy"}) == "C:\\legacy"
    assert (
        instruments.machinery_root_of({"machinery_root": "C:\\m", "repo_root": "C:\\legacy"})
        == "C:\\m"
    )
    with pytest.raises(InstrumentValidationError, match="machinery_root"):
        instruments.machinery_root_of({"python": "C:\\py\\python.exe"})


async def test_codex_profile_sandbox_is_validated_read_only_at_write(session):
    """D-6: a configuration surface that can quietly re-open a closed security decision
    is how closed decisions get reopened — refused at version WRITE, not at launch."""
    profile = await _profile(session)
    with pytest.raises(InstrumentValidationError, match="read-only"):
        await instruments.add_profile_version(
            session, profile_id=profile.id,
            content=make_content(codex_cmd="C:/bin/codex.exe exec -s danger-full-access -"),
            probe_evidence=make_evidence(),
        )


async def test_codex_cmd_must_invoke_the_recorded_engine_binary(session):
    """Round 1, engine-binary-not-bound: the recorded absolute binary is a guard only
    where the launch actually uses it — a command invoking anything else is refused."""
    profile = await _profile(session)
    with pytest.raises(InstrumentValidationError, match="engine_binary"):
        await instruments.add_profile_version(
            session, profile_id=profile.id,
            content=make_content(codex_cmd="codex exec -s read-only -"),  # bare, PATH-resolved
            probe_evidence=make_evidence(),
        )
    # ...and equality is separator/case-insensitive: a backslashed recorded path binds
    # the same forward-slash command.
    version = await instruments.add_profile_version(
        session, profile_id=profile.id,
        content=make_content(engine_binary="C:\\bin\\Codex.exe"),
        probe_evidence=make_evidence(),
    )
    assert version.version == 1


async def test_probe_commands_refuse_shell_operators_expansions_and_backslashes(session):
    """Round 1, probe-shell-semantics-diverge: everything that could make the startup
    gate's shell and the watcher's argv disagree is refused at write."""
    profile = await _profile(session)
    for bad, match in (
        ("C:/x.exe --a && b", "plain argv"),
        ("C:/x.exe %PATH%", "different semantics"),
        ("C:/x.exe $HOME", "different semantics"),
        ("C:\\x.exe --version", "forward"),
        ("relative_tool --version", "ABSOLUTE"),
    ):
        with pytest.raises(InstrumentValidationError, match=match):
            await instruments.add_profile_version(
                session, profile_id=profile.id,
                content=make_content(probes=[{"name": "p", "cmd": bad}]),
                probe_evidence=make_evidence(probe_names=["p"]),
            )


async def test_version_pointer_moves_and_rolls_back_but_never_onto_devalued(session):
    profile = await _profile(session)
    v1 = await instruments.add_profile_version(
        session, profile_id=profile.id, content=make_content(), probe_evidence=make_evidence()
    )
    v2 = await instruments.add_profile_version(
        session, profile_id=profile.id, content=make_content(), probe_evidence=make_evidence()
    )
    assert (v1.version, v2.version) == (1, 2)
    assert profile.active_version_id == v2.id  # storing IS the acceptance act
    await instruments.activate_profile_version(session, profile_id=profile.id, version_id=v1.id)
    assert profile.active_version_id == v1.id  # rollback is a pointer move
    await instruments.record_profile_observation(
        session, profile_id=profile.id, version_id=v1.id, kind="devaluation",
        observation="probe_flip", observed_hostname=TEST_HOSTNAME,
        observed_username=TEST_USERNAME,
    )
    assert v1.devalued_at is not None
    with pytest.raises(InstrumentValidationError, match="devalued"):
        await instruments.activate_profile_version(
            session, profile_id=profile.id, version_id=v1.id
        )


async def test_devaluation_is_host_bound_and_mislaunch_never_devalues(session):
    """C-6: a launcher run in a foreign environment refuses correctly, but that refusal
    is a MISLAUNCH — recorded as such, never devaluing the source profile."""
    profile = await _profile(session)
    v1 = await instruments.add_profile_version(
        session, profile_id=profile.id, content=make_content(), probe_evidence=make_evidence()
    )
    with pytest.raises(InstrumentValidationError, match="MISLAUNCH"):
        await instruments.record_profile_observation(
            session, profile_id=profile.id, version_id=v1.id, kind="devaluation",
            observation="startup_gate_refusal", observed_hostname="LAPTOP-OTHER",
            observed_username="someone",
        )
    row = await instruments.record_profile_observation(
        session, profile_id=profile.id, version_id=v1.id, kind="mislaunch",
        observation="startup_gate_refusal", observed_hostname="LAPTOP-OTHER",
        observed_username="someone",
    )
    assert row.kind == "mislaunch"
    assert v1.devalued_at is None
    # ...and a "mislaunch" claimed from the profile's own pair is contradictory.
    with pytest.raises(InstrumentValidationError, match="devaluation observation"):
        await instruments.record_profile_observation(
            session, profile_id=profile.id, version_id=v1.id, kind="mislaunch",
            observation="startup_gate_refusal", observed_hostname=TEST_HOSTNAME,
            observed_username=TEST_USERNAME,
        )


async def test_bypasses_hang_on_their_probe_and_close(session):
    profile = await _profile(session)
    await instruments.add_profile_version(
        session, profile_id=profile.id, content=make_content(), probe_evidence=make_evidence()
    )
    # Round 1, bypass-probe-ref-unvalidated: the ref must resolve against the active
    # version's probes — a typo would hang the bypass on nothing, forever silent.
    with pytest.raises(InstrumentValidationError, match="names no probe"):
        await instruments.add_bypass(
            session, profile_id=profile.id, probe_ref="engine_versoin",
            trigger_text="typo'd ref",
        )
    b = await instruments.add_bypass(
        session, profile_id=profile.id, probe_ref="engine_version",
        trigger_text="stands while engine_version fails",
    )
    hunch = await instruments.add_bypass(
        session, profile_id=profile.id, probe_ref=None,
        trigger_text="model stubbornly does the wrong thing; recheck on next engine update",
    )
    assert {x.id for x in await instruments.open_bypasses(session, profile.id)} == {b.id, hunch.id}
    await instruments.close_bypass(session, bypass_id=b.id)
    assert [x.id for x in await instruments.open_bypasses(session, profile.id)] == [hunch.id]


# --- engine model list (D-4) ----------------------------------------------


async def test_verified_entry_requires_its_run_and_duplicates_refuse(session):
    with pytest.raises(InstrumentValidationError, match="verification-run"):
        await instruments.add_engine_model(
            session, engine="codex", invocation_alias="m1", provider="openai",
            entry_class="verified",
        )
    await instruments.add_engine_model(
        session, engine="codex", invocation_alias="m1", provider="openai",
        entry_class="facts_only",
    )
    with pytest.raises(InstrumentValidationError, match="update the entry"):
        await instruments.add_engine_model(
            session, engine="codex", invocation_alias="m1", provider="openai",
            entry_class="facts_only",
        )


async def test_facts_only_upgrades_to_verified_with_the_run(session):
    row = await instruments.add_engine_model(
        session, engine="codex", invocation_alias="m2", provider="openai",
        entry_class="facts_only",
    )
    with pytest.raises(InstrumentValidationError, match="verification-run"):
        await instruments.update_engine_model(session, model_id=row.id, entry_class="verified")
    updated = await instruments.update_engine_model(
        session, model_id=row.id, entry_class="verified", verification=make_verification()
    )
    assert updated.entry_class == "verified"


async def test_verification_record_demands_its_stated_meaning(session):
    """Round 1, verified-model-evidence-unstructured: admission to the critic role on
    'any non-empty dict' was a strong word on a weak check — the record must attest the
    D-4 meaning, and a failed run is refused, not recorded."""
    for broken, match in (
        ({"at": "2026-08-18"}, "must be a non-empty string"),  # no host pair
        (make_verification() | {"engine_accepted": False}, "not verification"),
        (make_verification() | {"sandbox_held": None}, "not verification"),
        ({k: v for k, v in make_verification().items() if k != "effort_checked"},
         "effort_checked"),
    ):
        with pytest.raises(InstrumentValidationError, match=match):
            await instruments.add_engine_model(
                session, engine="codex", invocation_alias="mv", provider="openai",
                entry_class="verified", verification=broken,
            )


# --- instrument default (D-5) ---------------------------------------------


async def _decision_node(session, account):
    node = Node(type="Decision", label="critic default", created_by=account.id)
    session.add(node)
    await session.flush()
    return node


def _pair(critic_model="gpt-5.2-codex", dev_model="claude-fable-5"):
    return {
        "development": {"engine": "claude-code", "model": dev_model},
        "critic": {"engine": "codex", "model": critic_model, "effort": "high"},
    }


async def test_default_version_demands_quote_and_resolving_decision_ref(session, account):
    node = await _decision_node(session, account)
    with pytest.raises(InstrumentValidationError, match="operator_quote"):
        await instruments.add_default_version(
            session, pair_map={"global": _pair()}, operator_quote="  ", decision_ref=node.id
        )
    with pytest.raises(InstrumentValidationError, match="record the decision first"):
        await instruments.add_default_version(
            session, pair_map={"global": _pair()}, operator_quote="да, так",
            decision_ref="00000000-0000-0000-0000-000000000000",
        )
    # a resolving node of the WRONG type is refused too — existence is not enough
    not_decision = Node(type="Note", label="not a decision", created_by=account.id)
    session.add(not_decision)
    await session.flush()
    with pytest.raises(InstrumentValidationError, match="not Decision"):
        await instruments.add_default_version(
            session, pair_map={"global": _pair()}, operator_quote="да, так",
            decision_ref=not_decision.id,
        )
    row = await instruments.add_default_version(
        session, pair_map={"global": _pair()}, operator_quote="да, так", decision_ref=node.id
    )
    assert row.version == 1


async def test_default_override_key_must_name_a_registered_genre(session, account):
    """A misspelled override must never sit dormant while the global pair silently
    applies — refused at version WRITE against the genre registry."""
    node = await _decision_node(session, account)
    with pytest.raises(InstrumentValidationError, match="genre"):
        await instruments.add_default_version(
            session,
            pair_map={"global": _pair(), "per_genre": {"audiense": _pair()}},
            operator_quote="q", decision_ref=node.id,
        )
    row = await instruments.add_default_version(
        session,
        pair_map={"global": _pair(), "per_genre": {"audience": _pair(dev_model="gpt-5.2-codex")}},
        operator_quote="q", decision_ref=node.id,
    )
    assert row.version == 1


async def test_default_resolution_is_override_else_global_and_freezes_its_source(
    session, account
):
    instrument = await seed_instruments(session)
    node = await _decision_node(session, account)
    await instruments.add_default_version(
        session, pair_map={"global": _pair()}, operator_quote="q", decision_ref=node.id
    )
    issued = await create_review(
        session, slug="defaulted", mode="spec",
        # both roles resolved from the default; the threat context is per-review
        # (gate-confirmed) and never part of the default pair — and so is the
        # subject under review (B.14 A-3)
        instrument={"host": instrument["host"],
                    "threat_context": instrument["threat_context"],
                    "subject_root": instrument["subject_root"]},
    )
    snap = issued.review.config["instrument"]
    assert snap["critic"]["model"] == "gpt-5.2-codex"
    assert snap["development"]["model"] == "claude-fable-5"
    assert snap["resolved_from_default"] == 1
    assert snap["independence"]["step"] == "different_provider"


# --- the creation gate (C-5 / D-1 / D-3 / D-4) ----------------------------


async def test_every_refusal_is_a_route(session):
    """C-5's refusal names the missing pair and points at the preparation path; D-4's
    names the entry to add or verify; D-3's the identity to fill. A first run on a new
    machine lands in setup, not in a mystery."""
    instrument = await seed_instruments(session)
    with pytest.raises(ReviewCreationRefusedError, match="preparation path"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "host": {"hostname": "NEWBOX", "username": "who"},
        })
    with pytest.raises(ReviewCreationRefusedError, match="add"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "critic": {"engine": "codex", "model": "unlisted", "effort": "high"},
        })
    with pytest.raises(ReviewCreationRefusedError, match="facts_only suffices"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "development": {"engine": "claude-code", "model": "unlisted"},
        })
    # facts-only critic: not critic-selectable until verified
    await instruments.add_engine_model(
        session, engine="codex", invocation_alias="facts-model", provider="openai",
        provider_model_id="facts-model", entry_class="facts_only",
    )
    with pytest.raises(ReviewCreationRefusedError, match="verification"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "critic": {"engine": "codex", "model": "facts-model", "effort": None},
        })
    # no instrument at all, on the current protocol
    with pytest.raises(ReviewCreationRefusedError, match="instrument"):
        await create_review(session, slug="x", mode="spec")


async def test_creation_requires_an_explicit_subject_root(session):
    """B.14 A-3: the subject under review is a per-review creation input — required
    and explicit for EVERY review, including reviews of the machinery repository
    itself. No default: 'the machinery root' as a fallback would silently re-create
    the two-role reading the split exists to kill."""
    instrument = await seed_instruments(session)
    without = {k: v for k, v in instrument.items() if k != "subject_root"}
    with pytest.raises(ReviewCreationRefusedError, match="subject_root"):
        await create_review(session, slug="nosubj", mode="spec", instrument=without)


async def test_subject_root_must_be_absolute_under_the_profiles_shell(session):
    """B.14 A-4: validated like profile paths are at profile write — against the shell
    the host's proven profile records, not the shell the server happens to run."""
    instrument = await seed_instruments(session)  # cmd-shell profile
    for bad in ("proj/rel", "/posix/on/cmd", "   "):
        with pytest.raises(ReviewCreationRefusedError, match="subject_root|ABSOLUTE"):
            await create_review(
                session, slug="relsubj", mode="spec",
                instrument={**instrument, "subject_root": bad},
            )


async def test_snapshot_freezes_the_subject_root(session):
    instrument = await seed_instruments(session)
    issued = await create_review(
        session, slug="subj", mode="spec",
        instrument={**instrument, "subject_root": "D:\\subject repo"},
    )
    assert issued.review.config["instrument"]["subject_root"] == "D:\\subject repo"


async def test_effort_is_free_within_the_recorded_domain_only(session):
    instrument = await seed_instruments(session)
    with pytest.raises(ReviewCreationRefusedError, match="outside the recorded domain"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "critic": {**instrument["critic"], "effort": "ultra"},
        })
    with pytest.raises(ReviewCreationRefusedError, match="choose"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "critic": {**instrument["critic"], "effort": None},
        })


async def test_missing_official_id_refuses_with_fill_in_the_identity(session):
    instrument = await seed_instruments(session)
    await instruments.add_engine_model(
        session, engine="claude-code", invocation_alias="anon", provider="anthropic",
        entry_class="facts_only",  # no provider_model_id
    )
    with pytest.raises(ReviewCreationRefusedError, match="fill in the identity"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "development": {"engine": "claude-code", "model": "anon"},
        })


async def test_self_check_needs_a_typed_waiver_and_is_marked(session):
    """D-3: full coincidence is allowed only as a MARKED self-check with a typed
    operator waiver, frozen into the snapshot."""
    instrument = await seed_instruments(session)
    # development declared on the same model the critic runs (via its codex entry)
    same = {**instrument, "development": {"engine": "codex", "model": "gpt-5.2-codex"}}
    with pytest.raises(ReviewCreationRefusedError, match="self_check_waiver"):
        await create_review(session, slug="x", mode="spec", instrument=same)
    issued = await create_review(
        session, slug="ok", mode="spec",
        instrument={**same, "self_check_waiver": {
            "granted_by": "operator", "operator_quote": "осознанно, самопроверка",
        }},
    )
    snap = issued.review.config["instrument"]
    assert snap["independence"]["step"] == "self_check"
    assert snap["self_check_waiver"]["granted_by"] == "operator"


async def test_independence_is_conservative_on_ambiguous_lineage(session):
    instrument = await seed_instruments(session)
    await instruments.add_engine_model(
        session, engine="claude-code", invocation_alias="mystery-model",
        provider="mysterylab", provider_model_id="mystery-1", entry_class="facts_only",
        # family deliberately unfilled
    )
    issued = await create_review(
        session, slug="cons", mode="spec",
        instrument={**instrument, "development": {"engine": "claude-code", "model": "mystery-model"}},
    )
    step = issued.review.config["instrument"]["independence"]
    assert step["step"] == "same_family"
    assert step["conservative"] is True


async def test_snapshot_is_frozen_with_profile_version_and_host(session):
    instrument = await seed_instruments(session)
    issued = await create_review(session, slug="snap", mode="spec", instrument=instrument)
    snap = issued.review.config["instrument"]
    assert snap["host"] == {"hostname": TEST_HOSTNAME, "username": TEST_USERNAME}
    assert snap["profile_version"] == 1
    assert snap["critic"]["provider_model_id"] == "gpt-5.2-codex"
    assert snap["development"]["provider"] == "anthropic"


async def test_devalued_active_version_refuses_creation(session):
    instrument = await seed_instruments(session)
    profile = await instruments.get_profile(
        session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex"
    )
    await instruments.record_profile_observation(
        session, profile_id=profile.id, version_id=profile.active_version_id,
        kind="devaluation", observation="version_divergence",
        observed_hostname=TEST_HOSTNAME, observed_username=TEST_USERNAME,
    )
    with pytest.raises(ReviewCreationRefusedError, match="known-devalued"):
        await create_review(session, slug="x", mode="spec", instrument=instrument)


# --- round 2: script-identifier whitelist + durable probe stretches -------


async def test_script_identifiers_are_whitelisted_at_every_write_site(session):
    """Round 2, rendered-profile-values-are-shell-injectable: host pair and probe names
    sit in the script's shell text OUTSIDE the quoter — a quote/&/$( ) would break out
    and execute before the watcher starts. Whitelist with refusal, at every write."""
    with pytest.raises(InstrumentValidationError, match="rendered into"):
        await instruments.upsert_profile(
            session, hostname='TEST"&calc', username="tester", engine="codex"
        )
    with pytest.raises(InstrumentValidationError, match="rendered into"):
        await instruments.upsert_profile(
            session, hostname="TESTBOX", username="te$(ster)", engine="codex"
        )
    profile = await _profile(session)
    with pytest.raises(InstrumentValidationError, match="rendered into"):
        await instruments.add_profile_version(
            session, profile_id=profile.id,
            content=make_content(probes=[{"name": "p$(x)", "cmd": "C:/x.exe --v"}]),
            probe_evidence=make_evidence(probe_names=["p$(x)"]),
        )
    # ...and at the creation gate for the frozen host pair (the render's other input)
    instrument = await seed_instruments(session)
    with pytest.raises(ReviewCreationRefusedError, match="rendered into"):
        await create_review(session, slug="x", mode="spec", instrument={
            **instrument, "host": {"hostname": 'EVIL" & calc & rem ', "username": "tester"},
        })


def test_probe_failure_stretch_is_reconstructed_from_the_channel():
    """Round 2, probe-recovery-state-is-process-local: the fault/flip notice pair makes
    the stretch durable; a fresh runner seeded from the replay still flips."""
    from assistant_memory.review.watcher import (
        PROBE_FAULT_PHASE,
        PROBE_FLIP_PHASE,
        probe_fault_state,
        profile_probes_runner,
    )

    replay = [
        {"kind": "notice", "payload": {"phase": PROBE_FAULT_PHASE, "probe": "a"}},
        {"kind": "notice", "payload": {"phase": PROBE_FAULT_PHASE, "probe": "b"}},
        {"kind": "notice", "payload": {"phase": PROBE_FLIP_PHASE, "probe": "b"}},
        {"kind": "notice", "payload": {"phase": "other", "probe": "c"}},
    ]
    assert probe_fault_state(replay) == {"a"}

    import sys
    python = sys.executable.replace("\\", "/")  # the profile-command contract: forward slashes
    runner = profile_probes_runner([("a", f"{python} -c pass")], cwd=None)
    # fresh process: empty memory; the seed restores the open stretch from the channel
    runner.seed_failed(probe_fault_state(replay))
    assert runner() == ["a"], "a seeded failed probe that now passes must FLIP"
    assert runner() == ["a"], "re-offered until the publisher confirms the post (round 9)"
    runner.pending_flips.remove("a")
    assert runner() == [], "no flip once confirmed"
    runner.seed_failed({"unknown-probe"})
    assert runner.last_failed == set(), "seeding is bounded by the runner's known probes"


def test_recovered_flip_survives_a_later_probe_failure(tmp_path):
    """Round 8, recovered-probe-flip-lost-on-later-failure: probe A's recovery observed
    before probe B's failure must not be lost — it rides `pending_flips` through the
    raise and is returned once the run completes."""
    import sys

    from assistant_memory.review.watcher import EnvironmentFault, profile_probes_runner

    python = sys.executable.replace("\\", "/")
    flag = (tmp_path / "b_ok").as_posix()
    runner = profile_probes_runner(
        [
            ("a", f"{python} -c pass"),
            ("b", f'{python} -c "import sys,os; sys.exit(0 if os.path.exists(\'{flag}\') else 1)"'),
        ],
        cwd=None,
    )
    runner.seed_failed({"a"})
    with pytest.raises(EnvironmentFault):  # A recovers, then B fails — flip must survive
        runner()
    assert runner.pending_flips == ["a"], "the observed flip survives the raise"
    with pytest.raises(EnvironmentFault):  # B still down: no duplicate pending entry
        runner()
    assert runner.pending_flips == ["a"]
    # round 10: a NEW failure of the same probe supersedes its unconfirmed recovery —
    # the stale flip must not later close the freshly opened stretch
    runner.seed_failed(set())  # no-op, keeps the runner's shape explicit
    assert "b" not in runner.pending_flips
    (tmp_path / "b_ok").write_text("ok")
    assert runner() == ["a", "b"]
    (tmp_path / "b_ok").unlink()  # B fails AGAIN while its flip is still unconfirmed
    with pytest.raises(EnvironmentFault):
        runner()
    assert runner.pending_flips == ["a"], "B's stale unconfirmed flip is superseded"
    (tmp_path / "b_ok").write_text("ok")
    # the full run offers A's pending flip — and B's fresh recovery again
    assert runner() == ["a", "b"]
    # round 9: the runner does NOT clear pending itself — a flip leaves the queue only
    # when the PUBLISHER confirms the channel post; until then it is re-offered
    assert runner() == ["a", "b"], "unconfirmed flips are re-offered"
    runner.pending_flips.remove("a")  # what the watch loop does after a confirmed post
    runner.pending_flips.remove("b")
    assert runner() == [], "confirmed flips are gone"


# --- round 4: flip notices name their bypasses; usage-scope bypass read ---


def test_flip_notice_helper_names_only_the_matching_bypasses():
    """Round 4, probe-flip-omits-bypass-identities: with several open bypasses, the
    recovery must identify WHICH records are due — not point at the registry."""
    from assistant_memory.review.watcher import bypasses_for_probe

    bypasses = [
        {"id": "b1", "probe_ref": "sandbox_read", "trigger_text": "stands while sandbox_read fails"},
        {"id": "b2", "probe_ref": "sandbox_read", "trigger_text": "second bypass, same probe"},
        {"id": "b3", "probe_ref": "engine_version", "trigger_text": "other probe"},
        {"id": "b4", "probe_ref": None, "trigger_text": "probe-less hunch"},
    ]
    assert [b["id"] for b in bypasses_for_probe(bypasses, "sandbox_read")] == ["b1", "b2"]
    assert bypasses_for_probe(bypasses, "nonexistent") == []


async def test_profile_bypasses_read_is_usage_scope(api, session):
    """The watcher holds only a review token; the bypass read exists at usage scope and
    reads the review's OWN frozen profile — the C-4 management boundary is untouched."""
    client, bootstrap = api
    instrument = await seed_instruments(session)
    profile = await instruments.get_profile(
        session, hostname=TEST_HOSTNAME, username=TEST_USERNAME, engine="codex"
    )
    await instruments.add_bypass(
        session, profile_id=profile.id, probe_ref="engine_version",
        trigger_text="stands while engine_version fails",
    )
    r = await client.post(
        "/reviews", headers=_auth(bootstrap),
        json={"slug": "byp", "mode": "spec", "instrument": instrument},
    )
    assert r.status_code == 200, r.text
    created = r.json()
    got = await client.get(
        f"/reviews/{created['review_id']}/profile-bypasses",
        headers=_auth(created["critic_token"]),
    )
    assert got.status_code == 200, got.text
    listed = got.json()["bypasses"]
    assert len(listed) == 1 and listed[0]["probe_ref"] == "engine_version"
    # no token — no read
    assert (await client.get(f"/reviews/{created['review_id']}/profile-bypasses")).status_code == 401


# --- round 5: transient invocation faults; singleton birth; atomic identity ----------


def test_transient_provider_failure_routes_environmentally():
    """Operator-confirmed fix (2026-08-18): a KNOWN transient provider marker on a
    non-zero critic invocation is an EnvironmentFault (fault-stretch retry, nothing
    spent), never the terminal death that consumed an operator gate live."""
    import sys

    from assistant_memory.review.watcher import EnvironmentFault, codex_invoker

    python = sys.executable.replace("\\", "/")
    capacity = codex_invoker(
        f'{python} -c "import sys; sys.stderr.write(\'Selected model is at capacity\'); sys.exit(1)"',
        None, 30.0,
    )
    with pytest.raises(EnvironmentFault, match="TRANSIENT"):
        capacity("prompt")
    # quota exhaustion and unknown failures stay TERMINAL — deliberately not transient
    terminal = codex_invoker(
        f'{python} -c "import sys; sys.stderr.write(\'insufficient_quota: hard limit\'); sys.exit(1)"',
        None, 30.0,
    )
    with pytest.raises(RuntimeError, match="critic invocation failed"):
        terminal("prompt")
    # ...with PRECEDENCE (round 6): a terminal marker wins over a courtesy transient
    # suffix in the same message — no endless retry of an exhausted quota.
    mixed = codex_invoker(
        f'{python} -c "import sys; sys.stderr.write(\'insufficient quota; please try again\'); sys.exit(1)"',
        None, 30.0,
    )
    with pytest.raises(RuntimeError, match="critic invocation failed"):
        mixed("prompt")
    # ...and a HUNG provider is environmental too (round 7): the timeout kills the
    # child and routes to the fault stretch instead of killing the watcher.
    hung = codex_invoker(f'{python} -c "import time; time.sleep(30)"', None, 1.0)
    with pytest.raises(EnvironmentFault, match="timeout"):
        hung("prompt")


async def test_default_singleton_birth_is_idempotent(session, account):
    """Round 5, default-singleton-first-write-race: the parent's birth targets ONE fixed
    id, so concurrent first-writers cannot mint twin parents."""
    node = await _decision_node(session, account)
    await instruments.add_default_version(
        session, pair_map={"global": _pair()}, operator_quote="q", decision_ref=node.id
    )
    parent = await session.scalar(select(instruments.InstrumentDefault))
    assert parent.id == instruments.INSTRUMENT_DEFAULT_SINGLETON_ID


def test_cmd_script_disables_delayed_expansion_before_profile_values():
    """Round 5, cmd-delayed-expansion-not-refused: the setlocal guard precedes every
    profile-derived value, so /V:ON callers cannot re-expand !NAME! tokens."""
    from tests.instrument_helpers import make_content
    from assistant_memory.review.launcher import render_launch_script

    script, _ = render_launch_script(
        review_id="0123456789abcdef",
        snapshot={"host": {"hostname": "TESTBOX", "username": "tester"},
                  "critic": {"engine": "codex", "model": "gpt-5.2-codex", "effort": "high"}},
        profile_key={"hostname": "TESTBOX", "username": "tester", "engine": "codex"},
        version_id="v" * 32, version_n=1, content=make_content(),
        resolved_ping={"primary": "prod_bot", "fallback": "prod_bot"},
    )
    guard = script.index("setlocal DisableDelayedExpansion")
    assert guard < script.index("TESTBOX"), "the guard must precede profile-derived text"


# --- the D-2 stamp --------------------------------------------------------


async def _b9_review_reviewing(session):
    issued = await create_review(
        session, slug="stamp", mode="spec", instrument=await seed_instruments(session)
    )
    rid = issued.review.id
    await append_message(
        session, review_id=rid, role="development", kind="artifact",
        payload={"mode": "spec", "bundle": {"spec_markdown": "# s\n"}},
    )
    return rid


async def test_critic_status_stamp_is_validated_for_equality(session):
    rid = await _b9_review_reviewing(session)
    with pytest.raises(InvalidMessagePayloadError, match="EQUAL"):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "artifact_seq": 1},  # no stamp
        )
    with pytest.raises(InvalidMessagePayloadError, match="EQUAL"):
        await append_message(
            session, review_id=rid, role="critic", kind="status",
            payload={"value": "needs_iteration", "artifact_seq": 1,
                     "model": "gpt-5.2-codex", "effort": "low"},  # foreign effort
        )
    msg = await append_message(
        session, review_id=rid, role="critic", kind="status",
        payload={"value": "needs_iteration", "artifact_seq": 1,
                 "model": "gpt-5.2-codex", "effort": "high"},
    )
    assert msg.payload["model"] == "gpt-5.2-codex"


async def test_development_status_is_not_stamp_gated(session):
    rid = await _b9_review_reviewing(session)
    # the D-2 stamp is the CRITIC pass's record; other roles' statuses are untouched
    await append_message(
        session, review_id=rid, role="development", kind="status",
        payload={"value": "needs_human", "artifact_seq": 1},
    )


# --- HTTP wiring: the usage/management split (C-4) ------------------------
#
# The layer nobody calls looks like it works — so the new endpoints are driven over the
# real app at least once: launcher render under a per-review token, management under the
# bootstrap credential, and the boundary between them in both directions.


@pytest_asyncio.fixture
async def api(session):
    user = User(label="op", is_owner=True)
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label="personal")
    session.add(acc)
    await session.flush()
    bootstrap = (await issue_credential(session, account_id=acc.id, label="bootstrap")).token

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, bootstrap
    finally:
        app.dependency_overrides.pop(get_session, None)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def test_launcher_endpoint_renders_under_the_review_token(api, session):
    client, bootstrap = api
    instrument = await seed_instruments(session)
    r = await client.post(
        "/reviews", headers=_auth(bootstrap),
        json={"slug": "b9", "mode": "spec", "instrument": instrument},
    )
    assert r.status_code == 200, r.text
    created = r.json()
    rid, dev = created["review_id"], created["dev_token"]

    got = await client.get(f"/reviews/{rid}/launcher", headers=_auth(dev))
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["hash"] == hashlib.sha256(body["script"].encode("utf-8")).hexdigest()
    assert rid in body["script"]
    assert body["filename"].endswith(".cmd")
    # deterministic: re-requesting reproduces the same bytes bit-for-bit
    again = (await client.get(f"/reviews/{rid}/launcher", headers=_auth(dev))).json()
    assert (again["script"], again["hash"]) == (body["script"], body["hash"])

    # B.9 self-hosting anchor: the service names its build commit BESIDE the script —
    # never inside the script bytes (the render stays deterministic); unset env → None.
    assert "service_commit" in body
    if body["service_commit"]:
        assert body["service_commit"] not in body["script"]
    else:
        assert body["service_commit"] is None

    # the C-4 boundary, both directions: a review token reaches no management surface...
    assert (await client.get("/reviews/profiles", headers=_auth(dev))).status_code == 401
    # ...and the management side lists what the bootstrap credential seeded
    listed = await client.get("/reviews/profiles", headers=_auth(bootstrap))
    assert listed.status_code == 200
    assert any(p["hostname"] == TEST_HOSTNAME for p in listed.json()["profiles"])


async def test_creation_refusal_reaches_the_wire_as_a_route(api, session):
    client, bootstrap = api
    r = await client.post(
        "/reviews", headers=_auth(bootstrap),
        json={"slug": "no-ground", "mode": "spec", "instrument": {
            "host": {"hostname": "NOBOX", "username": "no"},
            "critic": {"engine": "codex", "model": "m", "effort": "high"},
            "development": {"engine": "claude-code", "model": "d"},
        }},
    )
    assert r.status_code == 422
    assert "preparation path" in r.json()["detail"]


async def test_management_endpoints_round_trip(api, session):
    client, bootstrap = api
    p = await client.post(
        "/reviews/profiles", headers=_auth(bootstrap),
        json={"hostname": "WIREBOX", "username": "wire", "engine": "codex"},
    )
    assert p.status_code == 200, p.text
    pid = p.json()["id"]
    from tests.instrument_helpers import make_content, make_evidence

    v = await client.post(
        f"/reviews/profiles/{pid}/versions", headers=_auth(bootstrap),
        json={"content": make_content(), "probe_evidence": make_evidence("WIREBOX", "wire")},
    )
    assert v.status_code == 200, v.text
    m = await client.post(
        "/reviews/models", headers=_auth(bootstrap),
        json={"engine": "codex", "invocation_alias": "wire-model", "provider": "openai",
              "provider_model_id": "wire-model", "entry_class": "verified",
              "verification": make_verification("WIREBOX", "wire")},
    )
    assert m.status_code == 200, m.text
    bad = await client.post(
        f"/reviews/profiles/{pid}/versions", headers=_auth(bootstrap),
        json={"content": make_content(codex_cmd="C:/bin/codex.exe exec -"),
              "probe_evidence": make_evidence("WIREBOX", "wire")},
    )
    assert bad.status_code == 422 and "read-only" in bad.json()["detail"]


def test_watcher_stamps_the_snapshot_not_the_models_claim():
    """The watcher stamps FROM the frozen snapshot — whatever the model wrote about
    itself in the verdict is overwritten, because a self-description is not evidence."""
    stamp = {"model": "gpt-5.2-codex", "effort": "high"}
    out = result_messages(
        {
            "findings": {"items": [], "artifact_seq": 3},
            "status": {"value": "needs_iteration", "artifact_seq": 3, "model": "im-lying"},
        },
        3,
        instrument_stamp=stamp,
    )
    status = [m for m in out if m["kind"] == "status"][0]["payload"]
    assert status["model"] == "gpt-5.2-codex" and status["effort"] == "high"
