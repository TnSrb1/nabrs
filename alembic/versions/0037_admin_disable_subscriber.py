"""Administrative disabling/re-enabling of a subscriber as a whole (admin)

Until this migration the operator had a single lever — `app_admin_revoke_access`
(0025), which sets `tenants.access_until = now()`. It stops the mailing (`matching.STAGE_A_SQL`), all
AI gates and, since #469, the cabinet content sections, but does NOT touch
two things:

* `user_channels` stay `active` — the linked Telegram chat keeps answering the disabled subscriber
  (`/start`, `/help`, `/status`, `/profile`, `/watching`);
* `users.deleted_at` stays NULL — the subscriber keeps receiving a magic-link and
  logs into the cabinet (the login resolver `app_resolve_user_for_auth` (0024) filters
  exactly on `deleted_at IS NULL`, but there was nothing and nowhere to set it).

There is NO physical deletion and none will appear: disabling is soft and reversible, data
stays (audit, delivery history, billing). Both functions are idempotent — a repeat does not
change an already disabled/enabled one.

Cross-tenant operations of the admin live ONLY in SECURITY DEFINER (0025/0030/0034):
the role `tendersrb_app` has no direct cross-tenant access to `tenants`/`users`,
and `user_channels` is closed by the join policy of 0022. RLS is NOT weakened.

No new tables/columns — only functions; `user_channels.status` is a free
TEXT without CHECK (0001), the value `'disabled'` requires no schema change. Additive,
rolls out on the live production.

Revision ID: 0037
Revises: 0036
Create Date: 2026-07-26
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Disabling a subscriber: three axes at once, each idempotent.
    #   1. access    — access_until = now() (the same axis as revoke_access);
    #   2. channels  — active bindings → 'disabled' (the bot stops answering);
    #   3. login     — users.deleted_at = now() (magic-link is no longer issued,
    #                  and the cabinet gate #469 also closes an already issued cookie).
    # user_channels has no tenant_id (isolation via JOIN users, 0022) —
    # therefore the selection is by the tenant’s user_id, not by a column.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_disable_subscriber(p_tenant uuid)
        RETURNS void
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        BEGIN
            UPDATE tenants
               SET access_until = now()
             WHERE id = p_tenant;

            UPDATE user_channels
               SET status = 'disabled'
             WHERE status = 'active'
               AND user_id IN (SELECT id FROM users WHERE tenant_id = p_tenant);

            UPDATE users
               SET deleted_at = now()
             WHERE tenant_id = p_tenant
               AND deleted_at IS NULL;
        END;
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_disable_subscriber(uuid) "
        "TO tendersrb_app"
    )

    # Re-enabling a subscriber. Channels are restored exactly those that were disabled ('disabled'),
    # so as not to resurrect ones that dropped off for their own reason (channel_dead_notify,
    # 0031 — there the statuses are different). The access term is extended ONLY when p_days > 0:
    # 0 = “restore login and channels, decide the term separately” (grant_access, 0025).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_enable_subscriber(
            p_tenant uuid, p_days int
        )
        RETURNS void
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        BEGIN
            UPDATE users
               SET deleted_at = NULL
             WHERE tenant_id = p_tenant
               AND deleted_at IS NOT NULL;

            UPDATE user_channels
               SET status = 'active'
             WHERE status = 'disabled'
               AND user_id IN (SELECT id FROM users WHERE tenant_id = p_tenant);

            IF p_days > 0 THEN
                UPDATE tenants
                   SET access_until = now() + make_interval(days => p_days)
                 WHERE id = p_tenant;
            END IF;
        END;
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_enable_subscriber(uuid, int) "
        "TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_admin_enable_subscriber(uuid, int)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_disable_subscriber(uuid)")
