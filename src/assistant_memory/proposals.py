# SPDX-License-Identifier: Apache-2.0
"""Applying a staged proposal — shared by the web-admin approve route and the bot's in-chat
approval (design B). The human approving IS the confirmation, so the write policy is NOT re-run
(re-running would just re-stage it, since the proposer is low-trust). Reindexes node writes.

Neutral of any transport: raises ``ValueError`` on an op it cannot apply (callers map it to their
own error surface). Does not commit — the caller owns the transaction.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from .models.graph import Node
from .models.identity import Account, Credential
from .models.policy import Proposal
from .profile import service as profile_service
from .repository import errors as repo_errors
from .repository import graph as repo
from .search import get_embedder, index_node

CONTAINMENT_EDGE = "contained_in"


class MissingVersionToken(ValueError):
    """A staged update/delete carries no expected-version token. Fail closed (operator ruling
    2026-07-14): re-stage it. Applying it would restore exactly the silent-overwrite hole the token
    exists to close."""


def _staged_version(payload: dict) -> uuid.UUID:
    """The version the proposer WROTE AGAINST — the optimistic-concurrency token.

    Applying against `node.current_version_id` (what this did before) reads the version at APPROVAL
    time, which always matches, so the CAS check could never fail: a node edited between staging and
    approval was silently overwritten by a stale proposal. That defeats the recorded invariant
    ("the write carries the expected version; if it is no longer current -> conflict; no silent
    overwrite"). Now a stale approval raises ConflictError and rolls the batch back, and the
    operator is told, instead of quietly losing the newer edit.

    A proposal staged BEFORE this field existed carries no token, and is REFUSED rather than applied
    on the old terms (operator ruling 2026-07-14). Falling back would preserve the data-corruption
    hole for exactly the proposals that cannot prove they are current — and only the bot stages
    proposals at all (a limited-trust credential; every other client is trusted and writes
    directly), so its proposals live for minutes and are cheaper to re-stage than to approve
    unsafely.
    """
    raw = payload.get("expected_version")
    if not raw:
        raise MissingVersionToken(
            "this change was prepared before an update to the approval rules and can no longer be "
            "applied safely — ask me to prepare it again"
        )
    return uuid.UUID(str(raw))


class OrphanedChild(ValueError):
    """A create_node proposal named a parent that did not materialize. Fail closed: the child is
    NOT created parentless — an orphan is a silent wrong answer (the list that looks empty),
    which is worse than a visible failure."""


async def _attach_parent(
    session: AsyncSession,
    proposal: Proposal,
    child_id: uuid.UUID,
    actor: uuid.UUID,
) -> None:
    """Create the containment edge for a proposal that named a parent, in the SAME transaction as
    the child's create. The parent is either a node that already existed at staging time, or a
    SIBLING create_node proposal whose node id only exists now, after it was applied — which is
    why parents must be applied before their children (see `order_proposals`)."""
    parent = (proposal.payload or {}).get("parent")
    if not parent:
        return

    if parent.get("node_id"):
        parent_id = uuid.UUID(str(parent["node_id"]))
    else:
        sibling = await session.get(Proposal, uuid.UUID(str(parent["proposal_id"])))
        if sibling is None or sibling.created_node_id is None:
            # The parent proposal was rejected, or has not been applied yet (a batch that was not
            # ordered parents-first). Either way we must not leave a child dangling.
            raise OrphanedChild(
                "the parent this node belongs to was not created — refusing to create it "
                "detached"
            )
        # THE APPROVAL UNIT IS THE BOUNDARY — ON EVERY TRANSPORT, not just the chat batch. The
        # web-admin route approves ONE proposal at a time, so a parent approved there earlier would
        # let a child be approved separately later and still link up: exactly the hidden cross-card
        # dependency the batch rule exists to kill, walking in through the other door. Inside one
        # batch the parent is applied but not yet MARKED approved, so the legitimate case passes.
        if sibling.status != "pending":
            raise OrphanedChild(
                "this change belongs inside another change that was approved separately — "
                "approve them together, or re-stage it against the node that now exists"
            )
        parent_id = sibling.created_node_id

    # A soft-deleted node is still a row, and `session.get` returns it happily. Linking a child
    # into a deleted parent is an orphan wearing an edge: the operator's item sits inside something
    # that no longer exists, and nothing complains.
    parent_node = await session.get(Node, parent_id)
    if parent_node is None or parent_node.deleted_at is not None:
        raise OrphanedChild(
            "the parent this node belongs to no longer exists — refusing to create it detached"
        )

    await repo.link(
        session,
        type=CONTAINMENT_EDGE,
        src_node=child_id,
        dst_node=parent_id,
        account_id=actor,
    )


def order_proposals(proposals: list[Proposal]) -> list[Proposal]:
    """Order a batch so a parent is applied BEFORE any child that references it.

    Staging order usually happens to be parents-first, but relying on that would make correctness
    depend on the model's call order. Kahn's algorithm over the sibling-parent references; a cycle
    (A parents B parents A) raises rather than applying a broken half.

    THE BATCH IS THE BOUNDARY. A ``parent.proposal_id`` must name a proposal in THIS batch — the
    sanctioned shape is "parent and child staged together, approved together". A reference to a
    proposal outside the batch is refused, not silently treated as a non-dependency: it would make
    the child's fate depend on whether some older card happened to be approved first, which is a
    hidden ordering dependency the operator never sees on the card they are answering. (Attaching
    to a node that ALREADY exists has its own, visible form: ``parent.node_id``.)
    """
    by_id = {p.id: p for p in proposals}
    depends: dict[uuid.UUID, uuid.UUID | None] = {}
    for p in proposals:
        parent = (p.payload or {}).get("parent") or {}
        raw = parent.get("proposal_id")
        dep = uuid.UUID(str(raw)) if raw else None
        if dep is not None and dep not in by_id:
            raise OrphanedChild(
                "a change in this batch belongs inside another change that is NOT in this batch — "
                "refusing to apply it (approve them together, or attach to an existing node)"
            )
        depends[p.id] = dep

    ordered: list[Proposal] = []
    emitted: set[uuid.UUID] = set()
    remaining = list(proposals)
    while remaining:
        ready = [p for p in remaining if depends[p.id] is None or depends[p.id] in emitted]
        if not ready:
            raise OrphanedChild("circular parent references among the staged changes")
        for p in ready:
            ordered.append(p)
            emitted.add(p.id)
        remaining = [p for p in remaining if p.id not in emitted]
    return ordered


async def apply_proposal(
    session: AsyncSession, proposal: Proposal, fallback_account: uuid.UUID
) -> None:
    """Apply the staged mutation directly to the repository under the proposer's account."""
    ref = proposal.target_ref or {}
    payload = proposal.payload or {}
    cred = (
        await session.get(Credential, proposal.proposer_client)
        if proposal.proposer_client
        else None
    )
    actor = cred.account_id if cred else fallback_account
    embedder = get_embedder()

    if proposal.op == "create_node":
        if proposal.created_node_id is not None:
            return  # already applied — applying it again would create a second node
        # `status` was silently dropped here before: a node proposed as `provisional` was applied
        # as `current` — the approval quietly upgraded the model's own epistemic hedge.
        status = payload.get("status") or "current"
        node = await repo.create_node(
            session,
            type=ref["type"],
            space_id=uuid.UUID(ref["space"]),
            account_id=actor,
            label=payload.get("label"),
            properties=payload.get("properties"),
            sensitivity=payload.get("sensitivity"),
            status=status,
        )
        await index_node(session, node, embedder)
        # Record what this proposal created, so a SIBLING proposal staged in the same turn can
        # resolve it as its parent (the node had no id when the sibling was staged).
        proposal.created_node_id = node.id
        await _attach_parent(session, proposal, node.id, actor)
    elif proposal.op == "update_node":
        node = await session.get(Node, uuid.UUID(ref["node_id"]))
        if node is None or node.deleted_at is not None:
            raise repo_errors.NotFoundError("target node gone")
        label = payload.get("label")
        status = payload.get("status")
        updated = await repo.update_node(
            session,
            node_id=node.id,
            account_id=actor,
            expected_version=_staged_version(payload),
            properties_patch=payload.get("patch"),
            label=repo._UNSET if label is None else label,
            # `status` was accepted at staging and then ignored here: approving a proposed
            # `provisional`/`superseded` left the node's status untouched — the same
            # epistemic-status loss the create path had.
            status=repo._UNSET if status is None else status,
        )
        await index_node(session, updated, embedder)
    elif proposal.op == "delete_node":
        node = await session.get(Node, uuid.UUID(ref["node_id"]))
        if node is None or node.deleted_at is not None:
            raise repo_errors.NotFoundError("target node gone")
        await repo.delete_node(
            session, node_id=node.id, expected_version=_staged_version(payload)
        )
    elif proposal.op == "link":
        await repo.link(
            session,
            type=ref["type"],
            src_node=uuid.UUID(ref["src"]),
            dst_node=uuid.UUID(ref["dst"]),
            account_id=actor,
            properties=payload.get("properties"),
        )
    elif proposal.op == "share_nodes":
        await repo.share_node(
            session,
            node_id=uuid.UUID(ref["node_id"]),
            space_id=uuid.UUID(ref["space"]),
            account_id=actor,
        )
    elif proposal.op == "share_edge":
        await repo.share_edge(
            session,
            edge_id=uuid.UUID(ref["edge_id"]),
            space_id=uuid.UUID(ref["space"]),
            account_id=actor,
        )
    elif proposal.op == "profile_write":
        # Operator-profile B2 staged under limited trust: the approval IS the
        # confirmation; the validated profile path runs at apply time (registry
        # normalization, standing split, Person bootstrap — all re-checked here).
        account = await session.get(Account, actor)
        space_id = uuid.UUID(ref["space"])
        try:
            if payload.get("retire"):
                await profile_service.retire_preference(
                    session,
                    account=account,
                    domain=ref["domain"],
                    scope=ref["scope"],
                    project_space_id=space_id if ref["scope"] == "project" else None,
                    credential=cred,
                )
                return
            result = await profile_service.write_preference(
                session,
                account=account,
                text=payload["text"],
                domain=ref["domain"],
                scope=ref["scope"],
                space_id=space_id,
                project_space_id=space_id if ref["scope"] == "project" else None,
                why=payload.get("why"),
                inferred=bool(payload.get("inferred")),
                new_domain=bool(payload.get("new_domain")),
                credential=cred,
            )
        except profile_service.ProfileError as exc:
            raise ValueError(str(exc)) from exc
        node = await session.get(Node, uuid.UUID(result["node_id"]))
        await index_node(session, node, embedder)
        proposal.created_node_id = node.id
    elif proposal.op == "profile_confirm":
        account = await session.get(Account, actor)
        try:
            # F012: re-run the B4 ordinary-access validation with the PROPOSER
            # credential AT EFFECT TIME — scope/permission changes between staging
            # and approval must refuse, not slide through.
            if cred is not None:
                await profile_service.check_scoped_write(
                    session, credential=cred, node_id=uuid.UUID(ref["node_id"])
                )
            await profile_service.confirm_preference(
                session,
                account=account,
                credential=cred,
                preference_id=uuid.UUID(ref["node_id"]),
                disposition=(payload or {}).get("disposition"),
                # F005: the slot state the operator SAW at staging; apply refuses
                # if it moved meanwhile.
                expected_slots=(payload or {}).get("slot_snapshot"),
                # The operator's stated reason survives the staging boundary
                # (finding staged-rejection-drops-reason).
                reason=(payload or {}).get("reason"),
            )
        except profile_service.BaseMismatch as exc:
            # Fail VISIBLY: the effective rule moved between staging and approval;
            # silently promoting a stale inference is the exact hazard E33 closes.
            raise ValueError(
                "the effective rule changed since this confirmation was prepared — "
                "ask me to prepare it again"
            ) from exc
        except profile_service.ProfileError as exc:
            raise ValueError(str(exc)) from exc
    elif proposal.op == "profile_domain":
        account = await session.get(Account, actor)
        try:
            await profile_service.resolve_domain_action(
                session,
                account=account,
                domain=ref["domain"],
                action=ref["action"],
                canonical=(payload or {}).get("canonical"),
                reason=(payload or {}).get("reason"),
            )
        except profile_service.ProfileError as exc:
            raise ValueError(str(exc)) from exc
    else:
        raise ValueError(f"cannot apply op {proposal.op}")
