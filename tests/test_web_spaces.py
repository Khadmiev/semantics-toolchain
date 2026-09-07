# SPDX-License-Identifier: Apache-2.0
import re

from sqlalchemy import select

from assistant_memory.models.identity import Account, Membership, Space, User


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)


async def _other_account(session) -> Account:
    user = User(label="colleague")
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label="colleague")
    session.add(acc)
    await session.flush()
    return acc


async def test_create_space_makes_admin_membership(owner_client, session):
    client, _owner = owner_client
    page = await client.get("/spaces")
    csrf = _csrf_from(page.text)

    resp = await client.post(
        "/spaces", data={"csrf": csrf, "name": "household", "template": "shared"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    space = await session.scalar(select(Space).where(Space.name == "household"))
    assert space is not None
    perm = await session.scalar(
        select(Membership.permission).where(Membership.space_id == space.id)
    )
    assert perm == "admin"


async def test_add_change_remove_member(owner_client, session):
    client, _owner = owner_client
    page = await client.get("/spaces")
    await client.post(
        "/spaces", data={"csrf": _csrf_from(page.text), "name": "team", "template": "work"}
    )
    space = await session.scalar(select(Space).where(Space.name == "team"))
    colleague = await _other_account(session)

    detail = await client.get(f"/spaces/{space.id}")
    csrf = _csrf_from(detail.text)

    # add
    await client.post(
        f"/spaces/{space.id}/members",
        data={"csrf": csrf, "account_id": str(colleague.id), "permission": "read"},
    )
    perm = await session.scalar(
        select(Membership.permission).where(
            Membership.space_id == space.id, Membership.account_id == colleague.id
        )
    )
    assert perm == "read"

    # change (upsert)
    await client.post(
        f"/spaces/{space.id}/members",
        data={"csrf": csrf, "account_id": str(colleague.id), "permission": "write"},
    )
    perm = await session.scalar(
        select(Membership.permission).where(
            Membership.space_id == space.id, Membership.account_id == colleague.id
        )
    )
    assert perm == "write"

    # remove
    resp = await client.post(
        f"/spaces/{space.id}/members/{colleague.id}/remove",
        data={"csrf": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    gone = await session.scalar(
        select(Membership).where(
            Membership.space_id == space.id, Membership.account_id == colleague.id
        )
    )
    assert gone is None


async def test_non_member_cannot_view_space(owner_client, session):
    client, _owner = owner_client
    # a space owned by someone else, with no membership for the owner
    other = await _other_account(session)
    space = Space(name="private", template="personal", created_by=other.id)
    session.add(space)
    await session.flush()
    session.add(Membership(account_id=other.id, space_id=space.id, permission="admin"))
    await session.flush()

    resp = await client.get(f"/spaces/{space.id}")
    assert resp.status_code == 404


async def test_non_admin_cannot_modify(owner_client, session):
    client, _owner = owner_client
    # owner is only a reader here
    other = await _other_account(session)
    space = Space(name="readonly", template="personal", created_by=other.id)
    session.add(space)
    await session.flush()
    owner_account = await session.scalar(
        select(Account).where(Account.user_id == _owner.id)
    )
    session.add(Membership(account_id=owner_account.id, space_id=space.id, permission="read"))
    session.add(Membership(account_id=other.id, space_id=space.id, permission="admin"))
    await session.flush()

    # read-only member sees no admin forms, so grab a CSRF token from /spaces
    csrf = _csrf_from((await client.get("/spaces")).text)
    resp = await client.post(
        f"/spaces/{space.id}/members",
        data={"csrf": csrf, "account_id": str(other.id), "permission": "read"},
    )
    assert resp.status_code == 403


async def test_set_conventions_home_records_the_id(owner_client, session, unseeded_deployment):
    """B.13 A-1/A-3: routing-id repair on the admin space surface — the owner records
    a space as the conventions home; identity is the recorded id."""
    import uuid as _uuid

    from assistant_memory.install import state as install_state
    from assistant_memory.models.install import SETTING_CONVENTIONS_SPACE_ID

    client, _owner = owner_client
    page = await client.get("/spaces")
    await client.post(
        "/spaces", data={"csrf": _csrf_from(page.text), "name": "conv-home"}
    )
    space = await session.scalar(select(Space).where(Space.name == "conv-home"))

    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{space.id}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    recorded = await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    assert recorded == str(space.id)

    # a dangling id is refused, nothing recorded over the good value
    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{_uuid.uuid4()}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert (
        await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    ) == str(space.id)


async def test_repair_refuses_a_space_without_the_projection(
    owner_client, session, unseeded_deployment
):
    """set-conventions-home is joined to the projection (review f46a31d2, finding
    routing-home-not-joined-to-conventions-projection): while a current projection
    exists somewhere, recording an empty space refuses and names the holder; with no
    projection anywhere the recording is the legal pre-seed repair."""
    from assistant_memory.auth.service import get_or_create_default_account
    from assistant_memory.conventions import DOC_LABEL
    from assistant_memory.install import state as install_state
    from assistant_memory.models.install import SETTING_CONVENTIONS_SPACE_ID
    from assistant_memory.repository import graph as repo

    client, owner = owner_client
    account = await get_or_create_default_account(session, owner)

    page = await client.get("/spaces")
    for name in ("holder", "empty"):
        await client.post("/spaces", data={"csrf": _csrf_from(page.text), "name": name})
    holder = await session.scalar(select(Space).where(Space.name == "holder"))
    empty = await session.scalar(select(Space).where(Space.name == "empty"))

    # No projection anywhere -> recording the empty space is allowed (pre-seed).
    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{empty.id}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)}, follow_redirects=False,
    )
    assert resp.status_code == 303

    # A projection exists in `holder` but is NOT yet FTS-indexed: it does not
    # qualify (the shared predicate demands the index), so it neither blocks the
    # repair nor gets named as a holder (finding
    # routing-home-projection-qualification-not-shared).
    doc = await repo.create_node(
        session, type="Document", space_id=holder.id, account_id=account.id,
        label=DOC_LABEL, properties={"text": "the conventions"},
    )
    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{empty.id}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)}, follow_redirects=False,
    )
    assert resp.status_code == 303  # unindexed projection = still the pre-seed flow

    # Indexed -> QUALIFYING: now the empty space refuses, naming the holder.
    from assistant_memory.search import index_fts

    await index_fts(session, doc)
    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{empty.id}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)}, follow_redirects=False,
    )
    assert resp.status_code == 403
    assert str(holder.id) in resp.json()["detail"]
    # ...and recording the holder itself passes.
    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{holder.id}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert (
        await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    ) == str(holder.id)


