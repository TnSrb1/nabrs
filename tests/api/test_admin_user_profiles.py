"""Templates (profiles) of the subscriber on the admin card /admin/users/{uid}.

No DB (ADR-013): a mock session and `dependency_overrides`, as in `test_admin.py`.
The tenant filter — in `svc.list_profiles(tenant_id=...)`; the SQL selection is shared with
/admin/templates (SECURITY DEFINER `app_admin_list_profiles`, migration 0025).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.config import CabinetSettings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory
from tests.api.test_admin import _client_admin

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_OTHER_TID = "cccc0000-0000-0000-0000-000000000003"
_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
_OPERATOR_EMAIL = "admin@example.com"
_ADMIN_SESSION = {"uid": _UID, "tid": _TID}


def _operator_settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def _user_row(profile_count: int):
    return SimpleNamespace(
        id=_UID,
        email="subscriber@example.com",
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=None,
        profile_count=profile_count,
    )


def _profile(name: str, *, tenant_id: str, is_active: bool, match_7d: int = 0):
    return SimpleNamespace(
        id=f"pid-{name}",
        name=name,
        tenant_id=tenant_id,
        is_active=is_active,
        owner_email="subscriber@example.com",
        match_7d=match_7d,
        created_at=_NOW,
        delivery_channels=["telegram"] if is_active else [],
    )


def _client(profile_rows, *, profile_count: int):
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),
        FakeResult(items=[_user_row(profile_count)]),
        FakeResult(scalar=0),  # get_user_delivered_count
        FakeResult(items=profile_rows),  # app_admin_list_profiles()
        FakeResult(items=[]),  # app_admin_user_channels()
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _operator_settings
    return TestClient(app, follow_redirects=False)


def _section(html: str) -> str:
    """The “Templates” block of the card: from the #profiles anchor to the next block of the card."""
    start = html.index('id="profiles"')
    return html[start : html.index("<!-- Продлить доступ -->", start)]


def teardown_function():
    app.dependency_overrides.clear()


def test_active_and_inactive_split_and_foreign_tenant_excluded():
    rows = [
        _profile("Активный-А", tenant_id=_TID, is_active=True, match_7d=5),
        _profile("Активный-Б", tenant_id=_TID, is_active=True, match_7d=2),
        _profile("Выключенный-В", tenant_id=_TID, is_active=False, match_7d=0),
        _profile("Чужой-Г", tenant_id=_OTHER_TID, is_active=True, match_7d=9),
    ]
    client = _client(rows, profile_count=3)
    resp = client.get(f"/admin/users/{_UID}")
    assert resp.status_code == 200
    html = resp.text

    section = _section(html)
    assert "Активных: 2 · выключенных: 1" in section
    assert "Активный-А" in section and "Активный-Б" in section
    assert "Выключенный-В" in section
    # Active ones come before disabled ones.
    assert section.index("Активный-А") < section.index("Выключенный-В")
    # A foreign tenant does not get into the card anywhere.
    assert "Чужой-Г" not in html


def test_count_matches_list_length():
    """profile_count and the list count the same thing: all non-deleted profiles of the tenant."""
    rows = [
        _profile("Один", tenant_id=_TID, is_active=True),
        _profile("Два", tenant_id=_TID, is_active=False),
    ]
    client = _client(rows, profile_count=2)
    html = client.get(f"/admin/users/{_UID}").text
    assert "Профилей (всего, вкл. выключенные)" in html
    assert re.search(r"Профилей \(всего[^<]*</dt>\s*<dd>2</dd>", html)
    assert "Активных: 1 · выключенных: 1" in _section(html)


def test_empty_tenant_shows_placeholder():
    client = _client(
        [_profile("Чужой", tenant_id=_OTHER_TID, is_active=True)], profile_count=0
    )
    html = client.get(f"/admin/users/{_UID}").text
    assert "Шаблонов нет" in _section(html)
    assert "Чужой" not in html


def test_users_list_links_count_to_profiles_anchor():
    """The number of templates in the /admin/users row leads to #profiles of the card."""
    from tests.api.test_admin import _user_row as _list_user_row

    rows = [_list_user_row(profile_count=3)]
    # require_admin; list_users (rows + total); get_platform_stats (stats + users total)
    client, _ = _client_admin(
        [
            FakeResult(items=rows),
            FakeResult(scalar=1),
            FakeResult(
                items=[SimpleNamespace(total_all_time=0, total_month=0, total_day=0)]
            ),
            FakeResult(scalar=1),
        ]
    )
    html = client.get("/admin/users").text
    assert f'href="/admin/users/{_UID}#profiles"' in html
