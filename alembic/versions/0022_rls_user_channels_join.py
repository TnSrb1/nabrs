"""RLS user_channels — replace USING(true) with a join policy via users.tenant_id

Closes the bootstrap hack from 0021: the ``USING(true)`` policy on ``user_channels``
gave the tendersrb_app role access to chat_id↔user_id of ALL tenants without GUC.

Decision (VG-01 hardening):
  * Replace ``USING(true)`` with a join via ``users.tenant_id`` — the same pattern
    as in 0003 for profile_revisions / delivery_outbox.
  * Add the SECURITY DEFINER function ``app_resolve_tenant_id_by_user_id`` for
    ``bind_channel_by_token`` (cabinet↔bot, C-04): it resolves tenant_id by
    user_id without GUC, so that GUC is set before user_channels is read.
  * ``user_channels`` is already under ``FORCE ROW LEVEL SECURITY`` from 0021 — not touched.
  * Tenant resolution for the bot (``get_subscriber``, ``save_subscriber``, ``cmd_start``,
    ``cmd_reset``) is provided by ``app_resolve_tenant_id`` (0021) — called BEFORE
    ``_find_channel``, so GUC is set before user_channels is read.

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Remove the bootstrap USING(true) — now app_resolve_tenant_id (SECURITY DEFINER)
    # is called BEFORE GUC is set, so the join policy works correctly.
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON user_channels")

    # Join policy: only the rows of the tenant from GUC are visible. Pattern from 0003.
    op.execute(
        "CREATE POLICY tenant_isolation ON user_channels\n"
        "  USING (user_id IN (\n"
        "    SELECT id FROM users\n"
        "    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid))"
    )

    # SECURITY DEFINER for bind_channel_by_token (cabinet↔bot, C-04):
    # obtains tenant_id by user_id from the token without GUC (token.user_id from auth_tokens,
    # which has no RLS), so that GUC is set before user_channels is read.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_resolve_tenant_id_by_user_id(p_user_id UUID)
        RETURNS UUID
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT tenant_id FROM users WHERE id = p_user_id
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_resolve_tenant_id_by_user_id(UUID) TO tendersrb_app"
    )


def downgrade() -> None:
    # Drop the SECURITY DEFINER function from 0022.
    op.execute("DROP FUNCTION IF EXISTS app_resolve_tenant_id_by_user_id(UUID)")

    # Restore the bootstrap USING(true) — the 0021 state.
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON user_channels")
    op.execute(
        "CREATE POLICY tenant_isolation ON user_channels USING (true)"
    )
