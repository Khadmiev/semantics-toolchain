# SPDX-License-Identifier: Apache-2.0
"""Review-orchestration core protocol (spec docs/design/2026-07-07_review_orchestration_spec.md).

The load-bearing logic behind the HTTP API: create a review + mint its two
per-review isolation tokens (§8); append messages with a monotonic per-review
`seq` (the long-poll cursor, §3.2/§6); drive the message-triggered lifecycle
transitions (§5); and — the ONE server-enforced guard — validate a `converged`
declaration for *consistency* before it can be recorded (§10, INV-2/INV-4).

Same transaction contract as repository/graph.py and auth/service.py: functions
`flush` but never `commit`; the caller (the route / session dependency) owns the
transaction. Token plaintext is returned once at creation; only its sha256 persists.

Role is NOT an authorization input here (option X, §8): a token's only job is
isolation (it resolves to exactly one `review_id`). Who may post which `kind` is
prompt-maintained on cooperative agents. The convergence guard is enforced for
consistency regardless of who posted it.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.tokens import generate_token, hash_token
from . import coverage, cycle, instruments, round_gate
from .genres import (
    AUDIENCE_GENRE,
    coverage_in_play,
    freeze_role_settings,
    genre_of,
)
from ..config import settings
from ..models.review import (
    REVIEW_MODES,
    Review,
    ReviewMessage,
    ReviewToken,
    ThreatBoundary,
)
from .errors import (
    ConvergenceRefusedError,
    InvalidMessagePayloadError,
    InvalidReviewTokenError,
    InvalidStateTransitionError,
    ReviewCreationRefusedError,
    ReviewNotFoundError,
    RevokedReviewTokenError,
)

# Lifecycle transitions NOT driven by a specific message kind — operator/critic
# lifecycle actions, applied via advance_state (spec §5). Message-driven loop
# transitions (artifact/status/escalation) are applied inside append_message.
# `abandoned` is reachable from every non-terminal state (operator ruling 2026-07-08):
# a mistaken/obsolete/stuck review must be closable, revoking its tokens.
_LIFECYCLE_TRANSITIONS: dict[str, set[str]] = {
    "created": {"abandoned"},
    "artifact_ready": {"critic_reviewing", "abandoned"},  # critic picks the artifact up
    "critic_reviewing": {"abandoned"},
    "dev_disposing": {"abandoned"},
    "converged": {"operator_gate", "abandoned"},  # dev produces the Post-review Intent Summary
    "operator_gate": {"finalized", "returned", "abandoned"},  # operator accepts / returns
    "returned": {"artifact_ready", "abandoned"},  # returned change re-enters the loop
    # B.7 F-1: the operator's own stop is a FIRST-CLASS terminal status, distinct from
    # `converged` — "the critic declared convergence" and "the operator stopped the review"
    # are different claims, and collapsing them recreates the measured disease of weak
    # checks recorded in strong words. From here the review takes the same road as a
    # converged one: development writes the post-review Intent Summary (audited, F-4) and
    # the operator's gate finalizes it.
    "operator_finalized": {"operator_gate", "abandoned"},
    # The one state where the after-fixes final artifact is accepted; it leaves only by
    # that artifact arriving (server-driven, W-3) or by abandonment.
    "operator_finalizing": {"abandoned"},
    # external_defect FREEZE (J.3): a frozen review is RESUMED by posting a new artifact
    # (base fix = new version, J.8.5, handled in _apply_message_transition) — NOT via a
    # lifecycle jump, which would strand a convergeable ledger in dev_disposing with nothing
    # to post and would reuse the stale pre-freeze clean pass. Only `abandoned` is a lifecycle
    # exit from frozen.
    "frozen": {"abandoned"},
}


#: The protocol a review created by this code runs (B.7, the round gate). Stamped into the
#: review's config at creation — see `create_review` for why the rollout marker lives there
#: rather than in a timestamp comparison. Defined once, in the module both the server and
#: the watcher read it from.
PROTOCOL_B7 = round_gate.PROTOCOL

#: B.10 A-4 — the artifact-contract generation. The SAME rollout device as the protocol
#: stamp (authoritative data written into the review's config at creation, never a clock
#: or deploy-history comparison), but a SIBLING key: `config.protocol` is untouched,
#: because `round_gate.in_force` is a literal equality on it and creation's reproduction
#: predicate reads it — writing a new value there would strip new reviews of the round
#: gate and the instrument freeze (finding b10-protocol-stamp-preservation-undefined).
ARTIFACT_CONTRACT_B10 = "B.10"
ARTIFACT_CONTRACT_LEGACY = "legacy"


def artifact_contract_current(config: dict | None) -> bool:
    """Does the B.10 referential artifact contract bind this review?

    Keyed on the sibling stamp ONLY: a review stamped with the current contract
    generation refuses inline code diffs and validates every ref-shaped field in the
    strict full-40-hex form (A-1). A review without the stamp — every pre-B.10
    review — keeps its contract by construction, for artifacts AND manifests alike
    (finding b10-legacy-manifest-ref-validation-gate): an open legacy review is still
    owed its next manifest in the form it has always posted.
    """
    return (config or {}).get("artifact_contract") == ARTIFACT_CONTRACT_B10


#: B.14 — the coverage-contract generation, the THIRD sibling stamp (round 10, finding
#: b14-legacy-contract-not-carried-through-critic-surfaces). Report-side validation
#: already keys on the manifest carrying a slicing (`_question_denominator`); what that
#: cannot answer is which contract a NEW manifest must be posted under — a pre-B.14
#: code review is still owed its next v1 place manifest for its next artifact version,
#: and blocks-presence cannot license that, because the very manifest being validated
#: is the thing whose form is in question.
COVERAGE_CONTRACT_B14 = "B.14"
COVERAGE_CONTRACT_LEGACY = "legacy"


def coverage_contract_current(config: dict | None) -> bool:
    """Does the B.14 question-denominator coverage contract bind this review?

    Keyed on the sibling stamp ONLY: a review stamped with the current generation
    posts block manifests (tool_version 2, a carried slicing). A review without the
    stamp — every pre-B.14 review — keeps its creation-time contract for manifests it
    has yet to POST, exactly as the artifact contract does for artifacts (finding
    b10-legacy-manifest-ref-validation-gate is the precedent).
    """
    return (config or {}).get("coverage_contract") == COVERAGE_CONTRACT_B14


def round_gate_in_force(review: Review) -> bool:
    """Does the B.7 round gate govern this review?

    Only for reviews stamped with the protocol at creation (R-1). An older review keeps the
    old round structure for its whole life: retrofitting the gate onto a channel whose
    findings were never proposed against would refuse the very messages it is owed.

    THE AUDIENCE GENRE IS OUT OF SCOPE (spec S-2, an explicit non-goal): it has its own
    gates, its own pass plan and its own convergence conditions, and layering a second gate
    over them was never designed. The exclusion is keyed on the genre rather than left to
    whoever creates the review to remember, because a forgotten `protocol` pin would have
    parked an audience review behind a mechanism nobody wrote for it.
    """
    return round_gate.in_force(review.config)


def _as_dicts(messages) -> list[dict]:
    """ORM messages -> the plain replay shape the pure round-gate core (and the watcher)
    read. One shape, one implementation of the gate, for both consumers."""
    return [
        {"seq": m.seq, "role": m.role, "kind": m.kind, "payload": m.payload or {}}
        for m in messages
    ]


# --- B.9 B-5: the threat-boundary registry ---------------------------------
#
# The durable source of the standing threat frame. Exactly two write paths exist by spec:
# the management endpoint (deployment scope, an operator act with their words) and the
# `records_boundary` marker on a relayed in-review operator message (review scope, minted
# inside append_message). A review's standing frame — and a waive's mechanical-route
# eligibility — is derived from scope: the deployment entries plus the entries of THIS
# review. Promotion of a review-scoped entry to deployment level is an explicit
# management act (a new deployment entry), never automatic.


def boundary_dict(b: ThreatBoundary) -> dict:
    return {
        "id": str(b.id),
        "text": b.text_,
        "source": b.source,
        "scope": b.scope,
        "review_id": str(b.review_id) if b.review_id else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


async def add_deployment_boundary(session: AsyncSession, *, text: str) -> ThreatBoundary:
    """Record one deployment-scoped boundary (the management write path, B-5)."""
    if not _is_nonempty_str(text):
        raise InvalidMessagePayloadError(
            "threat boundary: `text` must be a non-empty string — the boundary is the "
            "operator's recorded words, and an empty record answers nothing"
        )
    boundary = ThreatBoundary(text_=text, source="management", scope="deployment")
    session.add(boundary)
    await session.flush()
    return boundary


async def list_boundaries(
    session: AsyncSession, *, review_id: uuid.UUID | None = None
) -> list[ThreatBoundary]:
    """All registry entries (management view), or the entries ELIGIBLE for one review —
    deployment-scoped plus that review's own (the derived standing frame, B-5)."""
    query = select(ThreatBoundary).order_by(ThreatBoundary.created_at)
    if review_id is not None:
        query = query.where(
            (ThreatBoundary.scope == "deployment") | (ThreatBoundary.review_id == review_id)
        )
    return list(await session.scalars(query))


def _observation_state(messages: list[dict]) -> tuple[dict[str, int], set[str]]:
    """The G-3 observation ledger as the channel stands: `{observation_id: seq}` of every
    posted `dev_observation` notice, and the ids already answered by a critic pass's
    `observation_dispositions` block."""
    posted: dict[str, int] = {}
    answered: set[str] = set()
    for m in messages:
        payload = m.get("payload") or {}
        if (
            m.get("kind") == "notice"
            and payload.get("phase") == round_gate.DEV_OBSERVATION_PHASE
        ):
            oid = payload.get("observation_id")
            if _is_nonempty_str(oid):
                posted.setdefault(oid, m.get("seq") or 0)
        elif m.get("kind") == "status" and m.get("role") == "critic":
            for d in payload.get("observation_dispositions") or []:
                if isinstance(d, dict) and _is_nonempty_str(d.get("observation_id")):
                    answered.add(d["observation_id"])
    return posted, answered


async def _resolve_boundary_for_review(
    session: AsyncSession, ref, review_id: uuid.UUID
) -> bool:
    """Does `ref` resolve to a registry entry ELIGIBLE for this review (B-5 rule 3)?

    Resolution, not aptness (operator ruling, spec round 9): whether the boundary covers
    the waived threat is beyond a schema — that check is the critic's contest right. A
    malformed id resolves to nothing and simply leaves the entry judgment, exactly as a
    missing ref does.
    """
    try:
        boundary_id = uuid.UUID(str(ref))
    except (ValueError, AttributeError, TypeError):
        return False
    boundary = await session.get(ThreatBoundary, boundary_id)
    if boundary is None:
        return False
    return boundary.scope == "deployment" or boundary.review_id == review_id


@dataclass
class IssuedReview:
    """A freshly created review plus its two one-time plaintext tokens (§3.2).

    Both tokens are returned to development; development relays the critic token to
    the critic (the one manual handoff, §8). Neither plaintext is ever stored.
    """

    review: Review
    dev_token: str
    critic_token: str


@dataclass(frozen=True)
class ResolvedReviewToken:
    """What a per-review token resolves to: exactly one review, and a label-only role.

    `role` is audit/label only — NOT an authorization input in MVP (option X, §8).
    Isolation (one token -> one review_id) is the token's whole job.
    """

    review_id: uuid.UUID
    role: str


# --- creation & auth -----------------------------------------------------


async def create_review(
    session: AsyncSession,
    *,
    slug: str,
    mode: str,
    artifact_ref: dict | None = None,
    config: dict | None = None,
    instrument: dict | None = None,
    operator_profile: dict | None = None,
) -> IssuedReview:
    """Create a review and mint its dev + critic per-review tokens (spec §3.2, §8).

    Raises ``ValueError`` on an unknown ``mode`` (also DB-guarded by a CheckConstraint,
    but we fail early with a clean message). The review starts in ``created``.

    B.9 D-1: creation names the instruments. ``instrument`` carries the host pair and
    (explicitly, or resolved from the recorded default) the critic triple and the
    development instrument; ``freeze_instruments`` validates the ground (host-proven
    profile C-5, verified model entry + effort domain D-4, self-check waiver D-3) and
    the resulting snapshot is FROZEN into ``config["instrument"]`` — nothing changes it
    mid-review. Refusals are routes (``ReviewCreationRefusedError``), not dead ends.

    B.11 A-1/E-2/E-3: two role settings and the operator profile join the frozen block.
    ``coverage.in_play`` gives the coverage predicate a NAME and a value instead of
    letting three copies of it infer one from an empty channel; ``semantic_map`` declares
    the end-of-cycle map role the way the audience genre declares its reading roles; and
    the operator profile is resolved from the graph HERE, once, so the map is written to
    a reader the review cannot re-interpret halfway through.
    """
    if mode not in REVIEW_MODES:
        raise ValueError(f"unknown review mode {mode!r} (expected one of {REVIEW_MODES})")
    # Raises ValueError on a malformed declaration, which both creation paths already
    # surface as a 422/InvalidArgument: a setting the review will be FROZEN to is not a
    # thing to coerce quietly. Validated even for a reproduction that will not carry the
    # result — a caller who declared something malformed deserves to hear so, whatever the
    # review then does with it.
    roles = freeze_role_settings(config, mode)

    # B.9: the instrument gate (C-5/D-1/D-3/D-4) runs for every review created on the
    # CURRENT protocol. A review explicitly pinned to an OLDER protocol is the recorded
    # escape hatch for reproducing a pre-B.9 review's behaviour, and such a reproduction
    # may be created the way its original was — without an instrument snapshot; it then
    # carries none, and is not stamp-validated (the same R-1 rollout shape as the round
    # gate). Passing an instrument WITH an old pin still freezes it normally.
    pinned = (config or {}).get("protocol")
    reproducing_old = pinned is not None and pinned != PROTOCOL_B7
    frozen = (
        None
        if (reproducing_old and instrument is None)
        else await instruments.freeze_instruments(
            session, config=config, instrument=instrument, mode=mode
        )
    )

    # B.7 R-1 — THE ROLLOUT MARKER. The round gate applies to reviews created after
    # deploy; finished and in-flight reviews keep the old round structure. "After deploy"
    # is not something the server can read off a clock without guessing, so the review
    # STAMPS ITS OWN PROTOCOL at creation: a review created by this code carries
    # `protocol: B.7` and is gated, and every review predating it carries nothing and runs
    # exactly as before. A caller may pin another protocol explicitly — the escape hatch
    # for reproducing an old review's behaviour — and pinning is the only way to get one.
    # The frozen instrument snapshot lives beside the protocol stamp in the same config
    # carrier (its presence is the D-2 stamp-validation rollout marker).
    #
    # B.10 A-4 — the artifact-contract generation, a SIBLING stamp beside the untouched
    # protocol stamp. Legacy iff the existing reproduction predicate holds (an explicit
    # protocol pin different from the current constant) OR the creation request carries
    # an explicit legacy pin of the artifact contract itself (finding
    # b10-artifact-contract-pin-alias) — the second hatch exists because the protocol
    # constant did not change across B.8/B.9, so reproducing a pre-B.10 review whose
    # protocol was already the current constant is expressible no other way. Mirroring
    # the protocol device, ANY explicit contract pin different from the current constant
    # reads as that legacy hatch: pinning is the only way to get an old contract, and a
    # legacy contract admits the inline diff — reproduction means reproduction.
    contract_pin = (config or {}).get("artifact_contract")
    legacy_contract = reproducing_old or (
        contract_pin is not None and contract_pin != ARTIFACT_CONTRACT_B10
    )
    config = {
        **(config or {}),
        "protocol": pinned or PROTOCOL_B7,
        "artifact_contract": (
            ARTIFACT_CONTRACT_LEGACY if legacy_contract else ARTIFACT_CONTRACT_B10
        ),
        # B.14 rides the same reproduction predicate: an explicit legacy pin of the
        # artifact contract (or an old protocol pin) reproduces a pre-B.14 review,
        # whose coverage contract is then legacy too (round 10, finding
        # b14-legacy-contract-not-carried-through-critic-surfaces).
        "coverage_contract": (
            COVERAGE_CONTRACT_LEGACY if legacy_contract else COVERAGE_CONTRACT_B14
        ),
        # B.11 A-1/E-2 ride the SAME rollout hatch as the instrument gate below and the
        # B.10 artifact contract: a review explicitly pinned to an older protocol is
        # reproducing a pre-B.11 review's behaviour, and it must reproduce it exactly — no
        # frozen coverage setting (the predicate then falls back to the old inference from
        # message presence) and no map role. Stamping them would make a reproduction
        # behave like the thing it is trying to reproduce a change to.
        **({} if reproducing_old else roles),
    }
    if frozen is not None:
        config["instrument"] = frozen
    # E-3: absent when the caller could not resolve one (no account behind the
    # credential). A review is not refused over it — the map is then written to the
    # general register, and the config says so by carrying nothing.
    if operator_profile is not None:
        config["operator_profile"] = operator_profile
    # B.12 A-7: the development cycle this review belongs to, RESOLVED at creation rather
    # than trusted at use. A review names its cycle by the anchor's id, and the
    # reconciliation reads that cycle's documents from the graph instead of from a
    # declaration on the channel. Resolving here means a wrong id is a refusal at creation
    # — when a human is present and can fix it — rather than a refused reconciliation at
    # the far end of a converged review, where the single attempt is already being spent.
    # Absent is legal and common: a review with no map role, an audience review, and every
    # review created before this slice carry no anchor and behave exactly as they did.
    anchor_ref = cycle_anchor_of(config)
    if anchor_ref is not None:
        try:
            await cycle.resolve_cycle_documents(session, anchor_ref)
        except cycle.CycleAnchorError as exc:
            raise ReviewCreationRefusedError(str(exc)) from None
    review = Review(slug=slug, mode=mode, artifact_ref=artifact_ref, config=config)
    session.add(review)
    await session.flush()  # assign review.id before minting tokens against it

    dev_token = generate_token()
    critic_token = generate_token()
    session.add_all(
        [
            ReviewToken(review_id=review.id, role="development", token_hash=hash_token(dev_token)),
            ReviewToken(review_id=review.id, role="critic", token_hash=hash_token(critic_token)),
        ]
    )
    await session.flush()
    return IssuedReview(review=review, dev_token=dev_token, critic_token=critic_token)


async def resolve_review_token(session: AsyncSession, token: str) -> ResolvedReviewToken:
    """Resolve a per-review bearer token to its review (+ label role), or raise.

    Raises ``InvalidReviewTokenError`` (no such token) / ``RevokedReviewTokenError``
    (finalized/abandoned). This is the only auth check on the review data plane;
    it grants access to exactly one ``review_id`` (isolation, §8).
    """
    row = await session.scalar(
        select(ReviewToken).where(ReviewToken.token_hash == hash_token(token))
    )
    if row is None:
        raise InvalidReviewTokenError
    if row.revoked_at is not None:
        raise RevokedReviewTokenError
    return ResolvedReviewToken(review_id=row.review_id, role=row.role)


async def revoke_review_tokens(session: AsyncSession, review_id: uuid.UUID) -> None:
    """Revoke every live token of a review (on finalize/abandon, §8 lifetime)."""
    tokens = await session.scalars(
        select(ReviewToken).where(
            ReviewToken.review_id == review_id, ReviewToken.revoked_at.is_(None)
        )
    )
    now = datetime.now(UTC)
    for tok in tokens:
        tok.revoked_at = now
    await session.flush()


# --- messages & the message-driven loop ----------------------------------


