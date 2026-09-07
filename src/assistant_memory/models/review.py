# SPDX-License-Identifier: Apache-2.0
"""Review-orchestration tables (schema `review_orchestration`).

Transient coordination state for the automated dev<->critic review loop: reviews,
their append-only message log, and per-review auth tokens. Deliberately isolated in
its OWN Postgres schema so this ephemeral state never mixes with the knowledge-graph
tables — the durable OUTCOME of a review is folded into the graph separately, on
convergence (spec docs/design/2026-07-07_review_orchestration_spec.md §7/§13, INV-7).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, _check_values

SCHEMA = "review_orchestration"

# --- text "enums" (kept as text + CheckConstraint, per the codebase convention) ---
# Review mode selects the critic-scope profile (spec §7.1).
REVIEW_MODES = ("spec", "code")
# The review lifecycle FSM (spec §5). `parked` / transport are orthogonal flags, not states.
# `frozen` (spec Part J.3) is a park variant an `external_defect` FREEZE puts the review in
# while development goes to fix the base defect; the review RESUMES from it (not terminal).
# `operator_finalizing` / `operator_finalized` (B.7 F-1, W-3) carry the operator's own stop:
# the first is the ONE state where the after-fixes final artifact is accepted, the second is
# a terminal status DISTINCT from `converged` — "the operator stopped the review" and "the
# critic declared convergence" are different claims and the record must never render one as
# the other.
REVIEW_STATES = (
    "created",
    "artifact_ready",
    "critic_reviewing",
    "dev_disposing",
    "converged",
    "operator_gate",
    "finalized",
    "returned",
    "frozen",
    "operator_finalizing",
    "operator_finalized",
    "abandoned",
)
# Author of a message. `operator` messages are relayed by development (option C, §8);
# `system` covers server-emitted records. Only development/critic hold per-review tokens.
MESSAGE_ROLES = ("development", "critic", "operator", "system")
# Message kind = payload shape (spec §4). `external_defect_graph_result` (spec J.8.2) is
# development's ledger record of the graph handling of an `external_defect` escalation
# (dedup outcome + write status); convergence requires one per external_defect (J.8.2).
# `coverage_manifest` / `coverage_report` (B.6, T1-4/F-2) are the external coverage
# denominator and the critic's per-row verdicts against it; convergence gates on the
# report's unreached rows (G-3).
# The B.7 round gate adds five: `proposals` (one entry per finding of a pass, posted BEFORE
# anything is implemented), `gate_directive` (development's relay of the operator's
# per-finding ruling), `detail_report` / `class_report` (the answers to the two report
# directives — neither consumes a critic pass), `operator_finalize` (the operator's own
# stop, in either mode).
MESSAGE_KINDS = (
    "artifact",
    "findings",
    "disposition",
    "status",
    "human_question",
    "waiver",
    "dissent",
    "escalation",
    "decision_response",
    "external_defect_graph_result",
    "coverage_manifest",
    "coverage_report",
    "proposals",
    "gate_directive",
    "detail_report",
    "class_report",
    "operator_finalize",
    "notice",
    # B.11 Part E/F — the operator's own gate at the end of a development cycle. `map` is
    # the critic's cold reading of the FINAL CODE, written in Russian to the operator's
    # frozen profile; `map_disposition` is the operator's ruling over it (accepted /
    # new_cycle / task_in_graph); `reconciliation` is the auxiliary trailer that lays the
    # map beside the cycle's four intents.
    #
    # THEY ARE KINDS RATHER THAN ARTIFACTS ON PURPOSE, and it is the whole mechanism of
    # the operator's ruling «Карта ничего не переоткроет. Это мое и только мое решение»:
    # every `artifact` posted while a review is `converged` moves it back to
    # `artifact_ready`, the sole exception being the post-review intent summary. A second
    # exception in that transition was considered and declined — an exception must be
    # restated in every future branch of the transition, while a distinct kind cannot
    # collide with it by construction.
    "map",
    "map_disposition",
    "reconciliation",
    # B.12 C-1 — the human projection of an item routed to the operator. ONE record
    # carries the machine item VERBATIM and its four-part translation side by side, under
    # the key the server already computes for that open item
    # (`round_gate.open_operator_item_keys`). It is a kind of its own for the reason
    # already settled above for the map: an exception inside a shared form has to be
    # restated in every future branch that touches the form, while a distinct kind cannot
    # collide with them by construction. Posted by DEVELOPMENT — judging how to explain
    # something is a semantic act, and the server performs none.
    "operator_projection",
)
# Per-review tokens are minted only for the two acting roles; the operator uses the
# box's bootstrap credential, not a per-review token (option X, §8).
TOKEN_ROLES = ("development", "critic")
# B.9 Part C: what a profile observation record claims. A `devaluation` marks the observed
# version devalued in the store (C-6); a `mislaunch` records "launched outside its
# environment" and NEVER devalues the source profile (the refusal was the script working).
PROFILE_OBSERVATION_KINDS = ("devaluation", "mislaunch")
# The mechanical triggers that justify a devaluation (C-6): the agent does not decide the
# environment changed — it discovers it. A mislaunch record reuses `startup_gate_refusal`
# (the host-pair check is part of the same gate).
PROFILE_OBSERVATIONS = ("probe_flip", "version_divergence", "startup_gate_refusal")
# B.9 Part D: two classes of model-list entry (D-4). `verified` (one verification run on
# the engine) makes a model selectable as critic; `facts_only` exists so a development
# instrument can resolve its provider/family facts for the independence computation (D-3)
# — it does not make the model critic-selectable.
MODEL_ENTRY_CLASSES = ("verified", "facts_only")
# B.9 Part B (B-5): where a threat-boundary registry entry applies. `deployment` entries
# stand for every review; `review` entries are minted from an in-review operator ruling
# and are eligible only for that review — promotion to deployment level is an explicit
# management act, never automatic.
BOUNDARY_SCOPES = ("deployment", "review")
# The two authorized write paths of the registry (B-5): the management endpoint (an
# operator act with their quote) and the `records_boundary` marker on a relayed
# in-review operator message.
BOUNDARY_SOURCES = ("management", "review_ruling")


class Review(Base):
    """One review session — the unit isolated by `id` (spec §3.1)."""

    __tablename__ = "review"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, server_default="created")
    # `parked` and manual-transport are orthogonal flags (spec §5), not FSM states.
    parked: Mapped[bool] = mapped_column(server_default=text("false"))
    iteration: Mapped[int] = mapped_column(Integer, server_default="0")
    artifact_ref: Mapped[dict | None] = mapped_column(JSONB)
    config: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Reset when the operator makes a material change; the no-progress window (§10)
    # is measured from here, not from the review's start.
    last_material_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_check_values("mode", REVIEW_MODES), name="ck_review_mode"),
        CheckConstraint(_check_values("state", REVIEW_STATES), name="ck_review_state"),
        Index("ix_review_state", "state"),
        {"schema": SCHEMA},
    )


class ReviewMessage(Base):
    """Append-only message on a review's channel (spec §3.1, §4).

    `seq` is monotonic per review (the long-poll cursor: GET ...?after={seq}).
    Payload shape depends on `kind`; version anchoring (`artifact_seq`) and one-shot
    lock refs (`decision_ref`) live inside the jsonb payload, not as columns.
    """

    __tablename__ = "review_message"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.review.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_check_values("role", MESSAGE_ROLES), name="ck_review_message_role"),
        CheckConstraint(_check_values("kind", MESSAGE_KINDS), name="ck_review_message_kind"),
        UniqueConstraint("review_id", "seq", name="uq_review_message_seq"),
        Index("ix_review_message_review_seq", "review_id", "seq"),
        {"schema": SCHEMA},
    )


class ReviewToken(Base):
    """Per-review, role-scoped bearer token (isolation only, spec §8).

    A token resolves to exactly one review_id; role here is a label/audit field, NOT
    an authorization input in MVP (option X — role is prompt-maintained, not
    token-enforced). Only the sha256 hash is stored.
    """

    __tablename__ = "review_token"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.review.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String)
    token_hash: Mapped[str] = mapped_column(String)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(_check_values("role", TOKEN_ROLES), name="ck_review_token_role"),
        UniqueConstraint("token_hash", name="uq_review_token_hash"),
        Index("ix_review_token_review", "review_id"),
        {"schema": SCHEMA},
    )


# --- B.9 Part C/D: launch profiles, engine model lists, instrument defaults ----------
#
# Spec docs/design/2026-08-16_review_loop_next_spec.md. The launch stops being assembled
# "by place": the machine part is a frozen, host-proven profile version (C-1/C-2), the
# server renders the launch script from it (C-3), and the instrument that runs a review is
# chosen at creation and frozen into the review's snapshot (D-1). None of these tables
# hold protocol — the protocol template lives with the service and is joined at render.


class LaunchProfile(Base):
    """One host × critic-engine launch profile (C-1).

    "Host" is the OS hostname + OS username PAIR (C-2, Q-3): the same box under a
    different OS user is a different environment — its own token files, its own PATH,
    its own installs. Content lives in immutable versions; this row is the identity and
    the active-version pointer (rollback = move the pointer).
    """

    __tablename__ = "launch_profile"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    hostname: Mapped[str] = mapped_column(String)
    username: Mapped[str] = mapped_column(String)
    engine: Mapped[str] = mapped_column(String)
    # FK into the versions table that itself FKs back here — created after both tables
    # exist (use_alter), the standard resolution of the parent<->child pointer cycle.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            f"{SCHEMA}.launch_profile_version.id",
            use_alter=True,
            name="fk_launch_profile_active_version",
        )
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("hostname", "username", "engine", name="uq_launch_profile_key"),
        {"schema": SCHEMA},
    )


class LaunchProfileVersion(Base):
    """One immutable profile version + its evidence of operability (C-2).

    `content` is machine-only (repo root, interpreter, absolute engine-binary path,
    sandbox invocation form, probe commands, token-file path, notification-channel
    commands, projection root). `probe_evidence` records when and on which host pair the
    probes passed — "it worked when we wrote it" and "it works here now" are different
    facts, and this stores the second one. `devalued_at` is the durable devaluation state
    (C-6): set by an accepted devaluation observation, never cleared — the exit from the
    devalued state is a NEW proven version becoming active, not this one healing.
    """

    __tablename__ = "launch_profile_version"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.launch_profile.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSONB)
    probe_evidence: Mapped[dict | None] = mapped_column(JSONB)
    devalued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("profile_id", "version", name="uq_launch_profile_version"),
        Index("ix_launch_profile_version_profile", "profile_id"),
        {"schema": SCHEMA},
    )


class LaunchProfileObservation(Base):
    """A durable observation about a profile version: devaluation or mislaunch (C-6).

    Host-bound: the record carries the LIVE pair observed at the failure. The repository
    accepts a `devaluation` only when that pair equals the profile key's pair; a launcher
    run in a foreign environment refuses correctly, and that refusal is recorded as a
    `mislaunch` — never devaluing the source profile.
    """

    __tablename__ = "launch_profile_observation"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.launch_profile.id", ondelete="CASCADE")
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.launch_profile_version.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String)
    observation: Mapped[str] = mapped_column(String)
    observed_hostname: Mapped[str] = mapped_column(String)
    observed_username: Mapped[str] = mapped_column(String)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            _check_values("kind", PROFILE_OBSERVATION_KINDS),
            name="ck_launch_profile_observation_kind",
        ),
        CheckConstraint(
            _check_values("observation", PROFILE_OBSERVATIONS),
            name="ck_launch_profile_observation_observation",
        ),
        Index("ix_launch_profile_observation_profile", "profile_id"),
        {"schema": SCHEMA},
    )


class LaunchProfileBypass(Base):
    """A registered launch bypass, bound to the probe that justifies it (C-7).

    Not "revisit later" but "this bypass stands while probe `probe_ref` fails" — a probe
    that passes for the first time after a long failure is itself the notification that
    the bypasses hanging on it are due for removal. `probe_ref` is nullable for the
    honest reservation: some bypasses have no probe ("the model stubbornly does the
    wrong thing"); there `trigger_text` records in words what to check, and the check
    stays human. `removed_at` closes the record.
    """

    __tablename__ = "launch_profile_bypass"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.launch_profile.id", ondelete="CASCADE")
    )
    probe_ref: Mapped[str | None] = mapped_column(String)
    trigger_text: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_launch_profile_bypass_profile", "profile_id"),
        {"schema": SCHEMA},
    )


class ThreatBoundary(Base):
    """One recorded threat-model boundary (B.9 B-5) — the durable source of the standing
    threat frame.

    The registry has exactly two write paths: deployment-level entries enter through the
    management endpoint as an operator act carrying their words (`source="management"`,
    `scope="deployment"`); an in-review operator ruling becomes an entry when the relayed
    operator message carries the typed `records_boundary {text}` marker — the server
    validates the marker, mints the entry (`source="review_ruling"`, `scope="review"`,
    bound to that review) and returns its id. A review's standing frame is derived from
    scope: the deployment entries plus the entries of THIS review. A `boundary_ref` on a
    development waive classifies the gate entry mechanical only when it resolves to an
    entry eligible for the current review.
    """

    __tablename__ = "threat_boundary"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    text_: Mapped[str] = mapped_column("text", String)
    source: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String)
    review_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(f"{SCHEMA}.review.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(_check_values("scope", BOUNDARY_SCOPES), name="ck_threat_boundary_scope"),
        CheckConstraint(
            _check_values("source", BOUNDARY_SOURCES), name="ck_threat_boundary_source"
        ),
        # The scope IS the binding: a review-scoped entry names its review, a
        # deployment-scoped one names none — a mismatch has no meaning to store.
        CheckConstraint(
            "(scope = 'review') = (review_id IS NOT NULL)",
            name="ck_threat_boundary_scope_review",
        ),
        Index("ix_threat_boundary_review", "review_id"),
        {"schema": SCHEMA},
    )


class EngineModel(Base):
    """One entry of an engine's model list (D-4) — *what to choose from*, kept apart from
    the profile (*how to launch*) so a model release is a cheap append, not a new frozen
    profile version.

    `invocation_alias` is the engine-local name the CLI takes; `provider_model_id` is the
    provider's OWN name for the model (what Anthropic/OpenAI call it) — the identity the
    self-check gate compares on (D-3), operator-entered, nullable because an unfilled
    identity refuses REVIEW CREATION with a route, not entry creation. `provider` +
    `family` are the operator-entered lineage facts («на совести оператора», Q-4);
    an absent family resolves conservatively at computation time and never blocks.
    `effort_domain` is the list of effort values the engine's interface accepts for this
    model — a recorded interface fact, not an enumeration result (D-4).
    """

    __tablename__ = "engine_model"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    engine: Mapped[str] = mapped_column(String)
    invocation_alias: Mapped[str] = mapped_column(String)
    provider: Mapped[str] = mapped_column(String)
    provider_model_id: Mapped[str | None] = mapped_column(String)
    family: Mapped[str | None] = mapped_column(String)
    effort_domain: Mapped[list | None] = mapped_column(JSONB)
    entry_class: Mapped[str] = mapped_column(String)
    # The D-4 verification run for `verified` entries: engine accepted the identifier,
    # the run's effort mapped through, the sandbox held — plus when and on which pair.
    verification: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            _check_values("entry_class", MODEL_ENTRY_CLASSES), name="ck_engine_model_class"
        ),
        UniqueConstraint("engine", "invocation_alias", name="uq_engine_model_alias"),
        Index("ix_engine_model_engine", "engine"),
        {"schema": SCHEMA},
    )


class InstrumentDefault(Base):
    """The singleton parent of the default-instrument record (D-5): identity + active
    pointer, in the same "immutable versions + active pointer" shape as launch profiles.
    Created lazily on the first stored version; one row per deployment.
    """

    __tablename__ = "instrument_default"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            f"{SCHEMA}.instrument_default_version.id",
            use_alter=True,
            name="fk_instrument_default_active_version",
        )
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = ({"schema": SCHEMA},)


class InstrumentDefaultVersion(Base):
    """One immutable version of the two-level default-instrument PAIR map (D-5).

    `pair_map` = {"global": {development, critic}, "per_genre": {<genre>: same shape}}.
    Every version is an explicit operator act: `operator_quote` (verbatim) and
    `decision_ref` (a graph Decision node id, validated to RESOLVE at write time) are
    mandatory — the repository refuses a version missing either. A new model never
    becomes the default by itself.
    """

    __tablename__ = "instrument_default_version"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    default_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.instrument_default.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    pair_map: Mapped[dict] = mapped_column(JSONB)
    operator_quote: Mapped[str] = mapped_column(String)
    decision_ref: Mapped[uuid.UUID] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("default_id", "version", name="uq_instrument_default_version"),
        {"schema": SCHEMA},
    )
