"""``/statistika`` only by the FULL data (owner 14.10.2026) on a real PG.

Requires ``TEST_RLS_DATABASE_URL`` (a one-off PG; the gate brings it up itself). Its own
“country” ``XR``. “Today” is fixed by the parameter ``today`` — the expectation does not depend on the time of the run.

The fixture (all the expectations are counted by hand and written as literals):

- ``big`` — a tender 02.06 10:00 Belgrade, 5 lots, 3 winners:
  A (lots 1–2, the lot sum NULL, ``winner_contracts_eur`` = 3000 in both rows),
  B (lot 3, 500), C (lots 4–5 — the row 4 with a sum of 200, lot 5 without a sum). Decisions: A, B, C = 3. The count by lots would give 5, and the sum with winner_contracts_eur would double A (6000).
- ``out`` — a tender 01.06 23:00 Belgrade (= 21:00 UTC) — OUTSIDE the period; its decision (777) is not included.
- ``in`` — a tender 02.06 00:30 Belgrade (= 01.06 22:30 UTC) — INSIDE.
- ``fut`` — a decision with ``decision_date`` 2027-06-01 at today=2026-10-14 — cut off.
- a reversal — cut off.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.public import stats_page

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — агрегаты витрины пропускаются",
)

COUNTRY = "XR"
TODAY = date(2026, 10, 14)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# name: published_at (UTC), first_seen_at (UTC)
TENDERS = {
    "big": ("2026-06-02T08:00", "2026-06-03T08:00"),
    "out": ("2026-06-01T21:00", "2026-06-03T08:00"),  # 01.06 23:00 Belgrade
    "in": ("2026-06-01T22:30", "2026-06-03T08:00"),  # 02.06 00:30 Belgrade
}

# (tender, source_uid, lot, winner_pib, winner_name, decision_date, lot_eur,
#  winner_contracts_eur, reversed, customer_pib, customer_name)
A = ("big", "big-uid")
AWARDS = (
    (*A, "1", "111", "A", "2026-06-10", None, "3000", False, "900", "Grad Alfa"),
    (*A, "2", "111", "A", "2026-06-10", None, "3000", False, "900", "Grad Alfa"),
    (*A, "3", "222", "B", "2026-06-10", "500", None, False, "900", "Grad Alfa"),
    (*A, "4", "333", "C", "2026-06-10", "200", None, False, "900", "Grad Alfa"),
    (*A, "5", "333", "C", "2026-06-10", None, None, False, "900", "Grad Alfa"),
    # outside the period by the tender (published 01.06 23:00 Belgrade)
    (
        "out",
        "out-uid",
        None,
        "444",
        "D",
        "2026-06-20",
        "777",
        None,
        False,
        "800",
        "Opština Out",
    ),
    # inside (02.06 00:30 Belgrade), the sum is known
    (
        "in",
        "in-uid",
        None,
        "555",
        "E",
        "2026-07-15",
        "40",
        None,
        False,
        "700",
        "Opština In",
    ),
    # the future — cut off
    (
        "in",
        "fut-uid",
        None,
        "666",
        "F",
        "2027-06-01",
        "9999",
        None,
        False,
        "700",
        "Opština In",
    ),
    # the reversal — cut off
    (
        "in",
        "rev-uid",
        None,
        "777",
        "G",
        "2026-07-16",
        "8888",
        None,
        True,
        "700",
        "Opština In",
    ),
)


@pytest_asyncio.fixture
async def world():
    sup = create_async_engine(
        os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL),
        poolclass=NullPool,
        future=True,
    )
    sfx = uuid.uuid4().hex[:8]
    ids = {name: uuid.uuid4() for name in TENDERS}
    try:
        async with sup.begin() as c:
            for name, (pub, seen) in TENDERS.items():
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, title, published_at, first_seen_at, "
                        "classifications, procedure_type) VALUES (:id, :sk, :uid, "
                        ":cc, 'T', :pub, :seen, '[]'::jsonb, 'open')"
                    ),
                    {
                        "id": ids[name],
                        "sk": f"stc-{sfx}",
                        "uid": f"stc-{sfx}-{name}",
                        "cc": COUNTRY,
                        "pub": _ts(pub),
                        "seen": _ts(seen),
                    },
                )
            for (
                tn,
                uid,
                lot,
                pib,
                wname,
                dd,
                eur,
                contracts,
                rev,
                cpib,
                cname,
            ) in AWARDS:
                raw: dict = {"contract_row": {"IsReversed": rev}}
                if contracts is not None:
                    raw["winner_contracts_eur"] = contracts
                await c.execute(
                    text(
                        "INSERT INTO tender_awards (tender_id, source_uid, "
                        "lot_number, document_type_id, customer_pib, customer_name, "
                        "decision_date, award_amount_eur, winner_pib, winner_name, raw) "
                        "VALUES (:t, :uid, :lot, 1, :cpib, :cn, CAST(:d AS date), "
                        "CAST(:eur AS numeric), :wp, :wn, CAST(:raw AS jsonb))"
                    ),
                    {
                        "t": ids[tn],
                        "uid": f"stc-{sfx}-{uid}-{lot}",
                        "lot": lot,
                        "cpib": cpib,
                        "cn": cname,
                        "d": date.fromisoformat(dd),
                        "eur": Decimal(eur) if eur is not None else None,
                        "wp": pib,
                        "wn": wname,
                        "raw": json.dumps(raw),
                    },
                )
        eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
        yield eng
        await eng.dispose()
    finally:
        async with sup.begin() as c:
            await c.execute(
                text("DELETE FROM tender_awards WHERE tender_id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
        await sup.dispose()


async def _stats(eng) -> stats_page.PublicStats:
    async with AsyncSession(eng) as s:
        result = await stats_page.compute_stats(s, country=COUNTRY, today=TODAY)
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_population_boundary_is_belgrade_midnight(world):
    st = await _stats(world)
    # 01.06 23:00 Belgrade — outside; 02.06 00:30 Belgrade — inside; plus big = 2.
    assert st.tenders_total == 2
    assert [(r.key, r.count) for r in st.tender_months] == [("06.2026", 2)]
    # The period starts 02.06 00:00 Belgrade = 01.06 22:00 UTC.
    assert st.period_start == _ts("2026-06-01T22:00")
    assert st.current_month == "10.2026"


@pytest.mark.asyncio
async def test_decision_is_winner_in_tender_not_lot(world):
    st = await _stats(world)
    # The decisions of the period: A, B, C (big, June) and E (in, July). The rest is cut off:
    # D — a tender outside the period, F — the future, G — the reversal.
    assert [(m.label, m.count, m.known, m.sum_eur) for m in st.award_months] == [
        # June: A=3000 (winner_contracts_eur), B=500, C=200 → 3700, known 3 of 3
        ("06.2026", 3, 3, Decimal("3700")),
        ("07.2026", 1, 1, Decimal("40")),
    ]
    assert st.awards_total == 4
    assert st.awards_known == 4
    assert st.awards_sum_eur == Decimal("3740")
    # The top customers — by the same decisions.
    assert [(c.name, c.count, c.sum_eur) for c in st.top_customers] == [
        ("Grad Alfa", 3, Decimal("3700")),
        ("Opština In", 1, Decimal("40")),
    ]
