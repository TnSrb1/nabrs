"""Deterministic choice of “one row” in the SECURITY DEFINER resolvers.

Three places took a `LIMIT 1` WITHOUT `ORDER BY` — the order in PostgreSQL is then
undefined and depends on the plan and the physical placement of rows:

1. ``app_resolve_user_for_auth(p_email)`` (0024) — login resolve. ‼️ On production on
   10.08.2026 ``owner@example.com`` had TWO live users in DIFFERENT tenants (``e968c1ff…`` and ``f2cc3032…``);
   the function returned ``f2cc3032`` by luck of the plan. A plan change = a login into another account with an empty
   cabinet — exactly the symptom observed on 04.08.2026.
2. ``app_admin_tenant_notify_target(p_tid)`` (0041), the channel subquery — which of the
   user's active Telegram channels to take.
3. The same function, the choice of the tenant's user — whom to address the notice about the
   decision on a request.

The order is chosen so as NOT to change today's actual behavior (otherwise fixing the determinism would itself become a login incident):

* user by email → ``created_at``, tie-break ``id``: the earliest account.
  For the duplicate above that is ``f2cc3032`` (13.06) — the same the function returns now.
* user of the tenant → first ``role = 'admin'`` (the account owner), then ``created_at``, tie-break ``id``.
* channel → ``linked_at DESC NULLS LAST``, tie-break ``id DESC``: the ``CLAUDE.md`` canon —
  “take the freshest active” (one chat_id may have several active channels).

‼️ WHICH of two accounts of one email is “correct” at login is a product question and remains
with the owner (``QUESTIONS_D1``: multi-tenancy of one address). This migration does not decide it, it only removes the randomness.

Only function bodies change: no tables, no data, no grants (the GRANT on
name+signature is kept on ``CREATE OR REPLACE``, but we re-issue it explicitly —
cheaper than relying on it).

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Login resolve: deterministic choice when the email is duplicated.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_resolve_user_for_auth(p_email TEXT)
        RETURNS TABLE(id UUID, tenant_id UUID, email_verified_at TIMESTAMPTZ)
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT id, tenant_id, email_verified_at
          FROM users
          WHERE email = p_email AND deleted_at IS NULL
          ORDER BY created_at, id
          LIMIT 1
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_resolve_user_for_auth(TEXT) TO tendersrb_app"
    )

    # 2+3. Notice recipient: freshest active channel + tenant owner.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_tenant_notify_target(p_tid uuid)
        RETURNS TABLE(chat_id text, email text)
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT (SELECT uc.external_id FROM user_channels uc
                   WHERE uc.user_id = u.id AND uc.channel = 'telegram'
                     AND uc.status = 'active'
                   ORDER BY uc.linked_at DESC NULLS LAST, uc.id DESC
                   LIMIT 1)        AS chat_id,
                 u.email           AS email
          FROM users u
          WHERE u.tenant_id = p_tid AND u.deleted_at IS NULL
          ORDER BY (u.role = 'admin') DESC, u.created_at, u.id
          LIMIT 1
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_tenant_notify_target(uuid) "
        "TO tendersrb_app"
    )


def downgrade() -> None:
    # Return to the non-deterministic bodies (0024 / 0041) — as it was.
    op.execute(
        """
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
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_admin_tenant_notify_target(p_tid uuid)
        RETURNS TABLE(chat_id text, email text)
        LANGUAGE SQL STABLE SECURITY DEFINER
        SET search_path = public
        AS $$
          SELECT (SELECT uc.external_id FROM user_channels uc
                   WHERE uc.user_id = u.id AND uc.channel = 'telegram'
                     AND uc.status = 'active'
                   LIMIT 1)        AS chat_id,
                 u.email           AS email
          FROM users u
          WHERE u.tenant_id = p_tid AND u.deleted_at IS NULL
          LIMIT 1
        $$
        """
    )
