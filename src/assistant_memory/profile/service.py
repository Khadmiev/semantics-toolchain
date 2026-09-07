# SPDX-License-Identifier: Apache-2.0
"""Operator-profile service: resolution, versioning, compilation, and the four
validated write paths (spec docs/design/2026-07-14_operator_profile_plugin_spec.md,
review a6827c1a — element ids E13…E43 referenced below).

Design keys:
- TWO AXES (E40): Core node ``status`` stays epistemic; the profile ``standing``
  (member | candidate | parked) lives in ``profile_entries`` and ALONE drives
  compilation/resolution.
- VALIDATED PATH (E34): profile membership is a server-set marker; compilation
  additionally requires ``validated_version_id == nodes.current_version_id``.
- VERSION (E30): sha256 over every byte-affecting input of the compiled
  markdown EXCEPT the rendered version field itself.
- SENSITIVITY (E31/E36/E38): masked winners never fall back; withheld is a
  count; redaction keys off the CREDENTIAL's ``sensitive_capable`` flag.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import Access
from ..models.graph import Edge, Node, NodeSpace
from ..models.identity import Account, Credential, Membership, Space
from ..models.profile import ProfileDomain, ProfileEntry
from ..repository import graph as repo
from ..search import get_embedder, index_node
from ..search.indexer import index_fts

# F016: the closed shape of a registry key — kebab-case, bounded length. A domain
# is rendered into profile markdown headings, so it must never carry markup.
_DOMAIN_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_DOMAIN_MAX = 48


async def _index(session: AsyncSession, node: Node) -> None:
    """Reindex a node's current version (F017) — every validated profile mutation
    keeps search/S14 surfaces current, on the direct AND staged path."""
    await index_node(session, node, get_embedder())

# Bump when the markdown TEMPLATE changes (the one version input no data digest
# can see — E30).
FORMAT_VERSION = "operator-profile-md/1"

# The initial controlled vocabulary (E32). Seeded into the registry at bootstrap.
INITIAL_DOMAINS = (
    "language",
    "answer-format",
    "doc-references",
    "ui-buttons",
    "verbosity",
    "tone",
    "explanation-level",
    # B.12 C-7: the operator does not read what is written for machines. Added to the
    # shipped vocabulary because it is not a property of one operator — it is a DEFAULT of
    # every install, and the operator it most protects (a new one) is the least likely to
    # state it.
    "machine-artifact-reading",
)

#: B.12 C-7 — the one domain that ships with a VALUE, not just a registry slot. Every other
#: initial domain is an empty box the operator fills; this one starts filled, because the
#: rule it carries has to hold from the first minute of an install and its subject cannot
#: be expected to state it.
#:
#: Operator, 2026-08-25 (translated from Russian): "that the operator does not read what is
#: meant for the machine is a general default". The consequence is mechanical rather than
#: stylistic: a machine artifact either has a human projection, or it never becomes the
#: basis of a question to a human.
#:
#: An operator who DOES read such documents overrides this like any other profile rule —
#: it is a seeded member entry, not a constant.
#:
#: WHAT IS NOT SEEDED HERE, and it matters: the operator's LANGUAGE. That value is asked
#: for by the INSTALL PROCEDURE, which is external to this code and gated on publication
#: (operator, translated: "the install procedure comes before the repository is published in
#: any case; without it I will not publish").
#: Guessing it from a host locale would be a guess about a person, and ending guesses about
#: the operator is what the profile is for. This structural bootstrap runs unattended
#: inside the container with no human present, so it CANNOT ask — naming it as the source
#: would be worse than naming none: an absent owner reads as a gap, an impossible one reads
#: as done.
SEEDED_DEFAULT_ENTRIES: dict[str, dict[str, str]] = {
    "machine-artifact-reading": {
        "text": (
            "The operator does not read what is written for the machine: specs, prompts, "
            "exchange contracts, coverage tables, round digests. So a machine artifact "
            "either has a human projection, or it never becomes the basis of a question "
            "to a human. Tidied machine material is NOT a projection."
        ),
        "why": (
            "A default of every installation, not a trait of one operator: the person who "
            "needs this rule most, a new operator, is the least likely to state it. "
            "Overridden like any other profile rule."
        ),
    }
}

PREFERENCE_TAG = "preference"
SENSITIVE_LEVELS = ("high", "critical")


class ProfileError(Exception):
    """Base for controlled profile-path refusals (mapped to InvalidArgument upstream)."""


class DomainUnknown(ProfileError):
    def __init__(self, domain: str, known: list[str]):
        super().__init__(
            f"unknown preference domain '{domain}' — known domains: {', '.join(sorted(known))}. "
            "Pass new_domain=true to propose a new one (it stays pending, non-binding, until the "
            "operator accepts it via resolve_domain)."
        )
        self.domain = domain


class DomainRejected(ProfileError):
    def __init__(self, domain: str):
        super().__init__(
            f"preference domain '{domain}' was REJECTED by the operator — writes against it are "
            "refused; pick an accepted domain or propose a different one"
        )


class OwnershipError(ProfileError):
    """Target preference is not owned by the calling account (E35) or is not a profile node."""


class BaseMismatch(ProfileError):
    """Mode-1 base-freshness refusal (E33/E36). Carries a redaction-aware payload."""

    def __init__(self, payload: dict):
        super().__init__(
            "effective rule changed since the inference — explicit re-confirmation needed"
        )
        self.payload = payload


class ProtectedNode(ProfileError):
    """Generic mutation refused: the node is control-plane protected (E34/B5)."""

    def __init__(self, kind: str):
        super().__init__(
            "this node is operator-profile control-plane data "
            f"({kind}) — generic updates and deletes are refused; use the profile "
            "capabilities (remember_preference / confirm_preference / resolve_domain)"
        )


# --- identity (B3) ---------------------------------------------------------


async def personal_space_id(session: AsyncSession, account_id: uuid.UUID) -> uuid.UUID | None:
    """The account's personal space: template='personal', created by the account itself."""
    return await session.scalar(
        select(Space.id)
        .join(Membership, Membership.space_id == Space.id)
        .where(
            Membership.account_id == account_id,
            Space.template == "personal",
            Space.created_by == account_id,
        )
        .order_by(Space.created_at)
        .limit(1)
    )


