"""reminder_log — journal of sent deadline reminders (v6 port)

Idempotency of the reminder worker (:mod:`src.workers.reminders`): one
row for each sent threshold (5/3/1/0 days) for each pair
(tenant, tender). UNIQUE ``(tenant_id, tender_id, threshold_days)`` — the guarantee
“one threshold — once”: the worker does claim-then-send via
``INSERT ... ON CONFLICT DO NOTHING RETURNING id``.

Additive on top of 0008 (single head). No DROP/ALTER of existing objects —
only CREATE of a new table + UNIQUE + an RLS policy (rolled out to the LIVE prod).

RLS: a direct ``tenant_id`` column → a direct ``tenant_isolation`` policy (as
``profiles``/``tender_user_status``): ``ENABLE ROW LEVEL SECURITY`` + ``USING
(tenant_id = current_setting('app.current_tenant_id')::uuid)``. The worker writes
privileged (the owner bypasses RLS); the policy is kept for schema consistency.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-05
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "reminder_log",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column(
            "tenant_id",
            UUID,
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tender_id",
            UUID,
            sa.ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("threshold_days", sa.Integer(), nullable=False),
        sa.Column("sent_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint(
            "tenant_id",
            "tender_id",
            "threshold_days",
            name="reminder_log_tenant_tender_threshold_uniq",
        ),
    )

    # RLS: direct tenant_id column → direct tenant_isolation policy (§4),
    # as on profiles / tender_user_status.
    op.execute("ALTER TABLE reminder_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON reminder_log "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )


def downgrade() -> None:
    # DROP TABLE removes the UNIQUE and the tenant_isolation policy together with the table.
    op.execute("DROP TABLE IF EXISTS reminder_log CASCADE")
