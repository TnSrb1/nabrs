"""Canonical ``tenants.tier``: backfill → CHECK + DEFAULT 'lite' + NOT NULL (R8).

R8 audit: the column ``tenants.tier`` lived without CHECK/default/NOT NULL, and the write
sites diverged from the internal tier canon (``src/domain/tiers.py``
``TIER_ORDER = lite/starter/pro/business/enterprise``):

* bot signup wrote ``tier='free'`` (a storefront alias, NOT an internal tier);
* web signup (``auth_service.signup``) did not set ``tier`` at all → ``NULL``;

while gating (``effective_tier``/``normalize_tier``) silently collapsed everything
unknown into ``lite``. This worked by happy accident, but had no protection at the
DB level: any bad value would have reached storage.

Canon (owner's decision): normalize the stored values to the internal tiers and fix
the invariant in the schema. The order MATTERS — backfill BEFORE NOT NULL/CHECK, otherwise
ALTER would fail on existing ``'free'``/``NULL``:

1. UPDATE: ``free``/``NULL``/any non-canon → ``lite`` (protective backfill).
2. ALTER … SET DEFAULT 'lite' (new INSERTs without tier → lite, not NULL).
3. ALTER … SET NOT NULL (after the backfill there are no NULLs left).
4. ADD CONSTRAINT ck_tenants_tier — canon only.

The reference function ``app_referral_grant_bonus`` (0023) writes only canon
values (``pro``/``business``/``enterprise``) — the CHECK does not break it. Write sites
(bot/web/admin) are fixed in this same PR to the canon.

RLS: the migration runs as the superuser (service ``migrate``) — ALTER/UPDATE on
``tenants`` (FORCE RLS, 0021) are allowed, ``set_tenant`` is not needed here.

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Internal tier canon — SSOT ``src/domain/tiers.py`` (TIER_ORDER). Stored
# as a literal in the migration (migrations do not import runtime code), the values MUST
# match TIER_ORDER — a divergence is caught by the migration test.
_CANON_TIERS = ("lite", "starter", "pro", "business", "enterprise")
_CANON_SQL_LIST = ", ".join(f"'{t}'" for t in _CANON_TIERS)


def upgrade() -> None:
    # 1. Protective backfill: 'free'/NULL and any non-canon value → 'lite' (fallback
    #    of the normalizer). BEFORE NOT NULL/CHECK, otherwise ALTER would fail.
    op.execute(
        f"""
        UPDATE tenants
           SET tier = 'lite'
         WHERE tier IS NULL
            OR tier NOT IN ({_CANON_SQL_LIST})
        """
    )
    # 2. DEFAULT — new INSERTs without an explicit tier get 'lite', not NULL.
    op.execute("ALTER TABLE tenants ALTER COLUMN tier SET DEFAULT 'lite'")
    # 3. NOT NULL — after the backfill there are no NULLs left.
    op.execute("ALTER TABLE tenants ALTER COLUMN tier SET NOT NULL")
    # 4. CHECK — canon only.
    op.execute(
        f"""
        ALTER TABLE tenants
          ADD CONSTRAINT ck_tenants_tier
          CHECK (tier IN ({_CANON_SQL_LIST}))
        """
    )


def downgrade() -> None:
    # Reverse order: CHECK first, then NOT NULL, then DEFAULT.
    op.execute("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_tier")
    op.execute("ALTER TABLE tenants ALTER COLUMN tier DROP NOT NULL")
    op.execute("ALTER TABLE tenants ALTER COLUMN tier DROP DEFAULT")
