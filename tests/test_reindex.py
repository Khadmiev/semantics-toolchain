# SPDX-License-Identifier: Apache-2.0
from sqlalchemy import select

from assistant_memory.models.graph import EMBEDDING_DIM, NodeVersion
from assistant_memory.models.identity import Membership
from assistant_memory.repository import graph as repo
from assistant_memory.search.embedder import DeterministicEmbedder
from assistant_memory.search.reindex import reindex_all


async def test_reindex_all_embeds_current_versions(session, account, space):
    session.add(Membership(account_id=account.id, space_id=space.id, permission="write"))
    await session.flush()
    node = await repo.create_node(
        session, type="Note", space_id=space.id, account_id=account.id, label="reindex me"
    )
    # clear the embedding to prove reindex sets it
    await session.execute(
        NodeVersion.__table__.update()
        .where(NodeVersion.id == node.current_version_id)
        .values(embedding=None)
    )

    count = await reindex_all(session, DeterministicEmbedder(EMBEDDING_DIM))
    assert count >= 1

    embedding = await session.scalar(
        select(NodeVersion.embedding).where(NodeVersion.id == node.current_version_id)
    )
    assert embedding is not None
    assert len(embedding) == EMBEDDING_DIM
