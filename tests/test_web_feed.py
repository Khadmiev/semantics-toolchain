# SPDX-License-Identifier: Apache-2.0
import re

from sqlalchemy import func, select

from assistant_memory.models.graph import NodeVersion
from assistant_memory.models.identity import Account, Membership, Space
from assistant_memory.repository import graph as repo


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no CSRF token in page"
    return match.group(1)


async def _space_with(session, owner, permission="write"):
    acc = await session.scalar(select(Account).where(Account.user_id == owner.id))
    sp = Space(name="feedspace", template="personal", created_by=acc.id)
    session.add(sp)
    await session.flush()
    session.add(Membership(account_id=acc.id, space_id=sp.id, permission=permission))
    await session.flush()
    return acc, sp


async def test_feed_lists_changes(owner_client, session):
    client, owner = owner_client
    acc, sp = await _space_with(session, owner)
    await repo.create_node(session, type="Note", space_id=sp.id, account_id=acc.id, label="hello")

    page = await client.get("/feed")
    assert page.status_code == 200
    assert "hello" in page.text


async def test_undo_reverts_to_previous_version(owner_client, session):
    client, owner = owner_client
    acc, sp = await _space_with(session, owner)
    node = await repo.create_node(
        session, type="Note", space_id=sp.id, account_id=acc.id, label="v1"
    )
    await repo.update_node(
        session, node_id=node.id, account_id=acc.id,
        expected_version=node.current_version_id, label="v2",
    )

    csrf = _csrf_from((await client.get("/feed")).text)
    resp = await client.post(f"/feed/undo/{node.id}", data={"csrf": csrf}, follow_redirects=False)
    assert resp.status_code == 303

    await session.refresh(node)
    assert node.label == "v1"
    count = await session.scalar(
        select(func.count()).select_from(NodeVersion).where(NodeVersion.node_id == node.id)
    )
    assert count == 3  # v1, v2, and the restoring version


async def test_undo_of_create_soft_deletes(owner_client, session):
    client, owner = owner_client
    acc, sp = await _space_with(session, owner)
    node = await repo.create_node(
        session, type="Note", space_id=sp.id, account_id=acc.id, label="lonely"
    )

    csrf = _csrf_from((await client.get("/feed")).text)
    await client.post(f"/feed/undo/{node.id}", data={"csrf": csrf})
    await session.refresh(node)
    assert node.deleted_at is not None


async def test_undo_of_delete_undeletes(owner_client, session):
    client, owner = owner_client
    acc, sp = await _space_with(session, owner)
    node = await repo.create_node(
        session, type="Note", space_id=sp.id, account_id=acc.id, label="gone"
    )
    await repo.delete_node(session, node_id=node.id, expected_version=node.current_version_id)

    csrf = _csrf_from((await client.get("/feed")).text)
    await client.post(f"/feed/undo/{node.id}", data={"csrf": csrf})
    await session.refresh(node)
    assert node.deleted_at is None


async def test_undo_requires_write_permission(owner_client, session):
    client, owner = owner_client
    acc, sp = await _space_with(session, owner, permission="read")
    node = await repo.create_node(
        session, type="Note", space_id=sp.id, account_id=acc.id, label="readonly"
    )

    csrf = _csrf_from((await client.get("/feed")).text)
    resp = await client.post(f"/feed/undo/{node.id}", data={"csrf": csrf}, follow_redirects=False)
    assert resp.status_code == 403
    await session.refresh(node)
    assert node.deleted_at is None  # unchanged
