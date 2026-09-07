# SPDX-License-Identifier: Apache-2.0
"""Startup bootstrap: make a fresh instance usable before anyone logs in (IR-1).

Idempotent and cheap (no model): ensures the owner, a default `personal` space, the
owner account's **membership** in it (this is what makes seeded nodes VISIBLE to the
owner after login — without it the owner logs into an empty-looking memory), and — on
a genuinely fresh install only — seeds the conventions with full-text index only.
Semantic embeddings are filled later (backfill), so this can gate readiness without
loading bge-m3.

Startup writes the conventions AT MOST ONCE PER DEPLOYMENT. Until 2026-08-23 it seeded
on EVERY start, into the space found by the NAME `personal`; after the graph was split
into project spaces the projection left that space, the next start could not see it, and
a full second projection (30 nodes) was created silently. So the seed fires only when
BOTH hold: a conventions home RESOLVES (B.13 A-1: the recorded id in the
install-settings store — created and recorded by this bootstrap on a genuinely fresh
install, adopted from a pre-provisioned environment id, or repaired by the operator
through the admin space surface; never looked up by name), and no conventions Document
exists in ANY space. A fresh install therefore seeds into a space whose identity is
RECORDED in the same transaction that created it — chosen and auditable, not implied.

``conventions_visible_to_owner`` is the bootstrap-level observable (a LIVE conventions
Document visible to the owner account). Readiness itself is the install state
machine's stages 1-4 view (B.13 A-8, install/state.py) — GET /health/ready answers
from there, not from here, so there is one definition of readiness, not two.
"""

import logging
import uuid

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .auth.service import ensure_owner, get_or_create_default_account
from .config import settings
from .conventions import DOC_LABEL, seed_conventions
from .install import state as install_state
from .models.graph import Node, NodeSpace
from .models.identity import Account, Membership, Space, User
from .models.install import SETTING_CONVENTIONS_SPACE_ID
from .profile import service as profile_service

logger = logging.getLogger(__name__)

# One definition, shared with the seed-stage predicate (install/state.py, which this
# module already imports — the reverse import would be circular).
DEFAULT_SPACE_NAME = install_state.DEFAULT_SPACE_NAME

# What the seed counts look like when startup writes nothing at all.
_NO_SEED = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}


async def _ensure_personal_space(session: AsyncSession, account: Account) -> Space:
    """The account's `personal` space + an admin membership. Idempotent."""
    space = await session.scalar(
        select(Space)
        .join(Membership, Membership.space_id == Space.id)
        .where(Membership.account_id == account.id, Space.name == DEFAULT_SPACE_NAME)
        .order_by(Space.created_at)
        .limit(1)
    )
    if space is None:
        # Reuse an existing personal space if one exists (created via the admin UI),
        # else create it. Then ensure the membership either way.
        space = await session.scalar(
            select(Space)
            .where(Space.name == DEFAULT_SPACE_NAME)
            .order_by(Space.created_at)
            .limit(1)
        )
        if space is None:
            space = Space(name=DEFAULT_SPACE_NAME, template="personal", created_by=account.id)
            session.add(space)
            await session.flush()
    await session.execute(
        pg_insert(Membership)
        .values(account_id=account.id, space_id=space.id, permission="admin")
        .on_conflict_do_nothing(index_elements=["account_id", "space_id"])
    )
    await session.flush()
    return space


async def validate_configured_spaces(session: AsyncSession) -> None:
    """Fail loudly when a CONFIGURED space id does not resolve to an existing space.

    Unset is the legitimate pre-install state and passes silently. Set-but-dangling is a
    misconfiguration that would otherwise surface as a confusing runtime refusal much
    later — the caller (main.lifespan) lets this propagate and kill the boot, deliberately
    OUTSIDE the try/except that swallows bootstrap failures into a not-ready observable.
    """
    for field, value in (
        ("AM_CONVENTIONS_SPACE_ID", settings.conventions_space_id),
        ("AM_FEEDBACK_SPACE_ID", settings.feedback_space_id),
    ):
        if value is None:
            continue
        if await session.get(Space, value) is None:
            raise RuntimeError(
                f"{field}={value} does not resolve to an existing space. Create the space in "
                "the admin UI and configure its id, or unset the variable. Refusing to start."
            )