async def append_message(
    session: AsyncSession,
    *,
    review_id: uuid.UUID,
    role: str,
    kind: str,
    payload: dict | None = None,
) -> ReviewMessage:
    """Append a message (append-only, §3.1) and apply any message-driven transition (§5).

    Serializes ``seq`` per review by locking the ``review`` row (``FOR UPDATE``) so two
    concurrent appends can't collide on the same ``seq`` (the unique constraint would
    otherwise reject one). A ``status=converged`` runs the server-side convergence check
    (§10, Part K): a MALFORMED declaration (wrong state, stale version, or no materialized
    clean pass) is REFUSED (``ConvergenceRefusedError``, 409, review left open); a
    declaration blocked only by an unsettled LEDGER (undisposed findings, open operator
    items, ungrafted external defects) is instead RECORDED and the review is ROUTED to a
    wakeable state, then self-completed when the blocker clears — never a 409.

    Raises ``ReviewNotFoundError`` if the review is gone.
    """
    payload = payload or {}
    # Append-size invariant: the channel is append-only and its consumers replay it, so one
    # oversized message bricks the review forever (live: review 809987bd, a ~2MB artifact —
    # abandoned and recreated with the timeline lost). Fail at the AUTHOR, at POST time,
    # like every other wire-contract violation.
    payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if payload_bytes > settings.review_max_message_bytes:
        raise InvalidMessagePayloadError(
            f"payload is {payload_bytes} bytes, over the channel limit of "
            f"{settings.review_max_message_bytes} — the channel is append-only and an "
            "oversized message would permanently break its replay. A code artifact IS "
            "a git reference (B.10: `artifact_ref` base+commit, full 40-hex — inline "
            "diffs are refused on current-contract reviews). A spec artifact whose "
            "document lives in this repository may post the referential subject "
            "(`artifact_ref` + path) instead of the inline bundle; a spec body that "
            "must stay inline has to fit the limit — split the spec, or the operator "
            "raises the configured limit."
        )
    # --- B.8 Part A: partial acceptance ----------------------------------------------
    #
    # Row/item-level defects in a coverage report or a findings batch no longer void the
    # whole message: the valid content is recorded, the invalid content is returned with
    # its reasons (`rejected_rows` / `rejected_items` / `normalised_rows` on the accepted
    # response). One empty field voided 291 good verdicts (2026-08-10); the party that
    # could fix the message is gone when the 422 arrives. 422 stays reserved for
    # payload-level malformation and for a message whose rows/items ALL fail.
    rejected_rows: list[dict] = []
    normalised_rows: list[dict] = []
    downgraded_rows: list[dict] = []
    rejected_items: list[dict] = []
    rows_posted = len(payload.get("rows") or []) if kind == "coverage_report" else 0
    items_posted = len(payload.get("items") or []) if kind == "findings" else 0

    # Fetched BEFORE payload validation: the artifact and manifest validators are keyed
    # on the review's artifact-contract stamp (B.10 A-4), so they need the config. The
    # FOR UPDATE lock is simply taken a step earlier; an unknown review now 404s before
    # payload validation, which is the honest precedence anyway.
    review = await session.get(Review, review_id, with_for_update=True)
    if review is None:
        raise ReviewNotFoundError

    if kind == "disposition":
        _validate_disposition_payload(payload)
    if kind == "findings":
        rejected_items += _validate_findings_payload(
            payload, register_ids=_temporary_register_ids(review.config)
        )
        if items_posted and not payload.get("items"):
            # A batch whose every item is malformed must not be recorded as a CLEAN pass —
            # an empty accepted list is indistinguishable from "no findings" downstream.
            raise InvalidMessagePayloadError(
                "malformed findings: every item failed validation — "
                + "; ".join(str(i.get("reason")) for i in rejected_items)
            )
    if kind == "escalation":
        _validate_escalation_payload(payload)
    if kind == "human_question":
        _validate_human_question_payload(payload)
    if kind == "external_defect_graph_result":
        _validate_external_defect_graph_result_payload(payload)
    if kind == "coverage_manifest":
        _validate_coverage_manifest_payload(payload, config=review.config)
    if kind == "coverage_report":
        rejected_rows += _validate_coverage_report_payload(payload)
    if kind == "decision_response":
        _validate_decision_response_payload(payload)
    if kind == "artifact":
        _validate_artifact_payload(payload, config=review.config)
    if kind == "status":
        _validate_status_payload(payload)
        if role == "critic":
            _validate_credited_temporary_entries(payload, config=review.config)
    if kind == "notice":
        _validate_notice_payload(payload)
    if kind == "waiver":
        _validate_waiver_payload(payload)
    if kind == "dissent":
        _validate_dissent_payload(payload)
    # The label check reads the table above; see it for what this is and is not.
    if kind in AUTHOR_BOUND_KINDS and role != AUTHOR_BOUND_KINDS[kind]:
        raise InvalidMessagePayloadError(
            f"a `{kind}` message is labelled {AUTHOR_BOUND_KINDS[kind]!r}, not {role!r} — "
            f"{_AUTHOR_BOUND_WHY[kind]}. This is a consistency check on the label, not an "
            "authorization check: roles are prompt-maintained here, and what it catches is "
            "a client posting under the wrong one"
        )
    if kind == "map":
        _validate_map_payload(payload)
    if kind == "map_disposition":
        _validate_map_disposition_payload(payload)
    if kind == "reconciliation":
        _validate_reconciliation_payload(payload)
    if kind == "operator_projection":
        _validate_operator_projection_payload(payload)

    # --- B.11 Part E/F: the references these kinds carry must RESOLVE ------------------
    #
    # Every reference in this slice points from a LATER message to an EARLIER one, and the
    # channel is the record: no counter, no stored state, nothing to migrate. That is only
    # true if the pointers resolve, which is what this block checks — and it is deliberately
    # the ONLY thing it checks. Nothing here enforces an ordering between the map, the
    # operator's ruling and the reconciliation (G-6/G-7): a disposition may precede a
    # reconciliation, a reconciliation may land after the operator has already decided, and
    # a review with the map role on may reach finalization with no map at all. What holds
    # that order is the operator's discipline, which is a recorded decision rather than an
    # oversight.
    if kind in ("map_disposition", "reconciliation") or (
        kind == "decision_response" and payload.get(RECONCILIATION_RETRY_KEY) is not None
    ):
        e_log = _as_dicts(await get_messages(session, review_id, after=0))
        _validate_map_references(kind, payload, e_log)
    # B.12 A-7: the cycle's documents are a GRAPH read, so this half is async and sits
    # beside the channel-side references rather than inside them.
    if kind == "reconciliation":
        await _validate_reconciliation_against_cycle(session, review, payload)

    # --- B.9 D-2: every critic pass stamps its instrument; equality, not presence -----
    #
    # The watcher stamps model + effort from the FROZEN snapshot into the pass's status
    # message, and the server validates the stamp for EQUALITY with the snapshot's critic
    # instrument — a non-empty foreign value would corrupt the very measurement the stamp
    # exists for (finding b9-status-stamp-not-snapshot-bound). Keyed on the snapshot's
    # presence: reviews created before B.9 carry no frozen instrument and are not
    # stamp-validated (the R-1 rollout shape).
    if kind == "status" and role == "critic":
        frozen_critic = ((review.config or {}).get("instrument") or {}).get("critic")
        if frozen_critic:
            expected = {"model": frozen_critic.get("model"), "effort": frozen_critic.get("effort")}
            stamped = {"model": payload.get("model"), "effort": payload.get("effort")}
            if stamped != expected:
                raise InvalidMessagePayloadError(
                    f"status stamp {stamped} does not EQUAL the review's frozen critic "
                    f"instrument {expected} (B.9 D-2) — the instrument does not change in "
                    "the middle of a measurement, and without a truthful stamp the next "
                    "process measurement is impossible"
                )

    # --- B-5 pass-bound context freshness (finding
    # b9-threat-context-freshness-not-pass-bound): channel append ORDER is a proxy that
    # lies exactly for bindings without mid-pass abandonment — a pass begun before a
    # restatement may post after it. So the freshness is bound to the pass's own
    # evidence: once the review's context identity is non-zero (a threat_context
    # record exists; its seq IS the identity, served by the threat-frame endpoint),
    # every critic findings/status must stamp `context_seq` equal to it — a pass that
    # judged under a superseded frame cannot post at all, loudly, for EVERY binding.
    # Administrative ledger-exempt statuses keep their always-valid shape.
    if (
        role == "critic"
        and kind in ("findings", "status")
        and not (
            kind == "status"
            and (payload.get("phase"), payload.get("value"))
            in round_gate.LEDGER_EXEMPT_STATUS_SHAPES
        )
    ):
        ctx_log = _as_dicts(await get_messages(session, review_id, after=0))
        context_identity = max(
            (
                m["seq"]
                for m in ctx_log
                if m["kind"] == "notice"
                and (m["payload"] or {}).get("phase") == round_gate.THREAT_CONTEXT_PHASE
            ),
            default=0,
        )
        if context_identity and payload.get("context_seq") != context_identity:
            raise InvalidMessagePayloadError(
                f"context_seq {payload.get('context_seq')!r} does not match the "
                f"review's current threat-context identity {context_identity} — a "
                "threat_context restatement postdates the frame this pass judged "
                "under; re-run the pass against the newest frame (the threat-frame "
                "endpoint serves `context_seq`) and stamp it into the pass's findings "
                "and status"
            )

    # --- B.9 G-3: the observation ledger — an answer is a ledger entry, not a courtesy
    #
    # Development's incidental discoveries land on the channel as `dev_observation`
    # notices; the critic's next PASS-ENDING status (no `phase` — administrative and
    # refusal statuses carry one) must answer every observation posted before the pass
    # began with a validated `observation_dispositions` block: adopted (naming a finding
    # emitted in the SAME pass) or dismissed (with a reason). Same shape as the
    # proposals-per-finding validation — every item covered, no extras, an omission
    # mechanically visible — now with referential integrity, not just counting.
    if kind == "notice" and payload.get("phase") == round_gate.DEV_OBSERVATION_PHASE:
        observation_problems = []
        if role != "development":
            observation_problems.append(
                f"a `dev_observation` is development's own record — role {role!r} may "
                "not post one (findings stay critic-owned, and the critic answers "
                "observations in its status, not by posting them)"
            )
        if not _is_nonempty_str(payload.get("observation_id")):
            observation_problems.append("`observation_id` must be a non-empty string")
        if not _is_nonempty_str(payload.get("text")):
            observation_problems.append(
                "`text` must be a non-empty string — an observation that records "
                "nothing answers nothing"
            )
        if not observation_problems:
            posted, _ = _observation_state(
                _as_dicts(await get_messages(session, review_id, after=0))
            )
            if payload["observation_id"] in posted:
                observation_problems.append(
                    f"observation_id {payload['observation_id']!r} is already on this "
                    "channel — each observation carries a distinct stable id"
                )
        if observation_problems:
            raise InvalidMessagePayloadError(
                "dev_observation: " + "; ".join(observation_problems)
            )

    # --- B-5, the positive half of the standing frame (finding
    # b9-standing-threat-frame-omits-positive-model): the operator-confirmed threat
    # model and operating scale ride a validated `threat_context` notice. LATEST BINDS —
    # the operator may re-state the context mid-review; the newest record is what the
    # frame endpoint serves, the channel keeps the history. The critic never authors
    # its own frame.
    if kind == "notice" and payload.get("phase") == round_gate.THREAT_CONTEXT_PHASE:
        context_problems = []
        if role == "critic":
            context_problems.append(
                "the threat context is the operator's confirmed word, relayed by "
                "development — the critic may not author its own frame"
            )
        # A restatement needs a REACHABLE fresh pass (finding
        # b9-frame-repass-not-a-real-pass-boundary): once the review is converged or
        # beyond, no state transition can carry that pass, so the record is refused
        # WITH A ROUTE instead of accepted into a promise nothing can keep — a
        # validated convergence is never silently un-declared by a notice.
        if review.state in (
            "converged", "operator_gate", "operator_finalized", "finalized", "abandoned",
        ):
            context_problems.append(
                f"the review is {review.state!r} — a context correction now travels "
                "through the operator gate: RETURN the review (which reopens it with a "
                "new artifact version) and relay the corrected context with that "
                "return; a restatement never silently un-declares a validated "
                "convergence"
            )
        for field in ("threat_model", "operating_scale", "granted_by"):
            if not _is_nonempty_str(payload.get(field)):
                context_problems.append(f"`{field}` must be a non-empty string")
        if context_problems:
            raise InvalidMessagePayloadError(
                "threat_context: " + "; ".join(context_problems)
            )

    if (
        kind == "status"
        and role == "critic"
        and (payload.get("phase"), payload.get("value"))
        not in round_gate.LEDGER_EXEMPT_STATUS_SHAPES
    ):
        # The exemption is an enumerated (phase, value) SHAPE, not key presence and not
        # phase alone: an invented phase — or a reserved phase claimed on an ordinary
        # verdict like needs_iteration — carries the full ledger burden (findings
        # b9-observation-ledger-escape-paths, -reserved-phase-still-bypasses). A
        # phase-less verdict stays covered because (None, value) is never in the set.
        log_for_observations = _as_dicts(await get_messages(session, review_id, after=0))
        posted, answered = _observation_state(log_for_observations)
        anchor = payload.get("artifact_seq")
        anchor_msg = next(
            (
                m
                for m in log_for_observations
                if m["kind"] == "artifact" and m["seq"] == anchor
            ),
            None,
        )
        # The audit pass over a post-review summary owes no observation answers — its
        # subject is the summary's faithfulness, not the artifact the observations
        # concern.
        is_summary_pass = bool(
            anchor_msg and (anchor_msg["payload"] or {}).get("intent_summary")
        )
        # PASS IDENTITY (findings b9-observation-ledger-lacks-pass-identity and
        # b9-pass-identity-fix-excludes-supported-bindings): with same-version repasses,
        # artifact_seq no longer bounds one pass — and the boundary must hold for EVERY
        # advertised binding, not only the watcher. The default boundary is therefore
        # SERVER-DERIVED from the channel itself: a pass's findings message is the
        # latest critic findings after the previous pass-ending critic status (exempt
        # administrative shapes end nothing). The watcher's `projection_through_seq`
        # stamp — the last channel seq its rendered projection contained, recorded by
        # construction — remains a PRECISION OVERRIDE, never a privilege. Named
        # residual for stampless bindings: an observation posted between a pass's start
        # and its findings post is demanded though unseen — that fails LOUD (this very
        # refusal), and the next attempt sees it. A direct post can fake the stamp: the
        # recorded single-user trust boundary (option X), as with every other stamp.
        stamp = payload.get("projection_through_seq")
        stamp = stamp if isinstance(stamp, int) and not isinstance(stamp, bool) else None
        last_pass_end_seq = max(
            (
                m["seq"]
                for m in log_for_observations
                if m["kind"] == "status"
                and m["role"] == "critic"
                and (
                    (m["payload"] or {}).get("phase"),
                    (m["payload"] or {}).get("value"),
                )
                not in round_gate.LEDGER_EXEMPT_STATUS_SHAPES
            ),
            default=0,
        )
        pass_findings_msg = next(
            (
                m
                for m in reversed(log_for_observations)
                if m["kind"] == "findings"
                and m["role"] == "critic"
                and (m["payload"] or {}).get("artifact_seq") == anchor
                and m["seq"] > last_pass_end_seq
            ),
            None,
        )
        if is_summary_pass:
            owed = set()
        elif stamp is not None:
            owed = {
                oid
                for oid, seq in posted.items()
                if seq <= stamp and oid not in answered
            }
        elif pass_findings_msg is not None:
            owed = {
                oid
                for oid, seq in posted.items()
                if seq < pass_findings_msg["seq"] and oid not in answered
            }
        else:
            # No findings message in this pass (a blocked/status-only pass): the
            # artifact-anchored semantics remain for exactly this edge.
            owed = {
                oid
                for oid, seq in posted.items()
                if anchor_msg is not None and seq < anchor_msg["seq"] and oid not in answered
            }
        block = payload.get("observation_dispositions")
        disposition_problems: list[str] = []
        seen_ids: list[str] = []
        if block is not None and not isinstance(block, list):
            disposition_problems.append("`observation_dispositions` must be a list")
            block = []
        same_pass_findings = {
            str(f.get("id"))
            for m in log_for_observations
            if m["kind"] == "findings"
            and m["role"] == "critic"
            and (m["payload"] or {}).get("artifact_seq") == anchor
            and (
                (m["payload"] or {}).get("projection_through_seq") == stamp
                if stamp is not None
                else (pass_findings_msg is None or m["seq"] == pass_findings_msg["seq"])
            )
            for f in (m["payload"] or {}).get("items") or []
            if isinstance(f, dict) and f.get("id")
        }
        for d in block or []:
            if not isinstance(d, dict):
                disposition_problems.append("each disposition must be an object")
                continue
            oid = d.get("observation_id")
            if not _is_nonempty_str(oid):
                disposition_problems.append(
                    "each disposition requires a non-empty `observation_id`"
                )
                continue
            seen_ids.append(oid)
            where = f"observation {oid!r}"
            if oid not in posted:
                disposition_problems.append(
                    f"{where}: no such observation on this channel"
                )
                continue
            if oid in answered:
                # B.11 A-5: A RE-ANSWER IS A NO-OP, NOT A KILLED PASS. This branch used to
                # refuse the whole concluding status, and it destroyed a complete pass of
                # this slice's own review (e3c796f7, artifact_seq 25): real findings and a
                # real coverage report had already landed, and the status was refused
                # solely because it re-answered an observation the previous pass had
                # closed. The watcher could not repair it either — its bounded one-shot
                # repair only handles documented refusal shapes and correctly declines to
                # author semantics it never observed.
                #
                # The strictness itself is what is wrong here, so the strictness is what
                # changes rather than the repairer being taught a new shape. A re-answer
                # asserts nothing false: it confirms what is already closed. Accepting and
                # ignoring it cannot mask a MISSING disposition, which is a separate check
                # below and stays exactly as strict — and an entry naming an observation
                # that does not exist on the channel is still refused, above.
                continue
            # NOT refused when outside `owed`: a pass MAY answer any posted, unanswered
            # observation — including one posted after its anchor artifact (finding
            # b9-late-observation-no-legal-cleanup-path: the old "belongs to the NEXT
            # pass" refusal, combined with the no-empty-repost rule, left a late
            # observation with no legal answering move at all). The OWED set — what the
            # pass MUST answer — stays anchored to its artifact, so nothing is demanded
            # blind; the model can only answer what its projection showed it anyway.
            action = d.get("action")
            if action not in round_gate.OBSERVATION_ACTIONS:
                disposition_problems.append(
                    f"{where}: `action` must be one of "
                    f"{list(round_gate.OBSERVATION_ACTIONS)}, got {action!r}"
                )
            elif action == "adopted":
                fid = d.get("finding_id")
                if not _is_nonempty_str(fid):
                    disposition_problems.append(
                        f"{where}: an `adopted` disposition requires `finding_id`"
                    )
                elif fid not in same_pass_findings:
                    disposition_problems.append(
                        f"{where}: `finding_id` {fid!r} names no finding emitted in THIS "
                        "pass's findings message — adoption means the critic raised it "
                        "as its own, so the id must resolve within the same pass"
                    )
            elif action == "dismissed" and not _is_nonempty_str(d.get("reason")):
                disposition_problems.append(
                    f"{where}: a `dismissed` disposition requires a `reason`"
                )
        duplicates = sorted({i for i in seen_ids if seen_ids.count(i) > 1})
        if duplicates:
            disposition_problems.append(f"duplicate dispositions for {duplicates}")
        missing = sorted(owed - set(seen_ids))
        if missing:
            disposition_problems.append(
                f"observations posted before this pass began with no disposition: "
                f"{missing} — the server refuses a pass-ending status that leaves the "
                "observation ledger open (B.9 G-3)"
            )
        if disposition_problems:
            raise InvalidMessagePayloadError(
                "observation_dispositions: " + "; ".join(disposition_problems)
            )

    # --- B.9 B-5: the registry's two write paths, held at the wire --------------------
    #
    # An in-review operator ruling becomes a registry entry when the relayed operator
    # message carries the typed `records_boundary {text}` marker: the server validates
    # the marker, MINTS the review-scoped entry and stamps its id back into the stored
    # payload — the channel itself then carries the id the ruling is citable by. The
    # marker is the operator's: any other role carrying it is refused, and a pre-set
    # `boundary_id` is refused (the id is the server's receipt, never a client claim).
    if "records_boundary" in payload:
        marker = payload.get("records_boundary")
        boundary_problems = []
        if role != "operator":
            boundary_problems.append(
                f"`records_boundary` records an OPERATOR ruling — role {role!r} may not "
                "carry it (development relays the ruling as an operator message)"
            )
        if not isinstance(marker, dict) or not _is_nonempty_str(marker.get("text")):
            boundary_problems.append(
                "`records_boundary` must be {text: <the boundary, in the operator's "
                "words>}"
            )
        elif "boundary_id" in marker:
            boundary_problems.append(
                "`records_boundary.boundary_id` is the server's receipt for the minted "
                "entry — a client never sets it"
            )
        if boundary_problems:
            raise InvalidMessagePayloadError(
                "records_boundary: " + "; ".join(boundary_problems)
            )
        minted = ThreatBoundary(
            text_=marker["text"],
            source="review_ruling",
            scope="review",
            review_id=review_id,
        )
        session.add(minted)
        await session.flush()
        payload["records_boundary"] = {**marker, "boundary_id": str(minted.id)}

    # --- B.8 F-3: the operator's degraded-mode carrier, validated at POST -------------
    #
    # A failed ground probe with a live override launches the pass DEGRADED (grounds
    # delivered inline); a failed probe with no override is F-1's environmental refusal —
    # the two paths are exhaustive and the default is refusal. The override is an
    # OPERATOR-authored notice, names one ground the review kind actually declares (any
    # other is refused — an override of an undeclared ground would be consent to
    # nothing), carries its reason, binds until the review ends, and is recorded like
    # any waiver.
    if kind == "notice" and payload.get("phase") == round_gate.GROUNDS_OVERRIDE_PHASE:
        override_problems = []
        if role != "operator":
            override_problems.append(
                "a `grounds_override` is the OPERATOR's recorded permission to run "
                f"degraded — role {role!r} may not grant it"
            )
        if not _is_nonempty_str(payload.get("reason")):
            override_problems.append("`grounds_override` requires a non-empty `reason`")
        declared = round_gate.declared_grounds(review.mode, genre_of(review.config or {}))
        ground = payload.get("ground")
        if declared is None:
            override_problems.append(
                "this review kind declares NO grounds, so there is nothing to override — "
                "it refuses to run a seeing pass until its grounds are declared "
                "(fail-closed)"
            )
        elif ground not in declared:
            override_problems.append(
                f"`ground` is {ground!r}, but this review kind declares {list(declared)} "
                "— an override may only name a declared ground"
            )
        if override_problems:
            raise InvalidMessagePayloadError(
                "grounds_override: " + "; ".join(override_problems)
            )

    # --- B.7: the round gate ---------------------------------------------------------
    #
    # The gate stands BEFORE implementation, and it is the SERVER that holds the loop
    # closed — not discipline. Everything here refuses a single message with an actionable
    # error (W-5); nothing can invalidate a completed critic pass or consume an open gate.
    gate: round_gate.GateState | None = None
    if round_gate_in_force(review):
        log_dicts = _as_dicts(await get_messages(session, review_id, after=0))
        gate = round_gate.compute(log_dicts)
        problem = round_gate.refusal(gate, kind, payload, role)
        if problem is not None:
            raise InvalidMessagePayloadError(problem)
        if kind == "proposals":
            round_gate.validate_proposals(payload, gate.round)
            # B.9 B-5: resolve each boundary-citing waive's ref against the registry and
            # stamp the verdict into the STORED payload — the gate's replay then reads
            # the server's own resolution, never a client claim (validate_proposals just
            # refused any client-set marker). An unresolved or ineligible ref is not an
            # error: the entry simply stays judgment, exactly as a waive with no ref.
            for entry in payload.get("entries") or []:
                if (
                    isinstance(entry, dict)
                    and entry.get("proposed_outcome") == "waive"
                    and entry.get("boundary_ref") is not None
                    and await _resolve_boundary_for_review(
                        session, entry.get("boundary_ref"), review_id
                    )
                ):
                    entry["boundary_ref_resolved"] = True
        elif kind == "gate_directive":
            round_gate.validate_gate_directive(payload, gate.round)
        elif kind == "detail_report":
            round_gate.validate_detail_report(payload, gate.round)
        elif kind == "class_report":
            round_gate.validate_class_report(payload, gate.round)
        elif kind == "operator_finalize":
            round_gate.validate_operator_finalize(payload)
        elif kind == "findings" and role == "critic" and payload.get("items"):
            round_gate.validate_finding_types(payload)
            # THE ANCHOR MUST NAME A REAL VERSION. Under B.7 a findings message opens a
            # ROUND, and a round is a thing the operator is then asked to mark up — so an
            # anchor pointing at a sequence that is not an artifact would open a round over
            # nothing at all (critic finding
            # `findings-anchor-not-bound-to-current-artifact`). Deliberately "exists", not
            # "is the latest": a completed pass whose version was superseded mid-flight is
            # the most expensive thing this transport can destroy, and refusing it here
            # would add a member to exactly the class W-5 forbids growing.
            anchor = payload.get("artifact_seq")
            versions = {
                m.seq for m in await get_messages(session, review_id, after=0)
                if m.kind == "artifact"
            }
            if anchor not in versions:
                raise InvalidMessagePayloadError(
                    f"findings are anchored to artifact_seq {anchor!r}, which is not an "
                    f"artifact on this channel (versions: {sorted(versions) or 'none yet'}). "
                    "A round opens over a version the operator can look at, so the anchor "
                    "has to name one"
                )
        elif kind == "disposition":
            round_gate.validate_class_closure(payload, review.mode)
        elif kind == "notice":
            round_gate.validate_config_notice(payload, log_dicts)
        elif kind == "artifact" and payload.get("intent_summary"):
            # THE SUMMARY'S ANCHOR IS THE OPERATOR'S SURFACE, AND IT WAS UNCHECKED. The
            # summary is what the operator reads at their gate, and it claims to describe a
            # specific version; accepting any integer let that claim point anywhere — at a
            # version nobody reviewed, or at nothing at all (critic finding
            # `intent-summary-anchor-is-unverified`). The subject of a summary is the
            # review's latest ORDINARY version: a summary is never a summary of a summary.
            versions = [
                m.seq for m in await get_messages(session, review_id, after=0)
                if m.kind == "artifact" and not (m.payload or {}).get("intent_summary")
            ]
            latest_version = max(versions) if versions else None
            declared = payload.get("converged_artifact_seq")
            if latest_version is None:
                raise InvalidMessagePayloadError(
                    "an intent summary needs a version to summarize — this review has no "
                    "ordinary artifact version yet"
                )
            if declared != latest_version:
                raise InvalidMessagePayloadError(
                    f"`converged_artifact_seq` is {declared!r}, but this review's subject is "
                    f"artifact_seq {latest_version} (its latest ordinary version). The "
                    "summary is what the operator reads at their gate — it may not claim to "
                    "describe a version other than the one it summarizes"
                )
        elif kind == "escalation" and role == "development" and (
            payload.get("kind") != "external_defect"
        ):
            # FF-1 constrains the party that escalates FORKS to the operator: development.
            # An `external_defect` is a report about the base, not a fork, and the critic's
            # own escalations are not what the measured waste came from.
            round_gate.validate_fork_analysis(payload)

    # Timeline-integrity for external_defect (needs the log): a new external_defect id must
    # be UNIQUE (else two escalations collapse under one graph-result in the set-based gate),
    # a graph-result must reference a REAL prior external_defect (no dangling/pre-emptive
    # clearing of the J.8.2 gate), and an external_defect ANSWER (escalation_id) must resolve
    # a real prior external_defect (no closing/freezing a nonexistent escalation).
    if kind == "escalation" and payload.get("kind") == "external_defect":
        if payload["id"] in await _external_defect_ids(session, review_id):
            raise InvalidMessagePayloadError(
                f"duplicate external_defect id {payload['id']!r} — each external_defect "
                "carries a distinct stable id"
            )
    if kind == "external_defect_graph_result":
        if payload["escalation_ref"] not in await _external_defect_ids(session, review_id):
            raise InvalidMessagePayloadError(
                f"external_defect_graph_result references {payload['escalation_ref']!r} but "
                "no external_defect with that id was raised"
            )
    # F22: a findings item may not REUSE an already terminally-disposed id as its OWN id — a
    # closure dispute uses `reopens_finding_id` (which points at the old id) with a FRESH item
    # id; reusing the disposed id would make the ledger silently count the new finding as
    # already disposed. (Carrying a finding forward across passes happens while it is still
    # OPEN, so a disposed id never legitimately reappears as an item id.)
    #
    # B.8 A-3: the offending ITEM is rejected, not the batch — incident 9ee91783 lost a
    # whole completed pass to one such identifier, leaving development unable to tell a
    # substantive re-raise from a mechanical re-emission. A batch whose ONLY content was
    # invalid is still refused (below), so nothing records an accidental "clean pass".
    if kind == "findings":
        disposed = await _disposed_finding_ids(session, review_id)
        kept_items = []
        for f in payload.get("items") or []:
            if f.get("id") in disposed:
                rejected_items.append(
                    {
                        "id": f.get("id"),
                        "reason": (
                            f"finding id {f['id']!r} already has a terminal disposition — a "
                            "re-raise must use a fresh item `id` with `reopens_finding_id`, "
                            "not the disposed id"
                        ),
                    }
                )
            else:
                kept_items.append(f)
        payload["items"] = kept_items
        if items_posted and not kept_items:
            raise InvalidMessagePayloadError(
                "findings: every item failed validation — "
                + "; ".join(str(i.get("reason")) for i in rejected_items)
            )

    # A coverage report is a verdict sheet against a SPECIFIC manifest: it must name one
    # that was actually posted for the same artifact version, and it may not verdict a row
    # that manifest does not contain. Without this the report could certify coverage of a
    # denominator nobody ever saw — the one thing an external denominator exists to prevent.
    # A manifest is mode-specific by construction — element rows in spec mode, symbol reach
    # in code mode — so a mode mismatch means the review is being gated by a denominator
    # derived for a different kind of subject. Nothing downstream catches it: the critic's
    # no-tooling fallback compares base and commit, and those match.
    if kind == "coverage_manifest":
        artifact = await _artifact_message(session, review_id, payload.get("artifact_seq"))
        # The anchor must name a real artifact. Accepting an orphan let a manifest be posted
        # ahead of (or beside) any artifact and then become the denominator for whatever
        # later took that seq — with none of the checks below ever having run against it.
        if artifact is None:
            raise InvalidMessagePayloadError(
                f"coverage_manifest is anchored to artifact_seq "
                f"{payload.get('artifact_seq')!r}, which is not an artifact on this "
                "channel — a denominator must belong to a version that exists"
            )
        # GRANULARITY IS THE DEPTH TIER'S, not the poster's. The tier is what the operator
        # granted, and it says how finely the surface is enumerated; a manifest derived
        # coarser than the tier allows silently shrinks the denominator to a fraction of the
        # rows the review was supposed to verdict, while passing every other check.
        required = _required_granularity(await get_messages(session, review_id, after=0))
        if required == "symbol" and payload.get("granularity") != "symbol":
            raise InvalidMessagePayloadError(
                f"the review's depth tier requires symbol-level granularity, but this "
                f"manifest declares {payload.get('granularity')!r} — a coarser derivation "
                "would shrink the denominator below what the tier asks for"
            )

        artifact_payload = artifact.payload or {}
        # The post-review intent summary is AUDITED against the converged version, not
        # reached: it has no ref pair of its own and owes no denominator. The exception was
        # documented and unenforced — and an erroneous manifest here does real damage, since
        # the binding would then demand a coverage report from the audit pass and put the
        # operator gate's handoff behind machinery the design says must not apply to it.
        if artifact_payload.get("intent_summary") is True:
            raise InvalidMessagePayloadError(
                f"artifact_seq {payload.get('artifact_seq')!r} is a post-review intent "
                "summary — it is audited against the converged version, not reached, so it "
                "takes no coverage manifest and no coverage report"
            )
        artifact_mode = (
            artifact_payload.get("mode")
            if artifact_payload.get("mode") in _ARTIFACT_MODES
            else None
        )
        if artifact_mode is not None and payload.get("mode") != artifact_mode:
            raise InvalidMessagePayloadError(
                f"coverage_manifest declares mode {payload.get('mode')!r} but artifact_seq "
                f"{payload.get('artifact_seq')!r} is a {artifact_mode!r} artifact — a "
                "manifest derived for the other mode is the wrong denominator, and the "
                "base/commit check cannot see the difference"
            )
        # Where the artifact carries a usable git pointer, the manifest must be derived from
        # THAT pair. This is the check the critic performs by hand when it cannot re-run the
        # tool; making it mechanical means it holds in bindings that cannot run anything.
        ref = artifact_payload.get("artifact_ref")
        if _has_usable_ref(ref):
            for field in ("base", "commit"):
                if payload.get(field) != ref.get(field):
                    raise InvalidMessagePayloadError(
                        f"coverage_manifest {field} {payload.get(field)!r} does not match "
                        f"artifact_seq {payload.get('artifact_seq')!r}'s {field} "
                        f"{ref.get(field)!r} — the denominator would describe a different "
                        "change than the one under review"
                    )
        # A REPLACEMENT SAYS SO, AND WHY (finding b8-manifest-input-replacement-unanchored).
        # A manifest may legitimately be re-posted for the same version and readers take the
        # newest — but the high-stakes flags it carries decide where a clean verdict must
        # show typed reading evidence, so a silent replacement changes the ENFORCEMENT
        # denominator after the artifact was submitted, with no auditable trace (measured in
        # this design's own review: two tables for one version, different high-stakes
        # digests, no recorded cause). An identical re-post (same manifest_id) stays legal:
        # re-sending the same table changes nothing.
        prior = await _manifest_for_artifact(
            session, review_id, payload.get("artifact_seq")
        )
        if prior is not None and prior.get("manifest_id") != payload.get("manifest_id"):
            if payload.get("replaces_manifest_id") != prior.get("manifest_id"):
                raise InvalidMessagePayloadError(
                    f"a coverage_manifest ({prior.get('manifest_id')!r}) is already in "
                    f"force for artifact_seq {payload.get('artifact_seq')!r} — a different "
                    "table must carry `replaces_manifest_id` naming the one it replaces: "
                    "the denominator in force never changes silently"
                )
            if not _is_nonempty_str(payload.get("correction")):
                raise InvalidMessagePayloadError(
                    "a replacement coverage_manifest requires a non-empty `correction` "
                    "saying why the denominator changed — the change is legal, the "
                    "silence is not"
                )

    if kind == "coverage_report":
        # A report for a SUPERSEDED artifact version has no consumer: it describes a
        # denominator the review has moved past, while every reader of coverage state —
        # the gate, the persistence escalation, the watcher's re-pass signal — is asking
        # about the current one. Refusing it here kills that whole class at the root
        # instead of teaching each reader to distrust what it reads. (The watcher already
        # abandons a pass whose channel moved on, so this refuses only a genuinely stale
        # post.)
        latest_artifact = await _latest_artifact_seq(session, review_id)
        if latest_artifact is not None and payload.get("artifact_seq") != latest_artifact:
            raise InvalidMessagePayloadError(
                f"coverage_report is anchored to artifact_seq "
                f"{payload.get('artifact_seq')!r} but the latest artifact is "
                f"{latest_artifact} — a report for a superseded version describes a "
                "denominator the review has moved past, and would be read as coverage of "
                "the current one"
            )
        manifest = await _manifest_for_artifact(
            session, review_id, payload.get("artifact_seq")
        )
        screened, normalised, downgraded = _validate_coverage_report_against_manifest(
            payload, manifest
        )
        rejected_rows += screened
        normalised_rows += normalised
        downgraded_rows += downgraded
        # A `finding` verdict is the one way a row counts as REACHED without a clean claim,
        # so the finding it names has to exist. Otherwise a clean findings message plus a row
        # pointing at a ghost id satisfies coverage with nothing in the ledger at all.
        # B.8 A-1: the ghost row is rejected, not the report — same class as an invented
        # row id: the row's claim cannot be located, so it stays unreported.
        claimed = {
            r.get("finding_id")
            for r in payload.get("rows") or []
            if isinstance(r, dict) and r.get("verdict") == "finding" and r.get("finding_id")
        }
        if claimed:
            real = await _finding_ids_for_artifact(
                session, review_id, payload.get("artifact_seq")
            )
            missing = claimed - real
            if missing:
                kept_rows = []
                for r in payload.get("rows") or []:
                    if r.get("verdict") == "finding" and r.get("finding_id") in missing:
                        rejected_rows.append(
                            {
                                "row_id": r.get("row_id"),
                                "reason": (
                                    f"row {r.get('row_id')!r} names finding id "
                                    f"{r.get('finding_id')!r}, which was not raised for "
                                    f"artifact_seq {payload.get('artifact_seq')!r} — a "
                                    "`finding` verdict is what makes a row count as reached, "
                                    "so it cannot point at a finding that is not in the ledger"
                                ),
                            }
                        )
                    else:
                        kept_rows.append(r)
                payload["rows"] = kept_rows
        # A-1's floor: a report whose rows ALL fail is still a refusal — an accepted
        # report carrying zero verdicts would read as "coverage reported" downstream
        # while measuring nothing.
        if rows_posted and not payload.get("rows"):
            raise InvalidMessagePayloadError(
                "coverage_report: every row failed validation — "
                + "; ".join(str(r.get("reason")) for r in rejected_rows)
            )

    if kind == "decision_response" and payload.get("escalation_id"):
        esc_id = payload["escalation_id"]
        if esc_id not in await _external_defect_ids(session, review_id):
            raise InvalidMessagePayloadError(
                f"decision_response references external_defect {esc_id!r} but no "
                "external_defect with that id was raised"
            )
        # order-aware: an answer resolves only an OPEN external_defect. A stale/duplicate
        # answer (the escalation was already resolved) must not re-close it or re-`freeze`
        # the review (F13) — it is refused rather than silently applying side effects.
        if f"ext:{esc_id}" not in await _open_operator_items(session, review_id):
            raise InvalidMessagePayloadError(
                f"external_defect {esc_id!r} is not open (already resolved) — a stale or "
                "duplicate answer cannot re-close or re-freeze it"
            )

    # The one server-enforced guard: an inconsistent `converged` never enters `converged`
    # state. Two failure modes (Part K). A MALFORMED declaration — wrong state, a stale
    # version, or no materialized clean pass — is the critic's OWN error → REFUSE (409,
    # self-heal). A settled-ledger blocker — an undisposed finding, an open escalation —
    # is someone ELSE's turn: the declaration is recorded, but the review is ROUTED to a
    # wakeable state instead of dead-locking in critic_reviewing (K.4.2). The critic no
    # longer polices the ledger (K.2); the server is its sole gate, and it self-completes
    # convergence when the blocker clears (K.4.1, below).
    converged_check: _ConvergenceState | None = None
    if kind == "status" and payload.get("value") == "converged":
        # Operator ruling 2026-07-08 (map confirmation pass): the critic must have
        # PICKED UP the review — converged only from critic_reviewing.
        if review.state != "critic_reviewing":
            raise ConvergenceRefusedError(
                f"review is in {review.state!r}, not 'critic_reviewing' — the critic "
                "must pick the review up (advance to critic_reviewing) before "
                "declaring converged"
            )
        converged_check = await _convergence_state(session, review_id)
        # The declaring status may ITSELF carry the observation answers (a same-version
        # pass ends with one status doing both) — the state above is computed from the
        # log BEFORE this message lands, so subtract what this very payload answers, or
        # a pass that cleared the ledger would be blocked on its own answers.
        _discount_observations_answered_by(converged_check, payload)
        _raise_if_malformed_converged(converged_check, payload)  # stale / no-clean-pass → 409

    # A critic pass that REVIEWED the artifact owes its coverage report, and the obligation
    # has to live here rather than only in one binding: a service or manual binding could
    # otherwise end a pass with findings and no report, and the report could never be
    # supplied afterwards, because once a newer artifact lands a report for the older one is
    # refused. The ledger gap would be permanent and invisible.
    #
    # The trigger is "this pass posted FINDINGS", not the status value. Exempting
    # `needs_human` by its value was both inconsistent with the reference binding (which
    # demands the report whenever a manifest exists) and wrong in substance: a pass that
    # reviewed the artifact and then stopped on an operator item owes its verdicts like any
    # other. A pass genuinely blocked BEFORE reading anything posts no findings and owes
    # nothing — and if it does post findings, the honest report is every row `not-reached`
    # with the blockage as the reason, which puts the blockage IN the coverage ledger
    # instead of leaving a silent hole where a pass used to be.
    # THE FIRST EVIDENCE MESSAGE, not only the pass-ending status. Refusing only the status
    # let a PARTIAL pass land: the findings were already on the log, the status was rejected,
    # and `last_reviewed_seq` then read those orphaned findings as "this version is reviewed",
    # so the reference watcher waited forever while nothing told development it owed the
    # denominator. Refusing the evidence itself keeps the pass atomic — nothing lands — and
    # it holds for ANY client, not just the reference watcher.
    if role == "critic" and kind in PASS_EVIDENCE_KINDS and await _manifest_owed(
        session, review_id, await get_messages(session, review_id, after=0),
        payload.get("artifact_seq"),
    ):
        raise InvalidMessagePayloadError(
            f"artifact_seq {payload.get('artifact_seq')!r} owes a coverage_manifest before it "
            "can be reviewed — coverage is in play for this review, and only development can "
            "post the denominator. Nothing of this pass has been recorded; the manifest is "
            "owed first"
        )

    if kind == "status" and payload.get("value") in (
        "needs_iteration",
        "converged",
        "needs_human",
    ):
        anchor = payload.get("artifact_seq")
        log_now = await get_messages(session, review_id, after=0)
        this_pass = _critic_passes(log_now, anchor)[-1]
        if this_pass.reviewed:
            manifest = await _manifest_for_artifact(session, review_id, anchor)
            if manifest is None:
                # ONCE COVERAGE IS IN PLAY IT STAYS IN PLAY, and the check is kept HERE as
                # well as at the evidence message above: a pass whose evidence was an
                # ESCALATION rather than findings still reaches this point, and a reviewed
                # version without a denominator could otherwise be superseded by a new
                # artifact, taking the missing table out of reach of every later check.
                if await _manifest_owed(session, review_id, log_now, anchor):
                    raise InvalidMessagePayloadError(
                        f"this pass reviewed artifact_seq {anchor!r} but no coverage_manifest "
                        "was posted for that version — coverage is in play for this review, "
                        "and a reviewed version without a denominator leaves verdicts that "
                        "can never be supplied"
                    )
            else:
                in_force = manifest.get("manifest_id")
                if not this_pass.answering(anchor, in_force):
                    raise InvalidMessagePayloadError(
                        f"coverage_manifest {in_force!r} is in force for artifact_seq "
                        f"{anchor!r}, so this pass must post a coverage_report answering THAT "
                        "manifest before ending — one verdict per row, or one "
                        "`unanswerable` declaration saying why no verdicts could be "
                        "produced. A report for a superseded version is refused later, so an "
                        "omitted one can never be supplied"
                    )
                if payload.get("value") == "converged" and this_pass.answered_unanswerable(
                    anchor, in_force
                ):
                    # The escape hatch ends a pass; it never converges a review. An
                    # `unanswerable` report claims coverage of nothing, so converging over it
                    # would declare the artifact clean on a denominator nobody measured —
                    # exactly the silent hole the gate exists to stop. The pass may end
                    # (needs_iteration / needs_human) and a later pass answers properly.
                    raise InvalidMessagePayloadError(
                        f"this pass declared the coverage report for artifact_seq {anchor!r} "
                        "UNANSWERABLE, so it cannot also declare convergence: converging over "
                        "an unmeasured denominator is the silent coverage hole this gate "
                        "exists to prevent. End the pass with `needs_iteration` or "
                        "`needs_human` and answer the manifest in a later pass"
                    )


    next_seq = (
        await session.scalar(
            select(func.coalesce(func.max(ReviewMessage.seq), 0)).where(
                ReviewMessage.review_id == review_id
            )
        )
    ) + 1
    message = ReviewMessage(
        review_id=review_id, seq=next_seq, role=role, kind=kind, payload=payload
    )
    session.add(message)
    # B.8 A-1/A-3: what was screened OUT rides on the accepted response, not in the stored
    # payload — the channel records only what was accepted, and the poster is told exactly
    # which content was not, with reasons. Plain instance attribute (not a column): the
    # acceptance echo is a property of THIS post, not of the channel's history.
    partial = {}
    if rejected_rows:
        partial["rejected_rows"] = rejected_rows
    if normalised_rows:
        partial["normalised_rows"] = normalised_rows
    if downgraded_rows:
        partial["downgraded_rows"] = downgraded_rows
    if rejected_items:
        partial["rejected_items"] = rejected_items
    message.partial_acceptance = partial or None

    _apply_message_transition(review, kind, payload)

    # Part K: finalize the converged declaration now that it is on the log. A clean
    # ledger converges; a blocked one is ROUTED to a wakeable state + a blocker notice,
    # never left stalling in critic_reviewing (K.4.2). The server self-completes it later
    # (K.4.1) when the blocker clears — no artifact re-post needed.
    if kind == "status" and payload.get("value") == "converged" and converged_check is not None:
        if converged_check.convergeable:
            review.state = "converged"
        else:
            next_seq += 1
            session.add(
                ReviewMessage(
                    review_id=review_id, seq=next_seq, role="system", kind="notice",
                    payload={
                        "phase": "convergence_blocked",
                        "artifact_seq": payload.get("artifact_seq"),
                        "undisposed_findings": converged_check.undisposed,
                        "open_operator_items": converged_check.open_items,
                        "ungrafted_external_defects": converged_check.ungrafted_external_defects,
                        # B.9 G-3: an unanswered observation blocks convergence — the
                        # CRITIC's turn: the watcher schedules a same-version pass from
                        # this list, and that pass may answer any posted observation.
                        "unanswered_observations": converged_check.unanswered_observations,
                        # B.6 G-3: unreached manifest rows are a blocker like any other —
                        # RECORDED and routed, never a 409. The critic declared honestly;
                        # what is owed is another sweep, and this list is the worklist.
                        "unreached_rows": (
                            converged_check.unreached_rows
                            if converged_check.coverage_blocked
                            else []
                        ),
                        "unreached_allowance": converged_check.unreached_allowance,
                        # Deliberately NOT in `unreached_rows`: that list is what the
                        # watcher schedules another CRITIC sweep from, and only
                        # development can post a manifest.
                        "missing_coverage_manifest": converged_check.missing_manifest,
                        "detail": (
                            "clean pass recorded, but the ledger is not settled — the "
                            "server completes convergence when these clear (no artifact "
                            "re-post needed)"
                        ),
                    },
                )
            )
            # dev's turn: dispose the named findings, or post the missing graph-result(s)
            if (
                converged_check.undisposed
                or converged_check.ungrafted_external_defects
                or converged_check.missing_manifest
            ):
                review.state = "dev_disposing"
            review.parked = bool(converged_check.open_items)  # operator's turn, if any
            # A coverage-only OR observation-only blocker leaves the review in
            # `critic_reviewing`: it is the CRITIC's turn — another sweep over the open
            # rows, or a same-version pass answering the late observations (finding
            # b9-late-observation-no-legal-cleanup-path: routing observations to
            # development gave development a turn with no legal move). The notice above
            # is what the watcher re-plans a pass from.

    # --- B.7: the operator's own finals, and the gate's parking flag -----------------
    if gate is not None:
        post_log = log_dicts + [
            {"seq": next_seq, "role": role, "kind": kind, "payload": payload}
        ]
        after = round_gate.compute(post_log)
        if kind == "operator_finalize":
            if payload.get("mode") == "after_fixes" and after.state == round_gate.OPERATOR_FINALIZING:
                # The final version is still owed: development implements the directives
                # already accepted and posts it with its self-audit. Nothing is closed yet.
                review.state = "operator_finalizing"
            else:
                next_seq = await _close_ledger_on_final(
                    session, review_id, mode=payload.get("mode"), gate=gate,
                    finalize_seq=next_seq, next_seq=next_seq,
                )
                review.state = "operator_finalized"
                review.parked = False
        elif kind == "artifact" and gate.state == round_gate.OPERATOR_FINALIZING:
            # W-3: this is the ONE artifact `operator_finalizing` accepts. Its arrival is
            # what closes the ledger — per F-3's total rule, keyed on each finding's
            # settled directive, so a settled operator ruling is never relabelled as
            # unaddressed risk.
            next_seq = await _close_ledger_on_final(
                session, review_id, mode="after_fixes", gate=gate,
                finalize_seq=next_seq, next_seq=next_seq,
            )
            review.state = "operator_finalized"
            review.parked = False
        else:
            # THE GATE IS A PARK. `parked` is one flag shared with the operator-item
            # machinery, so it must reflect the whole remaining set: an open gate keeps the
            # review parked even when no escalation is outstanding, and a settled gate does
            # not unpark a review that still owes the operator an answer elsewhere.
            review.parked = after.gate_open or bool(
                await _open_operator_items(session, review_id)
            )

    # Oscillation auto-detect (spec §10, operator ruling 2026-07-08): a findings item
    # that reopens a previously-DISPOSED finding is a fix-revert / re-raise signal two
    # LLMs cannot settle between themselves. The server detects it mechanically (the
    # data is already here) and escalates as a system contested_fork — enforcing layer,
    # not prompt discipline. Parks the review; blocks convergence until the operator
    # settles it (the escalation is keyed by the reopened finding's id).
    if kind == "findings":
        for reopened_id in await _detect_oscillation(session, review_id, payload):
            next_seq += 1
            session.add(
                ReviewMessage(
                    review_id=review_id,
                    seq=next_seq,
                    role="system",
                    kind="escalation",
                    payload={
                        "kind": "contested_fork",
                        "artifact_seq": payload.get("artifact_seq"),
                        "finding_id": reopened_id,
                        "detail": (
                            "server-detected oscillation: this pass reopens finding "
                            f"{reopened_id!r}, which already had a terminal disposition"
                        ),
                        "requested_operator_action": "settle the contested fork",
                    },
                )
            )
            review.parked = True

    # B.6 G-3 non-deadlock exit: a row two consecutive passes could not reach stops being a
    # loop condition and becomes an operator-surfaced item. Detected mechanically here, in
    # the enforcing layer, for the same reason the oscillation detector lives here: the data
    # is already on the log, and a rule that depends on either LLM noticing its own repeated
    # failure is the rule most likely to go unnoticed.
    #
    # EVALUATED AT THE PASS-ENDING STATUS, never when a report is posted. A pass may report
    # more than once, and within a pass the LAST report speaks for it — so a first report
    # saying `not-reached` is not that pass's verdict, and firing on it escalated a row the
    # same pass went on to reach. The old trigger (`kind == "coverage_report"`) is GONE
    # rather than narrowed: left in place it would keep the mid-pass firing it caused, which
    # is the half-fix shape this review has already paid for twice.
    if kind == "status" and role == "critic":
        _log = await get_messages(session, review_id, after=0)
        _latest_artifact = max(
            (m.seq for m in _log if m.kind == "artifact"), default=None
        )
        _manifest = None
        for _m in _log:
            if (
                _m.kind == "coverage_manifest"
                and (_m.payload or {}).get("artifact_seq") == _latest_artifact
            ):
                _manifest = _m.payload
        if _question_denominator(_manifest):
            # B.14 B-6/B-7: under the question denominator the two-consecutive-passes trigger and the
            # unreached allowance are RETIRED — an axis escalates only on the critic's
            # explicit typed statement, never because a pass has not gotten to it yet
            # (an unanswered axis merely blocks convergence; a critic that stops
            # progressing toward totality is the ordinary stall alarm's case).
            # Both typed-failure routes are evaluated at the pass-ending status on the
            # ACCUMULATED outcome (latest binds within the version), and each keeps the
            # existing escalation carrier: the row-question fork for `cannot_reach`
            # (immediate — no two-pass wait), and the B.11 instrument-remedy fork for a
            # group of `instrument_failure` axes, its text never inviting a waiver.
            _accumulated: dict[str, str] = {}
            for _m in _log:
                if (
                    _m.kind == "coverage_report"
                    and (_m.payload or {}).get("artifact_seq") == _latest_artifact
                    and (_m.payload or {}).get("manifest_id") == _manifest.get("manifest_id")
                ):
                    for _r in (_m.payload or {}).get("rows") or []:
                        if isinstance(_r, dict) and _r.get("row_id"):
                            _accumulated[str(_r["row_id"])] = _r.get("verdict")
            _exempt = _escalated_coverage_rows(_log) | _scope_exempt_rows(_log, _manifest)
            for _target, _is_remedy in (
                (sorted(r for r, v in _accumulated.items() if v == "cannot_reach" and r not in _exempt), False),
                (sorted(r for r, v in _accumulated.items() if v == "instrument_failure" and r not in _exempt), True),
            ):
                if not _target:
                    continue
                next_seq += 1
                if len(_target) == 1:
                    _element_id = f"{COVERAGE_ROW_ELEMENT_PREFIX}{_target[0]}"
                    _fork_payload = {}
                else:
                    _element_id = f"{COVERAGE_ROWS_GROUP_PREFIX}{next_seq}"
                    _fork_payload = {"row_ids": list(_target)}
                if _is_remedy:
                    _fork_payload["cause"] = coverage.INSTRUMENT_FAILURE_REASON
                    _detail = (
                        f"block-axis row(s) {_target} carry the typed `instrument_failure` "
                        "outcome — the instrument failed to prove the reading; the axes "
                        "themselves are untested. This is not a question about whether "
                        "they are reviewable, and they must NOT be waived on this basis. "
                        "One answer under this element key settles the whole group"
                    )
                    _action = (
                        "an instrument remedy: fix what the row reasons name, then a "
                        "subsequent pass supersedes these axes with real outcomes "
                        "(latest binds within the version); answer `keep_gating` so the "
                        "axes stay in the denominator. Accepting them as unreviewable "
                        "would record coverage that was never measured"
                    )
                else:
                    _detail = (
                        f"block-axis row(s) {_target} carry the critic's explicit "
                        "`cannot_reach` statement, with its reason on the report row — "
                        "either the axes are genuinely unreviewable or the review's "
                        "scope is wrong; both are operator calls (B.14 B-6). One answer "
                        "under this element key settles the whole group"
                    )
                    _action = (
                        "decide: accept the axes as unreviewable "
                        "(`accepted_unreviewable`), or keep gating on them "
                        "(`keep_gating`) — optionally with a narrowed or redirected scope"
                    )
                session.add(
                    ReviewMessage(
                        review_id=review_id,
                        seq=next_seq,
                        role="system",
                        kind="escalation",
                        payload={
                            "kind": "contested_fork",
                            "artifact_seq": payload.get("artifact_seq"),
                            "element_id": _element_id,
                            **_fork_payload,
                            "detail": _detail,
                            "requested_operator_action": _action,
                        },
                    )
                )
                review.parked = True
            _unreached = []
        else:
            # The exit exists to break a DEADLOCK. Where the operator has already raised the
            # allowance far enough that coverage is not blocking, there is no deadlock to break —
            # and escalating anyway would spend exactly the attention the raised allowance was
            # granted to save.
            _blocked = len(_coverage_blockers(_log, _latest_artifact)) > _unreached_allowance(_log)
            _unreached = _persistently_unreached(_log, _latest_artifact) if _blocked else []
        if _unreached:
            # B.8 C-2: ONE contested_fork listing every row, not one per row. The
            # settle-per-key fold already collapses the ANSWERING side; this collapses the
            # ASKING side — review 59b5ec42 spent six operator items on one already-made
            # per-scope decision. A single row keeps the older singular key so historical
            # channels and their exemption bookkeeping read unchanged.
            next_seq += 1
            if len(_unreached) == 1:
                element_id = f"{COVERAGE_ROW_ELEMENT_PREFIX}{_unreached[0]}"
                fork_payload = {}
            else:
                element_id = f"{COVERAGE_ROWS_GROUP_PREFIX}{next_seq}"
                fork_payload = {"row_ids": list(_unreached)}
            # B.11 B-1: THE THIRD OUTCOME. When every row of the group came down with the
            # watcher's `instrument_failure` classification, the two outcomes this fork
            # used to offer are BOTH false — accepting the rows as unreviewable would
            # manufacture phantom coverage over high-stakes rows, and narrowing the scope
            # would confess an error of scope that did not happen. Measured in bd787de7,
            # where the operator had to invent the third path by hand. The remedy is an
            # instrument remedy, and the text must not invite a waiver.
            _by_instrument = _all_downgraded_by_instrument(_log, _unreached)
            if _by_instrument:
                fork_payload["cause"] = coverage.INSTRUMENT_FAILURE_REASON
                _detail = (
                    f"coverage row(s) {_unreached} were reported not-reached on two "
                    "consecutive passes, and EVERY one of them came down because the "
                    "evidence did not hold up to the watcher's re-read — not because the "
                    "rows could not be reached. The instrument failed to prove the "
                    "reading; the rows themselves are untested. This is not a question "
                    "about whether they are reviewable, and they must NOT be waived on "
                    "this basis. One answer under this element key settles the whole group"
                )
                _action = (
                    "an instrument remedy: re-run the pass with corrected evidence "
                    "discipline (the downgrade reasons name the ground each quote "
                    "actually served), then answer `keep_gating` so the rows stay in the "
                    "denominator. Accepting them as unreviewable would record coverage "
                    "that was never measured"
                )
            else:
                _detail = (
                    f"coverage row(s) {_unreached} were reported not-reached on "
                    "two consecutive passes — either the rows are genuinely "
                    "unreviewable or the review's scope is wrong; both are "
                    "operator calls, and more passes will not settle either. One "
                    "answer under this element key settles the whole group"
                )
                _action = (
                    "decide: accept the rows as unreviewable (`accepted_unreviewable`), "
                    "or keep gating on them (`keep_gating`) — optionally with a narrowed "
                    "or redirected scope"
                )
            session.add(
                ReviewMessage(
                    review_id=review_id,
                    seq=next_seq,
                    role="system",
                    kind="escalation",
                    payload={
                        "kind": "contested_fork",
                        "artifact_seq": payload.get("artifact_seq"),
                        "element_id": element_id,
                        **fork_payload,
                        "detail": _detail,
                        "requested_operator_action": _action,
                    },
                )
            )
            review.parked = True

    # An operator decision_response un-parks only when NOTHING ELSE still awaits the
    # operator — `parked` is a single flag, so with concurrent contests/questions it
    # must reflect the remaining open set, not the last event (map flag M1 + cycle-04
    # F2: an unanswered human_question keeps the review parked).
    if kind == "decision_response":
        review.parked = bool(await _open_operator_items(session, review_id))

    # external_defect FREEZE (J.3): the operator parks the review to go fix the base defect;
    # it RESUMES later (advance_state out of `frozen`, J.8.5 — the base re-capture on resume
    # is E.1 forward-debt). DEFER/REJECT resolve the escalation and let the loop continue.
    if (
        kind == "decision_response"
        and payload.get("escalation_id")
        and payload.get("action") == "freeze"
    ):
        review.state = "frozen"

    # F12: a return-signalling answer suppresses self-completion (below), so it must hand the
    # turn to DEVELOPMENT — otherwise the review strands in critic_reviewing (nothing for the
    # critic to do, the operator already answered, but no dev-owned wake state). A `freeze`
    # answer already routes to `frozen` (above); everything else that signals a return moves
    # to dev_disposing so development posts the required new artifact.
    if (
        kind == "decision_response"
        and _signals_return(payload)
        and review.state == "critic_reviewing"
    ):
        review.state = "dev_disposing"

    # K.4.1 — the server SELF-COMPLETES convergence. When a disposition, an
    # escalation-closing decision_response, or a posted external_defect_graph_result leaves
    # the ledger settled AND a materialized clean pass already exists for the LATEST
    # artifact, the server transitions to `converged` itself — no fresh critic pass, no
    # artifact re-post (what the old dead-lock fix "re-post the identical artifact"
    # hand-worked around). A decision_response that RETURNS the review / changes scope is
    # excluded: it makes the prior clean pass stale (K.4.1 restriction), and a new artifact —
    # which invalidates the clean pass for the latest — would in any case block this check.
    # A declaration blocked ONLY by unreached rows is settled by the sweep that fills them,
    # and without that the critic would have to re-declare a convergence it already declared
    # honestly — the hand-worked re-post K.4.1 exists to remove. The trigger for that case is
    # the sweep's PASS-ENDING STATUS, not its `coverage_report`.
    #
    # It was the report, and that raced the pass that owned it: the report self-completed the
    # convergence, the state left `critic_reviewing`, and the status the critic's output
    # contract ALWAYS posts next was then refused — the reference binding failing immediately
    # after doing the work it was scheduled to do. This is the same rule the two-pass coverage
    # exit already runs on: nothing that ends a pass is decided in the middle of one. Applying
    # the rule here dissolves the problem instead of adding a deferral or an idempotent-status
    # special case to work around it.
    #
    # `needs_human` is deliberately NOT a trigger: a pass that just asked for a human must not
    # converge on its way out. A `converged` status is handled above, before this point.
    if (
        (
            kind in ("disposition", "decision_response", "external_defect_graph_result")
            or (kind == "status" and payload.get("value") == "needs_iteration")
        )
        and not _signals_return(payload)
        and review.state in ("critic_reviewing", "dev_disposing")
    ):
        check = await _convergence_state(session, review_id)
        # Same pre-insert discount as the explicit-converged branch: a needs_iteration
        # status whose own `observation_dispositions` clear the last late observation
        # must be able to self-complete the earlier blocked declaration.
        _discount_observations_answered_by(check, payload)
        # Self-complete ONLY a convergence the critic already DECLARED (and that was then
        # blocked/routed). A clean findings pass alone is not a declaration — without this
        # gate, empty-findings + needs_human answered by the operator would wrongly converge
        # a review the critic never asked to converge (INV: the critic declares convergence).
        if check.convergeable and check.has_converged_declaration:
            review.state = "converged"
            review.parked = False
            next_seq += 1
            session.add(
                ReviewMessage(
                    review_id=review_id, seq=next_seq, role="system", kind="status",
                    payload={
                        "value": "converged",
                        "artifact_seq": check.latest,
                        "detail": (
                            "server self-completed convergence (K.4.1): the critic's "
                            "converged declaration was blocked, and the ledger is now settled"
                        ),
                    },
                )
            )

    await session.flush()
    return message


