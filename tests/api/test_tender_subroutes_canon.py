"""Normalisation of ``{tender_id}`` in ALL cabinet sub-routes ``/app/tenders/{id}…`` (#632).

On 08.10 ``/app/tenders/urn:uuid:…`` (and ``{…}``/UPPER) went as a raw string into ``CAST(:tender_id AS uuid)`` → DataError → 500. The card was canonicalised in #636; here — the other routes of the same prefix:

- GET (details, ``/description``, ``/title``, ``/analytics.csv``, ``/status/{level}``): junk → 404 without a trip to the DB; ``urn:uuid:``/``{…}``/UPPER → 301 to the canonical.
- POST (``/watch``, ``/status``, ``/note``, ``/folders/…``, ``/generate/{level}``): a 301 on a POST would give the browser a GET to a non-existent address, therefore the canonical is taken inside the route, and the operation itself goes with the canonical id.

The DB is not needed: a session factory that falls when called proves that the route does not reach the DB before the normalization.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet import tender_ai_generate as gen
from src.api.cabinet import workflow_service as svc
from src.api.main import app
from tests.api.conftest import FakeResult, FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
CANON = "22222222-2222-2222-2222-222222222222"
UPPER = "AAAAAAAA-2222-2222-2222-222222222222"
UPPER_CANON = "aaaaaaaa-2222-2222-2222-222222222222"
PROFILE = "33333333-3333-3333-3333-333333333333"
FOLDER = "44444444-4444-4444-4444-444444444444"

URN = f"urn:uuid:{CANON}"
BRACED = "{22222222-2222-2222-2222-222222222222}"
GARBAGE = ["not-a-uuid", "12345", "2222222222222222222222222222222x"]

# Path templates: ``{id}`` — we substitute junk / urn / canonical.
GET_ROUTES = [
    "/app/tenders/{id}?profile_id=" + PROFILE,
    "/app/tenders/{id}/description?profile_id=" + PROFILE,
    "/app/tenders/{id}/title?profile_id=" + PROFILE + "&view=feed",
    "/app/tenders/{id}/analytics.csv?profile_id=" + PROFILE + "&kind=customer",
    "/app/tenders/{id}/status/bid?profile_id=" + PROFILE,
]

POST_ROUTES = [
    ("/app/tenders/{id}/watch", {"watch": "on"}),
    ("/app/tenders/{id}/status", {"status": "active"}),
    ("/app/tenders/{id}/note", {"body": "заметка"}),
    (f"/app/tenders/{{id}}/folders/{FOLDER}/add", {}),
    (f"/app/tenders/{{id}}/folders/{FOLDER}/remove", {}),
    ("/app/tenders/{id}/generate/bid?profile_id=" + PROFILE, {}),
]


class _UuidStrictSession(FakeSession):
    """As asyncpg does in production: ``CAST(:tender_id AS uuid)`` with a non-canonical value → DataError (500).

    The access gate also goes through this session (the parameters tid/uid — not the tender), therefore we check only the names of the parameters with the tender id: ``tender_id`` (the AI routes) and ``tender`` (the workflow routes). The profile “exists” — otherwise the service answers 404 before the tender id reaches the DB, and the test proves nothing.
    """

    async def execute(self, statement, params=None):
        params = params or {}
        for key in ("tender_id", "tender"):
            value = params.get(key)
            if value is not None and not _is_canonical(value):
                raise DataError(
                    f"CAST(:{key} AS uuid)",
                    {},
                    Exception(f"invalid UUID {value!r}"),
                )
        if "profile_id" in params and "tender_id" not in params:
            return FakeResult(items=[SimpleNamespace(name="Профиль")])
        return await super().execute(statement, params)


def _is_canonical(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


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


def _capture(monkeypatch, target, name, result):
    """Substitutes the async service and remembers the ``tender_id`` with which it was called."""
    seen = {}

    async def _stub(*args, **kwargs):
        seen["tender_id"] = kwargs.get("tender_id")
        return result

    monkeypatch.setattr(target, name, _stub)
    return seen


def _patch_render(monkeypatch):
    """_render_actions reads the tender state — we give an empty one."""
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
# GET: junk → 404, urn/{…}/UPPER → 301 to the canonical
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("route", GET_ROUTES)
@pytest.mark.parametrize("bad", GARBAGE)
def test_get_subroute_garbage_id_is_404(route, bad):
    resp = _client().get(route.replace("{id}", bad))
    assert resp.status_code == 404


@pytest.mark.parametrize("route", GET_ROUTES)
def test_get_subroute_urn_redirects_to_canon_literal(route):
    resp = _client().get(route.replace("{id}", URN))
    assert resp.status_code == 301
    assert resp.headers["location"] == route.replace("{id}", CANON)


@pytest.mark.parametrize("route", GET_ROUTES)
def test_get_subroute_braced_id_redirects_to_canon(route):
    resp = _client().get(route.replace("{id}", BRACED))
    assert resp.status_code == 301
    assert resp.headers["location"] == route.replace("{id}", CANON)


@pytest.mark.parametrize("route", GET_ROUTES)
def test_get_subroute_upper_id_redirects_to_lower_canon(route):
    resp = _client().get(route.replace("{id}", UPPER))
    assert resp.status_code == 301
    assert resp.headers["location"] == route.replace("{id}", UPPER_CANON)


def test_urn_redirect_literal_for_detail_keeps_query():
    resp = _client().get(f"/app/tenders/{URN}?profile_id={PROFILE}")
    assert resp.status_code == 301
    assert resp.headers["location"] == f"/app/tenders/{CANON}?profile_id={PROFILE}"


def test_urn_csv_redirect_keeps_export_kind():
    resp = _client().get(
        f"/app/tenders/{URN}/analytics.csv?profile_id={PROFILE}&kind=competitor"
    )
    assert resp.status_code == 301
    assert resp.headers["location"] == (
        f"/app/tenders/{CANON}/analytics.csv?profile_id={PROFILE}&kind=competitor"
    )


def test_canonical_get_subroute_is_not_redirected(monkeypatch):
    """The canonical must not go into a 301 loop: /title with the canonical answers 200."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(None))
    resp = _client().get(f"/app/tenders/{CANON}/title?profile_id={PROFILE}&view=feed")
    assert resp.status_code != 301


