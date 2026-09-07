# SPDX-License-Identifier: Apache-2.0
"""A development cycle in the graph, for tests that need one (B.12 Part A).

A `DevelopmentCycle` anchor with its documents hanging off it is what the reconciliation
reads instead of a declaration on the channel (A-7), so any test of the reconciliation
needs one. Built here once rather than in each test file: two hand-rolled copies of "what
a cycle looks like in the graph" would drift, and the write contract of A-3/A-8/A-12 is
exactly the thing the drift would silently rewrite.

The node TYPE is registered here too. There is no migration for it, deliberately and in
line with this project's ontology: the type registry is soft («new type = a row, no
migration»), the deployment's row was created through the ordinary ontology path, and the
slice's one migration is spent on the projection's message kind — which genuinely cannot
be posted without it.
"""

from sqlalchemy.dialects.postgresql import insert as pg_insert

from assistant_memory.models.graph import NodeType
from assistant_memory.models.identity import Account, Space, User
from assistant_memory.repository import graph as repo
from assistant_memory.review.cycle import CYCLE_NODE_TYPE

#: A-12's four labels, as tests use them.
PRE_REVIEW = "intent_pre_review"
POST_REVIEW = "intent_post_review"
SPEC = "spec"
TRANSCRIPT = "free_phase_transcript"


async def ensure_cycle_type(session) -> None:
    """The soft type row for `DevelopmentCycle` — idempotent."""
    await session.execute(
        pg_insert(NodeType.__table__)
        .values(
            type=CYCLE_NODE_TYPE,
            default_sensitivity="normal",
            description=(
                "One slice of development work, from the operator's first free-form "
                "sentence to acceptance: the parent of that slice's documents and the "
                "target of its reviews' decisions (B.12 A-1)."
            ),
        )
        .on_conflict_do_nothing(index_elements=["type"])
    )


async def make_cycle_review(session, anchor_id, *, slug="cycle-review"):
    """A review that NAMES this anchor — the thing an intent's affinity has to resolve to.

    Round 2 of B.12's own implementation review closed the hole where any truthy affinity was
    accepted, so a cycle in a test is now only realistic if at least one real review belongs
    to it. Returns the review id as a string, ready to be used as `review_affinity`.
    """
    from tests.instrument_helpers import seed_instruments

    from assistant_memory.review.repository import create_review

    issued = await create_review(
        session, slug=slug, mode="code",
        instrument=await seed_instruments(session),
        config={"coverage": {"in_play": False}, "cycle_anchor": str(anchor_id)},
    )
    return str(issued.review.id)


async def make_cycle(session, account, space, *, documents=(), label="Цикл TEST", **props):
    """Create an anchor with its documents, and return ``(anchor, [document nodes])``.

    ``documents`` follows the write contract of A-3/A-8/A-12 as amended 2026-08-27, and
    the identity field depends on the KIND: ``(kind, label, text, review_affinity)`` for
    the post-review intent — the one kind that belongs to a review — and
    ``(kind, label, text, None, translates)`` for a pre-review intent, whose fifth slot
    carries the subject it translates (``spec`` | ``implementation``) while its affinity
    is empty by construction; the spec and the transcript pass ``None`` affinity and no
    fifth slot. Every field is persisted by the same act that writes the document and
    never inferred afterwards.

    A callable may be passed as the affinity: it is invoked with the anchor's id once the
    anchor exists, which is how a test gets a REAL review of this cycle into the field
    without knowing the anchor id before the call.
    """
    await ensure_cycle_type(session)
    anchor = await repo.create_node(
        session,
        type=CYCLE_NODE_TYPE,
        space_id=space.id,
        account_id=account.id,
        label=label,
        properties={"cycle": "TEST", **props},
    )
    made = []
    minted: dict = {}
    for entry in documents:
        # (kind, label, text, affinity) or (kind, label, text, affinity, translates) —
        # the fifth slot carries the pre-review intent's SUBJECT, which replaced its review
        # affinity on the operator's ruling of 2026-08-27.
        kind, doc_label, text, affinity = entry[:4]
        subject = entry[4] if len(entry) > 4 else None
        if callable(affinity):
            # Memoized per callable: the documents that DO name a review (the post-review
            # intent; a legacy fixture may pass others) should share the one review the
            # callable mints — calling it per document would mint several and quietly
            # scatter the affinities across them.
            if affinity not in minted:
                minted[affinity] = await affinity(session, anchor.id)
            affinity = minted[affinity]
        node = await repo.create_node(
            session,
            type="Document",
            space_id=space.id,
            account_id=account.id,
            label=doc_label,
            properties={
                "kind": kind,
                "review_affinity": affinity,
                "translates": subject,
                "text": text,
                "chars": len(text),
                "cycle": "TEST",
            },
        )
        await repo.link(
            session,
            type="contained_in",
            src_node=node.id,
            dst_node=anchor.id,
            account_id=account.id,
        )
        made.append(node)
    await session.flush()
    return anchor, made


async def make_standalone_cycle(session, *, documents=(), label="Цикл TEST", **props):
    """A cycle plus the account and space it lives in — for tests with no such fixtures.

    Most review tests care about the channel and never touch the graph, so they take no
    account/space fixture. They still need an anchor now, because a `produced`
    reconciliation names one. This makes the whole context in one call and returns the
    anchor's id as a string, ready for a review's ``cycle_anchor`` config key.
    """
    user = User(label="cycle-tester")
    session.add(user)
    await session.flush()
    account = Account(user_id=user.id, label="personal")
    session.add(account)
    await session.flush()
    space = Space(name="cycle-space", template="personal", created_by=account.id)
    session.add(space)
    await session.flush()
    anchor, docs = await make_cycle(
        session, account, space, documents=documents, label=label, **props
    )
    return str(anchor.id), docs


async def own_review(session, anchor_id):
    """Affinity callable: a real review of THIS cycle. Use as the affinity of an intent."""
    return await make_cycle_review(session, anchor_id)


def default_documents(review_id: str = None):
    """The ordinary shape of a cycle by the time its implementation review runs.

    With no id given the intents get a REAL review of the cycle, minted when the anchor
    exists — the shape the affinity check now demands.
    """
    review_id = own_review if review_id is None else review_id
    return [
        (TRANSCRIPT, "фаза 1 — свободное описание", "оператор: хочу вот это", None),
        (TRANSCRIPT, "фаза 2 — обсуждение", "оператор: давай так. разработка: хорошо", None),
        (SPEC, "спека среза", "нормативный текст спеки", None),
        # The pre-review intent belongs to the CYCLE and names the subject it translates;
        # only the post-review intent belongs to a review.
        (PRE_REVIEW, "интент до ревью", "что будет построено", None, "implementation"),
        (POST_REVIEW, "интент после ревью", "что изменилось", review_id),
    ]
