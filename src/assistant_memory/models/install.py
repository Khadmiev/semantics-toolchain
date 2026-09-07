# SPDX-License-Identifier: Apache-2.0
"""Install-state store (B.13 Part A, spec docs/design/2026-08-27_public_repo_install_process_spec.md).

Three tables back the install state machine. None of them IS the state — A-1's rule is
that the state is recomputed from server-checkable facts, and a stored flag alone is
never the state. What these tables hold is the facts that cannot live anywhere else:

- ``install_settings`` — the canonical store for every value the bootstrap must persist
  for the running process (A-1 stage 3): the conventions-home routing id (identity is
  the recorded id, never a name), the release identities (A-4: the PENDING TARGET an
  apply is trying to reach, and the ACTIVE INSTALLED release a successful apply was
  last proven on — two values, never one), and the installation generation counter
  (A-4/A-5: persisted, monotonic, starts at one, advances only on a recorded
  regression event). A key-value shape, written by the bootstrap in the same
  transaction as the facts it records; the running process reads through this store at
  use time, never from import-time environment parsing.

- ``install_completion_records`` — A-4's completion record, written by the server on
  its own observation of the canonical probe. Carries what was observed, when, under
  which credential, the installation generation current at the observation, the
  CONFIGURATION IDENTITY the loop closed under, and the release it was proven on.
  Records are history: never deleted, never re-armed — stage 5 requires a record
  matching the CURRENT generation, configuration identity, and active release.

- ``install_regression_events`` — A-5's durable invalidation event. A regression is an
  EVENT, not a value comparison: on first observing a structural predicate regress the
  server records one row and advances the generation, so an exact-value repair still
  leaves stage 5 unclosed until a new loop-close writes a record for the new
  generation.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# The closed key vocabulary of install_settings. A closed set rather than free-form:
# every writer of this store is install machinery shipped with the product, and an
# unknown key is a defect, not an extension point.
SETTING_CONVENTIONS_SPACE_ID = "conventions_space_id"
SETTING_PENDING_RELEASE = "pending_release"  # {"tag": str|None, "commit": str}
SETTING_ACTIVE_RELEASE = "active_release"  # {"tag": str|None, "commit": str}
SETTING_INSTALLATION_GENERATION = "installation_generation"  # int, starts at 1

INSTALL_SETTING_KEYS = (
    SETTING_CONVENTIONS_SPACE_ID,
    SETTING_PENDING_RELEASE,
    SETTING_ACTIVE_RELEASE,
    SETTING_INSTALLATION_GENERATION,
)


class InstallSetting(Base):
    """One installer-owned value the running process must be able to read back."""

    __tablename__ = "install_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | list | str | int | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InstallCompletionRecord(Base):
    """One observed close of the end-to-end loop (A-4). Append-only history."""

    __tablename__ = "install_completion_records"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Under which credential the canonical probe arrived. The account row outlives
    # credential rotation, so the record keys on it; the credential id is carried as
    # observation detail.
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"))
    # What was observed, as data: {"operation": "conventions", "credential_id": ...}.
    observation: Mapped[dict] = mapped_column(JSONB)
    # The installation generation current at the observation (A-4). Stage 5 requires
    # a record for the CURRENT generation; earlier generations are history forever.
    generation: Mapped[int] = mapped_column(Integer)
    # The configuration identity the loop closed under: a digest over exactly the
    # config stage's required-settings list (A-4 joins the two one-to-one).
    config_identity: Mapped[str] = mapped_column(String)
    # The release the loop was proven on (A-4): the ACTIVE release at observation.
    release_tag: Mapped[str | None] = mapped_column(String)
    release_commit: Mapped[str] = mapped_column(String)

    __table_args__ = (
        Index("ix_install_completion_generation", "generation"),
    )


class InstallRegressionEvent(Base):
    """One observed structural regression (A-5). Durable; advances the generation."""

    __tablename__ = "install_regression_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # The first failed stage's name at observation ("config", "seed", "owner"), or
    # "config_identity" when the structural stages hold but the live configuration
    # no longer matches the persisted identity of the record (A-5).
    failed_stage: Mapped[str] = mapped_column(String)
    # Diagnostic detail (which predicate failed, what was expected/observed) — for
    # the status surface and the runbook, never an input to any decision.
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    generation_before: Mapped[int] = mapped_column(Integer)
    generation_after: Mapped[int] = mapped_column(Integer)
