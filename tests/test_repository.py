# SPDX-License-Identifier: Apache-2.0
import pytest
from sqlalchemy import func, select

from assistant_memory.models.graph import NodeSpace, NodeVersion
from assistant_memory.repository import ConflictError, ContainerConflictError, graph


async def _count(session, model, **filters) -> int:
    stmt = select(func.count()).select_from(model)
    for col, val in filters.items():
        stmt = stmt.where(getattr(model, col) == val)
    return await session.scalar(stmt)


async def test_create_node_makes_version_and_ref(session, account, space):
    node = await graph.create_node(
        session,
        type="Note",
        space_id=space.id,
        account_id=account.id,
        label="hello",
        properties={"text": "hi"},
    )
    assert node.current_version_id is not None
    assert await _count(session, NodeVersion, node_id=node.id) == 1
    assert await _count(session, NodeSpace, node_id=node.id, space_id=space.id) == 1


async def test_update_node_cas(session, account, space):
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    v1 = node.current_version_id

    node = await graph.update_node(
        session,
        node_id=node.id,
        account_id=account.id,
        expected_version=v1,
        properties_patch={"a": 1},
    )
    v2 = node.current_version_id
    assert v2 != v1
    assert node.properties == {"a": 1}
    assert await _count(session, NodeVersion, node_id=node.id) == 2

    with pytest.raises(ConflictError):
        await graph.update_node(
            session,
            node_id=node.id,
            account_id=account.id,
            expected_version=v1,  # stale
            properties_patch={"b": 2},
        )


async def test_status_defaults_current(session, account, space):
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    assert node.status == "current"
    version = await session.get(NodeVersion, node.current_version_id)
    assert version.status == "current"


async def test_create_node_with_status(session, account, space):
    node = await graph.create_node(
        session,
        type="Hypothesis",
        space_id=space.id,
        account_id=account.id,
        status="provisional",
    )
    assert node.status == "provisional"
    version = await session.get(NodeVersion, node.current_version_id)
    assert version.status == "provisional"


async def test_update_status_versions_and_carries_over(session, account, space):
    node = await graph.create_node(
        session, type="Note", space_id=space.id, account_id=account.id, status="provisional"
    )
    v1 = node.current_version_id

    # Promote status only; the new version records it, the node projects it.
    node = await graph.update_node(
        session,
        node_id=node.id,
        account_id=account.id,
        expected_version=v1,
        status="current",
    )
    assert node.status == "current"
    v2 = await session.get(NodeVersion, node.current_version_id)
    assert v2.status == "current"
    assert await _count(session, NodeVersion, node_id=node.id) == 2

    # A later edit that omits status leaves it unchanged (carries over).
    node = await graph.update_node(
        session,
        node_id=node.id,
        account_id=account.id,
        expected_version=node.current_version_id,
        properties_patch={"a": 1},
    )
    assert node.status == "current"


async def test_delete_node_soft(session, account, space):
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    await graph.delete_node(session, node_id=node.id, expected_version=node.current_version_id)
    assert await graph.get_node(session, node.id) is None


async def test_link_is_idempotent(session, account, space):
    a = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    b = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    e1 = await graph.link(
        session, type="relates_to", src_node=a.id, dst_node=b.id, account_id=account.id
    )
    e2 = await graph.link(
        session, type="relates_to", src_node=a.id, dst_node=b.id, account_id=account.id
    )
    assert e1.id == e2.id


async def test_unlink_is_idempotent(session, account, space):
    a = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    b = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    e = await graph.link(
        session, type="relates_to", src_node=a.id, dst_node=b.id, account_id=account.id
    )
    assert await graph.unlink(session, edge_id=e.id) is True
    assert await graph.unlink(session, edge_id=e.id) is False


async def test_containment_single_parent(session, account, space):
    child = await graph.create_node(
        session, type="Decision", space_id=space.id, account_id=account.id
    )
    p1 = await graph.create_node(session, type="Project", space_id=space.id, account_id=account.id)
    p2 = await graph.create_node(session, type="Project", space_id=space.id, account_id=account.id)
    await graph.link(
        session, type="contained_in", src_node=child.id, dst_node=p1.id, account_id=account.id
    )
    with pytest.raises(ContainerConflictError):
        await graph.link(
            session, type="contained_in", src_node=child.id, dst_node=p2.id, account_id=account.id
        )


async def test_share_node_is_idempotent(session, account, space, space2):
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    assert (
        await graph.share_node(session, node_id=node.id, space_id=space2.id, account_id=account.id)
        is True
    )
    assert (
        await graph.share_node(session, node_id=node.id, space_id=space2.id, account_id=account.id)
        is False
    )
    assert await graph.unshare_node(session, node_id=node.id, space_id=space2.id) is True
    assert await graph.unshare_node(session, node_id=node.id, space_id=space2.id) is False


async def test_edge_sensitivity_derived_from_endpoint(session, account, space):
    note = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    proj = await graph.create_node(
        session, type="Project", space_id=space.id, account_id=account.id
    )
    # Project is high-sensitivity -> the edge is sensitive even though Note is normal.
    edge = await graph.link(
        session, type="relates_to", src_node=note.id, dst_node=proj.id, account_id=account.id
    )
    assert edge.sensitive is True