async def _close_ledger_on_final(
    session: AsyncSession,
    review_id: uuid.UUID,
    *,
    mode: str | None,
    gate: round_gate.GateState,
    finalize_seq: int,
    next_seq: int,
) -> int:
    """Close every still-undisposed finding when the operator stops the review (F-2/F-3).

    NOTHING IS DROPPED SILENTLY; what changes is only whose hand closes it. The labels are
    emitted by the SERVER — never authored as development waivers — which is what keeps the
    bookkeeping mechanical and un-fakeable, and it is why `fixed` and `waived` stay reserved
    for the paths that earned them.

    ``now`` (F-2): ONE uniform rule in every accepting state, with no state-dependent
    branching — everything undisposed closes as ``operator_risk_accepted``, and the truth
    about the work lives in the post-review walk of the stopped artifact (F-4). The corner
    this closes was judged «классический случай защиты от крайне маловероятных угроз» by
    the operator (round-24 gate), so it is closed by the cheapest total rule plus the walk,
    not by state-dependent labelling machinery.

    ``after_fixes`` (F-3): TOTAL, keyed on the settled directive. A sanctioned fix closes
    ``fixed_unverified`` — fixed, critic verification skipped by operator decision, and
    never plain ``fixed``, so the record stays readable a month later. A settled waive
    closes ``waived`` with the recorded reason: a settled operator ruling is never
    relabelled as unaddressed risk. Everything with no settled directive — unsettled,
    pending a report, or never directed — closes as ``operator_risk_accepted``, exactly as
    under ``now``.
    """
    messages = await get_messages(session, review_id, after=0)
    finding_ids: list[str] = []
    for m in messages:
        if m.kind == "findings":
            for f in (m.payload or {}).get("items") or []:
                fid = f.get("id") if isinstance(f, dict) else None
                if fid and fid not in finding_ids:
                    finding_ids.append(str(fid))
    disposed = {
        (m.payload or {}).get("finding_id")
        for m in messages
        if m.kind == "disposition"
        and (m.payload or {}).get("outcome") in round_gate.TERMINAL_OUTCOMES
    }
    latest = await _latest_artifact_seq(session, review_id)
    entries = gate.round.entries if gate.round is not None else {}
    for fid in finding_ids:
        if fid in disposed:
            continue
        entry = entries.get(fid)
        settled = entry.settled_outcome if entry is not None else None
        if mode == "after_fixes" and settled == "fix":
            outcome = round_gate.FIXED_UNVERIFIED
            reason = (
                "implemented under the operator's after-fixes final; the verifying critic "
                "pass was skipped by operator decision"
            )
        elif mode == "after_fixes" and settled == "waive":
            outcome = "waived"
            reason = entry.settled_reason if entry and entry.settled_reason else (
                "waived by operator directive at the round gate"
            )
        else:
            outcome = round_gate.OPERATOR_RISK_ACCEPTED
            reason = (
                "the operator ended the review before this finding was disposed of — no "
                "disposition was recorded. What was actually implemented is reported, per "
                "finding, by the post-review walk of the stopped artifact"
            )
        next_seq += 1
        session.add(
            ReviewMessage(
                review_id=review_id,
                seq=next_seq,
                role="system",
                kind="disposition",
                payload={
                    "finding_id": fid,
                    "outcome": outcome,
                    "reason": reason,
                    "artifact_seq": latest,
                    "operator_finalize_seq": finalize_seq,
                    "finalize_mode": mode,
                },
            )
        )
    return next_seq


def _apply_message_transition(review: Review, kind: str, payload: dict) -> None:
    """Apply the FSM side-effects a message triggers (spec §5); mutates ``review`` in place.

    Cooperative/prompt-maintained: a transition fires only when the current state is
    its legal source, otherwise the message is still recorded (append-only, no silent
    drop, INV-4) but state is untouched. ``parked`` is an orthogonal flag, not a state.
    """
    now = datetime.now(UTC)

    if kind == "artifact":
        # created / dev_disposing / returned / frozen -> artifact_ready; a new artifact is a
        # material change. `frozen` (external_defect FREEZE) resumes ONLY this way: a base
        # fix means a new artifact version (J.8.5), never a reuse of the pre-freeze clean pass.
        #
        # `converged` is ALSO a legal source — for a SUBSTANTIVE artifact only. Milestone-style
        # reviews continue posting artifacts after a convergence (live: review 12feddeb, M1-M5);
        # without this the review stays stuck in `converged`, and the critic's next honest
        # `converged` declaration 409s ("not critic_reviewing") — the recurring gotcha cleared
        # by operator precedent at seqs 28/87. The post-review `intent_summary` artifact is
        # EXCLUDED: the faithfulness audit runs with the review sitting in `converged`
        # (dev_loop/intents contract), so it must not reopen the loop.
        if review.state in ("created", "dev_disposing", "returned", "frozen") or (
            review.state == "converged" and not payload.get("intent_summary")
        ):
            review.state = "artifact_ready"
        review.last_material_change_at = now
        return

    if kind == "status":
        value = payload.get("value")
        if value == "needs_iteration":
            if review.state == "critic_reviewing":
                review.state = "dev_disposing"
                review.iteration += 1  # a completed critic pass (spec §5)
        elif value == "converged":
            # The converged transition (or its blocker-routing, Part K) is owned by
            # append_message — it needs the ledger check first. Nothing to do here.
            pass
        elif value == "needs_human":
            review.parked = True
        return

    # An escalation or a question needs the operator -> park (§5, §11.1). The un-park
    # on a decision_response lives in append_message (it needs the open-escalation
    # query — the flag reflects the REMAINING open set, not the last event).
    if kind in ("human_question", "escalation"):
        review.parked = True
        return


