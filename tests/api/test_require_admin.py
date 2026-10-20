"""Tests of the gate deps.require_admin (ADMIN-OPERATOR-GATE).

The gate decides ONLY the outcome from three inputs: the allowlist is configured, the session is there, the session belongs to the operator. The resolution itself “operator or not” lives in
:mod:`src.api.admin_access` (see tests/api/test_admin_access.py) — a single source of truth for the gate and for the link in the header.

The three outcomes differ on purpose (TASK admin-login):
- no session → 303 to /admin/login with a return (a bare 403 read as “broken”, not as “log in”);
- the session exists, but it is not the operator → 403: we do not reveal the existence of the admin to an outsider and do not call for login;
- an empty allowlist → fail-closed, 403 to everyone, including an anonymous user.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api import deps
from src.api.config import CabinetSettings

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_SESSION = {"uid": _UID, "tid": _TID}


def _settings(emails: str) -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=emails)


def _request(path: str = "/admin/users", query: str = "", headers=None) -> Request:
    """The minimal ASGI scope: the gate needs url.path/url.query and the headers."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("nab.rs", 443),
            "path": path,
            "query_string": query.encode(),
            "headers": [
                (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
            ],
        }
    )


@pytest.mark.asyncio
async def test_session_none_redirects_to_admin_login():
    """An anonymous user sees the login form, not a bare 403 (“I don’t understand how to get in”)."""
    with pytest.raises(HTTPException) as exc:
        await deps.require_admin(
            request=_request(),
            session=None,
            is_admin=False,
            settings=_settings("op@example.com"),
        )
    assert exc.value.status_code == 303
    assert exc.value.headers["Location"] == "/admin/login?next=%2Fadmin%2Fusers"


@pytest.mark.asyncio
async def test_session_none_next_carries_query_string():
    with pytest.raises(HTTPException) as exc:
        await deps.require_admin(
            request=_request(path="/admin/audit", query="page=3"),
            session=None,
            is_admin=False,
            settings=_settings("op@example.com"),
        )
    assert (
        exc.value.headers["Location"] == "/admin/login?next=%2Fadmin%2Faudit%3Fpage%3D3"
    )


@pytest.mark.asyncio
async def test_session_none_htmx_stays_403():
    """XHR waits for a fragment — a redirect to the HTML form would break it."""
    with pytest.raises(HTTPException) as exc:
        await deps.require_admin(
            request=_request(headers={"HX-Request": "true"}),
            session=None,
            is_admin=False,
            settings=_settings("op@example.com"),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_empty_allowlist_returns_403_even_for_operator():
    """Fail-closed: even if the resolution somehow said “operator”."""
    with pytest.raises(HTTPException) as exc:
        await deps.require_admin(
            request=_request(),
            session=_SESSION,
            is_admin=True,
            settings=_settings(""),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_empty_allowlist_returns_403_for_anon_too():
    """Fail-closed is stronger than convenience: there are no operators — nobody is called to log in."""
    with pytest.raises(HTTPException) as exc:
        await deps.require_admin(
            request=_request(),
            session=None,
            is_admin=False,
            settings=_settings(""),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_operator_gets_access():
    result = await deps.require_admin(
        request=_request(),
        session=_SESSION,
        is_admin=True,
        settings=_settings("op@example.com"),
    )
    assert result == _SESSION


@pytest.mark.asyncio
async def test_non_operator_with_session_returns_403_not_redirect():
    """A logged-in outsider: 403, the existence of the admin is not revealed."""
    with pytest.raises(HTTPException) as exc:
        await deps.require_admin(
            request=_request(),
            session=_SESSION,
            is_admin=False,
            settings=_settings("op@example.com"),
        )
    assert exc.value.status_code == 403
    assert "Location" not in (exc.value.headers or {})


def test_admin_login_url_falls_back_on_hostile_path():
    """The path is taken from the request, but still passes the validation of the return."""
    assert deps.admin_login_url(_request(path="/admin/x", query="a=1:2")) == (
        "/admin/login?next=%2Fadmin"
    )
