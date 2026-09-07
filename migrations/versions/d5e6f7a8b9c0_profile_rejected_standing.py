# SPDX-License-Identifier: Apache-2.0
"""profile entries: the rejected tombstone standing (B.13 A-9)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-28 18:00:00.000000

Candidate rejection — the correction branch of the install-time observations path —
retains the profile-entry row as a terminal TOMBSTONE with the standing literal
``rejected``: a fourth value beside member/candidate/parked. The check constraint is
the only schema surface that names the standing vocabulary (the slot uniqueness
indexes are partial on member/candidate and already exclude it), so this migration
widens exactly that constraint.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "profile_entries"
_NAME = "ck_profile_entries_standing"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, "standing IN ('member','candidate','parked','rejected')"
    )


def downgrade() -> None:
    # Refuses legibly when tombstones exist: narrowing the constraint under live
    # 'rejected' rows would fail at the database with a generic violation; name the
    # situation instead so the operator knows what stands in the way.
    conn = op.get_bind()
    count = conn.exec_driver_sql(
        "SELECT count(*) FROM profile_entries WHERE standing = 'rejected'"
    ).scalar()
    if count:
        raise RuntimeError(
            f"cannot downgrade: {count} profile entr(y/ies) carry the 'rejected' "
            "tombstone standing this revision introduced — resolve them first"
        )
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, "standing IN ('member','candidate','parked')"
    )
