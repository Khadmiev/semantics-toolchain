# SPDX-License-Identifier: Apache-2.0
"""The install state machine (B.13 Part A: A-1, A-4, A-5).

Covers the fact-derived state: stage predicates and their ordering, the
configuration identity (one list with the config stage), the release
compare/promote step, the completion record, and the durable regression event
that advances the installation generation.
"""

import uuid

import pytest

from assistant_memory.auth.service import ensure_owner, get_or_create_default_account
from assistant_memory.config import settings
from assistant_memory.install import state as install_state
from assistant_memory.install.state import (
    compute_state,
    config_identity,
    get_setting,
    observe_regression,
    reconcile_release,
    release_identity_problem,
    set_setting,
    write_completion_record,
)
from assistant_memory.conventions import DOC_LABEL
from assistant_memory.models.identity import Membership, Space
from assistant_memory.models.install import (
    SETTING_ACTIVE_RELEASE,
    SETTING_CONVENTIONS_SPACE_ID,
    SETTING_INSTALLATION_GENERATION,
    SETTING_PENDING_RELEASE,
    InstallRegressionEvent,
)
from assistant_memory.repository import graph as repo
from assistant_memory.search import index_fts
from sqlalchemy import select

pytestmark = pytest.mark.asyncio  # async tests; the three sync identity tests warn harmlessly

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


@pytest.fixture
def configured(monkeypatch):
    """A fully configured instance (stage 2 closed) with a known executing identity."""
    monkeypatch.setattr(settings, "base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "google_client_id", "client-id.apps.example")
    monkeypatch.setattr(settings, "google_client_secret", "client-secret-value")
    monkeypatch.setattr(settings, "owner_email", "owner@example.com")
    monkeypatch.setattr(settings, "session_secret", "a-real-session-secret")
    monkeypatch.setenv("AM_GIT_COMMIT", COMMIT_A)
    monkeypatch.delenv("AM_GIT_TAG", raising=False)
    return settings


@pytest.fixture
def unconfigured(monkeypatch):
    """The shipped defaults: placeholders and no OAuth client (stage 2 open)."""
    monkeypatch.setattr(settings, "google_client_id", None)
    monkeypatch.setattr(settings, "google_client_secret", None)
    monkeypatch.setattr(settings, "owner_email", None)
    monkeypatch.setattr(settings, "session_secret", "dev-insecure-change-me")
    monkeypatch.delenv("AM_GIT_COMMIT", raising=False)
    return settings


async def _seed_install(session, *, bind_owner: bool = True):
    """Bring a test database to 'stages 1-4 hold': owner (bound), the DEFAULT space
    with its membership, the conventions home space+membership, the conventions
    Document with its FTS row, and the recorded routing id."""
    owner = await ensure_owner(
        session, google_sub="google-sub-1" if bind_owner else None
    )
    account = await get_or_create_default_account(session, owner)
    default = Space(
        name=install_state.DEFAULT_SPACE_NAME, template="personal", created_by=account.id
    )
    home = Space(name="conventions-home", template="personal", created_by=account.id)
    session.add_all([default, home])
    await session.flush()
    session.add_all(
        [
            Membership(account_id=account.id, space_id=default.id, permission="admin"),
            Membership(account_id=account.id, space_id=home.id, permission="admin"),
        ]
    )
    await session.flush()
    doc = await repo.create_node(
        session,
        type="Document",
        space_id=home.id,
        account_id=account.id,
        label=DOC_LABEL,
        properties={"text": "the conventions"},
    )
    await index_fts(session, doc)
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(home.id))
    await session.flush()
    return owner, account, home


# --- configuration identity (A-4: joined 1:1 to the required list) -----------------


def test_identity_absent_while_config_unclosed(unconfigured):
    assert config_identity() is None


