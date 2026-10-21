"""Navigation of long pages (portal, Stage 9).

We check exactly what fixes the complaint “from the bottom to the navigation — a long scroll”: the sticky header, the “to top” button (markup + connection + progressive enhancement), the scroll-margin of the anchors, and that the tail of the dashboard settings is collapsed.

The styles here are not cosmetics but behavior: without ``position: sticky`` the header drifts away, without ``scroll-margin-top`` an anchor lands UNDER the header, without the z-index order the “More” dropdown hides behind the content. Therefore the CSS is checked by the file, not by eye.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import channel_service, routes_profiles
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.api.public.i18n import UI

_ROOT = Path(__file__).resolve().parents[2]
_CSS = (_ROOT / "src" / "api" / "static" / "css" / "app.css").read_text(
    encoding="utf-8"
)
_JS = (_ROOT / "src" / "api" / "static" / "js" / "scroll_nav.js").read_text(
    encoding="utf-8"
)
_TEMPLATES = _ROOT / "src" / "api" / "templates"

SESS = {"uid": "u-1", "tid": "t-1"}


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


def _authed():
    client = _client(sess=SESS)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key),
    )
    return client


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


def _rule(selector: str) -> str:
    """All declarations of the selector, glued in cascade order.

    ‼️ All of them, not the first occurrence: `.site-header` is declared twice (the base background/border in the frame + the stickiness in the Stage 9 block), and a check by the first rule would silently read the old one.
    """
    bodies = re.findall(
        r"(?:^|\n)" + re.escape(selector) + r"\s*\{([^}]*)\}", _CSS, flags=re.S
    )
    assert bodies, f"rule {selector} not found in app.css"
    return "\n".join(bodies)


# --------------------------------------------------------------------------- #
# (a) the sticky header
# --------------------------------------------------------------------------- #


def test_site_header_is_sticky_at_top():
    """The header is pinned to the top — the navigation is at hand on any page length."""
    body = _rule(".site-header")
    assert "position: sticky" in body
    assert "top: 0" in body


def test_site_header_stacks_above_content_and_carries_the_more_dropdown():
    """The z-index of the header is above the “More” dropdown: the menu lies INSIDE the header, therefore it floats together with it and cannot end up behind the content."""
    header_z = int(re.search(r"z-index:\s*(\d+)", _rule(".site-header")).group(1))
    menu_z = int(re.search(r"z-index:\s*(\d+)", _rule(".nav-more__menu")).group(1))
    tip_z = int(re.search(r"z-index:\s*(\d+)", _rule(".tip__bubble")).group(1))
    assert header_z > menu_z > tip_z


def test_translucent_header_has_opaque_fallback_without_backdrop_filter():
    """Without backdrop-filter a semi-transparent background would give see-through text."""
    assert "backdrop-filter" in _rule(".site-header")
    assert "@supports not ((backdrop-filter" in _CSS


def test_header_is_compact_on_narrow_screens():
    """On a narrow screen the cabinet navigation wraps — the header must not eat half the screen, and --header-h must grow to its real height.

    We look for the RULE among all the mobile blocks, not the first one that comes along: there are several `max-width: 639px` blocks in the file (the agenda is laid out there too), and `re.search` pinned not the contract but the ORDER of the blocks in app.css — a new block higher in the file painted the test red without breaking anything in the product.
    """
    narrow_blocks = re.findall(
        r"@media \(max-width: 639px\) \{(.*?)\n\}", _CSS, flags=re.S
    )
    assert any(
        "--header-h" in b and ".site-header .nav-link" in b for b in narrow_blocks
    ), "there is no mobile rule of the compact header: --header-h + .site-header .nav-link"


# --------------------------------------------------------------------------- #
# (b) the anchors do not go under the header
# --------------------------------------------------------------------------- #


def test_anchors_get_scroll_margin_from_header_height_token():
    """scroll-margin is computed from --header-h, not from a magic number: the header’s rhythm was changed — the anchors moved with it, not past it."""
    assert "--header-h:" in _CSS
    body = _rule("[id]")
    assert "scroll-margin-top" in body
    assert "var(--header-h)" in body


def test_smooth_scroll_enabled_and_disabled_under_reduced_motion():
    assert re.search(r"\nhtml \{ scroll-behavior: smooth; \}", _CSS)
    reduce_blocks = re.findall(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", _CSS, flags=re.S
    )
    assert any("scroll-behavior: auto" in b for b in reduce_blocks)


# --------------------------------------------------------------------------- #
# (c) the “To top” button: markup, accessibility, progressive enhancement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", ["/app", "/app/feed"])
def test_back_to_top_button_present_on_long_cabinet_pages(path, monkeypatch):
    monkeypatch.setattr(routes_profiles.feed_service, "get_feed_page", _async([]))
    resp = _authed().get(path)
    assert resp.status_code == 200
    assert 'class="to-top" data-scroll-top aria-label="Наверх"' in resp.text


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_back_to_top_button_present_on_public_pages(lang):
    """The landing and the plans are also long — the behavior is uniform.

    The button’s caption comes from the UI catalog (M-7 of the audit package 2): previously public_base.html had the Russian literal (U+041D U+0430 U+0432 U+0435 U+0440 U+0445) in ALL three languages.
    """
    resp = _client().get(f"/plans?lang={lang}")
    assert resp.status_code == 200
    assert (
        f'class="to-top" data-scroll-top aria-label="{UI[lang]["to_top"]}"' in resp.text
    )


def test_back_to_top_is_a_real_focusable_button_not_a_dead_div():
    """Accessibility: a real <button> with aria-label and focus-ring; the arrow is decoration (aria-hidden), otherwise a screen reader would read “arrow up”."""
    html = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '<button type="button" class="to-top"' in html
    # aria-label is localised through the cabinet catalog (i18n Phase 1.1): t.to_top.
    assert 'aria-label="{{ t.to_top }}"' in html
    assert '<span aria-hidden="true">↑</span>' in html
    assert ".to-top:focus-visible" in _CSS


def test_back_to_top_hidden_until_js_toggles_it():
    """Progressive enhancement: the class is-visible is set only by JS. Without it the button is hidden via visibility — NOT visible and NOT in the tab order."""
    body = _rule(".to-top")
    assert "position: fixed" in body
    assert "visibility: hidden" in body
    assert "opacity: 0" in body
    assert ".to-top.is-visible" in _CSS


def test_back_to_top_respects_safe_area_and_stays_under_the_header():
    """It does not climb under the iOS system bar and goes UNDER the header, not over it."""
    body = _rule(".to-top")
    assert "env(safe-area-inset-bottom" in body
    button_z = int(re.search(r"z-index:\s*(\d+)", body).group(1))
    header_z = int(re.search(r"z-index:\s*(\d+)", _rule(".site-header")).group(1))
    assert button_z < header_z


def test_scroll_nav_js_wired_with_sri_and_defer_in_both_layouts():
    for name in ("base.html", "public_base.html"):
        html = (_TEMPLATES / name).read_text(encoding="utf-8")
        match = re.search(
            r'<script src="\{\{ static_url\(\'js/scroll_nav\.js\'\) \}\}"[^>]*>',
            html,
            flags=re.S,
        )
        assert match, f"scroll_nav.js is not connected in {name}"
        tag = match.group(0)
        assert 'integrity="sha384-' in tag, f"no SRI in {name}"
        assert "defer" in tag
    # the actual hash is checked by test_local_script_sri_matches_files


def test_scroll_nav_js_is_dependency_free_and_reduced_motion_aware():
    """GSAP is not needed for it (it works also under reduced-motion), the scroll is native, the handler is throttled+passive: the scrolling does not jerk."""
    assert "gsap" not in _JS
    assert "prefers-reduced-motion: reduce" in _JS
    assert 'reduceMotion ? "auto" : "smooth"' in _JS
    assert "requestAnimationFrame" in _JS
    assert "{ passive: true }" in _JS


def test_scroll_nav_js_moves_focus_to_top_after_scrolling():
    """Otherwise a keyboard user after scrolling would tab from the very bottom."""
    assert "focus({ preventScroll: true })" in _JS


# --------------------------------------------------------------------------- #
# (d) the dashboard is shorter: the tail of the settings went to the cabinet, the essence is in view
# --------------------------------------------------------------------------- #


def _connected_dashboard(monkeypatch):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([{"id": "p-1"}]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(True)
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async({"has_live_channel": True, "gaps": [], "channelless": []}),
    )
    return _authed().get("/app")


def test_dashboard_has_no_settings_tail(monkeypatch):
    """owner 22.09: the tail “Settings and status” (Telegram, test access) went to the cabinet — there is no spoiler any more. v2 (owner 13.10): “Site statistics” is removed too."""
    resp = _connected_dashboard(monkeypatch)
    assert resp.status_code == 200
    assert '<details class="ck-more"' not in resp.text
    assert "Статистика площадки" not in resp.text
    assert "Тестовый доступ" not in resp.text


def test_dashboard_keeps_profiles_visible(monkeypatch):
    """Profiles — the essence of the cockpit (“what we search for you”), not moved under a spoiler."""
    resp = _connected_dashboard(monkeypatch)
    assert "Мои шаблоны" in resp.text
    assert 'id="profiles"' in resp.text


def test_dashboard_channel_cta_leads_to_channel_page():
    """‼️ There is no Telegram card on the dashboard — the anchor #telegram-card would lead to
    emptiness. The onboarding leads to the channel page in the cabinet."""
    resp = _authed().get("/app")
    assert resp.status_code == 200
    assert "#telegram-card" not in resp.text
    assert 'href="/cabinet/account#kanali" class="btn-secondary"' in resp.text


def test_channel_bind_links_lead_somewhere():
    """The nudge “connect a channel” (_delivery_gap.html) takes the address from
    CHANNEL_BIND_LINKS. The anchor must exist on the dashboard, otherwise the click goes
    into emptiness: the card #telegram-card is removed (owner 22.09), the link goes to the channel page. Mutation: return ``“#telegram-card”`` — the test goes red.
    """
    resp = _authed().get("/app")
    assert resp.status_code == 200
    for link in channel_service.CHANNEL_BIND_LINKS.values():
        if link.startswith("#"):
            assert (
                f'id="{link[1:]}"' in resp.text
            ), f"the anchor {link} is not on the dashboard"
        else:
            assert link in ("/cabinet/account#kanali",)


def test_header_h_js_wired_sync_after_header_with_sri_in_both_layouts():
    """header_h.js sets --header-h by the actual height of the header: connected in both
    layouts with SRI, WITHOUT defer (before the render of <main>), strictly after the header.
    """
    for name in ("base.html", "public_base.html"):
        html = (_TEMPLATES / name).read_text(encoding="utf-8")
        match = re.search(
            r'<script src="\{\{ static_url\(\'js/header_h\.js\'\) \}\}"[^>]*>', html
        )
        assert match, f"header_h.js is not connected in {name}"
        tag = match.group(0)
        assert 'integrity="sha384-' in tag, f"no SRI in {name}"
        assert "defer" not in tag
        assert match.start() < html.index("<main"), name
        assert match.start() > html.index("_cabinet_nav.html"), name


def test_header_h_js_observes_header_and_sets_css_variable():
    js = (_ROOT / "src" / "api" / "static" / "js" / "header_h.js").read_text(
        encoding="utf-8"
    )
    assert '".site-header"' in js
    assert "ResizeObserver" in js
    assert 'setProperty("--header-h"' in js
