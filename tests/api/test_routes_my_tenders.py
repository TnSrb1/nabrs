"""Tests of the routes ``src/api/cabinet/routes_my_tenders.py`` (VG-17).

DB/session/service are mocked (ADR-013). We check the session gate (anonymous → login / HTMX 401), the render of the “My tenders” page, and that the HTMX actions return a re-rendered partial. The service logic is not duplicated (see test_workflow_service).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import workflow_service as svc
from src.api.main import app
from src.db.models.tender_status import STATUS_LABELS
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_FOLDER = "33333333-3333-3333-3333-333333333333"


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _state(*, is_watched=False, note=None, folder_ids=frozenset(), status="new"):
    return svc.TenderState(
        is_watched=is_watched, note=note, folder_ids=folder_ids, status=status
    )


def _patch_actions(monkeypatch, state=None, folders=None):
    """A mock for _render_actions: get_tender_state + list_folders."""
    monkeypatch.setattr(svc, "get_tender_state", _async(state or _state()))
    monkeypatch.setattr(svc, "list_folders", _async(folders or []))


# --------------------------------------------------------------------------- #
# GET /app/my
# --------------------------------------------------------------------------- #


def test_my_tenders_anonymous_redirected():
    resp = _client(sess=None).get("/app/my")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_my_tenders_renders(monkeypatch):
    dl = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    data = svc.MyTenders(
        folders=[
            svc.FolderView(
                id="f1",
                name="Приоритет",
                tenders=[
                    svc.MyTenderRow(
                        tender_id="t1",
                        title="Тендер 1",
                        customer_name="Наручилац",
                        deadline_at=dl,
                        tender_url="https://x/1",
                        note="важно",
                    )
                ],
            )
        ],
        funnel=[
            svc.MyTenderRow(
                tender_id="t9",
                title="Слежу",
                customer_name=None,
                deadline_at=dl,
                tender_url="https://x/9",
                status="watching",
            )
        ],
    )
    monkeypatch.setattr(svc, "get_my_tenders", _async(data))
    resp = _client(sess=SESS).get("/app/my")
    assert resp.status_code == 200
    assert "Мои тендеры" in resp.text
    assert "Приоритет" in resp.text
    assert "Тендер 1" in resp.text
    assert "важно" in resp.text
    assert "Слежу" in resp.text
    # The stage is labelled as in the bot, and the watching group is marked with reminders.
    assert "👀 Изучаю" in resp.text
    assert "Напоминания о приближении дедлайна придут в Telegram" in resp.text


# --------------------------------------------------------------------------- #
# A filter by stage on /app/my (VG-17w)
# --------------------------------------------------------------------------- #


def _page_data():
    return svc.MyTenders(
        folders=[],
        funnel=[
            svc.MyTenderRow("t1", "Изучаю это", None, None, None, status="watching"),
            svc.MyTenderRow("t2", "Победа", None, None, None, status="won"),
        ],
    )


def test_my_tenders_filters_by_stage(monkeypatch):
    monkeypatch.setattr(svc, "get_my_tenders", _async(_page_data()))
    resp = _client(sess=SESS).get("/app/my?stage=won")
    assert resp.status_code == 200
    assert "Победа" in resp.text
    assert "Изучаю это" not in resp.text
    # The chips are counted over the full aggregate — one can leave the filter.
    assert "/app/my?stage=watching" in resp.text


def test_my_tenders_unknown_stage_shows_everything(monkeypatch):
    """Junk in ?stage= need not show an empty page."""
    monkeypatch.setattr(svc, "get_my_tenders", _async(_page_data()))
    resp = _client(sess=SESS).get("/app/my?stage=bogus")
    assert resp.status_code == 200
    assert "Победа" in resp.text and "Изучаю это" in resp.text


def test_my_tenders_export_link_survives_empty_stage_filter(monkeypatch):
    """The filter is empty, but there is something to export → the export link is in place."""
    monkeypatch.setattr(svc, "get_my_tenders", _async(_page_data()))
    resp = _client(sess=SESS).get("/app/my?stage=lost")
    assert "/app/my/export.csv" in resp.text
    assert "На этой стадии тендеров нет" in resp.text


# --------------------------------------------------------------------------- #
# Watch toggle (HTMX)
# --------------------------------------------------------------------------- #


def test_watch_anonymous_hx_401():
    resp = _client(sess=None).post(
        f"/app/tenders/{_TENDER}/watch", data={"watch": "on"}
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


def test_watch_on_returns_partial(monkeypatch):
    monkeypatch.setattr(svc, "set_watch", _async(True))
    _patch_actions(monkeypatch, state=_state(is_watched=True))
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/watch", data={"watch": "on"}
    )
    assert resp.status_code == 200
    assert f"actions-{_TENDER}" in resp.text
    assert "Слежу" in resp.text  # the state reflects watched=True


def test_watch_off_returns_partial(monkeypatch):
    monkeypatch.setattr(svc, "set_watch", _async(False))
    _patch_actions(monkeypatch, state=_state(is_watched=False))
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/watch", data={"watch": "off"}
    )
    assert resp.status_code == 200
    assert "Следить" in resp.text


# --------------------------------------------------------------------------- #
# The funnel stage (HTMX) — VG-17w
# --------------------------------------------------------------------------- #


def test_status_anonymous_hx_401():
    resp = _client(sess=None).post(
        f"/app/tenders/{_TENDER}/status", data={"status": "active"}
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


@pytest.mark.parametrize("status,label", list(STATUS_LABELS.items()))
def test_status_each_stage_returns_partial(monkeypatch, status, label):
    """All 7 stages are available from the cabinet and return a re-rendered partial."""
    seen = {}

    async def _spy(db, *, tenant_id, tender_id, status):
        seen.update(tenant_id=tenant_id, tender_id=tender_id, status=status)
        return True

    monkeypatch.setattr(svc, "set_status", _spy)
    _patch_actions(monkeypatch, state=_state(status=status))
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/status", data={"status": status}
    )
    assert resp.status_code == 200
    assert f"actions-{_TENDER}" in resp.text
    assert label in resp.text
    # The route passes the stage as is, and the SESSION’s tenant (not the request’s).
    assert seen == {"tenant_id": SESS["tid"], "tender_id": _TENDER, "status": status}


def test_status_rejected_value_is_400_not_500(monkeypatch):
    """The service rejected the value → 400, the partial is not rendered."""
    monkeypatch.setattr(svc, "set_status", _async(False))
    _patch_actions(monkeypatch)
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/status", data={"status": "bogus"}
    )
    assert resp.status_code == 400
    assert f"actions-{_TENDER}" not in resp.text


def test_status_missing_field_is_422():
    """A form without status — a validation error, not a silent default."""
    resp = _client(sess=SESS).post(f"/app/tenders/{_TENDER}/status", data={})
    assert resp.status_code == 422


def test_watch_route_still_works_alongside_status(monkeypatch):
    """The existing watch toggle is not broken by the new route (both are in the current UI)."""
    monkeypatch.setattr(svc, "set_watch", _async(True))
    _patch_actions(monkeypatch, state=_state(is_watched=True, status="watching"))
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/watch", data={"watch": "on"}
    )
    assert resp.status_code == 200
    assert "Слежу" in resp.text


# --------------------------------------------------------------------------- #
# Note / folders (HTMX)
# --------------------------------------------------------------------------- #


def test_save_note_returns_partial(monkeypatch):
    monkeypatch.setattr(svc, "save_note", _async("текст"))
    _patch_actions(monkeypatch, state=_state(note="текст"))
    resp = _client(sess=SESS).post(
        f"/app/tenders/{_TENDER}/note", data={"body": "текст"}
    )
    assert resp.status_code == 200
    assert "текст" in resp.text


def test_add_to_folder_returns_partial(monkeypatch):
    monkeypatch.setattr(svc, "add_to_folder", _async(True))
    _patch_actions(
        monkeypatch,
        state=_state(folder_ids=frozenset({_FOLDER})),
        folders=[svc.FolderSummary(_FOLDER, "Приоритет", 1)],
    )
    resp = _client(sess=SESS).post(f"/app/tenders/{_TENDER}/folders/{_FOLDER}/add")
    assert resp.status_code == 200
    assert "Приоритет" in resp.text


def test_remove_from_folder_returns_partial(monkeypatch):
    monkeypatch.setattr(svc, "remove_from_folder", _async(None))
    _patch_actions(monkeypatch, folders=[svc.FolderSummary(_FOLDER, "Приоритет", 0)])
    resp = _client(sess=SESS).post(f"/app/tenders/{_TENDER}/folders/{_FOLDER}/remove")
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Folder management (regular forms)
# --------------------------------------------------------------------------- #


def test_create_folder_redirects(monkeypatch):
    monkeypatch.setattr(svc, "create_folder", _async(svc.FolderResult(True, "f1")))
    resp = _client(sess=SESS).post("/app/folders", data={"name": "Новая"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/my"


def test_create_folder_anonymous_redirected():
    resp = _client(sess=None).post("/app/folders", data={"name": "X"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_delete_folder_redirects(monkeypatch):
    monkeypatch.setattr(svc, "delete_folder", _async(None))
    resp = _client(sess=SESS).post(f"/app/folders/{_FOLDER}/delete")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/app/my"


# --------------------------------------------------------------------------- #
# GET /app/my/export.csv (F2)
# --------------------------------------------------------------------------- #


def _export_row(**kw):
    base = dict(
        tender_id="t1",
        title="Тендер 1",
        customer_name="Град Београд",
        amount=Decimal("1000"),
        currency="RSD",
        deadline_at=datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc),
        tender_url="https://x/1",
        user_status="active",
        folder_names="Приоритет",
    )
    base.update(kw)
    return svc.MyTenderExportRow(**base)


def test_export_csv_anonymous_redirected():
    resp = _client(sess=None).get("/app/my/export.csv")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_export_csv_downloads_as_attachment(monkeypatch):
    monkeypatch.setattr(svc, "get_my_tenders_export", _async([_export_row()]))
    resp = _client(sess=SESS).get("/app/my/export.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "my_tenders.csv" in resp.headers["content-disposition"]
    # BOM + Cyrillic: Excel must open it without dances with the import.
    assert resp.content.startswith(b"\xef\xbb\xbf")
    body = resp.content.decode("utf-8-sig")
    assert "Град Београд" in body
    # The stage — from the SSOT of the common lexicon (COMMON), the same caption as in the cabinet and the bot.
    # Previously the export kept its own dictionary and wrote “participating” in lowercase.
    assert "Участвую" in body


def test_export_csv_empty_list_is_200_with_headers(monkeypatch):
    """Nothing to export — CSV headers and 200, not a 500 or an empty file."""
    monkeypatch.setattr(svc, "get_my_tenders_export", _async([]))
    resp = _client(sess=SESS).get("/app/my/export.csv")
    assert resp.status_code == 200
    assert "Название" in resp.content.decode("utf-8-sig")


def test_export_csv_asks_service_for_session_tenant(monkeypatch):
    """The route exports the tenant of the SESSION — not what would come from the request."""
    seen = {}

    async def _spy(db, tenant_id):
        seen["tid"] = tenant_id
        return []

    monkeypatch.setattr(svc, "get_my_tenders_export", _spy)
    _client(sess=SESS).get("/app/my/export.csv?tid=other-tenant")
    assert seen["tid"] == SESS["tid"]


def test_my_tenders_page_offers_export_link(monkeypatch):
    data = svc.MyTenders(
        folders=[],
        funnel=[
            svc.MyTenderRow(
                tender_id="t9",
                title="Слежу",
                customer_name=None,
                deadline_at=None,
                tender_url=None,
                status="watching",
            )
        ],
    )
    monkeypatch.setattr(svc, "get_my_tenders", _async(data))
    assert "/app/my/export.csv" in _client(sess=SESS).get("/app/my").text


def test_export_link_hidden_when_nothing_to_export(monkeypatch):
    monkeypatch.setattr(
        svc, "get_my_tenders", _async(svc.MyTenders(folders=[], funnel=[]))
    )
    assert "/app/my/export.csv" not in _client(sess=SESS).get("/app/my").text
