"""Admin: template connection date, template delivery channels and subscriber channels (RED)

The subscriber card ``/admin/users/{uid}`` showed neither the date the template was
connected nor its delivery channels: from one template it was impossible to tell that there was nowhere to deliver (``profiles.delivery_channels = '{}'``). Reason — 18.10.2026.

To do this we extend two SECURITY DEFINER functions and add one:

* ``app_admin_list_profiles()`` — +``created_at`` (template connection date) and +``delivery_channels`` (template delivery channels). The return type changes → DROP + CREATE, and ``GRANT EXECUTE ... TO tendersrb_app`` again.
* ``app_admin_user_channels(uid)`` — new: the USER's link channels (``user_channels`` — by ``user_id``, FORCE RLS). A direct SELECT from the app role would return nothing, so only via SECURITY DEFINER.

Bodies — without changing predicates: ``p.deleted_at IS NULL`` in the template list is kept, data is not changed.

Revision ID: 0057
Revises: 0056
Create Date: 2026-10-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0057"
down_revision: Union[str, None] = "0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # app_admin_list_profiles: RETURNS TABLE changes (+created_at, +delivery_channels)
    # → DROP + CREATE. Predicates and order — as in 0025.
    op.execute("DROP FUNCTION IF EXISTS app_admin_list_profiles()")
    op.execute("""
        CREATE FUNCTION app_admin_list_profiles()
        RETURNS TABLE(
            id uuid,
            name text,
            tenant_id uuid,
            is_active bool,
            owner_email text,
            match_7d bigint,
            created_at timestamptz,
            delivery_channels text[]
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
              COUNT(dox.id) AS match_7d,
              p.created_at,
              p.delivery_channels
          FROM profiles p
          LEFT JOIN users u
              ON u.tenant_id = p.tenant_id AND u.deleted_at IS NULL
          LEFT JOIN delivery_outbox dox
              ON dox.profile_id = p.id
              AND dox.created_at >= NOW() - INTERVAL '7 days'
          WHERE p.deleted_at IS NULL
          GROUP BY p.id, p.name, p.tenant_id, p.is_active, u.email,
                   p.created_at, p.delivery_channels
          ORDER BY p.created_at DESC
        $$
        """)
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_list_profiles() TO tendersrb_app")

    # User link channels (user_channels.user_id, not tenant_id: one
    # tenant can have several users with different channels).
    op.execute("""
        CREATE FUNCTION app_admin_user_channels(p_uid uuid)
        RETURNS TABLE(
            channel text,
            status text,
            linked_at timestamptz
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT uc.channel, uc.status, uc.linked_at
          FROM user_channels uc
          WHERE uc.user_id = p_uid
          ORDER BY uc.linked_at DESC NULLS LAST, uc.channel
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_user_channels(uuid) TO tendersrb_app"
    )


def downgrade() -> None:
    # Verbatim rollback to the app_admin_list_profiles definition from 0025 (without
    # created_at/delivery_channels), plus removal of the new function.
    op.execute("DROP FUNCTION IF EXISTS app_admin_user_channels(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_list_profiles()")
    op.execute("""
        CREATE FUNCTION app_admin_list_profiles()
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
        """)
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_list_profiles() TO tendersrb_app")
