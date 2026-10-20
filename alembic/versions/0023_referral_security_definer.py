"""referral SECURITY DEFINER — cross-tenant referral operations (VG-27 RLS regression)

After 0021 (FORCE RLS) + 0022 (user_channels join policy) the tendersrb_app role
handling ``/start ref_<id>`` runs under GUC = the invitee’s tenant. Referral operations
read/write data of the inviter’s (another) tenant, which RLS forbids:
  * ``user_channels`` — the join policy hides the inviter’s rows → attribute_referral → False.
  * ``tenants`` — FORCE RLS filters by GUC → the bonus UPDATE touches 0 rows.

Decision: two SECURITY DEFINER functions (run with superuser rights):
  * ``app_resolve_tenant_by_tg`` — tenant_id by Telegram chat_id (bypasses RLS on
    user_channels and users); used instead of direct JOIN queries.
  * ``app_referral_grant_bonus`` — UPDATE tenants (bypasses RLS on tenants);
    used instead of the direct _GRANT_BONUS_SQL.

The table ``referrals`` has no RLS (0020, 0021 did not add ENABLE/FORCE) —
INSERT/SELECT/UPDATE under tendersrb_app go through the GRANT from 0021.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Resolve tenant_id by Telegram chat_id.
    # Bypasses FORCE RLS on user_channels (0022 join policy) and users (0001).
    # Used in attribute_referral (inviter check) and reward_referral
    # (tenant_id of inviter and invitee).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_resolve_tenant_by_tg(p_tg TEXT)
        RETURNS UUID
        LANGUAGE plpgsql STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_tenant_id UUID;
        BEGIN
            SELECT u.tenant_id
              INTO v_tenant_id
              FROM user_channels uc
              JOIN users u ON u.id = uc.user_id AND u.deleted_at IS NULL
             WHERE uc.channel = 'telegram'
               AND uc.external_id = p_tg
             ORDER BY uc.linked_at DESC NULLS LAST
             LIMIT 1;
            RETURN v_tenant_id;
        END;
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_resolve_tenant_by_tg(TEXT) TO tendersrb_app"
    )

    # Grant of Pro bonus days to the tenant.
    # Bypasses FORCE RLS on tenants on UPDATE from tendersrb_app (GUC = invitee).
    # Logic: access_until = GREATEST(COALESCE(access_until, now()), now()) + N days;
    # tier lite/starter/free/NULL → pro; pro/business/enterprise — leave untouched.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_referral_grant_bonus(p_tenant UUID, p_days INT)
        RETURNS VOID
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = public
        AS $$
        BEGIN
            UPDATE tenants
               SET access_until = GREATEST(COALESCE(access_until, now()), now())
                                  + (p_days * INTERVAL '1 day'),
                   tier = CASE
                            WHEN tier IN ('pro', 'business', 'enterprise') THEN tier
                            ELSE 'pro'
                          END
             WHERE id = p_tenant;
        END;
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_referral_grant_bonus(UUID, INT) TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_referral_grant_bonus(UUID, INT)")
    op.execute("DROP FUNCTION IF EXISTS app_resolve_tenant_by_tg(TEXT)")
