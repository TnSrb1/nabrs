"""Dashboard v2 layout (owner 13.10, step 2): the order of blocks, a single instance of a tender,
removed blocks, sticky column.

The render is live (route + Jinja), services are substituted. Expectations — literals of the ru catalog
(cabinet default) and from the spec, not via ``t()``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import feed_service, routes_profiles
from src.api.cabinet.feed_service import FeedItem, HotDeadline
from src.api.config import get_cabinet_settings
from src.api.main import app

SESS = {"uid": "u-1", "tid": "t-1"}
_CSS = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "static" / "css" / "app.css"
).read_text(encoding="utf-8")


def _async(value):
    async def _stub(*args, **kwargs):
        return value

    return _stub


def _client():
    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(
        "tb_session",
        security.sign_session(
            SESS["uid"], SESS["tid"], get_cabinet_settings().cabinet_secret_key
        ),
    )
    return client


def teardown_function():
    app.dependency_overrides.clear()


def _deadlines(n: int) -> list[HotDeadline]:
    now = datetime.now(timezone.utc)
    return [
        HotDeadline(
            tender_id=f"t-{i}",
            title=f"Набавка бр. {i}",
            customer_name="Општина",
            deadline_at=now + timedelta(days=1 + i * 0.3),
            tender_url="https://portal.rs/t/1",
            days_left=1 + int(i * 0.3),
        )
        for i in range(n)
    ]


def _feed_item(tender_id: str) -> FeedItem:
    return FeedItem(
        tender_id=tender_id,
        title=f"Доставка {tender_id}",
        customer_name="ЈКП",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=3),
        tender_url="https://portal.rs/t/f",
        profile_id="p-1",
        profile_name="Градња",
        ai_level=None,
        sent_at=datetime.now(timezone.utc),
        ai_actions=feed_service.AiActions(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )


def _stub(monkeypatch, *, deadlines=(), feed=(), live=True):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": live, "gaps": [], "channelless": []}),
    )
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_calendar_deadlines", _async(list(deadlines))
    )
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_feed_items", _async(list(feed))
    )


def _agenda_ids(html: str) -> list[str]:
    return re.findall(r'href="/app/tenders/(t-\d+)\?profile_id=', html)


def test_each_tender_appears_once_on_the_page(monkeypatch):
    """“No duplicates”: a tender from the visible part of ROKOVI is not repeated in “New for you”,
    and each tender of the agenda is exactly one row (was: agenda + “Do now” +
    the teaser tab = three copies).

    Mutation: remove the `shown_ids` filter in routes_profiles.dashboard — tender t-0,
    which is both in the agenda and in the deliveries, will appear twice, and the test goes red."""
    _stub(
        monkeypatch,
        deadlines=_deadlines(12),
        feed=[_feed_item("t-0"), _feed_item("t-90")],
    )
    html = _client().get("/app").text
    ids = _agenda_ids(html)
    assert ids, "links to the cards not found — the test is blind"
    assert len(ids) == len(set(ids)), f"duplicates on the page: {sorted(ids)}"
    assert html.count("data-agenda-item") == 12
    assert "Доставка t-90" in html  # a foreign tender from the deliveries remained
    assert "Доставка t-0" not in html  # the agenda duplicate is struck out


def test_blocks_follow_mobile_order_and_removed_blocks_are_gone(monkeypatch):
    """Columns in the DOM: alert → hero → [main: ROKOVI, NOVO, TEMPLATES] → [side:
    calendar, VESTI]. The mobile order "ROKOVI → NOVO → VESTI → TEMPLATES →
    calendar" is set by CSS `order` (see below). What the owner removed is not brought back."""
    _stub(monkeypatch, deadlines=_deadlines(3), feed=[_feed_item("t-90")], live=False)
    monkeypatch.setattr(
        routes_profiles.svc, "list_profiles", _async([_p("p-1", "Градња")])
    )
    html = _client().get("/app").text
    marks = [
        'data-dash-alert="no_channel"',
        'id="cockpit-title"',
        'id="rokovi"',
        'data-cockpit="teaser"',
        'id="profiles"',
        'data-cockpit="calendar"',
        'id="vesti"',
    ]
    pos = [html.index(m) for m in marks]
    assert pos == sorted(pos), dict(zip(marks, pos))
    # Mobile order — values of `order` outside the media block (below 1024px).
    orders = {
        name: int(re.search(rf"\.{name}\s*\{{[^}}]*order:\s*(\d)", _CSS).group(1))
        for name in ("dash-rokovi", "dash-novo", "dash-vesti", "dash-tpl", "dash-cal")
    }
    assert sorted(orders, key=orders.get) == [
        "dash-rokovi",
        "dash-novo",
        "dash-vesti",
        "dash-tpl",
        "dash-cal",
    ]
    for gone in (
        "Сделать сейчас",
        'id="stats-title"',
        "data-news-banner",
        "Ближайшие сроки",
        "?teaser=deadline",
        'aria-label="Подписка и лимит шаблонов"',
    ):
        assert gone not in html, gone


def _p(pid, name):
    from types import SimpleNamespace

    return SimpleNamespace(
        id=pid,
        name=name,
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_vesti_lists_latest_three_with_link_to_all(monkeypatch):
    """Vesti: the last 3 journal entries + “All news →”; the #vesti container is the place
    for the Tržište tab (step 3)."""
    from src.api.public import news

    _stub(monkeypatch)
    html = _client().get("/app").text
    block = html[html.index('id="vesti"') :]
    block = block[: block.index("</section>")]
    assert block.count('href="/novosti/') == min(3, len(news.NEWS))
    assert 'href="/novosti"' in block
    assert "Все новости →" in block


def test_empty_tenant_renders_without_errors(monkeypatch):
    """Empty tenant: the page renders, ROKOVI — empty state, hero — zeros."""
    _stub(monkeypatch, live=False)
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert "В ближайшие 7 дней сроков нет." in resp.text
    assert 'data-count-to="0">0' in resp.text


def test_right_column_is_sticky_from_1024_and_flat_below():
    """The right column sticks only from 1024px; below, the wrappers are “dissolved”
    (`display: contents`), the order is set by `order`."""
    media = _CSS.split("@media (min-width: 1024px) {")[1].split("\n}\n")[0]
    assert re.search(r"\.dash-side\s*\{[^}]*position:\s*sticky", media)
    assert re.search(
        r"\.dash__body\s*\{[^}]*grid-template-columns:\s*minmax\(0, 2fr\)", media
    )
    assert re.search(r"\.dash-main,\s*\.dash-side\s*\{\s*display:\s*contents", _CSS)
