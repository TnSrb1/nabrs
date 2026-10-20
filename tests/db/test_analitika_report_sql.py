"""Integration: the report constructor «Analitika» (R4) on a LIVE PG.

The mocks do not execute the predicate “live tender”, ``percentile_cont``, nor the exclusion of canceled decisions — here the real SQL of ``report_builder``
against a throwaway PostgreSQL under the application role (``tendersrb_app``).

The fixture (the suffix ``sfx`` in the customer names isolates the rows from the rest of the
DB; each report is filtered by ``customer=sfx``):

    tenders   customer        published     EUR     CPV        district
    T1        Opština Alfa    2026-06-10    100     45210000   RS-00
    T2        Opština  Alfa␠  2026-06-20    NULL    45100000   RS-00   (double space)
    T3        Bolnica Beta    2026-07-05    300     33100000   RS-06
    T4        Opština Alfa    2019-05-01    999999  45210000   RS-00   ARCHIVE (first_seen 2026)
    T5        Opština Alfa    NULL          5555    45210000   RS-00   no publication date

    decisions tender  winner      date         EUR      Cpv
    A1        T1      W1          2026-06-15   1000     “45210000 - Zgrade”
    A2        T1      W1          2026-06-25   NULL     “45100000 - Radovi”
    A3        T3      W2          2026-07-10   500      “33100000 - Oprema”
    A4        T3      W2          2026-07-11   777777   IsReversed = true  (canceled)
    C (lot 1) T3      W2          2026-08-05   200      consortium: one decision,
              T3      W3          2026-08-05   NULL     two rows (the sum is on the first)

The expectations — LITERALS, counted by hand from the table above.
Requires ``TEST_RLS_DATABASE_URL`` (a throwaway PG, NEVER production).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.analytics import report_builder as rb
from src.api.cabinet import analitika_service as svc

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция конструктора отчётов пропускается",
)


def _ts(d: str | None) -> datetime | None:
    return datetime.fromisoformat(d + "T10:00:00+00:00") if d else None


@pytest_asyncio.fixture
async def world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:10]
    alfa = f"Opština Alfa {sfx}"
    tenders = {
        "T1": (alfa, "2026-06-10", "2026-06-10", Decimal("100"), "45210000", "RS-00"),
        "T2": (
            f"Opština  Alfa {sfx} ",
            "2026-06-20",
            "2026-06-20",
            None,
            "45100000",
            "RS-00",
        ),
        "T3": (
            f"Bolnica Beta {sfx}",
            "2026-07-05",
            "2026-07-05",
            Decimal("300"),
            "33100000",
            "RS-06",
        ),
        "T4": (
            alfa,
            "2019-05-01",
            "2026-06-03",
            Decimal("999999"),
            "45210000",
            "RS-00",
        ),
        "T5": (alfa, None, "2026-06-03", Decimal("5555"), "45210000", "RS-00"),
    }
    ids = {k: uuid.uuid4() for k in tenders}
    w1, w2, w3 = f"W1-{sfx}", f"W2-{sfx}", f"W3-{sfx}"
    beta = f"Bolnica Beta {sfx}"
    # (uid, lot, tender, customer, the winner’s PIB, name, date, EUR, Cpv, canceled)
    awards = [
        ("a1", None, "T1", alfa, w1, "Firma Jedan", "2026-06-15", Decimal("1000"), "45210000 - Zgrade", False),
        ("a2", None, "T1", alfa, w1, "Firma Jedan", "2026-06-25", None, "45100000 - Radovi", False),
        ("a3", None, "T3", beta, w2, "Firma Dva", "2026-07-10", Decimal("500"), "33100000 - Oprema", False),
        ("a4", None, "T3", beta, w2, "Firma Dva", "2026-07-11", Decimal("777777"), "33100000 - Oprema", True),
        # Consortium: ONE decision (uid cons, lot 1) in two rows, the sum only on the first.
        ("cons", "1", "T3", beta, w2, "Firma Dva", "2026-08-05", Decimal("200"), "33100000 - Oprema", False),
        ("cons", "1", "T3", beta, w3, "Firma Tri", "2026-08-05", None, "33100000 - Oprema", False),
    ]  # fmt: skip
    async with eng.begin() as c:
        for k, (cust, pub, seen, amt, cpv, reg) in tenders.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, customer_name, published_at, "
                    "first_seen_at, amount_eur, classifications, "
                    "customer_region_code, regions, procedure_type) "
                    "VALUES (:id, 'jnportal', :uid, 'RS', 'active', :cust, :pub, "
                    ":seen, :amt, CAST(:cls AS jsonb), :reg, ARRAY[CAST(:reg AS text)], "
                    "'open')"
                ),
                {
                    "id": ids[k],
                    "uid": f"an-{sfx}-{k}",
                    "cust": cust,
                    "pub": _ts(pub),
                    "seen": _ts(seen),
                    "amt": amt,
                    # As the jnportal ingest writes: CPV + a record of the planned period without a code.
                    "cls": json.dumps(
                        [
                            {"scheme": "PLAN", "label": "2026"},
                            {"scheme": "CPV", "code": cpv, "label": "x"},
                        ]
                    ),
                    "reg": reg,
                },
            )
        for uid, lot, tk, cust, wpib, wname, dd, amt, cpv, rev in awards:
            raw = {"contract_row": {"Cpv": cpv, "IsReversed": rev}}
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, document_type_id, "
                    "lot_number, customer_name, winner_pib, winner_name, decision_date, "
                    "award_amount_eur, raw) VALUES (:t, :uid, 11, :lot, :cust, :wp, :wn, "
                    "CAST(:d AS date), :amt, CAST(:raw AS jsonb))"
                ),
                {
                    "t": ids[tk],
                    "uid": f"an-{sfx}-{uid}",
                    "lot": lot,
                    "cust": cust,
                    "wp": wpib,
                    "wn": wname,
                    "d": date.fromisoformat(dd),
                    "amt": amt,
                    "raw": json.dumps(raw),
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    tenants: list[uuid.UUID] = []
    try:
        yield sfx, app_eng, eng, tenants
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
            if tenants:
                await c.execute(
                    text("DELETE FROM profiles WHERE tenant_id = ANY(:t)"),
                    {"t": tenants},
                )
                await c.execute(
                    text("DELETE FROM tenants WHERE id = ANY(:t)"), {"t": tenants}
                )
        await app_eng.dispose()
        await eng.dispose()


async def _run(app_eng, **params) -> rb.ReportResult:
    spec = rb.parse_spec(params)
    async with AsyncSession(app_eng) as s:
        return await rb.run_report(s, spec, limit=50, with_coverage=True)


def _by_key(res: rb.ReportResult) -> dict:
    return {r.g1_key: r for r in res.rows}


# --------------------------------------------------------------------------- #
#  Tenders
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tenders_by_customer_excludes_archive_and_keeps_null_amount(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="tenders", g1="customer", m="count", customer=sfx)

    # Two customers: Alfa (T1+T2 — the double space collapsed into one key) and Beta.
    # The archive T4 and T5 without a publication date are NOT counted.
    assert [r.g1_key for r in res.rows] == [
        f"opština alfa {sfx}",
        f"bolnica beta {sfx}",
    ]
    alfa = res.rows[0]
    assert alfa.n == 2
    assert alfa.with_amount == 1
    assert alfa.sum_eur == Decimal("100")
    # NULL is not zero: the average 100, not 50.
    assert alfa.avg_eur == Decimal("100")
    assert alfa.median_eur == 100.0
    assert res.total_groups == 2

    t = res.totals
    assert (t.n, t.with_amount, t.sum_eur) == (3, 2, Decimal("400"))
    assert t.avg_eur == Decimal("200")
    assert t.median_eur == 200.0
    assert t.distinct_customers == 2
    assert (t.first_date, t.last_date) == (date(2026, 6, 10), date(2026, 7, 5))


@pytest.mark.asyncio
async def test_tenders_by_month_is_chronological(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="tenders", g1="month", m="count", customer=sfx)
    assert [(r.g1_key, r.n) for r in res.rows] == [("2026-06", 2), ("2026-07", 1)]


@pytest.mark.asyncio
async def test_tenders_cpv_division_and_filters(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="tenders", g1="cpv2", customer=sfx)
    assert {r.g1_key: r.n for r in res.rows} == {"45": 2, "33": 1}

    res = await _run(app_eng, subject="tenders", g1="customer", customer=sfx, cpv="451")
    assert res.totals.n == 1

    res = await _run(
        app_eng, subject="tenders", g1="region", customer=sfx, region="RS-06"
    )
    assert [(r.g1_key, r.n) for r in res.rows] == [("RS-06", 1)]

    # The sum filter excludes a record without a sum (T2) and Alfa-100.
    res = await _run(
        app_eng, subject="tenders", g1="customer", customer=sfx, amin="150"
    )
    assert [(r.g1_key, r.n) for r in res.rows] == [(f"bolnica beta {sfx}", 1)]

    res = await _run(
        app_eng, subject="tenders", g1="month", customer=sfx, **{"from": "2026-07-01"}
    )
    assert res.totals.n == 1


@pytest.mark.asyncio
async def test_customer_filter_is_folded_latin(world):
    """Latin input without diacritics finds «Opština» (the fold)."""
    sfx, app_eng, *_ = world
    res = await _run(
        app_eng, subject="tenders", g1="customer", customer=f"opstina alfa {sfx}"
    )
    assert res.totals.n == 2


# --------------------------------------------------------------------------- #
#  Award decisions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_awards_by_winner_excludes_reversed(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="awards", g1="winner", m="sum", customer=sfx)
    rows = _by_key(res)
    # By sum: 1000 > 700 > NULL (the second member of the consortium has no sum).
    assert list(rows) == [f"W1-{sfx}", f"W2-{sfx}", f"W3-{sfx}"]
    w1, w2, w3 = rows[f"W1-{sfx}"], rows[f"W2-{sfx}"], rows[f"W3-{sfx}"]
    assert (w1.n, w1.with_amount, w1.sum_eur) == (2, 1, Decimal("1000"))
    # The canceled contract of 777 777 is not counted; the consortium — in the count of W2.
    assert (w2.n, w2.with_amount, w2.sum_eur) == (2, 2, Decimal("700"))
    # A member of the consortium won a decision — it is in its count, it has no sum.
    assert (w3.n, w3.with_amount, w3.sum_eur) == (1, 0, None)
    assert w1.g1_label == f"Firma Jedan · PIB W1-{sfx}"
    t = res.totals
    # 4 decisions (5 rows: the consortium is one decision), the sum without double counting.
    assert (t.n, t.with_amount, t.sum_eur) == (4, 3, Decimal("1700"))
    assert (t.distinct_winners, t.distinct_customers) == (3, 2)


@pytest.mark.asyncio
async def test_awards_consortium_counts_one_decision(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="awards", g1="customer", m="count", customer=sfx)
    got = {r.g1_key: (r.n, r.sum_eur, r.distinct_winners) for r in res.rows}
    assert got == {
        f"opština alfa {sfx}": (2, Decimal("1000"), 1),
        # a3 + consortium = 2 decisions (not 3 rows), the sum 500 + 200.
        f"bolnica beta {sfx}": (2, Decimal("700"), 2),
    }


@pytest.mark.asyncio
async def test_awards_cpv_group_from_contract_row(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="awards", g1="cpv3", customer=sfx)
    assert {r.g1_key: r.n for r in res.rows} == {"452": 1, "451": 1, "331": 2}

    res = await _run(app_eng, subject="awards", g1="winner", customer=sfx, winner="W2-")
    assert [r.g1_key for r in res.rows] == [f"W2-{sfx}"]


@pytest.mark.asyncio
async def test_awards_region_via_tender_join(world):
    sfx, app_eng, *_ = world
    res = await _run(app_eng, subject="awards", g1="region", customer=sfx)
    assert {r.g1_key: r.n for r in res.rows} == {"RS-00": 2, "RS-06": 2}


# --------------------------------------------------------------------------- #
#  Service: the gate under the application role + “as in my template”
# --------------------------------------------------------------------------- #


async def _tenant(super_eng, tenants, *, tier, status, access_until=None, cpv=None):
    tid = uuid.uuid4()
    tenants.append(tid)
    async with super_eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenants (id, country_code, tier, status, access_until) "
                "VALUES (:i, 'RS', :tier, :st, :au)"
            ),
            {"i": tid, "tier": tier, "st": status, "au": access_until},
        )
        pid = uuid.uuid4()
        if cpv is not None:
            await c.execute(
                text(
                    "INSERT INTO profiles (id, tenant_id, name, is_active, "
                    "classifications) VALUES (:i, :t, 'Šablon', true, "
                    "CAST(:cls AS jsonb))"
                ),
                {
                    "i": pid,
                    "t": tid,
                    "cls": json.dumps([{"system_code": "eu_cpv", "code": cpv}]),
                },
            )
    return str(tid), str(pid)


@pytest.mark.asyncio
async def test_gate_trial_allowed_with_profile_filter(world):
    sfx, app_eng, super_eng, tenants = world
    tid, pid = await _tenant(
        super_eng, tenants, tier="lite", status="trial", cpv="33000000"
    )
    async with AsyncSession(app_eng) as s:
        page = await svc.get_analitika_page(
            s,
            tid=tid,
            params={
                "subject": "tenders",
                "g1": "customer",
                "customer": sfx,
                "profile": pid,
            },
            lang="sr",
        )
    assert page.status == "success"
    assert page.spec.profile_cpv == ("33",)
    # Only Bolnica Beta (CPV 33…) — the tenant’s template is applied.
    assert [(r.g1, r.n) for r in page.rows] == [(f"Bolnica Beta {sfx}", 1)]


@pytest.mark.asyncio
async def test_gate_pro_denied_business_allowed_frozen_trial_denied(world):
    _, app_eng, super_eng, tenants = world
    pro, _ = await _tenant(super_eng, tenants, tier="pro", status="active")
    biz, _ = await _tenant(super_eng, tenants, tier="business", status="active")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    frozen, _ = await _tenant(
        super_eng, tenants, tier="lite", status="trial", access_until=past
    )
    out = {}
    for name, tid in (("pro", pro), ("biz", biz), ("frozen", frozen)):
        async with AsyncSession(app_eng) as s:
            out[name] = (
                await svc.get_analitika_page(s, tid=tid, params={}, lang="sr")
            ).status
    assert out == {"pro": "gate_business", "biz": "success", "frozen": "frozen"}


@pytest.mark.asyncio
async def test_template_filter_all_cpv_and_region_mirror_matching():
    """“As in my template”: CPV — by ANY code of the tender; districts — as in the matching.

    tender  CPV (in order)          districts  country
    owner      45210000, 33100000      RS-06     RS     → yes (the second code 33)
    partner      33600000                —         RS     → no (the district is not resolved)
    D3      33100000                —         BA     → yes (RS-xx does not cut BA)
    D4      45000000                RS-06     RS     → no (there is no CPV 33)
    """
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    s2 = "dlt" + uuid.uuid4().hex[:10]
    rows = {
        "owner": (["45210000", "33100000"], ["RS-06"], "RS"),
        "partner": (["33600000"], [], "RS"),
        "D3": (["33100000"], [], "BA"),
        "D4": (["45000000"], ["RS-06"], "RS"),
    }
    ids = {k: uuid.uuid4() for k in rows}
    async with eng.begin() as c:
        for k, (codes, regs, cc) in rows.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, customer_name, published_at, "
                    "first_seen_at, classifications, customer_region_code, regions) "
                    "VALUES (:id, 'jnportal', :uid, :cc, 'active', :cust, :ts, :ts, "
                    "CAST(:cls AS jsonb), :reg, CAST(:regs AS text[]))"
                ),
                {
                    "id": ids[k],
                    "uid": f"an-{s2}-{k}",
                    "cc": cc,
                    "cust": f"Delta {s2}",
                    "ts": _ts("2026-08-01"),
                    "cls": json.dumps(
                        [{"scheme": "CPV", "code": x, "label": "x"} for x in codes]
                    ),
                    "reg": regs[0] if regs else None,
                    "regs": regs,
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        base = rb.parse_spec({"subject": "tenders", "g1": "cpv2", "customer": s2})
        tpl = replace(base, profile_cpv=("33",), profile_regions=("RS-06",))
        async with AsyncSession(app_eng) as s:
            res = await rb.run_report(s, tpl, limit=50)
        assert res.totals.n == 2
        # Grouping — by the FIRST code: owner in section 45, D3 in 33.
        assert {r.g1_key: r.n for r in res.rows} == {"45": 1, "33": 1}

        # A user CPV prefix — also by any code: owner (the second) + D3.
        user = rb.parse_spec({"subject": "tenders", "customer": s2, "cpv": "331"})
        async with AsyncSession(app_eng) as s:
            res = await rb.run_report(s, user, limit=50)
        assert res.totals.n == 2
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
        await app_eng.dispose()
        await eng.dispose()
