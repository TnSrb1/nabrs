"""trial_requests.decision_comment + comment on the decision + email notice about the decision

Reliable reply to the subscriber about the decision on the request (approve/reject), INDEPENDENT of
Telegram. Before #401 the reply went only to Telegram (``bots/trial_service._notify_user``) —
a user without a linked Telegram account did NOT learn about the approval/rejection. This migration is
the server part of the reliable path (email + cabinet banner + admin comment):

1. ``trial_requests.decision_comment text NULL`` — optional operator comment on the decision
   (web form ``/admin/requests``). The bot path has no comment → NULL, this is OK. The table's RLS is NOT
   changed (FORCE since 0012/0032 — the additive ADD COLUMN does not affect it).

2. ``app_admin_decide_trial_request(uuid, text, int, text)`` — new 4-arg signature:
   the guarded UPDATE ``status='pending'`` additionally writes ``decision_comment = p_comment``. The old
   3-arg one is dropped (only ``api/admin/admin_service`` called it; the bot path goes through a raw
   UPDATE and does not write a comment).

3. ``app_billing_pending_notifications(int)`` — reuses the billing payment-notification queue (0033)
   WITHOUT a second worker/outbox/enqueue. The function already collects everything the email needs
   (JOIN tenants+users for the recipient/term) — we add a LEFT JOIN to ``trial_requests`` for decision rows
   (``provider='manual'``, ``event_id='decision:<request_id>'``), returning TWO new columns: ``plan``
   (from ``comment='upgrade_request:<plan>'``, as in 0034) and ``decision_comment``.
   For payment-lifecycle emails (``provider<>'manual'``) both columns are NULL — the behavior of the
   existing ``activated/renewed/past_due/canceled`` does not change.
   The decision key ``event_id='decision:<request_id>'`` is set by the SINGLE enqueue helper
   ``billing.service.enqueue_decision_notification`` (both decision points — bot and web — call it);
   idempotency via UNIQUE ``(provider, event_id)`` of 0033.

Changing the ``RETURNS TABLE`` return signature requires DROP+CREATE (not CREATE OR
REPLACE). Postgres validates SQL function bodies for columns at creation time
(``check_function_bodies``) → ``decision_comment`` is added FIRST.

All functions follow the pattern 1:1 with 0033/0034: ``SECURITY DEFINER``,
``SET search_path = public``, ``GRANT EXECUTE ... TO tendersrb_app``.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The original (0033) 7-column version of the pending function — restored in
# downgrade without the decision rows (plan/decision_comment).
_PENDING_0033 = """
    CREATE OR REPLACE FUNCTION app_billing_pending_notifications(p_limit int)
    RETURNS TABLE(
        id bigint,
        tenant_id uuid,
        kind text,
        email text,
        email_status text,
        access_until timestamptz,
        tz text
    )
    LANGUAGE SQL STABLE SECURITY DEFINER
    SET search_path = public
    AS $$
      SELECT
          o.id,
          o.tenant_id,
          o.kind,
          u.email,
          u.email_status,
          ten.access_until,
          ten.tz
      FROM billing_notification_outbox o
      JOIN tenants ten ON ten.id = o.tenant_id
      LEFT JOIN LATERAL (
          SELECT us.email, us.email_status
          FROM users us
          WHERE us.tenant_id = o.tenant_id
            AND us.deleted_at IS NULL
          ORDER BY (us.email IS NOT NULL
                    AND btrim(us.email) <> ''
                    AND us.email_status IS DISTINCT FROM 'bounced') DESC,
                   us.created_at,
                   us.id
          LIMIT 1
      ) u ON true
      WHERE o.sent_at IS NULL
      ORDER BY o.created_at, o.id
      LIMIT p_limit
    $$
    """

# The original (0034) 3-arg version of the decide function — restored in downgrade.
_DECIDE_0034 = """
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