async def ensure_person(session: AsyncSession, account: Account) -> Node:
    """The account's own Person node; auto-created in the personal space on first
    global write (B3). A missing personal space is an explicit error — the write
    never falls back into another zone."""
    if account.person_node_id is not None:
        node = await session.get(Node, account.person_node_id)
        if node is not None and node.deleted_at is None:
            return node
    space_id = await personal_space_id(session, account.id)
    if space_id is None:
        raise ProfileError(
            "this account has no personal space — onboarding is incomplete; a global "
            "preference cannot be stored (and will not fall back into another zone)"
        )
    node = await repo.create_node(
        session,
        type="Person",
        space_id=space_id,
        account_id=account.id,
        label=account.label or "Operator",
        properties={"text": f"Operator Person node for account {account.id} (auto-created "
                            "by the operator-profile write path)."},
    )
    await _index(session, node)
    account.person_node_id = node.id
    return node


# --- domain registry (E32, B5 lookup side) ---------------------------------


async def normalize_domain(
    session: AsyncSession, domain: Any, *, new_domain: bool = False
) -> tuple[str, str]:
    """Validate + alias-normalize a domain key. Returns (canonical_key, state).

    accepted → itself; alias → its canonical key; pending → itself (stored but
    never compiled); rejected → refused; unknown → refused unless ``new_domain``
    (then the CALLER must create the pending registry entry — see
    ``create_pending_domain``; this function only classifies).
    """
    if not isinstance(domain, str) or not domain.strip():
        raise ProfileError("domain must be a non-empty kebab-case string")
    key = domain.strip().lower()
    if not _DOMAIN_RE.fullmatch(key) or len(key) > _DOMAIN_MAX:
        raise ProfileError(
            f"invalid domain key {key!r} — a domain is kebab-case ([a-z0-9-], "
            f"max {_DOMAIN_MAX} chars); it is rendered into profile markup and must "
            "never carry spaces, punctuation, or newlines"
        )
    row = await session.get(ProfileDomain, key)
    if row is None:
        if new_domain:
            return key, "new"
        known = list(
            await session.scalars(
                select(ProfileDomain.domain).where(ProfileDomain.state == "accepted")
            )
        )
        raise DomainUnknown(key, known)
    if row.state == "alias":
        return row.canonical_of, "accepted"
    if row.state == "rejected":
        raise DomainRejected(key)
    return key, row.state  # accepted | pending


async def registry_home(session: AsyncSession) -> uuid.UUID:
    """The zone where the domain registry lives (B5: the memory-owner's zone —
    where the seeded entries are). Registry state never lands in project zones."""
    home = await session.scalar(
        select(Node.origin_space)
        .join(ProfileDomain, ProfileDomain.node_id == Node.id)
        .where(Node.origin_space.is_not(None))
        .limit(1)
    )
    if home is None:
        raise ProfileError(
            "the domain registry is not seeded — bootstrap has not run on this instance"
        )
    return home


async def create_pending_domain(
    session: AsyncSession, *, domain: str, account_id: uuid.UUID
) -> ProfileDomain:
    """Create a PENDING registry entry (audited graph node + index row) — E32/F19:
    pending domains never compile and never affect resolution until accepted.
    The node lands in the REGISTRY HOME zone regardless of where the triggering
    preference lives (F006)."""
    node = await repo.create_node(
        session,
        type="Note",
        space_id=await registry_home(session),
        account_id=account_id,
        label=f"Preference domain: {domain} (pending)",
        properties={
            "text": f"Operator-profile domain registry entry '{domain}'. State: pending — "
            "proposed by an agent, NOT binding until the operator accepts it via "
            "resolve_domain.",
            "registry_state": "pending",
        },
        status="provisional",
    )
    await _index(session, node)
    row = ProfileDomain(domain=domain, node_id=node.id, state="pending")
    session.add(row)
    await session.flush()
    return row


# --- protection (E34 total protection; B5 registry protection) -------------


async def protected_kind(session: AsyncSession, node_id: uuid.UUID) -> str | None:
    """'profile preference' / 'domain registry entry' when the node is control-plane
    protected; None otherwise. Generic update/delete paths call this and refuse."""
    if await session.get(ProfileEntry, node_id) is not None:
        return "profile preference"
    reg = await session.scalar(select(ProfileDomain).where(ProfileDomain.node_id == node_id))
    return "domain registry entry" if reg is not None else None


def check_unprotected(kind: str | None) -> None:
    if kind is not None:
        raise ProtectedNode(kind)


def _event(
    kind: str,
    *,
    account: Account,
    credential: Credential | None,
    scope: str | None = None,
    detail: dict | None = None,
) -> dict:
    """A structured confirming/stated event (F014): timestamp, account, relaying
    credential, scope, and operation detail — the Core S6 audit record."""
    return {
        "event": kind,
        "at": datetime.now(UTC).isoformat(),
        "account": str(account.id),
        "credential": str(credential.id) if credential is not None else None,
        **({"scope": scope} if scope else {}),
        **(detail or {}),
    }


async def visible_space_ids(
    session: AsyncSession, *, account_id: uuid.UUID, scopes: set[uuid.UUID] | None
) -> set[uuid.UUID]:
    """The spaces this CREDENTIAL can read: membership ∩ scopes (F011)."""
    member = set(
        await session.scalars(
            select(Membership.space_id).where(Membership.account_id == account_id)
        )
    )
    return member if scopes is None else member & scopes


async def check_entry_access(
    session: AsyncSession,
    *,
    entry: ProfileEntry,
    scopes: set[uuid.UUID] | None,
) -> None:
    """Entry-aware B4 authorization (F019): a PROJECT preference is confirmed
    through ITS OWN project space — a writable shared copy elsewhere is not
    enough. Requires (a) an active reference in entry.project_space_id and
    (b) the credential's scoped write/admin membership on that exact space."""
    if entry.scope != "project":
        return
    ref = await session.scalar(
        select(NodeSpace.node_id)
        .where(
            NodeSpace.node_id == entry.node_id,
            NodeSpace.space_id == entry.project_space_id,
        )
        .limit(1)
    )
    if ref is None:
        raise OwnershipError(
            "project preference is no longer referenced in its own project space — "
            "refusing to operate on it through a shared copy"
        )
    if scopes is not None and entry.project_space_id not in scopes:
        raise OwnershipError(
            "confirmation requires the credential's scope to cover the preference's "
            "own project space — a shared copy elsewhere is not enough"
        )
    perm = await session.scalar(
        select(Membership.permission).where(
            Membership.account_id == entry.account_id,
            Membership.space_id == entry.project_space_id,
        )
    )
    if perm not in ("write", "admin"):
        raise OwnershipError(
            "the account holds no write permission on the preference's own project space"
        )


