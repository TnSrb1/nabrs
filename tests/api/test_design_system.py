"""Design system tests (VG-28.1): tokens + components + frame.

We check: (a) key pages render 200; (b) the SRI hash of app.css in the templates matches the actual file (protection against “forgot to recompute” after a CSS edit); (c) navigation shows the active state on the current path.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_library, routes_profiles
from src.api.cabinet.feed_service import AiActions, FeedItem, HotDeadline
from src.api.config import get_cabinet_settings
from src.api.main import app

SESS = {"uid": "u-1", "tid": "t-1"}


def _session_cookie() -> str:
    """Signed session cookie (for _auth_context — the base template reads it directly, independently of the get_optional_session dependency override)."""
    settings = get_cabinet_settings()
    return security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key)


_APP_CSS = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "static" / "css" / "app.css"
)
_TEMPLATES_WITH_APP_CSS = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "templates" / "base.html",
    Path(__file__).resolve().parents[2]
    / "src"
    / "api"
    / "templates"
    / "public_base.html",
)

_CPV_WIDGET_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "api"
    / "static"
    / "js"
    / "cpv_widget.js"
)
_COCKPIT_JS = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "static" / "js" / "cockpit.js"
)
_DASHBOARD_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "templates" / "dashboard.html"
)
_PROFILE_FORM_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "api"
    / "templates"
    / "profile_form.html"
)


def _client(*, sess=None):
    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _authed_dashboard_client():
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    return client


@pytest.fixture(autouse=True)
def _stub_dashboard_services(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    # By default — no live channel and no gaps (the “connect a channel” step
    # of onboarding is not done). Tests where a channel is connected override it.
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": False, "gaps": [], "channelless": []}),
    )


# --------------------------------------------------------------------------- #
# (a) key pages render 200
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/", "/help", "/plans", "/login"])
def test_key_public_pages_render_200(path):
    resp = _client().get(path)
    assert resp.status_code == 200


def test_app_dashboard_renders_200():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# (b) SRI hash of app.css in the templates == the actual hash of the file
# --------------------------------------------------------------------------- #


def _actual_app_css_sri() -> str:
    digest = hashlib.sha384(_APP_CSS.read_bytes()).digest()
    return "sha384-" + base64.b64encode(digest).decode()


def test_app_css_sri_matches_templates():
    expected = _actual_app_css_sri()
    for template_path in _TEMPLATES_WITH_APP_CSS:
        html = template_path.read_text(encoding="utf-8")
        match = re.search(
            r'href="\{\{ static_url\(\'css/app\.css\'\) \}\}"\s+integrity="([^"]+)"',
            html,
        )
        assert match, f"app.css <link integrity=...> not found in {template_path.name}"
        assert match.group(1) == expected, (
            f"SRI hash of app.css in {template_path.name} is outdated — "
            "recompute: openssl dgst -sha384 -binary "
            "src/api/static/css/app.css | openssl base64 -A"
        )


def test_app_css_sri_served_matches_response_body():
    """The SRI in the markup must match the actually served /static/css/app.css."""
    expected = _actual_app_css_sri()
    resp = _client().get("/static/css/app.css")
    assert resp.status_code == 200
    served_digest = hashlib.sha384(resp.content).digest()
    served = "sha384-" + base64.b64encode(served_digest).decode()
    assert served == expected


def test_cpv_widget_js_sri_matches_template():
    """SRI hash of cpv_widget.js in profile_form.html == the actual hash of the file
    (protection against “forgot to recompute” after editing the widget — otherwise CSP silently
    blocks the script and the CPV widget stops working)."""
    digest = hashlib.sha384(_CPV_WIDGET_JS.read_bytes()).digest()
    expected = "sha384-" + base64.b64encode(digest).decode()
    html = _PROFILE_FORM_TEMPLATE.read_text(encoding="utf-8")
    match = re.search(
        r'src="\{\{ static_url\(\'js/cpv_widget\.js\'\) \}\}"\s+integrity="([^"]+)"',
        html,
    )
    assert match, "cpv_widget.js <script integrity=...> not found in profile_form.html"
    assert match.group(1) == expected, (
        "SRI hash of cpv_widget.js in profile_form.html is outdated — recompute: "
        "openssl dgst -sha384 -binary src/api/static/js/cpv_widget.js | "
        "openssl base64 -A"
    )


def test_cockpit_js_sri_matches_template():
    """SRI of cockpit.js in dashboard.html == the actual hash of the file.

    The same class as cpv_widget: forgot to recompute — CSP silently strips
    the script, and the dashboard loses the entry animation along with the counters.
    """
    js = _COCKPIT_JS
    digest = hashlib.sha384(js.read_bytes()).digest()
    expected = "sha384-" + base64.b64encode(digest).decode()
    html = _DASHBOARD_TEMPLATE.read_text(encoding="utf-8")
    match = re.search(
        r'src="\{\{ static_url\(\'js/cockpit\.js\'\) \}\}"\s+integrity="([^"]+)"', html
    )
    assert match, "cockpit.js <script integrity=...> not found in dashboard.html"
    assert match.group(1) == expected, (
        "SRI hash of cockpit.js in dashboard.html is outdated — recompute: "
        "openssl dgst -sha384 -binary src/api/static/js/cockpit.js | "
        "openssl base64 -A"
    )


def test_agenda_stagger_is_bounded_by_total_time():
    """The agenda cascade is limited by the TOTAL time, not by a step per element.

    Production measurement 15.08.2026: on a dashboard with a real feed of 202 agenda items,
    `stagger: 0.04` stretched the entrance to 8+ seconds — the “Do now” block,
    which follows in the DOM, stood transparent for several screens. The expectation
    is taken from this fact: the entrance duration must not depend on the volume of
    data.
    """
    js = _COCKPIT_JS.read_text(encoding="utf-8")
    block = re.search(r'animate\("\[data-agenda-item\]".*?\}\);', js, re.S)
    assert block, "the call of the agenda items animation not found"
    assert "amount:" in block.group(0), (
        "the agenda stagger is set as a step per element: on a long list the entrance "
        "will stretch in proportion to the number of items"
    )


# --------------------------------------------------------------------------- #
# (c) navigation — the active item on the current path
# --------------------------------------------------------------------------- #


def test_public_nav_marks_help_active_on_help_page():
    resp = _client().get("/help", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'href="/help" class="nav-link is-active"' in resp.text


def test_public_nav_does_not_mark_help_active_on_landing():
    resp = _client().get("/", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'href="/help" class="nav-link is-active"' not in resp.text
    assert 'href="/help" class="nav-link"' in resp.text


def test_cabinet_nav_marks_profiles_active_on_app():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert 'href="/app" class="nav-link is-active"' in resp.text
    assert 'href="/library" class="nav-link"' in resp.text


def test_cabinet_nav_marks_library_active_on_library(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    resp = client.get("/library")
    assert resp.status_code == 200
    assert 'href="/library" class="nav-link is-active"' in resp.text
    assert 'href="/app" class="nav-link"' in resp.text


def test_old_email_panel_address_redirects_to_hub_channels(monkeypatch):
    """“My Email” is no longer a page (owner 10.10): the old address → 303 to the hub section."""
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    resp = client.get("/cabinet/email")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/cabinet/account#kanali"


def test_cabinet_nav_labels_app_as_dashboard():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert '<a href="/app" class="nav-link is-active">Дашборд</a>' in resp.text


def test_cabinet_nav_groups_tools_into_dropdown():
    """IA owner 10.10: tools — in “Tools ▾”, the row — daily items; common
    pages (Plans/Sources/News) — in the footer, not in the header."""
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert 'class="nav-more nav-hide-mobile"' in resp.text
    assert "Инструменты ▾" in resp.text
    assert 'class="nav-more nav-mobile"' in resp.text  # one phone menu
    # “Logout” — POST form with CSRF: in the identification row on desktop (to the right of
    # the languages, NOT in the menu row — otherwise with “Admin” it wrapped as a third row) + in the phone menu.
    desktop_logout = '<form method="post" action="/logout" class="whoami-logout-form nav-hide-mobile">'
    assert desktop_logout in resp.text
    assert resp.text.index(desktop_logout) < resp.text.index("site-header__bar")
    assert resp.text.count('action="/logout"') == 2
    assert resp.text.count('name="csrf"') >= 2
    for href in ("/cabinet/narucilac", "/app/analitika", "/library", "/tenderi"):
        assert f'href="{href}"' in resp.text
    head = resp.text.split("</header>")[0]
    for href in ("/plans", "/izvori", "/novosti"):
        assert f'href="{href}"' not in head
        assert f'href="{href}"' in resp.text.split("</header>")[1]


# --------------------------------------------------------------------------- #
# (VG-28.3b-UI) dashboard — the delivery feed + burning deadlines + AI badges
# --------------------------------------------------------------------------- #

_HOT_DEADLINE_URGENT = HotDeadline(
    tender_id="t-1",
    title="Набавка рачунара",
    customer_name="Општина Нови Сад",
    deadline_at=datetime.now(timezone.utc) + timedelta(days=1),
    tender_url="https://portal.rs/t/1",
    days_left=1,
)

_FEED_ITEM_FULL = FeedItem(
    tender_id="t-1",
    title="Набавка рачунара",
    customer_name="Општина Нови Сад",
    deadline_at=datetime.now(timezone.utc) + timedelta(days=5),
    tender_url="https://portal.rs/t/1",
    profile_id="p-1",
    profile_name="ИТ набавке",
    ai_level="standard",
    sent_at=datetime.now(timezone.utc) - timedelta(hours=1),
    ai_actions=AiActions(
        delivered_analysis_level="standard",
        has_bid_verdict=True,
        has_checklist=True,
        has_draft=True,
    ),
)


def test_dashboard_hides_todo_section_when_nothing_burns():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Сделать сейчас" not in resp.text


def test_dashboard_shows_burning_deadline_with_urgent_accent(monkeypatch):
    """Cockpit: a burning deadline gets into the ROKOVI agenda with an accent.

    v2 (owner 13.10): there is no “Do now” block — it is merged into ROKOVI (“Deadlines” in ru).
    """
    monkeypatch.setattr(
        routes_profiles.feed_service,
        "get_calendar_deadlines",
        _async([_HOT_DEADLINE_URGENT]),
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Сделать сейчас" not in resp.text
    assert 'id="rokovi-title">Сроки' in resp.text
    assert "Набавка рачунара" in resp.text
    assert "badge-urgent" in resp.text


def test_dashboard_teaser_shows_delivery_without_catalog_feed(monkeypatch):
    """Dashboard — a cockpit, not a catalog: a delivery teaser instead of the feed.

    The heading leads to the tender card in the cabinet (entry to AI analysis in one click), and
    the artifact badges and the full list stay on /app/feed.
    """
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_feed_items", _async([_FEED_ITEM_FULL])
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Последние доставки" in resp.text
    assert "Лента доставок" not in resp.text
    assert "ИТ набавке" in resp.text
    assert "/app/tenders/t-1?profile_id=p-1" in resp.text
    assert 'href="/app/feed"' in resp.text
    # The catalog wrapping of the feed on the dashboard no longer lives here.
    assert "все AI-действия" not in resp.text
    assert 'title="Анализ ТЗ выполнен (уровень: standard)"' not in resp.text


def test_dashboard_feed_empty_state_no_profiles():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Лента пуста" in resp.text
    assert 'href="/profiles/new"' in resp.text


def test_dashboard_feed_empty_state_profiles_without_telegram(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([{"id": "p-1"}]))
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Подключите Telegram" in resp.text
    assert (
        'href="/cabinet/account#kanali" class="btn-primary">Подключить Telegram<'
        in resp.text
    )


def test_dashboard_feed_empty_state_profiles_and_telegram_linked(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([{"id": "p-1"}]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(True)
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "проверяет портал каждые 30 минут" in resp.text


def test_dashboard_todo_uses_default_badge_when_not_urgent(monkeypatch):
    not_urgent = HotDeadline(
        tender_id="t-2",
        title="Реконструкција школе",
        customer_name="Град Крагујевац",
        deadline_at=datetime.now(timezone.utc) + timedelta(days=6),
        tender_url="https://portal.rs/t/2",
        days_left=6,
    )
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_hot_deadlines", _async([not_urgent])
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "badge-new" in resp.text
    assert "🏛 Град Крагујевац" in resp.text


def _feed_item_deadline(days: int | None) -> FeedItem:
    dl = None if days is None else datetime.now(timezone.utc) + timedelta(days=days)
    return FeedItem(
        tender_id="t-dl",
        title="Набавка опреме",
        customer_name="Општина Ниш",
        deadline_at=dl,
        tender_url="https://portal.rs/t/dl",
        profile_id="p-1",
        profile_name="Опрема",
        ai_level=None,
        sent_at=datetime.now(timezone.utc),
        ai_actions=AiActions(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )


def test_dashboard_feed_deadline_human_readable_soon(monkeypatch):
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_feed_items", _async([_feed_item_deadline(5)])
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "осталось 5 дн." in resp.text
    assert "badge-new" in resp.text


def test_dashboard_feed_deadline_urgent_accent(monkeypatch):
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_feed_items", _async([_feed_item_deadline(1)])
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "осталось 1 дн." in resp.text
    assert "badge-urgent" in resp.text


def test_dashboard_feed_deadline_expired(monkeypatch):
    monkeypatch.setattr(
        routes_profiles.feed_service,
        "get_feed_items",
        _async([_feed_item_deadline(-3)]),
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "срок подачи истёк" in resp.text


def test_dashboard_downgrade_notice_shows_plans_gate_cta(monkeypatch):
    import types

    profile = types.SimpleNamespace(id="p-1", name="ИТ набавке", is_active=True)
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([profile]))
    monkeypatch.setattr(
        routes_profiles.svc,
        "downgrade_notice",
        _async(["Своё расписание (Pro+)"]),
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Доступно на Pro" in resp.text
    assert 'href="/plans"' in resp.text


def test_dashboard_teaser_links_tender_to_cabinet_card(monkeypatch):
    """The cockpit teaser does not carry the artifact badges, but keeps the entry to AI analysis:
    the heading leads to the tender card in the cabinet (the full set — on /app/feed).
    """
    item_no_actions = FeedItem(
        tender_id="t-3",
        title="Набавка канцеларијског материјала",
        customer_name="ЈКП Инфостан",
        deadline_at=None,
        tender_url="https://portal.rs/t/3",
        profile_id="p-2",
        profile_name="Канцеларија",
        ai_level=None,
        sent_at=None,
        ai_actions=AiActions(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_feed_items", _async([item_no_actions])
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Набавка канцеларијског материјала" in resp.text
    # Result badges (ready artifacts) are not shown.
    assert "🎯 вердикт" not in resp.text
    assert "📋 чек-лист" not in resp.text
    assert "📝 черновик" not in resp.text
    assert "🧠 анализ" not in resp.text
    assert "все AI-действия" not in resp.text
    # The entry to the tender card in the cabinet (from there — all AI actions) is present.
    assert "/app/tenders/t-3?profile_id=p-2" in resp.text


# --------------------------------------------------------------------------- #
# (VG-28.3a) dashboard — the cockpit hero strip + design-system components
# --------------------------------------------------------------------------- #


def test_days_left_filter_sign_and_none():
    from src.api.templating import _days_left

    assert _days_left(None) is None
    # calendar days in Belgrade time: +3 days by the clock = +3 dates
    assert _days_left(datetime.now(timezone.utc) + timedelta(days=3)) == 3
    assert _days_left(datetime.now(timezone.utc) - timedelta(days=2)) < 0
    # a naive datetime is treated as UTC (no failure)
    naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    assert _days_left(naive) >= 1


def test_dashboard_shows_cockpit_hero_summary():
    """The first screen — hero “My week”: three tiles (v2, owner 13.10)."""
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Моя неделя" in resp.text
    assert "Истекает за 7 дней" in resp.text
    assert "Новое для вас, 7 дней" in resp.text
    assert "В работе" in resp.text
    assert "доставлено вам" not in resp.text


def test_dashboard_uses_design_system_components_not_raw_slate():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "bg-slate-900" not in resp.text
    assert "btn-primary" in resp.text
    assert 'class="card' in resp.text or " card " in resp.text
    assert "badge-" in resp.text


# --------------------------------------------------------------------------- #
# (VG-30b) dashboard — the onboarding stepper “Getting started”
# --------------------------------------------------------------------------- #


def test_onboarding_steps_0_of_3_first_step_active():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Начало работы" in resp.text
    assert "0 из 3" in resp.text
    assert 'href="/profiles/new" class="btn-primary"' in resp.text
    assert 'href="/cabinet/account#kanali" class="btn-secondary"' in resp.text
    assert "is-pending" in resp.text
    assert "is-done" not in resp.text


def test_onboarding_steps_1_of_3_second_step_active(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([{"id": "p-1"}]))
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "1 из 3" in resp.text
    assert "is-done" in resp.text
    assert 'href="/cabinet/account#kanali" class="btn-primary"' in resp.text
    assert "is-pending" in resp.text


def test_onboarding_steps_2_of_3_third_step_active(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([{"id": "p-1"}]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(True)
    )
    # The delivery channel is connected (a live user_channels) → step 2 done.
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": True, "gaps": [], "channelless": []}),
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "2 из 3" in resp.text
    assert "Получить первый алерт" in resp.text
    assert "Придёт автоматически после совпадения тендера" in resp.text
    assert "is-pending" not in resp.text


def test_onboarding_steps_hidden_when_all_3_done(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([{"id": "p-1"}]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(True)
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": True, "gaps": [], "channelless": []}),
    )
    monkeypatch.setattr(
        routes_profiles.feed_service, "get_feed_items", _async([_FEED_ITEM_FULL])
    )
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert "Начало работы" not in resp.text


# --------------------------------------------------------------------------- #
# (VG-30) contextual “?” hints (single component _hint_tip.html)
# --------------------------------------------------------------------------- #


def _tip_classes_defined_in_app_css() -> set[str]:
    css = _APP_CSS.read_text(encoding="utf-8")
    return {cls for cls in ("tip", "tip__icon", "tip__bubble") if f".{cls}" in css}


def test_hint_tip_component_classes_defined_in_app_css():
    """The classes of the single hint component are declared in app.css (otherwise the markup
    will render, but the styles will silently not apply)."""
    assert _tip_classes_defined_in_app_css() == {"tip", "tip__icon", "tip__bubble"}


def test_dashboard_renders_contextual_tooltips():
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    # the component fits: trigger button + bubble with role=tooltip
    assert 'class="tip"' in resp.text
    assert 'class="tip__icon"' in resp.text
    assert 'role="tooltip"' in resp.text
    # the hint is attached to the headings of the key cabinet sections
    assert "Сроки, новые тендеры и тендеры в работе" in resp.text
    assert "фильтр тендеров" in resp.text


def test_tip_trigger_is_focusable_button_for_accessibility():
    """The hint trigger is a real <button type=button> (keyboard focus,
    accessibility), not a “dead” span with hover only."""
    resp = _authed_dashboard_client().get("/app")
    assert resp.status_code == 200
    assert '<button type="button" class="tip__icon"' in resp.text
    assert "aria-label=" in resp.text


def test_tender_detail_renders_contextual_tooltips(monkeypatch):
    """The card by canonical UUID (08.10: a non-UUID id now gives 404, see routes_tender_ai)."""
    import types

    _TID = "22222222-2222-2222-2222-222222222222"

    from src.api.cabinet import routes_tender_ai

    data = types.SimpleNamespace(
        title="Набавка рачунара",
        description=None,
        customer_name="Општина Нови Сад",
        deadline_at=None,
        profile_name="ИТ набавке",
        source_url="https://portal.rs/t/1",
        tender_id=_TID,
        profile_id="p-1",
        has_any=True,
        bid=None,
        checklist=None,
        draft=None,
        deep_analysis=None,
    )
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))
    monkeypatch.setattr(routes_tender_ai.cust, "get_narucilac_analytics", _async(None))
    client = _client(sess=SESS)
    client.cookies.set("tb_session", _session_cookie())
    resp = client.get(f"/app/tenders/{_TID}?profile_id=p-1")
    assert resp.status_code == 200
    assert 'class="tip__icon"' in resp.text
    assert "AI-вердикт: участвовать или пропустить" in resp.text


def test_agenda_row_stacks_on_mobile():
    """Guard: on mobile the agenda row lays out in TWO rows, not three columns.

    Measurement on live production 10.08.2026 (viewport 390, tenant with a real feed):
    date 58px + badge “N d. left” 105px — fixed columns, the body had 109px out of 324, the heading-link got 84px and fell apart into FIVE lines of one-two words; median row height 279px vs 123px on desktop, the whole agenda — 28 296px. After the fix (checked by substituting CSS in the browser on the same page): body 296px, heading 2 lines, row 176px, agenda 18 769px, still no overflow.

    The expectation is taken from the MEASUREMENT, not from the model of the code: the test pins the PRESENCE of the rule, because a rollback of the layout by numbers is not caught in CI — there is no browser there.
    """
    css = _APP_CSS.read_text(encoding="utf-8")
    mobile_blocks = re.findall(
        r"@media\s*\(max-width:\s*639px\)\s*\{(.*?)\n\}", css, re.S
    )
    stacking = [
        b
        for b in mobile_blocks
        if ".agenda__item" in b and "grid" in b and ".agenda__body" in b
    ]
    assert stacking, (
        "no mobile layout rule for .agenda__item — the tender heading "
        "will go back to an 84px column and fall apart into a column"
    )
    block = stacking[0]
    assert "grid-column: 1 / -1" in block, (
        ".agenda__body must take the whole second row — otherwise the body again "
        "shares the width with the date and the badge"
    )
