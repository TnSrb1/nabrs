"""Admin “Izvori” (SRC-0d): the page, enable/disable, audit, silence, inflow breakdown.

No DB (ADR-013): a mock session + ``dependency_overrides``, as in ``test_admin.py``.
The real SQL (``_SOURCES_ADMIN_SQL``, enable/disable) was run on a throwaway PG —
see TESTING_src0d_labels_admin.md. Expectations are literals.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.admin import admin_service as svc
from src.api.admin import sources_admin
from src.api.config import CabinetSettings
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_OPERATOR = "admin@example.com"
_NOW = datetime.now(timezone.utc)


def _src(key="jnportal", **kw):
    base = dict(
        source_key=key,
        display_name="Portal javnih nabavki",
        country_codes=["RS"],
        enabled=True,
        enabled_at=_NOW - timedelta(days=10),
        baseline_at=_NOW - timedelta(days=10),
        last_run_at=_NOW - timedelta(minutes=5),
        last_status="ok",
        error_count=0,
        cnt_24h=12,
        cnt_7d=80,
        cnt_total=4000,
        last_tender_at=_NOW - timedelta(hours=3),
    )
    base.update(kw)
    return SimpleNamespace(**base)


_FLAG = FakeResult(items=[SimpleNamespace(email=_OPERATOR)])


def _client(results, *, session=True):
    # first in the queue — the answer to the operator flag resolver (deps.require_admin)
    fs = FakeSession(results=([_FLAG] if session else []) + list(results))
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    app.dependency_overrides[deps.get_optional_session] = lambda: (
        {"uid": _UID, "tid": _TID} if session else None
    )
    app.dependency_overrides[deps.get_settings] = lambda: CabinetSettings(
        CABINET_ADMIN_EMAILS=_OPERATOR
    )
    return TestClient(app, follow_redirects=False), fs


def teardown_function():
    app.dependency_overrides.clear()


def _executed(fs, needle):
    return [(s, p) for s, p in fs.executed if needle in s]


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #


def test_sources_page_renders_row_with_counters_and_status():
    client, _ = _client([FakeResult(items=[_src(error_count=2, last_status="error")])])
    resp = client.get("/admin/sources")
    assert resp.status_code == 200
    html = resp.text
    assert 'data-source-row="jnportal"' in html
    assert "Portal javnih nabavki" in html
    assert "24 ч: 12 · 7 дн: 80 · всего: 4000" in html
    assert "статус: error" in html
    assert "ошибок подряд: 2" in html
    assert "тихий первый проход завершён" in html
    assert "/admin/sources/jnportal/disable" in html
    assert "data-silent-row" not in html


def test_sources_page_shows_quiet_first_pass_in_progress_and_enable_button():
    row = _src(key="ted", display_name="TED", enabled=False, baseline_at=None)
    client, _ = _client([FakeResult(items=[row])])
    html = client.get("/admin/sources").text
    assert "выключен" in html
    assert "/admin/sources/ted/enable" in html
    assert 'data-confirm="Включить источник «TED»?' in html

    row = _src(key="ted", display_name="TED", baseline_at=None, last_run_at=None)
    client, _ = _client([FakeResult(items=[row])])
    html = client.get("/admin/sources").text
    assert "тихий первый проход: в процессе" in html


def test_silent_enabled_source_gets_banner_and_row_alert():
    row = _src(last_tender_at=_NOW - timedelta(hours=40))  # jnportal threshold — 30 h
    client, _ = _client([FakeResult(items=[row])])
    html = client.get("/admin/sources").text
    assert "data-silent-banner" in html
    assert "data-silent-row" in html
    assert "молчит: проверить · 40 ч (порог 30 ч)" in html


def test_disabled_source_is_not_reported_silent():
    row = _src(enabled=False, last_tender_at=_NOW - timedelta(hours=400))
    client, _ = _client([FakeResult(items=[row])])
    html = client.get("/admin/sources").text
    assert "data-silent-banner" not in html
    assert "data-silent-row" not in html


def test_source_without_tenders_counts_silence_from_enabled_at():
    # enabled 2 h ago, no tenders — does not ring right away (owner evening item 4)
    fresh = _src(last_tender_at=None, enabled_at=_NOW - timedelta(hours=2))
    client, _ = _client([FakeResult(items=[fresh])])
    assert "data-silent-row" not in client.get("/admin/sources").text
    old = _src(last_tender_at=None, enabled_at=_NOW - timedelta(hours=50))
    client, _ = _client([FakeResult(items=[old])])
    assert "молчит: проверить · 50 ч" in client.get("/admin/sources").text


class _QueueByMarker(FakeSession):
    """Returns source rows only on a request for the source list."""

    def __init__(self, row):
        super().__init__(
            results=[
                _FLAG,
                FakeResult(items=[]),  # list_users: rows
                FakeResult(scalar=0),  # list_users: total
                FakeResult(
                    items=[
                        SimpleNamespace(total_all_time=0, total_month=0, total_day=0)
                    ]
                ),
                FakeResult(scalar=0),  # get_platform_stats: users total
            ]
        )
        self._row = row

    async def execute(self, statement, params=None):
        if "FROM sources s" in str(statement):
            self.executed.append((str(statement), params))
            return FakeResult(items=[self._row])
        return await super().execute(statement, params)


def test_banner_on_users_page_when_source_silent():
    """The banner — on the operator’s “dashboard” (/admin/users) too, not only on “Sources”."""
    row = _src(last_tender_at=_NOW - timedelta(hours=40))
    client, _ = _client([])
    fs = _QueueByMarker(row)
    app.dependency_overrides[deps.get_session_factory] = lambda: make_session_factory(
        fs
    )
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert "data-silent-banner" in resp.text
    assert "Portal javnih nabavki — 40 ч без новых тендеров (порог 30 ч)" in resp.text


# --------------------------------------------------------------------------- #
# POST enable/disable
# --------------------------------------------------------------------------- #

_ACTOR = FakeResult(
    items=[
        SimpleNamespace(
            id=_UID,
            email=_OPERATOR,
            created_at=_NOW,
            tenant_id=_TID,
            tier="pro",
            access_until=None,
            profile_count=1,
        )
    ]
)


def _before(enabled):
    return FakeResult(
        items=[
            SimpleNamespace(
                source_key="ted", enabled=enabled, enabled_at=None, baseline_at=None
            )
        ]
    )


def test_enable_uses_domain_contract_and_writes_audit():
    client, fs = _client([_ACTOR, _before(False)])
    resp = client.post("/admin/sources/ted/enable")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/sources"
    ((sql, params),) = _executed(fs, "SET enabled = true")
    assert "last_run_at = NULL" in sql and "baseline_at = NULL" in sql
    assert params["source_key"] == "ted"
    ((audit_sql, audit),) = _executed(fs, "app_admin_write_audit")
    assert audit["action"] == "source_enable"
    assert audit["actor"] == _OPERATOR
    assert '"before_enabled": false' in audit["details"]
    assert '"after_enabled": true' in audit["details"]
    assert fs.committed == 1


def test_disable_only_flips_flag_and_writes_audit():
    client, fs = _client([_ACTOR, _before(True)])
    resp = client.post("/admin/sources/ted/disable")
    assert resp.status_code == 303
    ((sql, _),) = _executed(fs, "SET enabled = false")
    assert "baseline_at" not in sql and "last_run_at" not in sql
    assert not _executed(fs, "SET enabled = true")
    ((_, audit),) = _executed(fs, "app_admin_write_audit")
    assert audit["action"] == "source_disable"
    assert fs.committed == 1


@pytest.mark.parametrize("path,state", [("enable", True), ("disable", False)])
def test_repeat_click_in_same_state_is_noop(path, state):
    """A double “Enable” does not reset the baseline_at of a live source."""
    client, fs = _client([_ACTOR, _before(state)])
    resp = client.post(f"/admin/sources/ted/{path}")
    assert resp.status_code == 303
    assert not _executed(fs, "UPDATE sources")
    assert not _executed(fs, "app_admin_write_audit")
    assert fs.committed == 0


def test_unknown_source_is_404():
    client, fs = _client([_ACTOR, FakeResult(items=[])])
    resp = client.post("/admin/sources/nope/enable")
    assert resp.status_code == 404
    assert not _executed(fs, "UPDATE sources")


def test_post_requires_admin():
    client, fs = _client([], session=False)
    resp = client.post("/admin/sources/ted/enable")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/login")
    assert not _executed(fs, "UPDATE sources")


# --------------------------------------------------------------------------- #
# /admin/health: breakdown and night-batch thresholds only for jnportal
# --------------------------------------------------------------------------- #


def _inflow(night_batch, hours, silent):
    return svc.SourceInflow(
        source_key="k",
        label="K",
        night_batch=night_batch,
        silent=silent,
        silent_hours=hours,
        last_tender_at=_NOW,
        tenders_24h=1,
        tenders_7d=2,
    )


def test_night_batch_thresholds_apply_only_to_night_batch_source():
    # 30 h without tenders: for the night batch — “window missed” (28 h), for the ordinary
    # a source with a threshold of 100 h (silent=False) — normal
    assert _inflow(True, 30.0, silent=True).status == "urgent"
    assert _inflow(True, 53.0, silent=True).status == "error"
    assert _inflow(True, 10.0, silent=False).status == "success"
    assert _inflow(False, 30.0, silent=False).status == "success"
    assert _inflow(False, 300.0, silent=True).status == "error"


def test_overall_tender_status_is_worst_source():
    base = dict(
        last_tender_at=_NOW,
        tenders_1h=0,
        tenders_24h=0,
        tenders_7d=0,
        last_award_at=None,
        awards_24h=0,
        awards_7d=0,
    )
    ok = _inflow(True, 5.0, silent=False)
    bad = _inflow(False, 300.0, silent=True)
    assert svc.IngestHealth(**base, sources=[ok]).tender_status == "success"
    assert svc.IngestHealth(**base, sources=[ok, bad]).tender_status == "error"


def test_adapter_attrs_marks_only_jnportal_as_night_batch():
    attrs = sources_admin.adapter_attrs()
    assert attrs["jnportal"]["night_batch"] is True
    assert all(not v["night_batch"] for k, v in attrs.items() if k != "jnportal")


# --------------------------------------------------------------------------- #
# Reconciliation with the source (M2): the “Reconciliation” column
# --------------------------------------------------------------------------- #


def _rec(day, src, ours, mismatch):
    return SimpleNamespace(
        source_key="jnportal",
        day=day,
        source_count=src,
        our_count=ours,
        checked_at=datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc),
        mismatch=mismatch,
    )


def test_sources_page_shows_last_reconcile_mismatch():
    from datetime import date

    recs = [  # day descending, as _RECONCILE_SQL returns it
        _rec(date(2026, 10, 15), 176, 175, False),
        _rec(date(2026, 10, 14), 200, 150, True),
    ]
    client, _ = _client([FakeResult(items=[_src()]), FakeResult(items=recs)])
    html = client.get("/admin/sources").text
    assert "data-reconcile-bad" in html
    assert "расхождение 14.10: источник 200 · у нас 150" in html
    assert "сверено 16.10.2026 02:00 UTC (за 15.10: 176 / 175)" in html


def test_sources_page_reconcile_match_and_empty():
    from datetime import date

    client, _ = _client(
        [
            FakeResult(items=[_src()]),
            FakeResult(items=[_rec(date(2026, 10, 15), 5, 5, False)]),
        ]
    )
    html = client.get("/admin/sources").text
    assert "совпало" in html and "data-reconcile-bad" not in html
    client, _ = _client(
        [FakeResult(items=[_src()])]
    )  # the reconciliation has not run yet
    html = client.get("/admin/sources").text
    assert "data-reconcile-cell" in html and "data-reconcile-bad" not in html