async def check_scoped_write(
    session: AsyncSession, *, credential: Credential, node_id: uuid.UUID
) -> None:
    """B4 ordinary-access validation, shared by the MCP tool and the staged apply
    path (F012): the node must be VISIBLE to the credential's scoped access and
    the account must hold write/admin on it — re-checked AT EFFECT TIME. When the
    node is a profile entry, the entry-aware project check (F019) applies on top."""
    scopes = (
        None
        if credential.scopes is None
        else {uuid.UUID(str(s)) for s in credential.scopes}
    )
    access = Access(session, credential.account_id, scopes=scopes)
    node = await access.get_node(node_id)
    if node is None:
        raise OwnershipError(
            "target preference is not visible to the proposing credential at apply time"
        )
    if await access.effective_permission(node_id) not in ("write", "admin"):
        raise OwnershipError(
            "the proposing credential's account holds no write permission on the target "
            "at apply time"
        )
    entry = await session.get(ProfileEntry, node_id)
    if entry is not None:
        await check_entry_access(session, entry=entry, scopes=scopes)


# --- resolution + compilation (B1, E13/E19/E20/E30/E31/E42) -----------------


def _effective_sensitivity(node: Node) -> str:
    return node.sensitivity or "normal"


async def _member_entries(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    project_space_id: uuid.UUID | None,
    visible_spaces: set[uuid.UUID] | None = None,
) -> list[tuple[ProfileEntry, Node]]:
    """All standing-member rows relevant to (account, project ctx) whose node is
    live and whose current version was produced by the validated path (E34/E42).
    ``visible_spaces`` (F011) restricts to nodes readable by the calling
    CREDENTIAL — B1's read path; server-internal resolution (base recording,
    alias re-key) passes None and sees the account-wide truth."""
    scope_filter = (ProfileEntry.scope == "global") | (
        (ProfileEntry.scope == "project") & (ProfileEntry.project_space_id == project_space_id)
    ) if project_space_id is not None else (ProfileEntry.scope == "global")
    accepted = select(ProfileDomain.domain).where(ProfileDomain.state == "accepted")
    stmt = (
        select(ProfileEntry, Node)
        .join(Node, Node.id == ProfileEntry.node_id)
        .where(
            ProfileEntry.account_id == account_id,
            ProfileEntry.standing == "member",
            scope_filter,
            ProfileEntry.domain.in_(accepted),
            Node.deleted_at.is_(None),
            Node.current_version_id == ProfileEntry.validated_version_id,
        )
    )
    if visible_spaces is not None:
        stmt = stmt.where(
            ProfileEntry.node_id.in_(
                select(NodeSpace.node_id).where(NodeSpace.space_id.in_(visible_spaces))
            )
        )
    rows = await session.execute(stmt)
    return [(e, n) for e, n in rows.all()]


async def resolve_effective(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    project_space_id: uuid.UUID | None,
    visible_spaces: set[uuid.UUID] | None = None,
) -> dict[str, tuple[ProfileEntry, Node]]:
    """The PRE-FILTER resolved effective set: per domain, the project member if
    present, else the global member (E21). Sensitivity filtering happens AFTER
    (E31) — a sensitive winner masks its domain, never falls back."""
    winners: dict[str, tuple[ProfileEntry, Node]] = {}
    for entry, node in await _member_entries(
        session,
        account_id=account_id,
        project_space_id=project_space_id,
        visible_spaces=visible_spaces,
    ):
        cur = winners.get(entry.domain)
        if cur is None or (entry.scope == "project" and cur[0].scope == "global"):
            winners[entry.domain] = (entry, node)
    return winners


