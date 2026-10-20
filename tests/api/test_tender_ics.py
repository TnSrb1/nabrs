"""The public ``GET /tender/{id}.ics`` (TestClient, DB — FakeSession).

Expectations — literals from the task and RFC 5545, not from the code of the route.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession
from tests.api.test_tender_page import _tender as _page_tender

TID = "11111111-1111-4111-8111-111111111111"


def _tender(**kw):
    base = dict(
        id=TID,
        title="Nabavka opreme, sto",
        customer_name="ОШ «Вук Караджич»",
        deadline_at=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc),
        source_key="jnportal",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _client(session: FakeSession):
    def factory():
        return session

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_ics_200_with_calendar_headers_and_literal_dtstart():
    client = _client(FakeSession(results=[FakeResult(items=[_tender()])]))
    resp = client.get(f"/tender/{TID}.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "text/calendar; charset=utf-8"
    assert (
        resp.headers["content-disposition"]
        == f'attachment; filename="tender-{TID}.ics"'
    )
    assert "DTSTART:20261105T100000Z" in resp.text.split("\r\n")


def test_ics_comma_in_title_is_escaped():
    client = _client(FakeSession(results=[FakeResult(items=[_tender()])]))
    resp = client.get(f"/tender/{TID}.ics")
    assert r"SUMMARY:Rok: Nabavka opreme\, sto" in resp.text.split("\r\n")


def test_ics_description_has_customer_and_card_link():
    client = _client(FakeSession(results=[FakeResult(items=[_tender()])]))
    resp = client.get(f"/tender/{TID}.ics")
    lines = resp.text.split("\r\n")
    assert f"URL:https://nab.rs/tender/{TID}" in lines
    assert any(x.startswith("DESCRIPTION:Naručilac:") for x in lines)


def test_ics_without_deadline_is_404():
    client = _client(
        FakeSession(results=[FakeResult(items=[_tender(deadline_at=None)])])
    )
    assert client.get(f"/tender/{TID}.ics").status_code == 404


def test_ics_unknown_tender_is_404():
    client = _client(FakeSession(results=[FakeResult(items=[])]))
    assert client.get(f"/tender/{TID}.ics").status_code == 404


def test_ics_non_uuid_is_404_without_db_call():
    def _no_db():
        raise AssertionError("a non-UUID must not reach the DB")

    app.dependency_overrides[deps.get_session_factory] = lambda: _no_db
    assert TestClient(app).get("/tender/not-a-uuid.ics").status_code == 404


def test_ics_route_does_not_fall_through_to_html_page():
    """Regression: without a separate route ``<id>.ics`` would catch the HTML card."""
    client = _client(FakeSession(results=[FakeResult(items=[_tender()])]))
    resp = client.get(f"/tender/{TID}.ics")
    assert "text/calendar" in resp.headers["content-type"]
    assert "<html" not in resp.text.lower()


def test_cfcu_without_hour_is_all_day_event():
    """CFKU: deadline 2026-11-17 23:59 Europe/Belgrade (= 22:59Z) without an hour → DATE."""
    deadline = datetime(2026, 11, 17, 22, 59, tzinfo=timezone.utc)
    client = _client(
        FakeSession(
            results=[
                FakeResult(items=[_tender(source_key="cfcu", deadline_at=deadline)])
            ]
        )
    )
    lines = client.get(f"/tender/{TID}.ics").text.split("\r\n")
    assert "DTSTART;VALUE=DATE:20261117" in lines
    assert "DTEND;VALUE=DATE:20261118" in lines
    assert not any(x.startswith("DTSTART:") for x in lines)
    assert "T225900Z" not in "\r\n".join(lines)


def test_public_ics_is_noindex():
    client = _client(FakeSession(results=[FakeResult(items=[_tender()])]))
    assert client.get(f"/tender/{TID}.ics").headers["x-robots-tag"] == "noindex"


def test_past_deadline_still_served_as_ics():
    past = datetime.now(timezone.utc) - timedelta(days=3)
    client = _client(
        FakeSession(results=[FakeResult(items=[_tender(deadline_at=past)])])
    )
    resp = client.get(f"/tender/{TID}.ics")
    assert resp.status_code == 200
    assert "BEGIN:VEVENT" in resp.text


def _card_html(deadline_at) -> str:
    client = _client(
        FakeSession(
            results=[FakeResult(items=[_page_tender(id=TID, deadline_at=deadline_at)])]
        )
    )
    resp = client.get(f"/tender/{TID}?lang=ru")
    assert resp.status_code == 200
    return resp.text


def test_card_shows_calendar_button_for_future_deadline():
    """The “To calendar” button next to the deadline — if the deadline is still ahead (ru)."""
    html = _card_html(datetime.now(timezone.utc) + timedelta(days=10))
    assert f'href="/tender/{TID}.ics"' in html
    assert "В календарь" in html


def test_card_hides_calendar_button_for_expired_deadline():
    html = _card_html(datetime.now(timezone.utc) - timedelta(days=10))
    assert f'href="/tender/{TID}.ics"' not in html
