"""admin_audit_log + SECURITY DEFINER app_admin_write/list/total_audit (VG-29.4)

An immutable journal of privileged operator-admin actions (who by
email / what / on which tenant-user / details before→after / when).

Scope (STRICTLY per CLAUDE.md — RLS/SECURITY DEFINER):

* ``admin_audit_log`` — a GLOBAL admin journal, NOT tenant-scoped, so RLS
  is not applied. But 0021 granted ``ALTER DEFAULT PRIVILEGES ... GRANT
  SELECT/INSERT/UPDATE/DELETE ON TABLES TO tendersrb_app`` — so the new table would
  automatically get direct access for the ``tendersrb_app`` role. That breaks the
  requirement “direct app-role access is fail-closed”, so here we EXPLICITLY
  revoke all privileges from ``tendersrb_app`` on the table and its sequence. In the
  application the app role reads/writes the journal ONLY through the SECURITY DEFINER functions
  below (owner — superuser ``tendersrb``, bypass the table grant control).
* ``app_admin_write_audit`` / ``app_admin_list_audit`` / ``app_admin_audit_total``
  — pattern 1:1 with ``app_admin_*`` (0025) and ``app_billing_*`` (0028):
  ``SECURITY DEFINER``, ``SET search_path = public``, ``GRANT EXECUTE ...
  TO tendersrb_app``.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Table of the global admin journal (no RLS, but no direct access for the app role).
    op.execute("""
        CREATE TABLE admin_audit_log (
            id bigserial PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            actor_email text NOT NULL,
            action text NOT NULL,
            target_tenant_id uuid,
            target_user_id uuid,
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            ip text
        )
        """)
    op.execute("CREATE INDEX admin_audit_log_at_idx ON admin_audit_log (at DESC)")
    op.execute(
        "CREATE INDEX admin_audit_log_actor_idx ON admin_audit_log (actor_email)"
    )
    op.execute(
        "CREATE INDEX admin_audit_log_target_tenant_idx "
        "ON admin_audit_log (target_tenant_id)"
    )

    # Revoke direct access from tendersrb_app (0021 granted DEFAULT PRIVILEGES on
    # future tables). The journal is accessible to the app role ONLY through SECURITY DEFINER —
    # a direct SELECT/INSERT under the app role → “permission denied” (fail-closed).
    op.execute("REVOKE ALL ON admin_audit_log FROM tendersrb_app")
    op.execute("REVOKE ALL ON SEQUENCE admin_audit_log_id_seq FROM tendersrb_app")

    # 2. SECURITY DEFINER: writing an audit row (cross-tenant INSERT from the admin panel).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_write_audit(
            p_actor text,
            p_action text,
            p_target_tenant uuid,
            p_target_user uuid,
            p_details jsonb,
            p_ip text
        )
        RETURNS void
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          INSERT INTO admin_audit_log (
              actor_email, action, target_tenant_id, target_user_id, details, ip
          )
          VALUES (
              p_actor, p_action, p_target_tenant, p_target_user,
              COALESCE(p_details, '{}'::jsonb), p_ip
          )
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_write_audit("
        "text, text, uuid, uuid, jsonb, text) TO tendersrb_app"
    )

    # 3. SECURITY DEFINER: paged reading of the journal (newest first).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_list_audit(p_limit int, p_offset int)
        RETURNS TABLE(
            id bigint,
            at timestamptz,
            actor_email text,
            action text,
            target_tenant_id uuid,
            target_user_id uuid,
            details jsonb,
            ip text
        )
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT
              id, at, actor_email, action,
              target_tenant_id, target_user_id, details, ip
          FROM admin_audit_log
          ORDER BY at DESC, id DESC
          LIMIT p_limit OFFSET p_offset
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_list_audit(int, int) TO tendersrb_app"
    )

    # 4. SECURITY DEFINER: total number of records (for page pagination).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_audit_total()
        RETURNS bigint
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT COUNT(*) FROM admin_audit_log
        $$
        """)
    op.execute("GRANT EXECUTE ON FUNCTION app_admin_audit_total() TO tendersrb_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_admin_audit_total()")
    op.execute("DROP FUNCTION IF EXISTS app_admin_list_audit(int, int)")
    op.execute(
        "DROP FUNCTION IF EXISTS app_admin_write_audit("
        "text, text, uuid, uuid, jsonb, text)"
    )
    op.execute("DROP TABLE IF EXISTS admin_audit_log")
