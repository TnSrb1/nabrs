"""The “full analysis in the cabinet” link from the Telegram footer leads to a live route (#634, PR #652).

The route ``/app/tenders/{id}`` without ``profile_id`` returns 404 (``routes_tender_ai.py``),
so the link from the bot must carry ``?profile_id=``. Proof without a test DB:
the analytics service is substituted with a signal — if the request reached it, the 404 check passed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import deps, security
from src.api.cabinet import routes_tender_ai
from src.api.config import get_cabinet_settings
from src.api.main import app
from src.bots.telegram import cabinet_tender_url
from tests.api.conftest import FakeSession

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE_ID = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_BASE = "https://app.nab.rs"
SESS = {"uid": "u-1", "tid": "t-1"}


class _ReachedService(Exception):
    """Signal: the request passed the 404 guard and reached the service."""


def _client():
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    client = TestClient(app, follow_redirects=False)
    settings = get_cabinet_settings()
    client.cookies.set(
        "tb_session",
        security.sign_session(SESS["uid"], SESS["tid"], settings.cabinet_secret_key),
    )
    return client


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    app.dependency_overrides.clear()


def _path_of(url: str) -> str:
    return url[len(_BASE) :]


def test_link_without_profile_id_is_404_in_cabinet():
    """The contract that was in the bot: a URL without profile_id → 404 (fixes the cause of the bug)."""
    resp = _client().get(f"/app/tenders/{_TENDER_ID}")
    assert resp.status_code == 404


def test_link_with_profile_id_passes_404_guard(monkeypatch):
    """The URL the bot builds (cabinet_tender_url) reaches the service — not 404."""

    async def _reached(db, **kwargs):
        raise _ReachedService

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _reached)
    url = cabinet_tender_url(_BASE, _TENDER_ID, _PROFILE_ID)
    client = _client()
    with pytest.raises(_ReachedService):
        client.get(_path_of(url))
