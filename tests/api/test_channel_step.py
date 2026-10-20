"""The channel step of the wizard: not connected → connect right on the step (template_wizard_2, step 5).

Connecting — the existing routes (/cabinet/telegram/link, /cabinet/email/link)
through SEPARATE forms outside the template form (the form= attribute), the response — in a new tab:
an unsaved template is not lost, and the binding fields do not get into POST /profiles.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_profiles
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}


def teardown_function():
    app.dependency_overrides.clear()


def _html(url: str = "/profiles/new?manual=1") -> str:
    app.dependency_overrides[deps.get_session_factory] = lambda: (
        lambda: FakeSession(user_locale="sr")
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False).get(url).text


def _live(channels):
    async def _stub(db, tid):
        return list(channels)

    return _stub


def test_no_live_channel_offers_connect_on_step(monkeypatch):
    monkeypatch.setattr(routes_profiles, "_live_delivery_channels", _live([]))
    html = _html()
    assert "Kanal za obaveštenja još nije povezan" in html
    tg_btn = re.search(r"<button[^>]*>Poveži Telegram</button>", html).group(0)
    assert 'form="pfTgLinkForm"' in tg_btn
    email = re.search(r'<input id="pf-ch-email"[^>]*>', html, re.S).group(0)
    assert 'form="pfEmailLinkForm"' in email and 'name="email"' in email
    assert "data-no-preview" in email
    assert (
        '<form id="pfTgLinkForm" method="post" action="/cabinet/telegram/link#kanali" '
        'target="_blank"></form>' in html
    )
    assert (
        '<form id="pfEmailLinkForm" method="post" action="/cabinet/email/link" '
        'target="_blank"></form>' in html
    )
    # The binding forms — AFTER the template form, not nested in it.
    profile_form_end = html.index("</form>", html.index('id="profileForm"'))
    assert html.index('id="pfTgLinkForm"') > profile_form_end


def test_live_channel_hides_connect_block(monkeypatch):
    monkeypatch.setattr(routes_profiles, "_live_delivery_channels", _live(["email"]))
    html = _html()
    assert "Kanal za obaveštenja još nije povezan" not in html
    assert "pfTgLinkForm" not in html
