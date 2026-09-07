# SPDX-License-Identifier: Apache-2.0
"""review: B.11 E-8/F-1 — the map, its disposition, and the reconciliation trailer

Revision ID: a1b2c3d4e5f6
Revises: f2a3b4c5d6e7
Create Date: 2026-08-24 00:00:00.000000

B.11 (spec docs/design/2026-08-23_after_refusal_seam_and_semantic_map_gate_spec.md,
Parts E and F) adds the operator's own gate at the end of a development cycle:

- ``map`` — the critic's cold reading of the FINAL CODE at the review's converged branch
  commit, written in Russian to the operator's frozen profile;
- ``map_disposition`` — the operator's ruling over it: ``accepted`` / ``new_cycle`` /
  ``task_in_graph``, with their verbatim words;
- ``reconciliation`` — the auxiliary trailer that lays the map beside the cycle's four
  Intent Summaries, exactly one attempt.

**Why kinds and not artifacts.** Operator ruling of 2026-08-23: «Карта ничего не
переоткроет. Это мое и только мое решение.» Every ``artifact`` posted while a review sits
in ``converged`` moves it back to ``artifact_ready``, the sole exception being the
post-review intent summary. A map posted as an artifact would therefore reopen the circle
by machinery, against the operator's decision. A second exception in the state machine was
considered and declined: an exception must be restated in every future branch of that
transition, while a distinct kind cannot collide with it by construction.

This is the ONE migration of the slice — an earlier draft of the spec claimed there was
none, which was false and is corrected there rather than quietly dropped, because "no
migration" is exactly the promise a deployment reads. Everything else in B.11 is
validation, scheduling, rendering, prompt text, or a creation-time config key.

Nothing is removed and no message of the three new kinds can exist before this runs, so
the downgrade is safe exactly while none has been written — the same shape as every prior
widening of this constraint.

**And the downgrade says so ITSELF rather than dying of a constraint violation** (critic
finding `migration-downgrade-rejects-existing-new-kinds`, round 4 of this slice's own
review). Once a message of a new kind exists, re-imposing the narrow constraint is refused
by the database — so the reversal becomes impossible exactly after the feature has been
used. That is NOT repaired by making the downgrade work: it could only work by deleting
channel content, and the channel is append-only and never rewritten, which is a stronger
invariant than the convenience of a reversal. What is repaired is the LEGIBILITY of the
refusal — it counts the messages, names the number, and points at the route that does
exist. No data is touched either way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"

_KIND_OLD = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','proposals','gate_directive','detail_report',"
    "'class_report','operator_finalize','notice')"
)
_KIND_NEW = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','proposals','gate_directive','detail_report',"
    "'class_report','operator_finalize','notice','map','map_disposition','reconciliation')"
)


def upgrade() -> None:
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_NEW, schema=SCHEMA
    )


#: The kinds this migration adds — named once so the guard below and the widened
#: constraint above cannot drift apart.
_ADDED_KINDS = ("map", "map_disposition", "reconciliation")


def downgrade() -> None:
    live = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT count(*) FROM {SCHEMA}.review_message "
                "WHERE kind IN ('map', 'map_disposition', 'reconciliation')"
            )
        )
        .scalar_one()
    )
    if live:
        raise RuntimeError(
            f"this migration cannot be reversed: {live} message(s) of kinds "
            f"{list(_ADDED_KINDS)} already exist on review channels, and the narrow "
            "constraint this downgrade restores forbids exactly those values. Reversing "
            "would require DELETING channel content — the channel is append-only and is "
            "never rewritten, which is a stronger invariant than the convenience of a "
            "reversal. The route that does exist is restoring the database from a backup "
            "taken before the upgrade. Refused here, with the count, rather than left to "
            "surface as a check-constraint violation from PostgreSQL."
        )
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_OLD, schema=SCHEMA
    )
