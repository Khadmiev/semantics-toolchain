# SPDX-License-Identifier: Apache-2.0
"""Operator pings for the review loop (B.7 G-7 / H-2 / H-4).

The watcher and its supervisor must be able to reach the operator without holding any
credential beyond their per-review token, and without knowing anything about Telegram,
quiet hours or the graph. So the transport lives HERE, behind one endpoint: they POST a
channel role and a sentence; the service turns that into a real delivery.

WHY THE BOT PATH RATHER THAN A DIRECT SEND: the bot's `Delivery` nodes already carry the
operator's quiet hours, the at-least-once retry, and an auditable record of what was sent.
A ping that bypassed them would be a second, weaker notification system beside the one the
box already runs — and the first thing it would lose is the operator's own do-not-disturb
window. A gate that waits until morning is the correct behaviour for a review; an alarm
that ignores the window is not.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: it does not decide WHEN to ping. The role
semantics (primary / fallback), the once-only rule and the escalation delay all live in the
watcher, which is where the state that justifies a ping lives.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.service import ensure_owner, get_or_create_default_account
from ..config import settings
from ..models.identity import Membership, Space
from ..repository import graph
from sqlalchemy import select

DELIVERY_TYPE = "Delivery"
#: Delivery kind, so a ping is distinguishable in the operator's own record from a
#: scheduled briefing or an agent answer.
PING_KIND = "review_ping"


async def _operator_target(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """The owner account and the space its deliveries live in (the bootstrap's pair)."""
    owner = await ensure_owner(session, google_sub=settings.owner_google_sub or None)
    account = await get_or_create_default_account(session, owner)
    space_id = await session.scalar(
        select(Space.id)
        .join(Membership, Membership.space_id == Space.id)
        .where(Membership.account_id == account.id)
        .order_by(Space.created_at)
        .limit(1)
    )
    if space_id is None:
        raise RuntimeError("the owner account has no space — cannot queue an operator ping")
    return account.id, space_id


async def queue_operator_ping(
    session: AsyncSession,
    *,
    review_id: uuid.UUID,
    channel: str,
    text: str,
    urgent: bool = False,
) -> bool:
    """Queue one operator ping; the bot's delivery worker sends it.

    Returns True when the ping was QUEUED — never a claim that it was read. The distinction
    is the whole reason the watcher journals the outcome of every send: "the ping fired" and
    "the operator saw it" are different facts, and only the first one is knowable here.

    ``urgent`` is False by default, which means the operator's quiet hours hold it. A parked
    review is not an emergency; it is still parked in the morning.
    """
    account_id, space_id = await _operator_target(session)
    await graph.create_node(
        session,
        type=DELIVERY_TYPE,
        space_id=space_id,
        account_id=account_id,
        label=text[:60] or PING_KIND,
        properties={
            "text": text,
            "kind": PING_KIND,
            "target": None,
            "key": f"review-ping:{review_id}",
            "status": "pending",
            "channel": channel,
            "urgent": urgent,
        },
    )
    return True
