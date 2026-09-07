# SPDX-License-Identifier: Apache-2.0
"""The conventions home space, and feedback as a server-routed operation.

One test per element that states a refusal or a required behaviour, each written to FAIL
if its element is removed from the code — a refusal nobody exercises is indistinguishable
from a refusal that is not there. Two parts of the change have no code surface and so no
test here, deliberately: read-only-ness is a per-account MEMBERSHIP permission set in the
admin UI (never a space write floor — a floor would apply to the operator too, and the
seeder writes below the gate anyway), and the one-off graph migration is data, not code.

Nothing here reads its preconditions off whatever the database happens to hold. Several
guards ask a DEPLOYMENT-WIDE question ("is there a projection ANYWHERE"), so a test of one
takes ``unseeded_deployment`` to start from "never seeded" and then builds the state it
needs. That is what lets the same tests pass against an empty database and against a copy
of production — a test that borrows ambient data is green by luck, not by verification.
"""

import importlib.util
import inspect
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from assistant_memory.auth import issue_credential, resolve_credential
from assistant_memory.auth.service import ensure_owner, get_or_create_default_account
from assistant_memory.bootstrap import bootstrap, conventions_visible_to_owner, validate_configured_spaces
from assistant_memory.config import settings
from assistant_memory.install import state as install_state
from assistant_memory.models.install import SETTING_CONVENTIONS_SPACE_ID
from assistant_memory.conventions import (
    DOC_LABEL,
    FEEDBACK_HUB_LABEL,
    ProjectionElsewhere,
    seed_conventions,
)
from assistant_memory.mcp import server as mcp_server
from assistant_memory.mcp import tools
from assistant_memory.mcp.errors import NotConfigured
from assistant_memory.models.graph import Edge, Node, NodeSpace
from assistant_memory.models.identity import Membership, Space
from assistant_memory.repository import graph as repo

# --- helpers -------------------------------------------------------------


async def _grant(session, account, space, permission="admin"):
    session.add(Membership(account_id=account.id, space_id=space.id, permission=permission))
    await session.flush()


async def _principal(session, account, *, trust="trusted"):
    issued = await issue_credential(session, account_id=account.id, trust=trust)
    return await resolve_credential(session, issued.token)


async def _projection_count(session) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Node)
        .where(Node.type == "Document", Node.label == DOC_LABEL, Node.deleted_at.is_(None))
    )


async def _doc_in_space(session, space_id) -> Node | None:
    return await session.scalar(
        select(Node)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .where(
            NodeSpace.space_id == space_id,
            Node.type == "Document",
            Node.label == DOC_LABEL,
            Node.deleted_at.is_(None),
        )
    )


async def _parent_of(session, node_id) -> Node | None:
    return await session.scalar(
        select(Node)
        .join(Edge, Edge.dst_node == Node.id)
        .where(
            Edge.src_node == uuid.UUID(node_id),
            Edge.type == "contained_in",
            Edge.valid_to.is_(None),
        )
    )


