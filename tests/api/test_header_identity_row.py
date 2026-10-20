"""The login row and the main row of the cabinet header (owner 11.10.2026, header-identity-hub).

Expectations — literals from the fact (what the client sees), not via ``t()``.
- In the main row (before the mobile menu “Meni”) there is NO link to the hub: two entries to one
  place widened the menu.
- In the login row there IS a link “Moj kabinet:” to /cabinet/account and a status badge to
  /cabinet/account#status.
- In the mobile menu the group “Nalog” contains “Moj kabinet”.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.api.nav_access import resolve_nav_access
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_NOW = datetime.now(timezone.utc)
_REAL_WHOAMI = deps.resolve_cabinet_whoami


_CASE: dict = {}


async def _whoami_dep(request: Request):
    # As resolve_cabinet_whoami in production: email + tenant + the access state of the badge.
    tier, status, until = _CASE["case"]
    now = datetime.now(timezone.utc)
    request.state.cabinet_whoami_email = "client@example.rs"
    request.state.cabinet_whoami_tenant_name = "ДОО Пример"
    request.state.cabinet_nav_access = resolve_nav_access(status, tier, until, now)
    request.state.cabinet_access_frozen = (
        status == "active" and until is not None and until < now
    )


async def _whoami_direct(request, session=None, session_factory=None):
    await _whoami_dep(request)


@pytest.fixture(autouse=True)
def _whoami(monkeypatch):
    # The login row is drawn only with whoami (email + tenant): we substitute the resolve,
    # as scripts/shots/stand_nav.py does. Both paths — Depends and a direct call.
    app.dependency_overrides[_REAL_WHOAMI] = _whoami_dep
    monkeypatch.setattr(deps, "resolve_cabinet_whoami", _whoami_direct)
    yield
    app.dependency_overrides.clear()


def _header(tier, status, until) -> str:
    _CASE["case"] = (tier, status, until)
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
    r = c.get("/help?lang=sr")
    assert r.status_code == 200
    return re.search(r"<header.*?</header>", r.text, re.S).group(0)


def _identity(head: str) -> str:
    return head.split('<div class="site-header__identity')[1].split(
        '<div class="site-header__bar'
    )[0]


def _main_row(head: str) -> str:
    # The main row — before the mobile menu “Meni” (there are copies of the items for the phone).
    return head.split('<nav class="site-header__actions')[1].split(
        '<details class="nav-more nav-mobile">'
    )[0]


TRIAL = ("lite", "trial", _NOW + timedelta(days=21) - timedelta(hours=1))
FROZEN = ("pro", "active", _NOW - timedelta(days=3))


@pytest.mark.parametrize("case", [TRIAL, FROZEN], ids=["trial", "frozen"])
def test_main_row_has_no_cabinet_link(case):
    head = _header(*case)
    assert 'href="/cabinet/account"' not in _main_row(head)
    assert "data-access-state" not in _main_row(head)


def test_identity_row_has_hub_link_and_status_badge_trial():
    ident = _identity(_header(*TRIAL))
    assert 'href="/cabinet/account"' in ident
    assert "Moj kabinet:" in ident
    assert 'href="/cabinet/account#status"' in ident
    assert 'data-access-state="trial"' in ident
    assert "Probni · 21 d" in ident


def test_identity_row_frozen_badge_is_pauzirano():
    ident = _identity(_header(*FROZEN))
    assert 'href="/cabinet/account#status"' in ident
    assert 'data-access-state="frozen"' in ident
    assert "Pauzirano" in ident


def test_mobile_menu_nalog_group_has_cabinet_link():
    head = _header(*TRIAL)
    mobile = head.split('<details class="nav-more nav-mobile">')[1]
    group = mobile.split('<p class="nav-group-title">Nalog</p>')[1]
    assert group.split("</details>")[0].count('href="/cabinet/account"') == 1
    assert ">Moj kabinet</a>" in group
