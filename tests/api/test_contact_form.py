"""Contact form (/kontakt): email to the operators + service Telegram.

No DB needed (POST does not write to the DB); channels are mocked, arguments are checked as literals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import admin_notify, deps
from src.api.cabinet import trial_service
from src.api.main import app
from src.api.public import routes_contact
from tests.api.conftest import FakeSession


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


def _client(*, tg=True, sess=None):
    settings = SimpleNamespace(
        tg_bot_token="TOKEN" if tg else "",
        admin_chat_id=777 if tg else 0,
        cabinet_base_url="https://nab.rs",
    )
    app.dependency_overrides[deps.get_settings] = lambda: settings
    app.dependency_overrides[deps.get_email_sender] = lambda: object()
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    return TestClient(app, follow_redirects=False)


_OK = {"email": "a@example.com", "name": "ACME", "message": "Нужен <b>Enterprise</b>"}


def test_get_form_has_fallback_address_and_no_mailto():
    resp = _client().get("/kontakt", cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert 'name="website"' in resp.text
    assert "info@nab.rs" in resp.text


def test_plans_cta_links_to_form_not_mailto():
    resp = _client().get("/plans")
    assert 'href="/kontakt"' in resp.text
    assert "mailto:" not in resp.text


def test_success_hits_both_channels(calls):
    resp = _client().post("/kontakt", data=_OK, cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert "Ответим на" in resp.text and "a@example.com" in resp.text
    assert len(calls["tg"]) == 1 and len(calls["mail"]) == 1
    tg = calls["tg"][0]
    assert tg["token"] == "TOKEN" and tg["admin_chat_id"] == 777
    # HTML is escaped, no keyboard
    assert "Нужен &lt;b&gt;Enterprise&lt;/b&gt;" in tg["text"]
    assert "<b>Enterprise</b>" not in tg["text"]
    assert "reply_markup" not in tg
    # no source field (direct /kontakt) — default source
    assert "Источник: kontakt" in tg["text"]
    mail = calls["mail"][0]
    assert mail["subject"] == "Сообщение с формы «Связаться с нами»"
    assert "Email: a@example.com" in mail["text_body"]
    assert "Нужен <b>Enterprise</b>" in mail["text_body"]


def test_empty_message_is_form_error(calls):
    resp = _client().post(
        "/kontakt", data={**_OK, "message": "   "}, cookies={"lang": "ru"}
    )
    assert resp.status_code == 422
    assert "Напишите сообщение." in resp.text
    assert calls["tg"] == [] and calls["mail"] == []


def test_bad_email_and_too_long(calls):
    c = _client()
    r1 = c.post("/kontakt", data={**_OK, "email": "nope"}, cookies={"lang": "ru"})
    assert r1.status_code == 422 and "корректный email" in r1.text
    r2 = c.post("/kontakt", data={**_OK, "message": "x" * 2001}, cookies={"lang": "ru"})
    assert r2.status_code == 422 and "слишком длинное" in r2.text
    assert calls["tg"] == [] and calls["mail"] == []


def test_honeypot_silent_refusal(calls):
    resp = _client().post(
        "/kontakt", data={**_OK, "website": "http://spam"}, cookies={"lang": "ru"}
    )
    assert resp.status_code == 200 and "Ответим на" in resp.text
    assert calls["tg"] == [] and calls["mail"] == []


def test_rate_limit_per_email(calls):
    c = _client()
    for _ in range(3):
        assert c.post("/kontakt", data=_OK, cookies={"lang": "ru"}).status_code == 200
    resp = c.post("/kontakt", data=_OK, cookies={"lang": "ru"})
    assert resp.status_code == 429 and "Слишком много запросов" in resp.text
    assert len(calls["mail"]) == 3


def test_rate_limit_per_ip_uses_forwarded_for(calls):
    # Behind Caddy, request.client is the proxy address: the limit was shared across all clients.
    c = _client()
    for i in range(10):
        data = {**_OK, "email": f"u{i}@example.com"}
        hdr = {"X-Forwarded-For": "203.0.113.7, 172.18.0.10"}
        assert c.post("/kontakt", data=data, headers=hdr).status_code == 200
    data = {**_OK, "email": "u10@example.com"}
    blocked = c.post("/kontakt", data=data, headers={"X-Forwarded-For": "203.0.113.7"})
    assert blocked.status_code == 429
    other = c.post("/kontakt", data=data, headers={"X-Forwarded-For": "198.51.100.9"})
    assert other.status_code == 200


def test_invalid_form_does_not_spend_ip_limit(calls):
    c = _client()
    hdr = {"X-Forwarded-For": "203.0.113.8"}
    for _ in range(12):
        bad = {**_OK, "message": ""}
        assert c.post("/kontakt", data=bad, headers=hdr).status_code == 422
    assert c.post("/kontakt", data=_OK, headers=hdr).status_code == 200


def test_telegram_text_capped(calls):
    c = _client()
    data = {**_OK, "message": "&" * 2000}
    assert c.post("/kontakt", data=data).status_code == 200
    assert len(calls["tg"][0]["text"]) < 4096
    assert len(calls["mail"]) == 1


def test_foreign_origin_rejected(calls):
    resp = _client().post(
        "/kontakt", data=_OK, headers={"origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert calls["mail"] == []


def test_telegram_failure_does_not_break_response(monkeypatch):
    sent = []

    async def boom(*a, **kw):
        raise RuntimeError("network down a@example.com")

    async def fake_mail(**kw):
        sent.append(kw)
        return 1

    # Real send_admin_text with a failing httpx: the response is still 200, the email went out.
    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    monkeypatch.setattr(admin_notify, "notify_admins_email", fake_mail)
    resp = _client().post("/kontakt", data=_OK, cookies={"lang": "ru"})
    assert resp.status_code == 200 and "Ответим на" in resp.text
    assert len(sent) == 1


def test_no_telegram_config_still_mails(calls):
    resp = _client(tg=False).post("/kontakt", data=_OK, cookies={"lang": "ru"})
    assert resp.status_code == 200
    assert calls["tg"] == [] and len(calls["mail"]) == 1


@pytest.mark.parametrize("lang,word", [("sr", "Hvala!"), ("en", "Thank you!")])
def test_thanks_languages(calls, lang, word):
    resp = _client().post("/kontakt", data=_OK, cookies={"lang": lang})
    assert word in resp.text