async def test_repair_observes_a_regression_before_writing(
    owner_client, session, unseeded_deployment, monkeypatch
):
    """A-5 at the repair seam (review f46a31d2, finding
    install-surface-bypasses-regression-observation): a lost fact is durably
    observed — the generation advances — BEFORE the repair can restore it."""
    import uuid as _uuid

    from sqlalchemy import delete as _delete

    from assistant_memory.auth.service import ensure_owner, get_or_create_default_account
    from assistant_memory.config import settings as _settings
    from assistant_memory.conventions import DOC_LABEL
    from assistant_memory.install import state as install_state
    from assistant_memory.models.install import SETTING_CONVENTIONS_SPACE_ID
    from assistant_memory.repository import graph as repo
    from assistant_memory.search import index_fts

    monkeypatch.setattr(_settings, "base_url", "http://localhost:8000")
    monkeypatch.setattr(_settings, "google_client_id", "cid.apps.example")
    monkeypatch.setattr(_settings, "google_client_secret", "cs-value")
    monkeypatch.setattr(_settings, "owner_email", "owner@example.com")
    monkeypatch.setattr(_settings, "session_secret", "a-real-session-secret")
    monkeypatch.setenv("AM_GIT_COMMIT", "a" * 40)
    monkeypatch.delenv("AM_GIT_TAG", raising=False)

    client, owner = owner_client
    # The seed predicates key on the OLDEST owner account: clear the flag on any
    # committed leftover owner first (same discipline as test_bootstrap's
    # readiness test), so this test's own owner decides the answer.
    from assistant_memory.models.identity import User as _User

    for other in (
        await session.scalars(select(_User).where(_User.is_owner.is_(True)))
    ).all():
        if other.id != owner.id:
            other.is_owner = False
    owner.is_owner = True
    if owner.google_sub is None:
        owner.google_sub = "sub-x"
    await session.flush()
    account = await get_or_create_default_account(session, owner)
    default = Space(
        name=install_state.DEFAULT_SPACE_NAME, template="personal", created_by=account.id
    )
    home = Space(name="obs-home", template="personal", created_by=account.id)
    session.add_all([default, home])
    await session.flush()
    session.add_all([
        Membership(account_id=account.id, space_id=default.id, permission="admin"),
        Membership(account_id=account.id, space_id=home.id, permission="admin"),
    ])
    await session.flush()
    doc = await repo.create_node(
        session, type="Document", space_id=home.id, account_id=account.id,
        label=DOC_LABEL, properties={"text": "the conventions"},
    )
    await index_fts(session, doc)
    await install_state.set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(home.id))
    await install_state.reconcile_release(session)
    await install_state.write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert (await install_state.compute_state(session)).status == "installed"
    gen_before = await install_state.get_generation(session)

    # Lose a structural fact: the owner's membership on the recorded home.
    await session.execute(
        _delete(Membership).where(
            Membership.space_id == home.id, Membership.account_id == account.id
        )
    )
    # The repair request itself must observe the loss durably, whatever it answers.
    page = await client.get("/spaces")
    await client.post(
        f"/spaces/{_uuid.uuid4()}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)}, follow_redirects=False,
    )
    assert (await install_state.get_generation(session)) == gen_before + 1


