"""The “Moj kabinet” hub (owner 10.10.2026): one page instead of four.

The expectations are literals (section ids, badges, buttons, Location), not via ``t()``.
The rendering runs under a real signed cookie (sr), the access state is taken by the same
``nav_access`` as the header. The hub services run on an empty FakeSession (no substitutions):
an empty tenant must render too.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_NOW = datetime.now(timezone.utc)

_SECTIONS = ("status", "kanali", "sabloni", "pretplata", "podrska", "nalog")


def teardown_function():
    app.dependency_overrides.clear()


def _client(*, tier="pro", status="active", until=None, sess=True):
    fs = FakeSession(
        results=[],
        tenant_tier=tier,
        tenant_status=status,
        access_until=until,
        user_locale="sr",
        answers_tenant_tier=True,
    )
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: (
        {"uid": _UID, "tid": _TID} if sess else None
    )
    c = TestClient(app, follow_redirects=False)
    secret = get_cabinet_settings().cabinet_secret_key
    c.cookies.set("tb_session", security.sign_session(_UID, _TID, secret, locale="sr"))
    return c


# (tier, status, access_until) → the Status badge, as in the header (_access_badge.html)
STATES = {
    "trial": (("lite", "trial", _NOW + timedelta(days=21, hours=-1)), "Probni · 21 d"),
    "pro": (("pro", "active", _NOW + timedelta(days=12, hours=-1)), "Pro · 12 d"),
    "free": (("lite", "active", None), "Free"),
    "frozen": (("pro", "active", _NOW - timedelta(days=3)), "Pauzirano"),
}


def _hub(name):
    tier, status, until = STATES[name][0]
    r = _client(tier=tier, status=status, until=until).get("/cabinet/account")
    assert r.status_code == 200
    return r.text


@pytest.mark.parametrize("name", list(STATES))
def test_hub_has_all_sections_in_frequency_order_in_every_state(name):
    html = _hub(name)
    ids = re.findall(r'\sid="(%s)"' % "|".join(_SECTIONS), html)
    assert ids == list(
        _SECTIONS
    )  # order: Status → Kanali → Šabloni → Pretplata → Podrška → Nalog
    # anchor navigation — plain <a href="#…">, without ?tab= (GET mutates in our app)
    nav = re.search(r'<nav class="mb-6[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r'href="([^"]+)"', nav) == [f"#{i}" for i in _SECTIONS]
    assert "?tab=" not in html
    for label in ("Status", "Kanali", "Šabloni", "Pretplata i limiti", "Nalog"):
        assert label in html


@pytest.mark.parametrize("name", list(STATES))
def test_status_section_shows_access_badge_via_shared_partial(name):
    html = _hub(name)
    status = re.search(r'id="status".*?</section>', html, re.S).group(0)
    state = "active" if name == "pro" else name
    assert f'data-access-state="{state}">{STATES[name][1]}<' in status


@pytest.mark.parametrize("name", list(STATES))
def test_frozen_has_exactly_one_free_extension_button_and_others_none(name):
    html = _hub(name)
    n = html.count("data-extension-form")
    assert n == (1 if name == "frozen" else 0)
    assert html.count("Produži besplatno") == n


@pytest.mark.parametrize("name", list(STATES))
def test_channels_section_has_telegram_and_email_actions(name):
    html = _hub(name)
    kanali = re.search(r'id="kanali".*?</section>', html, re.S).group(0)
    # Telegram not connected → the “connect” form; Email not connected → the binding form
    assert 'action="/cabinet/telegram/link#kanali"' in kanali
    assert 'action="/cabinet/email/link"' in kanali
    assert "Povežite Telegram" in kanali
    assert 'name="email"' in kanali


@pytest.mark.parametrize("name", list(STATES))
def test_sr_render_has_no_cyrillic(name):
    html = _hub(name)
    visible = re.sub(
        r"<[^>]+>", " ", re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)
    )
    assert not re.search(r"[Ѐ-ӿ]", visible)


@pytest.mark.parametrize("name", list(STATES))
def test_summary_is_first_child_of_every_details(name):
    html = _hub(name)
    opened = re.findall(r"<details\b[^>]*>(.{0,40})", html, re.S)
    assert opened, "there is not a single <details> in the hub"
    for chunk in opened:
        assert chunk.lstrip().startswith("<summary"), chunk


def test_no_inline_js_in_hub_markup():
    html = _hub("pro")
    assert not re.search(r"\bon(click|submit|change|load)\s*=", html)
    for body in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S):
        assert body.strip() == ""


def test_nalog_section_has_logout_post_language_and_danger_delete():
    html = _hub("pro")
    nalog = re.search(r'id="nalog".*?</section>', html, re.S).group(0)
    assert 'action="/logout"' in nalog
    assert 'action="/cabinet/locale"' in nalog
    delete = html.split('id="acct-delete"')[1]
    assert 'action="/cabinet/account/delete"' in delete
    assert 'class="btn-danger"' in delete


def test_tap_targets_in_anchor_nav_use_button_class():
    """Tap zone ≥2rem: the chips are .btn-secondary (padding .5rem + line-height 1.25rem)."""
    html = _hub("pro")
    nav = re.search(r'<nav class="mb-6[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert nav.count('class="btn-secondary"') == 6


# --------------------------------------------------------------------------- #
# Old addresses → 303 to the hub anchors; anonymous — to login with a return to the same address
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "old,new",
    [
        ("/cabinet/telegram", "/cabinet/account#kanali"),
        ("/cabinet/email", "/cabinet/account#kanali"),
        ("/cabinet/subscription", "/cabinet/account#pretplata"),
    ],
)
def test_old_addresses_redirect_303_to_hub_anchor(old, new):
    r = _client().get(old)
    assert r.status_code == 303  # NOT 301: it is cached forever
    assert r.headers["location"] == new


@pytest.mark.parametrize(
    "old",
    ["/cabinet/telegram", "/cabinet/email", "/cabinet/subscription"],
)
def test_anonymous_goes_to_login_back_to_old_address_which_leads_to_hub(old):
    r = _client(sess=False).get(old)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=" + old.replace("/", "%2F")
    # after login the browser goes to next → the same 303 to the hub
    assert _client().get(old).headers["location"].startswith("/cabinet/account#")


def test_revoked_user_destination_is_not_the_gated_hub():
    """Revoked/deleted → ``SUBSCRIPTION_PATH``; this is NOT the hub: the hub is closed by the same
    gate, a 303 to it would loop (mutation: return ``/cabinet/account``)."""
    assert deps.SUBSCRIPTION_PATH == "/plans"
    fs = FakeSession(user_deleted_at=_NOW - timedelta(days=1))
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    c = TestClient(app, follow_redirects=False)
    r = c.get("/cabinet/account")
    assert r.status_code == 303 and r.headers["location"] == "/plans"
    assert c.get("/plans").status_code == 200  # the destination is not closed by the gate


def test_internal_links_use_hub_anchors_not_old_pages():
    """No template/service of the cabinet refers to the three old GET addresses."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "api"
    pat = re.compile(r'["\']/cabinet/(telegram|email|subscription)["\'#?]')
    bad = []
    for path in list(root.rglob("*.html")) + list(root.rglob("*.py")):
        if path.name in ("routes_channels.py", "routes_subscription.py", "i18n.py"):
            continue
        for m in pat.finditer(path.read_text(encoding="utf-8")):
            bad.append((path.name, m.group(0)))
    assert bad == []