def _digest(
    winners: dict[str, tuple[ProfileEntry, Node]],
    *,
    withheld: int,
    account_id: uuid.UUID,
    project_space_id: uuid.UUID | None,
) -> str:
    """sha256 over every byte-affecting input EXCEPT the rendered version field
    (E30): pre-filter tuples (masked winners included — hashed, never exposed),
    the PER-WINNER delivery state (F021: two domains swapping masked/deliverable
    with a constant withheld count still change the bytes — the flag, not just
    the count, is a version input), withheld count, identity of the profile,
    and the format constant."""
    tuples = sorted(
        (
            d,
            str(e.node_id),
            str(e.validated_version_id),
            e.scope,
            _effective_sensitivity(n) in SENSITIVE_LEVELS,  # masked?
        )
        for d, (e, n) in winners.items()
    )
    canon = json.dumps(
        {
            "format": FORMAT_VERSION,
            "account": str(account_id),
            "project": str(project_space_id) if project_space_id else "",
            "withheld": withheld,
            "tuples": tuples,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _compile_markdown(
    deliverable: dict[str, tuple[ProfileEntry, Node]], *, version: str, withheld: int
) -> str:
    """Deterministic server part of the local profile file (E19/E27): version
    header + per-domain rules with their portable whys + the withheld note.
    NO timestamps — fetch metadata is client-side cache state."""
    lines = ["# Operator profile (compiled)", f"version: {version}", ""]
    for domain in sorted(deliverable):
        entry, node = deliverable[domain]
        props = node.properties or {}
        lines.append(f"## {domain} ({entry.scope})")
        lines.append(props.get("text", "").strip())
        why = (props.get("why") or "").strip()
        if why:
            lines.append(f"why: {why}")
        lines.append("")
    if withheld:
        lines.append(f"_{withheld} preference(s) withheld: sensitivity_")
        lines.append("")
    return "\n".join(lines)


async def build_profile(
    session: AsyncSession,
    *,
    account: Account,
    project_space_id: uuid.UUID | None,
    visible_spaces: set[uuid.UUID] | None = None,
) -> dict:
    """B1 get_operator_profile. Identity is folded in (B3): an account with no
    Person node gets person_node_id=null and an empty profile — never an error.
    ``visible_spaces`` scopes the read to the calling credential (F011), so
    access changes alter the resolved set — and therefore the version."""
    winners = await resolve_effective(
        session,
        account_id=account.id,
        project_space_id=project_space_id,
        visible_spaces=visible_spaces,
    )
    deliverable = {
        d: (e, n)
        for d, (e, n) in winners.items()
        if _effective_sensitivity(n) not in SENSITIVE_LEVELS
    }
    withheld = len(winners) - len(deliverable)
    version = _digest(
        winners, withheld=withheld, account_id=account.id, project_space_id=project_space_id
    )
    return {
        "operator": {
            "account_id": str(account.id),
            "person_node_id": str(account.person_node_id) if account.person_node_id else None,
            "display_name": account.label,
        },
        "version": version,
        "profile_markdown": _compile_markdown(deliverable, version=version, withheld=withheld),
        "preferences": [
            {
                "id": str(n.id),
                "scope": e.scope,
                "domain": d,
                "text": (n.properties or {}).get("text"),
                "why": (n.properties or {}).get("why"),
                "core_status": n.status,
            }
            for d, (e, n) in sorted(deliverable.items())
        ],
        "withheld": {"count": withheld},
    }


# --- B2: the validated preference write path --------------------------------


async def _slot(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    scope: str,
    domain: str,
    project_space_id: uuid.UUID | None,
    standing: str,
) -> ProfileEntry | None:
    return await session.scalar(
        select(ProfileEntry).where(
            ProfileEntry.account_id == account_id,
            ProfileEntry.scope == scope,
            ProfileEntry.domain == domain,
            ProfileEntry.project_space_id.is_(None)
            if project_space_id is None
            else ProfileEntry.project_space_id == project_space_id,
            ProfileEntry.standing == standing,
        )
    )


async def _resolved_base(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    domain: str,
    project_space_id: uuid.UUID | None,
) -> tuple[str | None, uuid.UUID | None, uuid.UUID | None]:
    """The resolved effective rule for a domain in a scope context (E33): the
    project member if present, else the global member, else the null triple."""
    winners = await resolve_effective(
        session, account_id=account_id, project_space_id=project_space_id
    )
    hit = winners.get(domain)
    if hit is None:
        return None, None, None
    entry, _node = hit
    return entry.scope, entry.node_id, entry.validated_version_id


async def _ensure_preference_tag(
    session: AsyncSession, *, space_id: uuid.UUID, account_id: uuid.UUID
) -> Node:
    """The `preference` Tag node in this space (F013) — the B2 shape contract is
    'a Note tagged preference', profile or not."""
    tag = await session.scalar(
        select(Node)
        .join(NodeSpace, NodeSpace.node_id == Node.id)
        .where(
            NodeSpace.space_id == space_id,
            Node.type == "Tag",
            Node.label == PREFERENCE_TAG,
            Node.deleted_at.is_(None),
        )
        .limit(1)
    )
    if tag is None:
        tag = await repo.create_node(
            session, type="Tag", space_id=space_id, account_id=account_id,
            label=PREFERENCE_TAG,
        )
    return tag


async def write_preference(
    session: AsyncSession,
    *,
    account: Account,
    text: str,
    domain: str,
    scope: str,
    space_id: uuid.UUID,
    project_space_id: uuid.UUID | None,
    why: str | None = None,
    inferred: bool = False,
    new_domain: bool = False,
    credential: Credential | None = None,
    index_semantic: bool = True,
) -> dict:
    """The profile write (B2). ``space_id`` is where the node lands (personal
    space for global — the caller resolves it via ensure_person/personal_space;
    the project zone for project scope). Standing split (E28): stated writes own
    the member slot and supersede any pending candidate; inferred writes own the
    candidate slot, record the resolved base, and never touch the member.

    ``index_semantic=False`` indexes full-text ONLY. It exists for exactly one caller,
    the structural bootstrap (B.12 C-7), which runs unattended inside the container at
    startup and must not load the embedding model — the same constraint the registry
    seeding beside it already obeys. It is a parameter rather than a second write path
    because a second path is how one operation quietly becomes two implementations that
    later disagree; the semantic backfill fills the embeddings afterwards."""
    key, state = await normalize_domain(session, domain, new_domain=new_domain)
    if state == "new":
        await create_pending_domain(session, domain=key, account_id=account.id)
    proj = project_space_id if scope == "project" else None
    person = await ensure_person(session, account) if scope == "global" else None

    label = f"Preference [{key}]: {text[:60]}"
    props: dict[str, Any] = {"text": text.strip(), "domain": key, "scope": scope}
    if why and why.strip():
        props["why"] = why.strip()
    # F014: the structured stated/inferred event — for a stated write the
    # operator's direct instruction IS the confirming event (Core S6).
    props["stated_event" if not inferred else "inferred_event"] = _event(
        "operator stated preference" if not inferred else "agent-inferred candidate",
        account=account, credential=credential, scope=scope,
    )

    index = _index if index_semantic else index_fts

    standing = "candidate" if inferred else "member"
    core_status = "provisional" if inferred else "current"
    existing = await _slot(
        session,
        account_id=account.id,
        scope=scope,
        domain=key,
        project_space_id=proj,
        standing=standing,
    )

    if existing is not None:
        node = await repo.update_node(
            session,
            node_id=existing.node_id,
            account_id=account.id,
            expected_version=existing.validated_version_id,
            properties_patch=props,
            label=label,
            status=core_status,
        )
        await index(session, node)
        existing.validated_version_id = node.current_version_id
        entry = existing
        # F020: a rewrite REPAIRS the declared shape in the same validated
        # transaction — the tag edge and (for globals) the Person attachment are
        # idempotent links, so a well-formed node is a no-op and a pre-fix or
        # malformed one comes out conformant rather than staying binding-but-broken.
        tag = await _ensure_preference_tag(
            session, space_id=space_id, account_id=account.id
        )
        await repo.link(
            session, type="tagged_with", src_node=node.id, dst_node=tag.id,
            account_id=account.id,
        )
        if person is not None:
            await repo.link(
                session, type="contained_in", src_node=node.id, dst_node=person.id,
                account_id=account.id,
            )
    else:
        node = await repo.create_node(
            session,
            type="Note",
            space_id=space_id,
            account_id=account.id,
            label=label,
            properties=props,
            status=core_status,
        )
        entry = ProfileEntry(
            node_id=node.id,
            account_id=account.id,
            scope=scope,
            project_space_id=proj,
            domain=key,
            standing=standing,
            validated_version_id=node.current_version_id,
        )
        session.add(entry)
        if person is not None:
            # Global preferences ATTACH to the operator's Person node (B3).
            await repo.link(
                session,
                type="contained_in",
                src_node=node.id,
                dst_node=person.id,
                account_id=account.id,
            )
        # F013: the B2 shape contract — a Note tagged `preference`.
        tag = await _ensure_preference_tag(
            session, space_id=space_id, account_id=account.id
        )
        await repo.link(
            session,
            type="tagged_with",
            src_node=node.id,
            dst_node=tag.id,
            account_id=account.id,
        )
        await index(session, node)

    if inferred:
        # Record the resolved effective base at inference time (E33). Never
        # rewritten afterwards — the freshness guard's baseline.
        b_scope, b_node, b_version = await _resolved_base(
            session, account_id=account.id, domain=key, project_space_id=proj
        )
        entry.base_scope, entry.base_node_id, entry.base_version_id = b_scope, b_node, b_version
    else:
        # A direct statement outranks a pending inference (E28): supersede the
        # candidate in the same transaction.
        candidate = await _slot(
            session,
            account_id=account.id,
            scope=scope,
            domain=key,
            project_space_id=proj,
            standing="candidate",
        )
        if candidate is not None:
            displaced_candidate = await repo.update_node(
                session,
                node_id=candidate.node_id,
                account_id=account.id,
                expected_version=candidate.validated_version_id,
                status="superseded",
            )
            await _index(session, displaced_candidate)
            await repo.link(
                session,
                type="supersedes",
                src_node=node.id,
                dst_node=candidate.node_id,
                account_id=account.id,
            )
            await session.delete(candidate)

    await session.flush()
    return {
        "node_id": str(node.id),
        "domain": key,
        "scope": scope,
        "standing": standing,
        "domain_state": "pending" if state in ("new", "pending") else "accepted",
    }


async def retire_preference(
    session: AsyncSession,
    *,
    account: Account,
    domain: str,
    scope: str,
    project_space_id: uuid.UUID | None,
    credential: Credential | None = None,
) -> dict:
    """B2 `retire: true` (F003): stated retirement of the key's binding member —
    Core status → superseded (no successor), standing removed, the operator's
    stated instruction recorded as the confirming event."""
    key, _state = await normalize_domain(session, domain)
    proj = project_space_id if scope == "project" else None
    member = await _slot(
        session,
        account_id=account.id,
        scope=scope,
        domain=key,
        project_space_id=proj,
        standing="member",
    )
    if member is None:
        raise ProfileError(
            f"no binding preference for domain '{key}' ({scope}) — nothing to retire"
        )
    retired = await repo.update_node(
        session,
        node_id=member.node_id,
        account_id=account.id,
        expected_version=member.validated_version_id,
        properties_patch={
            "retired": _event(
                "operator stated retirement", account=account, credential=credential,
                scope=scope,
            )
        },
        status="superseded",
    )
    await _index(session, retired)
    await session.delete(member)
    await session.flush()
    return {"status": "ok", "node_id": str(member.node_id), "domain": key, "retired": True}


def observed_slots(
    member: ProfileEntry | None, candidate: ProfileEntry | None
) -> dict:
    """The slot snapshot a staged parked disposition carries (F005): what the
    operator SAW when deciding. Apply-time state must still match."""
    def _snap(e: ProfileEntry | None) -> list[str] | None:
        return [str(e.node_id), str(e.validated_version_id)] if e is not None else None

    return {"member": _snap(member), "candidate": _snap(candidate)}


async def current_slots(
    session: AsyncSession, entry: ProfileEntry
) -> tuple[ProfileEntry | None, ProfileEntry | None]:
    member = await _slot(
        session,
        account_id=entry.account_id,
        scope=entry.scope,
        domain=entry.domain,
        project_space_id=entry.project_space_id,
        standing="member",
    )
    candidate = await _slot(
        session,
        account_id=entry.account_id,
        scope=entry.scope,
        domain=entry.domain,
        project_space_id=entry.project_space_id,
        standing="candidate",
    )
    return member, candidate


def _check_slot_snapshot(
    expected: dict | None, member: ProfileEntry | None, candidate: ProfileEntry | None
) -> None:
    """F005: a staged disposition refuses when the slot the operator ruled on has
    changed since staging — a newer member/candidate must never be displaced
    by an approval formed against older state."""
    if expected is None:
        return
    actual = observed_slots(member, candidate)
    if actual != expected:
        raise ProfileError(
            "the member/candidate slot changed since this disposition was prepared — "
            "ask me to prepare it again against the current state"
        )


# --- B4: confirm_preference (two modes) -------------------------------------


def _redacted_or_full(node: Node | None, *, sensitive_capable: bool) -> dict:
    if node is None:
        return {"text": None}
    if _effective_sensitivity(node) in SENSITIVE_LEVELS and not sensitive_capable:
        return {"text": None, "withheld": "sensitivity"}
    return {"text": (node.properties or {}).get("text")}


async def _validate_shape(
    session: AsyncSession, *, entry: ProfileEntry, node: Node, account: Account
) -> None:
    """B4 shape validation (F018): the marked node must actually BE what the
    marker claims — a Note tagged `preference`, properties consistent with the
    marker's key, globals attached to the operator's Person node. A malformed
    marker is refused, never operated on."""
    props = node.properties or {}
    problems: list[str] = []
    if node.type != "Note":
        problems.append(f"node type is {node.type}, not Note")
    if props.get("domain") != entry.domain:
        problems.append("domain property does not match the profile marker")
    if props.get("scope") != entry.scope:
        problems.append("scope property does not match the profile marker")
    tagged = await session.scalar(
        select(Edge.id)
        .join(Node, Node.id == Edge.dst_node)
        .where(
            Edge.src_node == node.id,
            Edge.type == "tagged_with",
            Edge.valid_to.is_(None),
            Node.type == "Tag",
            Node.label == PREFERENCE_TAG,
        )
        .limit(1)
    )
    if tagged is None:
        problems.append("missing the `preference` tag")
    if entry.scope == "global":
        if account.person_node_id is None:
            problems.append("account has no Person node for a global preference")
        else:
            attached = await session.scalar(
                select(Edge.id)
                .where(
                    Edge.src_node == node.id,
                    Edge.type == "contained_in",
                    Edge.dst_node == account.person_node_id,
                    Edge.valid_to.is_(None),
                )
                .limit(1)
            )
            if attached is None:
                problems.append("global preference not attached to the operator's Person node")
    if problems:
        raise ProfileError(
            "malformed profile node — refusing to operate on it: " + "; ".join(problems)
        )


async def confirm_preference(
    session: AsyncSession,
    *,
    account: Account,
    credential: Credential | None,
    preference_id: uuid.UUID,
    disposition: str | None = None,
    expected_slots: dict | None = None,
    reason: str | None = None,
) -> dict:
    """B4 — mode keyed off the target's standing (E41). Validation before effect:
    preference shape, ACCOUNT-EQUALITY ownership (E35), then per-mode checks.

    B.13 A-9 adds candidate REJECTION (`disposition="reject"`) — the correction
    branch of the install-time observations path: the operator declines a pending
    candidate, the entry becomes a terminal rejected TOMBSTONE (a fourth standing),
    and the domain+scope slot is free for a corrected re-proposal.
    """
    entry = await session.get(ProfileEntry, preference_id)
    node = await session.get(Node, preference_id)
    if entry is None or node is None or node.deleted_at is not None:
        raise OwnershipError("target is not a profile preference node")
    if entry.account_id != account.id:
        raise OwnershipError(
            "target preference belongs to a different operator account — zone access "
            "confers no right over another account's profile"
        )

    if entry.standing == "rejected":
        # The tombstone is TERMINAL (B.13 A-9): the only legal call against it is a
        # further reject, answered idempotently FROM the retained record — never an
        # error, and never a transition. Every other disposition is refused by name,
        # so an expressly rejected record can never bind again nor lose its marker.
        # (Checked before shape validation on purpose: idempotent retry must answer
        # from the tombstone even if unrelated drift would fail the shape check.)
        if disposition == "reject":
            return {
                "status": "already_retired",
                "node_id": str(entry.node_id),
                "standing": "rejected",
                "detail": "this candidate was already rejected; the tombstone stands",
            }
        raise ProfileError(
            "this preference was rejected and its tombstone is terminal — no "
            "disposition may promote, retire, or detach it; propose afresh in the "
            "same domain and scope instead"
        )

    await _validate_shape(session, entry=entry, node=node, account=account)  # F018
    sensitive_capable = bool(credential and credential.sensitive_capable)

    if entry.standing == "member":
        if disposition == "reject":
            raise ProfileError(
                "this entry was promoted to a binding member since the rejection was "
                "prepared — a member retires only through the stated-retirement "
                "operation (remember_preference with retire: true), never through "
                "candidate rejection"
            )
        return {"status": "noop", "detail": "already a binding member preference"}

    if entry.standing == "candidate":
        if disposition == "reject":
            return await _reject_candidate(
                session, account=account, entry=entry, node=node,
                credential=credential, reason=reason,
            )
        if disposition is not None:
            raise ProfileError(
                "a candidate confirmation takes no disposition argument "
                "(the one exception is 'reject' — B.13 A-9's correction branch)"
            )
        # (d) BASE-FRESH against the RESOLVED effective rule (E33).
        b_scope, b_node, b_version = await _resolved_base(
            session,
            account_id=account.id,
            domain=entry.domain,
            project_space_id=entry.project_space_id,
        )
        if (b_scope, b_node, b_version) != (
            entry.base_scope,
            entry.base_node_id,
            entry.base_version_id,
        ):
            newer = await session.get(Node, b_node) if b_node else None
            raise BaseMismatch(
                {
                    "status": "refused",
                    "reason": "effective rule changed since the inference",
                    "newer_rule": _redacted_or_full(newer, sensitive_capable=sensitive_capable),
                    # F007: the candidate's own text obeys the SAME redaction gate
                    "stale_candidate": _redacted_or_full(
                        node, sensitive_capable=sensitive_capable
                    ),
                    "action": "re-ask the operator; on explicit re-confirmation re-submit "
                    "against the fresh base"
                    + (
                        ""
                        if sensitive_capable
                        else " through a sensitivity-capable credential"
                    ),
                }
            )
        return await _promote(
            session, account=account, entry=entry, node=node, credential=credential
        )

    # standing == 'parked' (Mode 2)
    if disposition is None:
        raise ProfileError(
            "a parked preference needs an explicit disposition: promote_current | "
            "promote_candidate | retire | detach"
        )
    member, candidate = await current_slots(session, entry)
    _check_slot_snapshot(expected_slots, member, candidate)  # F005
    return await _dispose_parked(
        session, account=account, entry=entry, node=node, disposition=disposition,
        credential=credential,
    )


async def _reject_candidate(
    session: AsyncSession,
    *,
    account: Account,
    entry: ProfileEntry,
    node: Node,
    credential: Credential | None = None,
    reason: str | None = None,
) -> dict:
    """B.13 A-9: one atomic paired transition, its Core outcome named.

    The entry VACATES its domain+scope slot WITHOUT deleting its profile marker —
    the row is retained with the standing literal ``rejected`` (the slot uniqueness
    indexes key only member/candidate, so a fresh re-proposal coexists with the
    tombstone), which keeps account-and-scope ownership for the retry's
    authorization and keeps the node control-plane protected (generic update/delete
    key on the row's existence). The node itself is preserved with Core status
    explicitly ``rejected`` — never merely superseded — carrying ``rejected_reason``
    and the confirming event's provenance. The retained row is what answers the
    idempotent repeated reject, durably: the lookup survives restart because the
    row does.
    """
    rejected = await repo.update_node(
        session,
        node_id=entry.node_id,
        account_id=account.id,
        expected_version=entry.validated_version_id,
        properties_patch={
            "rejected_reason": reason
            or "operator rejection (relayed); no reason text supplied",
            "rejected": _event(
                "operator rejection (relayed)", account=account,
                credential=credential, scope=entry.scope,
            ),
        },
        status="rejected",
    )
    await _index(session, rejected)
    entry.standing = "rejected"
    entry.validated_version_id = rejected.current_version_id
    entry.base_scope = entry.base_node_id = entry.base_version_id = None
    await session.flush()
    return {"status": "ok", "node_id": str(entry.node_id), "standing": "rejected"}


async def _promote(
    session: AsyncSession,
    *,
    account: Account,
    entry: ProfileEntry,
    node: Node,
    credential: Credential | None = None,
) -> dict:
    """Both axes in one transaction (F37): candidate→member + provisional→current;
    the displaced member superseded + standing removed."""
    displaced = await _slot(
        session,
        account_id=account.id,
        scope=entry.scope,
        domain=entry.domain,
        project_space_id=entry.project_space_id,
        standing="member",
    )
    if displaced is not None:
        displaced_node = await repo.update_node(
            session,
            node_id=displaced.node_id,
            account_id=account.id,
            expected_version=displaced.validated_version_id,
            status="superseded",
        )
        await _index(session, displaced_node)
        await session.delete(displaced)  # no superseded node retains a standing
        await session.flush()
    promoted = await repo.update_node(
        session,
        node_id=entry.node_id,
        account_id=account.id,
        expected_version=entry.validated_version_id,
        properties_patch={
            "confirmed": _event(
                "operator confirmation (relayed)", account=account,
                credential=credential, scope=entry.scope,
                detail={"promoted_from": node.status},
            )
        },
        status="current",
    )
    if displaced is not None:
        await repo.link(
            session,
            type="supersedes",
            src_node=entry.node_id,
            dst_node=displaced.node_id,
            account_id=account.id,
        )
    await _index(session, promoted)
    entry.standing = "member"
    entry.validated_version_id = promoted.current_version_id
    entry.base_scope = entry.base_node_id = entry.base_version_id = None
    await session.flush()
    return {"status": "ok", "node_id": str(entry.node_id), "standing": "member"}


async def _dispose_parked(
    session: AsyncSession,
    *,
    account: Account,
    entry: ProfileEntry,
    node: Node,
    disposition: str,
    credential: Credential | None = None,
) -> dict:
    """Mode 2 (E39/E41): per-disposition slot validation; Core-status effects
    defined per source standing (F36) — no silent Core transitions."""
    if disposition == "promote_current":
        # Ex-member stays current; ex-candidate provisional→current with the
        # operator's disposition as the confirming event (F36).
        result = await _promote(
            session, account=account, entry=entry, node=node, credential=credential
        )
        result["disposition"] = "promote_current"
        return result
    if disposition == "promote_candidate":
        if node.status != "provisional":
            raise ProfileError(
                "promote_candidate is valid only for a parked node whose Core status is "
                "provisional (an ex-candidate); confirmed content goes to promote_current, "
                "retire, or detach — Core defines no current→provisional demotion"
            )
        existing = await _slot(
            session,
            account_id=account.id,
            scope=entry.scope,
            domain=entry.domain,
            project_space_id=entry.project_space_id,
            standing="candidate",
        )
        if existing is not None:
            displaced = await repo.update_node(
                session,
                node_id=existing.node_id,
                account_id=account.id,
                expected_version=existing.validated_version_id,
                status="superseded",
            )
            await _index(session, displaced)
            await repo.link(
                session,
                type="supersedes",
                src_node=entry.node_id,
                dst_node=existing.node_id,
                account_id=account.id,
            )
            await session.delete(existing)
            await session.flush()
        entry.standing = "candidate"
        await session.flush()
        return {"status": "ok", "node_id": str(entry.node_id), "standing": "candidate"}
    if disposition == "retire":
        retired = await repo.update_node(
            session,
            node_id=entry.node_id,
            account_id=account.id,
            expected_version=entry.validated_version_id,
            properties_patch={
                "retired": _event(
                    "operator disposition: retire", account=account,
                    credential=credential, scope=entry.scope,
                )
            },
            status="superseded",
        )
        await _index(session, retired)
        await session.delete(entry)
        await session.flush()
        return {"status": "ok", "node_id": str(node.id), "disposition": "retire"}
    if disposition == "detach":
        await session.delete(entry)
        await session.flush()
        return {"status": "ok", "node_id": str(node.id), "disposition": "detach"}
    raise ProfileError(
        f"unknown disposition '{disposition}' — expected promote_current | "
        "promote_candidate | retire | detach"
    )


# --- B5: resolve_domain ------------------------------------------------------


async def resolve_domain_action(
    session: AsyncSession,
    *,
    account: Account,
    domain: str,
    action: str,
    canonical: str | None = None,
    reason: str | None = None,
) -> dict:
    """The registry gate (E37). Trusted-credential authorization is enforced by
    the caller (MCP layer); this applies the state transition + re-keying."""
    key = (domain or "").strip().lower()
    if not _DOMAIN_RE.fullmatch(key) or len(key) > _DOMAIN_MAX:  # F016
        raise ProfileError(f"invalid domain key {key!r} — kebab-case, max {_DOMAIN_MAX} chars")
    row = await session.get(ProfileDomain, key)
    if row is None:
        raise ProfileError(f"domain '{key}' is not in the registry")
    reg_node = await session.get(Node, row.node_id)

    async def _stamp(state: str, extra: dict) -> None:
        stamped = await repo.update_node(
            session,
            node_id=reg_node.id,
            account_id=account.id,
            expected_version=reg_node.current_version_id,
            properties_patch={"registry_state": state, **extra},
            status="current" if state == "accepted" else reg_node.status,
        )
        await _index(session, stamped)

    if action == "accept":
        if row.state != "pending":  # F008: accept is a pending-domain transition
            raise ProfileError(
                f"domain '{key}' is {row.state} — only a pending domain can be accepted"
            )
        row.state = "accepted"
        await _stamp("accepted", {"accepted_by_operator": True})
        await session.flush()
        return {"status": "ok", "domain": key, "state": "accepted"}

    if action == "reject":
        if row.state != "pending":  # F008
            raise ProfileError(
                f"domain '{key}' is {row.state} — only a pending domain can be rejected"
            )
        row.state = "rejected"
        await _stamp("rejected", {"rejected_reason": reason or "operator rejection"})
        await session.flush()
        return {"status": "ok", "domain": key, "state": "rejected"}

    if action == "alias":
        if row.state not in ("pending", "accepted"):  # F008: no aliasing of aliases/rejected
            raise ProfileError(
                f"domain '{key}' is {row.state} — only a pending or accepted domain can "
                "become an alias"
            )
        target = (canonical or "").strip().lower()
        target_row = await session.get(ProfileDomain, target)
        if target_row is None or target_row.state != "accepted":
            raise ProfileError(f"alias target '{target}' must be an ACCEPTED canonical domain")
        if target == key:
            raise ProfileError("a domain cannot alias itself")
        row.state = "alias"
        row.canonical_of = target
        await _stamp("alias", {"canonical_of": target})
        parked: list[str] = []
        rekeys: list[str] = []
        entries = (
            await session.scalars(select(ProfileEntry).where(ProfileEntry.domain == key))
        ).all()
        for entry in entries:
            clean = False
            if entry.standing == "member":
                clash = await _slot(
                    session,
                    account_id=entry.account_id,
                    scope=entry.scope,
                    domain=target,
                    project_space_id=entry.project_space_id,
                    standing="member",
                )
                clean = clash is None
            elif entry.standing == "candidate":
                clash = await _slot(
                    session,
                    account_id=entry.account_id,
                    scope=entry.scope,
                    domain=target,
                    project_space_id=entry.project_space_id,
                    standing="candidate",
                )
                base = await _resolved_base(
                    session,
                    account_id=entry.account_id,
                    domain=target,
                    project_space_id=entry.project_space_id,
                )
                clean = clash is None and base == (
                    entry.base_scope,
                    entry.base_node_id,
                    entry.base_version_id,
                )
            # parked entries re-key without cleanliness checks (already parked)
            updated = await repo.update_node(
                session,
                node_id=entry.node_id,
                account_id=entry.account_id,
                expected_version=entry.validated_version_id,
                properties_patch={"domain": target},
            )
            await _index(session, updated)
            entry.validated_version_id = updated.current_version_id
            old_standing = entry.standing
            entry.domain = target
            if old_standing in ("member", "candidate") and not clean:
                entry.standing = "parked"
                entry.merge_provenance = {
                    "merged_from": key,
                    "into": target,
                    "was_standing": old_standing,
                }
                parked.append(str(entry.node_id))
            else:
                rekeys.append(str(entry.node_id))
            await session.flush()
        return {
            "status": "ok",
            "domain": key,
            "state": "alias",
            "canonical": target,
            "rekeyed": rekeys,
            "parked": parked,  # enumerate — the relaying agent reports these (E39)
        }

    raise ProfileError(f"unknown action '{action}' — expected accept | reject | alias")


# --- bootstrap seed ----------------------------------------------------------


async def seed_registry(
    session: AsyncSession, *, space_id: uuid.UUID, account_id: uuid.UUID
) -> int:
    """Idempotently seed the initial controlled vocabulary (E32) as ACCEPTED
    registry entries (audited nodes + index rows). Returns how many were created."""
    created = 0
    for key in INITIAL_DOMAINS:
        if await session.get(ProfileDomain, key) is not None:
            continue
        node = await repo.create_node(
            session,
            type="Note",
            space_id=space_id,
            account_id=account_id,
            label=f"Preference domain: {key}",
            properties={
                "text": f"Operator-profile domain registry entry '{key}'. State: accepted — "
                "part of the initial controlled vocabulary shipped with the plugin.",
                "registry_state": "accepted",
            },
        )
        # FTS-only at bootstrap (no embedder — the structural bootstrap must not
        # load the model); the semantic backfill fills embeddings later (F022:
        # index_node(None) would crash on embedder.embed).
        await index_fts(session, node)
        session.add(ProfileDomain(domain=key, node_id=node.id, state="accepted"))
        created += 1
        # B.12 C-7: a domain that ships WITH a value gets its member entry in the same act
        # that creates the registry slot — and ONLY then.
        #
        # THE GUARD IS THE SLOT'S CREATION, deliberately, and it is what makes this a
        # seeding rather than a standing opinion the container re-asserts at every start.
        # A retired preference DELETES its ProfileEntry row (`retire_preference`), so a
        # guard on "does a member entry exist" would resurrect a default the operator threw
        # away, on the next restart, silently. The registry row survives retirement, so
        # keying on its creation means: seeded once, at install, and never again.
        if key in SEEDED_DEFAULT_ENTRIES:
            seeded = SEEDED_DEFAULT_ENTRIES[key]
            account = await session.get(Account, account_id)
            # A global preference lands on the account's PERSON node in its personal space,
            # and refuses to fall back into another zone. The structural bootstrap creates
            # that space before it seeds anything, so the ordinary install path always has
            # one. A caller that seeds the registry for an account with no personal space —
            # an onboarding that has not finished — gets the registry slot alone rather
            # than a refusal: the vocabulary is not worth failing over a value.
            # THE RESOLVED SPACE IS USED, not merely checked (finding
            # `b12-seeded-global-preference-uses-caller-space`, round 2 of this slice's own
            # implementation review). This called `personal_space_id` for its truth value and
            # threw the answer away, then handed `write_preference` the CALLER's space — and
            # that function's contract puts the resolution on the caller ("personal space for
            # global — the caller resolves it"), so nothing downstream corrected it. A caller
            # seeding the registry from another zone wrote the global default into that zone,
            # which is the exact opposite of the sentence three lines above.
            personal = (
                await personal_space_id(session, account_id) if account is not None else None
            )
            if personal is not None:
                await write_preference(
                    session,
                    account=account,
                    text=seeded["text"],
                    domain=key,
                    scope="global",
                    space_id=personal,
                    project_space_id=None,
                    why=seeded["why"],
                    # FTS only — the structural bootstrap must not load the embedding
                    # model, exactly as the registry write above must not. The semantic
                    # backfill fills the embedding later.
                    index_semantic=False,
                )
    await session.flush()
    return created


async def snapshot_for_review(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    scopes: set[uuid.UUID] | None,
    project_space_id: uuid.UUID | None = None,
) -> dict | None:
    """The operator profile FROZEN into a review's config at creation (B.11 E-3).

    The semantic map is written "in my language, according to my profile" (the operator,
    translated), and the profile is real data rather than a figure of speech: it records
    that jargon is expanded on
    first use and that a reference to a spec section number is worthless to this reader.
    The prompt receives THIS snapshot, not a live lookup — the same discipline as every
    other frozen instrument of the loop, and for the same reason: a document produced by
    an instrument that can change underneath it is not comparable to the next one.

    Returns ``None`` when the credential resolves to no account — a review is not worth
    refusing over a missing profile, and a map with no profile is merely a map written
    to the general register.
    """
    account = await session.get(Account, account_id)
    if account is None:
        return None
    visible = await visible_space_ids(session, account_id=account_id, scopes=scopes)
    profile = await build_profile(
        session,
        account=account,
        project_space_id=project_space_id,
        visible_spaces=visible,
    )
    return {
        "version": profile.get("version"),
        "profile_markdown": profile.get("profile_markdown"),
        "frozen_at": datetime.now(UTC).isoformat(),
    }