def test_identity_changes_with_each_required_setting(configured):
    """Each case mutates exactly ONE setting from the same baseline and restores it
    before the next — the earlier loop wrote `monkeypatch.undo` without calling it,
    so mutations accumulated and later assertions could pass on an earlier change
    (review f46a31d2, finding config-identity-test-never-restores-settings)."""
    import pytest as _pytest

    base = config_identity()
    assert base is not None
    for name, value in [
        ("base_url", "https://elsewhere.example"),
        ("google_client_id", "other-client"),
        ("google_client_secret", "other-secret"),
        ("owner_email", "other@example.com"),
        ("session_secret", "other-session-secret"),
    ]:
        mp = _pytest.MonkeyPatch()
        try:
            mp.setattr(settings, name, value)
            changed = config_identity()
            assert changed is not None and changed != base, (
                f"required setting {name} must participate in the identity"
            )
        finally:
            mp.undo()
        assert config_identity() == base, f"baseline must be restored after {name}"


def test_identity_never_embeds_secret_values(configured):
    # The identity is a digest; this asserts the material rule at its seam:
    # secrets enter as digests (see _digest usage), and the output is one hash.
    identity = config_identity()
    assert settings.google_client_secret not in identity
    assert settings.session_secret not in identity


# --- stage predicates and status ---------------------------------------------------


async def test_fresh_instance_is_installing_with_seed_open(
    session, configured, unseeded_deployment
):
    state = await compute_state(session)
    assert state.status == "installing"
    assert not state.serving_open
    assert state.first_unclosed is not None
    # schema and config hold on a migrated, configured test database; the first
    # unclosed stage is the seed.
    assert state.first_unclosed.name == "seed"


async def test_unconfigured_instance_names_config_stage(session, unconfigured):
    state = await compute_state(session)
    assert state.first_unclosed.name == "config"
    missing = state.first_unclosed.detail["missing"]
    assert "google_client_id" in missing and "session_secret" in missing


async def test_seeded_unbound_owner_stops_at_owner_stage(session, configured):
    await _seed_install(session, bind_owner=False)
    state = await compute_state(session)
    assert state.first_unclosed.name == "owner"
    assert not state.serving_open


async def test_stages_one_to_four_open_serving_and_report_validating(
    session, configured
):
    await _seed_install(session)
    await reconcile_release(session)
    state = await compute_state(session)
    assert state.serving_open
    assert state.status == "validating"
    assert state.first_unclosed.name == "validated"


async def test_routing_id_must_resolve(session, configured):
    await _seed_install(session)
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(uuid.uuid4()))
    state = await compute_state(session)
    assert state.first_unclosed.name == "seed"
    assert "does not exist" in state.first_unclosed.detail["conventions_home"]


async def test_routing_home_needs_owner_membership(session, configured):
    """A-1's routing contract is existence AND ownership: a recorded id naming a
    space the owner has no membership in leaves the seed stage open (review
    f46a31d2, finding conventions-home-ownership-not-enforced)."""
    _, account, _ = await _seed_install(session)
    foreign = Space(name="foreign", template="personal", created_by=account.id)
    session.add(foreign)
    await session.flush()  # deliberately NO membership row
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(foreign.id))
    state = await compute_state(session)
    assert state.first_unclosed.name == "seed"
    assert "no membership" in state.first_unclosed.detail["conventions_home"]


