# SPDX-License-Identifier: Apache-2.0
"""Review-orchestration HTTP API (spec §3.2). A FastAPI router mounted on the memory
app, sharing its Postgres + tunnel.

Two auth surfaces (§8): ``POST /reviews`` and the read-only console use the operator's
**bootstrap credential** (an existing account credential, resolved by the box's
opaque-bearer machinery); every per-review endpoint uses a **per-review token** that
resolves to exactly one ``review_id`` (isolation — a token can't reach another review).
Role is NOT token-enforced (option X): who may post which ``kind`` is prompt-maintained;
the one server-enforced guard is the ``converged`` validation, raised from the repository.
"""

import asyncio
import os
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import PlainTextResponse

from ..auth import AuthError, Principal, resolve_credential
from ..db import get_session
from ..models.review import LaunchProfileVersion, Review
from ..profile import service as profile_service
from . import cycle, instruments, round_gate
from .errors import (
    ConvergenceRefusedError,
    InstrumentValidationError,
    InvalidMessagePayloadError,
    InvalidReviewTokenError,
    InvalidStateTransitionError,
    ReviewCreationRefusedError,
    ReviewNotFoundError,
    RevokedReviewTokenError,
)
from .launcher import LAUNCH_TEMPLATE_VERSION, render_launch_script
from .genres import semantic_map_owed
from .metrics import review_metrics
from .notify import queue_operator_ping
from .render import render_service_tail, render_timeline
from .repository import (
    ResolvedReviewToken,
    add_deployment_boundary,
    advance_state,
    append_message,
    boundary_dict,
    create_review,
    get_messages,
    list_boundaries,
    resolve_review_token,
    round_gate_in_force,
)

# Long-poll tuning (spec §6, §15 knobs). HOLD_MAX must sit below the smallest client
# per-command timeout in use so a foreground GET returns cleanly (ordering HOLD_MAX <= W < T).
HOLD_MAX = 25.0
POLL_INTERVAL = 0.5

router = APIRouter(prefix="/reviews", tags=["review-orchestration"])

# Who the review is waiting on, by state — a console convenience (§3.3), derived not stored.
_WAITING_ON = {
    "created": "development",
    "artifact_ready": "critic",
    "critic_reviewing": "critic",
    "dev_disposing": "development",
    "converged": "development",
    "operator_gate": "operator",
    "returned": "development",
    "frozen": "development",  # dev is off fixing the base defect (external_defect FREEZE)
    # B.7: the operator stopped the review. `operator_finalizing` owes exactly one artifact
    # — the final version; `operator_finalized` owes the post-review Intent Summary, like a
    # converged review.
    "operator_finalizing": "development",
    "operator_finalized": "development",
    "finalized": None,
    "abandoned": None,
}

#: B.11 A-3 — who owes the next move, by the ROUND GATE's own position rather than by the
#: review's FSM state. The two coincide most of the time, which is why a state-derived
#: surface looked honest for so long; A-2's route makes them diverge deliberately, and one
#: measured incident (`bd787de7`, 2026-08-21) had both sides reading "the critic is slow"
#: for twenty minutes while the ball sat with development.
#:
#: `IDLE` and `TERMINAL` are absent on purpose: outside a round the gate has no opinion,
#: and the review's own state is then the better answer.
_GATE_OWNER = {
    round_gate.PASS_RUNNING: "critic",
    round_gate.PROPOSALS_OWED: "development",
    round_gate.PARKED: "operator",
    round_gate.UNPARKED: "development",
    round_gate.ROUND_CLOSING: "development",
    round_gate.OPERATOR_FINALIZING: "development",
}


def resolve_waiting_on(review: Review, messages: list | None) -> str | None:
    """The party that actually owes the next move (B.11 A-3, E-10).

    Without the channel this falls back to the state map, which is what the surface did
    everywhere before: a snapshot with no messages in hand is still allowed to answer, it
    just answers less precisely.

    Two things it resolves that the state alone cannot. First, a round whose findings have
    landed belongs to development even while the review's state still reads
    `critic_reviewing` — the round gate holds it, and after a semantic POST refusal (A-2)
    there is no concluding status coming at all. Second, a converged review that owes its
    semantic map is waiting on the CRITIC (E-10), which no review state expresses because
    `converged` has always meant "development owes the post-review summary".
    """
    if review.state in ("finalized", "abandoned"):
        return None
    if messages is not None:
        if _map_owed(review, messages):
            return "critic"
        if round_gate_in_force(review):
            owner = _GATE_OWNER.get(round_gate.compute(_as_wire(messages)).state)
            if owner is not None:
                return owner
    return _WAITING_ON.get(review.state)


