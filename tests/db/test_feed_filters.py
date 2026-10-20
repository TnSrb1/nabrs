"""Integration: the filters and the sorting of the feed /app/feed on a real PG.

Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER the deploy production) —
otherwise a skip. A mock will not prove that PostgreSQL accepts ``CAST(:p AS type)`` and the
``strpos`` fold, therefore every filter is checked by a DB query: it must NARROW the set to the expected one (a literal of the fixture), and a foreign tenant — must not be visible.

The fixture (tenant A, two templates; tenant B with one “foreign” tender):
  t_school  — «Izgradnja škola»,   Zavod Beograd,  100 EUR, jnportal, region A, P1, deadline +10d, published 01.09
  t_road    — «Popravka puta»,     Opština Niš,    5000 EUR, jnportal, region B, P1, deadline +3d,  published 05.09
  t_it      — «Nabavka računara»,  Zavod Beograd,  NULL,    other,    region A, P2, deadline +30d, published 10.09
  t_old     — «Stara škola»,       Zavod Beograd,  300 EUR, jnportal, region A, P2, deadline −2d,  published 20.08
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.cabinet import feed_service
from src.api.public.portal_filters import parse_filters
from src.domain.text_norm import normalize_text

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция /app/feed пропускается",
)

REGION_A = "RS-00"
REGION_B = "RS-21"


@pytest_asyncio.fixture
async def sessionmaker_():
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    yield async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def fx():
    sup_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    eng = create_async_engine(sup_url, poolclass=NullPool, future=True)
    ta, tb = uuid.uuid4(), uuid.uuid4()
    p1, p2, pb = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    tenders = {
        "school": (
            "Izgradnja škola",
            "Zavod Beograd",
            100,
            "jnportal",
            REGION_A,
            p1,
            10,
            "2026-09-01",
            "opis radova",
        ),
        "road": (
            "Popravka puta",
            "Opština Niš",
            5000,
            "jnportal",
            REGION_B,
            p1,
            3,
            "2026-09-05",
            "asfalt",
        ),
        "it": (
            "Nabavka računara",
            "Zavod Beograd",
            None,
            "other",
            REGION_A,
            p2,
            30,
            "2026-09-10",
            "laptop",
        ),
        "old": (
            "Stara škola",
            "Zavod Beograd",
            300,
            "jnportal",
            REGION_A,
            p2,
            -2,
            "2026-08-20",
            "x",
        ),
    }
    ids = {k: uuid.uuid4() for k in tenders}
    foreign = uuid.uuid4()
    async with eng.begin() as conn:
        for tid in (ta, tb):
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) VALUES (:i,'RS','lite','trial')"
                ),
                {"i": tid},
            )
        for pid, tid, name in (
            (p1, ta, "Šablon 1"),
            (p2, ta, "Šablon 2"),
            (pb, tb, "Tuđi"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active) VALUES (:i,:t,:n,true)"
                ),
                {"i": pid, "t": tid, "n": name},
            )
        rows = [(ids[k], *v) for k, v in tenders.items()] + [
            (
                foreign,
                "Izgradnja škola tuđa",
                "Zavod Beograd",
                100,
                "jnportal",
                REGION_A,
                pb,
                10,
                "2026-09-01",
                "x",
            )
        ]
        for n, (tid, title, cust, amt, src, reg, pid, dd, pub, desc) in enumerate(rows):
            await conn.execute(
                text(
                    "INSERT INTO tenders_normalized (id, title, title_normalized, description,"
                    " description_normalized, customer_name, amount_eur, source_key, regions,"
                    " deadline_at, published_at, source_url)"
                    " VALUES (:id,:title,:tn,:d,:dn,:c,:a,:s,CAST(:r AS text[]),:dl,:pub,'https://x')"
                ),
                {
                    "id": tid,
                    "title": title,
                    "tn": normalize_text(title),
                    "d": desc,
                    "dn": normalize_text(desc),
                    "c": cust,
                    "a": amt,
                    "s": src,
                    "r": [reg],
                    "dl": now + timedelta(days=dd),
                    "pub": datetime.fromisoformat(pub).replace(tzinfo=timezone.utc),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO delivery_outbox (profile_id, tender_id, channel, status, sent_at)"
                    " VALUES (:p,:t,'email','sent',:s)"
                ),
                {"p": pid, "t": tid, "s": now - timedelta(hours=n)},
            )
    yield {
        "tenant": str(ta),
        "p1": str(p1),
        "p2": str(p2),
        "ids": {k: str(v) for k, v in ids.items()},
        "foreign": str(foreign),
    }
    async with eng.begin() as conn:
        await conn.execute(
            text("DELETE FROM delivery_outbox WHERE profile_id = ANY(:p)"),
            {"p": [p1, p2, pb]},
        )
        await conn.execute(
            text("DELETE FROM tenders_normalized WHERE id = ANY(:i)"),
            {"i": [*ids.values(), foreign]},
        )
        await conn.execute(
            text("DELETE FROM profiles WHERE id = ANY(:p)"), {"p": [p1, p2, pb]}
        )
        await conn.execute(
            text("DELETE FROM tenants WHERE id = ANY(:t)"), {"t": [ta, tb]}
        )
    await eng.dispose()


def _f(fx, **query):
    return parse_filters(
        query,
        source_keys={"jnportal": "UJN", "other": "Other"},
        sort_keys=feed_service.FEED_SORT_KEYS,
        default_sort="date",
        profile_ids=frozenset({fx["p1"], fx["p2"]}),
        allow_deadline_to=True,
    )


async def _run(sm, fx, **query):
    """→ (the ids of the page by the keys of the fixture in the order of the output, total, timed_out)."""
    rev = {v: k for k, v in fx["ids"].items()}
    async with sm() as db:
        items, total, timed_out = await feed_service.get_feed_listing(
            db, fx["tenant"], _f(fx, **query)
        )
    return [rev[i.tender_id] for i in items], total, timed_out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({}, {"school", "road", "it"}),  # current: «old» has expired
        ({"actual": "0"}, {"school", "road", "it", "old"}),
        ({"q": "skola", "actual": "0"}, {"school", "old"}),  # fold: «škola» → skola
        ({"q": "школа", "actual": "0"}, {"school", "old"}),  # Cyrillic → the same token
        ({"q": "laptop"}, {"it"}),  # by the description
        ({"q": "nis"}, {"road"}),  # by the customer (Opština Niš, fold)
        ({"customer": "zavod"}, {"school", "it"}),
        ({"region": REGION_B}, {"road"}),
        ({"source": "other"}, {"it"}),
        ({"min_amount": "1000"}, {"road"}),
        ({"max_amount": "200"}, {"school"}),
        ({"max_amount": "200", "no_amount": "1"}, {"school", "it"}),
        ({"published_from": "2026-09-05"}, {"road", "it"}),
        ({"published_to": "2026-09-01"}, {"school"}),
        ({"deadline_to": (date.today() + timedelta(days=5)).isoformat()}, {"road"}),
        ({"profile_id": "P1"}, {"school", "road"}),
        ({"profile_id": "P2"}, {"it"}),
        ({"q": "skola", "region": REGION_B}, set()),  # AND between the filters
    ],
)
async def test_each_filter_narrows_to_expected_set(sessionmaker_, fx, query, expected):
    query = {k: (fx[v.lower()] if k == "profile_id" else v) for k, v in query.items()}
    keys, total, timed_out = await _run(sessionmaker_, fx, **query)
    assert set(keys) == expected
    assert total == len(keys)  # the counter “Found” = the size of the set
    assert not timed_out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        (
            "date",
            ["school", "road", "it", "old"],
        ),  # sent_at DESC (n=0..3 → the fresh one first)
        ("deadline", ["old", "road", "school", "it"]),
        ("amount_desc", ["road", "old", "school", "it"]),  # NULL — last
        ("amount_asc", ["school", "old", "road", "it"]),  # NULL — last
        ("profile", ["school", "road", "it", "old"]),  # «Šablon 1» < «Šablon 2»
    ],
)
async def test_sort_orders(sessionmaker_, fx, sort, expected):
    keys, _, _ = await _run(sessionmaker_, fx, sort=sort, actual="0")
    assert keys == expected


@pytest.mark.asyncio
async def test_other_tenant_never_visible_even_with_matching_filters(sessionmaker_, fx):
    keys, total, _ = await _run(sessionmaker_, fx, q="skola", actual="0")
    assert fx["foreign"] not in {fx["ids"][k] for k in keys}
    assert (
        total == 2
    )  # school + old; «Izgradnja škola tuđa» of a foreign tenant is not counted


@pytest.mark.asyncio
async def test_foreign_profile_id_is_dropped_by_parser(fx):
    f = parse_filters(
        {"profile_id": str(uuid.uuid4())},
        profile_ids=frozenset({fx["p1"]}),
        sort_keys=feed_service.FEED_SORT_KEYS,
        default_sort="date",
    )
    assert f.profile_id == ""


@pytest.mark.asyncio
async def test_timeout_returns_flag_and_session_stays_usable(
    sessionmaker_, fx, monkeypatch
):
    monkeypatch.setattr(feed_service, "FEED_STATEMENT_TIMEOUT_MS", 1)
    async with sessionmaker_() as db:
        # pg_sleep inside the predicate guarantees exceeding 1 ms.
        monkeypatch.setattr(
            feed_service,
            "feed_filter_sql",
            lambda f: ("\n          AND pg_sleep(0.2) IS NOT NULL", {}),
        )
        items, total, timed_out = await feed_service.get_feed_listing(
            db, fx["tenant"], _f(fx)
        )
        assert (items, total, timed_out) == ([], 0, True)
        # After the rollback the session works (otherwise InFailedSQLTransactionError).
        await db.execute(text("SELECT 1"))
