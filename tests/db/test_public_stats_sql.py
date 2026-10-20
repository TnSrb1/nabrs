"""The aggregates of ``/statistika`` on a real PG (``stats_page.compute_stats``).

Requires ``TEST_RLS_DATABASE_URL`` (a one-off PG; the gate brings it up itself). We read
under the application role (as the route does). The fixture lives in its own “country” ``XQ`` —
the foreign rows of the common test DB do not get into the figures.

What we prove by literals:
  1. An archive row (published in 2019, seen by us in 2026) does not move the start
     of the period and does not get into the count — the liveness predicate works.
  2. The monthly series, the top of the CPV sections, the breakdown by procedures — exact numbers.
  3. Decisions: the reversed (``IsReversed``) is excluded; a NULL sum is counted
     in “decisions”, but not in “known” and is not turned into 0; a decision before the start
     of the period is not included; the top customers — without the PIB outward.
  4. The completeness and the unit of the decision (lots, the Belgrade boundaries, the future) —
     ``test_public_stats_complete.py``.
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

COUNTRY = "XQ"


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# name: (published_at, first_seen_at, cpv_code, procedure_type)
TENDERS = {
    "a": ("2026-06-10T08:00", "2026-06-10T08:00", "45210000", "open"),
    "b": ("2026-06-20T08:00", "2026-06-21T08:00", "45000000", "open"),
    "c": ("2026-07-05T08:00", "2026-07-05T09:00", "33100000", "restricted"),
    # An archive: published in 2019, seen 01.06.2026 — EARLIER than the live flow.
    "arch": ("2019-03-01T08:00", "2026-06-01T08:00", "15000000", "open"),
}

# (tender, the decision key source_uid, lot_number, decision_date, eur,
#  customer_pib, customer_name, IsReversed)
AWARDS = (
    ("a", "aw0", None, "2026-06-15", "1000.00", "100", "Opština X", False),
    ("a", "aw1", None, "2026-06-20", None, "100", "Opština X", False),
    ("b", "aw2", None, "2026-07-01", "500.00", "200", "Grad Y", True),  # reversal
    ("b", "aw3", None, "2026-07-02", "300.00", None, "Škola Z", False),
    # before the start of the period
    ("c", "aw4", None, "2026-05-01", "9999.00", "300", "Rano", False),
    # The consortium: ONE decision (source_uid+lot) in two rows, the sum — on the first
    # (this is how explode_award_winners lays it out).
    ("c", "kons", "1", "2026-07-10", "2000.00", "400", "Dom zdravlja K", False),
    ("c", "kons", "1", "2026-07-10", None, "400", "Dom zdravlja K", False),
    # The reversal as the string "True" (as in the raw JSON of the portal), rather than a boolean.
    ("c", "rev-str", "1", "2026-07-11", "700.00", "500", "Bolnica R", "True"),
)


@pytest_asyncio.fixture
async def world():
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    sup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:8]
    ids = {name: uuid.uuid4() for name in TENDERS}
    try:
        async with sup.begin() as c:
            for name, (pub, seen, cpv, proc) in TENDERS.items():
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, title, published_at, first_seen_at, "
                        "classifications, procedure_type) VALUES (:id, :sk, :uid, "
                        ":cc, 'T', :pub, :seen, CAST(:cls AS jsonb), :proc)"
                    ),
                    {
                        "id": ids[name],
                        "sk": f"stats-{sfx}",
                        "uid": f"stats-{sfx}-{name}",
                        "cc": COUNTRY,
                        "pub": _ts(pub),
                        "seen": _ts(seen),
                        "cls": json.dumps([{"scheme": "CPV", "code": cpv}]),
                        "proc": proc,
                    },
                )
            for tn, key, lot, dd, eur, pib, cname, rev in AWARDS:
                await c.execute(
                    text(
                        "INSERT INTO tender_awards (tender_id, source_uid, "
                        "lot_number, document_type_id, customer_pib, customer_name, "
                        "decision_date, award_amount_eur, winner_name, raw) "
                        "VALUES (:t, :uid, :lot, 1, :pib, :n, CAST(:d AS date), "
                        "CAST(:eur AS numeric), CAST(:wn AS text), "
                        "CAST(:raw AS jsonb))"
                    ),
                    {
                        "t": ids[tn],
                        "uid": f"stats-{sfx}-{key}",
                        # A winner without a PIB: the decision key is (tender, name), so
                        # different decisions of one tender are told apart by name.
                        "wn": f"Preduzetnik {key}",
                        "lot": lot,
                        "pib": pib,
                        "n": cname,
                        "d": date.fromisoformat(dd),
                        "eur": Decimal(eur) if eur is not None else None,
                        "raw": json.dumps({"contract_row": {"IsReversed": rev}}),
                    },
                )
        app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
        yield app_eng
        await app_eng.dispose()
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
        result = await stats_page.compute_stats(
            s, country=COUNTRY, today=date(2026, 10, 14)
        )
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_period_excludes_archive_row(world):
    st = await _stats(world)
    # The archive row does not go into the count; the start of the period is REGULAR_SINCE (02.06 00:00
    # Belgrade = 01.06 22:00 UTC), not min(published_at) (10.06).
    assert st.period_start == _ts("2026-06-01T22:00")
    assert st.as_of == _ts("2026-07-05T09:00")
    assert st.tenders_total == 3


@pytest.mark.asyncio
async def test_tender_breakdowns(world):
    st = await _stats(world)
    assert [(r.key, r.count) for r in st.tender_months] == [
        ("06.2026", 2),
        ("07.2026", 1),
    ]
    assert [(r.key, r.count) for r in st.cpv_top] == [("45", 2), ("33", 1)]
    assert [(r.key, r.count) for r in st.procedures] == [
        ("open", 2),
        ("restricted", 1),
    ]


@pytest.mark.asyncio
async def test_awards_reversed_excluded_null_not_zero(world):
    st = await _stats(world)
    # The consortium — one decision (not two), the sum 2000 once; the reversal «True»
    # as a string and as a boolean — outside the count; a NULL sum — in “decisions”, not in “known”.
    assert [(m.label, m.count, m.known, m.sum_eur) for m in st.award_months] == [
        ("06.2026", 2, 1, Decimal("1000.00")),
        ("07.2026", 2, 2, Decimal("2300.00")),
    ]
    assert st.awards_total == 4
    assert st.awards_known == 3
    assert st.awards_sum_eur == Decimal("3300.00")
    assert [(c.name, c.count, c.sum_eur) for c in st.top_customers] == [
        ("Dom zdravlja K", 1, Decimal("2000.00")),
        ("Opština X", 2, Decimal("1000.00")),
        ("Škola Z", 1, Decimal("300.00")),
    ]


@pytest.mark.asyncio
async def test_empty_country_returns_none(world):
    async with AsyncSession(world) as s:
        assert await stats_page.compute_stats(s, country="XZ") is None