async def test_space_creation_observes_a_regression_before_restoring(
    owner_client, session, unseeded_deployment, monkeypatch
):
    """A-5 at the second restoring seam (review f46a31d2, reopened finding
    install-surface-state-restoration-still-bypasses-observation): POST /spaces can
    recreate the default space, so the loss must be durably observed — the
    generation advances — before that restoring write lands."""
    from sqlalchemy import delete as _delete, update as _update

    from assistant_memory.auth.service import get_or_create_default_account
    from assistant_memory.config import settings as _settings
    from assistant_memory.conventions import DOC_LABEL
    from assistant_memory.install import state as install_state
    from assistant_memory.models.identity import User as _User
    from assistant_memory.models.install import SETTING_CONVENTIONS_SPACE_ID
    from assistant_memory.repository import graph as repo
    from assistant_memory.search import index_fts

    monkeypatch.setattr(_settings, "base_url", "http://localhost:8000")
    monkeypatch.setattr(_settings, "google_client_id", "cid.apps.example")
    monkeypatch.setattr(_settings, "google_client_secret", "cs-value")
    monkeypatch.setattr(_settings, "owner_email", "owner@example.com")
    monkeypatch.setattr(_settings, "session_secret", "a-real-session-secret")
    monkeypatch.setenv("AM_GIT_COMMIT", "a" * 40)
    monkeypatch.delenv("AM_GIT_TAG", raising=False)

    client, owner = owner_client
    for other in (
        await session.scalars(select(_User).where(_User.is_owner.is_(True)))
    ).all():
        if other.id != owner.id:
            other.is_owner = False
    owner.is_owner = True
    if owner.google_sub is None:
        owner.google_sub = "sub-x"
    await session.flush()
    account = await get_or_create_default_account(session, owner)
    default = Space(
        name=install_state.DEFAULT_SPACE_NAME, template="personal", created_by=account.id
    )
    home = Space(name="obs-home-2", template="personal", created_by=account.id)
    session.add_all([default, home])
    await session.flush()
    session.add_all([
        Membership(account_id=account.id, space_id=default.id, permission="admin"),
        Membership(account_id=account.id, space_id=home.id, permission="admin"),
    ])
    await session.flush()
    doc = await repo.create_node(
        session, type="Document", space_id=home.id, account_id=account.id,
        label=DOC_LABEL, properties={"text": "the conventions"},
    )
    await index_fts(session, doc)
    await install_state.set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(home.id))
    await install_state.reconcile_release(session)
    await install_state.write_completion_record(
        session, account_id=account.id, observation={"operation": "conventions"}
    )
    assert (await install_state.compute_state(session)).status == "installed"
    gen_before = await install_state.get_generation(session)

    # Lose the default-space fact entirely (rename sweeps committed twins too).
    await session.execute(
        _update(Space)
        .where(Space.name == install_state.DEFAULT_SPACE_NAME)
        .values(name="was-the-default")
    )
    # The restoring write (space creation named 'personal') must observe first.
    page = await client.get("/spaces")
    await client.post(
        "/spaces",
        data={"csrf": _csrf_from(page.text), "name": install_state.DEFAULT_SPACE_NAME},
    )
    assert (await install_state.get_generation(session)) == gen_before + 1


