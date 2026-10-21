"""Tests of the search/filters/sorting of the portal ``/tenderi`` (TASK portal_filters).

Three layers:

* the pure parsing of the parameters (:func:`parse_filters`) — a white list, junk ignored;
* the route on a MOCK of the DB (``FakeSession``, marked below): what is given to the SQL (the text + the bind parameters) and what is drawn (the form, chips, noindex, pagination);
* a REAL DB — ``TEST_PORTAL_DATABASE_URL`` (a throwaway PG with the ``unaccent`` extension; without the variable it is skipped, like the RLS tests): each filter narrows the results, “škola” finds “skola” and the Cyrillic form (U+0448 U+043A U+043E U+043B U+0430), an injection is harmless.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.api import deps, repo
from src.api.main import app
from src.api.public import sources_view
from src.api.public.portal_filters import DEFAULT_SORT, parse_filters
from tests.api.conftest import FakeResult, FakeSession
from tests.api.test_geo_landings import _prime_cpv, _prime_region, _prime_sources, _row

SOURCES = {"jnportal": "Portal javnih nabavki (UJN)"}


# --------------------------------------------------------------------------- #
# Parsing of the parameters
# --------------------------------------------------------------------------- #


def test_parse_defaults_are_inactive():
    f = parse_filters({}, source_keys=SOURCES)
    assert not f.active
    assert f.actual is True
    assert f.sort == DEFAULT_SORT
    assert f.query() == ""


def test_parse_unknown_sort_falls_back_to_default():
    f = parse_filters({"sort": "price; DROP TABLE tenders_normalized"})
    assert f.sort == "deadline"
    assert not f.active


def test_parse_ignores_garbage_values():
    f = parse_filters(
        {
            "region": "RS-99",
            "source": "evil",
            "min_amount": "abc",
            "max_amount": "-5",
            "published_from": "31.12.2026",
            "published_to": "2026-13-45",
            "bogus": "1",
        },
        source_keys=SOURCES,
    )
    assert not f.active


def test_parse_valid_values_and_folding_literals():
    f = parse_filters(
        {
            "q": "Škola  ШКОЛА",
            "region": "RS-00",
            "source": "jnportal",
            "min_amount": "1000,5",
            "max_amount": "50000",
            "published_from": "2026-01-01",
            "published_to": "2026-02-01",
            "actual": "0",
            "sort": "amount_desc",
        },
        source_keys=SOURCES,
    )
    assert f.q_tokens == ("skola", "skola")
    assert f.min_amount == Decimal("1000.50")
    assert f.published_from == date(2026, 1, 1)
    assert f.actual is False
    assert f.sort == "amount_desc"
    assert f.region == "RS-00" and f.source == "jnportal"


def test_parse_region_ignored_when_path_fixes_it():
    f = parse_filters({"region": "RS-00"}, allow_region=False)
    assert f.region == ""


def test_parse_swaps_inverted_amount_bounds():
    f = parse_filters({"min_amount": "100", "max_amount": "10"})
    assert (f.min_amount, f.max_amount) == (Decimal("10.00"), Decimal("100.00"))


def test_query_roundtrip_drops_one_param_and_keeps_lang():
    f = parse_filters({"q": "škola", "sort": "amount_asc", "lang": "ru"})
    assert f.query(drop=("q",)) == "?sort=amount_asc&lang=ru"
    assert f.query(page=3) == "?q=%C5%A1kola&sort=amount_asc&lang=ru&page=3"


# --------------------------------------------------------------------------- #
# The route on a MOCK of the DB (SQL is not executed — we check WHAT is given to the SQL and into the HTML)
# --------------------------------------------------------------------------- #


def _client(session: FakeSession):
    app.dependency_overrides[deps.get_session_factory] = lambda: lambda: session
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()
    sources_view.clear_cache()


def _listing(session):
    """The first listing request (the timeout set_config goes before it)."""
    return next(e for e in session.executed if "statement_timeout" not in e[0])


def _cpv_page(path: str, rows=None, found=1):
    _prime_cpv({"45": {"total": 60, "open": 60, "open_sum_eur": None, "label": "x"}})
    session = FakeSession(
        results=[FakeResult(items=rows or [_row()]), FakeResult(scalar=found)]
    )
    resp = _client(session).get(path)
    return resp, session


def test_mock_base_page_has_form_but_no_noindex_and_old_queries():
    _prime_sources()
    _prime_cpv({"45": {"total": 60, "open": 60, "open_sum_eur": None, "label": "x"}})
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(items=[])])
    resp = _client(session).get("/tenderi/cpv/45")
    assert resp.status_code == 200
    assert 'name="q"' in resp.text and 'name="sort"' in resp.text
    assert 'name="robots"' not in resp.text
    assert len(session.executed) == 2  # open + closed, as before the task
    assert "ORDER BY deadline_at ASC\n" in session.executed[0][0]


def test_mock_filtered_page_is_noindex_with_chips_and_found():
    # the full first page (50 rows) → count is executed and gives 70
    resp, session = _cpv_page(
        "/tenderi/cpv/45?q=skola&sort=amount_desc",
        rows=[_row() for _ in range(50)],
        found=70,
    )
    assert resp.status_code == 200
    assert '<meta name="robots" content="noindex,follow">' in resp.text
    assert (
        'rel="canonical" href="http://' in resp.text or 'rel="canonical"' in resp.text
    )
    assert "?q=" not in re.search(r'rel="canonical" href="([^"]+)"', resp.text).group(1)
    assert "Pronađeno: 70" in resp.text
    # the “q” chip’s cross leads to the same page without q, but with sort
    assert 'href="/tenderi/cpv/45?sort=amount_desc"' in resp.text
    assert 'href="/tenderi/cpv/45"' in resp.text  # «Poništi sve»
    sql, params = _listing(session)
    assert "ORDER BY amount_eur DESC NULLS LAST, id" in sql
    assert params["q0"] == "skola" and params["division"] == "45"


def test_mock_injection_in_sort_and_q_is_only_a_bind_param():
    evil = "x'); DROP TABLE tenders_normalized; --"
    resp, session = _cpv_page(f"/tenderi/cpv/45?sort={evil}&q={evil}")
    assert resp.status_code == 200
    sql, params = _listing(session)
    assert "DROP" not in sql and "ORDER BY deadline_at ASC NULLS LAST, id" in sql
    assert all(
        "DROP" not in str(v).upper() or k.startswith("q") for k, v in params.items()
    )
    assert "DROP" not in sql.upper()


def test_mock_pagination_keeps_filters():
    _prime_cpv({"45": {"total": 500, "open": 500, "open_sum_eur": None, "label": "x"}})
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(scalar=120)])
    resp = _client(session).get("/tenderi/cpv/45?q=skola&page=2&lang=en")
    assert resp.status_code == 200
    assert 'href="/tenderi/cpv/45?q=skola&amp;lang=en&amp;page=3"' in resp.text
    assert 'href="/tenderi/cpv/45?q=skola&amp;lang=en"' in resp.text  # prev → page 1


def test_mock_region_page_ignores_region_param_and_fixes_path_region():
    _prime_region({"RS-00": {"total": 20, "open": 20, "open_sum_eur": None}})
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(scalar=1)])
    resp = _client(session).get("/tenderi/region/RS-00?region=RS-01&min_amount=10")
    assert resp.status_code == 200
    _, params = _listing(session)
    assert params["region"] == "RS-00"
    assert 'id="pf-region"' not in resp.text


def test_mock_hub_without_filters_has_no_listing_queries():
    session = FakeSession(results=[])
    resp = _client(session).get("/tenderi")
    assert resp.status_code == 200
    assert 'name="q"' in resp.text and 'name="robots"' not in resp.text
    assert session.executed == []


def test_mock_hub_with_filters_lists_results():
    session = FakeSession(
        results=[FakeResult(items=[_row(id="t-77")]), FakeResult(scalar=1)]
    )
    resp = _client(session).get("/tenderi?q=skola")
    assert resp.status_code == 200
    assert 'href="/tender/t-77"' in resp.text
    assert 'content="noindex,follow"' in resp.text


def test_template_has_no_cyrillic_outside_catalog_in_sr_en():
    resp, _ = _cpv_page("/tenderi/cpv/45?q=skola&lang=en")
    form = resp.text.split('role="search"')[1].split("</form>")[0]
    assert not re.search("[Ѐ-ӿ]", form)


# --------------------------------------------------------------------------- #
# REAL DB
# --------------------------------------------------------------------------- #

_DB_URL = os.environ.get("TEST_PORTAL_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not _DB_URL, reason="TEST_PORTAL_DATABASE_URL не задан (нужен PG с unaccent)"
)

_NOW = datetime.now(timezone.utc)


def _t(title, customer="Opština Test", **kw):
    from src.domain.text_norm import normalize_text

    base = dict(
        id=str(uuid.uuid4()),
        country_code="RS",
        source_key="jnportal",
        title=title,
        title_normalized=normalize_text(title),
        description=None,
        description_normalized=None,
        customer_name=customer,
        customer_tax_id=None,
        amount_eur=1000,
        published_at=_NOW - timedelta(days=5),
        deadline_at=_NOW + timedelta(days=5),
        regions=["RS-00"],
        classifications="[]",
    )
    base.update(kw)
    return base


@pytest_asyncio.fixture
async def db():
    schema = f"pf_{uuid.uuid4().hex[:8]}"
    url = _DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": f"{schema},public"}}
    )
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS unaccent"))
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
        await conn.execute(text(f"""
            CREATE TABLE {schema}.tenders_normalized (
              id uuid PRIMARY KEY, country_code char(2), source_key text,
              title text, title_normalized text, description text,
              description_normalized text, customer_name text,
              customer_tax_id text, amount_eur numeric,
              published_at timestamptz, deadline_at timestamptz,
              regions text[], classifications jsonb)
            """))
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as session:
        yield session
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    await engine.dispose()


async def _seed(db, rows):
    for r in rows:
        await db.execute(
            text("""
            INSERT INTO tenders_normalized VALUES (
              CAST(:id AS uuid), :country_code, :source_key, :title,
              :title_normalized, :description, :description_normalized,
              :customer_name, :customer_tax_id, :amount_eur, :published_at,
              :deadline_at, CAST(:regions AS text[]), CAST(:classifications AS jsonb))
            """),
            r,
        )
    await db.commit()


async def _ids(db, params, **kw):
    f = parse_filters(params, source_keys=SOURCES)
    rows = await repo.search_portal_tenders(db, f, offset=0, limit=50, **kw)
    n = await repo.count_portal_tenders(db, f, **kw)
    assert n == len(rows)
    return [r.title for r in rows]


@needs_db
async def test_db_q_folds_latin_diacritics_and_cyrillic(db):
    await _seed(
        db,
        [
            _t("Izgradnja škola u Nišu"),
            _t("Реконструкција школа"),
            _t("Nabavka goriva"),
        ],
    )
    expected = {"Izgradnja škola u Nišu", "Реконструкција школа"}
    for q in ("škola", "skola", "школа"):
        assert set(await _ids(db, {"q": q})) == expected, q
    # AND between the tokens
    assert await _ids(db, {"q": "skola nis"}) == ["Izgradnja škola u Nišu"]


@needs_db
async def test_db_q_matches_customer_in_cyrillic(db):
    await _seed(
        db,
        [
            _t("A", customer="ЈП Путеви Србије"),
            _t("B", customer="Opština Test"),
        ],
    )
    assert await _ids(db, {"q": "putevi srbije"}) == ["A"]
    assert await _ids(db, {"customer": "Путеви"}) == ["A"]
    assert await _ids(db, {"customer": "PUTEVI"}) == ["A"]


@needs_db
async def test_db_customer_by_tax_id(db):
    await _seed(db, [_t("A", customer_tax_id="100123456"), _t("B")])
    assert await _ids(db, {"customer": "100123456"}) == ["A"]


@needs_db
async def test_db_each_filter_narrows(db):
    await _seed(
        db,
        [
            _t("base"),
            _t("other-region", regions=["RS-06"]),
            _t("other-source", source_key="other"),
            _t("cheap", amount_eur=10),
            _t("rich", amount_eur=900000),
            _t("no-amount", amount_eur=None),
            _t("old", published_at=_NOW - timedelta(days=100)),
            _t("closed", deadline_at=_NOW - timedelta(days=1)),
        ],
    )
    s = {"jnportal": "x", "other": "y"}

    async def titles(params):
        f = parse_filters(params, source_keys=s)
        rows = await repo.search_portal_tenders(db, f, offset=0, limit=50)
        return {r.title for r in rows}

    assert "other-region" not in await titles({"region": "RS-00"})
    assert await titles({"region": "RS-06"}) == {"other-region"}
    assert await titles({"source": "other"}) == {"other-source"}
    assert await titles({"min_amount": "500000"}) == {"rich"}
    assert await titles({"max_amount": "50"}) == {"cheap"}
    assert await titles({"min_amount": "500000", "no_amount": "1"}) == {
        "rich",
        "no-amount",
    }
    old = (_NOW - timedelta(days=100)).date().isoformat()
    assert await titles({"published_to": old}) == {"old"}
    recent = (_NOW - timedelta(days=10)).date().isoformat()
    assert "old" not in await titles({"published_from": recent})
    assert "closed" not in await titles({})
    assert "closed" in await titles({"actual": "0"})
    # a source outside the white list is ignored, rather than giving an empty result
    f = parse_filters({"source": "nope"}, source_keys=s)
    assert not f.active


@needs_db
async def test_db_sort_orders_with_stable_tiebreak(db):
    await _seed(
        db,
        [
            _t("a", amount_eur=5, deadline_at=_NOW + timedelta(days=3)),
            _t("b", amount_eur=50, deadline_at=_NOW + timedelta(days=1)),
            _t("c", amount_eur=None, deadline_at=_NOW + timedelta(days=2)),
            _t("d", amount_eur=500, published_at=_NOW - timedelta(days=1)),
        ],
    )
    assert (await _ids(db, {"sort": "amount_desc"}))[:3] == ["d", "b", "a"]
    assert (await _ids(db, {"sort": "amount_asc"}))[:3] == ["a", "b", "d"]
    assert (await _ids(db, {"sort": "amount_desc"}))[-1] == "c"  # NULL at the end
    assert (await _ids(db, {"sort": "published_desc"}))[0] == "d"
    assert (await _ids(db, {"sort": "deadline"}))[0] == "b"
    # pagination is deterministic: a tie by the sum does not get shuffled between pages
    same = [_t(f"s{i}", amount_eur=7) for i in range(6)]
    await _seed(db, same)
    f = parse_filters({"sort": "amount_asc", "max_amount": "7", "min_amount": "7"})
    p1 = await repo.search_portal_tenders(db, f, offset=0, limit=3)
    p2 = await repo.search_portal_tenders(db, f, offset=3, limit=3)
    assert len({r.id for r in list(p1) + list(p2)}) == 6


@needs_db
async def test_db_injection_is_harmless(db):
    await _seed(db, [_t("keep")])
    evil = "'; DROP TABLE tenders_normalized; --"
    await _ids(db, {"q": evil, "customer": evil, "sort": evil})
    assert await _ids(db, {}) == ["keep"]


@needs_db
async def test_db_cpv_and_path_region_scope(db):
    cpv = '[{"scheme": "CPV", "code": "45000000"}]'
    await _seed(
        db,
        [
            _t("in", classifications=cpv),
            _t("out"),
            _t("in-other-region", classifications=cpv, regions=["RS-06"]),
        ],
    )
    f = parse_filters({}, source_keys=SOURCES)
    rows = await repo.search_portal_tenders(
        db, f, offset=0, limit=50, cpv_division="45", region_code="RS-00"
    )
    assert {r.title for r in rows} == {"in"}


# --------------------------------------------------------------------------- #
# Rework after review: the page ceiling, timeout, short tokens, skipping count
# --------------------------------------------------------------------------- #

HUGE = "999999999999999999"
_PORTAL_PATHS = ("/tenderi", "/tenderi/cpv/45", "/tenderi/region/RS-00")


def _prime_all():
    _prime_cpv({"45": {"total": 60, "open": 60, "open_sum_eur": None, "label": "x"}})
    _prime_region({"RS-00": {"total": 20, "open": 20, "open_sum_eur": None}})


@pytest.mark.parametrize("path", _PORTAL_PATHS)
@pytest.mark.parametrize("query", ["", "q=skola&"])
def test_mock_huge_page_is_last_page_200_everywhere(path, query):
    # 999999999999999999 → PAGE_MAX (10^6), further clamped to the last: 200, not 404/500.
    _prime_all()
    session = FakeSession(
        results=[
            FakeResult(items=[]),
            FakeResult(scalar=60),
            FakeResult(items=[_row()]),
            FakeResult(scalar=60),
        ]
    )
    resp = _client(session).get(f"{path}?{query}page={HUGE}")
    assert resp.status_code == 200
    canonical = re.search(r'rel="canonical" href="([^"]+)"', resp.text).group(1)
    assert "999999999999999999" not in canonical


def test_mock_page_beyond_last_is_last_page_filtered_and_unfiltered():
    _prime_all()
    # filter: found=70 → 2 pages, page=3 → page 2 (empty page 3, count, page 2, count)
    session = FakeSession(
        results=[
            FakeResult(items=[]),
            FakeResult(scalar=70),
            FakeResult(items=[_row()]),
            FakeResult(scalar=70),
        ]
    )
    resp = _client(session).get("/tenderi/cpv/45?q=skola&page=3")
    assert resp.status_code == 200
    assert '<span class="fd-pager__num" aria-current="page">2</span>' in resp.text
    # without a filter: open=60 → 2 pages, page 2 — without count
    _prime_all()
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(items=[])])
    resp = _client(session).get("/tenderi/cpv/45?page=3")
    assert resp.status_code == 200
    assert '<span class="fd-pager__num" aria-current="page">2</span>' in resp.text


def test_mock_page_cap_boundary_with_no_results_is_first_page_200():
    _prime_all()
    session = FakeSession(results=[FakeResult(items=[]), FakeResult(scalar=0)])
    # 1000 — previously the ceiling (404); now with an empty result → page 1, 200, without ?page=1000
    resp = _client(session).get("/tenderi/cpv/45?q=skola&page=1000")
    assert resp.status_code == 200
    assert any("OFFSET" in s for s, _ in session.executed)
    canonical = re.search(r'rel="canonical" href="([^"]+)"', resp.text).group(1)
    assert "page=" not in canonical


class _QueryCanceledError(Exception):
    """MOCK asyncpg.exceptions.QueryCanceledError (SQLSTATE 57014)."""

    sqlstate = "57014"


class _TimeoutSession(FakeSession):
    """MOCK: any listing SELECT fails as if by statement_timeout."""

    def __init__(self, exc):
        super().__init__(results=[])
        self._exc = exc

    async def execute(self, statement, params=None):
        if "tenders_normalized" in str(statement):
            self.executed.append((str(statement), params))
            raise self._exc
        return await super().execute(statement, params)


def _canceled(wrapper):
    return wrapper("SELECT ...", {}, _QueryCanceledError("canceling statement"))


@pytest.mark.parametrize("wrapper_name", ["DBAPIError", "OperationalError"])
@pytest.mark.parametrize("path", _PORTAL_PATHS)
def test_mock_statement_timeout_renders_hint_not_500(path, wrapper_name, caplog):
    from sqlalchemy import exc as sa_exc

    _prime_all()
    session = _TimeoutSession(_canceled(getattr(sa_exc, wrapper_name)))
    resp = _client(session).get(f"{path}?q=skola&actual=0&sort=amount_desc")
    assert resp.status_code == 200
    assert "Pretraga je predugo trajala" in resp.text
    assert "Pronađeno:" not in resp.text
    # into the log — only the exception type, not the text of the query
    assert "skola" not in caplog.text
    assert "_QueryCanceledError" in caplog.text
    # the timeout is set locally in the transaction (set_config(..., true))
    assert any("statement_timeout" in s for s, _ in session.executed)


@pytest.mark.parametrize(
    ("lang", "needle"),
    [("ru", "Поиск занял слишком много времени"), ("en", "took too long")],
)
def test_mock_statement_timeout_hint_is_localized(lang, needle):
    from sqlalchemy import exc as sa_exc

    _prime_all()
    session = _TimeoutSession(_canceled(sa_exc.OperationalError))
    resp = _client(session).get(f"/tenderi?q=skola&lang={lang}")
    assert resp.status_code == 200 and needle in resp.text


@pytest.mark.parametrize("path", _PORTAL_PATHS)
def test_mock_statement_timeout_rolls_back_transaction(path):
    """After 57014 the transaction is aborted → without a rollback any next request gives 500."""
    from sqlalchemy import exc as sa_exc

    _prime_all()
    session = _TimeoutSession(_canceled(sa_exc.OperationalError))
    resp = _client(session).get(f"{path}?q=skola&actual=0")
    assert resp.status_code == 200
    assert session.rolled_back >= 1


def test_mock_non_timeout_db_error_is_not_swallowed():
    from sqlalchemy import exc as sa_exc

    _prime_all()
    boom = sa_exc.OperationalError("SELECT", {}, Exception("connection lost"))
    with pytest.raises(sa_exc.OperationalError):
        _client(_TimeoutSession(boom)).get("/tenderi?q=skola")


def test_parse_short_tokens_ignored():
    f = parse_filters({"q": "a škola 5 b"})
    assert f.q_tokens == ("skola",)
    assert not parse_filters(
        {"q": "a b"}
    ).active  # only short ones → there is no filter
    assert not parse_filters({"q": "я"}).active


def test_mock_count_skipped_when_first_page_is_short():
    _prime_all()
    session = FakeSession(
        results=[FakeResult(items=[_row(), _row()]), FakeResult(scalar=999)]
    )
    resp = _client(session).get("/tenderi/cpv/45?q=skola")
    assert resp.status_code == 200
    assert "Pronađeno: 2" in resp.text
    assert not [s for s, _ in session.executed if "count(*)" in s]


def test_mock_count_runs_when_first_page_is_full():
    _prime_all()
    session = FakeSession(
        results=[FakeResult(items=[_row() for _ in range(50)]), FakeResult(scalar=77)]
    )
    resp = _client(session).get("/tenderi/cpv/45?q=skola")
    assert "Pronađeno: 77" in resp.text
    assert len([s for s, _ in session.executed if "count(*)" in s]) == 1


def test_mock_count_runs_on_page_2_even_if_short():
    _prime_all()
    session = FakeSession(results=[FakeResult(items=[_row()]), FakeResult(scalar=51)])
    resp = _client(session).get("/tenderi/cpv/45?q=skola&page=2")
    assert "Pronađeno: 51" in resp.text


class _KeepOpen:
    """A session factory that gives ONE real session and does NOT close it.

    A real ``async with`` would close the session (= an implicit rollback) and would mask the absence of an explicit ``rollback()`` in the route.
    """

    def __init__(self, session):
        self._s = session

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


@needs_db
@pytest.mark.parametrize("path", _PORTAL_PATHS)
async def test_db_real_statement_timeout_page_200_and_session_usable(
    db, monkeypatch, path
):
    """A REAL 57014: the page is 200 with the timeout text, the session afterwards is alive."""
    import httpx

    from src.api.public import routes_geo

    async def slow_search(session, pf, **kw):
        return await session.execute(text("SELECT pg_sleep(2)"))

    monkeypatch.setattr(routes_geo, "PORTAL_STATEMENT_TIMEOUT_MS", 100)
    monkeypatch.setattr(routes_geo.repo, "search_portal_tenders", slow_search)
    _prime_all()
    app.dependency_overrides[deps.get_session_factory] = lambda: lambda: _KeepOpen(db)
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get(f"{path}?q=skola&actual=0")
    assert resp.status_code == 200
    assert "Pretraga je predugo trajala" in resp.text
    # The next request of the same session passes (without a rollback — InFailedSQLTransaction)
    assert (await db.execute(text("SELECT 1"))).scalar() == 1
    # the local timeout does not “tail” onto the next requests
    assert (await db.execute(text("SHOW statement_timeout"))).scalar() == "0"
    await db.execute(text("SELECT pg_sleep(0.3)"))
