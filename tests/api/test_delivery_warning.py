"""build_delivery_warning: two cases of “template exists, no deliveries”."""

from types import SimpleNamespace

from src.api.cabinet.channel_service import build_delivery_warning


def _p(active=True):
    return SimpleNamespace(is_active=active)


def test_case_a_active_profile_no_live_channel():
    got = build_delivery_warning([_p()], False, [])
    assert got == {"no_channel": True, "channelless": []}


def test_case_b_live_channel_but_channelless_profile():
    cl = [{"id": "p1", "name": "X"}]
    got = build_delivery_warning([_p()], True, cl)
    assert got == {"no_channel": False, "channelless": cl}


def test_no_warning_when_channel_live_and_profiles_ok():
    assert build_delivery_warning([_p()], True, []) is None


def test_no_warning_without_active_profile():
    assert build_delivery_warning([_p(False)], False, []) is None
    assert build_delivery_warning([], False, []) is None