async def conventions_exist_anywhere(session: AsyncSession) -> bool:
    """Is there a conventions Document in ANY space, whatever its status?

    Unfiltered by status ON PURPOSE: a retired projection still means this deployment has
    been seeded once, and seeding a fresh copy alongside it is exactly the duplication this
    guard exists to prevent. (What may be SERVED as the conventions is a different, stricter
    question — that one filters on `current`.)
    """
    doc = await session.scalar(
        select(Node.id)
        .where(Node.type == "Document", Node.label == DOC_LABEL, Node.deleted_at.is_(None))
        .limit(1)
    )
    return doc is not None


async def _conventions_home_candidates(session: AsyncSession) -> list[uuid.UUID]:
    """Spaces that HOLD a conventions Document projection — the same-purpose
    candidates A-1's named error must list when no id is recorded."""
    rows = await session.execute(
        select(NodeSpace.space_id)
        .join(Node, Node.id == NodeSpace.node_id)
        .where(
            Node.type == "Document", Node.label == DOC_LABEL, Node.deleted_at.is_(None)
        )
        .distinct()
    )
    return [r[0] for r in rows]


async def resolve_conventions_home(
    session: AsyncSession, account: Account
) -> uuid.UUID | None:
    """A-1 stage 3: the conventions-home routing id, identity = the RECORDED id.

    Resolution order, each branch explicit:
    - a recorded id in the install-settings store -> verify it under the SHARED
      routing-home contract (existence AND the owner's membership,
      install_state.routing_home_problem; a failing record is repaired via the admin
      space surface, never silently replaced);
    - a pre-provisioned environment id -> adopt it after the SAME
      existence-and-ownership verification (A-1's adoption rule) and record it;
    - nothing recorded, but candidate same-purpose spaces exist -> a NAMED ERROR
      listing them; resolution is the operator's, never a pick (space names are not
      unique, and name-based selection produced the recorded 2026-08-27 incident);
    - a genuinely fresh install -> CREATE the home space and record its id in the
      same transaction — no administrative UI step is part of the stock path.
    """
    recorded = await install_state.get_setting(session, SETTING_CONVENTIONS_SPACE_ID)
    if recorded is not None:
        # Same class as the /spaces guard (finding malformed-routing-id-breaks-repair-ui):
        # a malformed stored value is a NAMED, repairable state, never a stack trace.
        try:
            home_id = uuid.UUID(str(recorded))
        except ValueError:
            logger.error(
                "bootstrap: recorded conventions-home id %r is not a uuid — repair it "
                "via the admin space surface (set-conventions-home)",
                recorded,
            )
            return None
        problem = await install_state.routing_home_problem(session, home_id, account.id)
        if problem is None:
            return home_id
        logger.error(
            "bootstrap: recorded conventions-home id %s fails the routing-home "
            "contract (%s) — repair it via the admin space surface "
            "(set-conventions-home)",
            home_id,
            problem,
        )
        return None

    if settings.conventions_space_id is not None:
        env_id = settings.conventions_space_id
        problem = await install_state.routing_home_problem(session, env_id, account.id)
        if problem is not None:
            logger.error(
                "bootstrap: AM_CONVENTIONS_SPACE_ID=%s fails the routing-home "
                "contract (%s) — not adopted",
                env_id,
                problem,
            )
            return None
        await install_state.set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(env_id))
        logger.info("bootstrap: adopted pre-provisioned conventions home %s", env_id)
        return env_id

    candidates = await _conventions_home_candidates(session)
    if candidates:
        logger.error(
            "bootstrap: no conventions-home id is recorded, but candidate "
            "same-purpose spaces exist: %s — the bootstrap refuses to pick; record "
            "one via the admin space surface (set-conventions-home)",
            ", ".join(str(c) for c in candidates),
        )
        return None

    home = Space(name="conventions", template="personal", created_by=account.id)
    session.add(home)
    await session.flush()
    session.add(
        Membership(account_id=account.id, space_id=home.id, permission="admin")
    )
    # The id is recorded IN THE SAME TRANSACTION as the creation (A-1) — the two
    # facts commit or roll back together, so no space can exist unrecorded.
    await install_state.set_setting(session, SETTING_CONVENTIONS_SPACE_ID, str(home.id))
    await session.flush()
    logger.info("bootstrap: created conventions home %s and recorded its id", home.id)
    return home.id