async def test_default_space_and_membership_are_seed_facts(session, configured):
    """A-1 stage 3 names the default space and the owner's membership on it as seed
    facts: removing either reopens the stage (review f46a31d2, finding
    seed-stage-omits-default-space-facts)."""
    from sqlalchemy import delete as _delete

    from sqlalchemy import update as _update

    _, account, _ = await _seed_install(session)
    baseline = await compute_state(session)
    assert baseline.serving_open and baseline.first_unclosed.name == "validated"

    # The test database may hold committed default spaces from lifespan-boot tests
    # (the known order-dependence, see conftest.unseeded_deployment) — the predicate
    # legitimately accepts ANY of them, so each removal below sweeps them all.
    default_ids = list(
        await session.scalars(
            select(Space.id).where(Space.name == install_state.DEFAULT_SPACE_NAME)
        )
    )
    # Remove only the MEMBERSHIPS: the spaces survive, the stage must still open.
    await session.execute(
        _delete(Membership).where(
            Membership.space_id.in_(default_ids), Membership.account_id == account.id
        )
    )
    state = await compute_state(session)
    assert state.first_unclosed.name == "seed"
    assert "no default space" in state.first_unclosed.detail["default_space"]

    # Restore one membership, then remove the SPACE from the predicate's view
    # (renamed rather than row-deleted — committed spaces carry foreign keys; what
    # the predicate sees is the same: no default space).
    session.add(
        Membership(account_id=account.id, space_id=default_ids[0], permission="admin")
    )
    await session.flush()
    assert (await compute_state(session)).first_unclosed.name == "validated"
    await session.execute(
        _update(Space).where(Space.id.in_(default_ids)).values(name="not-the-default")
    )
    state = await compute_state(session)
    assert state.first_unclosed.name == "seed"
    assert "no default space" in state.first_unclosed.detail["default_space"]


async def test_projection_outside_the_home_leaves_seed_open(session, configured):
    """Stage 3 binds the projection TO the recorded home: an owner-visible
    projection in another space with an empty recorded home is not a closed seed
    (review f46a31d2, finding routing-home-not-joined-to-conventions-projection)."""
    _, account, home = await _seed_install(session)
    assert (await compute_state(session)).first_unclosed.name == "validated"

    # Re-point the recorded home at a fresh, empty, owner-member space: the
    # projection still exists and is owner-visible — but not IN the home.
    empty = Space(name="empty-home", template="personal", created_by=account.id)
    session.add(empty)
    await session.flush()
    session.add(Membership(account_id=account.id, space_id=empty.id, permission="admin"))
    await session.flush()
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(empty.id))
    state = await compute_state(session)
    assert state.first_unclosed.name == "seed"
    assert "IN the recorded home" in str(state.first_unclosed.detail["conventions_document"])


async def test_schema_probe_survives_a_missing_alembic_table(session, configured):
    """A failed probe SELECT must degrade to a computed `schema` failure, not poison
    the session's transaction (review f46a31d2, finding schema-probe-poisons-session:
    under PostgreSQL a failed statement aborts the transaction; the probe runs in a
    SAVEPOINT so the remaining stage queries still compute)."""
    from sqlalchemy import text as _text

    await session.execute(_text("ALTER TABLE alembic_version RENAME TO alembic_version_gone"))
    state = await compute_state(session)  # must not raise InFailedSQLTransaction
    schema_stage = state.stages[0]
    assert schema_stage.name == "schema" and not schema_stage.ok
    assert state.status == "installing"
    assert len(state.stages) == 5  # every later stage computed in the same session


# --- the release capture/apply step (A-4) ------------------------------------------


async def test_first_bring_up_adopts_and_promotes_executing_identity(
    session, configured
):
    await reconcile_release(session)
    active = await get_setting(session, SETTING_ACTIVE_RELEASE)
    assert active == {"tag": None, "commit": COMMIT_A}
    assert await get_setting(session, SETTING_PENDING_RELEASE) is None


async def test_tagged_first_bring_up_keeps_the_tag(session, configured, monkeypatch):
    """A-4: the first target is the FULL identity — 'tag where one exists, the commit
    always'. The tag rides the same inject step as the commit (AM_GIT_TAG), so a
    tag-deployed install records its tag as the base for later tag-to-tag updates
    (review f46a31d2, finding initial-release-bypasses-capture-and-loses-tag)."""
    monkeypatch.setenv("AM_GIT_TAG", "v1.0")
    await reconcile_release(session)
    active = await get_setting(session, SETTING_ACTIVE_RELEASE)
    assert active == {"tag": "v1.0", "commit": COMMIT_A}
    assert await get_setting(session, SETTING_PENDING_RELEASE) is None