async def test_exempt_admin_surface_is_owner_only_while_not_installed(session):
    """A-3: the exempt admin-space surface admits only the owner while installation
    is incomplete (review f46a31d2, finding install-space-surface-not-owner-scoped)."""
    import pytest as _pytest
    from fastapi import HTTPException

    from assistant_memory.install import gate as install_gate
    from assistant_memory.models.identity import User
    from assistant_memory.web.routes import _owner_only_while_not_installed

    owner = User(label="o", is_owner=True)
    stranger = User(label="s", is_owner=False)

    async def _closed(_s):
        return {"error": "not_installed", "stage": "seed", "closes_with": "..."}

    async def _open(_s):
        return None

    orig = install_gate.completion_check
    try:
        # The guard keys on the COMPLETION boundary (stage 5 included): a closed
        # completion check refuses a non-owner even when admission (stages 1-4)
        # would already be open — the VALIDATING window stays owner-only (finding
        # install-space-owner-guard-opens-before-install-completes).
        install_gate.completion_check = _closed
        await _owner_only_while_not_installed(session, owner)  # owner always passes
        with _pytest.raises(HTTPException) as exc:
            await _owner_only_while_not_installed(session, stranger)
        assert exc.value.status_code == 403
        install_gate.completion_check = _open
        await _owner_only_while_not_installed(session, stranger)  # installed: ordinary UI
    finally:
        install_gate.completion_check = orig


async def test_spaces_page_renders_with_a_malformed_recorded_id(
    owner_client, session, unseeded_deployment
):
    """The /spaces page is the NAMED repair surface for a malformed recorded
    routing id — it must render in exactly that state, show the broken value, and
    keep set-conventions-home usable (review f46a31d2, finding
    malformed-routing-id-breaks-repair-ui)."""
    from assistant_memory.install import state as install_state
    from assistant_memory.models.install import SETTING_CONVENTIONS_SPACE_ID

    client, _owner = owner_client
    await install_state.set_setting(
        session, SETTING_CONVENTIONS_SPACE_ID, "not-a-uuid-at-all"
    )
    await session.commit()

    page = await client.get("/spaces")
    assert page.status_code == 200
    assert "not-a-uuid-at-all" in page.text
    assert "malformed" in page.text

    # The repair itself still works from this state.
    await client.post(
        "/spaces", data={"csrf": _csrf_from(page.text), "name": "repair-home"}
    )
    space = await session.scalar(select(Space).where(Space.name == "repair-home"))
    page = await client.get("/spaces")
    resp = await client.post(
        f"/spaces/{space.id}/set-conventions-home",
        data={"csrf": _csrf_from(page.text)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert (
        await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    ) == str(space.id)