async def get_messages(
    session: AsyncSession, review_id: uuid.UUID, *, after: int = 0
) -> list[ReviewMessage]:
    """Messages with ``seq > after``, in order — the long-poll query core (§3.2, §6).

    The hold/wait loop lives in the route (it re-issues this until non-empty or the
    server hold cap). This is pure read; ``after=0`` returns the whole channel.
    """
    rows = await session.scalars(
        select(ReviewMessage)
        .where(ReviewMessage.review_id == review_id, ReviewMessage.seq > after)
        .order_by(ReviewMessage.seq)
    )
    return list(rows)


# --- lifecycle / gate transitions ----------------------------------------


async def advance_state(
    session: AsyncSession, review_id: uuid.UUID, target: str
) -> Review:
    """Advance a review to ``target`` for the operator/critic lifecycle transitions (§5).

    Guards against illegal jumps via ``_LIFECYCLE_TRANSITIONS`` (raises
    ``InvalidStateTransitionError``). ``operator_gate -> returned`` resets the
    no-progress window; ``-> finalized`` stamps ``closed_at`` and revokes the tokens
    (§8 lifetime). Message-driven loop transitions do NOT go through here — they are
    applied in ``append_message``.
    """
    review = await session.get(Review, review_id, with_for_update=True)
    if review is None:
        raise ReviewNotFoundError

    allowed = _LIFECYCLE_TRANSITIONS.get(review.state, set())
    if target not in allowed:
        raise InvalidStateTransitionError(
            f"cannot advance from {review.state!r} to {target!r} "
            f"(legal: {sorted(allowed) or 'none'})"
        )

    if target == "returned":
        review.last_material_change_at = datetime.now(UTC)  # resets the no-progress window (§10)
    review.state = target
    if target in ("finalized", "abandoned"):
        # Both terminal closes stamp closed_at and revoke every live token (§8 lifetime;
        # abandon added by operator ruling 2026-07-08 — no eternal tokens on dead reviews).
        review.closed_at = datetime.now(UTC)
        await revoke_review_tokens(session, review_id)

    await session.flush()
    return review


def _validate_disposition_payload(payload: dict) -> None:
    """Refuse a malformed disposition at POST time (feedback 4a83064b).

    The convergence guard counts only ``outcome in (fixed|waived)``, so a mis-keyed
    payload (the live incident: ``disposition: "fixed"`` instead of ``outcome``) would
    silently never satisfy condition (3) and surface iterations later as a 409 to the
    CRITIC's watcher. Validating here puts the error in front of its author instead.
    """
    problems = []
    if not _is_nonempty_str(payload.get("finding_id")):
        problems.append("`finding_id` must be a non-empty string")
    outcome = payload.get("outcome")
    if outcome not in ("fixed", "waived"):
        hint = " (the key is `outcome`, not `disposition`)" if "disposition" in payload else ""
        if outcome in round_gate.SERVER_ONLY_OUTCOMES:
            # W-4: the two operator-final outcomes are emitted by the SERVER inside the
            # F-2/F-3 transitions and are postable by neither LLM role — un-fakeable
            # bookkeeping is the whole point of having them.
            hint = (
                f" — {outcome!r} is emitted by the server when the OPERATOR finalizes a "
                "review; it is not a disposition either role may post"
            )
        problems.append(f"`outcome` must be 'fixed' or 'waived', got {outcome!r}{hint}")
    if problems:
        raise InvalidMessagePayloadError(
            "malformed disposition: " + "; ".join(problems) + " — expected payload "
            "{finding_id, outcome: fixed|waived, artifact_seq, reason}"
        )


def _question_denominator(manifest: dict | None) -> bool:
    """Does this manifest carry the B.14 QUESTION denominator (B-1)?

    Keyed on the manifest CARRYING A SLICING, never on its mode alone: an in-flight
    code review created before B.14 holds a v1 place manifest, and flipping it onto
    rules its critic contract never knew would break spec constraint 3 — reviews in
    flight finish under the coverage contract they were created with (finding
    b14-legacy-code-coverage-contract-overwritten). A v2 code manifest always carries
    `blocks` (the POST validator refuses one without), so the slicing is the honest
    self-describing key."""
    return (
        (manifest or {}).get("mode") == "code"
        and (manifest or {}).get("blocks") is not None
    )


def _temporary_register_ids(config: dict | None) -> set[str]:
    """The entry ids of the review's frozen temporary-state register (B.14 D-2).

    Creation-only and immutable, so the instrument snapshot is the single source; a
    review created before B.14 simply has an empty register."""
    register = ((config or {}).get("instrument") or {}).get("temporary_states") or []
    return {
        str(e.get("entry_id"))
        for e in register
        if isinstance(e, dict) and e.get("entry_id")
    }


def _validate_credited_temporary_entries(payload: dict, *, config: dict | None) -> None:
    """B.14 D-3 — the credit record on a pass-ending critic status.

    ``credited_temporary_entries`` is a list of ``{entry_id, case, evidence}``: ``case``
    names the D-3 case grounding the credit (``tracked`` | ``moved``; case 3 credits
    nothing and produces no record) and ``evidence`` is ``{quote, read_ref, tracking}``
    — the span as read in the CURRENT version, a read reference bound to this pass's
    read log, and the anchor-to-current change the credit relies on. Validated against
    the frozen snapshot register: an unknown or duplicate ``entry_id`` is a refusal of
    the status, exactly as ``observation_dispositions`` refusals work beside it. The
    field is optional — an absent field credits nothing."""
    block = payload.get("credited_temporary_entries")
    if block is None:
        return
    problems: list[str] = []
    if not isinstance(block, list):
        raise InvalidMessagePayloadError(
            "credited_temporary_entries: must be a list of {entry_id, case, evidence} "
            "objects (B.14 D-3)"
        )
    known = _temporary_register_ids(config)
    seen: list[str] = []
    for i, credit in enumerate(block):
        where = f"credited_temporary_entries[{i}]"
        if not isinstance(credit, dict):
            problems.append(f"{where}: each credit must be an object")
            continue
        entry_id = credit.get("entry_id")
        if not _is_nonempty_str(entry_id):
            problems.append(f"{where}: requires a non-empty string `entry_id`")
            continue
        seen.append(entry_id)
        if entry_id not in known:
            problems.append(
                f"{where}: entry_id {entry_id!r} names no entry in this review's frozen "
                "temporary-state register — the register is creation-only, nothing is "
                "credited into it retroactively"
            )
        if credit.get("case") not in ("tracked", "moved"):
            problems.append(
                f"{where}: `case` must be 'tracked' or 'moved' (D-3 case 3 credits "
                f"nothing), got {credit.get('case')!r}"
            )
        evidence = credit.get("evidence")
        if not isinstance(evidence, dict):
            problems.append(f"{where}: requires an `evidence` object")
        else:
            for field in ("quote", "read_ref", "tracking"):
                if not _is_nonempty_str(evidence.get(field)):
                    problems.append(
                        f"{where}: evidence requires a non-empty string `{field}`"
                    )
    duplicates = sorted({i for i in seen if seen.count(i) > 1})
    if duplicates:
        problems.append(
            f"duplicate credits for {duplicates} — one object per credited entry"
        )
    if problems:
        raise InvalidMessagePayloadError(
            "credited_temporary_entries: " + "; ".join(problems)
        )


