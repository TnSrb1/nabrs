"""Tests of the cockpit dashboard (portal, Stage 1): calendar, the “in progress” funnel, SRI.

Three blocks:
(a) assembly of ``calendar_events`` — pure functions of ``feed_service`` without a DB:
    sorting by date, the horizon, event types, dedup of the tender, the subscription event
    ONLY with a finite deadline;
(b) the route ``/app`` renders the cockpit with the new context and WITHOUT the catalog feed;
(c) the SRI of local scripts in the templates == the actual hash of the file (the same guard
    as for ``app.css``, but for ``<script src="/static/js/…">`` — otherwise
    the vendored GSAP would silently not load under CSP).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import feed_service, routes_profiles
from src.api.cabinet.i18n import CABINET
from src.api.cabinet.feed_service import (
    EVENT_ACTION,
    EVENT_DEADLINE,
    EVENT_SUBSCRIPTION,
    WORK_STATUSES,
    HotDeadline,
    build_calendar_events,
    build_calendar_month,
    build_work_summary,
    urgent_events,
)
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.db.models.tender_status import STATUSES

SESS = {"uid": "u-1", "tid": "t-1"}
_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _ROOT / "src" / "api" / "templates"
_STATIC = _ROOT / "src" / "api" / "static"

NOW = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)


def _deadline(
    days: float, *, tender_id="t-dl", title="Набавка рачунара"
) -> HotDeadline:
    at = NOW + timedelta(days=days)
    return HotDeadline(
        tender_id=tender_id,
        title=title,
        customer_name="Општина Нови Сад",
        deadline_at=at,
        tender_url="https://portal.rs/t/1",
        days_left=int(days),
    )


def _my_row(days: float, *, status="active", tender_id="t-my", title="Реконструкција"):
    return SimpleNamespace(
        tender_id=tender_id,
        title=title,
        customer_name=None,
        deadline_at=NOW + timedelta(days=days),
        tender_url="https://portal.rs/t/my",
        status=status,
    )


# --------------------------------------------------------------------------- #
# (a) assembly of calendar events — without a DB
# --------------------------------------------------------------------------- #


def test_work_statuses_are_subset_of_funnel():
    """A guard of parity with the bot: “in progress” — the real funnel stages.

    A typo/rename of a stage in the model would silently zero the “in progress” counter
    (the tenders exist, the number is 0) — here this is red, not silent.
    """
    assert set(WORK_STATUSES) <= set(STATUSES)
    assert "new" not in WORK_STATUSES
    assert set(WORK_STATUSES) == {"watching", "active", "submitted"}


def test_calendar_events_sorted_by_date():
    events = build_calendar_events(
        deadlines=[
            _deadline(9, tender_id="t-late", title="Поздний"),
            _deadline(2, tender_id="t-soon", title="Скорый"),
        ],
        now=NOW,
    )
    assert [e.title for e in events] == ["Скорый", "Поздний"]
    assert [e.at for e in events] == sorted(e.at for e in events)


def test_calendar_has_subscription_event_when_access_is_finite():
    events = build_calendar_events(access_until=NOW + timedelta(days=5), now=NOW)
    kinds = [e.kind for e in events]
    assert kinds == [EVENT_SUBSCRIPTION]
    assert events[0].days_left == 5
    assert events[0].url == "/cabinet/account#pretplata"


def test_calendar_has_no_subscription_event_when_access_is_unlimited():
    """Unlimited access (``access_until=None``) gives no event at all."""
    events = build_calendar_events(access_until=None, now=NOW)
    assert [e for e in events if e.kind == EVENT_SUBSCRIPTION] == []


def test_calendar_event_kinds_and_icons():
    events = build_calendar_events(
        deadlines=[_deadline(3, tender_id="t-a")],
        my_tenders=[_my_row(4, tender_id="t-b")],
        access_until=NOW + timedelta(days=5),
        now=NOW,
    )
    assert [e.kind for e in events] == [
        EVENT_DEADLINE,
        EVENT_ACTION,
        EVENT_SUBSCRIPTION,
    ]
    assert [e.icon for e in events] == ["⏰", "📝", "🔔"]


def test_calendar_drops_events_outside_horizon():
    """The past and whatever lies beyond the horizon do not get into the calendar."""
    events = build_calendar_events(
        deadlines=[
            _deadline(-1, tender_id="t-past", title="Прошёл"),
            _deadline(45, tender_id="t-far", title="Далёкий"),
            _deadline(10, tender_id="t-ok", title="В окне"),
        ],
        access_until=NOW + timedelta(days=200),
        now=NOW,
    )
    assert [e.title for e in events] == ["В окне"]


def test_calendar_tender_in_work_wins_over_plain_deadline():
    """One tender = one event: a tender taken into work is shown as an action."""
    events = build_calendar_events(
        deadlines=[_deadline(3, tender_id="t-same", title="Набавка")],
        my_tenders=[_my_row(3, tender_id="t-same", title="Набавка")],
        now=NOW,
    )
    assert len(events) == 1
    assert events[0].kind == EVENT_ACTION


def test_calendar_ignores_funnel_rows_without_deadline_or_not_in_work():
    rows = [
        SimpleNamespace(
            tender_id="t-nodl",
            title="Без дедлайна",
            deadline_at=None,
            tender_url=None,
            status="active",
        ),
        _my_row(3, status="won", tender_id="t-won"),
        _my_row(3, status="new", tender_id="t-new"),
    ]
    assert build_calendar_events(my_tenders=rows, now=NOW) == []


def test_calendar_month_marks_today_and_event_days():
    events = build_calendar_events(deadlines=[_deadline(2)], now=NOW)
    month = build_calendar_month(events, now=NOW)
    days = [d for week in month.weeks for d in week]
    today = [d for d in days if d.is_today]
    marked = [d for d in days if d.has_events]
    assert len(today) == 1 and today[0].day == NOW.date()
    assert [d.day for d in marked] == [(NOW + timedelta(days=2)).date()]
    assert marked[0].kinds == (EVENT_DEADLINE,)
    # Month and weekday names are no longer in the service — the template builds them from
    # locale (see test_cabinet_i18n.test_calendar_widget_renders_in_locale).
    assert (month.month, month.year) == (7, 2026)


def test_calendar_day_dots_are_unique_kinds_not_event_count():
    events = build_calendar_events(
        deadlines=[
            _deadline(2, tender_id="t-1", title="Первый"),
            _deadline(2, tender_id="t-2", title="Второй"),
        ],
        now=NOW,
    )
    month = build_calendar_month(events, now=NOW)
    marked = [d for week in month.weeks for d in week if d.has_events]
    assert marked[0].count == 2
    assert marked[0].kinds == (EVENT_DEADLINE,)  # one point — one type


def test_work_summary_counts_only_work_stages():
    rows = [
        _my_row(1, status="watching", tender_id="t-1"),
        _my_row(1, status="active", tender_id="t-2"),
        _my_row(1, status="active", tender_id="t-3"),
        _my_row(1, status="won", tender_id="t-4"),
        _my_row(1, status="new", tender_id="t-5"),
    ]
    summary = build_work_summary(rows)
    assert summary.total == 3
    assert [(b.status, b.count) for b in summary.buckets] == [
        ("watching", 1),
        ("active", 2),
    ]


def test_urgent_events_takes_week_window_without_subscription():
    events = build_calendar_events(
        deadlines=[
            _deadline(2, tender_id="t-soon", title="Скорый"),
            _deadline(20, tender_id="t-late", title="Поздний"),
        ],
        access_until=NOW + timedelta(days=3),
        now=NOW,
    )
    urgent = urgent_events(events)
    assert [e.title for e in urgent] == ["Скорый"]
    assert all(e.kind != EVENT_SUBSCRIPTION for e in urgent)


# --------------------------------------------------------------------------- #
# (b) route /app renders the cockpit
# --------------------------------------------------------------------------- #


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


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


def test_dashboard_renders_calendar_widget():
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert "Календарь дедлайнов и событий" in resp.text
    assert 'data-cockpit="calendar"' in resp.text
    # Empty 7-day window — a clean ROKOVI empty state, not a gap (v2: one agenda).
    assert "В ближайшие 7 дней сроков нет." in resp.text
    # The 30-day tab has its own empty state (literal from the ru catalog).
    assert "На ближайшие 30 дней событий нет." in _client().get("/app?agenda=30").text


def test_dashboard_subscription_is_not_an_agenda_event_but_a_call_strip(monkeypatch):
    """v2 (owner 13.10): subscription expiry is NOT an agenda/calendar event (it was the 5th repetition
    of the fact). Within ≤7 days — a call-to-action strip `data-dash-sub-strip`; for “distant” — silence.
    The previous test required “Subscription expires” + 🔔 in the agenda — the contract changed.
    """
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_days_left",
        _async(
            routes_profiles.stats_service.AccessDays(
                label="5 дн.",
                days=5,
                until=datetime.now(timezone.utc) + timedelta(days=5),
            )
        ),
    )
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert "Подписка истекает" not in resp.text
    assert "🔔" not in resp.text
    assert "data-dash-sub-strip" in resp.text
    assert "Подписка заканчивается через 5 дн." in resp.text
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_days_left",
        _async(
            routes_profiles.stats_service.AccessDays(
                label="30 дн.",
                days=30,
                until=datetime.now(timezone.utc) + timedelta(days=30),
            )
        ),
    )
    assert "data-dash-sub-strip" not in _client().get("/app").text


def test_dashboard_has_no_subscription_event_when_unlimited():
    """Open-ended (fake-session default: access_until=NULL) → no event."""
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert "Подписка истекает" not in resp.text


def test_dashboard_has_no_platform_stats_block(monkeypatch):
    """v2 (owner 13.10): “Platform statistics” removed from the dashboard (moves to Vesti/Tržište,
    step 3). Earlier tests of the “N tenders since date” counter and the archive row required the block —
    now we pin its ABSENCE so it does not come back silently."""
    stats = routes_profiles.stats_service.SystemStats(
        all_time=16047, this_month=0, today=0, since=date(2026, 5, 30), archive=40946
    )
    monkeypatch.setattr(
        routes_profiles.stats_service, "get_system_stats", _async(stats)
    )
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _client().get("/app").text))
    assert 'id="stats-title"' not in _client().get("/app").text
    assert "16047" not in text
    assert "в архиве старого портала" not in text


@pytest.mark.parametrize("count", [1, 2, 11])
def test_dashboard_hero_work_tile_shows_count(monkeypatch, count):
    """The “In progress” tile — funnel count (no plural caption “N tenders in progress” in v2:
    the number and the short caption are separate). Expectation — literal from the ru catalog.
    """
    rows = [_my_row(1, status="active", tender_id=f"t-{i}") for i in range(count)]
    monkeypatch.setattr(
        routes_profiles.workflow_service,
        "get_my_tenders",
        _async(SimpleNamespace(funnel=rows)),
    )
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert (
        f'data-count-to="{count}">{count}</p>\n      <p class="ck-tile__label">В работе'
        in resp.text
    )


def test_dashboard_carries_no_trial_or_telegram_card(monkeypatch):
    """owner 22.09: the dashboard is an overview, settings are in /cabinet/account. The test-access request and the Telegram card with the link button moved to the cabinet
    (request status captions are checked by test_routes_account), the access term — to the subscription strip. Here — only their absence, otherwise the duplicate returns silently.

    Mutation: put the “Test access” card with the form back into dashboard.html — the test goes red on action.
    """
    monkeypatch.setattr(
        routes_profiles.trial_service,
        "get_access_status",
        _async(
            routes_profiles.trial_service.AccessStatus(
                state="none", active=False, can_request=True, until=None
            )
        ),
    )
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert 'action="/cabinet/trial/request"' not in resp.text
    assert 'action="/cabinet/telegram/link"' not in resp.text
    assert 'id="telegram-card"' not in resp.text
    assert "тестовый доступ не активирован" not in resp.text


@pytest.mark.parametrize(
    "lang,weekday",
    [("en", "Mon"), ("sr", "pon"), ("ru", "Пн")],
)
def test_dashboard_calendar_weekdays_localized(lang, weekday):
    """Weekday and month names of the calendar come from the locale catalog, not from feed_service.

    The month caption is not hardcoded: it is taken from the CABINET catalog by the current UTC month
    (the same `now` as `feed_service.build_calendar_month`) — otherwise the test is date-dependent and goes red on the first day of the next month after it was written.

    Mutation: put _WEEKDAYS_RU/_MONTHS_RU back into the service and print `calendar_month.
    label`/`.weekdays` — en/sr lose their marker (“Mon” and the Russian month would remain).
    """
    now = datetime.now(timezone.utc)
    month_key = f"mon_{now.month}"
    month_label = f"{CABINET[lang][month_key]} {now.year}"
    resp = _client().get(f"/app?lang={lang}")
    assert resp.status_code == 200
    assert f'<div class="cal__wd">{weekday}</div>' in resp.text
    assert month_label in resp.text
    if lang == "en":
        assert '<div class="cal__wd">Пн</div>' not in resp.text
        assert CABINET["ru"][month_key] not in resp.text


def test_dashboard_teaser_replaces_catalog_feed():
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert "Последние доставки" in resp.text
    assert "Лента доставок" not in resp.text


def test_dashboard_limits_teaser_to_five_deliveries(monkeypatch):
    """The teaser asks the service for DASHBOARD_TEASER_LIMIT rows + a margin for duplicates with the visible part of ROKOVI (v2: such tenders are struck out of the teaser), not the full feed."""
    seen: dict = {}

    async def _spy(db, tid, limit=10, **kwargs):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(routes_profiles.feed_service, "get_feed_items", _spy)
    resp = _client().get("/app")
    assert resp.status_code == 200
    assert feed_service.DASHBOARD_TEASER_LIMIT == 5
    assert (
        seen["limit"]
        == feed_service.DASHBOARD_TEASER_LIMIT + feed_service.AGENDA_PREVIEW_LIMIT
    )


def _spy_teaser(monkeypatch):
    seen: dict = {}

    async def _spy(db, tid, limit=10, sort="date"):
        seen["sort"] = sort
        return []

    monkeypatch.setattr(routes_profiles.feed_service, "get_feed_items", _spy)
    return seen


def test_dashboard_search_form_leads_to_feed():
    """Dashboard search — GET on /app/feed with the q field (one search place in the cabinet).

    Mutation: change the form action in _cockpit_feed_teaser.html — the test goes red.
    """
    html = _client().get("/app").text
    form = re.search(
        r'<form method="get" action="([^"]+)"[^>]*role="search".*?</form>', html, re.S
    )
    assert form and form.group(1) == "/app/feed"
    assert 'name="q"' in form.group(0)


def test_dashboard_teaser_has_no_deadline_tab(monkeypatch):
    """v2: the teaser tab “nearest deadlines” is removed (the same rows are in ROKOVI);
    `?teaser=` is ignored, sorting is always by delivery date."""
    seen = _spy_teaser(monkeypatch)
    html = _client().get("/app?teaser=deadline").text
    assert seen["sort"] == "date"
    assert "?teaser=deadline" not in html
    assert "Ближайшие сроки" not in html


@pytest.mark.parametrize(
    "value,tab", [("", 7), ("7", 7), ("bogus", 7), ("30", 30), ("30;--", 7)]
)
def test_dashboard_agenda_tab_whitelist(value, tab):
    """ROKOVI 7/30 tabs — whitelist `?agenda=`: unknown → 7."""
    html = _client().get("/app", params={"agenda": value}).text
    assert re.search(
        rf'href="/app{"" if tab == 7 else "[?]agenda=30"}#rokovi" class="ck-teaser__tab" aria-current',
        html,
    )


def test_search_reset_script_wired_on_feed_and_portal_partials():
    """Search-reset JS — an external file with SRI matching the actual hash, in both partials."""
    import base64
    import hashlib

    js = (_TEMPLATES.parent / "static/js/pf_search_reset.js").read_bytes()
    expected = "sha384-" + base64.b64encode(hashlib.sha384(js).digest()).decode()
    for name in ("_feed_filters.html", "_portal_filters.html"):
        html = (_TEMPLATES / name).read_text(encoding="utf-8")
        tag = (
            'src="{{ static_url(\'js/pf_search_reset.js\') }}" integrity="'
            + expected
            + '"'
        )
        assert tag in html
        assert 'class="pf__clear"' in html


# --------------------------------------------------------------------------- #
# (c) progressive enhancement + SRI for local scripts
# --------------------------------------------------------------------------- #


def test_cockpit_markup_has_no_inline_scripts_and_no_external_sources():
    """CSP: scripts only external and only 'self'; inline JS is forbidden."""
    for name in ("base.html", "dashboard.html"):
        html = (_TEMPLATES / name).read_text(encoding="utf-8")
        for tag in re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S):
            assert tag.strip() == "", f"inline JS in {name}"
        for src in re.findall(r'<script[^>]*src="([^"]+)"', html):
            assert src.startswith(
                ("/static/", "{{ static_url(")
            ), f"external script {src} in {name}"


def test_vendored_gsap_pinned_and_fetches_nothing_at_runtime():
    """Vendored GSAP is pinned and at runtime loads nothing from outside.

    The gsap.com links inside the file are the license banner and the warning text
    (“target not found. https://gsap.com”), these are strings, not a load. Red
    here means a real network call appeared or the version was swapped.
    """
    js = (_STATIC / "js" / "gsap.min.js").read_text(encoding="utf-8")
    assert "GSAP 3.12.5" in js
    for network in ("fetch(", "XMLHttpRequest", "importScripts", "//cdn"):
        assert network not in js, f"GSAP pulls in external: {network}"
    external = {u.strip() for u in re.findall(r"https?://[^\"' )]+", js)}
    assert external <= {
        "https://gsap.com",
        "https://gsap.com/standard-license",
        "http://www.w3.org/1999/xhtml",  # XML namespace, not network
        "http://www.w3.org/2000/svg",
    }, external


def test_local_script_sri_matches_files():
    """SRI of each local <script src> == the file’s actual hash.

    A guard against “changed the js, forgot to recompute”: under CSP+SRI such a script
    silently won’t run, while the page looks intact from outside.
    """
    checked = 0
    for template in _TEMPLATES.glob("*.html"):
        html = template.read_text(encoding="utf-8")
        for src, integrity in re.findall(
            r'<script[^>]*src="\{\{ static_url\(\'([^\']+)\'\) \}\}"[^>]*integrity="([^"]+)"',
            html,
        ):
            path = _STATIC / src
            expected = (
                "sha384-"
                + base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()
            )
            assert integrity == expected, (
                f"SRI {src} in {template.name} is outdated — recompute: "
                f"openssl dgst -sha384 -binary {path} | openssl base64 -A"
            )
            checked += 1
    assert checked >= 3  # htmx + confirm_dialogs + gsap + cockpit


def test_cockpit_js_respects_reduced_motion_and_missing_gsap():
    """Animations are off under reduce and when GSAP failed to load."""
    js = (_STATIC / "js" / "cockpit.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in js
    assert "if (reduceMotion) return;" in js
    assert "if (!gsap) return;" in js


# --------------------------------------------------------------------------- #
# (d) Stage 7 — two-way link between date and tenders in the calendar
# --------------------------------------------------------------------------- #

_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _dates_in(html: str, marker: str) -> list[str]:
    """All ``data-date`` on tags carrying ``marker`` (same tag, any order)."""
    found: list[str] = []
    for tag in re.findall(r"<[a-z]+\b[^>]*>", html):
        if marker not in tag:
            continue
        match = re.search(r'data-date="([^"]*)"', tag)
        if match:
            found.append(match.group(1))
    return found


def test_calendar_day_and_agenda_item_share_iso_date_format(monkeypatch):
    """The date↔tenders link rests on a STRING match of the ISO day.

    If the format diverges on either side (e.g. the agenda starts printing ``%d.%m``) — matches will never happen, and the widget stays externally intact: a click simply stops highlighting anything. Therefore the format is checked on BOTH sides at once, on a live render.
    """
    monkeypatch.setattr(
        routes_profiles.feed_service,
        "get_calendar_deadlines",
        # The module’s NOW is pinned to July 2026 — the calendar window is computed from
        # the REAL now, so the deadline is built from it too.
        _async(
            [
                dataclasses.replace(
                    _deadline(0),
                    deadline_at=datetime.now(timezone.utc) + timedelta(days=2),
                )
            ]
        ),
    )
    html = _client().get("/app").text

    cells = _dates_in(html, "data-cal-day")
    items = _dates_in(html, "data-agenda-item")
    assert len(cells) >= 28, "grid cells have no data-date"
    assert items, "agenda items have no data-date"
    for value in cells + items:
        assert _ISO_DAY.match(value), f"data-date is not an ISO day: {value!r}"


def test_calendar_marks_exactly_today_and_announces_it():
    """“Where am I” — exactly one cell, and it is today.

    ``aria-current=“date”`` — the same truth for a screen reader as the disk is for the eye;
    it is also the roving-tabindex anchor in JS.
    """
    html = _client().get("/app").text
    today = datetime.now(timezone.utc).date().isoformat()

    marked = [
        tag
        for tag in re.findall(r"<div\b[^>]*data-cal-day[^>]*>", html)
        if "is-today" in tag
    ]
    assert len(marked) == 1, f"is-today cells must be 1, found {len(marked)}"
    assert f'data-date="{today}"' in marked[0]
    assert 'aria-current="date"' in marked[0]


def test_calendar_cells_are_not_fake_buttons_without_js():
    """Progressive enhancement: the server does NOT promise a button that does not exist without JS.

    role/tabindex/aria-label are set by cockpit_calendar.js — otherwise a keyboard user would get 42 stops on cells that do nothing.
    """
    html = (_TEMPLATES / "_calendar.html").read_text(encoding="utf-8")
    assert 'role="button"' not in html
    assert (
        'tabindex="' not in html
    )  # the attribute, not the word “tabindex” in a comment
    assert (
        "data-cal-label=" in html
    )  # text for the future aria-label — already in the markup

    js = (_STATIC / "js" / "cockpit_calendar.js").read_text(encoding="utf-8")
    assert 'setAttribute("role", "button")' in js
    assert 'setAttribute("aria-label"' in js


def test_calendar_js_does_not_hijack_agenda_link_click():
    """An agenda item is a link to a tender: a click on it must open the tender.

    Therefore “tender→date” hangs on mouseenter/focus, and preventDefault in the file exists only for the grid keys (space would otherwise scroll the page).
    """
    js = (_STATIC / "js" / "cockpit_calendar.js").read_text(encoding="utf-8")
    agenda_block = js.split("[data-agenda-item][data-date]")[-1]
    assert "preventDefault" not in agenda_block
    for event in ("mouseenter", "mouseleave", "focus", "blur"):
        assert f'"{event}"' in agenda_block


def test_calendar_js_respects_reduced_motion_for_scroll():
    """Under reduce, scroll to tenders is instant, without smooth scrolling."""
    js = (_STATIC / "js" / "cockpit_calendar.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in js
    assert 'behavior: reduceMotion ? "auto" : "smooth"' in js


def test_calendar_interactive_state_classes_are_defined_in_css():
    """State classes are set by JS — the CSS-coverage guard does not see them.

    It reads only the static ``class="..."`` of templates, so the highlight could silently not apply. Here — an explicit check of all four.
    """
    css = (_STATIC / "css" / "app.css").read_text(encoding="utf-8")
    for selector in (
        ".cal__day.is-selected",
        ".cal__day.is-peek",
        ".cal__day.is-today .cal__num",
        ".agenda__item.is-highlighted",
    ):
        # The whole selector, not a substring: `.is-peek-XX` contains `.is-peek`
        # and would pass a substring check while styling nothing.
        assert re.search(
            re.escape(selector) + r"\s*[{,]", css
        ), f"no style for {selector} — the highlight will not apply"


def test_today_marker_survives_has_events_rule():
    """The “today” marker must not depend on the cell background.

    It used to be set by the background — and lost to the ``.has-events`` rule lower in the file,
    i.e. it vanished exactly on days with events. Now the disk lives on ``.cal__num``:
    red here = someone returned the “today” highlight to the cell background.
    """
    css = (_STATIC / "css" / "app.css").read_text(encoding="utf-8")
    today_rule = css.split(".cal__day.is-today .cal__num {")[1].split("}")[0]
    assert "background: var(--color-primary-700)" in today_rule
    assert (
        ".cal__day.is-today {" not in css
    ), "“today” is again painted via the cell background — .has-events will override it"


def test_cockpit_numbers_rendered_server_side_not_hidden_until_animated():
    """Progressive enhancement: figures are visible without JS.

    The counter reads the target from data-count-to, and the number itself is already in the markup — if JS did not load, the subscriber sees values, not zeros or emptiness.
    """
    html = (_TEMPLATES / "_cockpit_hero.html").read_text(encoding="utf-8")
    assert 'data-count-to="{{ work_summary.total }}">{{ work_summary.total }}' in html
    assert 'data-count-to="{{ urgent_count }}">{{ urgent_count }}' in html
    assert 'data-count-to="{{ flow_pulse }}">{{ flow_pulse }}' in html
    assert "opacity: 0" not in html and "display:none" not in html


def test_dashboard_search_scope_switch_and_sources_link():
    """Search “in mine / in all” (owner 02.10): two buttons of one form; “in all” has its own formaction to /tenderi; the line “Your sources: K of N” leads to /izvori.
    N = 1 — conftest accepts one enabled source (jnportal); K = 0 — in this fixture the tenant has no templates (K — the intersection of the union of templates with enabled sources).

    Mutation: remove the formaction from the second button — the test goes red.
    """
    html = _client().get("/app").text
    form = re.search(r'<form method="get" action="/app/feed".*?</form>', html, re.S)
    assert form
    assert "Искать в моих</button>" in form.group(0)
    assert (
        '<button type="submit" formaction="/tenderi" class="btn-secondary pf__submit">'
        "Искать во всех</button>" in form.group(0)
    )
    assert (
        '<a href="/izvori" class="src-link hover:underline">Ваши площадки: 0 из 1 →</a>'
        in html
    )


def test_dashboard_teaser_title_is_constant(monkeypatch):
    """v2: the teaser heading is one (“Latest deliveries”) — there is no “by deadline” mode.
    The previous test required the heading to change to “Nearest deadlines” with `?teaser=deadline`.
    """
    _spy_teaser(monkeypatch)
    for url in ("/app", "/app?teaser=deadline"):
        html = _client().get(url).text
        assert 'id="teaser-title">Последние доставки' in html
