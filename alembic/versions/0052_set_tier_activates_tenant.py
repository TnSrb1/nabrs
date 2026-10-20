"""Issuing a paid plan moves tenants.status trial → active.

Fact 13.10.2026 (production): all 10 tenants have ``status='trial'``, ``active`` — 0.
``effective_tier`` (src/domain/tiers.py) with ``trial`` returns ``pro`` REGARDLESS of
``tier``, and the gate of auto-analysis (``auto_limit_decision``) on ``trial`` gives the demo quota
of 10. ``app_admin_set_tier`` (0025) changed only ``tier``, so a tenant granted
``business`` lived with Pro rights and the demo quota.

All plan-issuing paths — the webhook ``apply_billing_event``, ``activate_plan_manual``
(approval of a request in the bot and in the admin) and the “Change plan” button — go through this
function, therefore the status is set HERE, not in three places in Python.

Rule: a paid ``p_tier`` (not ``lite``) moves ``trial → active``. The other
statuses (``suspended``/``cancelled``/``active``) are NOT touched: suspension is done
by the operator deliberately. The return to ``trial``/``lite`` when the term ends does NOT change —
the freeze by ``access_until`` is held by ``is_frozen``. The function does not touch the access term
(monotonicity — ``app_admin_extend_access``/``app_admin_grant_access``).

A backfill of existing tenants is NOT done by the migration: the owner's decision, preview —
``scripts/oneoff/tenants_status_from_tier.sql``.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_set_tier(p_tid uuid, p_tier text)
        RETURNS void
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE tenants
             SET tier       = p_tier,
                 status     = CASE
                                WHEN status = 'trial' AND p_tier <> 'lite'
                                THEN 'active'
                                ELSE status
                              END,
                 updated_at = NOW()
           WHERE id = p_tid
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_set_tier(uuid, text) TO tendersrb_app"
    )


def downgrade() -> None:
    # Return to the body of 0025 (tier only).
    op.execute("""
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
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_set_tier(uuid, text) TO tendersrb_app"
    )