def _validate_findings_payload(
    payload: dict, *, register_ids: set[str] | None = None
) -> list[dict]:
    """Screen a findings batch at POST time (B.8 A-3 partial acceptance).

    ``items`` must be a list (whole refusal — use ``[]`` for a clean pass). Each ITEM
    must carry a non-empty string ``id`` (a set/key identity in the convergence gate and
    oscillation detection) and string link fields (``reopens_finding_id`` /
    ``lineage_of`` / ``root_class``) when present. A malformed item no longer voids the
    batch: it is removed from ``payload["items"]`` and returned as ``{"id", "reason"}``
    for the response's ``rejected_items`` echo (incident 9ee91783: one malformed
    identifier destroyed a pass whose other findings never landed). The caller refuses
    the whole batch when EVERY item failed — an all-invalid batch must not be recorded
    as a clean pass.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        raise InvalidMessagePayloadError(
            "findings `items` must be a list (use [] for a clean pass)"
        )
    rejected: list[dict] = []
    kept: list[dict] = []
    for f in items:
        if not isinstance(f, dict):
            rejected.append({"id": None, "reason": "each findings item must be an object"})
            continue
        if not _is_nonempty_str(f.get("id")):
            rejected.append(
                {"id": f.get("id"), "reason": "each item requires a non-empty string `id`"}
            )
            continue
        bad_link = None
        # `root_class` rides with the link fields: B.6 M-1 counter 2 groups on it, and a
        # non-string key would be a silent bucket of one.
        for link in ("reopens_finding_id", "lineage_of", "root_class"):
            if f.get(link) is not None and not isinstance(f.get(link), str):
                bad_link = link
                break
        if bad_link is not None:
            rejected.append(
                {
                    "id": f.get("id"),
                    "reason": f"finding `{bad_link}` must be a string when present",
                }
            )
            continue
        # B.14 D-3: a contest of a declared temporary state names its entry — validated
        # against the frozen creation register (the register never grows mid-review, so
        # an unresolvable id is an authoring defect, not a timing race).
        contests = f.get("contests_declaration")
        if contests is not None:
            if not _is_nonempty_str(contests):
                rejected.append(
                    {
                        "id": f.get("id"),
                        "reason": "`contests_declaration` must be a non-empty string "
                        "entry_id when present",
                    }
                )
                continue
            if contests not in (register_ids or set()):
                rejected.append(
                    {
                        "id": f.get("id"),
                        "reason": f"`contests_declaration` {contests!r} names no entry "
                        "in this review's frozen temporary-state register",
                    }
                )
                continue
        kept.append(f)
    payload["items"] = kept
    return rejected


def _is_nonempty_str(v) -> bool:
    """A wire identity used as a set/key member must be a non-empty string — not merely
    truthy. A list/dict/number identity passes truthiness but crashes the hashable-set logic
    in `_convergence_state` / `_open_operator_items` (a 500 instead of a fail-safe 422).
    Whitespace-only strings do not count either (finding rationale-whitespace-only-accepted):
    a reason of "   " records nothing auditable, and a whitespace identity is equally
    garbage as a key."""
    return isinstance(v, str) and bool(v.strip())


def _has_usable_ref(ref) -> bool:
    """An artifact_ref is a usable review-subject pointer only with non-empty (stripped)
    ``base`` and ``commit`` — the pair a critic needs to read the change from git."""
    return (
        isinstance(ref, dict)
        and _is_nonempty_str(ref.get("base"))
        and _is_nonempty_str(ref.get("commit"))
    )


#: A canonical immutable git object id: the full 40-hex form, nothing else.
_FULL_GIT_OID = re.compile(r"[0-9a-f]{40}")


def _strict_ref_problems(base, commit, *, where: str) -> list[str]:
    """B.10 A-1 — THE one validation helper for every ref-shaped field of the wire
    contract: the artifact ref in both modes and the manifest's base/commit identity
    fields. Under the current artifact contract each must be a canonical immutable git
    object id — the full 40-hex form — refused by FORM at POST: a branch or tag name,
    or an abbreviated prefix, is not a subject, because a movable name recorded in an
    append-only channel resolves to different bytes over time and silently breaks
    reproducibility and manifest identity. The poster resolves any name to the object
    id BEFORE posting (`git rev-parse <name>^{commit}`).

    The contract-generation gate TRAVELS WITH this helper to every field it validates
    (finding b10-legacy-manifest-ref-validation-gate): callers invoke it only for
    reviews stamped with the current generation — an unmarked or legacy-stamped review
    keeps its prior accepted form for artifacts and manifests alike. Semantic validity
    of the pair (both ids are commits, the pair is diffable) is deliberately NOT
    checked here: the service never holds the repository (B-2) — that check lives at
    the resolution boundary, where a failure is an environmental refusal.
    """
    problems = []
    for name, value in (("base", base), ("commit", commit)):
        if not (isinstance(value, str) and _FULL_GIT_OID.fullmatch(value)):
            problems.append(
                f"`{where}.{name}` must be a full 40-hex git object id under the B.10 "
                f"artifact contract, got {value!r} — a movable name or an abbreviated "
                "prefix is not an immutable subject; resolve it before posting "
                "(`git rev-parse <name>`)"
            )
    return problems


def _is_int(v) -> bool:
    """A wire integer must be an actual int, not a JSON boolean: in Python
    ``isinstance(True, int)`` holds and ``True == 1``, so a version anchor of ``true``
    would sail through an isinstance check and even compare equal to seq 1 in the
    convergence guard (finding version-anchor-bool-accepted)."""
    return type(v) is int


def _validate_external_defect_payload(payload: dict) -> None:
    """Refuse a malformed external_defect escalation at POST time (spec J.8.1).

    ``id`` is load-bearing: it keys the escalation in the open-items gate AND anchors the
    J.8.2 graph-result requirement (``ungrafted_external_defects`` counts external defects
    BY id). A keyless external_defect would be excluded from that gate and could converge
    with no ``external_defect_graph_result`` at all — and its seq-fallback key differs
    between server and watcher. So a missing ``id`` is rejected here rather than silently
    degrading; every external_defect carries a stable id by construction.
    """
    if not _is_nonempty_str(payload.get("id")):
        raise InvalidMessagePayloadError(
            "malformed external_defect escalation: `id` must be a non-empty string — it is "
            "required (J.8.1) and used as a stable set/key identity in the convergence gate "
            "(a non-scalar id would crash the gate instead of failing validation)"
        )


def _validate_escalation_payload(payload: dict) -> None:
    """Refuse a malformed escalation at POST time. An ``external_defect`` has its own rules;
    for every other escalation kind, the identity keys ``element_id`` / ``finding_id`` — used
    as raw dict keys in ``_open_operator_items`` — must be non-empty strings WHEN PRESENT (a
    list/dict key would crash the open-item computation, F19)."""
    if payload.get("kind") == "external_defect":
        _validate_external_defect_payload(payload)
        return
    for f in ("element_id", "finding_id"):
        if f in payload and not _is_nonempty_str(payload.get(f)):
            raise InvalidMessagePayloadError(
                f"escalation `{f}` must be a non-empty string when present"
            )


def _validate_human_question_payload(payload: dict) -> None:
    """A human_question ``id`` (used as an open-item key) must be a non-empty string when
    present; a keyless question is a legacy fallback and stays allowed (F19)."""
    if "id" in payload and not _is_nonempty_str(payload.get("id")):
        raise InvalidMessagePayloadError(
            "human_question `id` must be a non-empty string when present"
        )


_ARTIFACT_MODES = ("spec", "code")


def _validate_artifact_payload(payload: dict, *, config: dict | None = None) -> None:
    """Refuse a malformed artifact at POST time.

    ``mode`` drives the watcher's pass planning (a missing mode silently defaulted to a
    spurious spec cold-pass — feedback 2328ed74) and the FSM reads ``intent_summary`` to
    decide whether the artifact reopens a converged review; both must be well-formed at
    the author, not six steps later at the consumer.

    ``config`` is the review's config, carrying the B.10 artifact-contract stamp
    (A-4): under the current generation a code artifact is a git reference and nothing
    else — the inline ``diff`` is refused (A-1) and the ref must be the strict full
    40-hex form. Unmarked (pre-B.10) and legacy-stamped reviews keep their prior
    accepted forms unchanged.
    """
    problems = []
    # `intent_summary` routes the FSM (a converged review reopens on every artifact EXCEPT
    # an intent summary), so its type is load-bearing: a string like "false" is truthy and
    # would silently suppress the reopen (finding intent-summary-flag-not-typed).
    if "intent_summary" in payload and not isinstance(payload.get("intent_summary"), bool):
        problems.append("`intent_summary` must be a boolean when present")
    is_summary = payload.get("intent_summary") is True
    # `mode` is required for ordinary artifacts (it drives the watcher's pass planning),
    # but an intent summary is marked by `intent_summary` itself — the intents protocol
    # does not require a mode on it, and demanding one here would block the mandatory
    # faithfulness audit (finding intent-summary-mode-required-without-protocol-basis).
    # When present it must still be a legal value, on any artifact.
    if "mode" in payload and payload.get("mode") not in _ARTIFACT_MODES:
        problems.append(f"`mode` must be one of {_ARTIFACT_MODES}, got {payload.get('mode')!r}")
    elif not is_summary and payload.get("mode") not in _ARTIFACT_MODES:
        problems.append(f"`mode` must be one of {_ARTIFACT_MODES}, got {payload.get('mode')!r}")
    if "artifact_ref" in payload and not isinstance(payload.get("artifact_ref"), dict):
        problems.append("`artifact_ref` must be an object when present")
    if is_summary:
        if not _is_int(payload.get("converged_artifact_seq")):
            problems.append("an intent_summary artifact requires an integer `converged_artifact_seq`")
        if not _is_nonempty_str(payload.get("summary_markdown")):
            problems.append("an intent_summary artifact requires a non-empty `summary_markdown`")
    elif payload.get("mode") in _ARTIFACT_MODES:
        # A non-summary artifact must CARRY a review subject (finding
        # subjectless-artifact-accepted) — otherwise the loop can advance and even
        # converge over nothing. Code under the B.10 contract: a strict git reference
        # and nothing else. Code on a legacy/unmarked review: an inline diff or a
        # usable git pointer. Spec: a bundle with the spec text.
        if payload["mode"] == "code":
            if artifact_contract_current(config):
                # B.10 A-1: the inline diff is a second copy of bytes git already
                # holds, paid for on every append and every read, forever (measured:
                # review b5fd70de, 1.4MB projection; review 12feddeb, the audit pass
                # died twice). The critic has the repository at cwd and derives the
                # diff itself.
                if "diff" in payload:
                    problems.append(
                        "under the B.10 artifact contract a code artifact carries NO "
                        "inline `diff` — the only subject form is `artifact_ref` with "
                        "full 40-hex `base` and `commit`; the critic derives the diff "
                        "from the repository (`git diff base..commit`)"
                    )
                if not _has_usable_ref(payload.get("artifact_ref")):
                    problems.append(
                        "a code artifact requires its review subject: an `artifact_ref` "
                        "with `base` and `commit` (full 40-hex git object ids)"
                    )
                else:
                    ref = payload["artifact_ref"]
                    problems += _strict_ref_problems(
                        ref.get("base"), ref.get("commit"), where="artifact_ref"
                    )
            elif not (
                _is_nonempty_str(payload.get("diff"))
                or _has_usable_ref(payload.get("artifact_ref"))
            ):
                problems.append(
                    "a code artifact requires a review subject: a non-empty `diff` or an "
                    "`artifact_ref` with non-empty `base` and `commit`"
                )
        else:  # spec
            bundle = payload.get("bundle")
            has_inline = isinstance(bundle, dict) and _is_nonempty_str(
                bundle.get("spec_markdown")
            )
            if artifact_contract_current(config):
                # B.10 B-1: an ALTERNATIVE referential subject — `artifact_ref` (the
                # same strict form and the same helper as A-1) plus a
                # repository-relative `path` to the document. Exactly one subject form
                # per message: two subjects that can diverge silently is the exact
                # defect class the coverage machinery was built to prevent. The inline
                # bundle stays fully legal — subjects that do not live in git
                # (audience decks, external texts) exist and keep their form (B-3).
                # Every check here is FORM-LEVEL: the service never resolves the
                # referenced bytes (B-2 — the production image ships without .git).
                has_ref = "artifact_ref" in payload or "path" in payload
                if has_inline and has_ref:
                    problems.append(
                        "a spec artifact carries EXACTLY ONE subject form: either the "
                        "inline `bundle` or the referential subject (`artifact_ref` + "
                        "`path`), never both — one source of truth per version"
                    )
                elif has_ref:
                    if not _has_usable_ref(payload.get("artifact_ref")):
                        problems.append(
                            "a referential spec subject requires `artifact_ref` with "
                            "`base` and `commit` (full 40-hex git object ids)"
                        )
                    else:
                        ref = payload["artifact_ref"]
                        problems += _strict_ref_problems(
                            ref.get("base"), ref.get("commit"), where="artifact_ref"
                        )
                    path = payload.get("path")
                    if not _is_nonempty_str(path):
                        problems.append(
                            "a referential spec subject requires a non-empty "
                            "repository-relative `path` to the document"
                        )
                    elif path.startswith(("/", "\\")) or ":" in path.split("/")[0] or (
                        ".." in path.replace("\\", "/").split("/")
                    ):
                        problems.append(
                            f"`path` must be repository-relative (got {path!r}) — no "
                            "absolute paths, drive letters, or parent traversal"
                        )
                elif not has_inline:
                    problems.append(
                        "a spec artifact requires a review subject: either "
                        "`bundle.spec_markdown` (inline) or `artifact_ref` + `path` "
                        "(referential, for documents that live in this repository)"
                    )
            elif not has_inline:
                # Legacy/unmarked reviews keep their prior accepted form unchanged:
                # inline bundle only (the referential form did not exist before B.10).
                problems.append(
                    "a spec artifact requires a review subject: `bundle.spec_markdown` "
                    "must be a non-empty string"
                )
    if problems:
        raise InvalidMessagePayloadError("malformed artifact: " + "; ".join(problems))


_STATUS_VALUES = ("needs_iteration", "converged", "needs_human")


def _validate_status_payload(payload: dict) -> None:
    """Refuse a malformed status at POST time: ``value`` gates FSM transitions and the
    convergence guard; a typo'd value would be recorded and silently drive nothing.

    ``artifact_seq`` is REQUIRED — the version-anchor invariant: every pass-ending
    status speaks about a specific artifact version, and the convergence guard compares
    it against the latest (finding status-anchor-not-required). ``iteration`` stays
    optional: the wire contract sets it on ``needs_iteration`` pass ends, but
    ``needs_human`` / gate-handoff statuses legitimately omit it.
    """
    problems = []
    if payload.get("value") not in _STATUS_VALUES:
        problems.append(f"`value` must be one of {_STATUS_VALUES}, got {payload.get('value')!r}")
    if not _is_int(payload.get("artifact_seq")):
        problems.append("`artifact_seq` is required and must be an integer (version anchor)")
    if "iteration" in payload and not _is_int(payload.get("iteration")):
        problems.append("`iteration` must be an integer when present")
    if "phase" in payload and not _is_nonempty_str(payload.get("phase")):
        problems.append("`phase` must be a non-empty string when present")
    if problems:
        raise InvalidMessagePayloadError("malformed status: " + "; ".join(problems))


def _validate_notice_payload(payload: dict) -> None:
    """Notices are deliberately free-form (memos, cold verdicts, watcher errors); only the
    routing key ``phase`` must be a string when present — the watcher filters on it."""
    if "phase" in payload and not _is_nonempty_str(payload.get("phase")):
        raise InvalidMessagePayloadError("notice `phase` must be a non-empty string when present")


def _validate_waiver_payload(payload: dict) -> None:
    """A recorded waiver must carry its rationale — 'waived-with-reason' is a protocol
    invariant, and a bare marker with no recorded reason would defeat the ledger. The
    live shape is loose ({scope, waiver, follow_up} or {reason}); require a
    RATIONALE-bearing field — ``reason`` or ``waiver`` (the named grant). ``scope``
    alone does not qualify: it records what the waiver concerns, not why it was
    accepted (finding waiver-scope-alone-accepted)."""
    if not any(_is_nonempty_str(payload.get(f)) for f in ("reason", "waiver")):
        raise InvalidMessagePayloadError(
            "malformed waiver: carry the rationale in a non-empty `reason` (preferred) "
            "or `waiver` field — `scope` alone only names what it concerns"
        )


def _validate_dissent_payload(payload: dict) -> None:
    """A dissent exists to record the OBJECTION the operator overrode — an empty dissent
    records nothing. Same loose-shape policy as waivers."""
    if not any(
        _is_nonempty_str(payload.get(f)) for f in ("reason", "text", "detail", "objection")
    ):
        raise InvalidMessagePayloadError(
            "malformed dissent: carry the objection in at least one non-empty field of "
            "`reason` / `text` / `detail` / `objection`"
        )


_GRAPH_DEDUP_OUTCOMES = ("matched", "new", "skipped")
_GRAPH_WRITE_STATUSES = (
    "written",
    "deferred_no_access",
    "write_rejected_by_policy",
    "write_failed",
)


def _validate_external_defect_graph_result_payload(payload: dict) -> None:
    """Refuse a malformed external_defect_graph_result at POST time (spec J.8.2).

    This record CLEARS the graph-result convergence gate, so a garbage payload (e.g. just
    ``{escalation_ref}``) must not count. Require a well-formed ledger: a valid
    ``dedup_outcome`` + ``write_status`` enum, an ``actor``, and — for a successful write —
    the ``node_id`` it produced. (That the ref points at a REAL prior external_defect is
    checked against the log in ``append_message``.)
    """
    problems = []
    if not _is_nonempty_str(payload.get("escalation_ref")):
        problems.append("`escalation_ref` must be a non-empty string")
    if payload.get("dedup_outcome") not in _GRAPH_DEDUP_OUTCOMES:
        problems.append(f"`dedup_outcome` must be one of {_GRAPH_DEDUP_OUTCOMES}")
    if payload.get("write_status") not in _GRAPH_WRITE_STATUSES:
        problems.append(f"`write_status` must be one of {_GRAPH_WRITE_STATUSES}")
    if not payload.get("actor"):
        problems.append("missing `actor`")
    # dedup_outcome × write_status consistency — a result that CLEARS the gate must be
    # internally coherent about whether a durable graph target exists:
    outcome = payload.get("dedup_outcome")
    write_status = payload.get("write_status")
    # a matched result always names the node it matched (even if the link write later failed)
    if outcome == "matched" and not _is_nonempty_str(payload.get("reference_id")):
        problems.append("`dedup_outcome: matched` requires a non-empty string `reference_id`")
    # a SUCCESSFUL write must be a real write to a durable target; `skipped` never wrote one
    if write_status == "written":
        if outcome == "skipped":
            problems.append(
                "`dedup_outcome: skipped` is incompatible with `write_status: written` — a "
                "skipped write produced no durable graph target"
            )
        elif outcome == "new" and not _is_nonempty_str(payload.get("node_id")):
            problems.append(
                "`dedup_outcome: new` + `write_status: written` requires a non-empty "
                "string `node_id`"
            )
    if problems:
        raise InvalidMessagePayloadError(
            "malformed external_defect_graph_result: " + "; ".join(problems)
        )


_COVERAGE_ROW_KINDS = ("code", "markdown", "element", "divergence", "blind_edge", "block")
_COVERAGE_GRANULARITIES = ("file", "symbol")
#: The union of both modes' verdict domains at the PAYLOAD screen; which half a row may
#: actually use is the MANIFEST's mode, enforced where the manifest is in hand (B.14 B-3):
#: code mode is `reviewed-clean | finding | cannot_reach | instrument_failure` (the retired
#: `not-reached` is refused there, B-7); spec mode keeps today's three, untouched.
_COVERAGE_VERDICTS = (
    "reviewed-clean", "finding", "not-reached", "cannot_reach", "instrument_failure",
)
#: The typed failure outcomes of B.14 B-6 — each valid only with a stated reason.
_TYPED_FAILURE_VERDICTS = ("cannot_reach", "instrument_failure")
#: The blind edge is an ORDINARY row with a fixed literal id — never a sibling object
#: beside `rows`. Putting it outside the row list would let the one row that declares what
#: the manifest CANNOT see sit outside the verdict path, outside the report, and therefore
#: outside the convergence gate: the only part of the manifest nobody must answer for.
_BLIND_EDGE_ROW_ID = "hunt-by-name"
#: An input fingerprint is a truncated sha256 — 16 lowercase hex characters. Checking the
#: SHAPE and not merely non-emptiness is what makes the validator's own message true: a
#: placeholder like "x" would otherwise satisfy a rule that says "a digest or null". Be
#: clear about what this buys, since the stronger reading is tempting and wrong: it catches
#: a tool emitting the wrong thing, not a party deliberately emitting a plausible-but-wrong
#: digest — no format check can do the latter, and nothing here pretends to.
_COVERAGE_DIGEST = re.compile(r"[0-9a-f]{16}")


def _validate_coverage_manifest_payload(payload: dict, *, config: dict | None = None) -> None:
    """Refuse a malformed coverage manifest at POST time (B.6 T1-4 / F-2).

    The manifest is the review's coverage DENOMINATOR, so a malformed one is not a cosmetic
    problem: the report is validated against it, and the convergence gate counts its rows.
    Row ids must be unique (they are set keys in the gate) and exactly one ``blind_edge``
    row must be present — a manifest that presents itself as complete is more dangerous
    than no manifest at all.
    """
    problems = []
    if not _is_int(payload.get("artifact_seq")):
        problems.append("`artifact_seq` is required and must be an integer (version anchor)")
    for field in ("manifest_id", "tool_version", "base", "commit"):
        if not _is_nonempty_str(payload.get(field)):
            problems.append(f"`{field}` must be a non-empty string")
    # B.10 A-1: the contract-generation gate travels with the strict ref helper to
    # EVERY field it validates — the manifest's base/commit identity included (finding
    # b10-legacy-manifest-ref-validation-gate): a globally strict helper would refuse
    # the next manifest an open legacy review is owed (the bd787de7 journal itself
    # carries a legacy manifest with an abbreviated base as the reference form).
    if artifact_contract_current(config):
        problems += _strict_ref_problems(
            payload.get("base"), payload.get("commit"), where="manifest"
        )
    if payload.get("granularity") not in _COVERAGE_GRANULARITIES:
        problems.append(f"`granularity` must be one of {_COVERAGE_GRANULARITIES}")
    # The derivation must be the CURRENT one. A manifest built by an older tool version
    # hashes correctly against its own content, so self-consistency proves nothing about
    # whether it was derived the way this review is supposed to derive it — the version is
    # in the payload precisely so a skew shows up as a refusal rather than as two parties
    # silently disagreeing about what the denominator means.
    if coverage_contract_current(config):
        allowed_tool_versions = {coverage.TOOL_VERSION}
    elif payload.get("mode") == "code":
        # The question denominator changed the CODE derivation only: a legacy code
        # review finishes under its creation-time place denominator, and adopting the
        # block contract mid-review would change the semantics mid-measurement.
        allowed_tool_versions = {coverage.TOOL_VERSION_LEGACY}
    else:
        # The spec derivation is unchanged between the generations, and an in-flight
        # legacy review's pinned machinery may be either — refuse neither.
        allowed_tool_versions = {coverage.TOOL_VERSION_LEGACY, coverage.TOOL_VERSION}
    if payload.get("tool_version") not in allowed_tool_versions:
        problems.append(
            f"`tool_version` is {payload.get('tool_version')!r} but this review's frozen "
            f"coverage contract accepts {sorted(allowed_tool_versions)!r} — a manifest "
            "from another derivation is internally consistent and still the wrong "
            "denominator (an in-flight review finishes under its creation-time contract)"
        )
    if payload.get("mode") not in _ARTIFACT_MODES:
        problems.append(f"`mode` must be one of {_ARTIFACT_MODES}, got {payload.get('mode')!r}")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        problems.append("`rows` must be a non-empty list (every manifest has a blind edge)")
    else:
        seen: set[str] = set()
        blind_edges = 0
        for r in rows:
            if not isinstance(r, dict):
                problems.append("each manifest row must be an object")
                continue
            row_id = r.get("row_id")
            if not _is_nonempty_str(row_id):
                problems.append("each manifest row requires a non-empty string `row_id`")
            elif row_id in seen:
                problems.append(f"duplicate manifest row_id {row_id!r}")
            elif _is_nonempty_str(row_id):
                seen.add(row_id)
            if r.get("kind") not in _COVERAGE_ROW_KINDS:
                problems.append(
                    f"manifest row `kind` must be one of {_COVERAGE_ROW_KINDS}, "
                    f"got {r.get('kind')!r}"
                )
            if not _is_nonempty_str(r.get("locator")):
                problems.append("each manifest row requires a non-empty string `locator`")
            if "high_stakes" in r and not isinstance(r.get("high_stakes"), bool):
                problems.append("manifest row `high_stakes` must be a boolean when present")
            if "out_of_declared_scope" in r and not isinstance(
                r.get("out_of_declared_scope"), bool
            ):
                problems.append(
                    "manifest row `out_of_declared_scope` must be a boolean when present"
                )
            if r.get("kind") == "blind_edge":
                blind_edges += 1
                if row_id != _BLIND_EDGE_ROW_ID:
                    problems.append(
                        f"the blind-edge row must carry the fixed id {_BLIND_EDGE_ROW_ID!r}, "
                        f"got {row_id!r}"
                    )
        if blind_edges != 1:
            problems.append(
                f"a manifest carries EXACTLY ONE `blind_edge` row (got {blind_edges}) — it "
                "is how the manifest declares its own blind edge, and it is never omitted"
            )
        # B.14 B-2/B-3 form checks — what the server can hold without the repository
        # (mechanical completeness against the diff is the TOOL's refusal, at build).
        # A code-mode manifest carries its COMPLETE slicing, and the block-axis rows
        # must be exactly two per block — a slicing and a row set that disagree would
        # let a block exist with no verdict axes, outside the convergence gate.
        if payload.get("mode") == "code" and coverage_contract_current(config):
            blocks = payload.get("blocks")
            if not isinstance(blocks, list) or not blocks:
                problems.append(
                    "a code-mode manifest carries its complete slicing: a non-empty "
                    "`blocks` list of {block_id, claim, refs, high_stakes} (B.14 B-2)"
                )
            else:
                block_ids: list[str] = []
                for b in blocks:
                    if not isinstance(b, dict) or not _is_nonempty_str(b.get("block_id")):
                        problems.append(
                            "each manifest block must be an object with a non-empty "
                            "string `block_id`"
                        )
                        continue
                    block_ids.append(b["block_id"])
                    if not _is_nonempty_str(b.get("claim")):
                        problems.append(
                            f"block {b['block_id']!r} carries no prose `claim` — a block "
                            "IS one coherent claim of the change (B.14 B-2)"
                        )
                    if not isinstance(b.get("refs"), list) or not b.get("refs"):
                        problems.append(
                            f"block {b['block_id']!r} carries no changed-line `refs`"
                        )
                if len(set(block_ids)) != len(block_ids):
                    problems.append("duplicate block_id in the manifest slicing")
                expected_axis_rows = {
                    f"{bid}::{axis}" for bid in block_ids for axis in ("change", "seam")
                }
                actual_axis_rows = {
                    r.get("row_id")
                    for r in rows
                    if isinstance(r, dict) and r.get("kind") == "block"
                }
                if expected_axis_rows != actual_axis_rows:
                    problems.append(
                        "the block-axis rows must be exactly `<block_id>::change` and "
                        "`<block_id>::seam` for each block of the slicing (B.14 B-3); "
                        f"missing {sorted(expected_axis_rows - actual_axis_rows)}, "
                        f"unexpected {sorted(actual_axis_rows - expected_axis_rows)}"
                    )
                # Round 10, finding b14-high-stakes-block-flag-not-bound-to-slicing:
                # the carried block flag and the axis-row flags are ONE projection of
                # the declared list — two independent copies would let the
                # critic-facing slicing metadata disagree with the server's evidence
                # gate (the rows ride manifest_id; the carried flag would not).
                row_flags = {
                    r.get("row_id"): bool(r.get("high_stakes"))
                    for r in rows
                    if isinstance(r, dict) and r.get("kind") == "block"
                }
                for b in blocks:
                    if not isinstance(b, dict) or not _is_nonempty_str(b.get("block_id")):
                        continue
                    if not isinstance(b.get("high_stakes"), bool):
                        problems.append(
                            f"block {b['block_id']!r} requires a boolean `high_stakes` "
                            "flag — the computed projection of the declared list rides "
                            "the slicing (B.14 B-2)"
                        )
                        continue
                    for axis in ("change", "seam"):
                        axis_row = f"{b['block_id']}::{axis}"
                        if (
                            axis_row in row_flags
                            and row_flags[axis_row] != b["high_stakes"]
                        ):
                            problems.append(
                                f"block {b['block_id']!r} carries high_stakes="
                                f"{b['high_stakes']} but its axis row {axis_row!r} "
                                f"carries {row_flags[axis_row]} — the flag is one "
                                "computed projection, not two copies (round 10, "
                                "finding b14-high-stakes-block-flag-not-bound-to-"
                                "slicing)"
                            )
        elif payload.get("blocks") is not None:
            problems.append(
                "`blocks` is the slicing of a CURRENT-contract code manifest (B.14 "
                "B-1) — a spec-mode manifest does not carry one, and a legacy-contract "
                "review finishes under its creation-time place denominator"
            )
    # Inputs that do NOT come from the ref pair — the spec body, the declared scope, the
    # high-stakes list — are fingerprinted so a divergence between two honest re-runs is
    # attributable rather than mysterious. Required in BOTH modes: `high_stakes` is a
    # non-ref input in code mode too, since it decides which rows demand a cited observation
    # behind a clean claim.
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict) or not all(
        key in inputs for key in ("spec_markdown", "declared_scope", "high_stakes")
    ):
        problems.append(
            "a manifest requires an `inputs` object fingerprinting the non-ref-pair inputs "
            "(`spec_markdown`, `declared_scope`, `high_stakes`) — without them a re-run "
            "that differs cannot be attributed to anything"
        )
    else:
        # Shape is not substance. Each value must be a real digest or an explicit null
        # meaning "not supplied"; anything else is a fingerprint present in form while
        # carrying nothing, which is the same decoration this check was added to replace.
        for key, value in inputs.items():
            # `isinstance` first: coercing with `str()` let a JSON number whose text happens
            # to be sixteen digits satisfy a rule that says "a digest STRING or null".
            if value is not None and not (
                isinstance(value, str) and _COVERAGE_DIGEST.fullmatch(value)
            ):
                problems.append(
                    f"`inputs.{key}` must be a digest string or null (not supplied), "
                    f"got {value!r}"
                )
        # B.14 B-8: the slicing is a non-ref input exactly like the high-stakes list —
        # a code-mode manifest without its fingerprint cannot attribute a differing
        # re-run to the input that moved.
        if (
            payload.get("mode") == "code"
            and coverage_contract_current(config)
            and not _is_nonempty_str(inputs.get("blocks"))
        ):
            problems.append(
                "a code-mode manifest fingerprints its slicing: `inputs.blocks` must "
                "carry a digest (B.14 B-8)"
            )
        # Round 9, finding b14-carried-slicing-not-bound-to-manifest-id: the carried
        # slicing must hash to the fingerprint that participates in manifest_id —
        # symmetric with the high-stakes carry below.
        if (
            payload.get("mode") == "code"
            and isinstance(payload.get("blocks"), list)
            # Round 10, b14-malformed-block-manifest-crashes-validator: a non-object
            # entry is already recorded as a problem above — hashing it would turn the
            # documented refusal into an AttributeError 500.
            and all(isinstance(b, dict) for b in payload["blocks"])
        ):
            if coverage.slicing_digest(payload["blocks"]) != inputs.get("blocks"):
                problems.append(
                    "the carried `blocks` slicing does not hash to `inputs.blocks` — "
                    "the slicing on the wire and the fingerprinted input must be the "
                    "same cut"
                )
        # Finding b14-code-high-stakes-input-not-reproducible: the declared list must
        # RIDE the code manifest (the flags are a lossy projection of it, so a critic
        # cannot re-derive without it) and must MATCH the digest it claims — a carried
        # list that hashes differently is a silently replaced enforcement input.
        if payload.get("mode") == "code" and payload.get("blocks") is not None:
            declared = payload.get("high_stakes_declared")
            if not isinstance(declared, list) or not all(
                isinstance(h, str) for h in declared
            ):
                problems.append(
                    "a code-mode manifest carries `high_stakes_declared`: the declared "
                    "high-stakes locator list, verbatim — the computed block flags are "
                    "not recoverable into it (B.14, finding "
                    "b14-code-high-stakes-input-not-reproducible)"
                )
            elif coverage._digest(declared) != inputs.get("high_stakes"):
                problems.append(
                    "`high_stakes_declared` does not hash to `inputs.high_stakes` — "
                    "the carried list and the fingerprinted input must be the same list"
                )
        # In spec mode the body is a REQUIRED input, so a null there is not "not supplied" —
        # it is a missing fingerprint for the one input that always exists.
        if payload.get("mode") == "spec" and not _is_nonempty_str(inputs.get("spec_markdown")):
            problems.append(
                "a spec-mode manifest is always derived from a spec body, so "
                "`inputs.spec_markdown` must carry its digest"
            )
        # THE HIGH-STAKES LIST MUST BE DECLARED, EMPTY OR NOT. Everywhere else in this design
        # silence means the pessimistic thing — an omitted row reads as unreached, an absent
        # manifest reads as owed. Here it read as the OPTIMISTIC one: a null digest meant
        # "nothing is load-bearing", which is exactly what a forgotten `--high-stakes-file`
        # also produces, and the two were indistinguishable. The rows that most need a cited
        # observation then accepted a bare `reviewed-clean`.
        #
        # An empty declaration is a legitimate answer and is already distinguishable: the
        # digest of an empty list is a digest, while "not supplied" is null. So this asks
        # development to say which it meant, and nothing more.
        if not _is_nonempty_str(inputs.get("high_stakes")):
            problems.append(
                "`inputs.high_stakes` must carry a digest: the high-stakes list is declared "
                "even when it is EMPTY, because a forgotten list and a considered 'none' are "
                "otherwise the same payload. Pass --high-stakes-file (an empty file is the "
                "explicit 'nothing here is load-bearing')"
            )
    # THE IDENTITIES MUST BE THE REAL ONES, not opaque strings the poster chose. Everything
    # downstream keys on them: the report answers a `manifest_id`, the gate and the watcher
    # decide "is this denominator answered" by comparing it, and the late-surfacing
    # diagnostic matches rows across versions by `row_id`. Left unchecked, a re-posted
    # manifest could change every row while reusing the old id, and each of those checks
    # would go on agreeing about a table that had silently been replaced.
    if not problems:
        for r in rows:
            if r.get("kind") == "blind_edge":
                continue
            expected = coverage.row_id_for(r["kind"], r["locator"])
            if r["row_id"] != expected:
                problems.append(
                    f"row_id {r['row_id']!r} is not the content-addressed id for "
                    f"({r['kind']}, {r['locator']}) — expected {expected!r}; row ids are "
                    "derived from the locator, never chosen"
                )
        expected_manifest_id = coverage.manifest_id_for(payload)
        if payload.get("manifest_id") != expected_manifest_id:
            problems.append(
                f"manifest_id {payload.get('manifest_id')!r} does not match this manifest's "
                f"content (expected {expected_manifest_id!r}) — every downstream check "
                "compares that id, so a reused one would let a replaced table pass as the "
                "same denominator"
            )
    if problems:
        raise InvalidMessagePayloadError("malformed coverage_manifest: " + "; ".join(problems))


def _validate_coverage_report_payload(payload: dict) -> list[dict]:
    """Screen a coverage report at POST time (B.6 T1-4 / F-2; B.8 A-1 partial acceptance).

    PAYLOAD-level problems (missing anchor or `manifest_id`, a malformed `unanswerable`
    declaration, `rows` not a list) refuse the WHOLE message, as before — they are not
    row defects. ROW-level problems no longer void the report: each invalid row is
    REMOVED from ``payload["rows"]`` and returned as ``{"row_id", "reason"}`` for the
    response's ``rejected_rows`` echo. A rejected row gets NO verdict — the stored
    report simply never mentions it, so it stays unreported and the ordinary
    not-reached machinery resurfaces it (A-1; operator ruling, round 1, settling Q-1).
    A report whose rows ALL fail is still a refusal — enforced by the caller AFTER the
    manifest screen, because prefix normalisation (A-2) can still save rows this screen
    knows nothing about.

    Rows are stored as identifiers and status — no prose — with three defined carve-outs,
    each enforced here because each is the row's only evidence:

    - the **blind-edge row carries `searches_performed` under EVERY verdict**, including
      `reviewed-clean`. Without it that row would assert the blind edge was covered with no
      evidence anything was searched — the one row whose entire value is the evidence;
    - **every `not-reached` row carries its `reason`**: the reason is not commentary, it is
      what the gate routes on and what the operator reads when a row survives two passes;
    - a `finding` verdict names the finding, or the row claims a defect nobody can locate.
    """
    problems = []
    if not _is_int(payload.get("artifact_seq")):
        problems.append("`artifact_seq` is required and must be an integer (version anchor)")
    if not _is_nonempty_str(payload.get("manifest_id")):
        problems.append("`manifest_id` must be a non-empty string (which denominator this answers)")
    if "unanswerable" in payload:
        # THE PASS'S NON-STRANDING EXIT. A report that cannot be produced row by row is
        # declared as such, in one field, and the pass ends. Without it a pass whose report
        # the server refuses can neither end nor say why — ending is itself gated on the
        # report — so the pass strands, its findings hang unterminated and the review stops
        # dead with no live counterpart (live: review b0ef1b30, where the critic mistyped one
        # 12-character row id out of 278 and killed the whole pass). The declaration is loud
        # rather than convenient: it claims coverage of NOTHING, it is disclosed in the
        # post-review summary like any other row, and `converged` is refused over it.
        if not _is_nonempty_str(payload.get("unanswerable")):
            problems.append(
                "`unanswerable` must be a non-empty string saying why the report could not be "
                "produced — an unexplained one is a silent coverage hole, which is what the "
                "whole mechanism exists to prevent"
            )
        if payload.get("rows"):
            problems.append(
                "an `unanswerable` coverage_report carries no rows: it claims coverage of "
                "nothing. Post verdicts or declare the report unanswerable, never both"
            )
        if problems:
            raise InvalidMessagePayloadError("malformed coverage_report: " + "; ".join(problems))
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        problems.append("`rows` must be a list")
    if problems:
        raise InvalidMessagePayloadError("malformed coverage_report: " + "; ".join(problems))

    rejected: list[dict] = []
    kept: list[dict] = []
    seen: set[str] = set()

    def _reject(row_id, reason: str) -> None:
        rejected.append({"row_id": row_id, "reason": reason})

    for r in rows:
        if not isinstance(r, dict):
            _reject(None, "each report row must be an object")
            continue
        row_id = r.get("row_id")
        if not _is_nonempty_str(row_id):
            _reject(row_id, "each report row requires a non-empty string `row_id`")
            continue
        if row_id in seen:
            _reject(row_id, f"duplicate report row_id {row_id!r} — one verdict per row")
            continue
        seen.add(row_id)
        verdict = r.get("verdict")
        if verdict not in _COVERAGE_VERDICTS:
            _reject(
                row_id,
                f"report row `verdict` must be one of {_COVERAGE_VERDICTS}, got {verdict!r}",
            )
            continue
        if verdict == "finding" and not _is_nonempty_str(r.get("finding_id")):
            _reject(row_id, "a `finding` verdict requires a non-empty `finding_id`")
            continue
        if verdict == "not-reached" and not _is_nonempty_str(r.get("reason")):
            _reject(
                row_id,
                "every `not-reached` verdict requires a `reason` — it is what the "
                "convergence gate routes on and what the operator reads when the row "
                "survives two passes",
            )
            continue
        # B.14 B-3/B-6: the typed failure outcomes are valid only with a stated reason —
        # "cannot reach, HERE IS WHY" is the whole distinction from "have not gotten to
        # it yet", and an instrument failure without its fault names no remedy.
        if verdict in _TYPED_FAILURE_VERDICTS and not _is_nonempty_str(r.get("reason")):
            _reject(row_id, f"every `{verdict}` verdict requires a `reason` (B.14 B-6)")
            continue
        if row_id == _BLIND_EDGE_ROW_ID:
            searches = r.get("searches_performed")
            if not (
                isinstance(searches, list)
                and searches
                and all(_is_nonempty_str(s) for s in searches)
            ):
                _reject(
                    row_id,
                    "the blind-edge row requires a non-empty `searches_performed` list of "
                    "the name/key searches actually run — under EVERY verdict, including "
                    "reviewed-clean",
                )
                continue
        kept.append(r)
    payload["rows"] = kept
    return rejected


#: B.8 F-2: the ONE evidence-shape implementation lives beside the grounds registry —
#: the coverage-report row and the claim-map confirmation cite the same schema, and two
#: copies of a schema drift.
_evidence_shape_problems = round_gate.evidence_shape_problems


def _validate_coverage_report_against_manifest(
    payload: dict, manifest: dict | None
) -> tuple[list[dict], list[dict], list[dict]]:
    """Screen a report against the manifest it answers (needs the log; B.8 A-1/A-2).

    WHOLE-refusals stay whole, because they are not per-row defects: the manifest must
    exist for this artifact version, and the report must answer *that* manifest — a
    ``manifest_id`` mismatch is a report answering a different denominator entirely.

    ROW-level screening (returns ``(rejected_rows, normalised_rows)``, mutating
    ``payload["rows"]`` to the accepted set):

    - LEGACY place manifests only (no slicing): a ``row_id`` absent from the manifest
      but a PREFIX of exactly one manifest row id is accepted under the manifest's id,
      echoed in ``normalised_rows`` (``{reported, accepted}``) — THOSE row ids are
      opaque 12-hex tokens copied by hand across a model boundary, and a unique prefix
      carries no ambiguity (A-2). Under a slicing-carrying manifest the block-axis ids
      are literal ``<block_id>::<axis>`` identities and an unknown id is refused
      outright, never recovered (B.14 B-4; round 11);
    - an absent id with zero or two-plus prefix matches is rejected (ambiguity is real);
    - a bare ``reviewed-clean`` on a high-stakes row is rejected — only the manifest
      knows which rows are high-stakes, and on those a bare clean claim is the cheapest
      claim there is. The rejected row stays unreported (A-1);
    - a high-stakes ``reviewed-clean`` whose typed ``evidence`` block is missing or
      malformed (B.8 F-2) is DOWNGRADED, not rejected: the row is recorded
      ``not-reached`` with reason ``instrument_failure`` — no verdict is invented, the
      contract's three verdicts stand, and the downgrade is echoed in
      ``downgraded_rows``. Returns ``(rejected_rows, normalised_rows,
      downgraded_rows)``.
    """
    if manifest is None:
        raise InvalidMessagePayloadError(
            f"coverage_report for artifact_seq {payload.get('artifact_seq')!r} references no "
            "posted coverage_manifest for that version — the denominator must exist before "
            "coverage can be claimed against it"
        )
    if payload.get("manifest_id") != manifest.get("manifest_id"):
        raise InvalidMessagePayloadError(
            f"coverage_report names manifest_id {payload.get('manifest_id')!r} but the "
            f"manifest posted for artifact_seq {payload.get('artifact_seq')!r} is "
            f"{manifest.get('manifest_id')!r} — a report must answer the current denominator"
        )
    # B.12 C-4: THE COUNT AND ITS GRANULARITY TRAVEL TOGETHER, from here on. The depth
    # tier changes what a coverage ROW IS — a file or a top-level section at `light`, a
    # symbol on the changed surface at `standard` and `deep` — so "5 of 5" and "13 of 113"
    # can describe the same reading, and the number is what a human remembers. The value is
    # stamped by the SERVER, taken from the manifest it has just held this report against,
    # rather than asked of the poster: a granularity the report declared about itself could
    # disagree with the denominator it was computed over, which is the one thing this pair
    # exists to prevent.
    payload["granularity"] = manifest.get("granularity")
    rows_by_id = {
        r.get("row_id"): r
        for r in (manifest.get("rows") or [])
        if isinstance(r, dict)
    }
    rejected: list[dict] = []
    normalised: list[dict] = []
    downgraded: list[dict] = []
    kept: list[dict] = []
    # Identity screen first, so the high-stakes screen below reads the row's REAL manifest
    # identity — including one recovered from a unique prefix.
    taken = {r.get("row_id") for r in payload.get("rows") or [] if r.get("row_id") in rows_by_id}
    for r in payload.get("rows") or []:
        row_id = r.get("row_id")
        manifest_row = rows_by_id.get(row_id)
        if manifest_row is None:
            # Round 11, finding b14-block-axis-prefix-normalization: prefix recovery
            # exists for the OPAQUE 12-hex place-row ids copied by hand across a model
            # boundary. Block-axis ids are literal `<block_id>::<axis>` identities —
            # B-4 refuses an unknown identity outright, because `b1::c` silently
            # becoming `b1::change` is exactly the imprecision the literal form
            # retired.
            if _question_denominator(manifest):
                rejected.append(
                    {
                        "row_id": row_id,
                        "reason": (
                            f"row {row_id!r} is not in manifest "
                            f"{manifest.get('manifest_id')!r} — block-axis row ids "
                            "are literal `<block_id>::<axis>` identities; no prefix "
                            "recovery under a slicing-carrying manifest (B.14 B-4)"
                        ),
                    }
                )
                continue
            matches = [mid for mid in rows_by_id if isinstance(mid, str) and mid.startswith(row_id)]
            if len(matches) == 1:
                accepted_id = matches[0]
                if accepted_id in taken:
                    rejected.append(
                        {
                            "row_id": row_id,
                            "reason": (
                                f"row {row_id!r} normalises to {accepted_id!r}, which this "
                                "report already verdicts — one verdict per row"
                            ),
                        }
                    )
                    continue
                taken.add(accepted_id)
                r["row_id"] = accepted_id
                normalised.append({"reported": row_id, "accepted": accepted_id})
                manifest_row = rows_by_id[accepted_id]
            else:
                ambiguity = (
                    f" (an ambiguous prefix of {len(matches)} manifest rows)" if matches else ""
                )
                rejected.append(
                    {
                        "row_id": row_id,
                        "reason": (
                            f"row {row_id!r} is not in manifest "
                            f"{manifest.get('manifest_id')!r}{ambiguity}"
                        ),
                    }
                )
                continue
        # B.14 B-3/B-7: the verdict domain follows the DENOMINATOR the manifest
        # carries, not the mode alone. Under a block manifest the retired `not-reached`
        # is refused — an axis a pass has not gotten to is simply left unanswered
        # (within-version accumulation); a spec manifest AND a legacy v1 code manifest
        # (an in-flight pre-B.14 review, spec constraint 3) keep today's machinery
        # untouched, typed failures refused.
        if _question_denominator(manifest) and r.get("verdict") == "not-reached":
            rejected.append(
                {
                    "row_id": r.get("row_id"),
                    "reason": (
                        "`not-reached` is retired in code mode (B.14 B-7): state "
                        "`cannot_reach` with why, `instrument_failure` with what failed, "
                        "or leave the axis to a later pass — accumulation covers it"
                    ),
                }
            )
            continue
        if not _question_denominator(manifest) and r.get("verdict") in _TYPED_FAILURE_VERDICTS:
            rejected.append(
                {
                    "row_id": r.get("row_id"),
                    "reason": (
                        f"`{r.get('verdict')}` is an outcome of the B.14 question "
                        "denominator (B-3) — this manifest carries no slicing (a spec "
                        "denominator, or a legacy pre-B.14 code review finishing under "
                        "its own contract), so `not-reached` remains its unreached form"
                    ),
                }
            )
            continue
        if manifest_row.get("high_stakes") is True and r.get("verdict") == "reviewed-clean":
            if not _is_nonempty_str(r.get("observation")):
                rejected.append(
                    {
                        "row_id": r.get("row_id"),
                        "reason": (
                            f"row {r.get('row_id')!r} is high-stakes: a `reviewed-clean` "
                            "verdict must cite a specific checked `observation` (a bare "
                            "clean claim is the cheapest claim there is)"
                        ),
                    }
                )
                continue
            # B.8 F-2: the confirmation must carry typed evidence of READING. A failing
            # one is mechanically DOWNGRADED to `not-reached` with reason
            # `instrument_failure` — recorded, not rejected: the row was looked at, but
            # the instrument did not prove the look, and inventing a verdict either way
            # is what this element removes.
            #
            # THE SERVER'S HALF STOPS AT FORM — a named boundary, not an omission
            # (finding b8-direct-coverage-evidence-bypasses-watcher-verification). The
            # repository under review lives on the development box; this server cannot
            # re-read it, so holding the quote against the actual read belongs to the
            # transport tract that can (the reference watcher, which marks the rows it
            # verified with `evidence_verification: content_reread_by_watcher`). A
            # binding that posts directly, bypassing that tract, is the same actor as
            # the watcher under this loop's recorded trust model (roles are
            # prompt-maintained, option X) — transport authentication is the recorded
            # revisit point, not this validator's job.
            shape_problems = _evidence_shape_problems(r.get("evidence"))
            if shape_problems:
                downgraded.append(
                    {"row_id": r.get("row_id"), "reason": "; ".join(shape_problems)}
                )
                # B.14 B-3: the downgrade lands in the DENOMINATOR's own domain — the
                # typed `instrument_failure` verdict under a block manifest, the
                # reason-classified `not-reached` under a spec or legacy code one.
                if _question_denominator(manifest):
                    kept.append(
                        {
                            "row_id": r.get("row_id"),
                            "verdict": "instrument_failure",
                            "reason": (
                                "a high-stakes `reviewed-clean` without valid typed "
                                "evidence of reading: " + "; ".join(shape_problems)
                            ),
                        }
                    )
                else:
                    kept.append(
                        {
                            "row_id": r.get("row_id"),
                            "verdict": "not-reached",
                            "reason": (
                                "instrument_failure — a high-stakes `reviewed-clean` without "
                                "valid typed evidence of reading: " + "; ".join(shape_problems)
                            ),
                        }
                    )
                continue
        kept.append(r)
    payload["rows"] = kept
    return rejected, normalised, downgraded


_EXTERNAL_DEFECT_ACTIONS = ("freeze", "defer", "reject")


#: B.11, critic finding `semantic-map-origin-unbound` (round 2 of this slice's own
#: review): the kinds this slice introduces whose MEANING depends on who wrote them, and
#: the author each one claims.
#:
#: WHAT THIS IS NOT: authorization. Role is not an authorization input anywhere in this
#: service and is not authenticated — a token's only job is isolation (see this module's
#: docstring). The critic proposed binding the actor to the authenticated token; that
#: would be a system-wide change to a recorded decision, smuggled in through one slice,
#: and it would leave a service where three kinds of twenty are authorized. The recorded
#: revisit condition for that decision — the loop running unattended, untrusted content,
#: or a multi-user repository — has not arrived, and this review's frozen threat model
#: puts the adversary such a check would need explicitly out of scope.
#:
#: WHAT IT IS: a consistency check on the LABEL, in the same shape as the three the
#: server already performs (a `dev_observation` is development's, the threat-boundary
#: marker and the degraded-grounds permission are the operator's). The failure it catches
#: is a client bug, not an attacker: a `map` posted under a development label would be
#: exactly the self-checking reading the map pass exists to prevent, and it would suppress
#: the real map's scheduling on top of that.
#:
#: A TABLE RATHER THAN THREE CHECKS, deliberately. A kind missing its author is then
#: visible in one place by inspection, which is what makes this a class closure rather
#: than three fixes that the fourth kind will not inherit.
AUTHOR_BOUND_KINDS: dict[str, str] = {
    "map": "critic",
    "reconciliation": "critic",
    "map_disposition": "operator",
    "operator_projection": "development",
}

#: What each author-bound kind IS, said in the refusal — a bare "wrong role" leaves the
#: author guessing which half of the message is wrong.
_AUTHOR_BOUND_WHY: dict[str, str] = {
    "map": (
        "a map is the CRITIC's independent reading of the final code; a map authored by "
        "development is the self-description the pass exists to replace"
    ),
    "reconciliation": (
        "a reconciliation is the CRITIC's comparison of the map against the cycle's "
        "intents"
    ),
    "map_disposition": (
        "a map disposition carries the OPERATOR's own ruling and their verbatim words; "
        "development relays it as an operator message, it never authors one"
    ),
    "operator_projection": (
        "a projection is DEVELOPMENT's human rendering of an item the loop is putting to "
        "the operator; judging how to explain something is a semantic act, and neither the "
        "server nor the critic performs one on the operator's behalf here"
    ),
}


#: B.11 E-8 — the operator's three ways of disposing of a map. `accepted`: finalization
#: proceeds. `new_cycle`: another development cycle starts at once. `task_in_graph`: the
#: work is landed as a graph task, which carries references to BOTH the review and the map
#: it came from — without that provenance the task exists a month later with no way back to
#: what raised it.
MAP_OUTCOMES = ("accepted", "new_cycle", "task_in_graph")

#: B.11 F-1 — a reconciliation attempt either produced its list or failed saying why. The
#: two shapes are validated SYMMETRICALLY: what one requires the other refuses, so a
#: `failed` attempt cannot smuggle entries and a `produced` one cannot be empty. A failed
#: attempt is a SPENT attempt (F-1) — that is the whole reason it has to be recordable.
RECONCILIATION_OUTCOMES = ("produced", "failed")

#: B.12 B-1 — SIX categories, because they are six different things. They are derived by
#: asking three questions of each observation — is it in the code; did any intent assert
#: it; was it SETTLED in the free phases — and the ORDER of the tuple is the selection
#: procedure, first match wins (see `_reconciliation_category_order` below).
#:
#: `contradicted`        — an intent asserted something, and the code does something else.
#: `promised_absent`     — an intent asserted something, and the code does not contain it.
#: `stated_not_surfaced` — in the code, in an intent, therefore signed off, reported anyway.
#: `decided_in_talk_only`— in the code, SETTLED in the free phases, written down by no intent.
#: `silent`              — in the code and absent from EVERY document of the cycle.
#: `dropped_in_talk`     — absent from the code and every intent, and SETTLED in the talk.
#:
#: B.11 had three, and its `misreported` merged the first two — which are not one thing to
#: the reader: the first says the report was wrong, the second says something was dropped.
#: The last three are what the free-phase transcript's arrival in the read set (A-7, A-8)
#: makes expressible at all.
#:
#: THE LIST IS OPEN. Six because six cases were found, not because the report is built
#: around the number six. Operator, 2026-08-26: «если критик или ты еще какой-то вариант
#: найдете, то приноси мне - можно и расширить список». A seventh is added by an operator
#: decision, here and in the prompt, not by a pass improvising a label.
RECONCILIATION_LABELS = (
    "contradicted",
    "promised_absent",
    "stated_not_surfaced",
    "decided_in_talk_only",
    "silent",
    "dropped_in_talk",
)

#: B.12 B-1/B-8 — the two categories whose ground is a CONVERSATION rather than a document.
#: For them presence in the transcript is not enough: the quote must show the matter was
#: SETTLED, and the entry must say in one clause what in the quote makes it a settlement
#: rather than thinking aloud. A false entry here accuses the reader of breaking a promise
#: nobody gave, which is more expensive than a missed one.
RECONCILIATION_TALK_BORNE = ("decided_in_talk_only", "dropped_in_talk")

#: B.12 B-4 — the one category with nothing to quote from a document: `silent` means no
#: document of the cycle mentions the thing at all. Its evidence is the CODE, verbatim,
#: with a file location, and its "what was claimed" half is written out as explicitly
#: empty IN WORDS (`claimed_absent`) rather than left blank or filled with a paraphrase. A
#: blank reads as an omission by the pass; a paraphrase would be a fabricated promise, and
#: the whole force of this report rests on its quotes being real.
RECONCILIATION_CODE_BORNE = "silent"

#: B.12 B-3 — the cost of missing an entry, and the report's ONLY ordering. One question
#: asked of each entry: if the operator does not read this now, how and when will they find
#: out? `never` — nothing will announce it; `next_review` — it surfaces when someone next
#: works nearby; `self_announcing` — it breaks, refuses, or produces visibly wrong output.
#: Ordered here from most to least expensive; the report is ONE FLAT LIST in this order,
#: with no sections, and the server checks the order because a rule that lives only in the
#: prompt is a rule the prompt can lose (constraint 1 of the slice).
COST_OF_MISSING = ("never", "next_review", "self_announcing")

#: B.11 E-11 — the operator's request for a SECOND reconciliation attempt, riding an
#: existing `decision_response` rather than a fourth message kind. A fourth kind would have
#: to enter the model tuple, the database constraint and the gate's legality table, and it
#: would buy nothing this field does not. Re-exported from `round_gate`, where the
#: once-only fold that reads it lives, because the server and the watcher must not hold two
#: spellings of one wire field.
RECONCILIATION_RETRY_KEY = round_gate.RECONCILIATION_RETRY_KEY


def _validate_map_payload(payload: dict) -> None:
    """Refuse a malformed semantic map at POST time (B.11 E-1, E-4, E-8, E-9).

    ONE NAME FOR THE TARGET COMMIT, deliberately. E-8 lists "the target commit" and "the
    frozen base+commit pair" as if they were two things; they are one — the target commit
    of E-9 IS the `commit` half of the pair, and giving one fact two fields is how the
    coverage predicate ended up with three disagreeing copies. So the payload carries
    `base` + `commit`, and `commit` is the target commit.

    `modules` is the scope derived MECHANICALLY from that pair (E-4), not chosen by the
    pass: two maps of the same slice are meant to be comparable, which a pass-chosen scope
    would not be. The server checks that it is a non-empty list of names — it cannot
    re-derive the set without the repository, and says so rather than pretending to.
    """
    problems = []
    for field in ("base", "commit"):
        if not _is_nonempty_str(payload.get(field)):
            problems.append(
                f"a map requires a non-empty `{field}` — the slice's frozen pair is what "
                "its scope is derived from, and `commit` is the target commit itself"
            )
    if not _is_nonempty_str(payload.get("body_markdown")):
        problems.append(
            "a map requires a non-empty `body_markdown` — the map IS the document the "
            "operator reads; a map message without one is an announcement of nothing"
        )
    modules = payload.get("modules")
    if not isinstance(modules, list) or not modules:
        problems.append(
            "a map requires a non-empty `modules` list — the scope of the slice, derived "
            "from the base+commit pair rather than chosen by the pass (E-4)"
        )
    elif not all(_is_nonempty_str(m) for m in modules):
        problems.append("every entry of `modules` must be a non-empty string")
    if problems:
        raise InvalidMessagePayloadError("map: " + "; ".join(problems))


def _validate_map_disposition_payload(payload: dict) -> None:
    """Refuse a malformed operator ruling over a map (B.11 E-5, E-8).

    `map_seq` is required for EVERY outcome, not only for `task_in_graph`: the reference
    identifies WHICH material the operator had in front of them, and a ruling that names
    no map is a signature on an unnamed document.

    `reconciliation_seq` is optional and binds only what existed when the record was
    written — a disposition names the reconciliation if one had arrived and creates no
    obligation to wait for one that had not (E-8, F-4). Nothing here enforces an ordering,
    and that is a decision rather than an omission: the map is self-sufficient and the
    operator's own discipline holds the order (G-6).
    """
    problems = []
    outcome = payload.get("outcome")
    if outcome not in MAP_OUTCOMES:
        problems.append(f"`outcome` must be one of {list(MAP_OUTCOMES)}, got {outcome!r}")
    if not _is_int(payload.get("map_seq")):
        problems.append(
            "`map_seq` is required on every disposition — it names the map this ruling "
            "resolves"
        )
    if not _is_nonempty_str(payload.get("operator_words")):
        problems.append(
            "`operator_words` must carry the operator's own words verbatim — the ruling is "
            "theirs, and a relayed paraphrase of it is development speaking for them"
        )
    if "reconciliation_seq" in payload and not _is_int(payload.get("reconciliation_seq")):
        problems.append("`reconciliation_seq` must be an integer when present")
    if outcome == "task_in_graph":
        for field in ("task_node_id", "review_id"):
            if not _is_nonempty_str(payload.get(field)):
                problems.append(
                    f"a `task_in_graph` disposition requires `{field}` — a task raised this "
                    "way carries references to BOTH the review and the map it came from, or "
                    "a month later it exists with no way back to what raised it"
                )
    if problems:
        raise InvalidMessagePayloadError("map_disposition: " + "; ".join(problems))


#: B.12 C-1 — the four translated parts of a projection, in the order they are read. Each
#: is a separate field rather than one blob because an empty part is the failure mode this
#: element exists to prevent, and a form that accepts it guarantees nothing: a single
#: `translation` string is "non-empty" the moment any one of the four is written.
#:
#: `what_happened` — in ordinary words, naming the objects in terms the operator already
#: owns rather than in the loop's vocabulary; `concerns` — how much, of what, and where;
#: `proposal` — what development proposes, WITH its reason; `answers` — what each available
#: answer will actually do, stated as outcomes rather than as the names of wire fields.
PROJECTION_PARTS = ("what_happened", "concerns", "proposal", "answers")


def _validate_operator_projection_payload(payload: dict) -> None:
    """Refuse a malformed operator projection at POST time (B.12 C-1).

    THE ELEMENT THIS SERVES, in one sentence: no item routed to the operator may require
    reading a machine artifact to answer. The operator does not read what is written for
    machines — not a property of this operator but a DEFAULT of every install (constraint 4
    of the slice, seeded at C-7) — so a machine artifact either has a human projection or
    it never becomes the basis of a question to a human.

    **A PAIR, not a translation.** One record carries the machine item VERBATIM and its
    translation side by side. This replaced an earlier requirement that the projection
    stand EARLIER IN THE CHANNEL than the notification: that ordering was unobservable by
    construction — the notification is a chat message and the channel does not see chat, so
    one of the two events being compared had no record at all. The pair makes the ordering
    unnecessary rather than unverifiable: what the order protected (that the operator was
    not shown the raw item instead of an explanation) is carried by the record itself. And
    the pair has a second purpose, named by the operator when they proposed it — original
    beside translation is the material from which it later becomes visible which
    translations read and which do not.

    **The key is not a new identifier.** The server already computes exactly one key per
    OPEN operator item, namespaced by the item's kind — an escalation on its element or its
    finding, an external defect on its own id, a human question on its question id — and
    falling back to the raising message's sequence number for an item that carries no key
    of its own (`round_gate.open_operator_item_keys`). THAT value is what a projection
    carries. The fallback is what makes the field total: every item has a key, because an
    item with no key of its own has its seq.

    **What is NOT checked here, and where each is instead.** Membership of the key in the
    currently-open set is not required: the round gate is an operator item that exists as a
    message and has no entry in that fold, so a membership check would refuse exactly the
    projection the operator most needs. The join — every operator item to AT LEAST ONE
    projection written for that raising — is the acceptance check H-6 describes, run over
    the channel journal at the close of a review and, since round 1 of this slice's own
    review, on every read of the review's state. "At least one" rather than "exactly one"
    is the operator's amendment of 2026-08-27: a second translation appears when the first
    did not land and they asked again. And FAITHFULNESS is not checked at all, deliberately (G-14): nothing
    verifies that the carried original is faithful to the item or the translation to the
    original. The operator accepts that boundary in their own words: «сразу говорю, что
    несешь сообщение в канал ты. И я осознанно принимаю выбор доверять тебе». The boundary
    covers faithfulness only — that a projection exists, carries both halves, and has no
    empty part is mechanical, and it is this function.
    """
    problems = []
    if not _is_nonempty_str(payload.get("item_key")):
        problems.append(
            "`item_key` is required — the key of the operator item this projection "
            "explains, exactly as the server computes it for an open item (an element or "
            "finding id, an external-defect id, a question id, or the raising message's "
            "seq for an item with no key of its own). A projection keyed on nothing "
            "explains nothing in particular, and nothing can be joined to it afterwards"
        )
    # THE KEY ROUTES; THE SEQ ATTRIBUTES. Finding
    # `b12-projection-key-aliases-distinct-items` (round 2 of this slice's own
    # implementation review): the key is deliberately SHARED with the settle-per-key fold,
    # so two distinct raisings about the same finding — a contested disposition, and later a
    # contested fork — carry one key. Joining on the key alone then credits one projection
    # to both raisings, and the tail truthfully reports "every item carries its verbatim
    # original" while that original is verbatim for only one of them; the second question
    # went unexplained and nothing saw it.
    #
    # The remedy is NOT a second key. The critic proposed re-keying projections onto the
    # raising message or a digest of the machine item, which would give this loop two
    # hand-rolled notions of identity — the exact defect the "both folds agree what a key
    # is" test exists to prevent. The operator's observation is what made the cheap remedy
    # visible: a projection is ALREADY attributed to one original, by its own content. So
    # the record says WHICH message that original came from, and attribution becomes exact
    # while the routing key stays shared and unchanged.
    #
    # Required rather than optional, and required NOW: this message kind is not deployed
    # yet, so the field costs nothing today and would cost a migration tomorrow.
    # SHAPE ONLY, deliberately: this refuses a record that cannot name a raising, not one
    # that names the wrong raising. Resolving the seq and the key against the journal belongs
    # to the closing audit, which must do it anyway for channels this validator never saw.
    item_seq = payload.get("item_seq")
    if type(item_seq) is not int or item_seq < 1:
        problems.append(
            "`item_seq` is required and must be the positive sequence number of the "
            "message this projection explains — the key routes (it is shared with the fold "
            "that settles operator answers), the seq attributes. Two different raisings "
            "about one finding share a key; only the seq says which of them this record "
            "was written for"
        )
    if not _is_nonempty_str(payload.get("original")):
        problems.append(
            "`original` is required — the machine-side item VERBATIM, carried in the same "
            "record as the translation. The pair is the mechanism: it is what makes the "
            "ordering against the notification unnecessary rather than unverifiable, and "
            "it is what later shows which translations are read"
        )
    for part in PROJECTION_PARTS:
        if not _is_nonempty_str(payload.get(part)):
            problems.append(
                f"`{part}` is required and must not be empty — the four parts are what a "
                "projection IS: what happened in ordinary words, what it concerns (how "
                "much, of what, where), what development proposes and why, and what each "
                "available answer will actually do, said as outcomes rather than as the "
                "names of wire fields. An empty part is the failure this record exists to "
                "prevent"
            )
    if problems:
        raise InvalidMessagePayloadError("operator_projection: " + "; ".join(problems))


def _validate_reconciliation_payload(payload: dict) -> None:
    """Refuse a malformed reconciliation record at POST time (B.12 B-1…B-4, B-6).

    SYMMETRIC by outcome, because one shape must refuse what the other requires: a
    `failed` attempt carries a reason and NO entries, a `produced` one carries entries.
    Without the symmetry a failed attempt could quietly carry a half-list, and "exactly one
    attempt" would stop meaning anything measurable.

    A run that compared everything and found NO discrepancy is `produced` with an empty
    `entries` list — that is a real result and the commonest good one. It is distinguished
    from a run that never happened by the outcome word, not by the list being empty, which
    is exactly why the two words exist.

    WHAT B.12 CHANGES HERE, and why each half is checked by the server rather than asked
    for in the prompt (constraint 1 of the slice — prose does not bind at the moment of
    decision):

    * **Six categories instead of three** (B-1), with the selection procedure carried in
      the prompt. The server cannot check that the FIRST matching category was chosen —
      that needs the observation, which only the pass has — but it can and does check that
      exactly one category from the closed vocabulary is carried.
    * **Evidence follows the category** (B-4). Five categories quote a DOCUMENT; `silent`
      has no document to quote by definition, so it quotes the CODE and writes its "what
      was claimed" half out as explicitly empty IN WORDS. A single rule demanding a
      promise-quote of every entry made `silent` unwritable, and a contract that cannot
      express a case it mandates forces fabrication, mislabelling, or silence.
    * **The two transcript-borne categories must show a SETTLEMENT** (B-1, B-8), and say in
      one clause what makes the quote one. The server checks the clause EXISTS; whether the
      quote really settles anything is a judgement, and is the pass's.
    * **Cost of missing, and the order it imposes** (B-3). One flat list, no sections,
      ordered `never` → `next_review` → `self_announcing` across ALL entries. The order is
      mechanically checkable, so it is checked: an ordering rule that lives only in the
      prompt is one the prompt can lose, and this report is read by someone who may stop
      halfway.

    `intent_refs` is renamed `document_refs` in the same motion, because the set is no
    longer four intents: it is the cycle's documents as resolved from its anchor (A-7),
    the free-phase transcript and the spec included. A field named for a subset of what it
    now holds is the kind of name that later gets believed.
    """
    problems = []
    if not _is_int(payload.get("map_seq")):
        problems.append("`map_seq` is required — a reconciliation names the map it follows")
    outcome = payload.get("outcome")
    if outcome not in RECONCILIATION_OUTCOMES:
        problems.append(
            f"`outcome` must be one of {list(RECONCILIATION_OUTCOMES)}, got {outcome!r}"
        )
    if "retry_request_seq" in payload and not _is_int(payload.get("retry_request_seq")):
        problems.append("`retry_request_seq` must be an integer when present")
    entries = payload.get("entries")
    if outcome == "failed":
        if not _is_nonempty_str(payload.get("reason")):
            problems.append(
                "a `failed` reconciliation requires a `reason` — the attempt is spent "
                "either way, so the record is the only thing left of it"
            )
        if entries:
            problems.append(
                "a `failed` reconciliation carries NO `entries` — an attempt that failed "
                "did not produce a partial list"
            )
    elif outcome == "produced":
        # THE OPENING INVENTORY IS PART OF THE RECORD (finding
        # `b12-acting-constraints-not-bound`, sol round 1). The report is required to open by
        # naming every document it read and every one it could not — and that requirement
        # lived only in the prompt: nothing carried it, nothing accepted it, nothing rendered
        # it. The consequence is not cosmetic. An EMPTY entry list is the commonest good
        # result, and without the inventory it is indistinguishable from a comparison that
        # silently ran against three documents out of four. The pass gets ONE attempt, so
        # that silence is permanent.
        #
        # Checked for PRESENCE and shape, never for truth: whether the pass really read what
        # it names is not decidable here, and the check that pretends otherwise is worse than
        # none. What the server can hold is that the claim was made explicitly.
        inventory = payload.get("documents_read")
        if not isinstance(inventory, list) or not inventory:
            problems.append(
                "a `produced` reconciliation requires a non-empty `documents_read` — the "
                "report opens by naming what it read and what it could not, and an entry "
                "list without that opening cannot be told apart from a comparison that never "
                "happened"
            )
        else:
            for i, item in enumerate(inventory):
                if not isinstance(item, dict):
                    problems.append(f"`documents_read[{i}]` must be an object")
                    continue
                if not _is_nonempty_str(item.get("node_id")):
                    problems.append(
                        f"`documents_read[{i}]` requires `node_id` — the document's identity, "
                        "not its label: labels are not unique within a cycle"
                    )
                # EXACT type, not membership (finding `b12-inventory-read-bool-type-loose`,
                # sol round 3): `1 in (True, False)` is True by Python equality, while the
                # renderer tests identity — so an accepted numeric flag rendered as an
                # unexplained UNREAD row after the single attempt was already spent.
                if not isinstance(item.get("read"), bool):
                    problems.append(
                        f"`documents_read[{i}]` requires `read` as a boolean — the whole "
                        "point of the inventory is the difference between the two, and a "
                        "numeric 0/1 is refused because the renderer distinguishes them "
                        "by type"
                    )
                if item.get("read") is False and not _is_nonempty_str(item.get("problem")):
                    problems.append(
                        f"`documents_read[{i}]` was not read and states no `problem` — an "
                        "unread document with no reason is the silence this field exists to "
                        "break"
                    )
        if not isinstance(entries, list):
            problems.append(
                "a `produced` reconciliation requires an `entries` list (possibly empty — "
                "a comparison that found nothing is a real and good result)"
            )
        else:
            ranks: list[int] = []
            for i, entry in enumerate(entries):
                where = f"entry {i}"
                if not isinstance(entry, dict):
                    problems.append(f"{where}: each entry must be an object")
                    continue
                label = entry.get("label")
                if label not in RECONCILIATION_LABELS:
                    problems.append(
                        f"{where}: `label` must be exactly one of "
                        f"{list(RECONCILIATION_LABELS)}, got {label!r}. The list is open, "
                        "but it grows by an operator decision — never by a pass improvising "
                        "a seventh value"
                    )
                cost = entry.get("cost_of_missing")
                if cost not in COST_OF_MISSING:
                    problems.append(
                        f"{where}: `cost_of_missing` must be one of {list(COST_OF_MISSING)}, "
                        f"got {cost!r} — the answer to «if the operator does not read this "
                        "now, how and when will they find out?». It is the report's only "
                        "ordering, and it is written into the entry so the reader can audit "
                        "the order instead of trusting it"
                    )
                else:
                    ranks.append(COST_OF_MISSING.index(cost))
                for field in ("quote", "address"):
                    if not _is_nonempty_str(entry.get(field)):
                        problems.append(
                            f"{where}: `{field}` is required — every entry quotes its "
                            "evidence VERBATIM with its address; a paraphrase is a contract "
                            "violation, not a stylistic lapse. What is quoted follows the "
                            "category: the document's own words for five of them, the CODE "
                            "with its file location for `silent`"
                        )
                if not _is_nonempty_str(entry.get("note")):
                    problems.append(
                        f"{where}: `note` is required — the second half of the entry: what "
                        "the code actually does, in plain words. An entry legible only to "
                        "someone holding the documents is written for a reader who does not "
                        "need it"
                    )
                if label == RECONCILIATION_CODE_BORNE:
                    if not _is_nonempty_str(entry.get("claimed_absent")):
                        problems.append(
                            f"{where}: `{RECONCILIATION_CODE_BORNE}` requires "
                            "`claimed_absent` — the «what was claimed» half written out as "
                            "explicitly EMPTY, in words (no document of the cycle mentions "
                            "this). A blank reads as an omission by the pass, and a "
                            "paraphrase would be a fabricated promise"
                        )
                else:
                    refs = entry.get("document_refs")
                    if not isinstance(refs, list) or not refs:
                        problems.append(
                            f"{where}: `document_refs` is required — a non-empty list naming "
                            "the cycle documents this entry was worked out from, by the NODE "
                            "ID the pass's input block prints (never a label — labels are "
                            "display text and are not unique). An entry whose sources are "
                            "empty is an entry nobody can check"
                        )
                    elif not all(_is_nonempty_str(r) for r in refs):
                        problems.append(
                            f"{where}: every entry of `document_refs` must be a non-empty "
                            "string"
                        )
                if label in RECONCILIATION_TALK_BORNE and not _is_nonempty_str(
                    entry.get("settlement")
                ):
                    problems.append(
                        f"{where}: `{label}` requires `settlement` — one clause saying what "
                        "in the quote makes it a SETTLEMENT (an instruction, an agreement, "
                        "a conclusion) rather than thinking aloud. A hypothesis, an option "
                        "turned over, and a thought abandoned later do not qualify: an entry "
                        "built on one accuses the reader of breaking a promise nobody gave"
                    )
            if ranks != sorted(ranks):
                problems.append(
                    "entries must be ordered by `cost_of_missing` across the WHOLE list — "
                    f"{list(COST_OF_MISSING)}, most expensive first — and this one is not. "
                    "The report has no sections: a reader who stops halfway down a "
                    "cost-ordered list has seen the most expensive items of every category, "
                    "while one who stops after a section has seen every case of one kind and "
                    "none of any other"
                )
    if problems:
        raise InvalidMessagePayloadError("reconciliation: " + "; ".join(problems))


#: B.12 A-7 — re-exported from `round_gate`, where the key lives, for the same reason
#: `RECONCILIATION_RETRY_KEY` is: the server and the watcher must not hold two spellings of
#: one wire field.
CYCLE_ANCHOR_KEY = round_gate.CYCLE_ANCHOR_KEY
cycle_anchor_of = round_gate.cycle_anchor_of


async def _validate_reconciliation_against_cycle(
    session: AsyncSession, review: Review, payload: dict
) -> None:
    """B.12 A-7 — THE SET IS READ FROM THE ANCHOR, NOT DECLARED.

    B.11 required development to post the cycle's Intent Summaries as a `notice` and
    validated every citation against that declaration. Both are gone, and it is one removal
    rather than two: the declaration was unverifiable by construction — three of the four
    documents were files and the server has no repository, so the only one it could check
    was the one in which a mistake is impossible. The critic of review `36a80d12` peeled
    three layers off the same element in successive rounds (citations outside the set, then
    the absence of the declaration, then the declaration itself), which is the signature of
    a mechanism defending something that should not have been declared at all.

    What replaces it is a FACT the server can read: the review names its development cycle,
    and the cycle's documents hang off that anchor in the same database. The citation check
    survives with a ground under it for the first time — an entry may cite only documents
    that actually are on this cycle's anchor.

    `failed` stays postable in every state, which is the half worth keeping from the
    original objection: the attempt is spent either way, so the pass must always be able to
    record its own failure with a reason. Only `produced` — the shape that CLAIMS a
    comparison happened — needs the set to exist.
    """
    if payload.get("outcome") != "produced":
        return
    anchor_ref = cycle_anchor_of(review.config)
    if anchor_ref is None:
        raise InvalidMessagePayloadError(
            "a `produced` reconciliation requires this review to name its development "
            f"cycle (`{CYCLE_ANCHOR_KEY}` in the config frozen at creation). The pass "
            "compares the map against the documents of ONE cycle, read from that cycle's "
            "anchor; with no anchor there is nothing its citations can be held against, and "
            "the record would claim a comparison while proving nothing about what was "
            "compared. A `failed` reconciliation is postable in any state — the attempt is "
            "spent either way, and its reason is what the operator reads instead of a list"
        )
    try:
        resolved = await cycle.resolve_cycle_documents(session, anchor_ref)
    except cycle.CycleAnchorError as exc:
        raise InvalidMessagePayloadError(
            f"this review's development-cycle anchor does not resolve: {exc}"
        ) from None
    # IDENTITY IS THE NODE ID, AND ONLY THE NODE ID (finding
    # `b12-document-label-reference-ambiguous`, round 1 of the sol review). Labels used to be
    # accepted here as well, and `Node.label` is not unique: this cycle alone carries three
    # pre-review intents with near-identical labels. A collision does not merely mis-cite —
    # it flips the narrower standing below, so an intent-grounded broken-promise entry can be
    # refused because a transcript happens to share a name, and a transcript-grounded one
    # accepted because an intent does. That is precisely the attribution the review-affinity
    # work of this same slice exists to protect, defeated by the key used to look documents
    # up. The label stays DISPLAY TEXT in the rendered report and nothing else.
    allowed = {d["node_id"] for d in resolved["documents"]}
    stray = sorted(
        {
            str(ref)
            for entry in payload.get("entries") or []
            if isinstance(entry, dict)
            for ref in entry.get("document_refs") or []
            if str(ref) not in allowed
        }
    )
    if stray:
        raise InvalidMessagePayloadError(
            f"reconciliation cites documents that are not on this cycle's anchor: {stray}. "
            "The pass compares the map against what the OPERATOR WAS TOLD by THIS cycle, so "
            "a citation of another cycle's intent — or of a path nobody wrote to the anchor "
            "— makes the record prove nothing about what was actually compared. Cite "
            "documents by the NODE ID given in the pass's input block; a label is display "
            "text and is not unique within a cycle"
        )

    # THE INVENTORY IS JOINED TO THE SET, NOT ONLY SHAPED (finding
    # `b12-inventory-not-joined-to-cycle-set`, sol round 2). The shape check accepts any
    # non-empty list of well-formed rows, and the renderer calls a list with no `read: false`
    # row "fully read" — so a partial or duplicated inventory recreated the exact misleading
    # clean result the field was added to prevent. The meaning of the inventory is a claim
    # ABOUT THE SET, and a claim about the set is checkable only against the set: exactly one
    # row per document of the cycle — no duplicates, no foreign ids, none missing.
    inventory_ids = [
        str(item.get("node_id"))
        for item in payload.get("documents_read") or []
        if isinstance(item, dict)
    ]
    duplicated = sorted({i for i in inventory_ids if inventory_ids.count(i) > 1})
    foreign = sorted(set(inventory_ids) - allowed)
    missing = sorted(allowed - set(inventory_ids))
    inventory_problems: list[str] = []
    if duplicated:
        inventory_problems.append(
            f"`documents_read` lists {duplicated} more than once — one row per document, or "
            "the counts the report opens with stop meaning anything"
        )
    if foreign:
        inventory_problems.append(
            f"`documents_read` names documents that are not on this cycle's anchor: {foreign}"
        )
    if missing:
        inventory_problems.append(
            f"`documents_read` omits documents of the cycle: {missing} — an unread document "
            "is declared with `read: false` and its reason, never by leaving it off the list, "
            "because a shortened list reads as a fully covered set"
        )
    if inventory_problems:
        raise InvalidMessagePayloadError(
            "reconciliation inventory does not match the cycle's document set (exactly one "
            "row per document of the set, by node id): " + "; ".join(inventory_problems)
        )

    # B-8: THE TRANSCRIPT'S STANDING IS NARROWER, IN EXACTLY ONE DIRECTION — it may not
    # support `contradicted` or `promised_absent`. An intent asserting something is a
    # commitment; a sentence in a free-form discussion is not, so it cannot ground a claim
    # that a promise was BROKEN. Treating a musing as an assertion would manufacture
    # divergences and would, in practice, punish thinking out loud — which is the one thing
    # those phases exist for.
    #
    # Checked here rather than left to the prompt because the type label makes it decidable:
    # the server knows which of the cited documents is the transcript. It is deliberately
    # the WEAK form of the rule — an entry may still cite the transcript as CORROBORATION
    # beside an intent (B-8 allows exactly that); what is refused is an entry of those two
    # categories standing on the transcript ALONE.
    transcripts = {
        d["node_id"]
        for d in resolved["documents"]
        if d.get("kind") == cycle.TRANSCRIPT_KIND
    }
    for i, entry in enumerate(payload.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("label") not in ("contradicted", "promised_absent"):
            continue
        refs = {str(r) for r in entry.get("document_refs") or []}
        if refs and refs <= transcripts:
            raise InvalidMessagePayloadError(
                f"entry {i}: `{entry.get('label')}` stands on the free-phase transcript "
                "alone. The transcript may not ground a claim that a PROMISE WAS BROKEN — "
                "an intent asserting something is a commitment, a sentence in a free-form "
                "discussion is not, and the operator may turn a thought over and drop it. "
                "What the transcript does ground is its own two categories, "
                "`decided_in_talk_only` and `dropped_in_talk`; beside an intent it may "
                "corroborate an entry of any category"
            )


def _validate_map_references(kind: str, payload: dict, wire: list[dict]) -> None:
    """Resolve the cross-references of a map disposition, a reconciliation, or a retry
    request against the channel (B.11 E-8, E-11, F-1).

    THE ONCE-ONLY RULE, and why it is two conditions read as one: an attempt is not
    scheduled when a `reconciliation` for this map already exists — WITH EITHER OUTCOME —
    and no unconsumed operator retry request stands for it. A spent attempt closes the
    door; an unconsumed request re-opens it exactly once. A `failed` attempt is a spent
    attempt: treating it as unspent would turn the operator's «Попытка пусть будет одна»
    into an unbounded retry after every failure.
    """
    map_seqs = {m["seq"] for m in wire if m.get("kind") == "map"}

    if kind == "decision_response":
        request = payload.get(RECONCILIATION_RETRY_KEY)
        if not isinstance(request, dict) or not _is_int(request.get("map_seq")):
            raise InvalidMessagePayloadError(
                f"`{RECONCILIATION_RETRY_KEY}` must be an object carrying an integer "
                "`map_seq` — the request names the map whose reconciliation is to be "
                "re-attempted"
            )
        if not _is_nonempty_str(payload.get("operator_words")):
            raise InvalidMessagePayloadError(
                f"a `{RECONCILIATION_RETRY_KEY}` request must carry `operator_words` — a "
                "second attempt exists only on the operator's explicit request, so their "
                "own words are what licenses it"
            )
        target = request["map_seq"]
        if target not in map_seqs:
            raise InvalidMessagePayloadError(
                f"`{RECONCILIATION_RETRY_KEY}.map_seq` {target!r} names no `map` message on "
                f"this channel (maps: {sorted(map_seqs) or 'none'})"
            )
        if not any(
            m.get("kind") == "reconciliation"
            and (m.get("payload") or {}).get("map_seq") == target
            for m in wire
        ):
            raise InvalidMessagePayloadError(
                f"no reconciliation attempt has been spent over map seq {target} — there is "
                "nothing to re-license. The first attempt is scheduled by the watcher and "
                "needs no request"
            )
        return

    map_seq = payload.get("map_seq")
    if map_seq not in map_seqs:
        raise InvalidMessagePayloadError(
            f"`map_seq` {map_seq!r} names no `map` message on this channel "
            f"(maps: {sorted(map_seqs) or 'none'}) — the reference identifies WHICH "
            "material this record stands over, and a reference that resolves to nothing "
            "identifies nothing"
        )

    if kind == "map_disposition":
        reconciliation_seq = payload.get("reconciliation_seq")
        if reconciliation_seq is not None:
            named = next(
                (
                    m for m in wire
                    if m.get("kind") == "reconciliation" and m["seq"] == reconciliation_seq
                ),
                None,
            )
            if named is None:
                raise InvalidMessagePayloadError(
                    f"`reconciliation_seq` {reconciliation_seq!r} names no `reconciliation` "
                    "message on this channel. The field binds only what existed when the "
                    "record was written — leave it out when no reconciliation had arrived"
                )
            # ...AND IT MUST BE THE RECONCILIATION OF THIS MAP (critic finding
            # `map-disposition-cross-map-reconciliation-ref`, round 7 of this slice's own
            # review). Existence was checked and belonging was not, so a ruling over map A
            # could record map B's reconciliation as the material it considered. These
            # references exist for exactly one purpose — to say WHICH material the operator
            # had in front of them — and a reference to the wrong sibling states something
            # false about the operator's own reading. Reachable whenever a review holds maps
            # for more than one base+commit pair, which the identity repair of round 2 made
            # an ordinary rather than an exotic state.
            belongs_to = (named.get("payload") or {}).get("map_seq")
            if belongs_to != map_seq:
                raise InvalidMessagePayloadError(
                    f"`reconciliation_seq` {reconciliation_seq!r} is the reconciliation of "
                    f"map seq {belongs_to!r}, but this disposition resolves map seq "
                    f"{map_seq!r} — a ruling may only name the material that stood over the "
                    "map it rules on"
                )
        return

    # The once-only guard. The fold itself lives in `round_gate`
    # and is the SAME code the watcher schedules from — two implementations of "may another
    # attempt be made" would eventually disagree, and disagreement here means either a
    # second attempt nobody licensed or a licensed one refused.
    standing = round_gate.reconciliation_standing(wire, map_seq)
    requests = standing["requests"]
    spent = standing["spent"]
    claimed = payload.get("retry_request_seq")
    if claimed is not None:
        if claimed not in requests:
            raise InvalidMessagePayloadError(
                f"`retry_request_seq` {claimed!r} names no operator "
                f"`{RECONCILIATION_RETRY_KEY}` request for map seq {map_seq} on this channel"
            )
        if requests[claimed]:
            raise InvalidMessagePayloadError(
                f"the retry request at seq {claimed} was already consumed by an earlier "
                "reconciliation — one request licenses exactly one attempt, and a third "
                "attempt needs a second request"
            )
        return
    if spent:
        unconsumed = sorted(seq for seq, used in requests.items() if not used)
        raise InvalidMessagePayloadError(
            f"a reconciliation over map seq {map_seq} has already been attempted "
            f"(seq {spent}) — the attempt is spent, with either outcome, "
            "and «Попытка пусть будет одна». A second attempt exists only on the "
            "operator's explicit request and must NAME it via `retry_request_seq`"
            + (
                f"; unconsumed requests stand at {unconsumed}"
                if unconsumed
                else " (no unconsumed request stands)"
            )
        )


def _validate_decision_response_payload(payload: dict) -> None:
    """Refuse a malformed decision_response at POST time.

    An external_defect answer (one carrying ``escalation_id``) MUST carry a real operator
    ``action`` — ``freeze`` | ``defer`` | ``reject`` — otherwise a bare ``{escalation_id}``
    would close the open-operator axis (and ``freeze`` would strand the review) with no
    actual disposition of the base defect. The optional return signal ``requires_new_artifact``
    (K.4.1) must be a boolean if present. (That the escalation_id points at a REAL prior
    external_defect is checked against the log in ``append_message``.)
    """
    # Every identity a decision_response can carry is used as an open-item key — validate each
    # as a non-empty string WHEN PRESENT (presence-based, so falsey non-strings like [] / {} /
    # "" are refused too, not bypassed by a truthiness check — F19).
    for f in ("question_id", "finding_id", "element_id", "escalation_id"):
        if f in payload and not _is_nonempty_str(payload.get(f)):
            raise InvalidMessagePayloadError(
                f"decision_response `{f}` must be a non-empty string"
            )
    # an external_defect answer (escalation_id) must carry a real freeze/defer/reject action
    if payload.get("escalation_id") and payload.get("action") not in _EXTERNAL_DEFECT_ACTIONS:
        raise InvalidMessagePayloadError(
            "external_defect decision_response requires `action` one of "
            f"{_EXTERNAL_DEFECT_ACTIONS}, got {payload.get('action')!r}"
        )
    if "requires_new_artifact" in payload and not isinstance(
        payload["requires_new_artifact"], bool
    ):
        raise InvalidMessagePayloadError("`requires_new_artifact` must be a boolean")
    # B.6 M-1 counter 3: development's report of whether the operator's ruling matched the
    # route it proposed. Typed because a metric that silently counts the string "false" as
    # agreement is worse than no metric — it reads as evidence while being noise.
    if "agreed_with_proposal" in payload and not isinstance(
        payload["agreed_with_proposal"], bool
    ):
        raise InvalidMessagePayloadError("`agreed_with_proposal` must be a boolean")
    # B.11 B-2: AN ANSWER TO A COVERAGE-ROW ESCALATION SAYS WHICH OF TWO THINGS IT MEANS.
    # The old code inferred it from what the answer did NOT carry: any answer without
    # `requires_new_artifact` exempted the whole group. The operator's real answer — "the
    # rows are reviewable, keep making passes" — needs no new artifact, so it set no flag,
    # so ~128 rows silently left the denominator (5be9cf61). Refusing at POST is the only
    # place the ambiguity can be caught while the author is still there to resolve it.
    element_id = payload.get("element_id") or ""
    if isinstance(element_id, str) and element_id.startswith(
        (COVERAGE_ROW_ELEMENT_PREFIX, COVERAGE_ROWS_GROUP_PREFIX)
    ):
        disposition = payload.get(COVERAGE_DISPOSITION_KEY)
        if disposition not in COVERAGE_DISPOSITIONS:
            raise InvalidMessagePayloadError(
                f"a decision_response answering coverage rows ({element_id!r}) must name "
                f"`{COVERAGE_DISPOSITION_KEY}` explicitly, one of "
                f"{list(COVERAGE_DISPOSITIONS)} — got {disposition!r}. "
                f"`{COVERAGE_ACCEPTED_UNREVIEWABLE}`: the rows are accepted as genuinely "
                "unreviewable and LEAVE the coverage denominator. "
                f"`{COVERAGE_KEEP_GATING}`: the rows STAY in the denominator and keep "
                "blocking convergence; what the answer spends is the persistence counter, "
                "so the same group will not re-escalate on the next pass. The two are not "
                "distinguishable from silence, and silence used to mean the first one"
            )


async def _disposed_finding_ids(session: AsyncSession, review_id: uuid.UUID) -> set:
    """Finding ids that already carry a terminal disposition on the log.

    Terminal includes B.7's two server-emitted outcomes: a finding the operator's final
    closed is as disposed as one development fixed, and re-raising its id would make the
    ledger silently count the new finding as already closed.
    """
    messages = await session.scalars(
        select(ReviewMessage).where(
            ReviewMessage.review_id == review_id, ReviewMessage.kind == "disposition"
        )
    )
    return {
        (m.payload or {}).get("finding_id")
        for m in messages
        if (m.payload or {}).get("outcome") in round_gate.TERMINAL_OUTCOMES
    }


async def _external_defect_ids(session: AsyncSession, review_id: uuid.UUID) -> list[str]:
    """The ids of every external_defect escalation on the review's log, in order."""
    messages = await session.scalars(
        select(ReviewMessage).where(
            ReviewMessage.review_id == review_id, ReviewMessage.kind == "escalation"
        )
    )
    return [
        (m.payload or {}).get("id")
        for m in messages
        if (m.payload or {}).get("kind") == "external_defect" and (m.payload or {}).get("id")
    ]


