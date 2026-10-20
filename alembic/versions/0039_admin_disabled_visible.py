"""Admin: a disabled subscriber is visible in the list/card and can be restored (RED)

Until this migration ``app_admin_get_user``/``app_admin_list_users``/
``app_admin_users_total`` (0025) filtered ``u.deleted_at IS NULL`` — the same predicate the login
resolver (0024) uses to keep a disabled subscriber out of the cabinet. For the ADMIN this filter is a
defect: after ``app_admin_disable_subscriber`` (0037) the user row disappears from
``/admin/users``, and the card ``/admin/users/{uid}`` returns nothing → the “Restore” button
(``app_admin_enable_subscriber``, also 0037) is physically unreachable
through the interface, although the label promises “softly and reversibly”. The subscriber could be
restored only by hand in SQL.

We remove the filter in both read functions and add the ``deleted_at`` column to
``RETURNS TABLE`` so that the service/template can show a “Disabled” badge and
switch the “Disable”/“Restore” block. ``app_admin_users_total`` counts the same set
as the list (otherwise the pagination would diverge).

``CREATE OR REPLACE FUNCTION`` does not change the return type — for
``app_admin_get_user``/``app_admin_list_users`` (new column in
``RETURNS TABLE``) a ``DROP FUNCTION`` + ``CREATE`` is needed, and again
``GRANT EXECUTE ... TO tendersrb_app`` (without the grant — ``permission denied`` on
production). ``app_admin_users_total`` does not change its return type — a plain
``CREATE OR REPLACE``.

The other ``app_admin_*`` functions, the RLS policies and ``FORCE ROW LEVEL SECURITY`` are not touched.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # app_admin_list_users: RETURNS TABLE changes (+ deleted_at) → DROP + CREATE.
    op.execute("DROP FUNCTION IF EXISTS app_admin_list_users(int, int)")
    op.execute(
        """
        CREATE FUNCTION app_admin_list_users(p_limit INT, p_offset INT)
        RETURNS TABLE(
            id uuid,
            email text,
            created_at timestamptz,
            tenant_id uuid,
            tier text,
            access_until timestamptz,
            profile_count bigint,
            deleted_at timestamptz
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
              COUNT(p.id) AS profile_count,
              u.deleted_at
          FROM users u
          JOIN tenants t ON t.id = u.tenant_id
          LEFT JOIN profiles p ON p.tenant_id = u.tenant_id AND p.deleted_at IS NULL
          GROUP BY u.id, u.email, u.created_at, u.tenant_id, t.tier, t.access_until
          ORDER BY u.created_at DESC
          LIMIT p_limit OFFSET p_offset
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_list_users(INT, INT) TO tendersrb_app"
    )

    # app_admin_users_total: return type (bigint) does not change — REPLACE.
    # The predicate must match the list (now — “no predicate”), otherwise
    # the pagination diverges.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_users_total()
        RETURNS bigint
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT COUNT(*) FROM users
        $$
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_users_total() TO tendersrb_app")

    # app_admin_get_user: RETURNS TABLE changes (+ deleted_at) → DROP + CREATE.
    op.execute("DROP FUNCTION IF EXISTS app_admin_get_user(uuid)")
    op.execute(
        """
        CREATE FUNCTION app_admin_get_user(p_uid uuid)
        RETURNS TABLE(
            id uuid,
            email text,
            created_at timestamptz,
            tenant_id uuid,
            tier text,
            access_until timestamptz,
            profile_count bigint,
            deleted_at timestamptz
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
              COUNT(p.id) AS profile_count,
              u.deleted_at
          FROM users u
          JOIN tenants t ON t.id = u.tenant_id
          LEFT JOIN profiles p ON p.tenant_id = u.tenant_id AND p.deleted_at IS NULL
          WHERE u.id = p_uid
          GROUP BY u.id, u.email, u.created_at, u.tenant_id, t.tier, t.access_until
        $$
        """
    )
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_get_user(uuid) TO tendersrb_app")


def downgrade() -> None:
    # A verbatim rollback to the 0025 definitions (taken from pg_get_functiondef on the seeded
    # database) — the filter deleted_at IS NULL and the previous RETURNS TABLE without the column.
    op.execute("DROP FUNCTION IF EXISTS app_admin_get_user(uuid)")
    op.execute(
        """
        CREATE FUNCTION app_admin_get_user(p_uid uuid)
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
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_get_user(uuid) TO tendersrb_app")

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

    op.execute("DROP FUNCTION IF EXISTS app_admin_list_users(int, int)")
    op.execute(
        """
        CREATE FUNCTION app_admin_list_users(p_limit INT, p_offset INT)
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
