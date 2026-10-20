"""The cabinet locale from the SIGNED session, not a DB query on every render (Part A).

`deps.resolve_cabinet_locale` hangs on all cabinet routes (including the polling of the generation status) — before this PR it did `set_config` + `SELECT locale FROM users` on EVERY tick. Now the language travels in the signed session (it can be trusted like uid/tid); the DB is touched ONLY for old cookies without the `locale` field (backward compatibility). The resolve priority (?lang→users.locale→cookie→Accept-Language→ru) is NOT changed — it is answered for by `i18n.resolve_authed_lang` (see test_cabinet_i18n).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.config import get_cabinet_settings
from src.api.main import app
from tests.api.conftest import FakeSession

_UID = "22222222-2222-2222-2222-222222222222"
_TID = "11111111-1111-1111-1111-111111111111"


def teardown_function():
    app.dependency_overrides.clear()


class _CountingFactory:
    """Session factory counting the accesses — a marker of a trip to the DB."""

    def __init__(self, session: FakeSession):
        self.session = session
        self.calls = 0

    def __call__(self) -> FakeSession:
        self.calls += 1
        return self.session


def _req() -> SimpleNamespace:
    # resolve_cabinet_locale touches only request.state.
    return SimpleNamespace(state=SimpleNamespace())


# --------------------------------------------------------------------------- #
# (a) a fresh session carries the locale → the DB is NOT touched
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fresh_session_skips_db():
    factory = _CountingFactory(FakeSession(user_locale="sr"))  # The DB would say sr…
    req = _req()
    await deps.resolve_cabinet_locale(
        req, session={"uid": _UID, "tid": _TID, "locale": "en"}, session_factory=factory
    )
    # …but we take it from the session (en), not from the DB (sr).
    assert req.state.cabinet_user_locale == "en"
    assert factory.calls == 0  # not a single transaction
    assert factory.session.executed == []  # not a single SELECT locale


# --------------------------------------------------------------------------- #
# (b) an old cookie without the locale key → a one-off fallback to the DB
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_legacy_session_without_locale_falls_back_to_db():
    factory = _CountingFactory(FakeSession(user_locale="sr"))
    req = _req()
    await deps.resolve_cabinet_locale(
        req, session={"uid": _UID, "tid": _TID}, session_factory=factory
    )
    assert req.state.cabinet_user_locale == "sr"
    assert factory.calls == 1
    stmts = [sql for sql, _ in factory.session.executed]
    # set_tenant FIRST (RLS), then SELECT locale.
    assert any("set_config" in s or "app.current_tenant_id" in s for s in stmts)
    assert any("SELECT locale FROM users" in s for s in stmts)


@pytest.mark.asyncio
async def test_anonymous_never_touches_db():
    factory = _CountingFactory(FakeSession(user_locale="sr"))
    req = _req()
    await deps.resolve_cabinet_locale(req, session=None, session_factory=factory)
    assert req.state.cabinet_user_locale is None
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_db_error_on_legacy_session_falls_back_to_none():
    """A DB error in the fallback does not break the page — locale None (the resolver → cookie/AL)."""

    def boom():
        raise RuntimeError("db down")

    req = _req()
    await deps.resolve_cabinet_locale(
        req, session={"uid": _UID, "tid": _TID}, session_factory=boom
    )
    assert req.state.cabinet_user_locale is None


# --------------------------------------------------------------------------- #
# End-to-end render: a fresh session with locale does not make a SELECT locale
# --------------------------------------------------------------------------- #
def _shared_client(fake: FakeSession, sess):
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fake)
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def test_cabinet_render_with_session_locale_issues_no_select_locale():
    fake = FakeSession(user_locale="sr")
    client = _shared_client(fake, {"uid": _UID, "tid": _TID, "locale": "en"})
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'lang="en"' in resp.text  # the language from the session
    assert not any("SELECT locale FROM users" in sql for sql, _ in fake.executed)


def test_cabinet_render_legacy_session_reads_locale_from_db():
    fake = FakeSession(user_locale="sr")
    client = _shared_client(fake, {"uid": _UID, "tid": _TID})  # without a locale key
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'lang="sr"' in resp.text  # the language from the DB (fallback)
    assert any("SELECT locale FROM users" in sql for sql, _ in fake.executed)


# --------------------------------------------------------------------------- #
# (c) changing the language re-issues the session → the new language IMMEDIATELY on the next render
# --------------------------------------------------------------------------- #
def test_set_locale_reissues_session_cookie_with_new_locale():
    from src.api.cabinet.i18n import SUPPORTED  # noqa: F401 — sanity: en is valid

    fake = FakeSession()
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fake)
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": _UID,
        "tid": _TID,
    }
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    resp = client.post("/cabinet/locale", data={"lang": "en", "return_to": "/app"})
    assert resp.status_code == 303

    # The re-issued session cookie carries the new locale — the next render
    # takes it from the signature, without a trip to the DB.
    set_cookie = resp.headers.get_list("set-cookie")
    token = None
    for raw in set_cookie:
        if raw.startswith(f"{deps.SESSION_COOKIE}="):
            token = raw.split("=", 1)[1].split(";", 1)[0]
    assert token, "the session cookie was not re-issued"
    body = security.verify_session(token, get_cabinet_settings().cabinet_secret_key)
    assert body is not None
    assert body["locale"] == "en"
    assert body["uid"] == _UID and body["tid"] == _TID


# --------------------------------------------------------------------------- #
# (d) a broken/forged session → anonymous, locale None, without a trip to the DB
# --------------------------------------------------------------------------- #
def test_tampered_session_cookie_is_anonymous():
    fake = FakeSession(user_locale="sr")
    # get_optional_session is NOT overridden — verify_session works on the junk.
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: fake)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    client.cookies.set(deps.SESSION_COOKIE, "garbage.notasig")
    resp = client.get("/login")
    assert resp.status_code == 200
    # Anonymous: the language is the default. On the login screens the default is sr (the showcase contour,
    # audit package 2, HIGH-2), not the cabinet ru. The DB is not touched in this — the point of
    # the test: a forged signature gives neither a session nor a trip for users.locale.
    assert 'lang="sr"' in resp.text
    assert not any("SELECT locale FROM users" in sql for sql, _ in fake.executed)