async def _latest_artifact_seq(session: AsyncSession, review_id: uuid.UUID) -> int | None:
    """Seq of the newest ``artifact`` message, or None before the first one."""
    return await session.scalar(
        select(func.max(ReviewMessage.seq)).where(
            ReviewMessage.review_id == review_id, ReviewMessage.kind == "artifact"
        )
    )


async def _artifact_message(
    session: AsyncSession, review_id: uuid.UUID, artifact_seq
) -> ReviewMessage | None:
    """The artifact message at ``artifact_seq``, or None when that seq is not an artifact."""
    if not _is_int(artifact_seq):
        return None
    return await session.scalar(
        select(ReviewMessage).where(
            ReviewMessage.review_id == review_id,
            ReviewMessage.kind == "artifact",
            ReviewMessage.seq == artifact_seq,
        )
    )


async def _finding_ids_for_artifact(
    session: AsyncSession, review_id: uuid.UUID, artifact_seq
) -> set[str]:
    """Finding ids raised against one artifact version BY THE CURRENT PASS.

    Pass-local, like every other coverage obligation. Validating against every id ever
    raised for the artifact let a later clean pass mark a row `finding` by pointing at a
    finding an EARLIER pass had raised and development had already disposed — the row would
    count as reached while this pass found nothing there, which is the same hollow claim as
    a bare `reviewed-clean`, only harder to see.
    """
    messages = (
        await session.scalars(
            select(ReviewMessage)
            .where(ReviewMessage.review_id == review_id)
            .order_by(ReviewMessage.seq)
        )
    ).all()
    this_pass = _critic_passes(messages, artifact_seq)[-1]
    return {
        f.get("id")
        for m in messages
        if m.kind == "findings"
        and m.seq in this_pass.findings_seqs
        for f in (m.payload or {}).get("items") or []
        if isinstance(f, dict) and f.get("id")
    }