async def test_malformed_executing_identity_refuses_end_to_end(
    session, configured, monkeypatch
):
    """The shared form contract holds END TO END, on the real path (review f46a31d2,
    findings initial-release-shared-capture-still-bypassed and
    malformed-first-release-not-end-to-end): a non-40-hex AM_GIT_COMMIT (a cmd shell
    leaving the substitution literal, a zip checkout) is refused by name at every
    actor — nothing recorded or promoted at reconcile, the STATUS names the malformed
    identity (not merely "no active release"), and the completion record refuses to
    certify it."""
    _, account, _ = await _seed_install(session)
    monkeypatch.setenv("AM_GIT_COMMIT", "$(git rev-parse HEAD)")
    await reconcile_release(session)
    assert await get_setting(session, SETTING_PENDING_RELEASE) is None
    assert await get_setting(session, SETTING_ACTIVE_RELEASE) is None
    state = await compute_state(session)
    assert state.release_refusal is not None and "malformed" in state.release_refusal
    assert not state.stages[4].ok
    record = await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert record is None


async def test_malformed_rebuild_of_an_installed_instance_is_named(
    session, configured, monkeypatch
):
    """The other cell of the same class: an INSTALLED instance rebuilt with a
    malformed GIT_COMMIT. The stage-5 release detail names the malformed identity
    instead of a bare mismatch, and the status refusal says the same."""
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)  # promotes {tag: None, commit: COMMIT_A}
    assert await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    ) is not None
    assert (await compute_state(session)).status == "installed"

    monkeypatch.setenv("AM_GIT_COMMIT", "$(git rev-parse HEAD)")
    state = await compute_state(session)
    validated = state.stages[4]
    assert validated.name == "validated" and not validated.ok
    assert "malformed" in validated.detail["release"]
    assert state.release_refusal is not None and "malformed" in state.release_refusal


def test_commit_form_rejects_a_trailing_newline():
    """Python's $ matches before a FINAL newline, so the validator must fullmatch:
    a 41-character substitution artifact ('<40 hex>\\n') is refused on both consumers
    at once — the environment and CLI paths share this one function (review f46a31d2,
    finding commit-form-regex-accepts-final-newline)."""
    from assistant_memory.install.state import commit_form_problem

    assert commit_form_problem("a" * 40) is None
    assert commit_form_problem("a" * 40 + "\n") is not None
    assert commit_form_problem("A" * 40) is not None  # uppercase is not the git form


def test_apply_cli_refuses_a_malformed_commit(capsys):
    """The update path's refusal, now phrased by the shared contract — pinned so the
    two paths cannot drift apart again silently."""
    from assistant_memory.install.apply import main as apply_main

    with pytest.raises(SystemExit) as excinfo:
        apply_main(["--commit", "abc123"])
    assert excinfo.value.code == 2
    assert "not a full 40-character" in capsys.readouterr().err


async def test_matching_pending_target_promotes_and_clears(session, configured, monkeypatch):
    # A FULL identity match — the rebuild carried the target's tag too (A-4).
    monkeypatch.setenv("AM_GIT_TAG", "v2")
    await set_setting(session, SETTING_ACTIVE_RELEASE, {"tag": "v1", "commit": COMMIT_B})
    await set_setting(session, SETTING_PENDING_RELEASE, {"tag": "v2", "commit": COMMIT_A})
    await reconcile_release(session)
    assert (await get_setting(session, SETTING_ACTIVE_RELEASE))["tag"] == "v2"
    assert await get_setting(session, SETTING_PENDING_RELEASE) is None