def test_frozen_with_can_request_still_shows_only_the_banner_button(monkeypatch):
    """Expired access + can_request=True: the “access request” form from Status is not
    drawn — otherwise a second renewal button next to the banner (mutation: remove
    ``and not access_frozen`` in account.html)."""
    from src.api.cabinet import routes_account
    from src.api.cabinet.trial_service import AccessStatus

    async def _status(*a, **k):
        return AccessStatus(state="expired", active=False, can_request=True, until=None)

    monkeypatch.setattr(routes_account.trial_service, "get_access_status", _status)
    html = _hub("frozen")
    assert html.count('action="/cabinet/trial/request"') == 1
    assert html.count("data-extension-form") == 1


def _pretplata_block(html):
    """The slice of the hub’s “Pretplata” section — from ``id=“pretplata”`` to ``id=“nalog”``."""
    start = html.index('id="pretplata"')
    end = html.index('id="nalog"', start)
    return html[start:end]


def test_manual_pro_without_subscription_shows_pro_not_free():
    """tier=pro without a row in billing_subscriptions (access granted manually):
    Status writes “Pro”, the subscription block must too — without “Besplatno”/“Na besplatnom”
    (mutation: return ``plan_name=“Free”`` in the branch without a subscription → red)."""
    block = _pretplata_block(
        _client(tier="pro", status="active").get("/cabinet/account").text
    )
    assert "Besplatno" not in block
    assert "Na besplatnom" not in block
    assert re.search(r">\s*Pro\s*<", block)
    assert "Pristup je dodeljen ručno" in block


def test_free_tenant_without_subscription_still_shows_free():
    block = _pretplata_block(
        _client(tier="lite", status="active").get("/cabinet/account").text
    )
    assert "Besplatno" in block
    assert "Na besplatnom ste planu" in block
    assert "Izaberite plan" in block
