# SPDX-License-Identifier: Apache-2.0
"""review: external_defect_graph_result kind + frozen state

Revision ID: b8c9d0e1f2a3
Revises: f6a7b8c9d0e1
Create Date: 2026-07-15 00:00:00.000000

Intent-genre change, Part E (external defect + Part K handshake):

- review_message.kind gains ``external_defect_graph_result`` (spec J.8.2) — development's
  ledger record of the graph handling (dedup + write status) of an ``external_defect``
  escalation; convergence requires one per external_defect.
- review.state gains ``frozen`` (spec J.3) — a park variant an external_defect FREEZE puts
  the review in while development fixes the base defect; the review resumes from it.

Both are CheckConstraint edits: drop the old constraint, add the widened one.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"

_STATE_OLD = (
    "state IN ('created','artifact_ready','critic_reviewing','dev_disposing',"
    "'converged','operator_gate','finalized','returned','abandoned')"
)
_STATE_NEW = (
    "state IN ('created','artifact_ready','critic_reviewing','dev_disposing',"
    "'converged','operator_gate','finalized','returned','frozen','abandoned')"
)
_KIND_OLD = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','notice')"
)
_KIND_NEW = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'notice')"
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
