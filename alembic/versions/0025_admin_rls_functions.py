"""SECURITY DEFINER functions for the admin console — fix for the cross-tenant 500 (FIX-ADMIN-RLS)

``/admin/*`` (admin_service.py) runs cross-tenant queries over
``users``/``tenants``/``profiles``/``delivery_outbox`` (all under FORCE RLS since
0021) — the operator sees ALL tenants, so ``set_tenant`` does not fit
(it would narrow the selection to one tenant). Under the ``tendersrb_app`` role
(non-superuser, VG-01) the policies ``USING (tenant_id = current_setting(...))``
raise ``UndefinedObjectError`` without a set GUC → 500.

Decision (pattern 1:1 with ``app_resolve_*`` from 0021/0022/0023/0024):
SECURITY DEFINER functions (owner — superuser ``tendersrb``) bypass FORCE
RLS, ``GRANT EXECUTE ... TO tendersrb_app``. ``_TENDER_STATS_SQL`` (table
``tenders_normalized`` without RLS) is NOT wrapped — it works as is.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # User list, paged (body 1:1 with _USERS_LIST_PAGED_SQL).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_list_users(p_limit INT, p_offset INT)
        RETURNS TABLE(
            id uuid,
            email text,
            created_at timestamptz,
            tenant_id uuid,
            tier text,
            access_until timestamptz,
            profile_count bigint
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              u.id,
              u.email,
              u.created_at,
              u.tenant_id,
              t.tier,
              t.access_until,
              COUNT(p.id) AS profile_count
          FROM users u
          JOIN tenants t ON t.id = u.tenant_id
          LEFT JOIN profiles p ON p.tenant_id = u.tenant_id AND p.deleted_at IS NULL
          WHERE u.deleted_at IS NULL
          GROUP BY u.id, u.email, u.created_at, u.tenant_id, t.tier, t.access_until
          ORDER BY u.created_at DESC
          LIMIT p_limit OFFSET p_offset
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_list_users(INT, INT) TO tendersrb_app"
    )

    # Total number of users (body 1:1 with _USERS_TOTAL_SQL).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_users_total()
        RETURNS bigint
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT COUNT(*) FROM users WHERE deleted_at IS NULL
        $$
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_users_total() TO tendersrb_app")

    # One user by id (body 1:1 with _USER_DETAIL_SQL).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_get_user(p_uid uuid)
        RETURNS TABLE(
            id uuid,
            email text,
            created_at timestamptz,
            tenant_id uuid,
            tier text,
            access_until timestamptz,
            profile_count bigint
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              u.id,
              u.email,
              u.created_at,
              u.tenant_id,
              t.tier,
              t.access_until,
              COUNT(p.id) AS profile_count
          FROM users u
          JOIN tenants t ON t.id = u.tenant_id
          LEFT JOIN profiles p ON p.tenant_id = u.tenant_id AND p.deleted_at IS NULL
          WHERE u.id = p_uid AND u.deleted_at IS NULL
          GROUP BY u.id, u.email, u.created_at, u.tenant_id, t.tier, t.access_until
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_get_user(uuid) TO tendersrb_app"
    )

    # Profile list (body 1:1 with _PROFILES_LIST_SQL).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_list_profiles()
        RETURNS TABLE(
            id uuid,
            name text,
            tenant_id uuid,
            is_active bool,
            owner_email text,
            match_7d bigint
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              p.id,
              p.name,
              p.tenant_id,
              p.is_active,
              u.email AS owner_email,
              COUNT(dox.id) AS match_7d
          FROM profiles p
          LEFT JOIN users u
              ON u.tenant_id = p.tenant_id AND u.deleted_at IS NULL
          LEFT JOIN delivery_outbox dox
              ON dox.profile_id = p.id
              AND dox.created_at >= NOW() - INTERVAL '7 days'
          WHERE p.deleted_at IS NULL
          GROUP BY p.id, p.name, p.tenant_id, p.is_active, u.email
          ORDER BY p.created_at DESC
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_list_profiles() TO tendersrb_app"
    )

    # Number of delivered tenders for a tenant (body 1:1 with _USER_DELIVERED_SQL).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_user_delivered_count(p_tid uuid)
        RETURNS bigint
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT COUNT(dox.id)
          FROM delivery_outbox dox
          JOIN profiles p ON p.id = dox.profile_id
          WHERE p.tenant_id = p_tid
            AND dox.status = 'sent'
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_user_delivered_count(uuid) TO tendersrb_app"
    )

    # Writes (cross-tenant UPDATE tenants) — bodies 1:1 with _GRANT_ACCESS_SQL /
    # _REVOKE_ACCESS_SQL / _SET_TIER_SQL.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_grant_access(p_tid uuid, p_until timestamptz)
        RETURNS void
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE tenants
             SET access_until = p_until,
                 updated_at   = NOW()
           WHERE id = p_tid
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_grant_access(uuid, timestamptz) TO tendersrb_app"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_revoke_access(p_tid uuid)
        RETURNS void
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE tenants
             SET access_until = NOW(),
                 updated_at   = NOW()
           WHERE id = p_tid
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_revoke_access(uuid) TO tendersrb_app"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_set_tier(p_tid uuid, p_tier text)
        RETURNS void
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE tenants
             SET tier       = p_tier,
                 updated_at = NOW()
           WHERE id = p_tid
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_set_tier(uuid, text) TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_admin_set_tier(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_revoke_access(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_grant_access(uuid, timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_user_delivered_count(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_list_profiles()")
    op.execute("DROP FUNCTION IF EXISTS app_admin_get_user(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_users_total()")
    op.execute("DROP FUNCTION IF EXISTS app_admin_list_users(int, int)")
