# SPDX-License-Identifier: Apache-2.0
"""review: B.7 round-gate message kinds + operator-final states

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-09 00:00:00.000000

Round-gate rework B.7 («убавить автоматики»): the loop stops at every round, BEFORE
anything is implemented, and the operator marks the round up per finding.

- review_message.kind gains five: ``proposals`` (one entry per finding of the pass,
  nothing implemented yet), ``gate_directive`` (development's relay of the operator's
  per-finding ruling), ``detail_report`` / ``class_report`` (the typed answers to the two
  report directives — neither consumes a critic pass), ``operator_finalize`` (the
  operator's own stop, mode ``now`` or ``after_fixes``).
- review.state gains ``operator_finalizing`` — the ONE state where the after-fixes final
  artifact is accepted — and ``operator_finalized``, a terminal status DISTINCT from
  ``converged`` (only the critic may declare convergence; the operator stopping the
  review is a different claim and the record must never render one as the other).

Both are CheckConstraint edits: drop the old constraint, add the widened one. Nothing is
removed, so the downgrade is safe only while no message of the new kinds and no review in
the new states exist — the same shape as every prior widening here. B.7 applies to reviews
created after deploy (R-1); in-flight reviews keep the old round structure, so this
migration needs no data backfill.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"

_STATE_OLD = (
    "state IN ('created','artifact_ready','critic_reviewing','dev_disposing',"
    "'converged','operator_gate','finalized','returned','frozen','abandoned')"
)
_STATE_NEW = (
    "state IN ('created','artifact_ready','critic_reviewing','dev_disposing',"
    "'converged','operator_gate','finalized','returned','frozen',"
    "'operator_finalizing','operator_finalized','abandoned')"
)
_KIND_OLD = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','notice')"
)
_KIND_NEW = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','proposals','gate_directive','detail_report',"
    "'class_report','operator_finalize','notice')"
)


def upgrade() -> None:
    op.drop_constraint("ck_review_state", "review", schema=SCHEMA, type_="check")
    op.create_check_constraint("ck_review_state", "review", _STATE_NEW, schema=SCHEMA)
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_NEW, schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_OLD, schema=SCHEMA
    )
    op.drop_constraint("ck_review_state", "review", schema=SCHEMA, type_="check")
    op.create_check_constraint("ck_review_state", "review", _STATE_OLD, schema=SCHEMA)
