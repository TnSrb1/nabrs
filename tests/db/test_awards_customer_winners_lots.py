"""Integration: the top winners of the customer (the profile) count DECISIONS, not lot rows.

Issue #687: ``_CUSTOMER_TOP_WINNERS_*`` did ``count(*)`` over ``tender_awards``, and a decision over several lots has as many rows as there are lots (the sum is repeated on each).
The mocks do not check the SQL — here a real PG (throwaway, NOT the deploy production).

The fixture (``sfx`` isolates the rows; the customers by name, without PIB):

    Solo-sfx: tender S1 — 3 lots, winner A (PIB 111), the decision sum 300 on each lot
    Duo-sfx:  tender owner — 3 lots, A (300 on each); tender partner — 1 lot, A (200)

Expectation (from the fact, not from the model of the code):
    Solo → A: wins=1, sum=300   (a row count would give wins=3, sum=900)
    Duo  → A: wins=2, sum=500   (a row count would give wins=4, sum=1100)

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.analytics.awards_analytics import customer_award_stats

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция победителей наручиоца пропускается",
)

# (customer, [(tender, [(lot, sum RSD)])]) — the winner A (PIB 111) in all the rows.
_CASES = [
    ("Solo", [("S1", [(1, 300), (2, 300), (3, 300)])]),
    ("Duo", [("owner", [(1, 300), (2, 300), (3, 300)]), ("partner", [(1, 200)])]),
]


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:8]
    tender_ids: list[uuid.UUID] = []
    async with eng.begin() as c:
        for who, tenders in _CASES:
            cname = f"{who}-{sfx}"
            for key, lots in tenders:
                tid = uuid.uuid4()
                tender_ids.append(tid)
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, status, first_seen_at, title_normalized) "
                        "VALUES (:id, :k, :k, 'RS', 'active', now(), 'x')"
                    ),
                    {"id": tid, "k": f"lots-{sfx}-{key}"},
                )
                for lot, amount in lots:
                    await c.execute(
                        text(
                            "INSERT INTO tender_awards (tender_id, source_uid, "
                            "document_type_id, lot_number, winner_pib, winner_name, "
                            "customer_name, award_amount_rsd, decision_date) "
                            "VALUES (:t, :u, 11, :lot, '111', 'A', :cn, :a, "
                            "CAST('2026-06-01' AS date))"
                        ),
                        {
                            "t": tid,
                            "u": f"lots-{sfx}-{key}-{lot}",
                            "lot": str(lot),
                            "cn": cname,
                            "a": amount,
                        },
                    )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield sfx, app_eng
    finally:
        # The parents (tenders_normalized) are removed too: otherwise the neighbouring tests, which
        # count “the table is empty”, fall over on our rows.
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tender_awards WHERE tender_id = ANY(:ids)"),
                {"ids": tender_ids},
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": tender_ids},
            )
        await app_eng.dispose()
        await eng.dispose()


async def _winners(app_eng, name: str):
    async with AsyncSession(app_eng) as s:
        stats = await customer_award_stats(s, customer_name=name, months=None)
    return [(w.pib, w.wins, int(w.sum_rsd)) for w in stats.top_winners]


@pytest.mark.asyncio
async def test_one_tender_three_lots_is_one_win(world):
    sfx, app_eng = world
    # 1 tender, 3 lots, 1 winner → 1 win; the decision sum 300, not 900.
    assert await _winners(app_eng, f"Solo-{sfx}") == [("111", 1, 300)]


@pytest.mark.asyncio
async def test_two_decisions_sum_by_decision_max_per_lot(world):
    sfx, app_eng = world
    # owner (3 lots of 300) = one win for 300; partner (1 lot, 200) = the second win.
    assert await _winners(app_eng, f"Duo-{sfx}") == [("111", 2, 500)]
