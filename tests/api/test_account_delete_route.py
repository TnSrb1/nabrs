"""Self-deletion route ``POST /cabinet/account/delete`` and the form on “My account” (LEG-06).

The deletion service is mocked (its SQL is in tests/db/test_account_self_delete.py on PG);
here — the route contract: session, CSRF, outcomes, email, cookie, form markup.
Expectations are literals.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import account_delete_service as svc
from src.api.cabinet import routes_account
from src.api.config import get_cabinet_settings
from src.api.emailer import MockEmailSender
from src.api.main import app
from tests.api.conftest import FakeSession, make_session_factory
from tests.api.test_routes_account import _setup

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"


def _secret() -> str:
    return get_cabinet_settings().cabinet_secret_key


def _client(monkeypatch, outcome=None, *, authed=True):
    calls: list[dict] = []
    sender = MockEmailSender()

    async def fake_delete(db, **kw):
        calls.append(kw)
        return outcome

    monkeypatch.setattr(routes_account.delete_svc, "delete_own_account", fake_delete)
    fs = FakeSession(results=[])
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_email_sender] = lambda: sender
    client = TestClient(app, follow_redirects=False)
    token = ""
    if authed:
        import time

        cookie = security.sign_session(
            _UID, _TID, _secret(), locale="sr", now=int(time.time())
        )
        client.cookies.set("tb_session", cookie)
        token = security.csrf_token(
            security.verify_session(cookie, _secret()), _secret()
        )
    return client, calls, sender, token


def teardown_function():
    app.dependency_overrides.clear()


def test_anonymous_goes_to_login_and_service_not_called(monkeypatch):
    client, calls, _, _ = _client(monkeypatch, authed=False)
    r = client.post("/cabinet/account/delete", data={"confirm_email": "a@b.rs"})
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert calls == []


def test_bad_csrf_does_not_delete(monkeypatch):
    client, calls, sender, _ = _client(monkeypatch)
    r = client.post(
        "/cabinet/account/delete", data={"confirm_email": "a@b.rs", "csrf": "forged"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/cabinet/account?del_err=csrf#acct-delete"
    assert calls == [] and sender.outbox == []


def test_success_sends_latin_mail_clears_cookie_and_shows_done(monkeypatch):
    out = svc.DeleteResult(
        svc.OK, email="klijent@example.rs", locale="sr", tenant_deleted=True
    )
    client, calls, sender, token = _client(monkeypatch, out)
    r = client.post(
        "/cabinet/account/delete",
        data={"confirm_email": "klijent@example.rs", "csrf": token},
    )
    assert r.status_code == 200
    assert calls == [
        {"tenant_id": _TID, "user_id": _UID, "confirm_email": "klijent@example.rs"}
    ]
    assert (
        'tb_session=""' in r.headers["set-cookie"]
        or "tb_session=;" in r.headers["set-cookie"]
    )
    assert "Nalog je obrisan" in r.text
    assert len(sender.outbox) == 1
    mail = sender.outbox[0]
    assert mail.to == "klijent@example.rs"
    assert mail.subject == "Nalog TenderSRB je obrisan"
    assert "Vaš nalog TenderSRB je obrisan" in mail.text_body


def test_mail_failure_does_not_break_deletion(monkeypatch):
    out = svc.DeleteResult(svc.OK, email="k@example.rs", locale="ru")
    client, _, sender, token = _client(monkeypatch, out)

    async def boom(**kw):
        raise RuntimeError("smtp down")

    sender.send = boom  # type: ignore[method-assign]
    r = client.post(
        "/cabinet/account/delete", data={"confirm_email": "k@example.rs", "csrf": token}
    )
    assert r.status_code == 200


def test_refusals_redirect_back_with_flag_and_keep_session(monkeypatch):
    for outcome, flag in (
        (svc.EMAIL_MISMATCH, "email_mismatch"),
        (svc.ACTIVE_SUBSCRIPTION, "active_subscription"),
    ):
        client, _, sender, token = _client(monkeypatch, svc.DeleteResult(outcome))
        r = client.post(
            "/cabinet/account/delete", data={"confirm_email": "x@y.rs", "csrf": token}
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/cabinet/account?del_err={flag}#acct-delete"
        assert "set-cookie" not in r.headers and sender.outbox == []


def test_account_page_has_delete_form_with_csrf_and_confirm(monkeypatch):
    _setup(monkeypatch)
    client, _, _, token = _client(monkeypatch)
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
        "iat": 1,
    }
    r = client.get("/cabinet/account?del_err=email_mismatch")
    assert r.status_code == 200
    assert 'action="/cabinet/account/delete"' in r.text
    assert 'data-confirm="Удалить аккаунт навсегда? Это нельзя отменить."' in r.text
    assert 'name="confirm_email"' in r.text and 'type="email"' in r.text
    assert "Введённый email не совпадает с адресом аккаунта" in r.text
    assert 'name="csrf" value="' + token + '"' in r.text
