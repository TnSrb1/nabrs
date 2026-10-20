"""The “Contact us” form in three places: /kontakt, the block in “Help”, the cabinet hub.

One receiver — POST /kontakt; the difference is the hidden field ``source`` (help|cabinet|…).
Expectations — as LITERALS (address, action, anchor, source name), not via ``t()``.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import admin_notify, deps, security
from src.api.cabinet import trial_service
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.api.public import routes_contact
from tests.api.conftest import FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_OK = {"email": "a@example.com", "name": "ACME", "message": "Нужен тариф"}


@pytest.fixture(autouse=True)
def _clean():
    routes_contact._CONTACT_PER_EMAIL.reset()
    routes_contact._CONTACT_PER_IP.reset()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def calls(monkeypatch):
    rec = {"tg": [], "mail": []}

    async def fake_tg(**kw):
        rec["tg"].append(kw)
        return True

    async def fake_mail(**kw):
        rec["mail"].append(kw)
        return 1

    monkeypatch.setattr(trial_service, "send_admin_text", fake_tg)
    monkeypatch.setattr(admin_notify, "notify_admins_email", fake_mail)
    return rec


def _public(*, sess=None):
    settings = SimpleNamespace(
        tg_bot_token="TOKEN",
        admin_chat_id=777,
        cabinet_base_url="https://nab.rs",
    )
    app.dependency_overrides[deps.get_settings] = lambda: settings
    app.dependency_overrides[deps.get_email_sender] = lambda: object()
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    return TestClient(app, follow_redirects=False)


def _authed_client(tier="pro"):
    fs = FakeSession(
        results=[],
        tenant_tier=tier,
        tenant_status="active",
        access_until=None,
        user_locale="sr",
        answers_tenant_tier=True,
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


# --- /help: form block and FAQ link ------------------------------------- #


def test_help_has_contact_block_with_mailto_and_post_form():
    html = _public().get("/help", cookies={"lang": "sr"}).text
    block = re.search(r'<section id="pisite-nam".*?</section>', html, re.S).group(0)
    assert "Pišite nam" in block
    assert 'href="mailto:info@nab.rs"' in block
    assert '<form method="post" action="/kontakt"' in block
    assert 'name="source" value="help"' in block
    assert "Opišite šta vam treba" in block


@pytest.mark.parametrize(
    "lang,phrase,block_title",
    [
        ("sr", "nam pišite", "Pišite nam"),
        ("ru", "напишите нам", "Напишите нам"),
        ("en", "contact us", "Write to us"),
    ],
)
def test_help_faq_phrase_is_link_to_contact_block(lang, phrase, block_title):
    html = _public().get("/help", cookies={"lang": lang}).text
    assert f'<a href="#pisite-nam" class="underline">{phrase}</a>' in html
    assert 'id="pisite-nam"' in html and block_title in html


def test_help_jsonld_faq_answer_stays_plain_valid_text():
    html = _public().get("/help", cookies={"lang": "sr"}).text
    scripts = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    )
    faq = [json.loads(s) for s in scripts if '"FAQPage"' in s]
    assert len(faq) == 1
    answers = [q["acceptedAnswer"]["text"] for q in faq[0]["mainEntity"]]
    assert "Pošaljite /help komandu botu ili nam pišite." in answers


# --- cabinet hub: section “Podrška” ---------------------------------------------- #


def test_hub_has_support_section_with_cabinet_source_and_mailto():
    html = _authed_client().get("/cabinet/account").text
    section = re.search(
        r'<section class="card mb-6" id="podrska".*?</section>', html, re.S
    )
    assert section, "section #podrska is not in the hub"
    block = section.group(0)
    assert 'href="#podrska"' in html
    assert '<form method="post" action="/kontakt"' in block
    assert 'name="source" value="cabinet"' in block
    assert "Opišite šta vam treba" in block
    assert 'href="mailto:info@nab.rs"' in block


# --- POST /kontakt: source reaches the operator -------------------------- #


def test_cabinet_source_reaches_operator_notifications(calls):
    resp = _public().post(
        "/kontakt", data={**_OK, "source": "cabinet"}, cookies={"lang": "sr"}
    )
    assert resp.status_code == 200
    assert len(calls["mail"]) == 1 and len(calls["tg"]) == 1
    assert "Источник: cabinet" in calls["mail"][0]["text_body"]
    assert "Источник: cabinet" in calls["tg"][0]["text"]


def test_help_source_thanks_page_links_back_to_help(calls):
    resp = _public().post(
        "/kontakt", data={**_OK, "source": "help"}, cookies={"lang": "sr"}
    )
    assert resp.status_code == 200
    assert 'href="/help"' in resp.text
    assert "Nazad na pomoć" in resp.text
    assert "Источник: help" in calls["mail"][0]["text_body"]


def test_unknown_source_falls_back_to_kontakt(calls):
    _public().post(
        "/kontakt", data={**_OK, "source": "evil<script>"}, cookies={"lang": "ru"}
    )
    assert "Источник: kontakt" in calls["mail"][0]["text_body"]
    assert "evil" not in calls["mail"][0]["text_body"]


def test_cabinet_source_invalid_form_keeps_source_in_form(calls):
    resp = _public().post(
        "/kontakt",
        data={**_OK, "source": "cabinet", "message": " "},
        cookies={"lang": "sr"},
    )
    assert resp.status_code == 422
    assert 'name="source" value="cabinet"' in resp.text
    assert calls["mail"] == [] and calls["tg"] == []


# --- contact address: one constant ---------------------------------------- #


def test_security_txt_contact_comes_from_the_same_constant():
    resp = _public().get("/security.txt")
    assert resp.status_code == 200
    assert "Contact: mailto:info@nab.rs" in resp.text


def test_no_delivery_channel_is_error_not_thanks(monkeypatch):
    # Neither TG nor email accepted the request: a “thank you” would be a lie → 503 and the error text.
    async def tg_fail(**kw):
        return False

    async def mail_none(**kw):
        return 0

    monkeypatch.setattr(trial_service, "send_admin_text", tg_fail)
    monkeypatch.setattr(admin_notify, "notify_admins_email", mail_none)
    resp = _public().post(
        "/kontakt", data={**_OK, "source": "help"}, cookies={"lang": "sr"}
    )
    assert resp.status_code == 503
    assert "Poruku trenutno nije moguće poslati" in resp.text
    assert "Hvala!" not in resp.text