def _map_owed(review: Review, messages: list) -> bool:
    """Does this converged review still owe the semantic map it declared? (B.11 E-10)

    IDEMPOTENCE COMES FROM THE CHANNEL, not from a marker: a `map` message naming this
    target commit makes the pass unnecessary, and there is nothing else to consult. The
    target commit is the converged version's own commit (E-9) — the branch commit that
    converged, never a merge result, because a merge result is a tree nobody reviewed.

    This is a SURFACE and a scheduling input, never an enforcement: no transition consults
    it and no message is refused because of it (G-7). A review with the role on can reach
    finalization with no map, and no component will notice — the operator will, by not
    receiving a document they were promised.
    """
    if review.state != "converged" or not semantic_map_owed(review.config):
        return False
    # ONE PREDICATE, ASKED HERE TOO. This site used to re-derive the target and match on
    # the commit alone — a third hand-rolled copy of a question the scheduler already
    # answers, and it drifted from the scheduler exactly as such copies do (critic finding
    # `map-identity-drops-base`). It now calls the same two functions the watcher calls.
    wire = _as_wire(messages)
    target = round_gate.map_target(wire)
    if target is None:
        return False
    return round_gate.map_seq_for(wire, target) is None


def _kind_of(m) -> str | None:
    return m.get("kind") if isinstance(m, dict) else m.kind


def _payload_of(m) -> dict:
    payload = m.get("payload") if isinstance(m, dict) else m.payload
    return payload or {}


def _as_wire(messages: list) -> list[dict]:
    """ORM rows or wire dicts, whichever the caller had — the gate fold reads dicts."""
    if messages and isinstance(messages[0], dict):
        return messages
    return [_msg(m) for m in messages]


# --- request bodies ------------------------------------------------------


class CreateReviewBody(BaseModel):
    slug: str
    mode: str
    artifact_ref: dict | None = None
    config: dict | None = None
    # B.9 D-1: the instrument block — host {hostname, username} plus, explicitly or via
    # the recorded default, critic {engine, model, effort} and development {engine,
    # model}, optionally self_check_waiver {granted_by, operator_quote}.
    instrument: dict | None = None


class ProfileBody(BaseModel):
    hostname: str
    username: str
    engine: str


class ProfileVersionBody(BaseModel):
    content: dict
    probe_evidence: dict


class ActivateBody(BaseModel):
    version_id: uuid.UUID


class ObservationBody(BaseModel):
    version_id: uuid.UUID
    kind: str
    observation: str
    observed_hostname: str
    observed_username: str
    evidence: dict | None = None


class BypassBody(BaseModel):
    trigger_text: str
    probe_ref: str | None = None


class BoundaryBody(BaseModel):
    # B.9 B-5: one deployment-scoped threat boundary, in the operator's own words.
    text: str


class ModelEntryBody(BaseModel):
    engine: str
    invocation_alias: str
    provider: str
    entry_class: str
    provider_model_id: str | None = None
    family: str | None = None
    effort_domain: list[str] | None = None
    verification: dict | None = None


class ModelEntryPatchBody(BaseModel):
    provider: str | None = None
    provider_model_id: str | None = None
    family: str | None = None
    effort_domain: list[str] | None = None
    entry_class: str | None = None
    verification: dict | None = None


class DefaultVersionBody(BaseModel):
    pair_map: dict
    operator_quote: str
    decision_ref: str


class AppendMessageBody(BaseModel):
    role: str
    kind: str
    payload: dict = Field(default_factory=dict)


class AdvanceStateBody(BaseModel):
    target: str


class PingBody(BaseModel):
    """One operator ping: which channel ROLE resolved to it, and what to say.

    `channel` is recorded, not interpreted — the role semantics (primary / fallback) and
    the once-only rule live in the watcher, which holds the state that justifies a ping.
    """

    channel: str = "prod_bot"
    text: str
    urgent: bool = False


# --- auth dependencies ---------------------------------------------------


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization[len("bearer ") :].strip()


