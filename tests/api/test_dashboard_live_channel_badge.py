"""The alert badge and the teaser’s empty state check “there is a live channel”, not “there is Telegram”.

Bug of 13.10.2026: a client with a confirmed email (a live channel) saw "Telegram: not
connected“ and ”Connect Telegram", because the templates checked `telegram_linked`.
Dashboard v2 (13.10, step 2): the channel badges from the hero are REMOVED — the meaning is carried by the single alert
`_dash_alert.html` (data-dash-alert) and by the teaser’s empty state; the contract “a live channel, not Telegram” is held on them. The former expected badge `<span …>Email</span>`
in the hero no longer exists.
Expectations — literals of the ru catalog (cabinet default), not `t()` calls.
"""

from __future__ import annotations

from src.api.cabinet import routes_profiles
from tests.api.test_routes_profiles import SESS, _async, _client, _profile

TG_BADGE_NOT_CONNECTED = "Telegram: не подключён"
TG_CONNECT_CTA = "Подключить Telegram"
CHANNEL_NOT_CONNECTED = "Канал не подключён"


def _dashboard(monkeypatch, *, has_live, live_channels, telegram_linked):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_profile()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(telegram_linked)
    )
    monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async(
            {
                "has_live_channel": has_live,
                "live_channels": live_channels,
                "gaps": [],
                "channelless": [],
            }
        ),
    )
    resp = _client(sess=SESS).get("/app")
    assert resp.status_code == 200
    return resp.text


def test_email_only_live_channel_shows_channel_name_not_telegram_prompt(monkeypatch):
    """Only email is live, Telegram is not linked → neither the alert nor a request to connect Telegram."""
    text = _dashboard(
        monkeypatch, has_live=True, live_channels=["email"], telegram_linked=False
    )
    assert "data-dash-alert" not in text
    assert TG_BADGE_NOT_CONNECTED not in text
    assert CHANNEL_NOT_CONNECTED not in text
    assert TG_CONNECT_CTA not in text


def test_no_live_channel_links_to_channels_section(monkeypatch):
    """No channel is live → the “no channel” alert with a link to the channels section."""
    text = _dashboard(
        monkeypatch, has_live=False, live_channels=[], telegram_linked=False
    )
    assert 'data-dash-alert="no_channel"' in text
    assert 'href="/cabinet/account#kanali"' in text
    assert TG_CONNECT_CTA in text