async def bootstrap(session: AsyncSession) -> dict[str, int]:
    """Structural bootstrap. Flushes; the caller commits. Returns the seed counts."""
    owner = await ensure_owner(session)
    account = await get_or_create_default_account(session, owner)
    space = await _ensure_personal_space(session, account)

    home_id = await resolve_conventions_home(session, account)
    if home_id is None:
        counts = _NO_SEED.copy()
        logger.info(
            "bootstrap: conventions not seeded — no resolvable home space; "
            "installation is not finished"
        )
    elif await conventions_exist_anywhere(session):
        counts = _NO_SEED.copy()
        logger.info("bootstrap: conventions already projected — startup seeds nothing")
    else:
        counts = await seed_conventions(
            session, space_id=home_id, account_id=account.id, embedder=None
        )
        logger.info("bootstrap: conventions seeded into home space %s: %s", home_id, counts)

    # Operator-profile initial controlled vocabulary (E32) — idempotent, and deliberately
    # NOT moved with the conventions: a restricted account keeps the same visibility gap for
    # the registry that the home space closes for the conventions (recorded as debt).
    domains = await profile_service.seed_registry(
        session, space_id=space.id, account_id=account.id
    )
    logger.info(
        "bootstrap: owner=%s account=%s space=%s conventions=%s profile_domains=%s",
        owner.id, account.id, space.id, counts, domains,
    )
    return counts


async def conventions_visible_to_owner(session: AsyncSession) -> bool:
    """True once a LIVE conventions Document is visible to the owner account (IR-1).

    B.11 D-3: the status filter, matching the resolution the `conventions` tool received in
    the 2026-08-23 conventions-home slice. Without it a deployment holding only a
    SUPERSEDED copy of the conventions reports ready and then serves requests with no live
    projection — and that situation exists right now in the `personal` space, where the
    owner sees two documents carrying the canonical title.

    That slice deliberately left readiness out of scope, and that was the right call for
    it; the family is the same predicate-on-an-incomplete-premise class as B.11's coverage
    predicate, which is why the repair rides here. The alternative — recording the
    divergence as accepted — was offered and declined (operator ruling 2026-08-24).

    Ordered oldest-first for the same reason the tool is: when two live copies exist, the
    two resolutions must pick the SAME one, or "ready" and "what you get" describe
    different documents.
    """
    owner_account = await session.scalar(
        select(Account.id)
        .join(User, User.id == Account.user_id)
        .where(User.is_owner.is_(True))
        .order_by(Account.created_at)
        .limit(1)
    )
    if owner_account is None:
        return False
    doc = await session.scalar(
        select(Node.id)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .join(
            Membership,
            and_(
                Membership.space_id == NodeSpace.space_id,
                Membership.account_id == owner_account,
            ),
        )
        .where(
            Node.type == "Document",
            Node.label == DOC_LABEL,
            Node.deleted_at.is_(None),
            # THE D-3 FILTER, and it belongs to THIS predicate and not to the existence
            # guard above. The two questions are different and their docstrings say so:
            # "has this deployment been seeded at all" is answered by any projection
            # whatever its status, while "is there something live to SERVE" is answered
            # only by a current one. Landing the filter on the guard instead — which is
            # what an ambiguous edit anchor did on the first attempt — both broke the
            # guard and left this element unimplemented (critic finding
            # `bootstrap-current-only-seed-guard`, round 6 of this slice's own review).
            Node.status == "current",
        )
        .limit(1)
    )
    return doc is not None
