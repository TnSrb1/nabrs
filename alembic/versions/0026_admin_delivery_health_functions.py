"""SECURITY DEFINER functions for the delivery/queue health panel (VG-29.3)

``/admin/health`` (step 29.3) shows the operator the delivery queue backlog and
the freshness of background workers without SSH. Sources: ``delivery_outbox`` (JOIN on
``profiles.tenant_id``, RLS_MATRIX cat. “b”) and the reminder journals
``reminder_log`` / ``subscription_reminder_log`` (direct ``tenant_id``,
cat. “a”) — ALL under ``FORCE ROW LEVEL SECURITY`` (0021). The operator looks
AT ONCE across all tenants, so ``set_tenant`` does not fit (it would narrow to
one tenant). Under the ``tendersrb_app`` role (non-superuser, VG-01) the policies
``USING (…)`` without a set GUC raise ``UndefinedObjectError`` → 500.

Decision (pattern 1:1 with ``app_admin_*`` from 0025 / ``app_resolve_*`` from
0021–0024): SECURITY DEFINER functions (owner — superuser ``tendersrb``)
bypass FORCE RLS, ``GRANT EXECUTE ... TO tendersrb_app``. The functions return
ONLY aggregates (COUNT/MAX) — no per-row tenant data / PII.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Delivery queue backlog: aggregates over delivery_outbox (cat. “b”, JOIN
    # profiles.tenant_id → FORCE RLS). COUNT/MAX only, no tenant rows.
    # “Awaiting delivery” = statuses picked up by the delivery worker
    # (src/workers/delivery.py: status IN ('pending','ai_done')); terminal
    # 'sent'/'failed'/'suppressed' are not part of the backlog.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_delivery_backlog()
        RETURNS TABLE(
            pending_total bigint,
            failed_24h bigint,
            sent_1h bigint,
            sent_24h bigint,
            last_sent_at timestamptz
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              COUNT(*) FILTER (WHERE status IN ('pending', 'ai_done'))          AS pending_total,
              COUNT(*) FILTER (
                  WHERE status = 'failed' AND failed_at >= NOW() - INTERVAL '24 hours'
              )                                                                  AS failed_24h,
              COUNT(*) FILTER (
                  WHERE status = 'sent' AND sent_at >= NOW() - INTERVAL '1 hour'
              )                                                                  AS sent_1h,
              COUNT(*) FILTER (
                  WHERE status = 'sent' AND sent_at >= NOW() - INTERVAL '24 hours'
              )                                                                  AS sent_24h,
              MAX(sent_at) FILTER (WHERE status = 'sent')                        AS last_sent_at
          FROM delivery_outbox
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_delivery_backlog() TO tendersrb_app"
    )

    # Freshness of reminder workers: MAX(sent_at) over the reminder journals.
    # reminder_log (0009, deadlines) and subscription_reminder_log (0017, end
    # of subscription) — direct tenant_id, cat. “a”, FORCE RLS. MAX only, no rows.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_reminder_freshness()
        RETURNS TABLE(
            last_deadline_reminder_at timestamptz,
            last_subscription_reminder_at timestamptz
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              (SELECT MAX(sent_at) FROM reminder_log)               AS last_deadline_reminder_at,
              (SELECT MAX(sent_at) FROM subscription_reminder_log)  AS last_subscription_reminder_at
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_reminder_freshness() TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_admin_reminder_freshness()")
    op.execute("DROP FUNCTION IF EXISTS app_admin_delivery_backlog()")
