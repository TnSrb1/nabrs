"""SECURITY DEFINER user-by-email resolver — fix email-auth 500 under RLS

After 0021 (FORCE RLS on ``users``) the authentication routes (``request_magic_link``,
``resend_verification``, ``signup``, ``verify_email_token``, ``consume_magic_link``)
read/write ``users``/``tenants`` BEFORE the GUC ``app.current_tenant_id`` is set —
the policy ``USING (tenant_id = current_setting('app.current_tenant_id')::uuid)``
(0001) raises ``UndefinedObjectError`` under the ``tendersrb_app`` role (non-superuser,
FORCE RLS), because the GUC is not set yet. Email login had not been tested before this
rollout (users came in via Telegram) — the first live attempt exposed the bug.

Decision (pattern 1:1 with ``app_resolve_tenant_id_by_user_id`` from 0022):
``app_resolve_user_for_auth`` — SECURITY DEFINER resolver of a user by email,
bypasses RLS and returns ``id``/``tenant_id`` BEFORE the GUC is set. The resolver
for user_id already exists (0022), so no new one by user_id is needed.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-04
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Resolve user by email without GUC — for request_magic_link,
    # resend_verification (auth_service.py). Bypasses FORCE RLS on users (0001/0021).
    op.execute("""
        CREATE OR REPLACE FUNCTION app_resolve_user_for_auth(p_email TEXT)
        RETURNS TABLE(id UUID, tenant_id UUID, email_verified_at TIMESTAMPTZ)
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT id, tenant_id, email_verified_at
          FROM users
          WHERE email = p_email AND deleted_at IS NULL
          LIMIT 1
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_resolve_user_for_auth(TEXT) TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_resolve_user_for_auth(TEXT)")