async def _review_coverage_in_play(session, review_id, log) -> bool:
    """THE predicate: is a coverage denominator in play for this review? (B.11 A-1)

    The single source of truth behind every "does this version owe a manifest" question
    the server and the watcher ask. It reads the review's FROZEN setting; the message
    scan it is handed is the pre-B.11 fallback only, and `genres.coverage_in_play` says
    why it still exists.

    Not a free function on the log, because the answer is a property of the REVIEW and
    the log cannot carry it: before the first manifest lands, every version of every
    coverage-bearing review looks exactly like a review that never had coverage at all.
    """
    review = await session.get(Review, review_id)
    return coverage_in_play(
        review.config if review is not None else None,
        any_manifest=any(m.kind == "coverage_manifest" for m in log),
    )


async def _manifest_owed(session, review_id, log, anchor) -> bool:
    """Is a coverage manifest owed before this artifact version may be reviewed?

    True when coverage is IN PLAY for the review and this version — which must not be an
    intent summary, audited against the converged version rather than reached — has none
    of its own. One predicate, asked at both points that must agree; the two sites had
    computed it separately, and a check that exists twice is a check that eventually
    disagrees with itself.

    B.11 A-1: "in play" is now the review's own frozen answer rather than an inference
    from an empty channel. The old inference made the FIRST version of every review
    un-gated by construction — a manifest references an artifact and cannot precede one —
    so the first pass either spent a finding on a phantom or, worse, ran with no
    denominator and left nothing on the channel saying it was unmeasured.
    """
    if anchor is None:
        return False
    if not await _review_coverage_in_play(session, review_id, log):
        return False
    if any(
        m.kind == "artifact"
        and m.seq == anchor
        and (m.payload or {}).get("intent_summary") is True
        for m in log
    ):
        return False
    return await _manifest_for_artifact(session, review_id, anchor) is None


async def _manifest_for_artifact(
    session: AsyncSession, review_id: uuid.UUID, artifact_seq
) -> dict | None:
    """The NEWEST coverage manifest posted for one artifact version, or None.

    Newest wins: a manifest may legitimately be re-posted for the same version (a
    granularity downgrade after a row-cap escalation, a corrected derivation), and the
    report is always judged against the denominator in force.
    """
    if not _is_int(artifact_seq):
        return None
    messages = (
        await session.scalars(
            select(ReviewMessage)
            .where(
                ReviewMessage.review_id == review_id,
                ReviewMessage.kind == "coverage_manifest",
            )
            .order_by(ReviewMessage.seq)
        )
    ).all()
    latest = None
    for m in messages:
        if (m.payload or {}).get("artifact_seq") == artifact_seq:
            latest = m.payload
    return latest


#: How many `not-reached` rows the server tolerates before it refuses to complete
#: convergence. The DEFAULT IS ZERO at every depth tier: every row must carry a verdict, and
#: `not-reached` is a verdict that routes another pass, not a pass-through. It is a tunable
#: only because a surface may contain rows that are genuinely unreviewable; raising it is an
#: operator act, recorded like any waiver.
_DEFAULT_UNREACHED_ALLOWANCE = 0
#: The channel carrier for the confirmed depth tier: a notice the gate posts after the
#: operator grants it. Read from the channel rather than from `review.config` because the
#: tier is confirmed at the pre-review gate — after the review row already exists — and
#: because a recorded grant belongs in the auditable timeline, not in a mutable side table.
DEPTH_TIER_PHASE = "review_depth"
#: Escalation key namespace for a row that stayed unreached across two consecutive passes.
#: Namespaced through `element_id` so the existing order-aware open-item machinery closes it
#: on an ordinary operator `decision_response` — no new resolution path to keep in step.
COVERAGE_ROW_ELEMENT_PREFIX = "coverage-row:"
#: B.8 C-2: the element key of a GROUPED unreached-rows fork — one operator item per pass,
#: not one per row. The payload carries the rows as `row_ids`; one decision_response naming
#: this element key settles the whole group (the settle-per-key fold already does that).
COVERAGE_ROWS_GROUP_PREFIX = "coverage-rows:"

#: B.11 B-2: what an operator's answer to an unreached-row escalation MEANS, said on the
#: wire instead of inferred from what the answer does not carry.
#:
#: `accepted_unreviewable` — the rows leave the denominator, which is what the old code
#: did for every answer that set no `requires_new_artifact` flag. `keep_gating` — the rows
#: STAY in the denominator and keep blocking convergence; what the answer spends is the
#: persistence counter, which resets, so the same group does not re-escalate on the next
#: pass. Measured in code review 5be9cf61: the operator's real answer was "the rows are
#: reviewable, keep making passes", it needed no new artifact, so it set no flag, so ~128
#: rows silently left the denominator and `convergence_blocked` then counted 6.
COVERAGE_DISPOSITION_KEY = "coverage_disposition"
COVERAGE_ACCEPTED_UNREVIEWABLE = "accepted_unreviewable"
COVERAGE_KEEP_GATING = "keep_gating"
COVERAGE_DISPOSITIONS = (COVERAGE_ACCEPTED_UNREVIEWABLE, COVERAGE_KEEP_GATING)

PASS_EVIDENCE_KINDS = ("findings", "escalation")
"""Critic message kinds that PROVE a pass read the subject.

This is the definition `_CriticPass.reviewed` is computed from, named once so its consumers
cannot drift from it. The manifest-owed guard had enumerated `findings` alone, which left a
substantive escalation able to land over a version that still owes its denominator — and an
escalation routes the operator's attention, so it is the more expensive half of the pair.

`human_question` is deliberately ABSENT: asking the operator something is not evidence the
subject was read, and gating it would block the pre-review question path. `status` is absent
for the same reason it is checked separately — it ENDS a pass rather than evidencing one.
"""


def _unreached_allowance(messages) -> int:
    """The tier's tolerated `not-reached` count, from the newest depth-tier notice.

    **Only an OPERATOR-authored notice may RAISE it.** The default is zero, and raising it
    is an operator act recorded like any waiver — so a development-authored notice must not
    be able to move it, or the party the gate constrains could switch the gate off. This is
    the same trust level the rest of the loop runs on (role is prompt-maintained, not
    authenticated), but it is the level everything else uses, and accepting any author here
    was strictly weaker than that.

    Lowering it needs no such check: a smaller allowance only ever gates more.

    The comparison is against the allowance IN FORCE, not against the default. Comparing to
    the default made a LOWERING from an operator-raised value look like a raise — after the
    operator allowed 5, a later notice asking for 3 was dropped and the review kept running on
    the more permissive 5. The rule the docstring states is "a non-operator notice may never
    WIDEN the gate", and only the running value can express it.
    """
    allowance = _DEFAULT_UNREACHED_ALLOWANCE
    for m in messages:
        payload = m.payload or {}
        if m.kind == "notice" and payload.get("phase") == DEPTH_TIER_PHASE:
            value = payload.get("unreached_allowance")
            if type(value) is int and value >= 0:
                if value > allowance and m.role != "operator":
                    continue
                allowance = value
    return allowance


@dataclass
class _CriticPass:
    """One critic pass over one artifact version, as the log actually records it.

    THE PASS IS THE UNIT every coverage obligation is defined on — "this pass owes its
    report", "two consecutive passes could not reach the row", "a report answers for the
    pass it was posted in". That unit had been re-derived ad hoc at each site with its own
    ``max(seq …)``, and each round of review found another place where two of those
    derivations disagreed: one counted report MESSAGES as passes, another treated an
    escalation as reviewing for one purpose and not for another, a third erased passes that
    posted no report. Making the pass explicit is what stops that class rather than its
    latest instance.

    A pass runs from just after the previous critic pass-ending status to its own status
    (or to the end of the log, for a pass still in flight).
    """

    artifact_seq: int | None
    start_after: int  # seq of the previous pass-ending status (0 for the first pass)
    end_seq: int | None  # seq of this pass's status, or None while it is still open
    findings_seqs: list[int]
    escalation_seqs: list[int]
    reports: list[tuple[int, dict]]  # (seq, payload) — the seq is what dates the evidence

    @property
    def reviewed(self) -> bool:
        """Did this pass read the subject?

        Findings OR an escalation — the two kinds named in ``PASS_EVIDENCE_KINDS``, which the
        manifest-owed guard keys on so the definition lives in one place. An escalation is
        proof the pass read something: "I could not reach the subject" and "here is a defect I
        found in the base" cannot both be true of one pass.
        """
        return bool(self.findings_seqs or self.escalation_seqs)

    @property
    def evidence_boundary(self) -> int:
        """The seq after which a coverage report counts as this pass's own.

        The latest thing the report must be able to speak about: the pass's own findings and
        escalations, and failing those, the previous pass's end. A report written before them
        cannot carry verdicts about them.
        """
        return max([self.start_after, *self.findings_seqs, *self.escalation_seqs])

    def answering(self, artifact_seq, manifest_id: str) -> bool:
        """Did THIS pass post a report answering the manifest in force, in time?

        In time means after ``evidence_boundary`` — a report predating this pass's own
        findings or escalations cannot carry verdicts about them. Defining that boundary and
        then not asking it was the whole defect: the property existed and nothing called it.
        """
        return any(
            seq > self.evidence_boundary
            and payload.get("artifact_seq") == artifact_seq
            and payload.get("manifest_id") == manifest_id
            for seq, payload in self.reports
        )

    def answered_unanswerable(self, artifact_seq, manifest_id: str) -> bool:
        """Did this pass answer ONLY by declaring the report unanswerable?

        A pass that also posted a real row-by-row report has measured something and is not
        limited by the escape hatch; the question is whether the hatch is all there is.
        """
        answering = [
            payload
            for seq, payload in self.reports
            if seq > self.evidence_boundary
            and payload.get("artifact_seq") == artifact_seq
            and payload.get("manifest_id") == manifest_id
        ]
        return bool(answering) and all(p.get("unanswerable") for p in answering)


def _critic_passes(messages, artifact_seq=None) -> list[_CriticPass]:
    """Split the log into critic passes, optionally for one artifact version.

    Passes are delimited by the critic's own pass-ending statuses. A pass that posted no
    report is still a pass — erasing it made two report-bearing passes look consecutive when
    a third sat between them.
    """
    passes: list[_CriticPass] = []
    start_after = 0
    findings: list[int] = []
    escalations: list[int] = []
    reports: list[dict] = []
    for m in messages:
        payload = m.payload or {}
        anchor = payload.get("artifact_seq")
        if artifact_seq is not None and anchor != artifact_seq and m.kind != "status":
            continue
        if m.role != "critic":
            continue
        if m.kind == "findings":
            findings.append(m.seq)
        elif m.kind == "escalation":
            escalations.append(m.seq)
        elif m.kind == "coverage_report":
            reports.append((m.seq, payload))
        elif m.kind == "status":
            if artifact_seq is not None and anchor != artifact_seq:
                continue
            passes.append(
                _CriticPass(anchor, start_after, m.seq, findings, escalations, reports)
            )
            start_after, findings, escalations, reports = m.seq, [], [], []
    passes.append(_CriticPass(artifact_seq, start_after, None, findings, escalations, reports))
    return passes


def _required_granularity(messages) -> str:
    """The finest granularity the review's depth tier demands of a manifest.

    Only `light` may derive at file level; `standard` and `deep` both enumerate the changed
    surface symbol by symbol. With no tier recorded the answer is the full pipeline's, which
    is the same fail-toward-more-work default the tier proposal itself falls back to when
    the operator says nothing.

    Only an OPERATOR-authored notice may RELAX it, exactly as with the unreached allowance
    and the coverage-row exemption. This is the third reader of an operator-owned setting in
    this module, and the first two learned the same lesson: a development-authored notice
    that could set `tier: light` would let the party the coverage gate constrains grant
    itself a coarser denominator.
    """
    tier = None
    for m in messages:
        payload = m.payload or {}
        if m.kind == "notice" and payload.get("phase") == DEPTH_TIER_PHASE:
            if payload.get("tier") and not (
                payload["tier"] == "light" and m.role != "operator"
            ):
                tier = payload["tier"]
    return "file" if tier == "light" else "symbol"


def _coverage_blockers(messages, latest: int | None) -> list[str]:
    """Manifest rows for the latest artifact that carry no reached verdict (G-3).

    A row is a blocker when the report marks it ``not-reached`` **or when the report does
    not mention it at all** — an absent row is not a silent pass: the absence of a verdict
    is exactly the "never looked" case the manifest exists to make visible.

    Vacuous when the latest artifact has no manifest (a review predating the coverage
    machinery, or an ``intent_summary`` artifact, which is audited rather than reached).
    Rows already escalated as persistently unreached are excluded: they have STOPPED being
    a loop condition and are held open by the operator escalation instead (the non-deadlock
    exit) — counting them in both places would be a deadlock with extra steps.
    """
    if latest is None:
        return []
    manifest = None
    for m in messages:
        if m.kind == "coverage_manifest" and (m.payload or {}).get("artifact_seq") == latest:
            manifest = m.payload
    if manifest is None:
        return []
    reports = [
        m.payload
        for m in messages
        if m.kind == "coverage_report"
        and (m.payload or {}).get("artifact_seq") == latest
        and (m.payload or {}).get("manifest_id") == manifest.get("manifest_id")
    ]
    if _question_denominator(manifest):
        # B.14 B-3: answers ACCUMULATE WITHIN the version — an axis answered by any pass
        # over the same artifact version stands, a later report's row for an already-
        # answered axis SUPERSEDES the earlier one (latest binds; the append-only channel
        # keeps the history). An axis is a blocker while its accumulated outcome is
        # absent or a typed failure: `cannot_reach` until the operator's ruling settles
        # it (the escalation exemption below), `instrument_failure` until a superseding
        # REAL outcome arrives after remedy (B-6).
        verdicts: dict = {}
        for report in reports:
            for r in report.get("rows") or []:
                if isinstance(r, dict) and r.get("row_id"):
                    verdicts[r.get("row_id")] = r.get("verdict")
        blocking = (None, "cannot_reach", "instrument_failure")
    else:
        report = reports[-1] if reports else None
        verdicts = {
            r.get("row_id"): r.get("verdict")
            for r in ((report or {}).get("rows") or [])
            if isinstance(r, dict)
        }
        blocking = (None, "not-reached")
    escalated = _escalated_coverage_rows(messages)
    # B.8 C-1: rows under the collective declared-scope exemption are CLOSED — the
    # operator already ruled on the scope once, and re-blocking on them would re-ask it.
    exempt = escalated | _scope_exempt_rows(messages, manifest)
    blockers = [
        r.get("row_id")
        for r in (manifest.get("rows") or [])
        if isinstance(r, dict)
        and verdicts.get(r.get("row_id")) in blocking
        and r.get("row_id") not in exempt
    ]
    return sorted(str(b) for b in blockers if b)


