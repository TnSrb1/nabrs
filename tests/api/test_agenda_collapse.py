"""Collapsing the cockpit agenda: the dashboard must remain an overview under a flow of items.

Measurement of 04.08.2026 on the live prod (a tenant with 1643 deliveries): the calendar agenda —
51 items, “do now” — 50, the page 30 817 CSS px = 36 mobile screens.
The metric at that time was CLEAN (no overflow, tap zones normal, no errors),
i.e. no guard was watching for a regression of this kind at all.

What we pin down:
(a) exactly ``AGENDA_PREVIEW_LIMIT`` items are visible, the rest are behind the expansion;
(b) the tail stays IN THE MARKUP with its own ``data-date`` — the “month cell ↔ agenda item” link
    rests on it, and the JS MUST EXPAND it on a click on a day
    (otherwise the client clicks a day and sees nothing);
(c) BOTH event blocks collapse, not one: “do now” is short not by design but by luck — the tenant with a flow had the same 50 rows there;
(d) a short list does not get an expansion — a spoiler above three rows is worse than the three rows themselves.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import feed_service, routes_profiles
from src.api.cabinet.feed_service import AGENDA_PREVIEW_LIMIT, HotDeadline
from src.api.config import get_cabinet_settings
from src.api.main import app

SESS = {"uid": "u-1", "tid": "t-1"}
_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "src" / "api" / "static"

NOW = datetime.now(timezone.utc)


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _deadlines(count: int) -> list[HotDeadline]:
    """``count`` of deadlines INSIDE the week — they get into both the calendar and “now”.

    The ``urgent_events`` window is 7 days, so the step is fractional: this way both blocks get
    the same long list, as it was on prod.
    """
    return [
        HotDeadline(
            tender_id=f"t-{i}",
            title=f"Набавка бр. {i}",
            customer_name="Општина Нови Сад",
            deadline_at=NOW + timedelta(days=1 + i * 0.2),
            tender_url="https://portal.rs/t/1",
            days_left=1 + int(i * 0.2),
        )
        for i in range(count)
    ]


def _client():
    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    client = TestClient(app, follow_redirects=False)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key),
    )
    return client


def teardown_function():
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _stub_dashboard_services(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": False, "gaps": [], "channelless": []}),
    )


def _render(monkeypatch, count: int) -> str:
    monkeypatch.setattr(
        routes_profiles.feed_service,
        "get_calendar_deadlines",
        _async(_deadlines(count)),
    )
    resp = _client().get("/app")
    assert resp.status_code == 200
    return resp.text


def _section(html: str, marker: str, until: str) -> str:
    """A piece of HTML of the cockpit section: from its ``data-cockpit`` to the ``until`` marker.

    The boundary is set EXPLICITLY: inside the calendar there is its own ``data-cockpit=“agenda”``
    (the agenda list), and “up to the next data-cockpit” would cut the section by it.
    """
    start = html.index(f'data-cockpit="{marker}"')
    end = html.index(f'data-cockpit="{until}"')
    assert start < end, (marker, until)
    return html[start:end]


def _split_by_disclosure(section: str) -> tuple[str, str]:
    """(visible part, collapsed tail) — by the boundary of ``<details class="agenda-more"``."""
    marker = '<details class="agenda-more"'
    if marker not in section:
        return section, ""
    head, _, tail = section.partition(marker)
    return head, tail


def test_long_agenda_shows_preview_and_hides_the_rest(monkeypatch):
    """ROKOVI: 20 events → 8 visible, 12 under “Show 12 more”, and none is lost."""
    html = _render(monkeypatch, 20)
    head, tail = _split_by_disclosure(_section(html, "todo", "teaser"))

    assert head.count("data-agenda-item") == AGENDA_PREVIEW_LIMIT
    assert tail.count("data-agenda-item") == 20 - AGENDA_PREVIEW_LIMIT
    assert f"Показать ещё {20 - AGENDA_PREVIEW_LIMIT}" in tail
    assert "Свернуть" in tail


def test_agenda_is_single_block_no_todo_copy(monkeypatch):
    """Dashboard v2 (owner 13.10): “Do now” merged into ROKOVI — the agenda is ONE.

    Previously the same rows were drawn twice (the calendar agenda + “todo”); now
    each tender appears on the page exactly once (in the tail or in the head).
    """
    html = _render(monkeypatch, 20)

    assert html.count('data-cockpit="todo"') == 1
    assert html.count('data-cockpit="agenda"') == 1
    assert html.count("data-agenda-item") == 20


def test_short_agenda_has_no_disclosure(monkeypatch):
    """A list shorter than the expansion limit does not get an expansion — the spoiler would be noise."""
    html = _render(monkeypatch, 3)
    section = _section(html, "todo", "teaser")

    assert section.count("data-agenda-item") == 3
    assert "agenda-more" not in section
    assert "Показать ещё" not in html


def test_hidden_items_keep_their_date_link_to_the_month_grid(monkeypatch):
    """The tail stays in the markup with ``data-date`` — otherwise the link with the grid breaks.

    The expectation is taken from the link mechanics: ``cockpit_calendar.js`` finds the items
    by the ``data-date`` selector; a tail cut out of the markup would mean that
    a click on a day from the second half of the month finds NOTHING.
    """
    html = _render(monkeypatch, 20)
    _, tail = _split_by_disclosure(_section(html, "todo", "teaser"))

    dates = re.findall(r'data-agenda-item[^>]*data-date="([^"]+)"', tail)
    assert len(dates) == 20 - AGENDA_PREVIEW_LIMIT
    for value in dates:
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", value), value


def test_summary_is_first_child_of_details(monkeypatch):
    """‼️ ``<summary>`` not as the first child → the UA hides the content entirely."""
    html = _render(monkeypatch, 20)
    for match in re.finditer(r'<details class="agenda-more"[^>]*>\s*(<[a-z]+)', html):
        assert match.group(1) == "<summary", match.group(0)


def test_calendar_js_opens_the_collapsed_tail_on_day_click():
    """A click on a day MUST SHOW the item, not highlight the invisible one.

    We check exactly ``select`` (the branch “date → tenders”): a highlight without
    expansion is exactly the case that “works” by the markup and does not
    work for the client.
    """
    js = (_STATIC / "js" / "cockpit_calendar.js").read_text(encoding="utf-8")
    select_body = js.split("function select(cell)")[1].split("function peek")[0]

    assert "[data-agenda-more]" in select_body
    assert "more.open = true" in select_body
    # The expansion — BEFORE scrolling to the item: scrolling to the collapsed block would miss.
    assert select_body.index("more.open = true") < select_body.index("scrollIntoView")


def test_preview_limit_is_a_display_limit_not_a_query_limit():
    """Collapsing is about DISPLAY. The selection (50 deadlines / 30 days) is not narrowed.

    Red here = someone “fixed” the dashboard length by trimming the data: then
    the events would simply disappear, and the calendar would start lying about the month.
    """
    assert AGENDA_PREVIEW_LIMIT < feed_service.CALENDAR_DEADLINE_LIMIT
    assert feed_service.CALENDAR_DEADLINE_LIMIT == 50
    assert feed_service.CALENDAR_HORIZON_DAYS == 30
