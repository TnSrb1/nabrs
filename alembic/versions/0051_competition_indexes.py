"""Indexes for the «Konkurencija» block on the tender card (issue #686).

``src/api/cabinet/competition_service.py`` reads two cuts by awards for each card. Measurement BEFORE on production (read-only, 13.10.2026, docs/reports/TESTING_awards-index.md):

1. By customer: the key ``lower(btrim(regexp_replace(customer_name, …)))`` — an expression
   without an index, ``Seq Scan`` over ``tender_awards`` (13,134 rows), **100 ms**.
   → a functional btree ``tender_awards_customer_key_idx`` on THE SAME key (the expression must match ``_CUSTOMER_NAME_KEY`` verbatim). On the fixture: 4–20 ms.

2. By CPV group: ``EXISTS (… jsonb_array_elements(classifications) … left(code,3) = g)``
   — a ``Nested Loop`` with 6,423 probes over ``tenders_normalized``, **129 ms**. A set-returning
   function cannot be put into an index, so: IMMUTABLE ``cpv_group3(jsonb) → text[]`` (all
   ``left(code,3)`` of the tender’s CPV codes) + GIN over it. The predicate
   ``cpv_group3(classifications) @> ARRAY[g]`` gives the same set of tenders (checked on the fixture: 498 = 498). The group is fixed at 3 digits — ``CPV_GROUP_DIGITS`` in the service.

The indexes are built ``CONCURRENTLY`` via ``autocommit_block`` (not allowed inside an alembic transaction): writes to ``tenders_normalized`` are not blocked. If the build was interrupted, an ``INVALID`` index with the same name remains: before a retry drop it
(``DROP INDEX CONCURRENTLY <name>``), otherwise ``CREATE`` fails with «already exists».

The function ``cpv_group3`` is plain SQL, with no access to tables except the argument; RLS is not affected (the tables are public, RLS_MATRIX category U+0432). ``downgrade`` — DROP of the indexes and the function in reverse order (the index depends on the function).

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CPV_FN = r"""
    CREATE OR REPLACE FUNCTION cpv_group3(j jsonb)
    RETURNS text[]
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
        SELECT COALESCE(array_agg(DISTINCT left(e->>'code', 3)), '{}'::text[])
        FROM jsonb_array_elements(
            CASE WHEN jsonb_typeof(j) = 'array' THEN j ELSE '[]'::jsonb END
        ) e
        WHERE e->>'scheme' = 'CPV'
    $$
"""

_CUSTOMER_KEY_IDX = r"""
    CREATE INDEX CONCURRENTLY tender_awards_customer_key_idx
    ON tender_awards (lower(btrim(regexp_replace(customer_name, '\s+', ' ', 'g'))))
"""

_CPV_GIN_IDX = """
    CREATE INDEX CONCURRENTLY tenders_normalized_cpv_group3_gin
    ON tenders_normalized USING gin (cpv_group3(classifications))
"""


def upgrade() -> None:
    op.execute(_CPV_FN)
    with op.get_context().autocommit_block():
        op.execute(_CUSTOMER_KEY_IDX)
        op.execute(_CPV_GIN_IDX)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS tenders_normalized_cpv_group3_gin"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS tender_awards_customer_key_idx")
    op.execute("DROP FUNCTION IF EXISTS cpv_group3(jsonb)")