def upgrade() -> None:
    # 1. Operator comment on the decision (optional). Additive, rolls out on the
    # live production; trial_requests RLS is not touched (FORCE stays from 0012/0032).
    op.execute("ALTER TABLE trial_requests ADD COLUMN decision_comment text")

    # 2. Decision with a comment: new 4-arg signature instead of the 3-arg one. Guarded UPDATE
    # status='pending' (repeat → 0 rows → «already»), additionally writes
    # decision_comment. The old one is dropped — only the web admin_service called it.
    op.execute(
        "DROP FUNCTION IF EXISTS app_admin_decide_trial_request(uuid, text, int)"
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_decide_trial_request(
            p_id uuid, p_status text, p_days int, p_comment text
        )
        RETURNS TABLE(tenant_id uuid, comment text)
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE trial_requests
             SET status = p_status,
                 days = p_days,
                 decided_at = now(),
                 decision_comment = p_comment
           WHERE id = p_id AND status = 'pending'
          RETURNING tenant_id, comment
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_decide_trial_request("
        "uuid, text, int, text) TO tendersrb_app"
    )

    # 3. The billing payment queue (0033) gives the email the decision context. Return
    # signature changes (7→9 columns) → DROP+CREATE. LEFT JOIN trial_requests
    # only for decision rows (provider='manual', event_id='decision:<rid>');
    # for payment emails plan/decision_comment = NULL (their texts ignore these columns).
    # plan — from comment='upgrade_request:<plan>' (as in 0034).
    op.execute("DROP FUNCTION IF EXISTS app_billing_pending_notifications(int)")
    op.execute("""
        CREATE OR REPLACE FUNCTION app_billing_pending_notifications(p_limit int)
        RETURNS TABLE(
            id bigint,
            tenant_id uuid,
            kind text,
            email text,
            email_status text,
            access_until timestamptz,
            tz text,
            plan text,
            decision_comment text
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              o.id,
              o.tenant_id,
              o.kind,
              u.email,
              u.email_status,
              ten.access_until,
              ten.tz,
              CASE
                  WHEN dr.comment LIKE 'upgrade_request:%'
                  THEN split_part(dr.comment, ':', 2)
                  ELSE NULL
              END                                           AS plan,
              dr.decision_comment
          FROM billing_notification_outbox o
          JOIN tenants ten ON ten.id = o.tenant_id
          LEFT JOIN LATERAL (
              SELECT us.email, us.email_status
              FROM users us
              WHERE us.tenant_id = o.tenant_id
                AND us.deleted_at IS NULL
              ORDER BY (us.email IS NOT NULL
                        AND btrim(us.email) <> ''
                        AND us.email_status IS DISTINCT FROM 'bounced') DESC,
                       us.created_at,
                       us.id
              LIMIT 1
          ) u ON true
          LEFT JOIN trial_requests dr
              ON o.provider = 'manual'
             AND o.event_id LIKE 'decision:%'
             AND dr.id = NULLIF(split_part(o.event_id, ':', 2), '')::uuid
          WHERE o.sent_at IS NULL
          ORDER BY o.created_at, o.id
          LIMIT p_limit
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_billing_pending_notifications(int) "
        "TO tendersrb_app"
    )


def downgrade() -> None:
    # Restores the 7-column pending function (without decision context).
    op.execute("DROP FUNCTION IF EXISTS app_billing_pending_notifications(int)")
    op.execute(_PENDING_0033)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_billing_pending_notifications(int) "
        "TO tendersrb_app"
    )

    # Restores the 3-arg decide function.
    op.execute(
        "DROP FUNCTION IF EXISTS app_admin_decide_trial_request(uuid, text, int, text)"
    )
    op.execute(_DECIDE_0034)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_decide_trial_request(uuid, text, int) "
        "TO tendersrb_app"
    )

    op.execute("ALTER TABLE trial_requests DROP COLUMN IF EXISTS decision_comment")
