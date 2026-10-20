"""Garbage/urn UUID in ``folder_id`` and ``?profile_id=`` → 404, not 500 (the class #632).

On 08.10 a raw string in ``CAST(:x AS uuid)`` gave ``DataError`` → 500. The tender id
was canonicalised (#636), but these two parameters were not: the folder in ``workflow_service``,
the profile in ``tender_ai_service``. Here we check the same contract as for
the tender: junk → 404 without a trip to the DB, ``urn:uuid:`` → work with the canonical.

The DB is not needed: a session that answers ``DataError`` on a non-canonical
``fid``/``profile_id`` (as asyncpg does in production) proves that before the normalization
the value reached the SQL.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from src.api import deps
from src.api.cabinet import tender_ai_service as ai_svc
from src.api.cabinet import workflow_service as svc
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
CANON = "22222222-2222-2222-2222-222222222222"
PROFILE = "33333333-3333-3333-3333-333333333333"
FOLDER = "44444444-4444-4444-4444-444444444444"
GARBAGE = ["not-a-uuid", "12345", "4444444444444444444444444444444x"]


def _is_canonical(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


class _UuidStrictSession(FakeSession):
    """asyncpg in production: ``CAST(:fid/:profile_id AS uuid)`` with a non-canonical → DataError."""

    async def execute(self, statement, params=None):
        params = params or {}
        for key in ("fid", "profile_id"):
            value = params.get(key)
            if value is not None and not _is_canonical(value):
                raise DataError(
                    f"CAST(:{key} AS uuid)", {}, Exception(f"invalid UUID {value!r}")
                )
        return await super().execute(statement, params)


def _client():
    def factory():
        return _UuidStrictSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    return TestClient(app, follow_redirects=False, raise_server_exceptions=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


def _patch_render(monkeypatch):
    monkeypatch.setattr(
        svc,
        "get_tender_state",
        _async(
            svc.TenderState(
                is_watched=False, note=None, folder_ids=frozenset(), status="new"
            )
        ),
    )
    monkeypatch.setattr(svc, "list_folders", _async([]))


# --------------------------------------------------------------------------- #
# folder_id in add/remove
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize("bad", GARBAGE)
def test_folder_garbage_id_is_404(action, bad):
    resp = _client().post(f"/app/tenders/{CANON}/folders/{bad}/{action}")
    assert resp.status_code == 404


@pytest.mark.parametrize("action", ["add", "remove"])
def test_folder_urn_id_operates_on_canonical(monkeypatch, action):
    _patch_render(monkeypatch)
    seen = {}

    async def _stub(db, *, tenant_id, folder_id, tender_id):
        seen["folder_id"] = folder_id

    name = "add_to_folder" if action == "add" else "remove_from_folder"
    monkeypatch.setattr(svc, name, _stub)
    resp = _client().post(f"/app/tenders/{CANON}/folders/urn:uuid:{FOLDER}/{action}")
    assert resp.status_code == 200
    assert seen["folder_id"] == FOLDER


# --------------------------------------------------------------------------- #
# ?profile_id= on the card and in the artifacts service
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", GARBAGE)
def test_detail_garbage_profile_is_404(bad):
    resp = _client().get(f"/app/tenders/{CANON}?profile_id={bad}")
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", GARBAGE)
async def test_service_garbage_profile_returns_none_without_db(bad):
    s = _UuidStrictSession()
    data = await ai_svc.get_tender_analyses(
        s, tid=SESS["tid"], tender_id=CANON, profile_id=bad
    )
    assert data is None


@pytest.mark.asyncio
async def test_service_urn_profile_uses_canonical_id():
    s = _UuidStrictSession(
        results=[
            FakeResult(items=[SimpleNamespace(name="ИТ")]),
            FakeResult(
                items=[
                    SimpleNamespace(
                        title="Т",
                        description=None,
                        customer_name=None,
                        deadline_at=None,
                        source_url=None,
                    )
                ]
            ),
            FakeResult(items=[]),
        ]
    )
    data = await ai_svc.get_tender_analyses(
        s, tid=SESS["tid"], tender_id=CANON, profile_id=f"urn:uuid:{PROFILE}"
    )
    assert data is not None
    assert data.profile_id == PROFILE
