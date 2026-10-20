"""The predicate of the banner “template without a channel while a channel is alive” (_channelless_profiles).

The reverse of ``_delivery_gaps`` gap: the recipient EXISTS, but an active template with an empty
``delivery_channels`` is skipped by routing (``write_outbox``) — the tenders do not go out.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.api.cabinet.channel_service import _channelless_profiles
from src.workers.matching import ChannelState

_LIVE = {"telegram": ChannelState("telegram", "active", "424242")}
_PENDING_ONLY = {"email": ChannelState("email", "pending", "a@b.rs")}


def _p(pid, channels, *, active=True, name="Gradnja"):
    return SimpleNamespace(
        id=pid, name=name, is_active=active, delivery_channels=channels
    )


def test_active_empty_profile_with_live_channel_is_flagged():
    got = _channelless_profiles([_p("p1", []), _p("p2", None)], _LIVE)
    assert got == [{"id": "p1", "name": "Gradnja"}, {"id": "p2", "name": "Gradnja"}]


def test_no_live_channel_is_onboarding_not_this_banner():
    assert _channelless_profiles([_p("p1", [])], {}) == []
    assert _channelless_profiles([_p("p1", [])], _PENDING_ONLY) == []


def test_profile_with_channels_or_inactive_is_not_flagged():
    profiles = [_p("p1", ["email"]), _p("p2", [], active=False)]
    assert _channelless_profiles(profiles, _LIVE) == []
