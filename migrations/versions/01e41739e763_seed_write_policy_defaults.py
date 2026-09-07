# SPDX-License-Identifier: Apache-2.0
"""seed write policy defaults

Revision ID: 01e41739e763
Revises: 5101366eff46
Create Date: 2026-06-29 22:24:45.581558

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "01e41739e763"
down_revision: str | Sequence[str] | None = "5101366eff46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPERATIONS = ["create", "update", "delete", "share", "unshare"]
SENSITIVITIES = ["low", "normal", "high", "critical"]

# base[sensitivity][operation]: allow everywhere except critical mutations (unshare stays allow).
TRUST_FLOORS = [("trusted", "allow"), ("limited", "confirm"), ("untrusted", "deny")]


def _base_mode(sensitivity: str, operation: str) -> str:
    if sensitivity == "critical" and operation != "unshare":
        return "deny"
    return "allow"


def upgrade() -> None:
    for sensitivity in SENSITIVITIES:
        for operation in OPERATIONS:
            mode = _base_mode(sensitivity, operation)
            op.execute(
                "INSERT INTO sensitivity_mode (sensitivity, operation, mode) "
                f"VALUES ('{sensitivity}', '{operation}', '{mode}') "
                "ON CONFLICT (sensitivity, operation) DO NOTHING"
            )
    for trust, mode in TRUST_FLOORS:
        op.execute(
            "INSERT INTO trust_floor (client_trust, min_mode) "
            f"VALUES ('{trust}', '{mode}') ON CONFLICT (client_trust) DO NOTHING"
        )


def downgrade() -> None:
    op.execute("DELETE FROM trust_floor")
    op.execute("DELETE FROM sensitivity_mode")