def _missing_manifest(messages, latest: int | None, *, in_play: bool) -> bool:
    """Is a coverage denominator OWED for the latest artifact and absent?

    ``in_play`` is the review's own frozen answer, resolved once by
    ``_review_coverage_in_play`` (B.11 A-1) and passed in — this is the third site that
    used to derive it from an empty channel, and the third that was therefore blind to
    the first version of every review.

    A review that declares coverage OUT of play is not gated, and neither is a pre-B.11
    review that never posted a manifest — that is what lets a review started before this
    machinery existed finish under the process it started with. But under a review where
    coverage IS in play, an artifact arriving without a manifest is development dropping
    the denominator, and the gate would otherwise go quiet exactly when it stopped being
    fed.

    This is kept SEPARATE from the unreached-row list, which an earlier fix conflated it
    with, on two counts that both matter. It is not tolerable under the unreached allowance:
    the allowance exists for rows a reviewer genuinely could not reach, not for the absence
    of the table itself. And it is owed by DEVELOPMENT, not the critic — routing it as an
    unreached row asked the critic for another sweep it had no way to satisfy, since only
    development posts the manifest and a report without one is refused.
    """
    if latest is None:
        return False
    if not in_play:
        return False
    for m in messages:
        if m.kind != "artifact" or m.seq != latest:
            continue
        # The post-review intent summary is audited, not reached: it owes no denominator.
        if (m.payload or {}).get("intent_summary") is True:
            return False
    return not any(
        m.kind == "coverage_manifest" and (m.payload or {}).get("artifact_seq") == latest
        for m in messages
    )


def _escalated_coverage_rows(messages) -> set[str]:
    """Row ids currently EXEMPT from the coverage gate by an operator escalation.

    Two states earn the exemption, and one deliberately does not:

    - **escalation still open** — the row has stopped being a loop condition and is held by
      the operator item instead; counting it in both places would be the deadlock again;
    - **answered without demanding a change** — the operator accepted the row as
      genuinely unreviewable, so it stays exempt;
    - **answered with a return signal** — the operator narrowed or redirected the review
      rather than accepting the row. The exemption LAPSES: a permanent exemption here would
      mean that once a row hit the two-pass exit, every later version silently stopped being
      gated on it, which is the economic layer suppressing exactly what it was told to fix.

    Failing toward re-gating is the safe direction: the cost of a lapsed exemption is that
    the row must be reached or escalated again, and the cost of a wrong permanent one is a
    surface nobody ever looks at.
    """
    escalated: set[str] = set()
    lapsed: set[str] = set()
    for m, row_ids in _coverage_row_messages(messages):
        payload = m.payload or {}
        if m.kind == "escalation":
            escalated.update(row_ids)
            lapsed.difference_update(row_ids)  # a fresh escalation supersedes a lapse
        elif m.kind == "decision_response":
            # Only an OPERATOR-authored answer that does not demand a change can leave the
            # row exempt. A development-authored one closing the escalation would otherwise
            # read exactly like "the operator accepted this row as unreviewable" — letting
            # the party the coverage gate constrains grant itself a permanent exemption.
            # Same rule as the unreached allowance: an operator-owned setting moves only on
            # an operator-authored message.
            if _signals_return(payload) or m.role != "operator":
                lapsed.update(row_ids)
                continue
            # B.11 B-2: THE ANSWER SAYS WHICH OF THE TWO IT MEANS. `keep_gating` leaves the
            # rows in the denominator and spends the persistence counter instead
            # (`_coverage_gating_resets`); `accepted_unreviewable` takes them out, as
            # before. An answer naming NEITHER can only be a pre-B.11 channel — new ones
            # are refused at POST — and for those the old reading is kept deliberately, so
            # closed reviews continue to read exactly as they did.
            if payload.get(COVERAGE_DISPOSITION_KEY) == COVERAGE_KEEP_GATING:
                lapsed.update(row_ids)
    return escalated - lapsed


def _coverage_row_messages(messages):
    """Every channel message that speaks about coverage rows, with the rows it speaks about.

    One scan, two consumers (the exemption fold and the persistence-counter reset), so a
    group key resolves to the same rows in both. A GROUP fork (B.8 C-2) carries its rows
    in the escalation payload and later answers name only the key, which is why the
    mapping has to be built by replay rather than read off the answer.
    """
    groups: dict[str, tuple[str, ...]] = {}
    for m in messages:
        payload = m.payload or {}
        element_id = payload.get("element_id") or ""
        if not isinstance(element_id, str):
            continue
        if element_id.startswith(COVERAGE_ROW_ELEMENT_PREFIX):
            row_ids: tuple[str, ...] = (element_id[len(COVERAGE_ROW_ELEMENT_PREFIX) :],)
        elif element_id.startswith(COVERAGE_ROWS_GROUP_PREFIX):
            if m.kind == "escalation":
                groups[element_id] = tuple(
                    str(r) for r in payload.get("row_ids") or [] if r
                )
            row_ids = groups.get(element_id, ())
        else:
            continue
        yield m, row_ids


def _coverage_gating_resets(messages) -> dict[str, int]:
    """Row id -> the seq of the latest operator answer that said `keep_gating` (B.11 B-2).

    What a `keep_gating` answer SPENDS. The rows stay in the denominator, so the exemption
    fold above does not hold them; without this, the two-consecutive-passes exit would
    re-raise the identical escalation on the very next pass and the operator would answer
    the same question forever. Resetting the counter is the whole content of "the answer
    was spent": the rows must again go unreached by two passes that both postdate the
    answer before the fork may be raised a second time.
    """
    resets: dict[str, int] = {}
    for m, row_ids in _coverage_row_messages(messages):
        if m.kind != "decision_response" or m.role != "operator":
            continue
        if (m.payload or {}).get(COVERAGE_DISPOSITION_KEY) != COVERAGE_KEEP_GATING:
            continue
        for row_id in row_ids:
            resets[str(row_id)] = m.seq
    return resets


def _scope_exempt_rows(messages, manifest: dict | None) -> set[str]:
    """Rows closed by the ONE collective declared-scope exemption (B.8 C-1).

    A code-mode manifest built with a declared scope keeps out-of-scope reach rows in the
    denominator, each marked ``out_of_declared_scope``. The OPERATOR closes them all at
    once with a single recorded message — a ``decision_response`` carrying
    ``{"scope_exemption": {"declared_scope": "<the manifest's inputs.declared_scope
    digest>"}}`` — instead of answering one escalation per row for a decision they made
    before the review started (review 59b5ec42: six forks, one per-scope decision).

    Keyed on the DECLARED-SCOPE DIGEST, not the manifest id: rows are content-addressed
    and the scope statement is version-stable, so one exemption survives artifact
    versions until the scope itself changes — at which point the digest differs and the
    exemption honestly lapses.

    Two hard edges: only an OPERATOR-authored message counts (the same trust rule as the
    unreached allowance — the party the gate constrains must not grant itself the
    exemption), and a HIGH-STAKES row is never exemptable this way: load-bearing surface
    is load-bearing wherever the focus sits.
    """
    if manifest is None:
        return set()
    scope_digest = (manifest.get("inputs") or {}).get("declared_scope")
    if not scope_digest:
        return set()
    granted = any(
        m.kind == "decision_response"
        and m.role == "operator"
        and isinstance((m.payload or {}).get("scope_exemption"), dict)
        and (m.payload or {}).get("scope_exemption", {}).get("declared_scope")
        == scope_digest
        for m in messages
    )
    if not granted:
        return set()
    return {
        str(r["row_id"])
        for r in manifest.get("rows") or []
        if isinstance(r, dict)
        and r.get("out_of_declared_scope") is True
        and r.get("high_stakes") is not True
        and r.get("row_id")
    }


def _all_downgraded_by_instrument(messages, row_ids: list[str]) -> bool:
    """Did the instrument, rather than the reading, fail on EVERY row of this group?

    B.11 B-1. The watcher's content re-read holds a coverage row's quote against what the
    referenced read actually served, and mechanically downgrades a mismatch to
    `not-reached` with a reason classified `instrument_failure` — the classification is
    already machine-readable at the moment the escalation is raised; the escalation simply
    never looked at it.

    STRICT, and in both directions. Every row of the group must carry the classification
    in BOTH of the two passes that put it here: a row unreached for a mixed set of reasons
    is not an instrument problem, and a row merely OMITTED from a report carries no reason
    at all, which is the "never looked" case rather than "the evidence did not hold up".
    Getting this wrong in the lenient direction would offer the operator an instrument
    remedy for rows the critic genuinely could not reach — the same shape of false answer
    this element exists to remove.
    """
    if not row_ids:
        return False
    passes = [p for p in _critic_passes(messages) if p.end_seq is not None]
    if len(passes) < 2:
        return False
    reports = [p.reports[-1][1] for p in passes[-2:] if p.reports]
    if len(reports) < 2:
        return False
    wanted = {str(r) for r in row_ids}
    for report in reports:
        classified = {
            str(row.get("row_id"))
            for row in (report.get("rows") or [])
            if isinstance(row, dict)
            and str(row.get("reason") or "").startswith(coverage.INSTRUMENT_FAILURE_REASON)
        }
        if not wanted <= classified:
            return False
    return True


def _persistently_unreached(messages, latest: int | None) -> list[str]:
    """Rows left unreached by the two most recent COMPLETED passes (G-3's exit).

    Two passes, each judged by the LAST report it posted before its status — not the two
    most recent report messages, and never a pass still in flight.

    An unbounded coverage gate is a deadlock with extra steps. A row that two consecutive
    passes could not reach is no longer a loop condition: either it is genuinely
    unreviewable or the scope is wrong, and both of those are the operator's call, not
    something more passes will resolve. Row ids are content-addressed, so "the same row" is
    well defined across artifact versions — which is deliberate: pairing the reports by
    version instead would let posting a new artifact silently reset the counter, and a
    non-deadlock exit that any resubmission resets is not an exit.

    But the row must still be in the manifest for the CURRENT version. A row that no longer
    exists in the denominator is one nobody is being asked to verdict any more, and
    escalating it would spend the operator's attention on a question the review has already
    stopped asking.
    """
    # TWO CONSECUTIVE PASSES. Not two report messages — one pass reporting twice is one
    # pass — and not two report-BEARING passes either: a pass that legitimately posted no
    # report (blocked before reading the subject) sits between them and breaks the run. It
    # did not fail to reach this row; it did not look at all, and erasing it would make two
    # separated passes appear consecutive.
    # EVERY pass counts as a boundary, including one that produced nothing at all. Filtering
    # those out was the same erasure the finding named one level up: a legal pre-review
    # `needs_human` pass sits between two report-bearing passes and BREAKS the run, and
    # dropping it made them look consecutive again.
    # CLOSED passes only. `_critic_passes` always appends the pass still in flight, and an
    # open pass has no verdict yet — its latest report is provisional until its own status
    # ends it. Comparing against that open pass is what let a first `not-reached` report
    # escalate a row the same pass later reached.
    passes = [p for p in _critic_passes(messages) if p.end_seq is not None]
    if len(passes) < 2:
        return []
    last_two = passes[-2:]
    if not all(p.reports for p in last_two):
        return []
    reports = [p.reports[-1][1] for p in last_two]
    # AN `unanswerable` REPORT IS NOT A PER-ROW VERDICT — it claims coverage of NOTHING, by
    # its own contract, and exists so a pass that cannot produce rows can still end
    # honestly. Feeding it to this exit reads "I could not measure" as "I looked at every
    # row and reached none of them", and the exit then escalates the WHOLE denominator.
    #
    # Measured live, 2026-08-09 (review 5e57eca1): two consecutive passes answered
    # `unanswerable` — the first because the critic's interpreter could not import the
    # coverage tool, the second because it stopped early on a blocking defect — and the
    # server raised 244 contested-fork escalations, one per manifest row. Every one of them
    # was an open operator item, so the review could not converge until a human answered
    # 244 questions that nobody had actually asked. The channel was abandoned.
    #
    # Convergence stays blocked meanwhile, and that is correct: with nothing measured, the
    # coverage blockers hold. What must not happen is turning an honest "I could not
    # measure" into an operator's inbox.
    if any(r.get("unanswerable") for r in reports):
        return []
    already = _escalated_coverage_rows(messages)
    manifests = {
        (m.payload or {}).get("manifest_id"): m.payload
        for m in messages
        if m.kind == "coverage_manifest"
    }
    current_manifest = None
    for m in messages:
        if m.kind == "coverage_manifest" and (m.payload or {}).get("artifact_seq") == latest:
            current_manifest = m.payload
    if current_manifest is None:
        return []
    live_rows = _manifest_row_ids(current_manifest)

    def unreached(report: dict) -> set[str]:
        """Rows this report left without a REACHED verdict.

        An OMITTED row counts exactly like an explicit ``not-reached`` — the absence of a
        verdict is the "never looked" case, and the coverage gate already reads it that way.
        Reading omission as unreached in the gate but not here left the two-consecutive-
        passes exit unreachable by omission: a row silently dropped from every report would
        block convergence forever without ever becoming the operator's call. Wherever a
        verdict is read, silence means the same thing.
        """
        manifest = manifests.get(report.get("manifest_id"))
        if manifest is None:
            return set()
        verdicted = {
            r.get("row_id")
            for r in (report.get("rows") or [])
            if isinstance(r, dict)
            and r.get("verdict") in ("reviewed-clean", "finding")
            and r.get("row_id")
        }
        return _manifest_row_ids(manifest) - verdicted

    persistent = unreached(reports[-1]) & unreached(reports[-2]) & live_rows
    # B.11 B-2: A `keep_gating` ANSWER RESETS THE COUNTER FOR THE ROWS IT NAMED. Both
    # passes of the run must postdate the answer, and "postdate" is measured on the REPORT
    # that carries the verdict — not on the pass boundary, because a pass that was already
    # running when the operator answered produced its verdict without having seen it.
    # Without this the rows would re-escalate on the very next pass, which is exactly the
    # deadlock the two-pass exit exists to break, only with the operator inside the loop.
    resets = _coverage_gating_resets(messages)
    if resets:
        earliest = min(
            (seq for seq, _ in (p.reports[-1] for p in last_two)),
            default=None,
        )
        if earliest is not None:
            persistent = {r for r in persistent if resets.get(str(r), -1) < earliest}
    # B.8 C-1: scope-exempted rows never reach the operator as unreached forks — the one
    # collective exemption IS the operator's answer about them.
    exempt = already | _scope_exempt_rows(messages, current_manifest)
    return sorted(str(r) for r in persistent - exempt)


def _manifest_row_ids(manifest: dict) -> set[str]:
    return {
        r.get("row_id")
        for r in (manifest.get("rows") or [])
        if isinstance(r, dict) and r.get("row_id")
    }


# --- the load-bearing convergence guard (spec §10, INV-2, INV-4) ---------


async def _open_operator_items(session: AsyncSession, review_id: uuid.UUID) -> list[str]:
    """Escalations + human_questions still awaiting the operator — ORDER-AWARE.

    Walks the timeline by ``seq`` keeping an open-set: an ``escalation`` opens its
    element/finding key (keyless → a unique unclosable key — fail-safe, an ill-formed
    contest can only hold a review open); a ``human_question`` opens its ``id`` (a
    keyless question joins a pool closed by ANY later operator answer — a question is
    milder than a contest, so its fail-safe is softer); a ``decision_response`` closes
    only a key that is ALREADY OPEN at that point — a response can never pre-answer a
    future contest/question (cycle-04 security review, F1: the old set-difference let
    an earlier response silently satisfy a later escalation with the same key).

    Shared by the convergence guard (condition 4) and the un-park recompute, so the
    ``parked``/``pending_human`` flag and the gate always agree (cycle-04 F2).

    THE LOGIC LIVES IN ``round_gate.open_operator_item_keys`` — one implementation for
    this DB-side authority, the watcher's replay and the gate fold. The first B.7 cut kept
    a hand copy there, and the two disagreed in exactly the places this docstring's
    history had paid for (the keyless-question pool; one answer settling a whole fork,
    e9cf8a67) — the server unparked while the watcher waited on a human forever. History
    of the semantics themselves stays with the pure function.
    """
    messages = (
        await session.scalars(
            select(ReviewMessage)
            .where(ReviewMessage.review_id == review_id)
            .order_by(ReviewMessage.seq)
        )
    ).all()
    return round_gate.open_operator_item_keys(_as_dicts(messages))


async def _detect_oscillation(
    session: AsyncSession, review_id: uuid.UUID, findings_payload: dict
) -> list[str]:
    """Finding ids this pass reopens that were already terminally disposed (spec §10).

    ``reopens_finding_id`` is the critic's declared "same issue" link; the server's job
    is the mechanical check that the target actually had a disposition — that is what
    makes it an oscillation (a settled thing re-raised) rather than a continuation.
    """
    targets = [
        f.get("reopens_finding_id")
        for f in (findings_payload.get("items") or [])
        if f.get("reopens_finding_id")
    ]
    if not targets:
        return []
    dispositions = await session.scalars(
        select(ReviewMessage).where(
            ReviewMessage.review_id == review_id, ReviewMessage.kind == "disposition"
        )
    )
    disposed = {(m.payload or {}).get("finding_id") for m in dispositions}
    return [t for t in targets if t in disposed]


@dataclass
class _ConvergenceState:
    """The four §10 convergence facts, computed once from the message log (Part K).

    Consistency only — NOT authorship (option X): the server cannot prove a clean pass
    came from the critic; INV-2 is prompt-maintained in MVP, and the operator's
    Post-review Intent Summary is the backstop.
    """

    latest: int | None  # newest artifact_seq, or None if no artifact posted
    has_clean_pass: bool  # a findings items:[] for `latest` exists
    undisposed: list[str]  # finding ids without a terminal disposition (fixed|waived)
    open_items: list[str]  # escalations/questions still awaiting the operator
    ungrafted_external_defects: list[str]  # external_defect ids lacking a graph_result (J.8.2)
    has_converged_declaration: bool  # the critic posted `status: converged` for `latest`
    unanswered_observations: list[str]  # dev_observation ids no critic pass answered (B.9 G-3)
    unreached_rows: list[str]  # manifest rows for `latest` with no reached verdict (B.6 G-3)
    unreached_allowance: int  # how many of those the depth tier tolerates (T2-1)
    missing_manifest: bool  # coverage is in play but `latest` has no denominator

    @property
    def coverage_blocked(self) -> bool:
        """Coverage is short of the tier's allowance (B.6 G-3).

        The gate lives HERE, in the server, and never in the critic: the critic reports
        unreached rows honestly and declares what it found, and the server accounts. A
        coverage obligation phrased as a critic-side stopping rule would rebuild exactly the
        deadlock that made past reviews stall — the critic re-deriving the ledger from its
        lossy replay and withholding a clean pass over it.
        """
        return len(self.unreached_rows) > self.unreached_allowance

    @property
    def convergeable(self) -> bool:
        """All §10 conditions met — the review may enter `converged`. Includes J.8.2: every
        `external_defect` escalation has its `external_defect_graph_result` ledger record,
        and B.6 G-3: coverage within the depth tier's unreached allowance."""
        return (
            self.latest is not None
            and self.has_clean_pass
            and not self.undisposed
            and not self.open_items
            and not self.ungrafted_external_defects
            and not self.unanswered_observations
            and not self.coverage_blocked
            and not self.missing_manifest
        )


async def _convergence_state(
    session: AsyncSession, review_id: uuid.UUID
) -> _ConvergenceState:
    """Compute the §10 convergence facts from the log — the server's SOLE gate (K.2).

    Shared by the explicit-``converged`` guard, the blocker routing (K.4.2) and the
    server self-completion (K.4.1), so all three read one source of truth: (1) the
    latest artifact version; (2) a materialized clean pass (``findings items:[]``) for
    it; (3) every raised finding terminally disposed; (4) nothing still awaiting the
    operator (order-aware, via ``_open_operator_items``).
    """
    messages = (
        await session.scalars(
            select(ReviewMessage)
            .where(ReviewMessage.review_id == review_id)
            .order_by(ReviewMessage.seq)
        )
    ).all()

    artifact_seqs = [m.seq for m in messages if m.kind == "artifact"]
    latest = max(artifact_seqs) if artifact_seqs else None
    # Order-aware freshness (F16): a clean pass / converged declaration for `latest` is
    # INVALIDATED by any LATER non-empty findings pass for the same artifact. Compute the seq
    # of the last non-empty findings for `latest`; a clean pass or declaration counts only if
    # it comes AFTER it (otherwise the critic found more issues after its clean verdict, and a
    # fresh clean pass is required before the server may (self-)converge).
    last_nonempty_findings_seq = max(
        (
            m.seq
            for m in messages
            if m.kind == "findings"
            and (m.payload or {}).get("artifact_seq") == latest
            and (m.payload or {}).get("items")
        ),
        default=-1,
    )
    # B-5 freshness seam 3 (finding b9-threat-context-not-a-freshness-input): a clean
    # pass counts only if it POSTDATES the latest threat_context record — the same
    # order-aware rule that already invalidates a clean pass overtaken by findings. A
    # restated frame therefore always buys one fresh critic look before convergence;
    # the watcher schedules that pass (frame_repass_due) from the same ordering.
    last_context_seq = max(
        (
            m.seq
            for m in messages
            if m.kind == "notice"
            and (m.payload or {}).get("phase") == round_gate.THREAT_CONTEXT_PHASE
        ),
        default=-1,
    )
    has_clean_pass = latest is not None and any(
        m.kind == "findings"
        and (m.payload or {}).get("artifact_seq") == latest
        and (m.payload or {}).get("items") == []
        and m.seq > last_nonempty_findings_seq
        and m.seq > last_context_seq
        for m in messages
    )
    finding_ids = {
        f.get("id")
        for m in messages
        if m.kind == "findings"
        for f in (m.payload or {}).get("items") or []
        if f.get("id") is not None
    }
    # TERMINAL means terminal, whoever closed it: B.7's two server-emitted outcomes
    # (`operator_risk_accepted`, `fixed_unverified`) settle the ledger exactly as
    # `fixed`/`waived` do. Counting only the LLM-postable pair would leave an
    # operator-finalized review's ledger reading as permanently open.
    disposed = {
        (m.payload or {}).get("finding_id")
        for m in messages
        if m.kind == "disposition"
        and (m.payload or {}).get("outcome") in round_gate.TERMINAL_OUTCOMES
    }
    undisposed = sorted(str(x) for x in (finding_ids - disposed))
    open_items = await _open_operator_items(session, review_id)
    # J.8.2: every external_defect escalation needs its development graph-result record
    # (dedup + write status) before convergence — regardless of freeze/defer/reject.
    external_defect_ids = {
        (m.payload or {}).get("id")
        for m in messages
        if m.kind == "escalation"
        and (m.payload or {}).get("kind") == "external_defect"
        and (m.payload or {}).get("id") is not None
    }
    grafted = {
        (m.payload or {}).get("escalation_ref")
        for m in messages
        if m.kind == "external_defect_graph_result"
    }
    ungrafted = sorted(str(x) for x in (external_defect_ids - grafted))
    # The critic MUST have declared convergence for the latest artifact — a mere clean
    # findings pass is not a convergence declaration (the critic can post empty findings and
    # then `needs_human`). Server self-completion (K.4.1) reuses THIS declaration once its
    # blocker clears; it never invents convergence the critic did not ask for.
    has_converged_declaration = latest is not None and any(
        m.kind == "status"
        and (m.payload or {}).get("value") == "converged"
        and (m.payload or {}).get("artifact_seq") == latest
        # order-aware (F16): the declaration is not superseded by a later non-empty pass
        and m.seq > last_nonempty_findings_seq
        # ...nor by a later threat-context restatement — the claim "done" must be made
        # under the newest frame, like the clean pass it rests on (finding
        # b9-threat-context-freshness-not-pass-bound).
        and m.seq > last_context_seq
        for m in messages
    )
    # B.9 G-3, the ledger's second escape path closed: an unanswered dev_observation is a
    # convergence fact. An observation posted AFTER the latest artifact is owed by no
    # pass anchored to it (by design — the pass could not see it), so without this fact
    # a clean converged pass would end the review with the observation permanently
    # unanswered (finding b9-observation-ledger-escape-paths). The clearing route: an
    # observation-only blocker leaves the review in critic_reviewing and the watcher
    # schedules a SAME-VERSION answering pass (observation_repass_due); a pass may
    # answer any posted observation, and the declaring status discounts its own answers
    # (finding b9-late-observation-no-legal-cleanup-path — this comment previously
    # described the retired dev-posts-next-version route).
    obs_posted: set[str] = set()
    obs_answered: set[str] = set()
    for m in messages:
        p = m.payload or {}
        if m.kind == "notice" and p.get("phase") == round_gate.DEV_OBSERVATION_PHASE:
            oid = p.get("observation_id")
            if isinstance(oid, str) and oid:
                obs_posted.add(oid)
        elif m.kind == "status" and m.role == "critic":
            for od in p.get("observation_dispositions") or []:
                if isinstance(od, dict) and od.get("observation_id"):
                    obs_answered.add(od["observation_id"])
    return _ConvergenceState(
        latest=latest,
        has_clean_pass=has_clean_pass,
        undisposed=undisposed,
        open_items=open_items,
        ungrafted_external_defects=ungrafted,
        has_converged_declaration=has_converged_declaration,
        unanswered_observations=sorted(obs_posted - obs_answered),
        unreached_rows=_coverage_blockers(messages, latest),
        # B.14 B-7: the unreached-rows tolerance is retired in code mode — convergence
        # demands TOTALITY there (every axis of the current version with an accumulated
        # outcome, final pass clean; B-3/B-4). Spec mode keeps the tier's allowance.
        unreached_allowance=(
            0
            if any(
                m.kind == "coverage_manifest"
                and (m.payload or {}).get("artifact_seq") == latest
                and _question_denominator(m.payload)
                for m in messages
            )
            else _unreached_allowance(messages)
        ),
        missing_manifest=_missing_manifest(
            messages,
            latest,
            in_play=await _review_coverage_in_play(session, review_id, messages),
        ),
    )


def _discount_observations_answered_by(check: _ConvergenceState, payload: dict) -> None:
    """Subtract from the convergence facts the observations THIS payload answers.

    ``_convergence_state`` reads the log before the message being validated lands, but a
    critic status may carry the very ``observation_dispositions`` that settle the ledger
    (B.9 G-3, finding b9-late-observation-no-legal-cleanup-path) — without the discount
    the pass that cleared the ledger would be blocked on its own answers.
    """
    answered_now = {
        d.get("observation_id")
        for d in payload.get("observation_dispositions") or []
        if isinstance(d, dict) and d.get("observation_id")
    }
    if answered_now:
        check.unanswered_observations = [
            o for o in check.unanswered_observations if o not in answered_now
        ]


def _raise_if_malformed_converged(check: _ConvergenceState, payload: dict) -> None:
    """Refuse (409) a ``converged`` that is the CRITIC's own error — fail-safe.

    No artifact, a stale version, or no materialized clean pass mean the declaration
    itself is malformed (the critic should not have declared): the review stays open and
    the critic's watcher sees the reason. A settled-ledger blocker (an undisposed finding,
    an open escalation) is NOT refused here — it is someone else's turn and gets ROUTED
    (K.4.2), so the loop wakes the right party instead of stalling.
    """
    if check.latest is None:
        raise ConvergenceRefusedError("no artifact has been posted for this review")
    declared = payload.get("artifact_seq")
    if declared != check.latest:
        raise ConvergenceRefusedError(
            f"converged references artifact_seq {declared!r} but the latest is "
            f"{check.latest} (stale version)"
        )
    if not check.has_clean_pass:
        raise ConvergenceRefusedError(
            f"no materialized clean verifying pass (findings items:[]) for artifact_seq "
            f"{check.latest}"
        )


def _signals_return(payload: dict) -> bool:
    """A relayed operator decision that RETURNS the review or demands an artifact change
    makes any prior clean pass stale — the server must NOT self-complete convergence over it
    (the K.4.1 restriction). The **canonical, documented** field is ``requires_new_artifact``
    (decision_response wire contract, dev_loop.md §2): development sets it when the operator's
    answer needs a new artifact version. ``returns`` / ``requires_artifact_change`` / ``reopen``
    are accepted as legacy aliases. Defence in depth: even if the flag is omitted, a genuine
    return still posts a new artifact, which invalidates the clean pass for the latest version
    and blocks self-completion anyway (server self-completion requires a clean pass for the
    LATEST artifact)."""
    return bool(
        payload.get("requires_new_artifact")
        or payload.get("returns")
        or payload.get("requires_artifact_change")
        or payload.get("reopen")
    )
