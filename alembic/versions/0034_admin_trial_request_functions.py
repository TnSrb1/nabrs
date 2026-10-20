"""SECURITY DEFINER for the web list/decision of subscription requests (sub-approval)

The web admin receives the list of pending ``trial_requests`` and decides them
(approve/reject) CROSS-TENANT (the operator looks at all tenants). The table is under
FORCE RLS (0012 ENABLE + 0032 FORCE), so direct SELECT/UPDATE under the role
``tendersrb_app`` sees only its own tenant / fails closed without GUC. RLS is NOT
weakened — access strictly through SECURITY DEFINER (pattern 1:1 with app_admin_*
0025/0030):

* ``app_admin_list_trial_requests(p_limit, p_offset)`` — pending of all tenants
  (id, tenant_id, initiator email, plan from comment, requested_at, days).
  ``plan`` = the tail of ``comment='upgrade_request:<plan>'`` (NULL for regular
  trial access). email = the oldest live user of the tenant (as _LIST_PENDING_SQL
  in bot/trial_service).
* ``app_admin_trial_requests_total()`` — the number of pending (for pagination).
* ``app_admin_decide_trial_request(p_id, p_status, p_days)`` — idempotent
  guarded UPDATE ``WHERE status='pending'`` (a repeat → 0 rows → “already”, no second
  grant), RETURNING (tenant_id, comment) for routing to the billing path.
  Applying the plan/access itself — the existing app_billing_upsert_subscription
  (0028) + app_admin_set_tier/grant_access (0025), not duplicated here.

No new tables/columns — only functions. Additive, rolled out to the live prod.

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # List of pending requests of all tenants (cross-tenant, bypasses FORCE RLS).
    # plan = the tail of 'upgrade_request:<plan>' (NULL for regular trial access);
    # email = the oldest live user of the tenant (as _LIST_PENDING_SQL in the bot).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_list_trial_requests(
            p_limit int, p_offset int
        )
        RETURNS TABLE(
            id uuid,
            tenant_id uuid,
            email text,
            plan text,
            requested_at timestamptz,
            days int
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              tr.id,
              tr.tenant_id,
              (SELECT u.email FROM users u
                WHERE u.tenant_id = tr.tenant_id AND u.deleted_at IS NULL
                ORDER BY u.created_at ASC
                LIMIT 1)                                    AS email,
              CASE
                  WHEN tr.comment LIKE 'upgrade_request:%'
                  THEN split_part(tr.comment, ':', 2)
                  ELSE NULL
              END                                           AS plan,
              tr.requested_at,
              tr.days
          FROM trial_requests tr
          WHERE tr.status = 'pending'
          ORDER BY tr.requested_at ASC
          LIMIT p_limit OFFSET p_offset
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_list_trial_requests(int, int) "
        "TO tendersrb_app"
    )

    # Number of pending requests (pagination).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_trial_requests_total()
        RETURNS bigint
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT COUNT(*) FROM trial_requests WHERE status = 'pending'
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_trial_requests_total() TO tendersrb_app"
    )

    # Idempotent cross-tenant decision (guarded UPDATE status='pending').
    # The body mirrors _DECIDE_SQL from bot/trial_service — a repeat returns 0 rows.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_decide_trial_request(
            p_id uuid, p_status text, p_days int
        )
        RETURNS TABLE(tenant_id uuid, comment text)
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE trial_requests
             SET status = p_status,
                 days = p_days,
                 decided_at = now()
           WHERE id = p_id AND status = 'pending'
          RETURNING tenant_id, comment
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_decide_trial_request(uuid, text, int) "
        "TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS app_admin_decide_trial_request(uuid, text, int)"
    )
    op.execute("DROP FUNCTION IF EXISTS app_admin_trial_requests_total()")
    op.execute(
        "DROP FUNCTION IF EXISTS app_admin_list_trial_requests(int, int)"
    )
