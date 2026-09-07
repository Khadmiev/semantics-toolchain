# SPDX-License-Identifier: Apache-2.0
"""The development cycle's documents, RESOLVED from its anchor (B.12 Part A).

A development cycle — from the operator's first free-form sentence to acceptance — is one
`DevelopmentCycle` node (A-1). Everything the cycle produced hangs off it: both of its
reviews, all four of its Intent Summaries, its spec, and the literal transcript of its two
free-form phases. This module is the READ side of that arrangement, and it exists because
of one element:

**A-7 — the reconciliation reads its document set from the anchor.** B.11 had development
DECLARE the set on the channel and validated citations against the declaration. That was
unverifiable by construction, and it was established rather than argued: three of the four
documents were files, the server has no repository, so the only one it could check was the
one in which a mistake is impossible. Resolution from the graph replaces it — the server
holds the documents, so the set is a fact it can read rather than a claim it must take.

**Deriving the set from filename patterns is forbidden and stays forbidden.** It is a
guaranteed error rather than a rigour: as of 2026-08-25 there were 43 intent files in
`docs/implementation`, the names are not paired (B.11's spec-cycle pre-review intent is
`..._2026-08-23_b11-after-refusal-seam-and-map.md` while its post-review intent is
`..._2026-08-24_b11-after-refusal-seam.md`), and several pre-review intents have no
post-review twin at all.

**Each document carries its own type label and — per kind — its review affinity or its
subject** (A-12), written by the act that wrote the document and never inferred afterwards
from the text, the node's label, or the file it came from. The pass needs them all: B-8
gives the transcript a standing narrower than an intent's — it may not ground a claim that
a promise was broken; the POST-review intent is named by the review it reports
(`review_affinity`); and a cycle's several PRE-review intents are told apart by the subject
each translates (`translates`: `spec` | `implementation`), because they belong to the cycle
and labels are not identity.

**A document whose label is absent or outside the vocabulary is NAMED as unreadable**
rather than silently skipped or guessed at. Skipping it would shrink the comparison set
without saying so, which is the failure this whole arrangement is built to make visible.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.graph import Edge, Node
from ..models.review import Review

#: A-1: the node type one cycle is recorded as. Named here rather than spelled inline at
#: each call site, because "which node is an anchor" is a fact of the ontology and a second
#: spelling of it is a second answer.
CYCLE_NODE_TYPE = "DevelopmentCycle"

#: A-12: the CLOSED vocabulary of type labels. Four values rather than eight, because the
#: number of reviews in a cycle is a property of the cycle and not of the vocabulary:
#: folding the review affinity into the label would break on the first cycle that has three.
CYCLE_DOCUMENT_KINDS = (
    "intent_pre_review",
    "intent_post_review",
    "spec",
    "free_phase_transcript",
)

#: B-8: the label the transcript's narrower standing is keyed on, AND ON NOTHING ELSE.
#: Named here rather than spelled at the one place that reads it, because "which document is
#: the transcript" is a fact of the write contract: a second spelling of it in a validator
#: is how the standing quietly stops applying.
TRANSCRIPT_KIND = "free_phase_transcript"

#: A-12: the labels whose affinity is EMPTY BY CONSTRUCTION — they belong to the cycle rather
#: than to either of its reviews, and that emptiness is written out explicitly.
#:
#: `intent_pre_review` JOINED THEM on the operator's ruling of 2026-08-27, and the ruling is
#: what dissolved finding `b12-review-affinity-before-review-id`. The rule used to require the
#: pre-review intent to be written at its finalization — before the review is created — while
#: carrying the id of that not-yet-existing review. Development met the impossibility twice in
#: one day, worked around it both times, and reported it in a `note` rather than raising it;
#: the critic found it by reading the text.
#:
#: The operator's answer removed the cause instead of the symptom: «интент до ревью создается,
#: но он относится к циклу разработки… А ревью уже проходит по той спеке или коду, переводом
#: которых на мой язык интент и является». A pre-review intent is the human translation of the
#: SUBJECT — the spec, or the implementation — and the subject exists before any review does.
#:
#: The asymmetry is real rather than a convenience: the POST-review intent genuinely does
#: belong to a review, because it reports what that review did, and it is finalized when that
#: review already exists. So it keeps its affinity and never had the problem.
CYCLE_DOCUMENT_KINDS_WITHOUT_AFFINITY = ("spec", "free_phase_transcript", "intent_pre_review")

#: What a pre-review intent carries INSTEAD of a review: the subject it translates. A cycle
#: has two of these documents (this one has three), so something must tell them apart — and
#: the label may not, because labels are not unique and are not machine identity (finding
#: `b12-document-label-reference-ambiguous`, same pass). The subject is known when the
#: operator closes the gate, which is exactly when the document is finalized, so it can be
#: written honestly and on time.
SUBJECT_PROPERTY = "translates"
INTENT_SUBJECTS = ("spec", "implementation")
KIND_REQUIRING_SUBJECT = "intent_pre_review"

#: Where the type label, the review affinity (post-review intent only) and the subject
#: (pre-review intent only, `SUBJECT_PROPERTY` above) live on the document node. All are
#: properties of the WRITE (A-3, A-8) — this module reads them and never derives them.
KIND_PROPERTY = "kind"
AFFINITY_PROPERTY = "review_affinity"


class CycleAnchorError(Exception):
    """The review names an anchor that is not one. Raised as a refusal, never defaulted.

    Defaulting would mean comparing against an empty set and reporting no discrepancies,
    which is indistinguishable from a clean comparison — the exact failure this slice
    removed from the declared-set arrangement.
    """


def _as_uuid(anchor_ref: Any) -> uuid.UUID:
    try:
        return anchor_ref if isinstance(anchor_ref, uuid.UUID) else uuid.UUID(str(anchor_ref))
    except (ValueError, AttributeError, TypeError):
        raise CycleAnchorError(
            f"{anchor_ref!r} is not a node id — a review names its development cycle by the "
            "anchor's id, which is what makes the document set a fact the server can read "
            "rather than a claim it has to take"
        ) from None


def _document_view(node: Node) -> dict:
    """One document of the cycle, as the pass and the report see it.

    ``readable`` is the honest half: a child of the anchor whose type label is missing or
    outside the vocabulary is carried through with the problem NAMED, so B-7's opening list
    can say it is unreadable. A pass cannot apply B-8's narrower standing to a document it
    cannot classify, and quietly dropping it would shrink the comparison without a trace.
    """
    props = node.properties or {}
    kind = props.get(KIND_PROPERTY)
    affinity = props.get(AFFINITY_PROPERTY)
    text = props.get("text")
    problem: str | None = None
    if kind is None:
        problem = (
            f"carries no `{KIND_PROPERTY}` — the pass cannot tell an intent from a "
            "transcript, and the two have different standing"
        )
    elif kind not in CYCLE_DOCUMENT_KINDS:
        problem = (
            f"`{KIND_PROPERTY}` is {kind!r}, which is outside the vocabulary "
            f"{list(CYCLE_DOCUMENT_KINDS)}"
        )
    elif not isinstance(text, str) or not text.strip():
        problem = "carries no text — the graph holds the substance, not a pointer to it"
    elif kind == KIND_REQUIRING_SUBJECT and props.get(SUBJECT_PROPERTY) not in INTENT_SUBJECTS:
        problem = (
            f"is an {kind!r} whose `{SUBJECT_PROPERTY}` is "
            f"{props.get(SUBJECT_PROPERTY)!r}, outside {list(INTENT_SUBJECTS)} — a cycle has "
            "more than one pre-review intent, and what tells them apart is the subject each "
            "one translates, not its label"
        )
    elif kind not in CYCLE_DOCUMENT_KINDS_WITHOUT_AFFINITY and not affinity:
        problem = (
            f"is an {kind!r} with no `{AFFINITY_PROPERTY}` — it reports what one review did, "
            "so the label alone does not say which one"
        )
    elif kind in CYCLE_DOCUMENT_KINDS_WITHOUT_AFFINITY and affinity:
        # A-12 said this emptiness is BY CONSTRUCTION and written out explicitly. Until
        # round 2 of this slice's own review nothing read it back, so "explicitly empty"
        # accepted any value — and an "explicitly empty" that accepts any value is not a
        # rule. A spec or a transcript belongs to the CYCLE; an id here does not narrow it,
        # it contradicts it, and the reconciliation report attributes by exactly this field.
        problem = (
            f"is a {kind!r} carrying `{AFFINITY_PROPERTY}` {affinity!r} — this kind belongs "
            "to the cycle rather than to either of its reviews, and its emptiness is part "
            "of the write contract rather than an omission"
        )
    return {
        "node_id": str(node.id),
        "label": node.label,
        "kind": kind,
        "review_affinity": str(affinity) if affinity else None,
        SUBJECT_PROPERTY: props.get(SUBJECT_PROPERTY),
        "repo_path": props.get("repo_path"),
        "chars": len(text) if isinstance(text, str) else 0,
        "text": text if isinstance(text, str) else "",
        "created_at": node.created_at.isoformat() if node.created_at else None,
        "readable": problem is None,
        "problem": problem,
    }


async def resolve_cycle_documents(session: AsyncSession, anchor_ref: Any) -> dict:
    """The cycle's documents, read from the anchor (A-7, A-12).

    Returns ``{"anchor": {...}, "documents": [...]}``. Documents are every live
    ``contained_in`` child of the anchor that is a ``Document``, in creation order —
    the order is the cycle's own history, and A-6 derives "which documents a decision
    stood on" from exactly these timestamps.

    Raises ``CycleAnchorError`` when the reference resolves to nothing, to a deleted node,
    or to a node that is not a cycle anchor. The refusal is deliberate: an unresolvable
    anchor that silently produced an empty set would let a reconciliation report "no
    discrepancies" over a comparison it never made.
    """
    anchor_id = _as_uuid(anchor_ref)
    anchor = await session.get(Node, anchor_id)
    if anchor is None or anchor.deleted_at is not None:
        raise CycleAnchorError(
            f"development cycle anchor {anchor_id} does not exist (or was deleted) — the "
            "cycle's documents are read from it, so there is nothing to compare against"
        )
    if anchor.type != CYCLE_NODE_TYPE:
        raise CycleAnchorError(
            f"node {anchor_id} is a {anchor.type!r}, not a {CYCLE_NODE_TYPE!r} — a review "
            "belongs to a development cycle, and any other node would make the document "
            "set mean something else"
        )
    rows = (
        await session.scalars(
            select(Node)
            .join(Edge, Edge.src_node == Node.id)
            .where(
                Edge.type == "contained_in",
                Edge.dst_node == anchor_id,
                Edge.valid_to.is_(None),
                Node.deleted_at.is_(None),
                Node.type == "Document",
            )
            .order_by(Node.created_at, Node.id)
        )
    ).all()
    # THE INTENT'S AFFINITY MUST NAME A REVIEW OF THIS CYCLE (finding
    # `b12-cycle-document-affinity-invariant-unchecked`, round 2 of this slice's own
    # implementation review). `_document_view` is pure and sees one node, so it can only
    # check that the field is there; whether the id belongs to THIS cycle needs the reviews,
    # and they are here.
    #
    # No adversary is postulated — the threat model has no third parties. The writer of a
    # wrong id is the development session itself: the document is written at finalization
    # and the id is pasted by hand, while a cycle has TWO reviews and the previous cycle had
    # its own. That is a typo, and typos are in the recorded model as corruption of one's
    # own data. The cost is asymmetric: a malformed record is visible immediately, whereas a
    # foreign id READS as valid and silently moves which review a promise is attributed to —
    # which is precisely the attribution the reconciliation report is built to make, so the
    # output is a confident sentence about a promise nobody gave in that review.
    own_reviews = {
        str(r)
        for r in (
            await session.scalars(
                select(Review.id).where(
                    Review.config["cycle_anchor"].astext == str(anchor_id)
                )
            )
        ).all()
    }
    documents = [_document_view(n) for n in rows]
    for doc in documents:
        affinity = doc["review_affinity"]
        # Only the POST-review intent still carries one; every other kind is empty by
        # construction and `_document_view` has already refused a non-empty value there.
        if not doc["readable"] or affinity is None:
            continue
        if affinity not in own_reviews:
            doc["readable"] = False
            doc["problem"] = (
                f"names `{AFFINITY_PROPERTY}` {affinity!r}, which is not a review of this "
                "cycle — a foreign or mistyped id reads as valid and attributes what the "
                "document promises to a review that never held it"
            )

    anchor_props = anchor.properties or {}
    return {
        "anchor": {
            "node_id": str(anchor.id),
            "label": anchor.label,
            "cycle": anchor_props.get("cycle"),
            "spec_path": anchor_props.get("spec_path"),
            "branch": anchor_props.get("branch"),
            "base_commit": anchor_props.get("base_commit"),
            "started_at": anchor_props.get("started_at"),
            "start_message": anchor_props.get("start_message"),
        },
        "documents": documents,
    }
