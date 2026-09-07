# SPDX-License-Identifier: Apache-2.0
"""review: B.12 C-1 — the operator projection as a channel record

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-26 00:00:00.000000

B.12 (spec docs/design/2026-08-25_development_cycle_and_reconciliation_form_spec.md,
Part C) gives every item the loop routes to the operator a HUMAN PROJECTION, and makes
that projection a record rather than a habit:

- ``operator_projection`` — one record carrying the machine item VERBATIM beside its
  four-part translation (what happened / what it concerns / what development proposes /
  what each answer will do), under the key the server already computes for that open
  operator item.

**Why a kind and not a notice.** The same reason B.11 gave for the map, restated because
it is the reason and not an analogy: an exception inside a shared form has to be restated
in every future branch that touches the form, while a distinct kind cannot collide with
them by construction. A projection carried as a general-purpose ``notice`` would also be
indistinguishable, at read time, from development's rationale notices — which are
deliberately hidden from the critic's projection of the channel.

**This is the ONE migration of the slice, and it is required by exactly one element.**
The scope table of the spec said "no database migration" until round 5 of its review and
was corrected there rather than quietly: the projection acquired its own message kind at
round 2, and ``review_message.kind`` is constrained by an enumerated check constraint, so
a new kind cannot be posted without widening it. Everything else in B.12 is validation,
graph resolution, prompt text, rendering, or a creation-time config key — the launch
probe's tool version (D-2) lands in the already-nullable JSONB evidence column, which is
why that element states "no migration" rather than hoping for one.

Nothing is removed and no message of the new kind can exist before this runs, so the
downgrade is safe exactly while none has been written. Once one exists the reversal is
refused HERE, with the count and the route that does exist, rather than left to surface as
a check-constraint violation from PostgreSQL — the shape B.11's migration settled: the
channel is append-only and is never rewritten, which is a stronger invariant than the
convenience of a reversal.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review_orchestration"

_KIND_OLD = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','proposals','gate_directive','detail_report',"
    "'class_report','operator_finalize','notice','map','map_disposition','reconciliation')"
)
_KIND_NEW = (
    "kind IN ('artifact','findings','disposition','status','human_question',"
    "'waiver','dissent','escalation','decision_response','external_defect_graph_result',"
    "'coverage_manifest','coverage_report','proposals','gate_directive','detail_report',"
    "'class_report','operator_finalize','notice','map','map_disposition','reconciliation',"
    "'operator_projection')"
)

#: The kind this migration adds — named once so the guard below and the widened
#: constraint above cannot drift apart.
_ADDED_KINDS = ("operator_projection",)


def upgrade() -> None:
    op.drop_constraint("ck_review_message_kind", "review_message", schema=SCHEMA, type_="check")
    op.create_check_constraint(
        "ck_review_message_kind", "review_message", _KIND_NEW, schema=SCHEMA
    )


def downgrade() -> None:
    live = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT count(*) FROM {SCHEMA}.review_message "
                "WHERE kind IN ('operator_projection')"
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