def _seed_cli():
    """Import the publish CLI by path — scripts/ is not a package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed_conventions.py"
    spec = importlib.util.spec_from_file_location("_seed_conventions_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- configuration: unset is pre-install, dangling is a hard stop --------


async def test_unset_space_ids_are_the_pre_install_state(session, monkeypatch):
    """Neither id configured is a legitimate state — the installation is not finished yet."""
    monkeypatch.setattr(settings, "conventions_space_id", None)
    monkeypatch.setattr(settings, "feedback_space_id", None)
    await validate_configured_spaces(session)  # must not raise


async def test_dangling_conventions_space_id_fails_startup(session, monkeypatch):
    monkeypatch.setattr(settings, "conventions_space_id", uuid.uuid4())
    monkeypatch.setattr(settings, "feedback_space_id", None)
    with pytest.raises(RuntimeError, match="AM_CONVENTIONS_SPACE_ID"):
        await validate_configured_spaces(session)


async def test_dangling_feedback_space_id_fails_startup(session, monkeypatch):
    """The same requirement on the second id — the class, not just the instance."""
    monkeypatch.setattr(settings, "conventions_space_id", None)
    monkeypatch.setattr(settings, "feedback_space_id", uuid.uuid4())
    with pytest.raises(RuntimeError, match="AM_FEEDBACK_SPACE_ID"):
        await validate_configured_spaces(session)


# --- startup writes at most once per deployment --------------------------


async def test_fresh_install_creates_home_records_id_and_seeds(
    session, monkeypatch, unseeded_deployment
):
    """B.13 A-1: on a genuinely fresh install the bootstrap CREATES the conventions
    home itself, records its id into the install-settings store in the same
    transaction, and seeds into it — no administrative step and no restart is part
    of the stock path. The answer to "where would it go" is the recorded id."""
    monkeypatch.setattr(settings, "conventions_space_id", None)
    counts = await bootstrap(session)
    assert counts["created"] > 0
    assert await _projection_count(session) == 1
    recorded = await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    assert recorded is not None
    assert await _doc_in_space(session, uuid.UUID(str(recorded))) is not None
    # A restart seeds nothing further and keeps the same recorded id.
    counts = await bootstrap(session)
    assert counts["created"] == 0
    assert await _projection_count(session) == 1
    assert (
        await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    ) == recorded


async def test_unrecorded_home_with_candidates_is_a_named_refusal(
    session, account, space2, monkeypatch, unseeded_deployment
):
    """No recorded id + candidate same-purpose spaces = the bootstrap refuses to
    pick (a named error for the operator), and seeds nothing (B.13 A-1)."""
    await seed_conventions(session, space_id=space2.id, account_id=account.id, embedder=None)
    # Simulate the record being lost while the projection survives.
    monkeypatch.setattr(settings, "conventions_space_id", None)
    counts = await bootstrap(session)
    assert counts == {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    assert await _projection_count(session) == 1  # nothing new was invented
    assert (
        await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    ) is None  # and nothing was silently adopted


async def test_startup_seeds_nothing_when_a_projection_exists_anywhere(
    session, account, space, space2, monkeypatch, unseeded_deployment
):
    """A configured home is not enough either: the deployment is already seeded, so a restart
    must not create a second projection — not even into the space it is now pointed at.

    The precondition is BUILT here rather than borrowed from whatever the database happens to
    hold. An earlier version asserted "a projection already exists" against the ambient data
    and passed only because the test database was a copy of production; on an empty one it
    failed for a reason that had nothing to do with the behaviour under test.
    """
    await seed_conventions(session, space_id=space2.id, account_id=account.id, embedder=None)
    # The owner's membership makes the adoption itself legal, so the refusal under
    # test is the already-seeded guard, not the ownership check.
    owner = await ensure_owner(session)
    owner_account = await get_or_create_default_account(session, owner)
    session.add(
        Membership(account_id=owner_account.id, space_id=space.id, permission="admin")
    )
    await session.flush()
    monkeypatch.setattr(settings, "conventions_space_id", space.id)

    counts = await bootstrap(session)

    assert counts == {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    assert await _doc_in_space(session, space.id) is None
    assert await _projection_count(session) == 1


async def test_startup_seeds_the_configured_home_on_a_fresh_install(
    session, account, space, monkeypatch, unseeded_deployment
):
    """Both halves hold — this is the one moment startup is allowed to write.

    Adoption requires the OWNER's membership on the pre-provisioned space (A-1's
    existence-and-ownership rule), so the owner is created first and granted one.
    """
    owner = await ensure_owner(session)
    owner_account = await get_or_create_default_account(session, owner)
    session.add(
        Membership(account_id=owner_account.id, space_id=space.id, permission="admin")
    )
    await session.flush()
    monkeypatch.setattr(settings, "conventions_space_id", space.id)
    counts = await bootstrap(session)
    assert counts["created"] > 0
    assert await _doc_in_space(session, space.id) is not None
    assert await _projection_count(session) == 1


async def test_adoption_refuses_a_space_without_owner_membership(
    session, space, monkeypatch, unseeded_deployment
):
    """A-1: a pre-provisioned id is adopted after the SAME existence-and-ownership
    verification as every other routing-home consumer — a space the owner account
    has no membership in is refused, nothing recorded, nothing seeded (review
    f46a31d2, finding conventions-home-ownership-not-enforced)."""
    monkeypatch.setattr(settings, "conventions_space_id", space.id)
    counts = await bootstrap(session)
    assert counts == {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
    assert await _doc_in_space(session, space.id) is None
    assert (
        await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    ) is None


# --- the seeder stops being an automatic writer --------------------------


async def test_seeder_refuses_a_projection_in_another_space(
    session, account, space, space2, unseeded_deployment
):
    """The per-space dedup is blind to exactly the copy it is about to make."""
    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    with pytest.raises(ProjectionElsewhere, match=str(space.id)):
        await seed_conventions(session, space_id=space2.id, account_id=account.id, embedder=None)
    assert await _doc_in_space(session, space2.id) is None


async def test_seeder_never_resurrects_a_retired_node(
    session, account, space, unseeded_deployment
):
    """Retirement is an operator act; a publish pass is not entitled to reverse it."""
    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    doc = await _doc_in_space(session, space.id)
    section = await session.scalar(
        select(Node)
        .join(Edge, Edge.src_node == Node.id)
        .where(Edge.dst_node == doc.id, Edge.type == "contained_in", Edge.valid_to.is_(None))
        .limit(1)
    )
    await repo.update_node(
        session, node_id=section.id, account_id=account.id,
        expected_version=section.current_version_id, status="superseded",
    )

    counts = await seed_conventions(
        session, space_id=space.id, account_id=account.id, embedder=None
    )

    await session.refresh(section)
    assert section.status == "superseded"
    assert counts["skipped"] >= 1


async def test_seeder_ambiguity_is_an_error_not_a_lottery(
    session, account, space, unseeded_deployment
):
    """B.13 A-8: two same-labeled candidates make the seeder REFUSE with both ids
    named — never pick one by `limit(1)` chance (the recorded 2026-08-27 near-miss:
    a cancelled duplicate with 28 children relabeled because of that lottery)."""
    from assistant_memory.conventions import SeederAmbiguity

    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    doc = await _doc_in_space(session, space.id)
    twin = await repo.create_node(
        session, type="Document", space_id=space.id, account_id=account.id,
        label=doc.label, properties={"text": "a same-labeled twin"},
    )
    with pytest.raises(SeederAmbiguity) as err:
        await seed_conventions(
            session, space_id=space.id, account_id=account.id, embedder=None
        )
    assert str(doc.id) in str(err.value) and str(twin.id) in str(err.value)


async def test_seeder_does_not_build_the_feedback_hub(
    session, account, space, unseeded_deployment
):
    """Feedback belongs to the project, not to the conventions — and after the split a hub
    built here would be the only edge crossing the boundary, built in the wrong space."""
    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    hub = await session.scalar(
        select(Node)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .where(NodeSpace.space_id == space.id, Node.label == FEEDBACK_HUB_LABEL)
    )
    assert hub is None


async def test_publish_cli_refuses_without_a_target(session, monkeypatch):
    """The silent `--space-name personal` default is the proximate cause of the duplicate."""
    monkeypatch.setattr(settings, "conventions_space_id", None)
    cli = _seed_cli()
    with pytest.raises(SystemExit, match="AM_CONVENTIONS_SPACE_ID"):
        await cli._resolve_space(session, SimpleNamespace(space_id=None))


async def test_publish_cli_defaults_to_the_configured_home(session, space, monkeypatch):
    monkeypatch.setattr(settings, "conventions_space_id", space.id)
    cli = _seed_cli()
    resolved = await cli._resolve_space(session, SimpleNamespace(space_id=None))
    assert resolved.id == space.id


# --- what may be SERVED as the conventions -------------------------------


async def test_not_found_asks_for_access_and_never_for_a_re_seed(session, account, space):
    """The caller least able to judge it was being told to manufacture a second copy."""
    await _grant(session, account, space)
    principal = await _principal(session, account)
    res = await tools.conventions(session, principal)
    assert res["found"] is False
    message = res["message"].lower()
    assert "access" in message
    assert "seed_conventions.py" not in message
    assert "operator can seed" not in message


async def test_a_retired_projection_is_never_served_as_the_conventions(
    session, account, space, unseeded_deployment
):
    """The dangerous shape: a credential that can see the retired duplicate but not the
    home space would otherwise receive a stale spec silently."""
    await repo.create_node(
        session, type="Document", space_id=space.id, account_id=account.id,
        label=DOC_LABEL, properties={"version": "v1 (old)"}, status="superseded",
    )
    await _grant(session, account, space)
    principal = await _principal(session, account)
    res = await tools.conventions(session, principal)
    assert res["found"] is False
    assert "access" in res["message"].lower()


async def test_conventions_lookup_is_scoped_to_the_callers_spaces(
    session, account, space, space2, unseeded_deployment
):
    """The caller-visible scope is what makes "ask for read access" the right answer."""
    await seed_conventions(session, space_id=space2.id, account_id=account.id, embedder=None)
    await _grant(session, account, space)  # membership in the OTHER space only
    principal = await _principal(session, account)
    assert (await tools.conventions(session, principal))["found"] is False


# --- feedback is a server-routed operation -------------------------------


async def test_feedback_has_no_space_parameter(session):
    """Not missing — refused. The destination is not the caller's business, and handing it
    back is what left the channel dead (a refusal the tool's own signature could not
    satisfy)."""
    assert "space" not in inspect.signature(tools.feedback).parameters
    assert "space" not in mcp_server.SCHEMAS["feedback"]["properties"]
    assert "confirm" not in mcp_server.SCHEMAS["feedback"]["properties"]


async def test_feedback_ignores_the_callers_writable_spaces(
    session, account, space, space2, monkeypatch
):
    """Several writable spaces used to make the call impossible; now they are irrelevant."""
    monkeypatch.setattr(settings, "feedback_space_id", space.id)
    await _grant(session, account, space2, "write")
    principal = await _principal(session, account)
    res = await tools.feedback(session, principal, text="the destination is the server's")
    landed = await session.scalar(
        select(NodeSpace.space_id).where(NodeSpace.node_id == uuid.UUID(res["node"]["id"]))
    )
    assert landed == space.id


async def test_feedback_needs_no_write_permission_on_the_destination(
    session, account, space, feedback_destination
):
    """A restricted account is the case the channel exists for; checking its permission
    would close the channel for exactly that caller."""
    await _grant(session, account, space, "read")
    principal = await _principal(session, account)
    res = await tools.feedback(session, principal, text="reported from a read-only account")
    assert res["status"] == "ok"


async def test_feedback_is_not_queued_for_approval(
    session, account, space, feedback_destination
):
    """Queueing bug reports for confirmation defeats a cheap defect-reporting channel."""
    space.write_floor = "confirm"
    await session.flush()
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.feedback(session, principal, text="applies despite a confirm floor")
    assert res["status"] == "ok"
    assert res["node"]["status"] == "provisional"


async def test_feedback_stamps_the_conventions_version_it_cannot_see(
    session, account, space, space2, monkeypatch, unseeded_deployment
):
    """Deployment-wide on purpose: a reporter need not be able to READ the conventions in
    order to report on them."""
    monkeypatch.setattr(settings, "feedback_space_id", space.id)
    await repo.create_node(
        session, type="Document", space_id=space2.id, account_id=account.id,
        label=DOC_LABEL, properties={"version": "v9 (test edition)"}, status="current",
    )
    await _grant(session, account, space, "write")  # no membership in space2
    principal = await _principal(session, account)
    res = await tools.feedback(session, principal, text="stamped with the edition in force")
    assert res["node"]["properties"]["conventions_version"] == "v9 (test edition)"


async def test_feedback_omits_the_version_when_it_cannot_be_read(
    session, account, space, feedback_destination, unseeded_deployment
):
    """A report never fails because a version could not be read."""
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    res = await tools.feedback(session, principal, text="lands without a version")
    assert res["status"] == "ok"
    assert "conventions_version" not in res["node"]["properties"]


async def test_feedback_hub_is_found_in_the_destination_space_only(
    session, account, space, space2, monkeypatch
):
    """The old cross-space lookup was unordered, so the database picked the parent — and it
    could attach a node in one space to a parent in another."""
    monkeypatch.setattr(settings, "feedback_space_id", space.id)
    stray = await repo.create_node(
        session, type="Note", space_id=space2.id, account_id=account.id,
        label=FEEDBACK_HUB_LABEL, properties={"text": "a hub in the wrong space"},
    )
    await _grant(session, account, space2, "write")
    principal = await _principal(session, account)
    res = await tools.feedback(session, principal, text="parented in the destination")

    parent = await _parent_of(session, res["node"]["id"])
    assert parent is not None and parent.id != stray.id
    parent_space = await session.scalar(
        select(NodeSpace.space_id).where(NodeSpace.node_id == parent.id)
    )
    assert parent_space == space.id


async def test_feedback_refuses_when_no_destination_is_configured(
    session, account, space, monkeypatch
):
    """A node must live somewhere and guessing where is the defect — so it names the step."""
    monkeypatch.setattr(settings, "feedback_space_id", None)
    await _grant(session, account, space, "write")
    principal = await _principal(session, account)
    with pytest.raises(NotConfigured, match="AM_FEEDBACK_SPACE_ID"):
        await tools.feedback(session, principal, text="nowhere to go")


# --- the installation order, end to end ----------------------------------


async def test_installation_order_end_to_end(session, monkeypatch, unseeded_deployment):
    """The one case that belongs to no single element, in its two B.13 shapes.

    Pre-provisioned: an environment id is ADOPTED (verified, then recorded in the
    install-settings store) and the seed fires into it on the same start — the
    restart-to-see-the-id step of the old manual path is gone from the stock path.
    """
    # 1-2. The operator pre-provisioned a home space and configured its id.
    owner = await ensure_owner(session)
    owner_account = await get_or_create_default_account(session, owner)
    home = Space(name="conventions", template="shared", created_by=owner_account.id)
    session.add(home)
    await session.flush()
    session.add(Membership(account_id=owner_account.id, space_id=home.id, permission="admin"))
    await session.flush()
    monkeypatch.setattr(settings, "conventions_space_id", home.id)

    # 3. The first start adopts the id (recording it) and seeds into it.
    await bootstrap(session)
    assert await _projection_count(session) == 1
    assert await _doc_in_space(session, home.id) is not None
    assert await conventions_visible_to_owner(session) is True
    recorded = await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    assert recorded == str(home.id)

    # A further restart writes nothing more, and the RECORD now rules: the env
    # value could disappear and the home would still resolve.
    monkeypatch.setattr(settings, "conventions_space_id", None)
    counts = await bootstrap(session)
    assert counts["created"] == 0
    assert await _projection_count(session) == 1
