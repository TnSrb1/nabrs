"""“Svi rokovi (.ics)” — ``GET /app/tenders/calendar.ics`` (VG-17, cabinet).

The DB is mocked (session and service); the selection and RLS are checked by tests/db. Here — the
session gate, the headers, the literals of the event, and that the route does not go to
``/app/tenders/{tender_id}`` (the order of inclusion in main.py).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import workflow_service as svc
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
TID = "22222222-2222-2222-2222-222222222222"


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _row(**kw):
    base = dict(
        tender_id=TID,
        title="Nabavka opreme, sto",
        customer_name="ОШ «Вук Караджич»",
        deadline_at=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc),
        source_key="jnportal",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _capture(monkeypatch, rows):
    """Substitutes the selection and remembers (tid, now) with which it was called."""
    calls = []

    async def _stub(db, tenant_id, now):
        calls.append((tenant_id, now))
        return rows

    monkeypatch.setattr(svc, "get_calendar_deadlines", _stub)
    return calls


def test_anonymous_redirects_to_login(monkeypatch):
    calls = _capture(monkeypatch, [])
    resp = _client(sess=None).get("/app/tenders/calendar.ics")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert calls == []  # an anonymous user never reaches the DB


def test_session_gets_ics_with_headers(monkeypatch):
    calls = _capture(monkeypatch, [_row()])
    resp = _client(sess=SESS).get("/app/tenders/calendar.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/calendar; charset=utf-8"
    assert (
        resp.headers["content-disposition"]
        == 'attachment; filename="tenders-rokovi.ics"'
    )
    assert [tid for tid, _ in calls] == ["t-1"]


def test_event_literals_and_card_url(monkeypatch):
    _capture(monkeypatch, [_row()])
    lines = _client(sess=SESS).get("/app/tenders/calendar.ics").text.split("\r\n")
    assert "DTSTART:20261105T100000Z" in lines
    assert r"SUMMARY:Rok: Nabavka opreme\, sto" in lines
    # the base — from the environment settings, so we compare the tail of the card path
    assert any(x.startswith("URL:") and x.endswith(f"/tender/{TID}") for x in lines)


def test_no_rows_is_valid_empty_calendar(monkeypatch):
    _capture(monkeypatch, [])
    body = _client(sess=SESS).get("/app/tenders/calendar.ics").text
    assert body.startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" not in body
    assert body.endswith("END:VCALENDAR\r\n")


def test_two_tenders_two_events(monkeypatch):
    other = "33333333-3333-3333-3333-333333333333"
    _capture(monkeypatch, [_row(), _row(tender_id=other, title="Drugi")])
    body = _client(sess=SESS).get("/app/tenders/calendar.ics").text
    assert body.count("BEGIN:VEVENT") == 2
    assert f"UID:tender-{other}@nab.rs" in body


def test_calendar_path_not_captured_by_tender_detail(monkeypatch):
    """A regression of the include order: ``/app/tenders/{tender_id}`` must not swallow the .ics."""
    _capture(monkeypatch, [])
    resp = _client(sess=SESS).get("/app/tenders/calendar.ics")
    assert "text/calendar" in resp.headers["content-type"]


def test_my_tenders_page_has_calendar_button(monkeypatch):
    """“Svi rokovi (.ics)” is visible on /app/my when there is something to mark."""
    data = svc.MyTenders(
        folders=[],
        funnel=[svc.MyTenderRow("t1", "Изучаю", None, None, None, status="watching")],
    )

    async def _stub(db, tenant_id):
        return data

    monkeypatch.setattr(svc, "get_my_tenders", _stub)
    resp = _client(sess=SESS).get("/app/my")
    assert resp.status_code == 200
    assert 'href="/app/tenders/calendar.ics"' in resp.text
    # a test session without locale → ru; the sr caption “Svi rokovi (.ics)” — for an sr user
    assert "Все сроки (.ics)" in resp.text


def test_calendar_ics_is_noindex(monkeypatch):
    _capture(monkeypatch, [_row()])
    resp = _client(sess=SESS).get("/app/tenders/calendar.ics")
    assert resp.headers["x-robots-tag"] == "noindex"


def test_calendar_cfcu_row_without_hour_is_all_day(monkeypatch):
    row = _row(
        source_key="cfcu",
        deadline_at=datetime(2026, 11, 17, 22, 59, tzinfo=timezone.utc),
    )
    _capture(monkeypatch, [row])
    lines = _client(sess=SESS).get("/app/tenders/calendar.ics").text.split("\r\n")
    assert "DTSTART;VALUE=DATE:20261117" in lines
    assert "DTEND;VALUE=DATE:20261118" in lines
    assert not any(x.startswith("DTSTART:") for x in lines)
