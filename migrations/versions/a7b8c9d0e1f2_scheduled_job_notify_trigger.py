# SPDX-License-Identifier: Apache-2.0
"""scheduler NOTIFY: DB trigger emitting pg_notify on ScheduledJob change

Revision ID: a7b8c9d0e1f2
Revises: f4a1b2c3d4e5
Create Date: 2026-07-05 16:00:00.000000

D6 precision layer (actionable layer, slice 2): a row trigger on `nodes` raises NOTIFY on
the `scheduled_job_changed` channel whenever a ScheduledJob is inserted/updated, so the
resident runner reacts immediately instead of waiting for the rescan. The trigger is cheap
(a single type check on NEW); only ScheduledJob rows emit. Losing the listener degrades to
rescan-latency, not incorrectness.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | Sequence[str] | None = "f4a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION notify_scheduled_job_change() RETURNS trigger AS $$
        BEGIN
            IF NEW.type = 'ScheduledJob' THEN
                PERFORM pg_notify('scheduled_job_changed', NEW.id::text);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER scheduled_job_notify
            AFTER INSERT OR UPDATE ON nodes
            FOR EACH ROW EXECUTE FUNCTION notify_scheduled_job_change();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS scheduled_job_notify ON nodes;")
    op.execute("DROP FUNCTION IF EXISTS notify_scheduled_job_change();")
