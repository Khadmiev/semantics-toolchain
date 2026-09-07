# SPDX-License-Identifier: Apache-2.0
from sqlalchemy import select

from assistant_memory.bootstrap import (
    _ensure_personal_space,
    conventions_exist_anywhere,
    conventions_visible_to_owner,
)
from assistant_memory.conventions import DOC_LABEL, SECTIONS, seed_conventions
from assistant_memory.models.graph import Node, NodeSpace, NodeVersion
from assistant_memory.models.identity import Membership, User
from assistant_memory.search import get_embedder


async def _doc_version(session, space_id) -> NodeVersion:
    node = await session.scalar(
        select(Node)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .where(NodeSpace.space_id == space_id, Node.type == "Document", Node.label == DOC_LABEL)
    )
    return await session.get(NodeVersion, node.current_version_id)


async def test_seed_conventions_fts_only(session, account, space, unseeded_deployment):
    """No model: content is created + full-text indexed, but embeddings stay NULL."""
    counts = await seed_conventions(
        session, space_id=space.id, account_id=account.id, embedder=None
    )
    assert counts["created"] == len(SECTIONS) + 2  # Document + Tag + section notes
    version = await _doc_version(session, space.id)
    assert version.search_tsv is not None  # FTS populated without a model
    assert version.embedding is None       # embedding deferred to backfill


async def test_seed_conventions_idempotent(session, account, space, unseeded_deployment):
    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    counts = await seed_conventions(
        session, space_id=space.id, account_id=account.id, embedder=None
    )
    assert counts["created"] == 0
    assert counts["updated"] == 0


async def test_seed_conventions_with_embedder_fills_embedding(
    session, account, space, unseeded_deployment
):
    await seed_conventions(
        session, space_id=space.id, account_id=account.id, embedder=get_embedder()
    )
    version = await _doc_version(session, space.id)
    assert version.embedding is not None
    assert version.search_tsv is not None


async def test_ensure_personal_space_creates_admin_membership(session, account):
    space = await _ensure_personal_space(session, account)
    assert space.name == "personal"
    membership = await session.scalar(
        select(Membership).where(
            Membership.account_id == account.id, Membership.space_id == space.id
        )
    )
    assert membership is not None
    assert membership.permission == "admin"


# --- B.11 D-3: the two DOC_LABEL lookups ask DIFFERENT questions -----------------------
#
# They sit a few lines apart in one file and their queries were, until this slice, byte
# identical — which is how one edit meant for the second landed on the first, breaking the
# guard AND leaving the element unimplemented, with both docstrings claiming otherwise
# (critic finding `bootstrap-current-only-seed-guard`). Neither behaviour had a test; that
# absence is what let the swap survive a green suite. These two are that test.


async def test_a_retired_projection_still_counts_as_seeded(session, account, space, unseeded_deployment):
    """The existence guard is status-AGNOSTIC on purpose, as its docstring says.

    A retired projection still means this deployment has been seeded once. Reading it as
    "not seeded" makes bootstrap seed a fresh copy alongside it — the duplication the guard
    exists to prevent — or, when the projection lives in another space, raise outright.
    """
    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    assert await conventions_exist_anywhere(session) is True

    doc = await session.scalar(
        select(Node).where(Node.type == "Document", Node.label == DOC_LABEL)
    )
    doc.status = "superseded"
    await session.flush()

    assert await conventions_exist_anywhere(session) is True, (
        "a retired projection is still a projection: the guard must not offer to reseed"
    )


async def test_readiness_needs_a_LIVE_projection(session, account, space, unseeded_deployment):
    """Readiness is the stricter question: is there something live to SERVE.

    A deployment holding only a superseded copy used to report ready and then answer
    requests with no live projection.
    """
    # Readiness asks whether the OWNER can see a live projection, so this test makes its
    # own account the owner and a member of the space it seeds. It also clears the flag on
    # anyone else first: the predicate takes the OLDEST owner account, so a leftover owner
    # from another run would silently decide the answer — a test green (or red) by luck,
    # which is exactly what this suite's own database rule exists to stop.
    for other in (await session.scalars(select(User).where(User.is_owner.is_(True)))).all():
        other.is_owner = False
    user = await session.get(User, account.user_id)
    user.is_owner = True
    session.add(Membership(space_id=space.id, account_id=account.id, permission="admin"))
    await session.flush()

    await seed_conventions(session, space_id=space.id, account_id=account.id, embedder=None)
    assert await conventions_visible_to_owner(session) is True

    doc = await session.scalar(
        select(Node).where(Node.type == "Document", Node.label == DOC_LABEL)
    )
    doc.status = "superseded"
    await session.flush()

    assert await conventions_visible_to_owner(session) is False, (
        "with only a retired copy there is nothing live to serve — readiness must say so"
    )
