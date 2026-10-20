"""Integration: the sum of the decision of a winner of several lots = raw.winner_contracts_rsd.

The fixture — tender 391142 from the live probe of 13.10 (docs/reports/probe_awards_contracts_20261013.json):
5 lots, 3 winners. 100270693 won lots 1,4,5 → award_amount_rsd NULL on the lots,
but his three contracts give 6 821 317.65 (4787690.16 + 1337124.69 + 696502.8).
100000266 (lot 2) → 2 370 678.0; 107304324 (lot 3) → 303 406.6.

Expectation: 3 decisions, the sum 9 495 402.25 (without winner_contracts_rsd it would be 2 674 084.6 —
the decision of 100270693 went to NULL and dropped out of the sum). The competitor 100270693: 1 win, 6 821 317.65.
EUR — the same decision sum divided by the batch rate 117.0 (the fixture), rounded per each
decision: 58 301.86 + 20 262.21 + 2 593.22 = 81 157.29 (the naive 9495402.25/117 gives 81 157.28 — a cent
lower because of the rounding per decisions). The competitor 100270693: 58 301.86.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.analytics.awards_analytics import competitor_stats, customer_award_stats

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция сумм решения пропускается",
)

# the batch rate by which the parser computes raw.winner_contracts_eur (the fixture, as in TESTING)
FX = 117.0

# (lot, PIB, the lot’s award_amount_rsd, raw.winner_contracts_rsd) — from the probe 391142
LOTS = [
    (1, "100270693", None, 6821317.65),
    (2, "100000266", 2370678.0, 2370678.0),
    (3, "107304324", 303406.6, 303406.6),
    (4, "100270693", None, 6821317.65),
    (5, "100270693", None, 6821317.65),
]


@pytest_asyncio.fixture
async def world():
    sfx = uuid.uuid4().hex[:8]
    cname = f"studenica-{sfx}"
    tid = uuid.uuid4()
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                "country_code, status, first_seen_at, title_normalized) "
                "VALUES (:id, :k, :k, 'RS', 'active', now(), 'x')"
            ),
            {"id": tid, "k": f"wct-{sfx}"},
        )
        for lot, pib, amount, total in LOTS:
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, "
                    "document_type_id, lot_number, winner_pib, winner_name, "
                    "customer_name, award_amount_rsd, decision_date, raw) "
                    "VALUES (:t, :u, 11, :lot, :pib, 'W', :cn, "
                    "CAST(:amt AS numeric), CAST('2026-09-16' AS date), "
                    "CAST(:raw AS jsonb))"
                ),
                {
                    "t": tid,
                    "u": f"wct-{sfx}-{lot}",
                    "lot": str(lot),
                    "pib": pib,
                    "cn": cname,
                    "amt": amount,
                    "raw": json.dumps(
                        {
                            "winner_contracts_rsd": total,
                            "winner_contracts_eur": round(total / FX, 2),
                        }
                    ),
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield cname, app_eng
    finally:
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
async def test_customer_summary_uses_winner_total(world):
    cname, app_eng = world
    async with AsyncSession(app_eng) as s:
        stats = await customer_award_stats(s, customer_name=cname, months=None)
    assert stats.total_awards == 3
    assert stats.with_amount == 3
    assert float(stats.total_rsd) == pytest.approx(9495402.25)
    assert float(stats.total_eur) == pytest.approx(81157.29)


@pytest.mark.asyncio
async def test_competitor_total_uses_winner_total(world):
    async with AsyncSession(world[1]) as s:
        stats = await competitor_stats(s, winner_pib="100270693", months=None)
    assert stats.total_wins == 1
    assert float(stats.total_rsd) == pytest.approx(6821317.65)
    assert float(stats.total_eur) == pytest.approx(58301.86)


@pytest.mark.asyncio
async def test_backfill_dry_run_counts_only_real_changes(world):
    """Lot 2 in the DB already matches the recount by the sum and raw → 0; lot 1 (NULL) → also 0 by the sum,
    but for lot 3 we substitute the sum → exactly 1 real change."""
    from pathlib import Path

    from scripts.backfill_award_contracts import _count_changes, recompute_rows

    probe = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/reports/probe_awards_contracts_20261013.json"
        ).read_text(encoding="utf-8")
    )["391142"]
    rows = {
        r["lot_number"]: r
        for r in recompute_rows("391142", probe["decisions"], probe["contracts"], 117.0)
    }
    cname, app_eng = world
    async with AsyncSession(app_eng) as s:
        tid = (
            await s.execute(
                text(
                    "SELECT tender_id FROM tender_awards WHERE customer_name = :c LIMIT 1"
                ),
                {"c": cname},
            )
        ).scalar()
        # the fixture’s dates/flags match the recount (2026-09-16, has_changed=false)
        before = {
            lot: await _count_changes(s, tid, rows[lot]) for lot in ("1", "2", "3")
        }
    # the EUR/VAT/contract date/term are not filled in the fixture → the recount changes them on lots 2,3;
    # on lot 1 (all NULL, raw.winner_contracts_rsd matches) — there is nothing to change.
    assert before["1"] == 0
    assert before["2"] == 1
    assert before["3"] == 1


@pytest.mark.asyncio
async def test_backfill_dry_run_sees_missing_winner_eur(world):
    """A row where raw.winner_contracts_eur has not been written yet (an old ingest) — a real change.

    Lot 1 (the winner 100270693, the RSD sum is already correct) → the EUR field is erased → the count = 1; without the predicate on winner_contracts_eur the count would be 0 and the backfill would not write the EUR into the old rows.
    """
    from pathlib import Path

    from scripts.backfill_award_contracts import _count_changes, recompute_rows

    probe = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "docs/reports/probe_awards_contracts_20261013.json"
        ).read_text(encoding="utf-8")
    )["391142"]
    rows = {
        r["lot_number"]: r
        for r in recompute_rows("391142", probe["decisions"], probe["contracts"], FX)
    }
    cname, app_eng = world
    async with AsyncSession(app_eng) as s:
        tid = (
            await s.execute(
                text(
                    "SELECT tender_id FROM tender_awards WHERE customer_name = :c LIMIT 1"
                ),
                {"c": cname},
            )
        ).scalar()
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as c:
            await c.execute(
                text(
                    "UPDATE tender_awards SET raw = raw - 'winner_contracts_eur' "
                    "WHERE tender_id = :t AND lot_number = '1'"
                ),
                {"t": tid},
            )
    finally:
        await eng.dispose()
    async with AsyncSession(app_eng) as s:
        assert await _count_changes(s, tid, rows["1"]) == 1
