"""«Tržište» on a real PG: the common figures and the personal line (market_service).

Requires ``TEST_RLS_DATABASE_URL`` (a one-off PG; the gate brings it up itself). We read under
the application role. The common figures go by “today” 2031-03-15 — in the window there are no foreign
rows of the common test DB; the personal line — on CPV ``9999*``, which does not exist in the
real data.

What we prove by literals:
  1. “Yesterday” — by the publication date in Europe/Belgrade (23:30 UTC = 00:30 in Belgrade is already the NEXT day), by countries.
  2. The top 3 of the week — by decreasing sum, a tender without a sum does not get in.
  3. Decisions: the consortium = one decision; a reversal, a decision older than the window and a decision “from the future” (the production contains decision_date 2027-06-30) are not included.
  4. Personally: a foreign template (another tenant) and an inactive template do not affect the count; one tender of two templates is counted once.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.api import repo
from src.api.cabinet import market_service
from src.domain.tz import SERBIA_TZ

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — SQL «Tržište» пропускается",
)

TODAY = date(2031, 3, 15)


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


# name: (country, published_utc, amount_eur, cpv)
SHARED = {
    "rs1": ("RS", "2031-03-14T10:00", 500.0, "99990001"),
    "rs2": ("RS", "2031-03-14T11:00", 9000.0, "99990002"),
    "mk1": ("MK", "2031-03-14T12:00", 700.0, "99990003"),
    # 23:30 UTC on the 13th = 00:30 Belgrade on the 14th → “yesterday”
    "rs3": ("RS", "2031-03-13T23:30", None, "99990004"),
    # 22:30 UTC on the 14th = 23:30 Belgrade on the 14th → “yesterday”
    "rs4": ("RS", "2031-03-14T22:30", 100.0, "99990005"),
    # 23:30 UTC on the 14th = 00:30 Belgrade on the 15th → NOT yesterday
    "rs5": ("BA", "2031-03-14T23:30", 50000.0, "99990006"),
    # published 2 years before we saw it → the archive
    "arch": ("RS", "2029-01-01T10:00", 99999.0, "99990007"),
    # live, but published before 02.06.2026 (the tail of the initial load)
    "pre": ("RS", "2026-05-30T10:00", None, "99990008"),
}

# (tender, source_uid, lot, decision_date, rsd, customer, IsReversed, pib, winner,
#  contracts_rsd)
# The unit is a DECISION (tender + winner), not a lot. rs2: 5 lots, 3 winners,
# PIB 333 has one contract for 5000 over three lots (the lot sum NULL, the contract sum
# lies in raw.winner_contracts_rsd) → 3 decisions, not 5 rows.
AWARDS = (
    (
        "rs1",
        "m-aw1",
        None,
        "2031-03-14",
        "1000.00",
        "Opština A",
        False,
        "111",
        "W1",
        None,
    ),
    ("rs1", "m-aw2", None, "2031-03-10", "250.00", "Grad B", False, "222", "W2", None),
    ("rs1", "m-aw3", None, "2031-03-12", None, "Škola C", False, "444", "W4", None),
    (
        "rs2",
        "m-lot1",
        "1",
        "2031-03-15",
        None,
        "Dom zdravlja",
        False,
        "333",
        "W3",
        "5000",
    ),
    (
        "rs2",
        "m-lot2",
        "2",
        "2031-03-15",
        None,
        "Dom zdravlja",
        False,
        "333",
        "W3",
        "5000",
    ),
    (
        "rs2",
        "m-lot3",
        "3",
        "2031-03-15",
        None,
        "Dom zdravlja",
        False,
        "333",
        "W3",
        "5000",
    ),
    (
        "rs2",
        "m-lot4",
        "4",
        "2031-03-14",
        "700.00",
        "Dom zdravlja",
        False,
        "555",
        "W5",
        None,
    ),
    (
        "rs2",
        "m-lot5",
        "5",
        "2031-03-14",
        None,
        "Dom zdravlja",
        False,
        "666",
        "W6",
        None,
    ),
    ("rs2", "m-rev", None, "2031-03-14", "777.00", "Storno", True, "777", "W7", None),
    ("rs2", "m-old", None, "2031-03-08", "9999.00", "Staro", False, "888", "W8", None),
    (
        "rs2",
        "m-future",
        None,
        "2031-04-01",
        "8888.00",
        "Budućnost",
        False,
        "999",
        "W9",
        None,
    ),
    # a tender before the start of the regular collection (REGULAR_SINCE) — outside the /statistika population
    (
        "pre",
        "m-pre",
        None,
        "2031-03-14",
        "4444.00",
        "Do juna",
        False,
        "121",
        "W10",
        None,
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
    ids = {k: uuid.uuid4() for k in SHARED}
    ta, tb = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    try:
        async with sup.begin() as c:
            for name, (cc, pub, eur, cpv) in SHARED.items():
                seen = now if name == "arch" else _utc(pub)
                await c.execute(
                    text(
                        "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                        "country_code, title, customer_name, amount_eur, published_at, "
                        "first_seen_at, classifications) VALUES (:id, :sk, :uid, :cc, "
                        ":title, 'Zavod', :eur, :pub, :seen, CAST(:cls AS jsonb))"
                    ),
                    {
                        "id": ids[name],
                        "sk": f"mkt-{sfx}",
                        "uid": f"mkt-{sfx}-{name}",
                        "cc": cc,
                        "title": f"T-{name}",
                        "eur": eur,
                        "pub": _utc(pub),
                        "seen": seen,
                        "cls": json.dumps([{"scheme": "CPV", "code": cpv}]),
                    },
                )
            for tn, uid, lot, dd, rsd, cust, rev, pib, wn, wc in AWARDS:
                await c.execute(
                    text(
                        "INSERT INTO tender_awards (tender_id, source_uid, lot_number, "
                        "document_type_id, customer_name, decision_date, "
                        "award_amount_rsd, winner_pib, winner_name, raw) VALUES (:t, "
                        ":uid, :lot, 1, :cust, CAST(:d AS date), "
                        "CAST(:rsd AS numeric), :pib, :wn, CAST(:raw AS jsonb))"
                    ),
                    {
                        "t": ids[tn],
                        "uid": f"{uid}-{sfx}",
                        "lot": lot,
                        "cust": cust,
                        "d": date.fromisoformat(dd),
                        "rsd": Decimal(rsd) if rsd else None,
                        "pib": pib,
                        "wn": wn,
                        "raw": json.dumps(
                            {
                                "contract_row": {"IsReversed": rev},
                                **({"winner_contracts_rsd": wc} if wc else {}),
                            }
                        ),
                    },
                )
            for tid in (ta, tb):
                await c.execute(
                    text(
                        "INSERT INTO tenants (id, country_code, tier, status) "
                        "VALUES (:i, 'RS', 'lite', 'trial')"
                    ),
                    {"i": tid},
                )
        yield {"ids": ids, "ta": ta, "tb": tb, "sfx": sfx, "sup": sup}
    finally:
        async with sup.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE source_key = :sk"),
                {"sk": f"mkt-{sfx}"},
            )
            await c.execute(
                text(
                    "DELETE FROM profiles WHERE tenant_id IN "
                    "(CAST(:a AS uuid), CAST(:b AS uuid))"
                ),
                {"a": ta, "b": tb},
            )
            await c.execute(
                text(
                    "DELETE FROM tenants WHERE id IN "
                    "(CAST(:a AS uuid), CAST(:b AS uuid))"
                ),
                {"a": ta, "b": tb},
            )
        await sup.dispose()


@pytest_asyncio.fixture
async def app_session():
    engine = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    async with AsyncSession(engine) as s:
        yield s
    await engine.dispose()


async def test_shared_numbers_are_literal(world, app_session):
    data = await market_service.compute_shared(app_session, TODAY)

    pub = data["published"]
    assert pub["date"] == "2031-03-14"
    # rs1, rs2, rs3 (00:30 Belgrade), rs4 (23:30 Belgrade) = RS 4; mk1 = MK 1; rs5 — already the 15th.
    assert pub["by_country"] == {"RS": 4, "BA": 0, "ME": 0, "MK": 1}
    assert pub["total"] == 5

    top = data["top"]["items"]
    # week 08–14.03 Belgrade time: rs2 9000, mk1 700, rs1 500 (rs4 100 — the fourth)
    assert [i["title"] for i in top] == ["T-rs2", "T-mk1", "T-rs1"]
    assert [i["amount_eur"] for i in top] == [9000.0, 700.0, 500.0]

    aw = data["awards"]
    # window (08.03, 15.03]: DECISIONS per winner, not lot rows (9 rows in the window):
    # W1 1000, W2 250, W4 (no sum), W3 5000 (3 lots, one contract), W5 700,
    # W6 (no sum) = 6 decisions, the sum is known for 4. The reversal, 08.03, 01.04 and the tender
    # before 02.06 are not included.
    assert aw["count"] == 6
    assert aw["known"] == 4
    assert aw["total_rsd"] == 6950.0
    assert aw["top"]["amount_rsd"] == 5000.0
    assert aw["top"]["customer"] == "Dom zdravlja"


async def _add_profile(sup, tid, name, include, active=True):
    async with sup.begin() as c:
        await c.execute(
            text(
                "INSERT INTO profiles (id, tenant_id, name, is_active, country_code, "
                "classifications) VALUES (:i, CAST(:t AS uuid), :n, :a, 'RS', "
                "CAST(:cls AS jsonb))"
            ),
            {
                "i": uuid.uuid4(),
                "t": tid,
                "n": name,
                "a": active,
                "cls": json.dumps({"include": include}),
            },
        )


async def test_personal_counts_only_own_active_cpv(world, app_session):
    sup, ta, tb = world["sup"], world["ta"], world["tb"]
    now = datetime.now(timezone.utc)
    rows = (
        ("n1", now - timedelta(days=2), now + timedelta(days=30), "99991100"),
        ("n2", now - timedelta(days=1), now + timedelta(days=3), "99991200"),
        ("old", now - timedelta(days=20), now + timedelta(days=4), "99991300"),
        ("other", now - timedelta(days=1), now + timedelta(days=2), "99770000"),
    )
    async with sup.begin() as c:
        for name, pub, dl, cpv in rows:
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, title, published_at, first_seen_at, deadline_at, "
                    "classifications) VALUES (:id, :sk, :uid, 'RS', 'T', :pub, :pub, "
                    ":dl, CAST(:cls AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "sk": f"mkt-{world['sfx']}",
                    "uid": f"mkt-{world['sfx']}-p-{name}",
                    "pub": pub,
                    "dl": dl,
                    "cls": json.dumps([{"scheme": "CPV", "code": cpv}]),
                },
            )
    # Tenant A: two templates, both cover n1 (the intersection) + an inactive one on 9977.
    await _add_profile(sup, ta, "A1", [{"system_code": "eu_cpv", "code": "99990000"}])
    await _add_profile(sup, ta, "A2", [{"system_code": "eu_cpv", "code": "9999110*"}])
    await _add_profile(
        sup, ta, "A-off", [{"system_code": "eu_cpv", "code": "99770000"}], active=False
    )
    # Tenant B: a foreign template on 9977 — A does not see it.
    await _add_profile(sup, tb, "B1", [{"system_code": "eu_cpv", "code": "99770000"}])

    await repo.set_tenant(app_session, ta)
    # A1: «99990000» → prefix «9999*» → n1, n2, old; A2: «9999110*» → n1 (a repeat).
    # New in 7 days: n1, n2 (old — 20 days ago) = 2.
    # Expiring ≤7 days: n2 (3 days), old (4 days); n1 — in 30 = 2.
    got = await market_service.get_personal(app_session, ta)
    assert got == {"new": 2, "expiring": 2}

    await app_session.rollback()
    await repo.set_tenant(app_session, tb)
    # B1: «99770000» → «9977*» → only «other»: new and expiring in 2 days.
    got_b = await market_service.get_personal(app_session, tb)
    assert got_b == {"new": 1, "expiring": 1}


async def test_personal_without_cpv_is_none(world, app_session):
    await _add_profile(world["sup"], world["ta"], "kw", [])
    await repo.set_tenant(app_session, world["ta"])
    assert await market_service.get_personal(app_session, world["ta"]) is None


async def test_expiring_window_is_calendar_days_in_belgrade(world, app_session):
    """ "Expires ≤7 days" — calendar days in Belgrade time (#824), not 7×24 h.

    The deadline is set to noon of the Belgrade calendar day "today + N", so the difference of calendar dates is exactly N regardless of the launch hour. N=7 — included,
    N=8 — not (the margin of the index ``< now() + 9 days`` must not cut the 7-day one).
    Day boundaries: 23:59 "today+7" — included (the last minute of the window), 00:01
    "today+8" — not (the first minute outside the window).
    """
    sup, ta = world["sup"], world["ta"]
    local_today = datetime.now(SERBIA_TZ).date()

    def belgrade_at(days: int, hour: int, minute: int) -> datetime:
        d = local_today + timedelta(days=days)
        return datetime(
            d.year, d.month, d.day, hour, minute, tzinfo=SERBIA_TZ
        ).astimezone(timezone.utc)

    pub = datetime.now(timezone.utc) - timedelta(days=60)
    rows = (
        ("seven", belgrade_at(7, 12, 0)),
        ("seven_last_minute", belgrade_at(7, 23, 59)),
        ("eight", belgrade_at(8, 12, 0)),
        ("eight_first_minute", belgrade_at(8, 0, 1)),
    )
    async with sup.begin() as c:
        for name, dl in rows:
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, title, published_at, first_seen_at, deadline_at, "
                    "classifications) VALUES (:id, :sk, :uid, 'RS', 'T', :pub, :pub, "
                    ":dl, CAST(:cls AS jsonb))"
                ),
                {
                    "id": uuid.uuid4(),
                    "sk": f"mkt-{world['sfx']}",
                    "uid": f"mkt-{world['sfx']}-w-{name}",
                    "pub": pub,
                    "dl": dl,
                    "cls": json.dumps([{"scheme": "CPV", "code": "99993300"}]),
                },
            )
    await _add_profile(sup, ta, "W", [{"system_code": "eu_cpv", "code": "99993300"}])
    await repo.set_tenant(app_session, ta)
    got = await market_service.get_personal(app_session, ta)
    # published 60 days ago → no "new" ones; in the window fall +7 days (noon and 23:59),
    # +8 days (noon and 00:01) — no
    assert got == {"new": 0, "expiring": 2}
