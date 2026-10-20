"""Integration: the «Analitika» constructor, phase 2 (the «ističu» preset, the keywords of the template).

A live PG under the application role. The contract dates are set as OFFSETS from
``CURRENT_DATE`` in the DB itself (not from the test’s clock) — the test does not depend on the day of the run.

Decisions (the customers carry the suffix ``sfx``; ``contract_date`` / ``duration_months``):

    uid   customer  contract        term  end             Cpv       expectation at exp=12
    e1    Alfa      today-6 mo      12   today+6 mo      45210000  IN
    e9    Alfa      today-12 mo     12   today           45210000  IN (boundary >=)
    e10   Alfa      today           12   today+12 mo     45100000  IN (boundary <=)
    e3    Alfa      today-24 mo     12   today-12 mo    45210000  no (already expired)
    e5    Alfa      today           NULL no end          45210000  no
    e8    Alfa      today           13   today+13 mo    45210000  no (outside the window)
    e2    Beta      today-1 mo      6    today+5 mo     33100000  IN
    e4    Beta      today-1 mo      24   today+23 mo    33100000  no
    e6    Beta      today-6 mo      12   today+6 mo     33100000  no (canceled)

Without the «ističu» filter all the non-canceled are counted: Alfa 6, Beta 2.
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.analytics import report_builder as rb

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — интеграция конструктора пропускается",
)

# (uid, customer, the offset of the contract from today in months, the term in months, Cpv, canceled)
_EXPIRY_ROWS = [
    ("e1", "alfa", -6, 12, "45210000 - Zgrade", False),
    ("e9", "alfa", -12, 12, "45210000 - Zgrade", False),
    ("e10", "alfa", 0, 12, "45100000 - Radovi", False),
    ("e3", "alfa", -24, 12, "45210000 - Zgrade", False),
    ("e5", "alfa", 0, None, "45210000 - Zgrade", False),
    ("e8", "alfa", 0, 13, "45210000 - Zgrade", False),
    ("e2", "beta", -1, 6, "33100000 - Oprema", False),
    ("e4", "beta", -1, 24, "33100000 - Oprema", False),
    ("e6", "beta", -6, 12, "33100000 - Oprema", True),
]


@pytest_asyncio.fixture
async def expiry_world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:10]
    names = {"alfa": f"Opština Alfa {sfx}", "beta": f"Bolnica Beta {sfx}"}
    tender_id = uuid.uuid4()
    async with eng.begin() as c:
        await c.execute(
            text(
                "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                "country_code, status, customer_name, published_at, first_seen_at) "
                "VALUES (:id, 'jnportal', :uid, 'RS', 'active', 'x', now(), now())"
            ),
            {"id": tender_id, "uid": f"an2-{sfx}-t"},
        )
        for uid, who, off, dur, cpv, rev in _EXPIRY_ROWS:
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, document_type_id, "
                    "customer_name, winner_pib, winner_name, decision_date, "
                    "contract_date, duration_months, award_amount_eur, raw) "
                    "VALUES (:t, :uid, 11, :cust, 'W', 'Firma', CURRENT_DATE, "
                    "CURRENT_DATE + make_interval(months => CAST(:off AS integer)), "
                    "CAST(:dur AS integer), 100, "
                    "jsonb_build_object('contract_row', jsonb_build_object("
                    "'Cpv', CAST(:cpv AS text), 'IsReversed', CAST(:rev AS boolean))))"
                ),
                {
                    "t": tender_id,
                    "uid": f"an2-{sfx}-{uid}",
                    "cust": names[who],
                    "off": off,
                    "dur": dur,
                    "cpv": cpv,
                    "rev": rev,
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield sfx, app_eng
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = :id"),
                {"id": tender_id},
            )
        await app_eng.dispose()
        await eng.dispose()


async def _run(app_eng, **params) -> rb.ReportResult:
    spec = rb.parse_spec(params)
    async with AsyncSession(app_eng) as s:
        return await rb.run_report(s, spec, limit=50, with_coverage=False)


@pytest.mark.asyncio
async def test_expiring_preset_window_and_boundaries(expiry_world):
    sfx, app_eng = expiry_world
    res = await _run(
        app_eng, subject="awards", g1="customer", m="count", customer=sfx, exp="12"
    )
    got = {r.g1_key: r.n for r in res.rows}
    # Alfa: e1, e9 (end = today), e10 (end = today+12 mo);
    # Beta: e2. The expired e3, no-term e5, outside the window e8/e4, the canceled e6 — no.
    assert got == {f"opština alfa {sfx}": 3, f"bolnica beta {sfx}": 1}
    assert res.totals.n == 4


@pytest.mark.asyncio
async def test_without_expiring_flag_counts_all_unreversed(expiry_world):
    sfx, app_eng = expiry_world
    res = await _run(app_eng, subject="awards", g1="customer", customer=sfx)
    assert {r.g1_key: r.n for r in res.rows} == {
        f"opština alfa {sfx}": 6,
        f"bolnica beta {sfx}": 2,
    }


@pytest.mark.asyncio
async def test_expiring_preset_groups_by_customer_and_cpv(expiry_world):
    sfx, app_eng = expiry_world
    res = await _run(
        app_eng,
        subject="awards",
        g1="customer",
        g2="cpv2",
        m="count",
        customer=sfx,
        exp="12",
    )
    got = {(r.g1_key, r.g2_key): r.n for r in res.rows}
    assert got == {
        (f"opština alfa {sfx}", "45"): 3,
        (f"bolnica beta {sfx}", "33"): 1,
    }


@pytest.mark.asyncio
async def test_expiring_is_ignored_for_tenders_subject():
    # For tenders there is no end: the parameter is discarded, rather than “zeroing” the report.
    assert rb.parse_spec({"subject": "tenders", "exp": "12"}).expiring_months == 0
    # Only the windows from the whitelist.
    assert rb.parse_spec({"subject": "awards", "exp": "7"}).expiring_months == 0
    assert rb.parse_spec({"subject": "awards", "exp": "12"}).expiring_months == 12


@pytest.mark.asyncio
async def test_multiple_cpv_prefixes_any_matches(expiry_world):
    sfx, app_eng = expiry_world
    # The widget sent two codes: 33 (Beta) and 4510 (Alfa e10: «45100000»).
    res = await _run(
        app_eng,
        subject="awards",
        g1="customer",
        customer=sfx,
        exp="12",
        cpv="eu_cpv:33*\neu_cpv:45100000",
    )
    assert {r.g1_key: r.n for r in res.rows} == {
        f"opština alfa {sfx}": 1,  # only e10 (Cpv 45100000)
        f"bolnica beta {sfx}": 1,  # e2 (Cpv 33100000)
    }
    # One code — a different picture (a check that it is ANY, not “the first of the list”).
    only33 = await _run(
        app_eng, subject="awards", g1="customer", customer=sfx, exp="12", cpv="33*"
    )
    assert {r.g1_key: r.n for r in only33.rows} == {f"bolnica beta {sfx}": 1}


# --------------------------------------------------------------------------- #
#  The keywords of the template (step 3)
# --------------------------------------------------------------------------- #
#
#   tender  customer  title (title_normalized)   publication  note
#   k1      kw-A      “nabavka laptop racunara”      now      include «laptop»
#   k2      kw-A      "nabavka stolova i stolica"    now      no word
#   k3      kw-B      “laptop za skolu”              now      the word is there, but stop «skolu»
#   k4      kw-A      "laptop za arhivu"             2019, seen now   ARCHIVE — not live
#   decisions (customer kw-A, all have the tender k2 — the “foreign” title):
#   w1  lot_title "Isporuka laptop opreme"            -> by lot_title
#   w2  lot_title "Radovi na krovu"                   -> no
#   the consortium wc: two rows with one (uid, lot) and lot_title with «laptop» -> 1 decision


@pytest_asyncio.fixture
async def kw_world():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    sfx = uuid.uuid4().hex[:10]
    ids = {k: uuid.uuid4() for k in ("k1", "k2", "k3", "k4")}
    titles = {
        "k1": ("kw-A", "nabavka laptop racunara", "now"),
        "k2": ("kw-A", "nabavka stolova i stolica", "now"),
        "k3": ("kw-B", "laptop za skolu", "now"),
        "k4": ("kw-A", "laptop za arhivu", "old"),
    }
    async with eng.begin() as c:
        for k, (cust, title, age) in titles.items():
            await c.execute(
                text(
                    "INSERT INTO tenders_normalized (id, source_key, source_uid, "
                    "country_code, status, customer_name, title_normalized, "
                    "published_at, first_seen_at) VALUES (:id, 'jnportal', :uid, "
                    "'RS', 'active', :cust, :title, "
                    "CASE WHEN :age = 'old' THEN timestamptz '2019-05-01' "
                    "ELSE now() END, now())"
                ),
                {
                    "id": ids[k],
                    "uid": f"an2kw-{sfx}-{k}",
                    "cust": f"{cust} {sfx}",
                    "title": title,
                    "age": age,
                },
            )
        for uid, lot, lot_title in (
            ("w1", None, "Isporuka laptop opreme"),
            ("w2", None, "Radovi na krovu"),
            ("wc", "1", "Laptop racunari"),
            ("wc", "1", "Laptop racunari"),
        ):
            await c.execute(
                text(
                    "INSERT INTO tender_awards (tender_id, source_uid, "
                    "document_type_id, lot_number, customer_name, winner_pib, "
                    "winner_name, lot_title, decision_date, raw) VALUES (:t, :uid, "
                    "11, :lot, :cust, :wp, 'Firma', :lt, CURRENT_DATE, "
                    "'{}'::jsonb)"
                ),
                {
                    "t": ids["k2"],
                    "uid": f"an2kw-{sfx}-{uid}",
                    "lot": lot,
                    "cust": f"kw-A {sfx}",
                    "wp": f"P{len(uid)}{uid}",
                    "lt": lot_title,
                },
            )
    app_eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        yield sfx, app_eng
    finally:
        async with eng.begin() as c:
            await c.execute(
                text("DELETE FROM tenders_normalized WHERE id = ANY(:ids)"),
                {"ids": list(ids.values())},
            )
        await app_eng.dispose()
        await eng.dispose()


def _kw(include=None, exclude=None, dict_=False):
    return {
        "include": [{"all_of": g} for g in (include or [])],
        "exclude": exclude or [],
        "dict": dict_,
    }


async def _run_kw(app_eng, kw, **params) -> rb.ReportResult:
    from dataclasses import replace

    spec = replace(rb.parse_spec(params), profile_keywords=kw)
    async with AsyncSession(app_eng) as s:
        return await rb.run_report(s, spec, limit=50, with_coverage=False)


@pytest.mark.asyncio
async def test_keywords_include_and_exclude_same_predicate_as_matching(kw_world):
    sfx, app_eng = kw_world
    base = dict(subject="tenders", g1="customer", customer="kw-")
    # Without words: the live k1, k2, k3 (k4 — archive).
    none = await _run_kw(app_eng, None, **{**base, "customer": sfx})
    assert none.totals.n == 3
    # include «laptop»: k1 and k3 (k4 — archive, not counted).
    inc = await _run_kw(app_eng, _kw([["laptop"]]), **{**base, "customer": sfx})
    assert inc.totals.n == 2
    # + stop word «školu» (Cyrillic/diacritics folded): k3 drops out.
    exc = await _run_kw(
        app_eng, _kw([["laptop"]], ["školu"]), **{**base, "customer": sfx}
    )
    assert exc.totals.n == 1
    assert [r.g1_key for r in exc.rows] == [f"kw-a {sfx}"]
    # AND group: «laptop» AND «racunara» — only k1.
    both = await _run_kw(
        app_eng, _kw([["laptop", "racunara"]]), **{**base, "customer": sfx}
    )
    assert both.totals.n == 1


@pytest.mark.asyncio
async def test_keywords_cyrillic_dictionary_matches_latin_haystack(kw_world):
    # The latent defect #424: a dictionary in Cyrillic against a Latin haystack.
    sfx, app_eng = kw_world
    res = await _run_kw(
        app_eng,
        _kw([["лаптоп"]], ["школу"]),
        subject="tenders",
        g1="customer",
        customer=sfx,
    )
    assert res.totals.n == 1


@pytest.mark.asyncio
async def test_keywords_for_awards_use_lot_title_and_count_consortium_once(kw_world):
    sfx, app_eng = kw_world
    res = await _run_kw(
        app_eng,
        _kw([["laptop"]]),
        subject="awards",
        g1="customer",
        customer=sfx,
    )
    # w1 (lot_title) and the consortium wc (two rows = ONE decision); w2 — no.
    assert res.totals.n == 2


@pytest.mark.asyncio
async def test_keywords_no_unit_passes_gives_empty_report(kw_world):
    sfx, app_eng = kw_world
    res = await _run_kw(
        app_eng,
        _kw([["nepostojeca rec"]]),
        subject="tenders",
        g1="customer",
        customer=sfx,
    )
    assert res.rows == [] and res.totals.n == 0


@pytest.mark.asyncio
async def test_keywords_too_broad_scope_is_refused_not_truncated(kw_world, monkeypatch):
    sfx, app_eng = kw_world
    monkeypatch.setattr(rb, "MAX_KEYWORD_CANDIDATES", 2)  # candidates 3
    with pytest.raises(rb.KeywordScopeTooBroad):
        await _run_kw(
            app_eng,
            _kw([["laptop"]]),
            subject="tenders",
            g1="customer",
            customer=sfx,
        )
