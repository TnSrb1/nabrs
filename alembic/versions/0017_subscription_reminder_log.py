"""subscription_reminder_log — journal of subscription expiry reminders

Idempotency of the subscription reminder worker
(:mod:`src.workers.subscription_reminders`): one row for each sent
threshold (7 / 1 days) for each pair (tenant_id,
access_until_snapshot). UNIQUE ``(tenant_id, threshold_days,
access_until_snapshot)`` — the guarantee "one threshold for one expiry date —
once": the worker does claim-then-send via
``INSERT ... ON CONFLICT DO NOTHING RETURNING id``.

Key property: on **renewal** of the subscription (new access_until)
access_until_snapshot changes → new UNIQUE key → the thresholds fire again
(the next cycle claims and sends again). This distinguishes the table from reminder_log (where the snapshot is the tender) and summary_log
(where the snapshot is the date).

Additive on top of 0016 (single head). No DROP/ALTER of existing objects
— only CREATE of a new table + UNIQUE + RLS policy (rolled out to the LIVE
prod).

RLS: a direct ``tenant_id`` column → a direct ``tenant_isolation`` policy
(as ``reminder_log`` / ``summary_log``): ``ENABLE ROW LEVEL SECURITY`` +
``USING (tenant_id = current_setting('app.current_tenant_id')::uuid)``.
The worker writes privileged (the owner bypasses RLS); the policy is kept for
schema consistency.

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "subscription_reminder_log",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("threshold_days", sa.Integer(), nullable=False),
        sa.Column("access_until_snapshot", TS, nullable=False),
        sa.Column("sent_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint(
            "tenant_id",
            "threshold_days",
            "access_until_snapshot",
            name="subscription_reminder_log_tenant_threshold_snapshot_uniq",
        ),
    )

    # RLS: direct tenant_id column → direct tenant_isolation policy (§4),
    # as on reminder_log / summary_log.
    op.execute("ALTER TABLE subscription_reminder_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON subscription_reminder_log "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )


def downgrade() -> None:
    # DROP TABLE removes the UNIQUE and the tenant_isolation policy together with the table.
    op.execute("DROP TABLE IF EXISTS subscription_reminder_log CASCADE")