async def require_bootstrap_principal(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Resolve the operator's bootstrap credential (any valid account credential, §8)."""
    try:
        return await resolve_credential(session, _bearer(authorization))
    except AuthError:
        raise HTTPException(status_code=401, detail="invalid or revoked credential") from None


async def require_review(
    review_id: uuid.UUID = Path(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ResolvedReviewToken:
    """Resolve a per-review token and confirm it authorizes THIS review (isolation, §8)."""
    try:
        resolved = await resolve_review_token(session, _bearer(authorization))
    except (InvalidReviewTokenError, RevokedReviewTokenError):
        raise HTTPException(status_code=401, detail="invalid or revoked review token") from None
    if resolved.review_id != review_id:
        raise HTTPException(status_code=403, detail="token does not authorize this review")
    return resolved


async def require_review_read(
    review_id: uuid.UUID = Path(...),
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> ResolvedReviewToken:
    """B.9 E-1: READ access = a live per-review token for THIS review, OR the deployment
    owner's bootstrap credential — any valid account credential, the same deployment-level
    "owner" as the C-4 management boundary (recorded: every deployment account belongs to
    the operator's own agents; a side effect the operator accepted knowingly is that
    other operator-owned accounts can read a closed journal).

    Finalization keeps revoking per-review tokens (a token is for a foreign participant
    process, and a closed review has no live participants) — this path is what keeps the
    closed journal REACHABLE, the E-2 pointer's precondition. Durable-but-unreachable
    rows read as "saved" and are the disease class this project keeps finding.
    """
    token = _bearer(authorization)
    try:
        resolved = await resolve_review_token(session, token)
    except (InvalidReviewTokenError, RevokedReviewTokenError):
        try:
            await resolve_credential(session, token)
        except AuthError:
            raise HTTPException(
                status_code=401,
                detail="invalid or revoked credential (neither a live review token nor "
                "a bootstrap credential)",
            ) from None
        return ResolvedReviewToken(review_id=review_id, role="owner")
    if resolved.review_id != review_id:
        raise HTTPException(status_code=403, detail="token does not authorize this review")
    return resolved


# --- serialization -------------------------------------------------------


def _msg(m) -> dict:
    return {
        "id": str(m.id),
        "seq": m.seq,
        "role": m.role,
        "kind": m.kind,
        "payload": m.payload,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _snapshot(r: Review, messages: list | None = None) -> dict:
    return {
        "review_id": str(r.id),
        "slug": r.slug,
        "mode": r.mode,
        # The review's own configuration, returned VERBATIM and never interpreted here. It
        # is accepted at creation and was stored but not readable back, so anything declared
        # in it — the genre, for one — could not reach the watcher that has to act on it.
        # Returning it keeps ONE carrier of that declaration; a launch flag beside it would
        # be a second copy of one fact, and two copies of one fact drift.
        "config": r.config,
        "state": r.state,
        "parked": r.parked,
        "iteration": r.iteration,
        "waiting_on": resolve_waiting_on(r, messages),
        "pending_human": r.parked,
        # B.12 H-6, ROUND 1 FINDING `b12-operator-projection-unbound-at-action`: the
        # projection audit had exactly one caller — the service tail — reachable through one
        # pull endpoint development fetches while WRITING THE POST-REVIEW SUMMARY. So an
        # operator item raised with no projection first became visible after every question
        # of the review had already been asked, which is too late for the only failure mode
        # the audit can actually prevent: honest forgetting. It rides the state snapshot
        # because that is the surface development polls every round by its own protocol.
        #
        # It is NOT a hold and NOT a refusal, deliberately. The server has no channel to the
        # operator, so conditioning any operation on a projection's existence would
        # guarantee a MESSAGE and never the delivery of an EXPLANATION — and conditioning
        # the one operation development does post (the relay of the operator's answer) would
        # be worse than the audit: by then the question has been asked, so the check cannot
        # prevent the harm, and it would reward back-filling a projection purely to unblock
        # the relay. That manufactures the very artifact whose absence the audit exists to
        # reveal, indistinguishably from a timely one.
        "projection_audit": (
            None if messages is None else _projection_audit_summary(messages)
        ),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    }


def _projection_audit_summary(messages: list) -> dict:
    """The audit's FAILURES, sized for a state poll rather than for the summary.

    The tail renders every item and its projection seqs; here the reader is a development
    session checking whose turn it is, so what travels is the verdict, the named failures,
    and how many operator items they were counted against. The failures are carried in full
    — a count alone would say "something is missing" without saying what, which is the shape
    that gets ignored.
    """
    audit = round_gate.projection_audit([_msg(m) for m in messages])
    return {
        "clean": audit["clean"],
        "operator_items": len(audit["items"]),
        "failures": audit["failures"],
    }


# --- endpoints -----------------------------------------------------------


@router.post("")
async def create_review_endpoint(
    body: CreateReviewBody,
    principal: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Create a review; return its id + the two per-review tokens (shown once, §8).

    B.9: creation runs the instrument gate (C-5/D-1/D-3/D-4). A refusal is a ROUTE — the
    422 detail names what is missing and the preparation path.
    """
    # B.11 E-3: the operator profile, resolved once at creation and frozen into the
    # review's config — the map is written to THIS reader, and to a snapshot of them that
    # cannot move under the review.
    scopes = None if principal.scopes is None else set(principal.scopes)
    operator_profile = await profile_service.snapshot_for_review(
        session, account_id=principal.account_id, scopes=scopes
    )
    try:
        issued = await create_review(
            session,
            slug=body.slug,
            mode=body.mode,
            artifact_ref=body.artifact_ref,
            config=body.config,
            instrument=body.instrument,
            operator_profile=operator_profile,
        )
    except ReviewCreationRefusedError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    await session.commit()
    return {
        "review_id": str(issued.review.id),
        "state": issued.review.state,
        "dev_token": issued.dev_token,
        "critic_token": issued.critic_token,
    }


@router.get("")
async def list_reviews_endpoint(
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read-only console list of active reviews (§3.3): state, iteration, waiting-on, pending-human."""
    rows = await session.scalars(
        select(Review)
        .where(Review.state.notin_(("finalized", "abandoned")))
        .order_by(Review.created_at.desc())
    )
    # B.11 A-3: `waiting_on` is resolved from the CHANNEL, not from the state alone, so
    # this list reads each active review's log. Active reviews only, and the same replay
    # the per-review metrics endpoint already does — the surface being right is worth one
    # query per open review, and it is the surface the operator reads when they wonder
    # why a review is taking so long.
    return {
        "reviews": [
            _snapshot(r, await get_messages(session, r.id, after=0)) for r in rows
        ]
    }


@router.get("/metrics")
async def aggregate_metrics_endpoint(
    limit: int = Query(default=50, ge=1, le=500),
    include_open: bool = Query(default=True),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The §6 counters across reviews — including CLOSED ones (B.6 M-1).

    Declared before ``GET /{review_id}`` deliberately: that route parses its segment as a
    UUID, so a ``/metrics`` request would otherwise be matched by it and rejected as a
    malformed id.

    Reading closed reviews is the whole point. Counters 2-5 are computable from the channel
    as it already exists, so the baseline for them is drawn RETROACTIVELY from past review
    channels rather than from a staged measurement window. Counter 1 cannot be: it reads
    coverage reports, which begin existing only with this change — so it will read empty
    here for every historical review, and that is a true answer, not a gap to paper over.
    """
    query = select(Review).order_by(Review.created_at.desc()).limit(limit)
    if not include_open:
        query = query.where(Review.state.in_(("finalized", "abandoned")))
    reviews = (await session.scalars(query)).all()
    out = []
    for review in reviews:
        messages = await get_messages(session, review.id, after=0)
        out.append(
            {
                **_snapshot(review, messages),
                "metrics": review_metrics([_msg(m) for m in messages]),
            }
        )
    return {"reviews": out}


# --- B.9 C/D management surface (bootstrap credential — the C-4 boundary) ------------
#
# Usage ("give me the launcher for review X") authorizes by the existing per-review
# token; management ("store a profile version", "move the pointer", "change the
# default") is an owner action under the deployment bootstrap credential. A single
# review's token must never be able to change how all future reviews launch. Declared
# BEFORE the ``/{review_id}`` routes so the static prefixes are not parsed as review ids.


@router.get("/profiles")
async def list_profiles_endpoint(
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"profiles": await instruments.list_profiles(session)}


@router.post("/profiles")
async def upsert_profile_endpoint(
    body: ProfileBody,
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        profile = await instruments.upsert_profile(
            session, hostname=body.hostname, username=body.username, engine=body.engine
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return instruments.profile_key_dict(profile)


@router.post("/profiles/{profile_id}/versions")
async def add_profile_version_endpoint(
    body: ProfileVersionBody,
    profile_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Store a proven, immutable version and move the active pointer to it (C-2/C-6).

    Refused without complete passing probe evidence from the profile's own host pair,
    with non-absolute paths, or (codex) with a sandbox form not pinned read-only (D-6).
    """
    try:
        version = await instruments.add_profile_version(
            session,
            profile_id=profile_id,
            content=body.content,
            probe_evidence=body.probe_evidence,
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return {"id": str(version.id), "version": version.version}


@router.post("/profiles/{profile_id}/activate")
async def activate_profile_version_endpoint(
    body: ActivateBody,
    profile_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Rollback = move the pointer to a prior proven version; a devalued one is refused."""
    try:
        profile = await instruments.activate_profile_version(
            session, profile_id=profile_id, version_id=body.version_id
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return instruments.profile_key_dict(profile)


@router.post("/profiles/{profile_id}/observations")
async def record_observation_endpoint(
    body: ObservationBody,
    profile_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The durable devaluation / mislaunch record (C-6) — posted by the development agent
    session under the deployment credential; scripts and leaves post nothing themselves."""
    try:
        row = await instruments.record_profile_observation(
            session,
            profile_id=profile_id,
            version_id=body.version_id,
            kind=body.kind,
            observation=body.observation,
            observed_hostname=body.observed_hostname,
            observed_username=body.observed_username,
            evidence=body.evidence,
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return {"id": str(row.id), "kind": row.kind, "observation": row.observation}


@router.get("/profiles/{profile_id}/bypasses")
async def list_bypasses_endpoint(
    profile_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await instruments.open_bypasses(session, profile_id)
    return {
        "bypasses": [
            {
                "id": str(b.id),
                "probe_ref": b.probe_ref,
                "trigger_text": b.trigger_text,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in rows
        ]
    }


@router.post("/profiles/{profile_id}/bypasses")
async def add_bypass_endpoint(
    body: BypassBody,
    profile_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        row = await instruments.add_bypass(
            session, profile_id=profile_id, probe_ref=body.probe_ref, trigger_text=body.trigger_text
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return {"id": str(row.id)}


@router.post("/bypasses/{bypass_id}/close")
async def close_bypass_endpoint(
    bypass_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        row = await instruments.close_bypass(session, bypass_id=bypass_id)
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return {"id": str(row.id), "removed_at": row.removed_at.isoformat() if row.removed_at else None}


@router.get("/models")
async def list_models_endpoint(
    engine: str | None = Query(default=None),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = await instruments.list_engine_models(session, engine)
    return {"models": [instruments.model_entry_dict(r) for r in rows]}


@router.post("/models")
async def add_model_endpoint(
    body: ModelEntryBody,
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        row = await instruments.add_engine_model(
            session,
            engine=body.engine,
            invocation_alias=body.invocation_alias,
            provider=body.provider,
            entry_class=body.entry_class,
            provider_model_id=body.provider_model_id,
            family=body.family,
            effort_domain=body.effort_domain,
            verification=body.verification,
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return instruments.model_entry_dict(row)


@router.patch("/models/{model_id}")
async def patch_model_endpoint(
    body: ModelEntryPatchBody,
    model_id: uuid.UUID = Path(...),
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    fields = body.model_dump(exclude_unset=True)
    try:
        row = await instruments.update_engine_model(session, model_id=model_id, **fields)
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return instruments.model_entry_dict(row)


@router.get("/instrument-default")
async def get_default_endpoint(
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"default": await instruments.get_default(session)}


@router.post("/instrument-default/versions")
async def add_default_version_endpoint(
    body: DefaultVersionBody,
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Change the default (D-5): refused without the operator's verbatim quote, with a
    non-resolving graph-Decision ref, or with an override key unknown to the genre
    registry. Read once at review creation; changing it never touches running reviews."""
    try:
        row = await instruments.add_default_version(
            session,
            pair_map=body.pair_map,
            operator_quote=body.operator_quote,
            decision_ref=body.decision_ref,
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return {"id": str(row.id), "version": row.version}


@router.post("/instrument-default/activate")
async def activate_default_version_endpoint(
    body: ActivateBody,
    _: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        parent = await instruments.activate_default_version(session, version_id=body.version_id)
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return {"active_version_id": str(parent.active_version_id)}


@router.get("/boundaries")
async def list_boundaries_endpoint(
    principal: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The whole threat-boundary registry (B-5) — a MANAGEMENT read: the pre-review gate
    and the fetch leaves read the deployment's recorded shape from here."""
    rows = await list_boundaries(session)
    return {"boundaries": [boundary_dict(b) for b in rows]}


@router.post("/boundaries")
async def add_boundary_endpoint(
    body: BoundaryBody,
    principal: Principal = Depends(require_bootstrap_principal),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record one deployment-scoped boundary (B-5) — the management write path, an
    operator act carrying their words. The OTHER write path is the `records_boundary`
    marker on a relayed in-review operator message (review scope, minted by the server
    inside the channel append); promoting a review-scoped entry to deployment level is
    an explicit new entry here, never automatic."""
    try:
        boundary = await add_deployment_boundary(session, text=body.text)
    except InvalidMessagePayloadError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    await session.commit()
    return boundary_dict(boundary)


@router.get("/{review_id}/threat-frame")
async def threat_frame_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """THIS review's standing threat frame (B-5): the operator-confirmed POSITIVE
    context — threat model and operating scale from the review's latest validated
    `threat_context` notice (LATEST BINDS; the operator may re-state mid-review) —
    plus the boundary exclusions: deployment entries and the entries minted from this
    review's own operator rulings. A USAGE read — the watcher renders the frame into
    the resident core (A-3) holding nothing but its review token. Absent context is
    served as nulls, never dressed as an empty-but-fine model (finding
    b9-standing-threat-frame-omits-positive-model)."""
    rows = await list_boundaries(session, review_id=resolved.review_id)
    review = await session.get(Review, resolved.review_id)
    # The initial record is frozen at creation (instrument.threat_context); a
    # latest-binds `threat_context` notice overrides it — keep overwriting.
    context: dict = (
        ((review.config or {}).get("instrument") or {}).get("threat_context") or {}
        if review is not None
        else {}
    )
    # The context IDENTITY is server-issued: the seq of the latest threat_context
    # record (0 while only the creation snapshot binds). A pass stamps it into its
    # findings/status, and the server refuses stale evidence (finding
    # b9-threat-context-freshness-not-pass-bound).
    context_seq = 0
    for m in await get_messages(session, resolved.review_id, after=0):
        if m.kind == "notice" and (m.payload or {}).get("phase") == round_gate.THREAT_CONTEXT_PHASE:
            context = m.payload or {}
            context_seq = m.seq
    # B.14 D-3: the register of declared known-temporary states travels beside the
    # frame — the same usage read. Frozen at creation with no restatement channel,
    # so the creation snapshot is its only source.
    temporary_states = (
        ((review.config or {}).get("instrument") or {}).get("temporary_states") or []
        if review is not None
        else []
    )
    return {
        "threat_model": context.get("threat_model"),
        "operating_scale": context.get("operating_scale"),
        "granted_by": context.get("granted_by"),
        "context_seq": context_seq,
        "boundaries": [boundary_dict(b) for b in rows],
        "temporary_states": temporary_states,
    }


@router.get("/{review_id}/cycle-documents")
async def cycle_documents_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """THIS review's development cycle and its documents, read from the anchor (B.12 A-7).

    A USAGE read, like the threat frame and the profile bypasses beside it: the watcher
    holds nothing but a review token, and the reconciliation pass runs inside a sandbox
    with no access to the graph at all. So the documents travel to the pass the only way
    they can — IN ITS PROMPT — and this is where the watcher fetches them.

    The full TEXT of each document is served, not a path. That is the arrangement A-3
    settled and it is what makes the pass possible: the canon holds that the graph carries
    substance rather than a pointer to it, a path is unreadable to anyone without the
    repository, and the party that must read these documents has no repository.

    Serves ``{"anchor": null, "documents": []}`` when the review names no cycle — a review
    with no map role, an audience review, or any review created before this slice. An
    unresolvable anchor is a 422 rather than an empty set: an empty set would let a
    reconciliation report "nothing to compare" over a comparison it never made, which is
    exactly the shape this element removed from B.11's declaration.
    """
    review = await session.get(Review, resolved.review_id)
    anchor_ref = round_gate.cycle_anchor_of(review.config if review else None)
    if anchor_ref is None:
        return {"anchor": None, "documents": []}
    try:
        return await cycle.resolve_cycle_documents(session, anchor_ref)
    except cycle.CycleAnchorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/{review_id}/profile-bypasses")
async def profile_bypasses_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Open bypasses of THIS review's frozen profile — a USAGE read (per-review token).

    The C-7 flip notice must name the bypass records hanging on a recovered probe
    (round 4, finding probe-flip-omits-bypass-identities); the watcher holds only a
    review token, so this read path exists at usage scope. It reads, never changes —
    the C-4 management boundary (a review token must not change how future reviews
    launch) is untouched, and the scope is the review's own frozen profile only.
    """
    review = await session.get(Review, resolved.review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    snapshot = ((review.config or {}).get("instrument")) or {}
    profile_id = snapshot.get("profile_id")
    if not profile_id:
        return {"bypasses": []}  # pre-B.9 review: no frozen profile, nothing to read
    rows = await instruments.open_bypasses(session, uuid.UUID(profile_id))
    return {
        "bypasses": [
            {"id": str(b.id), "probe_ref": b.probe_ref, "trigger_text": b.trigger_text}
            for b in rows
        ]
    }


@router.get("/{review_id}/launcher")
async def launcher_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The rendered launch script for this review (C-3/C-9), usage entry: per-review
    token, no management rights. Returns ``{script, hash, filename}`` — the client
    copies the script VERBATIM, stores the hash as a sidecar, verifies, runs. The render
    is deterministic; re-requesting reproduces the same bytes bit-for-bit."""
    review = await session.get(Review, resolved.review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    snapshot = ((review.config or {}).get("instrument")) or {}
    if not snapshot:
        raise HTTPException(
            status_code=409,
            detail="this review predates B.9 and carries no frozen instrument snapshot — "
            "no server render exists for it",
        )
    version = await session.get(
        LaunchProfileVersion, uuid.UUID(snapshot["profile_version_id"])
    )
    if version is None:
        raise HTTPException(status_code=409, detail="the frozen profile version is gone")
    messages = await get_messages(session, resolved.review_id, after=0)
    resolved_ping = round_gate.resolved_config(
        [{"seq": m.seq, "role": m.role, "kind": m.kind, "payload": m.payload or {}} for m in messages]
    )["ping"]
    try:
        script, digest = render_launch_script(
            review_id=str(review.id),
            snapshot=snapshot,
            profile_key={
                "hostname": snapshot["host"]["hostname"],
                "username": snapshot["host"]["username"],
                "engine": snapshot["critic"]["engine"],
            },
            version_id=str(version.id),
            version_n=version.version,
            content=version.content,
            resolved_ping=resolved_ping,
            # B.12 D-2: the proven tool versions ride into the script's header, so a
            # running launch names the versions it was proven on.
            probe_evidence=version.probe_evidence,
        )
    except InstrumentValidationError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    ext = "cmd" if version.content.get("shell") == "cmd" else "sh"
    return {
        "script": script,
        "hash": digest,
        "filename": f"launch_review_{str(review.id)[:8]}.{ext}",
        "template_version": LAUNCH_TEMPLATE_VERSION,
        "profile_version_id": str(version.id),
        # B.9 self-hosting anchor (finding b9-self-hosting-launch-procedure-still-
        # unanchored, operator-amended): the service names the git commit its image was
        # built from (baked at image build; None on images built without the arg). It
        # rides BESIDE the script, never inside its bytes — the C-9 deterministic-render
        # contract stays a pure function of (profile version, template, snapshot). A
        # self-hosting launch pins its worktree at exactly this commit; absence falls
        # back to the deployment's process boundary, then to the operator naming the ref.
        "service_commit": os.environ.get("AM_GIT_COMMIT") or None,
    }


@router.post("/{review_id}/messages")
async def append_message_endpoint(
    body: AppendMessageBody,
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Append a message. A MALFORMED ``status=converged`` (wrong state / stale version / no
    clean pass) 409s; a converged blocked by an unsettled ledger is recorded + routed (Part K).

    B.8 Part A: a coverage report or findings batch with row/item-level defects is accepted
    IN PART — the response then carries ``rejected_rows`` / ``rejected_items`` (each with its
    reason) and ``normalised_rows`` (unique-prefix row ids, ``{reported, accepted}``). 422 is
    reserved for payload-level malformation and for messages whose rows/items all fail.
    """
    try:
        msg = await append_message(
            session,
            review_id=resolved.review_id,
            role=body.role,
            kind=body.kind,
            payload=body.payload,
        )
    except ConvergenceRefusedError as e:
        raise HTTPException(status_code=409, detail=e.reason) from None
    except InvalidMessagePayloadError as e:
        raise HTTPException(status_code=422, detail=e.reason) from None
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="review not found") from None
    # Read the plain-attribute echo BEFORE commit/refresh: refresh re-loads column state,
    # and the acceptance report is a property of this post, not a column.
    partial = getattr(msg, "partial_acceptance", None)
    await session.commit()
    await session.refresh(msg)  # populate the DB-side created_at for the response
    out = _msg(msg)
    if partial:
        out.update(partial)
    return out


@router.get("/{review_id}/messages")
async def poll_messages_endpoint(
    after: int = Query(default=0),
    wait: float = Query(default=0.0),
    resolved: ResolvedReviewToken = Depends(require_review_read),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Long-poll (§6.1): messages with ``seq > after``; else hold up to ``min(wait, HOLD_MAX)``.

    A waiting agent has nothing else to do for this review, so blocking costs ~nothing.
    Under READ COMMITTED each re-query sees newly committed rows without resetting the
    session. (Phase-2 optimization: LISTEN/NOTIFY to drop the idle hold, like the bot.)
    """
    deadline = time.monotonic() + max(0.0, min(wait, HOLD_MAX))
    while True:
        msgs = await get_messages(session, resolved.review_id, after=after)
        if msgs or time.monotonic() >= deadline:
            return {"messages": [_msg(m) for m in msgs]}
        await asyncio.sleep(POLL_INTERVAL)


@router.post("/{review_id}/state")
async def advance_state_endpoint(
    body: AdvanceStateBody,
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Advance a lifecycle/gate transition (§5); 409 on an illegal jump."""
    try:
        review = await advance_state(session, resolved.review_id, body.target)
    except InvalidStateTransitionError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except ReviewNotFoundError:
        raise HTTPException(status_code=404, detail="review not found") from None
    await session.commit()
    return _snapshot(review, await get_messages(session, review.id, after=0))


@router.get("/{review_id}/timeline")
async def timeline_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review_read),
    session: AsyncSession = Depends(get_session),
) -> PlainTextResponse:
    """The human-readable timeline, rendered on demand from the store (§9.1) — no parallel
    log. The operator reads this when a chat-surfaced item looks suspicious."""
    review = await session.get(Review, resolved.review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    messages = await get_messages(session, resolved.review_id, after=0)
    markdown = render_timeline(
        [_msg(m) for m in messages], header=f"Review {review.slug} ({review.state})"
    )
    return PlainTextResponse(markdown, media_type="text/markdown")


@router.post("/{review_id}/ping")
async def ping_endpoint(
    body: PingBody,
    resolved: ResolvedReviewToken = Depends(require_review),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reach the operator on behalf of a review (B.7 G-7 / H-2 / H-4).

    The watcher and its stdlib supervisor hold nothing but a per-review token, so the
    transport lives on this side of the wire: they name a channel role and a sentence, and
    the service turns it into a real delivery through the path that already carries the
    operator's quiet hours and its own retry.

    A queued ping is reported as queued — never as read. The watcher journals the outcome
    of every send precisely because "the ping fired" and "the operator saw it" are
    different facts.
    """
    try:
        queued = await queue_operator_ping(
            session,
            review_id=resolved.review_id,
            channel=body.channel,
            text=body.text,
            urgent=body.urgent,
        )
    except Exception as e:  # a ping must never take the loop down with it
        raise HTTPException(status_code=503, detail=f"could not queue the ping: {e}") from None
    await session.commit()
    return {"queued": queued, "channel": body.channel}


@router.get("/{review_id}/service_tail")
async def service_tail_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review_read),
    session: AsyncSession = Depends(get_session),
) -> PlainTextResponse:
    """The post-review summary's bookkeeping half, computed from the journal (B.7 F-4).

    Development copies this VERBATIM into the summary rather than authoring it: a computed
    ledger needs no faithfulness audit, which shortens the audit cycle to the subject
    description alone — and the ledger is the proof that no finding was dropped, which is
    the claim least safe to leave to the recollection of the party the summary gates.
    """
    review = await session.get(Review, resolved.review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    messages = await get_messages(session, resolved.review_id, after=0)
    return PlainTextResponse(
        render_service_tail([_msg(m) for m in messages], state=review.state),
        media_type="text/markdown",
    )


@router.get("/{review_id}/metrics")
async def review_metrics_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review_read),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The §6 counters for one review, computed from its log (B.6 M-1).

    Read-time, not stored: the message log already holds everything these counters need, and
    a stored counter is one more thing that can drift from the timeline it claims to
    summarize.
    """
    review = await session.get(Review, resolved.review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    messages = await get_messages(session, resolved.review_id, after=0)
    return {
        **_snapshot(review, messages),
        "metrics": review_metrics([_msg(m) for m in messages]),
    }


@router.get("/{review_id}")
async def get_review_endpoint(
    resolved: ResolvedReviewToken = Depends(require_review_read),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Status snapshot for one review (§3.2)."""
    review = await session.get(Review, resolved.review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return _snapshot(review, await get_messages(session, resolved.review_id, after=0))
