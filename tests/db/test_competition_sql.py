"""Integration: the «Konkurencija» block on a LIVE PG (the mocks check neither the GROUP BY nor the ORDER BY).

The fixture (``sfx`` isolates the rows; the CPV group ``999`` is non-existent in the real
data; “today” is fixed → the 24-month window = from 2024-10-13):

    customer Naruc-sfx (CPV 999xxxxx), tenders:
      T0 the CURRENT tender; it already has a decision in favour of Z (must be excluded)
      T1 A (PIB 111)  100 RSD  2026-09-01
      T2 A            200 RSD  2026-08-01
      T3 B (PIB 222) 5000 RSD  2026-07-01
      T4 B  — THREE lots of 300 RSD (the repeat of the decision sum) 2026-06-01 → ONE win
      T5 «X, Y» (PIB «333, 444») 50 RSD 2026-05-01 — a joint bid
      T6 C (PIB 555)  without a sum 2026-04-01
      T7 D (PIB 888)  2024-09-01 — OUTSIDE the window
      T10 F (PIB 999) 90000 RSD 2026-03-01 — one win, but a large sum
    another customer Other-sfx (the same CPV 999): E (PIB 666) 2 × 1000 RSD
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet.competition_service import get_competition

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
TODAY = date(2026, 10, 13)
_TID = "11111111-1111-1111-1111-111111111111"

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция Konkurencija пропускается",
)

# (the tender key, customer, CPV, [(PIB, name, sum, date, lots)])
_CASES = [
    ("T0", "N", "99900000", [("777", "Z", 9, date(2026, 9, 20), 1)]),
    ("T1", "N", "99900001", [("111", "A", 100, date(2026, 9, 1), 1)]),
    ("T2", "N", "99900002", [("111", "A", 200, date(2026, 8, 1), 1)]),
    ("T3", "N", "99900003", [("222", "B", 5000, date(2026, 7, 1), 1)]),
    ("T4", "N", "99900004", [("222", "B", 300, date(2026, 6, 1), 3)]),
    ("T5", "N", "99900005", [("333, 444", "X, Y", 50, date(2026, 5, 1), 1)]),
    ("T6", "N", "99900006", [("555", "C", None, date(2026, 4, 1), 1)]),
    ("T7", "N", "99900007", [("888", "D", 7, date(2024, 9, 1), 1)]),
    ("T10", "N", "99900010", [("999", "F", 90000, date(2026, 3, 1), 1)]),
    ("T8", "O", "99900008", [("666", "E", 1000, date(2026, 9, 5), 1)]),
    ("T9", "O", "99900009", [("666", "E", 1000, date(2026, 9, 6), 1)]),
]


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:8]
    ids: dict[str, uuid.UUID] = {}
    async with eng.begin() as c:
        for key, who, cpv, awards in _CASES:
            tid = ids[key] = uuid.uuid4()
            cname = f"{'Naruc' if who == 'N' else 'Other'}-{sfx}"
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, first_seen_at, title_normalized, "
                    "customer_name, classifications) VALUES (:id, :k, :k, 'RS', "
                    "'active', now(), 'x', :cn, CAST(:cl AS jsonb))"
                ),
                {
                    "id": tid,
                    "k": f"kn-{sfx}-{key}",
                    "cn": cname,
                    "cl": '[{"scheme": "CPV", "code": "%s"}]' % cpv,
                },
            )
            for pib, name, amount, dd, lots in awards:
                for lot in range(lots):
                    await c.execute(
                        text(
                            "INSERT INTO tender_awards (tender_id, source_uid, "
                            "document_type_id, lot_number, winner_pib, winner_name, "
                            "customer_name, award_amount_rsd, decision_date) "
                            "VALUES (:t, :u, 11, :lot, :p, :n, :cn, :a, "
                            "CAST(:d AS date))"
                        ),
                        {
                            "t": tid,
                            "u": f"kn-{sfx}-{key}-{lot}",
                            "lot": str(lot + 1),
                            "p": pib,
                            "n": name,
                            "cn": cname,
                            "a": amount,
                            "d": dd,
                        },
                    )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield ids, app_eng
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
        await app_eng.dispose()
        await eng.dispose()


def _rows(items):
    return [(r.name, r.wins, r.sum_fmt, r.last_fmt, r.is_joint) for r in items]


@pytest.mark.asyncio
async def test_customer_and_cpv_top(world):
    ids, app_eng = world
    async with AsyncSession(app_eng) as s:
        view = await get_competition(s, tid=_TID, tender_id=str(ids["T0"]), today=TODAY)
    assert view.cpv_group == "999"
    # Customer: B (2 wins: the multi-lot = one; 5300) before A (2; 300) — equality of the
    # wins is decided by the sum; then by 1 win: F (90000 — the number of wins matters more than the sum:
    # F does NOT overtake the two-win A/B), the consortium (50), C (no sum).
    # Z (the current tender) and D (outside the window) are absent.
    assert _rows(view.customer) == [
        ("B", 2, "5 300 RSD", "01.07.2026", False),
        ("A", 2, "300 RSD", "01.09.2026", False),
        ("F", 1, "90 000 RSD", "01.03.2026", False),
        ("X, Y", 1, "50 RSD", "01.05.2026", True),
        ("C", 1, "", "01.04.2026", False),
    ]
    # CPV 999: E of another customer is added to them (2 × 1000 = 2000 RSD);
    # the sixth (C) does not fit into the top 5.
    assert _rows(view.cpv) == [
        ("B", 2, "5 300 RSD", "01.07.2026", False),
        ("E", 2, "2 000 RSD", "06.09.2026", False),
        ("A", 2, "300 RSD", "01.09.2026", False),
        ("F", 1, "90 000 RSD", "01.03.2026", False),
        ("X, Y", 1, "50 RSD", "01.05.2026", True),
    ]
    # The consortium’s PIB is not passed off as an identifier.
    assert [r.pib for r in view.customer] == ["222", "111", "999", "", "555"]


@pytest.mark.asyncio
async def test_other_customer_has_own_slice(world):
    ids, app_eng = world
    async with AsyncSession(app_eng) as s:
        view = await get_competition(s, tid=_TID, tender_id=str(ids["T8"]), today=TODAY)
    # Other-sfx has a single OTHER decision — T9 (T8 is excluded as current).
    assert _rows(view.customer) == [("E", 1, "1 000 RSD", "06.09.2026", False)]
