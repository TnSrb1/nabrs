"""FORCE ROW LEVEL SECURITY on tenant-scoped tables that dropped out of the invariant

Two tables have a ``tenant_isolation`` policy but no ``FORCE`` (fact from prod,
``relforcerowsecurity=false``):

* ``channel_dead_notice_log`` — 0031 enabled ENABLE + policy, but forgot FORCE;
* ``trial_requests`` — 0012 enabled ENABLE + policy; 0021, granting FORCE to "all
  17 tenant tables", did not include this table in its list. The gap has quietly lived
  since 0021 and is unrelated to 0031 — it was found by the general regression test
  ``tests/db/test_rls_force_invariant.py``, added in this same PR.

0012/0021/0031 are already rolled out to prod and are NOT edited — the addition is a separate revision.

What FORCE gives. ``ENABLE`` makes the policies apply to everyone EXCEPT the table
owner; ``FORCE`` extends them to the owner as well. The application (bot+api) runs
under ``tendersrb_app`` (NOBYPASSRLS) — for it the policy works even without FORCE,
so there is no leak through the application. The gap is on the owner: a script/worker/migration
under the owner role would read the rows of all tenants, silently and without an error. FORCE
closes exactly this.

The superuser (``BYPASSRLS``) bypasses RLS despite FORCE — this is a
PostgreSQL guarantee, not a regression; the policies and grants of 0012/0031 are not rewritten.

Additive: DDL only on relforcerowsecurity; schema, data, policies and indexes
do not change. Rolled out to the LIVE prod.

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FORCE_TABLES = (
    "channel_dead_notice_log",
    "trial_requests",
)


def upgrade() -> None:
    for table in _FORCE_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # Only the removal of FORCE — the tables, tenant_isolation policies and ENABLE RLS
    # belong to 0012/0031 and stay in place.
    for table in reversed(_FORCE_TABLES):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
