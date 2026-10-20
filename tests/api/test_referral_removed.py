"""Regression of the removal of the referral program (owner decision, 26.07.2026).

The program is removed from the product entirely, not switched off by a flag: the route ``/cabinet/invite`` is removed together with the router, the menu item — from ``base.html``. We check the RENDER under a signed session, not the template: a link could remain in a conditional partial and not get into the search by the file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_profiles
from src.api.config import get_cabinet_settings
from src.api.main import app

SESS = {"uid": "u-1", "tid": "t-1"}


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _authed_client() -> TestClient:
    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    client = TestClient(app, follow_redirects=False)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key),
    )
    return client


def teardown_function():
    app.dependency_overrides.clear()


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


def test_cabinet_invite_route_is_gone():
    """The router is removed — the page gives 404 even under a valid session."""
    resp = _authed_client().get("/cabinet/invite")
    assert resp.status_code == 404


def test_dashboard_render_has_no_invite_link():
    """In the cabinet render there is neither a link to the removed route, nor a menu item."""
    resp = _authed_client().get("/app")
    assert resp.status_code == 200
    assert "/cabinet/invite" not in resp.text
    assert "Пригласить коллегу" not in resp.text
