"""Cabinet menu: 4 access states × sr (owner 10.10.2026, cabinet IA).

Expectations — literals (which links are there, where the plan label is, the renewal button), not via ``t()``. The plan label is visible ONLY if there is no access — by the same gates that close the pages (``nav_access``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import analitika_service, narucilac_page_service
from src.api.cabinet.analitika_service import AnalitikaPageView
from src.api.cabinet.narucilac_page_service import NarucilacPageView
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.api.nav_access import resolve_nav_access
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_NOW = datetime.now(timezone.utc)


def teardown_function():
    app.dependency_overrides.clear()


def _client(*, tier, status, until):
    fs = FakeSession(
        results=[], tenant_tier=tier, tenant_status=status, access_until=until
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    c = TestClient(app, follow_redirects=False)
    secret = get_cabinet_settings().cabinet_secret_key
    c.cookies.set("tb_session", security.sign_session(_UID, _TID, secret, locale="sr"))
    return c


def _header(tier, status, until) -> str:
    r = _client(tier=tier, status=status, until=until).get("/help?lang=sr")
    assert r.status_code == 200
    return re.search(r"<header.*?</header>", r.text, re.S).group(0)


# (tier, status, access_until) → badge, the Naručioci label, the Analitika label
CASES = {
    "trial": (
        ("lite", "trial", _NOW + timedelta(days=21) - timedelta(hours=1)),
        "Probni · 21 d",
        None,
        None,
    ),
    "pro": (
        ("pro", "active", _NOW + timedelta(days=12) - timedelta(hours=1)),
        "Pro · 12 d",
        None,
        "BUSINESS",
    ),
    "business": (
        ("business", "active", _NOW + timedelta(days=40) - timedelta(hours=1)),
        "Business · 40 d",
        None,
        None,
    ),
    "free": (("lite", "active", None), "Free", "PRO", "BUSINESS"),
    "frozen": (
        ("pro", "active", _NOW - timedelta(days=3)),
        "Pauzirano",
        "Pauzirano",
        "Pauzirano",
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_menu_badge_and_tool_labels_per_state(name):
    args, badge, nar, ana = CASES[name]
    head = _header(*args)
    assert (
        f'data-access-state="{ "active" if name in ("pro", "business") else name }">{badge}<'
        in head
    )
    tools = head.split('<details class="nav-more nav-hide-mobile">')[1].split(
        "</details>"
    )[0]
    labels = re.findall(
        r'href="(/[^"]+)" class="nav-link[^"]*">([^<]*)(?:\s*<span[^>]*data-tool-label[^>]*>([^<]*)</span>)?',
        tools,
    )
    by_href = {h: lab for h, _, lab in labels}
    assert by_href["/cabinet/narucilac"] == (nar or "")
    assert by_href["/app/analitika"] == (ana or "")
    # we do not hide the items in any state
    assert set(by_href) == {
        "/tenderi",
        "/cabinet/narucilac",
        "/app/analitika",
        "/library",
        "/cpv-kodovi",
    }
    assert by_href["/library"] == "" and by_href["/tenderi"] == ""
    assert by_href["/cpv-kodovi"] == ""


@pytest.mark.parametrize("name", list(CASES))
def test_menu_first_row_links_and_no_cyrillic_on_sr(name):
    head = _header(*CASES[name][0])
    for href in ("/app", "/app/feed", "/app/my", "/help", "/cabinet/account"):
        assert f'href="{href}"' in head, href
    for gone in (
        "/plans",
        "/izvori",
        "/novosti",
        "/cabinet/telegram",
        "/cabinet/email",
        "/cabinet/subscription",
    ):
        assert f'href="{gone}"' not in head, gone
    assert "Moj kabinet" in head and "Alati ▾" in head and "Meni ▾" in head
    assert "Moj nalog" not in head
    assert not re.search(r"[А-Яа-яЁё]", re.sub(r"<[^>]+>", " ", head))
    # three groups of the mobile menu
    for g in ("Svakog dana", "Alati", "Nalog"):
        assert f'<p class="nav-group-title">{g}</p>' in head


def test_state_resolution_literals():
    s = resolve_nav_access("trial", "lite", _NOW + timedelta(days=2), _NOW)
    assert (s.state, s.plan_key, s.days) == ("trial", "pro", 2)
    # trial = Pro: Naručioci is open, Analitika on the trial is open (owner 10.10)
    assert s.narucilac_label is None and s.analitika_label is None
    s = resolve_nav_access("active", "pro", _NOW - timedelta(days=1), _NOW)
    assert (s.state, s.narucilac_label, s.analitika_label) == (
        "frozen",
        "paused",
        "paused",
    )
    s = resolve_nav_access("active", "lite", None, _NOW)
    assert (s.state, s.days, s.narucilac_label, s.analitika_label) == (
        "free",
        None,
        "pro",
        "business",
    )


def test_footer_has_plans_sources_news_for_cabinet_pages():
    r = _client(tier="pro", status="active", until=None).get("/cabinet/account")
    foot = r.text.split("<footer")[1]
    for href in ("/plans", "/izvori", "/novosti"):
        assert f'href="{href}"' in foot


def test_delete_account_button_is_danger():
    r = _client(tier="pro", status="active", until=None).get("/cabinet/account")
    assert re.search(
        r'<button type="submit" class="btn-danger">[^<]+</button>\s*</form>\s*</section>',
        r.text,
    )


@pytest.mark.parametrize(
    "path,mod,fn,view",
    [
        (
            "/app/analitika",
            analitika_service,
            "get_analitika_page",
            AnalitikaPageView(status="frozen"),
        ),
        (
            "/cabinet/narucilac",
            narucilac_page_service,
            "get_narucilac_page",
            NarucilacPageView(status="frozen"),
        ),
    ],
)
def test_frozen_empty_state_has_single_free_extension_button(
    monkeypatch, path, mod, fn, view
):
    async def _stub(*a, **k):
        return view

    monkeypatch.setattr(mod, fn, _stub)
    r = _client(tier="pro", status="active", until=_NOW - timedelta(days=3)).get(
        path + "?lang=sr"
    )
    assert r.status_code == 200
    main = r.text.split('<main id="main"')[1]
    assert main.count('action="/cabinet/trial/request"') == 1
    assert "Produži besplatno" in main
    assert "Produži plan" not in r.text
