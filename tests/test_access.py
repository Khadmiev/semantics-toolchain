# SPDX-License-Identifier: Apache-2.0
from assistant_memory.access import Access, containment_subtree
from assistant_memory.models.identity import Account, Membership, User
from assistant_memory.repository import graph


async def _grant(session, account, space, permission="admin"):
    session.add(Membership(account_id=account.id, space_id=space.id, permission=permission))
    await session.flush()


async def _account(session, label="other"):
    user = User(label=label)
    session.add(user)
    await session.flush()
    acc = Account(user_id=user.id, label=label)
    session.add(acc)
    await session.flush()
    return acc


async def test_visibility_isolation(session, account, space):
    await _grant(session, account, space)
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)

    assert await Access(session, account.id).is_node_visible(node.id) is True

    other = await _account(session)
    assert await Access(session, other.id).is_node_visible(node.id) is False


async def test_effective_permission_is_max(session, account, space, space2):
    await _grant(session, account, space, "read")
    await _grant(session, account, space2, "write")
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    await graph.share_node(session, node_id=node.id, space_id=space2.id, account_id=account.id)

    assert await Access(session, account.id).effective_permission(node.id) == "write"


async def test_effective_permission_none_when_invisible(session, account, space):
    node = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    # account has no membership in `space` -> not visible
    assert await Access(session, account.id).effective_permission(node.id) is None


async def test_induced_edge_visible(session, account, space):
    await _grant(session, account, space)
    a = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    b = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    await graph.link(
        session, type="relates_to", src_node=a.id, dst_node=b.id, account_id=account.id
    )

    neighbors = await Access(session, account.id).neighbors(a.id)
    assert b.id in {n.id for _, n in neighbors}


async def test_edge_to_invisible_node_hidden(session, account, space, space2):
    await _grant(session, account, space)  # member of space only, not space2
    a = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    b = await graph.create_node(session, type="Note", space_id=space2.id, account_id=account.id)
    await graph.link(
        session, type="relates_to", src_node=a.id, dst_node=b.id, account_id=account.id
    )

    neighbors = await Access(session, account.id).neighbors(a.id)
    assert b.id not in {n.id for _, n in neighbors}


async def test_sensitive_edge_gated_by_edge_spaces(session, account, space):
    await _grant(session, account, space)
    note = await graph.create_node(session, type="Note", space_id=space.id, account_id=account.id)
    proj = await graph.create_node(
        session, type="Project", space_id=space.id, account_id=account.id
    )
    # Project is high -> the edge is sensitive; both endpoints visible.
    edge = await graph.link(
        session, type="relates_to", src_node=note.id, dst_node=proj.id, account_id=account.id
    )
    access = Access(session, account.id)

    assert proj.id not in {n.id for _, n in await access.neighbors(note.id)}

    await graph.share_edge(session, edge_id=edge.id, space_id=space.id, account_id=account.id)
    assert proj.id in {n.id for _, n in await access.neighbors(note.id)}


async def test_containment_subtree(session, account, space):
    proj = await graph.create_node(
        session, type="Project", space_id=space.id, account_id=account.id
    )
    d1 = await graph.create_node(session, type="Decision", space_id=space.id, account_id=account.id)
    d2 = await graph.create_node(session, type="Decision", space_id=space.id, account_id=account.id)
    chunk = await graph.create_node(
        session, type="DocumentChunk", space_id=space.id, account_id=account.id
    )
    for child, parent in [(d1, proj), (d2, proj), (chunk, d1)]:
        await graph.link(
            session,
            type="contained_in",
            src_node=child.id,
            dst_node=parent.id,
            account_id=account.id,
        )

    assert await containment_subtree(session, proj.id) == {proj.id, d1.id, d2.id, chunk.id}
    assert await containment_subtree(session, d1.id) == {d1.id, chunk.id}
