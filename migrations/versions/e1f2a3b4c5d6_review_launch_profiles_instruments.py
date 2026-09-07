# SPDX-License-Identifier: Apache-2.0
"""review: B.9 C+D — launch profiles, engine model lists, instrument defaults

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-18 00:00:00.000000

B.9 slice 1 (spec docs/design/2026-08-16_review_loop_next_spec.md, Parts C+D — the
protocol-neutral half, sequenced first by S-3):

- ``launch_profile`` + ``launch_profile_version``: the launch stops being assembled "by
  place". A profile is keyed host × engine, where host = OS hostname + OS username pair
  (C-1/C-2); content lives in immutable versions carrying probe evidence; the active
  version is a pointer on the profile (rollback = move it back).
- ``launch_profile_observation``: devaluation is a durable server state entered by
  observation, never by the agent's opinion (C-6); a launch outside the profile's own
  environment is a ``mislaunch`` record and never devalues.
- ``launch_profile_bypass``: a bypass is registered against the probe that justifies it
  (C-7) — a probe passing after a long failure names the bypasses due for removal.
- ``engine_model``: the engine's model list, separate from the machine profile (D-4) —
  invocation alias + official provider model id + provider/family lineage facts +
  supported effort domain; ``verified`` vs ``facts_only`` entry classes.
- ``instrument_default`` + ``instrument_default_version``: the two-level default PAIR
  map (global + per-genre), immutable versions + active pointer, operator quote and a
  resolving graph-Decision ref mandatory per version (D-5).

The review table itself is untouched: the frozen instrument snapshot (D-1) lives inside
``review.config`` (JSONB), whose presence is also the rollout marker — reviews created
before this deploy carry no snapshot and are not stamp-validated (same R-1 shape as B.7).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"


def upgrade() -> None:
    op.create_table(
        "launch_profile",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("hostname", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("engine", sa.String(), nullable=False),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.UniqueConstraint("hostname", "username", "engine", name="uq_launch_profile_key"),
        schema=SCHEMA,
    )

    op.create_table(
        "launch_profile_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("probe_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("devalued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], [f"{SCHEMA}.launch_profile.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("profile_id", "version", name="uq_launch_profile_version"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_launch_profile_version_profile", "launch_profile_version", ["profile_id"], schema=SCHEMA
    )
    # The parent -> child active pointer, added after both tables exist (the FK cycle).
    op.create_foreign_key(
        "fk_launch_profile_active_version",
        "launch_profile",
        "launch_profile_version",
        ["active_version_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )

    op.create_table(
        "launch_profile_observation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("observation", sa.String(), nullable=False),
        sa.Column("observed_hostname", sa.String(), nullable=False),
        sa.Column("observed_username", sa.String(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], [f"{SCHEMA}.launch_profile.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], [f"{SCHEMA}.launch_profile_version.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "kind IN ('devaluation','mislaunch')", name="ck_launch_profile_observation_kind"
        ),
        sa.CheckConstraint(
            "observation IN ('probe_flip','version_divergence','startup_gate_refusal')",
            name="ck_launch_profile_observation_observation",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_launch_profile_observation_profile",
        "launch_profile_observation",
        ["profile_id"],
        schema=SCHEMA,
    )

    op.create_table(
        "launch_profile_bypass",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("probe_ref", sa.String(), nullable=True),
        sa.Column("trigger_text", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["profile_id"], [f"{SCHEMA}.launch_profile.id"], ondelete="CASCADE"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_launch_profile_bypass_profile", "launch_profile_bypass", ["profile_id"], schema=SCHEMA
    )

    op.create_table(
        "engine_model",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("engine", sa.String(), nullable=False),
        sa.Column("invocation_alias", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provider_model_id", sa.String(), nullable=True),
        sa.Column("family", sa.String(), nullable=True),
        sa.Column("effort_domain", postgresql.JSONB(), nullable=True),
        sa.Column("entry_class", sa.String(), nullable=False),
        sa.Column("verification", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.CheckConstraint(
            "entry_class IN ('verified','facts_only')", name="ck_engine_model_class"
        ),
        sa.UniqueConstraint("engine", "invocation_alias", name="uq_engine_model_alias"),
        schema=SCHEMA,
    )
    op.create_index("ix_engine_model_engine", "engine_model", ["engine"], schema=SCHEMA)

    op.create_table(
        "instrument_default",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "instrument_default_version",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("default_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("pair_map", postgresql.JSONB(), nullable=False),
        sa.Column("operator_quote", sa.String(), nullable=False),
        sa.Column("decision_ref", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.ForeignKeyConstraint(
            ["default_id"], [f"{SCHEMA}.instrument_default.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("default_id", "version", name="uq_instrument_default_version"),
        schema=SCHEMA,
    )
    op.create_foreign_key(
        "fk_instrument_default_active_version",
        "instrument_default",
        "instrument_default_version",
        ["active_version_id"],
        ["id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_instrument_default_active_version", "instrument_default", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_table("instrument_default_version", schema=SCHEMA)
    op.drop_table("instrument_default", schema=SCHEMA)
    op.drop_table("engine_model", schema=SCHEMA)
    op.drop_table("launch_profile_bypass", schema=SCHEMA)
    op.drop_table("launch_profile_observation", schema=SCHEMA)
    op.drop_constraint(
        "fk_launch_profile_active_version", "launch_profile", schema=SCHEMA, type_="foreignkey"
    )
    op.drop_table("launch_profile_version", schema=SCHEMA)
    op.drop_table("launch_profile", schema=SCHEMA)