# --------------------------------------------------------------------------- #
# POST: junk → 404 without DB; urn → works with the canonical id (without 301)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path, data", POST_ROUTES)
@pytest.mark.parametrize("bad", GARBAGE)
def test_post_subroute_garbage_id_is_404(path, data, bad):
    resp = _client().post(path.replace("{id}", bad), data=data)
    assert resp.status_code == 404


def test_post_watch_urn_operates_on_canonical_id(monkeypatch):
    _patch_render(monkeypatch)
    seen = _capture(monkeypatch, svc, "set_watch", True)
    resp = _client().post(f"/app/tenders/{URN}/watch", data={"watch": "on"})
    assert resp.status_code == 200
    assert seen["tender_id"] == CANON


def test_post_status_urn_operates_on_canonical_id(monkeypatch):
    _patch_render(monkeypatch)
    seen = _capture(monkeypatch, svc, "set_status", True)
    resp = _client().post(f"/app/tenders/{BRACED}/status", data={"status": "active"})
    assert resp.status_code == 200
    assert seen["tender_id"] == CANON


def test_post_note_urn_operates_on_canonical_id(monkeypatch):
    _patch_render(monkeypatch)
    seen = _capture(monkeypatch, svc, "save_note", None)
    resp = _client().post(f"/app/tenders/{UPPER}/note", data={"body": "x"})
    assert resp.status_code == 200
    assert seen["tender_id"] == UPPER_CANON


def test_post_folder_add_urn_operates_on_canonical_id(monkeypatch):
    _patch_render(monkeypatch)
    seen = _capture(monkeypatch, svc, "add_to_folder", None)
    resp = _client().post(f"/app/tenders/{URN}/folders/{FOLDER}/add")
    assert resp.status_code == 200
    assert seen["tender_id"] == CANON


def test_post_folder_remove_urn_operates_on_canonical_id(monkeypatch):
    _patch_render(monkeypatch)
    seen = _capture(monkeypatch, svc, "remove_from_folder", None)
    resp = _client().post(f"/app/tenders/{URN}/folders/{FOLDER}/remove")
    assert resp.status_code == 200
    assert seen["tender_id"] == CANON


def test_post_generate_urn_operates_on_canonical_id(monkeypatch):
    seen = {}

    async def _precheck(db, *, tid, tender_id, profile_id, level):
        seen["tender_id"] = tender_id
        return gen.STATUS_NO_TENDER

    monkeypatch.setattr(gen, "precheck_generation", _precheck)
    resp = _client().post(f"/app/tenders/{URN}/generate/bid?profile_id={PROFILE}")
    assert resp.status_code != 500
    assert seen["tender_id"] == CANON
