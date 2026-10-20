"""Subscriber card: template connection date, delivery channels and subscriber channels.

Without DB (ADR-013): mock session and `dependency_overrides`, as in `test_admin_user_profiles.py`.
Date literals — from UTC fixtures → Europe/Belgrade (October 2026 = UTC+2):
11.10.2026 10:07:55 UTC → 12:07, 09:50 UTC → 11:50, 09:55 UTC → 11:55.
A live check under the tendersrb_app role — in tests/db/test_0057_admin_profile_channels_rls.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.config import CabinetSettings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_OTHER_TID = "cccc0000-0000-0000-0000-000000000003"
_OPERATOR_EMAIL = "admin@example.com"
_ADMIN_SESSION = {"uid": _UID, "tid": _TID}
_NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
_WITH_CHANNEL_AT = datetime(2026, 10, 11, 10, 7, 55, tzinfo=timezone.utc)  # → 12:07
_NO_CHANNEL_AT = datetime(2026, 10, 11, 9, 50, 0, tzinfo=timezone.utc)  # → 11:50
_LINKED_AT = datetime(2026, 10, 11, 9, 55, 0, tzinfo=timezone.utc)  # → 11:55


def _settings() -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=_OPERATOR_EMAIL)


def _user_row():
    return SimpleNamespace(
        id=_UID,
        email="subscriber@example.com",
        created_at=_NOW,
        tenant_id=_TID,
        tier="pro",
        access_until=None,
        profile_count=2,
        deleted_at=None,
    )


def _profile(name, *, tenant_id, created_at, delivery_channels):
    return SimpleNamespace(
        id=f"pid-{name}",
        name=name,
        tenant_id=tenant_id,
        is_active=True,
        owner_email="subscriber@example.com",
        match_7d=0,
        created_at=created_at,
        delivery_channels=delivery_channels,
    )


def _channels(*rows):
    return [SimpleNamespace(channel=c, status=s, linked_at=at) for c, s, at in rows]


def _client(profile_rows, channel_rows):
    results = [
        FakeResult(items=[SimpleNamespace(email=_OPERATOR_EMAIL)]),  # require_admin
        FakeResult(items=[_user_row()]),  # get_user
        FakeResult(scalar=0),  # get_user_delivered_count
        FakeResult(items=profile_rows),  # app_admin_list_profiles()
        FakeResult(items=channel_rows),  # app_admin_user_channels()
    ]
    fs = FakeSession(results=results)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: _ADMIN_SESSION
    app.dependency_overrides[deps.get_settings] = _settings
    return TestClient(app, follow_redirects=False)


def _section(html: str) -> str:
    """The "Templates" block: from the #profiles anchor to the "Extend access" block."""
    start = html.index('id="profiles"')
    return html[start : html.index("<!-- Продлить доступ -->", start)]


def teardown_function():
    app.dependency_overrides.clear()


NO_CHANNEL_TEXT = "нет канала — доставок не будет"


def test_profile_dates_and_channels_shown_and_empty_channels_flagged():
    rows = [
        _profile(
            "С-каналом",
            tenant_id=_TID,
            created_at=_WITH_CHANNEL_AT,
            delivery_channels=["telegram"],
        ),
        _profile(
            "Без-канала",
            tenant_id=_TID,
            created_at=_NO_CHANNEL_AT,
            delivery_channels=[],
        ),
        _profile(
            "Чужой-тенант",
            tenant_id=_OTHER_TID,
            created_at=_WITH_CHANNEL_AT,
            delivery_channels=[],
        ),
    ]
    client = _client(rows, _channels(("telegram", "active", _LINKED_AT)))
    html = client.get(f"/admin/users/{_UID}").text
    section = _section(html)

    # Connection date — in Belgrade time, as a literal.
    assert "подключён 11.10.2026 12:07" in section
    assert "подключён 11.10.2026 11:50" in section
    # Template channels with a channel — Telegram; a template without a channel — a badge.
    assert "каналы: Telegram" in section
    assert section.count(NO_CHANNEL_TEXT) == 1
    # The badge is on the template without a channel, not on its neighbour.
    no_channel_at = section.index("Без-канала")
    with_channel_at = section.index("С-каналом")
    badge_at = section.index(NO_CHANNEL_TEXT)
    assert badge_at > no_channel_at
    assert badge_at > with_channel_at
    assert "Чужой-тенант" not in html


def test_subscriber_channel_row_shows_telegram_status_and_date():
    client = _client([], _channels(("telegram", "active", _LINKED_AT)))
    html = client.get(f"/admin/users/{_UID}").text
    assert '<dt class="text-slate-500">Каналы</dt>' in html
    assert "Telegram" in html
    assert "активен" in html
    assert "с 11.10.2026 11:55" in html
    assert "не подключены" not in html


def test_no_subscriber_channels_says_not_connected():
    client = _client([], [])
    html = client.get(f"/admin/users/{_UID}").text
    assert "не подключены" in html


def test_profile_without_created_at_shows_no_date_line():
    rows = [
        _profile(
            "Без-даты", tenant_id=_TID, created_at=None, delivery_channels=["telegram"]
        )
    ]
    html = _client(rows, []).get(f"/admin/users/{_UID}").text
    assert "Без-даты" in _section(html)
    assert "подключён" not in _section(html)
