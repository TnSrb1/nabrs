"""Integration: the summary, the trend, the directory and the competitor profile count DECISIONS, not lot rows.

A continuation of #687/#696: the top winners already count the decision (tender_id, winner_pib), while the customer's summary (``_SUMMARY_COLS``) — ``count(*)``/``sum(award_amount_rsd)`` over the lot rows. For a decision over three lots this gives 3 “decisions” and a sum of 900 instead of 300.

The fixture (one tender, three lots, the same decision sum 300 on each lot):

    Solo-sfx: tender S1 — 3 lots, winner W-sfx (PIB 'W-sfx'), the sum 300 on each lot

Expectation (from the fact, not from the model of the code):
    summary:    total_awards=1, total_rsd=300   (a row count would give 3 and 900)
    trend:      2026 → awards=1, total_rsd=300
    directory:  awards=1                        (a row count would give 3)
    competitor: total_wins=1, total_rsd=300

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

from src.analytics.awards_analytics import (
    competitor_stats,
    customer_award_stats,
    customer_yearly_trend,
    search_customers_page,
)

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция сводки по решениям пропускается",
)


@pytest_asyncio.fixture
async def world():
    """One tender × three lots → one decision. The names in lower case (the reconciliation key)."""
    sfx = uuid.uuid4().hex[:8]
    cname = f"solo-{sfx}"
    winner_pib = f"w{sfx}"
    tid = uuid.uuid4()
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                "country_code, status, first_seen_at, title_normalized) "
                "VALUES (:id, :k, :k, 'RS', 'active', now(), 'x')"
            ),
            {"id": tid, "k": f"sumdec-{sfx}"},
        )
        for lot in (1, 2, 3):
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, "
                    "document_type_id, lot_number, winner_pib, winner_name, "
                    "customer_name, award_amount_rsd, decision_date) "
                    "VALUES (:t, :u, 11, :lot, :pib, 'W', :cn, 300, "
                    "CAST('2026-06-01' AS date))"
                ),
                {
                    "t": tid,
                    "u": f"sumdec-{sfx}-{lot}",
                    "lot": str(lot),
                    "pib": winner_pib,
                    "cn": cname,
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield cname, winner_pib, app_eng
    finally:
        # The parent (tenders_normalized) is removed too: otherwise the tests “the table is empty” will fall.
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tender_awards WHERE tender_id = :id"), {"id": tid}
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = :id"), {"id": tid}
            )
        await app_eng.dispose()
        await eng.dispose()


@pytest.mark.asyncio
async def test_summary_one_tender_three_lots_is_one_decision(world):
    cname, _, app_eng = world
    async with AsyncSession(app_eng) as s:
        stats = await customer_award_stats(s, customer_name=cname, months=None)
    # 1 tender, 3 lots, 1 winner → one decision; the decision sum 300, not 900.
    assert stats.total_awards == 1
    assert int(stats.total_rsd) == 300
    assert stats.with_amount == 1
    assert int(stats.avg_rsd) == 300


@pytest.mark.asyncio
async def test_trend_one_tender_three_lots_is_one_decision(world):
    cname, _, app_eng = world
    async with AsyncSession(app_eng) as s:
        trend = await customer_yearly_trend(s, customer_name=cname)
    assert [(y.year, y.awards, int(y.total_rsd)) for y in trend] == [(2026, 1, 300)]


@pytest.mark.asyncio
async def test_directory_one_tender_three_lots_is_one_decision(world):
    cname, _, app_eng = world
    async with AsyncSession(app_eng) as s:
        page = await search_customers_page(s, term=cname)
    assert [(r.name, r.awards) for r in page.rows] == [(cname, 1)]


@pytest.mark.asyncio
async def test_competitor_one_tender_three_lots_is_one_win(world):
    _, winner_pib, app_eng = world
    async with AsyncSession(app_eng) as s:
        stats = await competitor_stats(s, winner_pib=winner_pib, months=None)
    assert stats.total_wins == 1
    assert int(stats.total_rsd) == 300
