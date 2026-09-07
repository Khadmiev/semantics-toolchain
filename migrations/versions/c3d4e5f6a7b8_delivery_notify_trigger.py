# SPDX-License-Identifier: Apache-2.0
"""delivery NOTIFY: DB trigger emitting pg_notify on Delivery insert

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-07-06 11:00:00.000000

Actionable-layer small option: a row trigger on `nodes` raises NOTIFY on the `delivery_changed`
channel whenever a Delivery is inserted, so the bot's outbound worker drains it immediately
instead of waiting for the poll. Cheap (one type check on NEW); only Delivery rows emit. Losing
the listener degrades to poll-latency, not incorrectness (the poll backstop still drains).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_delivery_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.type = 'Delivery' THEN
                PERFORM pg_notify('delivery_changed', NEW.id::text);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER delivery_notify
            AFTER INSERT ON nodes
            FOR EACH ROW EXECUTE FUNCTION notify_delivery_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS delivery_notify ON nodes;")
    op.execute("DROP FUNCTION IF EXISTS notify_delivery_change();")