async def test_same_commit_different_tag_neither_promotes_nor_certifies(
    session, configured, monkeypatch
):
    """A-4 defines the identity as tag AND commit: a target sharing the commit but
    not the tag must not promote, and the status must not stay installed on the old
    proof (review f46a31d2, finding release-tag-not-compared-as-identity)."""
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)  # active {tag: None, commit: A}
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert (await compute_state(session)).status == "installed"

    # An apply records a re-tag of the SAME commit; the rebuild did not carry it.
    await set_setting(session, SETTING_PENDING_RELEASE, {"tag": "v2.1", "commit": COMMIT_A})
    await reconcile_release(session)
    assert (await get_setting(session, SETTING_ACTIVE_RELEASE))["tag"] is None  # no promotion
    state = await compute_state(session)
    assert "release tag mismatch" in state.release_refusal
    assert state.status == "validating"  # the old proof does not certify the new target

    # The rebuild carries the tag -> full match promotes; the OLD record does not
    # prove the new tag, so the status stays validating until a fresh probe.
    monkeypatch.setenv("AM_GIT_TAG", "v2.1")
    await reconcile_release(session)
    assert (await get_setting(session, SETTING_ACTIVE_RELEASE))["tag"] == "v2.1"
    state = await compute_state(session)
    assert state.status == "validating"
    assert "no record proves" in state.stages[4].detail["release"]

    # The fresh probe's record subject is the EXECUTING identity, tag included.
    record = await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert record.release_tag == "v2.1"
    assert (await compute_state(session)).status == "installed"


async def test_mismatched_pending_target_never_promotes(session, configured, monkeypatch):
    monkeypatch.setenv("AM_GIT_COMMIT", COMMIT_B)
    await set_setting(session, SETTING_ACTIVE_RELEASE, {"tag": "v1", "commit": COMMIT_B})
    await set_setting(session, SETTING_PENDING_RELEASE, {"tag": "v2", "commit": COMMIT_A})
    await reconcile_release(session)
    # The active release stays what it was; the pending target stays visible.
    assert (await get_setting(session, SETTING_ACTIVE_RELEASE))["tag"] == "v1"
    assert (await get_setting(session, SETTING_PENDING_RELEASE))["tag"] == "v2"
    state = await compute_state(session)
    assert state.release_refusal == "tree updated, old code executing"


async def test_pending_mismatch_holds_status_below_installed(session, configured, monkeypatch):
    """A-4: a pending target the executing identity does not match HOLDS the state
    below INSTALLED — the refusal is not a report beside a certificate (review
    f46a31d2, finding pending-release-mismatch-still-installed)."""
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert (await compute_state(session)).status == "installed"

    # An apply records a new target, but the restart still runs the OLD code.
    await set_setting(session, SETTING_PENDING_RELEASE, {"tag": "v2", "commit": COMMIT_B})
    state = await compute_state(session)
    assert state.release_refusal == "tree updated, old code executing"
    assert state.status == "validating"  # NOT installed
    validated = state.stages[4]
    assert not validated.ok
    assert "pending target not promoted" in validated.detail["release"]


def test_every_shipped_placeholder_keeps_config_open(configured, monkeypatch):
    """The placeholder vocabulary is ONE set across .env.example, the startup guard,
    and this predicate (review f46a31d2, finding
    sample-session-placeholder-passes-config): every shipped stand-in reads as
    missing."""
    from assistant_memory.config import PLACEHOLDER_SESSION_SECRETS

    assert "change-me-to-a-long-random-string" in PLACEHOLDER_SESSION_SECRETS
    for placeholder in PLACEHOLDER_SESSION_SECRETS:
        monkeypatch.setattr(settings, "session_secret", placeholder)
        assert config_identity() is None, f"placeholder passed as real: {placeholder!r}"


def test_env_example_placeholder_is_in_the_vocabulary(configured):
    """The shipped sample file and the vocabulary cannot drift apart silently: the
    value .env.example actually ships is asserted to be recognized."""
    from pathlib import Path

    from assistant_memory.config import PLACEHOLDER_SESSION_SECRETS

    example = Path(__file__).resolve().parent.parent / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        if line.startswith("AM_SESSION_SECRET="):
            shipped = line.split("=", 1)[1].strip()
            assert shipped in PLACEHOLDER_SESSION_SECRETS
            break
    else:
        raise AssertionError("no AM_SESSION_SECRET line in .env.example")


