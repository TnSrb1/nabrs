"""non-superuser app role + FORCE ROW LEVEL SECURITY (VG-01)

Creates the restricted role ``tendersrb_app`` (NOSUPERUSER, NOBYPASSRLS),
grants privileges, sets FORCE ROW LEVEL SECURITY on all 17 tenant tables,
adds policies for tables without them (user_channels, award_watch_*)
and creates SECURITY DEFINER functions for bootstrap (resolving tenant_id before
the GUC is set).

Requires ENV: ``APP_DB_PASSWORD`` (password for the role tendersrb_app). Fail-hard
if the variable is not set — an empty password is dangerous in production.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-02
"""
from __future__ import annotations

import os
from typing import Sequence, Union

from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables with existing tenant_isolation policies (0001/0003/0008-0010/0017).
# For them we only add FORCE ROW LEVEL SECURITY.
_EXISTING_RLS_TABLES = (
    "tenants",
    "users",
    "profiles",
    "subscriptions",
    "anti_abuse_identifiers",
    "ai_usage",
    "tender_user_status",
    "reminder_log",
    "summary_log",
    "subscription_reminder_log",
    "profile_revisions",
    "delivery_outbox",
    "delivery_attempts",
    "tender_ai_analyses",
    "user_channels",
)

# Tables without RLS from 0019 — add ENABLE + policy + FORCE.
_NEW_RLS_TABLES = (
    "award_watch_subscriptions",
    "award_watch_sent",
)

_ALL_FORCE_TABLES = _EXISTING_RLS_TABLES + _NEW_RLS_TABLES


def upgrade() -> None:
    # 1. Password only from ENV — fail-hard if not set.
    app_db_password = os.environ.get("APP_DB_PASSWORD", "")
    if not app_db_password:
        raise RuntimeError(
            "APP_DB_PASSWORD environment variable is not set or empty. "
            "Set it in the migrate service environment before running 0021."
        )

    # 2. Create the role (idempotent).
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tendersrb_app') "
        "  THEN CREATE ROLE tendersrb_app "
        "    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
        "  END IF; "
        "END $$"
    )
    # Parameter via concatenation — only inside the migration, after the validation above.
    # The password does not get into git: it is taken from ENV at runtime.
    op.execute(
        "ALTER ROLE tendersrb_app PASSWORD '"
        + app_db_password.replace("'", "''")
        + "'"
    )

    # 3. Grants on the current DB (name taken from the connection).
    op.execute(
        "GRANT CONNECT ON DATABASE tendersrb TO tendersrb_app"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO tendersrb_app")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        "ON ALL TABLES IN SCHEMA public TO tendersrb_app"
    )
    op.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tendersrb_app"
    )
    # DEFAULT PRIVILEGES — apply to tables/sequences of future migrations.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO tendersrb_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO tendersrb_app"
    )

    # 4. user_channels: add policy USING(true) — bootstrap access without GUC.
    # RLS is already ENABLE from 0001, no policy (default-deny). USING(true) allows
    # reading all rows as tendersrb_app to resolve the tenant before the GUC is set.
    op.execute(
        "CREATE POLICY tenant_isolation ON user_channels USING (true)"
    )

    # 5. award_watch_subscriptions: ENABLE + direct policy + FORCE.
    op.execute("ALTER TABLE award_watch_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON award_watch_subscriptions "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )

    # 6. award_watch_sent: ENABLE + join policy (no tenant_id — join via
    # award_watch_subscriptions) + FORCE.
    op.execute("ALTER TABLE award_watch_sent ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON award_watch_sent\n"
        "  USING (subscription_id IN (\n"
        "    SELECT id FROM award_watch_subscriptions\n"
        "    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid))"
    )

    # 7. FORCE ROW LEVEL SECURITY on all 17 tenant tables.
    # FORCE makes tendersrb_app (non-superuser) always obey the policies.
    # The superuser tendersrb bypasses RLS despite FORCE — this is a PostgreSQL guarantee.
    for tbl in _ALL_FORCE_TABLES:
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")

    # 8. SECURITY DEFINER functions for bootstrap (called before the GUC is set).
    # Created by the superuser tendersrb → run with its privileges → bypass RLS.
    # SET search_path = public — protection against path hijacking in SECURITY DEFINER.

    # app_resolve_tenant_id: tenant_id by Telegram external_id (without GUC).
    # Used in status_service.resolve_tenant_id and telegram.get_subscriber.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_resolve_tenant_id(
            p_external_id TEXT,
            p_channel     TEXT DEFAULT 'telegram'
        )
        RETURNS UUID
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT u.tenant_id
          FROM user_channels uc
          JOIN users u ON u.id = uc.user_id
          WHERE uc.channel = p_channel
            AND uc.external_id = p_external_id
          ORDER BY (uc.status = 'active') DESC, uc.linked_at DESC
          LIMIT 1
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_resolve_tenant_id(TEXT, TEXT) TO tendersrb_app"
    )

    # app_active_tenant_ids: all active/trial tenant_ids (for workers).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_active_tenant_ids()
        RETURNS SETOF UUID
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT id FROM tenants WHERE status IN ('trial', 'active')
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_active_tenant_ids() TO tendersrb_app"
    )


def downgrade() -> None:
    # Drops the SECURITY DEFINER functions.
    op.execute("DROP FUNCTION IF EXISTS app_resolve_tenant_id(TEXT, TEXT)")
    op.execute("DROP FUNCTION IF EXISTS app_active_tenant_ids()")

    # Removes FORCE ROW LEVEL SECURITY.
    for tbl in reversed(_ALL_FORCE_TABLES):
        op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")

    # Drops the policies added in this migration.
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON user_channels")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON award_watch_subscriptions")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON award_watch_sent")

    # Rolls back ENABLE RLS on tables that did not have it before 0021.
    op.execute("ALTER TABLE award_watch_sent DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE award_watch_subscriptions DISABLE ROW LEVEL SECURITY")

    # Revokes DEFAULT PRIVILEGES.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM tendersrb_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM tendersrb_app"
    )

    # The role is NOT dropped: it may have active connections and grants on other objects.
    # For full removal: DROP OWNED BY tendersrb_app; DROP ROLE tendersrb_app;
