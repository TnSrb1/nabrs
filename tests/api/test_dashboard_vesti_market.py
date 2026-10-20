"""Vesti → “Tržište” (owner 13.10, step 3) and fixes from acceptance #690.

Live render (route + Jinja), services substituted. Expectations — literals of the ru catalog
(cabinet default) and fixture numbers, not via ``t()``. SQL — in
``tests/db/test_market_sql.py`` (a test on mocks does not check the SQL).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.api import deps
from src.api.cabinet import market_service
from src.api.main import app
from tests.api.test_dashboard_v2_layout import _async, _stub, _client  # noqa: F401

_ROOT = Path(__file__).resolve().parents[2]
_CSS = (_ROOT / "src/api/static/css/app.css").read_text(encoding="utf-8")
_TPL = _ROOT / "src/api/templates"
TODAY = date(2026, 10, 13)

SHARED = {
    "day": "2026-10-13",
    "published": {
        "date": "2026-10-12",
        "total": 23,
        "by_country": {"RS": 14, "BA": 0, "ME": 0, "MK": 9},
    },
    "top": {
        "date": "2026-10-12",
        "items": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "title": "Hemodijaliza",
                "customer": "KC Srbije",
                "country": "RS",
                "amount_eur": 12774456.23,
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "title": "Rezervni delovi",
                "customer": "",
                "country": "RS",
                "amount_eur": 9426271.76,
            },
        ],
    },
    "awards": {
        "date": "2026-10-13",
        "count": 17,
        "known": 16,
        "total_rsd": 182998485.89,
        "top": {
            "amount_rsd": 99590740.56,
            "customer": "JKP Infostan",
            "tender_id": "33333333-3333-3333-3333-333333333333",
        },
    },
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    market_service.clear_cache()
    monkeypatch.setattr(market_service, "local_today", lambda now=None: TODAY)
    yield
    market_service.clear_cache()


def _market_page(monkeypatch, shared, personal, calls=None, path="/app?vesti=trziste"):
    _stub(monkeypatch)

    async def get_shared(*a, **k):
        if calls is not None:
            calls.append("shared")
        return shared

    async def get_personal(*a, **k):
        if calls is not None:
            calls.append("personal")
        return personal

    monkeypatch.setattr(market_service, "get_shared", get_shared)
    monkeypatch.setattr(market_service, "get_personal", get_personal)
    return _client().get(path)


def _block(html: str) -> str:
    block = html[html.index('id="vesti"') :]
    return block[: block.index("</section>")]


def test_market_tab_shows_four_dated_records(monkeypatch):
    """Each entry — with a date; numbers — exactly from the fixture data."""
    html = _market_page(monkeypatch, SHARED, {"new": 12, "expiring": 4}).text
    b = _block(html)
    assert "Вчера опубликовано тендеров: 23 (RS 14, BA 0, ME 0, MK 9)" in b
    assert "Самые крупные тендеры недели:" in b
    assert 'href="/app/tenders/11111111-1111-1111-1111-111111111111"' in b
    assert "Hemodijaliza" in b and "€12 774 456" in b and "KC Srbije" in b
    assert "Заключено договоров за 7 дней: 17, всего 182 998 486 RSD;" in b
    assert (
        'href="/app/tenders/33333333-3333-3333-3333-333333333333"' in b
        and "крупнейший: JKP Infostan, 99 590 741 RSD" in b
    )
    assert "В ваших областях: 12 новых, истекает 4" in b
    assert re.findall(r'vesti__date">([^<]+)<', b) == [
        "12.10.2026",
        "12.10.2026",
        "13.10.2026",
        "13.10.2026",
    ]
    assert 'data-vesti-tab="market" aria-current="true"' in b


def test_market_empty_is_honest_not_silent(monkeypatch):
    """No data — “no data for the date”, not an emptiness and not zeros."""
    html = _market_page(monkeypatch, None, None).text
    b = _block(html)
    assert "Вчера опубликовано: нет данных за 12.10.2026" in b
    assert "Самые крупные тендеры недели: нет данных за 12.10.2026" in b
    assert "Договоры за 7 дней: нет данных за 13.10.2026" in b
    assert "В ваших областях: нет данных за 13.10.2026" in b
    assert "Вчера опубликовано тендеров: 0" not in b


def test_news_tab_is_default_and_does_not_touch_market(monkeypatch):
    """The TenderSRB tab is the default; market queries are not executed on it.

    Mutation: count the market unconditionally in routes_profiles.dashboard — calls is not empty."""
    calls: list[str] = []
    html = _market_page(
        monkeypatch, SHARED, {"new": 1, "expiring": 1}, calls, path="/app"
    ).text
    b = _block(html)
    assert calls == []
    assert "data-vesti-market" not in b
    assert 'href="/novosti/' in b
    assert 'data-vesti-tab="news" aria-current="true"' in b


def test_market_tab_links_have_no_inline_js(monkeypatch):
    html = _market_page(monkeypatch, SHARED, None).text
    b = _block(html)
    assert 'href="/app?vesti=trziste#vesti"' in b and 'href="/app#vesti"' in b
    assert " onclick=" not in b


@pytest.mark.parametrize(
    "lang,needle",
    [
        ("sr", "Juče objavljeno tendera: 23 (RS 14, BA 0, ME 0, MK 9)"),
        ("en", "Tenders published yesterday: 23 (RS 14, BA 0, ME 0, MK 9)"),
    ],
)
def test_market_texts_in_sr_and_en(monkeypatch, lang, needle):
    _stub(monkeypatch)
    monkeypatch.setattr(market_service, "get_shared", _async(SHARED))
    monkeypatch.setattr(market_service, "get_personal", _async(None))
    c = _client()
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u-1",
        "tid": "t-1",
        "locale": lang,
    }
    assert needle in _block(c.get("/app?vesti=trziste").text)


# --- service: daily cache ------------------------------------------------------


class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.sets: list[tuple[str, int]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.sets.append((key, ex))


class _Factory:
    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


async def test_shared_computed_once_per_day_and_cached_in_redis(monkeypatch):
    """Once a day: the second call does not compute; the Redis key carries the date in Belgrade time.

    Mutation: remove the `_local`/`redis.get` check in get_shared — compute will be called twice.
    """
    n = []

    async def compute(db, today):
        n.append(today)
        return SHARED

    monkeypatch.setattr(market_service, "compute_shared", compute)
    redis = _Redis()
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    assert await market_service.get_shared(_Factory(), redis, now=now) == SHARED
    market_service._local.clear()  # “another process”: takes it from Redis
    assert await market_service.get_shared(_Factory(), redis, now=now) == SHARED
    assert n == [date(2026, 10, 13)]
    assert [k for k, _ in redis.sets] == ["vesti:market:v1:2026-10-13"]
    assert json.loads(redis.store["vesti:market:v1:2026-10-13"]) == SHARED


async def test_shared_failure_returns_none_and_backs_off(monkeypatch):
    n = []

    async def compute(db, today):
        n.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(market_service, "compute_shared", compute)
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    assert await market_service.get_shared(_Factory(), None, now=now) is None
    assert await market_service.get_shared(_Factory(), None, now=now) is None
    assert n == [1]  # FAIL_TTL pause: a sick DB does not get a query on each display


def test_cpv_patterns_literal():
    rows = [
        ("RS", {"include": [{"system_code": "eu_cpv", "code": "30000000"}]}),
        ("RS", {"include": [{"scheme": "CPV", "code": "45210*"}]}),
        ("MK", {"include": [{"system_code": "other", "code": "11111111"}]}),
        ("RS", {"include": []}),
        ("RS", None),
    ]
    assert market_service.cpv_patterns(rows) == (["RS", "RS"], ["30%", "45210%"])


def test_build_view_formats_numbers_and_flags_empty():
    v = market_service.build_view(SHARED, {"new": 1234, "expiring": 0}, today=TODAY)
    assert v["awards"]["total"] == "182 998 486"
    assert v["awards"]["top"]["amount"] == "99 590 741"
    assert v["personal"]["new"] == "1 234"
    empty = market_service.build_view(None, None, today=TODAY)
    assert [empty[k]["empty"] for k in ("published", "top", "awards", "personal")] == [
        True
    ] * 4
    assert empty["published"]["date"] == "12.10.2026"


# --- fixes from acceptance #690 ----------------------------------------


def _top_level_media_only(css: str) -> bool:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # in comments there is sometimes “@media”
    depth = 0
    i = 0
    while i < len(css):
        if css.startswith("@media", i) and depth != 0:
            return False
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return depth == 0


def test_media_blocks_are_top_level():
    """New @media — only at the top level (bracket stack)."""
    assert _top_level_media_only(_CSS)


def test_mobile_search_rule_comes_after_base_flex_rule():
    """B1: `flex: 1 1 100%` at 390 was overridden by a later `flex: 1` — order decides."""
    base = _CSS.index(".ck-teaser__search .field-control { flex: 1; min-width: 0; }")
    mobile = _CSS.index(".ck-teaser__search .field-control { flex: 1 1 100%; }")
    assert mobile > base
    assert _CSS.count(".ck-teaser__search .field-control { flex: 1 1 100%; }") == 1


def test_hero_subtitle_matches_three_tiles(monkeypatch):
    html = _market_page(monkeypatch, SHARED, None).text
    assert "Сроки, новые тендеры и тендеры в работе" in html
    assert "Сводка по доступу" not in html


def test_calendar_tip_is_in_heading_row(monkeypatch):
    """B4: “?” — inside the h2 heading, not as a separate `.dash-cal__tip` block."""
    html = _market_page(monkeypatch, SHARED, None).text
    head = html[html.index('id="calendar-title"') :]
    head = head[: head.index("</summary>")]
    assert 'class="tip"' in head
    assert "dash-cal__tip" not in html


def test_rokovi_status_always_on_its_own_line():
    """B5: status/customer — on a second line, both in the “action” and in the deadline."""
    assert ".dash-rokovi .agenda__meta { flex: 1 1 100%; }" in _CSS


def test_onboarding_has_no_extra_bottom_margin():
    """B6: the spacing between onboarding and the body is set by the gap of `.dash`, not by `mb-6` on top."""
    src = (_TPL / "_onboarding_steps.html").read_text(encoding="utf-8")
    assert '<section class="card">' in src
    assert "mb-6" not in src.split("{% for step")[0]