async def test_missing_executing_identity_is_a_named_refusal(session, configured, monkeypatch):
    monkeypatch.delenv("AM_GIT_COMMIT")
    await reconcile_release(session)
    assert await get_setting(session, SETTING_ACTIVE_RELEASE) is None
    state = await compute_state(session)
    assert "missing executing identity" in state.release_refusal


# --- the completion record and INSTALLED (A-4) -------------------------------------


async def test_probe_record_closes_stage_five(session, configured):
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)
    record = await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert record is not None
    assert record.generation == 1
    assert record.release_commit == COMMIT_A
    state = await compute_state(session)
    assert state.status == "installed"


async def test_record_refused_without_executing_identity(session, configured, monkeypatch):
    _, account, _ = await _seed_install(session)
    monkeypatch.delenv("AM_GIT_COMMIT")
    record = await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert record is None


async def test_new_code_reopens_validating_without_generation_advance(
    session, configured, monkeypatch
):
    """After the running release changes, the status is VALIDATING until a fresh
    probe writes a new record — no generation advance is involved (A-4)."""
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    # An update: pending recorded, restart under the new code (the rebuild carries
    # BOTH identity halves), promotion succeeds.
    await set_setting(session, SETTING_PENDING_RELEASE, {"tag": "v2", "commit": COMMIT_B})
    monkeypatch.setenv("AM_GIT_COMMIT", COMMIT_B)
    monkeypatch.setenv("AM_GIT_TAG", "v2")
    await reconcile_release(session)
    state = await compute_state(session)
    assert state.status == "validating"
    assert state.generation == 1
    assert not await observe_regression(session, state)  # not a regression
    # The fresh probe under the new code closes the loop again.
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert (await compute_state(session)).status == "installed"


# --- degradation and the generation (A-5) ------------------------------------------


async def test_structural_regression_records_event_and_advances_generation(
    session, configured
):
    _, account, home = await _seed_install(session)
    await reconcile_release(session)
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    # The regression: the routing id stops resolving (the space row is gone).
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(uuid.uuid4()))
    state = await compute_state(session)
    assert await observe_regression(session, state) is True
    events = (await session.execute(select(InstallRegressionEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].failed_stage == "seed"
    assert events[0].generation_before == 1 and events[0].generation_after == 2

    state = await compute_state(session)
    assert state.status == "degraded"  # installed-then-degraded, stage named
    assert state.first_unclosed.name == "seed"

    # Exact repair does NOT re-arm the old record: the generation moved.
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(home.id))
    state = await compute_state(session)
    assert state.status == "validating"
    assert state.generation == 2
    # A new loop-close writes a record for the current generation.
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert (await compute_state(session)).status == "installed"


async def test_config_identity_change_is_a_regression(session, configured, monkeypatch):
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    # A valid-but-different configuration: stages hold, the identity differs.
    monkeypatch.setattr(settings, "google_client_secret", "rotated-secret")
    state = await compute_state(session)
    assert state.serving_open
    assert await observe_regression(session, state) is True
    events = (await session.execute(select(InstallRegressionEvent))).scalars().all()
    assert events[0].failed_stage == "config_identity"
    assert (await compute_state(session)).status == "validating"


async def test_regression_event_is_recorded_once_per_episode(session, configured):
    _, account, _ = await _seed_install(session)
    await reconcile_release(session)
    await write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    await set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(uuid.uuid4()))
    state = await compute_state(session)
    assert await observe_regression(session, state) is True
    # A second observer of the same episode stands down: no record exists for the
    # advanced generation, so there is nothing further to retire.
    assert await observe_regression(session, await compute_state(session)) is False
    events = (await session.execute(select(InstallRegressionEvent))).scalars().all()
    assert len(events) == 1
    assert await install_state.get_generation(session) == 2


async def test_no_regression_before_first_install(session, configured):
    """A never-installed instance failing predicates is INSTALLING, not degraded,
    and records no events."""
    state = await compute_state(session)
    assert await observe_regression(session, state) is False
    assert state.status == "installing"
    assert await get_setting(session, SETTING_INSTALLATION_GENERATION) is None
