"""A frozen subscriber SEES the cabinet (owner 10.10.2026); a deleted/revoked one does not.

Owner decision: after access ends (``access_until`` has expired) the mailing stops, but
the cabinet login stays — the feed, “My tenders”, the library, customers, the account are open, and
on every page a banner “Pretplata je završena” with a button for a free renewal.
The paid/AI paths are closed by their own gates (tests — ``test_translation_gates`` and
the service ones). ``deps.require_active_access`` now closes only:

- ``users.deleted_at`` (a disconnected subscriber) → 303 to ``/plans`` (``deps.SUBSCRIPTION_PATH``; not the hub — otherwise a loop);
- the absence of the user/tenant row and a DB error (fail-closed).

DB/session are mocked (ADR-013): ``FakeSession`` answers the gate with its pair
``access_until``/``deleted_at``, without consuming the ``results`` queue.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import feed_service, routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"

_PAST = datetime.now(timezone.utc) - timedelta(days=1)
_FUTURE = datetime.now(timezone.utc) + timedelta(days=30)

SUBSCRIPTION = "/plans"

# Content paths closed by the freeze (the routers tender_ai / my_tenders /
# narucilac / library + selectively the dashboard feed).
CLOSED_PATHS = [
    "/app/feed",
    f"/app/tenders/{_TENDER}?profile_id={_PROFILE}",
    f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=customer",
    "/app/my",
    "/app/my/export.csv",
    "/cabinet/narucilac",
    "/cabinet/narucilac.csv?name=%D0%9E%D0%BF%D1%88%D1%82%D0%B8%D0%BD%D0%B0",
    "/library",
]

# The path to come back and pay — the freeze does NOT close it.
OPEN_PATHS = ["/app", "/cabinet/account", "/plans"]


def _client(*, sess=SESS, session=None, **fake_kw):
    """TestClient with a substituted DB. ``session`` — the shared FakeSession for introspection."""

    def factory():
        return session if session is not None else FakeSession(**fake_kw)

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _analyses():
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs={},
    )


# --------------------------------------------------------------------------- #
# 1–2. The freeze (access_until in the past) does NOT close the content — the banner instead of the gate
# --------------------------------------------------------------------------- #


BANNER_TITLE = "Pretplata je završena"
BANNER_BTN = "Produži besplatno"


def test_frozen_content_paths_are_open_with_banner(monkeypatch):
    """All former closed paths → 200 and the banner (a literal from the owner’s decision)."""
    monkeypatch.setattr(feed_service, "count_feed_total", _async(0))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([]))
    client = _client(access_until=_PAST, user_locale="sr")
    for path in ("/app", "/app/feed", "/app/my", "/library", "/cabinet/account"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert BANNER_TITLE in resp.text, path
        assert BANNER_BTN in resp.text, path
        assert 'action="/cabinet/trial/request"' in resp.text, path
        assert 'name="kind" value="extension"' in resp.text, path


def test_frozen_tender_card_is_open(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc, "get_tender_analyses", _async(_analyses())
    )
    resp = _client(access_until=_PAST).get(
        f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"
    )
    assert resp.status_code == 200
    assert "Набавка рачунара" in resp.text


def test_frozen_workflow_post_is_not_redirected_by_gate():
    resp = _client(access_until=_PAST).post(f"/app/tenders/{_TENDER}/watch")
    assert resp.headers.get("location") != SUBSCRIPTION


def test_frozen_pending_request_replaces_button_with_status():
    resp = _client(access_until=_PAST, has_pending_request=True, user_locale="sr").get(
        "/app/my"
    )
    assert "Zahtev za produženje je poslat" in resp.text
    assert BANNER_BTN not in resp.text


def test_active_access_has_no_banner():
    resp = _client(access_until=_FUTURE, user_locale="sr").get("/library")
    assert resp.status_code == 200
    assert BANNER_TITLE not in resp.text


def test_unlimited_access_has_no_banner():
    resp = _client(access_until=None).get("/library")
    assert BANNER_TITLE not in resp.text


# --------------------------------------------------------------------------- #
# 3. Soft-deleting the user = revoking access with a live cookie
# --------------------------------------------------------------------------- #


def test_soft_deleted_user_closed_despite_live_access():
    client = _client(access_until=_FUTURE, user_deleted_at=_PAST)
    for path in CLOSED_PATHS:
        resp = client.get(path)
        assert resp.status_code == 303, path
        assert resp.headers["location"] == SUBSCRIPTION, path


def test_missing_row_is_fail_closed():
    """Neither the tenant nor the user is visible (RLS/deleted) → closed, not open."""
    resp = _client(access_row=False).get("/app/feed")
    assert resp.status_code == 303
    assert resp.headers["location"] == SUBSCRIPTION


def test_db_error_is_fail_closed(monkeypatch):
    """A DB error must not OPEN paid content (unlike the admin flag)."""

    class _BoomSession(FakeSession):
        async def execute(self, statement, params=None):
            if "SELECT t.access_until" in str(statement):
                raise RuntimeError("db down")
            return await super().execute(statement, params)

    resp = _client(session=_BoomSession()).get("/app/feed")
    assert resp.status_code == 303
    assert resp.headers["location"] == SUBSCRIPTION


# --------------------------------------------------------------------------- #
# 4. Regression: live access works as before
# --------------------------------------------------------------------------- #


def test_active_access_keeps_content_open(monkeypatch):
    monkeypatch.setattr(feed_service, "count_feed_total", _async(0))
    monkeypatch.setattr(feed_service, "get_feed_page", _async([]))
    client = _client(access_until=_FUTURE)
    for path in ("/app/feed", "/app/my", "/app/my/export.csv", "/library"):
        assert client.get(path).status_code == 200, path


def test_active_access_renders_tender_card(monkeypatch):
    monkeypatch.setattr(
        routes_tender_ai.svc, "get_tender_analyses", _async(_analyses())
    )
    resp = _client(access_until=_FUTURE).get(
        f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"
    )
    assert resp.status_code == 200
    assert "Набавка рачунара" in resp.text


def test_unlimited_access_is_active():
    """``access_until IS NULL`` = open-ended access, not “no date → close”."""
    resp = _client(access_until=None).get("/library")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 5. The path to come back and pay is alive
# --------------------------------------------------------------------------- #


def test_frozen_keeps_dashboard_and_payment_paths_open():
    client = _client(access_until=_PAST, user_locale="sr")
    for path in OPEN_PATHS:
        assert client.get(path).status_code == 200, path


# --------------------------------------------------------------------------- #
# 6. HTMX: a header instead of a redirect body (as _hx_login for an anonymous user)
# --------------------------------------------------------------------------- #


def test_deleted_htmx_fragment_returns_hx_redirect():
    resp = _client(access_until=_FUTURE, user_deleted_at=_PAST).get(
        f"/app/tenders/{_TENDER}/title?profile_id={_PROFILE}&view=feed",
        headers={"HX-Request": "true"},
    )
    # The code — as in routes_tender_ai._hx_login (401), so the front end behaves the same.
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == SUBSCRIPTION
    assert "location" not in resp.headers


def test_deleted_htmx_post_returns_hx_redirect():
    resp = _client(access_until=_FUTURE, user_deleted_at=_PAST).post(
        f"/app/tenders/{_TENDER}/watch", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == SUBSCRIPTION


# --------------------------------------------------------------------------- #
# Anonymous and the RLS contour
# --------------------------------------------------------------------------- #


def test_anonymous_still_goes_to_login_without_db_query():
    """We do not build a second auth contour: /login is served by the route itself, the DB is not touched."""
    db = FakeSession()
    resp = _client(sess=None, session=db).get("/app/feed")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Fapp%2Ffeed"
    assert not any("SELECT t.access_until" in sql for sql, _ in db.executed)


def test_gate_sets_tenant_before_reading_access():
    """RLS (ADR-002): set_tenant — the FIRST action of ITS OWN transaction of the gate.

    We measure inside the session where the gate’s SELECT lives: someone else’s ``set_config``
    (the cabinet locale resolver goes to the DB earlier) does not prove the gate’s contour.
    """
    opened: list[FakeSession] = []

    def factory():
        db = FakeSession(access_until=_FUTURE)
        opened.append(db)
        return db

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    TestClient(app, follow_redirects=False).get("/library")

    gate_db = next(
        db for db in opened if any("SELECT t.access_until" in s for s, _ in db.executed)
    )
    assert "set_config" in gate_db.executed[0][0]
