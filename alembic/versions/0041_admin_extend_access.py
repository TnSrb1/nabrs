"""SECURITY DEFINER for extending access + the notification target per tenant

Closes TWO defects with one additive migration (no new tables/columns,
only functions — rolls out on the live production).

1. ``app_admin_extend_access(p_tid uuid, p_days int) RETURNS timestamptz`` —
   ATOMIC extension of ``tenants.access_until`` by N days. The base of the count is the LARGER of the dates
   (the effective term / ``now()``), the expression is read in the same
   UPDATE, so the caller does not need to know the current term (under the role
   ``tendersrb_app`` it does not see a foreign tenant, FORCE RLS). The body is a mirror of
   :func:`src.domain.access.extend_until_sql` (“one predicate per meaning”);
   the guard of the mirror is ``tests/db/test_0041_admin_extend_access.py``.

   Why: three issuing points (``admin_service.approve_request``,
   ``billing.service.activate_plan_manual``, ``bots.trial_service.decide``)
   computed the term from ``now()`` and silently SHORTENED the access of a tenant whose access was already
   longer (incident 04.08.2026 on the neighbouring admin button: −11 days with an active
   subscription). There was no way to read the base under the app role: there was no SECURITY DEFINER
   returning ``access_until`` BY tenant_id (0025 returns it only by
   ``user_id`` — ``app_admin_get_user``).

   ``app_admin_grant_access`` (0025) is NOT touched: it sets the ABSOLUTE date of the provider
   (``current_period_end`` of the webhook) — a different contract, not “N days”.

2. ``app_admin_tenant_notify_target(p_tid uuid)
   RETURNS TABLE(chat_id text, email text)`` — the recipient of the decision notice on a request:
   the linked Telegram chat (if any) + the email of one live user of the tenant. The body is 1:1 with the old
   bot ``_TARGET_SQL`` (the behavior does not change).

   Why: the decision on a request is made by the owner-operator, the tenant is a FOREIGN one, and ``users`` and
   ``user_channels`` are under FORCE RLS (0021/0022). The bot runs under ``tendersrb_app``
   and without a GUC got ``UndefinedObjectError`` on this same query — that is, the
   “3/15/30 days / reject” buttons failed ENTIRELY, not only on granting access. A GUC does not fit here
   by the invariant: cross-tenant access — only via SECURITY DEFINER.

Both functions follow the pattern 1:1 with 0025/0034/0035: ``SECURITY DEFINER``,
``SET search_path = public``, ``GRANT EXECUTE ... TO tendersrb_app``.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-06
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The extension expression — a VERBATIM mirror of
# ``src.domain.access.extend_until_sql("tenants", "p_days")``. A copy here, not an
# import: a migration must remain an immutable snapshot, and the evolution of src
# does not rewrite an already applied body. The agreement of the two sides is held by the test
# ``tests/db/test_0041_admin_extend_access.py`` (divergence → red).
_EXTEND_EXPR = (
    "GREATEST(COALESCE(tenants.access_until, now()), now()) "
    "+ (CAST(p_days AS int) * INTERVAL '1 day')"
)


def upgrade() -> None:
    # Extending access by N days. updated_at is touched the same way as
    # app_admin_grant_access (0025). A non-existent tenant → UPDATE 0 rows →
    # the function returns NULL (the caller must notice this, see
    # src/domain/access_grants.py).
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION app_admin_extend_access(p_tid uuid, p_days int)
        RETURNS timestamptz
        LANGUAGE SQL VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
          UPDATE tenants
             SET access_until = {_EXTEND_EXPR},
                 updated_at   = now()
           WHERE id = p_tid
          RETURNING access_until
        $$
        """
    )
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_extend_access(uuid, int) TO tendersrb_app"
    )

    # The recipient of the decision notice (cross-tenant read of users/user_channels).
    # The body 1:1 with the old raw _TARGET_SQL of the bot: the Telegram chat (if linked and
    # active) + the email of one live user of the tenant.
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
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_tenant_notify_target(uuid) "
        "TO tendersrb_app"
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS app_admin_tenant_notify_target(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_extend_access(uuid, int)")
