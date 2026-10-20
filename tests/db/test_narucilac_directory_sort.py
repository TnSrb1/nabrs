"""Integration: the customer directory — sorting, pages, search by PIB on a LIVE PG.

The mocks check neither the ORDER BY, nor the HAVING, nor ``count(*) OVER ()`` — here the real
SQL ``search_customers_page`` against a throwaway PostgreSQL.

The fixture (the suffix ``sfx`` in the names isolates the rows from the rest of the DB; the selection
always goes with ``term=sfx``):

    name       decisions  last date       PIB
    Alfa-sfx      1       2026-01-01      —
    Mid-sfx       2       2026-03-01      —
    Nul-sfx       1       none            —
    Zeta-sfx      3       2026-02-01      on ONE of the three rows

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production).
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

from src.analytics.awards_analytics import search_customers_page

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция справочника наручиоца пропускается",
)

# (name, decision date, PIB) — one row per decision.
_FIXTURE = [
    ("Alfa", date(2026, 1, 1), None),
    ("Mid", date(2026, 3, 1), None),
    ("Mid", date(2026, 2, 1), None),
    ("Nul", None, None),
    ("Zeta", date(2026, 2, 1), "SFXPIB"),
    ("Zeta", date(2026, 1, 15), None),
    ("Zeta", date(2025, 12, 1), None),
]


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:8]
    # The PIB — digits, unique per run: the prefix must not catch foreign rows.
    pib = "9" + str(uuid.uuid4().int)[:8]
    tender_ids: list[uuid.UUID] = []
    async with eng.begin() as c:
        for i, (name, dd, p) in enumerate(_FIXTURE):
            tid = uuid.uuid4()
            tender_ids.append(tid)
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, first_seen_at, title_normalized) "
                    "VALUES (:id, :k, :uid, 'RS', 'active', now(), 'nabavka')"
                ),
                {"id": tid, "k": f"nar-{sfx}-{i}", "uid": f"nar-{sfx}-{i}"},
            )
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, "
                    "document_type_id, customer_name, customer_pib, decision_date) "
                    "VALUES (:t, :uid, 1, :n, :p, CAST(:d AS date))"
                ),
                {
                    "t": tid,
                    "uid": f"nar-{sfx}-{i}",
                    "n": f"{name}-{sfx}",
                    "p": pib if p else None,
                    "d": dd,
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield sfx, pib, app_eng
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": tender_ids},
            )
        await app_eng.dispose()
        await eng.dispose()


async def _names(app_eng, sfx, **kw) -> list[str]:
    async with AsyncSession(app_eng) as s:
        page = await search_customers_page(s, term=sfx, **kw)
    return [r.name.split("-")[0] for r in page.rows]


@pytest.mark.asyncio
async def test_order_per_sort_key(world):
    sfx, _, eng = world
    # By default = decisions_desc; equal ones (Alfa/Nul by 1) — the tie-break by name.
    assert await _names(eng, sfx) == ["Zeta", "Mid", "Alfa", "Nul"]
    assert await _names(eng, sfx, sort="decisions_desc") == [
        "Zeta",
        "Mid",
        "Alfa",
        "Nul",
    ]
    # Decisions without a date — AT THE END (NULLS LAST), not first.
    assert await _names(eng, sfx, sort="last_desc") == ["Mid", "Zeta", "Alfa", "Nul"]
    assert await _names(eng, sfx, sort="name_asc") == ["Alfa", "Mid", "Nul", "Zeta"]


@pytest.mark.asyncio
async def test_unknown_sort_falls_back_to_default(world):
    sfx, _, eng = world
    assert await _names(eng, sfx, sort="name_asc; DROP TABLE x") == [
        "Zeta",
        "Mid",
        "Alfa",
        "Nul",
    ]


@pytest.mark.asyncio
async def test_pagination_total_and_stable_pages(world):
    sfx, _, eng = world
    async with AsyncSession(eng) as s:
        p1 = await search_customers_page(
            s, term=sfx, limit=3, offset=0, sort="name_asc"
        )
        p2 = await search_customers_page(
            s, term=sfx, limit=3, offset=3, sort="name_asc"
        )
    assert p1.total == p2.total == 4
    assert [r.name.split("-")[0] for r in p1.rows] == ["Alfa", "Mid", "Nul"]
    assert [r.name.split("-")[0] for r in p2.rows] == ["Zeta"]
    # A page past the end — empty (total unknown → the service falls back to the 1st).
    async with AsyncSession(eng) as s:
        p9 = await search_customers_page(s, term=sfx, limit=3, offset=30)
    assert p9.rows == [] and p9.total == 0


@pytest.mark.asyncio
async def test_search_by_pib_prefix_keeps_whole_group(world):
    """The PIB is present in ONE of the three Zeta rows: the group is found and counts all 3
    decisions (a WHERE by PIB would give 1 — it would diverge from the profile)."""
    _, pib, eng = world
    async with AsyncSession(eng) as s:
        page = await search_customers_page(s, term=pib[:7])
    assert [(r.name.split("-")[0], r.awards, r.pib) for r in page.rows] == [
        ("Zeta", 3, pib)
    ]
    assert page.total == 1
