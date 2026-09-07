# SPDX-License-Identifier: Apache-2.0
"""review: coverage_manifest + coverage_report message kinds (B.6)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-21 00:00:00.000000

Review-loop rework B.6, Tier-1b coverage (spec elements T1-4 / F-2 / G-3):

- ``coverage_manifest`` — the externally, mechanically derived denominator for one
  artifact version: every row the critic is obliged to reach, each with a stable
  content-addressed id, plus exactly one ``hunt-by-name`` blind-edge row declaring what
  the derivation cannot see.
- ``coverage_report`` — the critic's per-row verdicts against that manifest
  (``reviewed-clean`` / ``finding`` / ``not-reached``). Convergence gates on it: while
  rows are unreached beyond the depth tier's allowance, the server records the critic's
  declaration and routes another pass.

A CheckConstraint edit: drop the old constraint, add the widened one. The two kinds are
ADDED, nothing is removed, so the downgrade is safe only while no message of either kind
exists — which is the same shape as every prior kind-widening here.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"

_KIND_OLD = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'notice')"
)
_KIND_NEW = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','notice')"
)


def upgrade() -> None:
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_NEW, schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_OLD, schema=SCHEMA
    )
