"""Tests of the shared “platform operator” resolution (src.api.admin_access).

One source of truth for the admin gate (deps.require_admin) and for the “Admin” link
in the cabinet header — otherwise access and the link drift apart. Here we check
the allowlist parsing itself and the email match against the DB.

users.role='admin' is not suitable as an operator marker (the first user of EACH
tenant receives it at signup, see #283).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api import admin_access
from src.api.config import CabinetSettings
from tests.api.conftest import FakeResult, FakeSession, make_session_factory

_UID = "aaaa0000-0000-0000-0000-000000000001"
_TID = "bbbb0000-0000-0000-0000-000000000002"
_SESSION = {"uid": _UID, "tid": _TID}


def _settings(emails: str) -> CabinetSettings:
    return CabinetSettings(CABINET_ADMIN_EMAILS=emails)


def _db(email: str | None):
    items = [SimpleNamespace(email=email)] if email is not None else []
    return FakeSession(results=[FakeResult(items=items)])


def test_parse_allowlist_normalizes_and_drops_blanks():
    assert admin_access.parse_allowlist(
        _settings(" Op@X.com , ,other@example.com ")
    ) == {
        "op@x.com",
        "other@example.com",
    }


def test_parse_allowlist_empty_config():
    assert admin_access.parse_allowlist(_settings("")) == set()
    assert admin_access.parse_allowlist(_settings(" , ")) == set()


@pytest.mark.asyncio
async def test_anon_is_not_operator():
    fs = FakeSession()
    assert (
        await admin_access.is_platform_admin(
            None, make_session_factory(fs), _settings("op@example.com")
        )
        is False
    )
    assert fs.executed == []  # anonymous — without a DB query


@pytest.mark.asyncio
async def test_empty_allowlist_is_fail_closed_without_db():
    fs = _db("op@example.com")
    assert (
        await admin_access.is_platform_admin(
            _SESSION, make_session_factory(fs), _settings("")
        )
        is False
    )
    assert fs.executed == []


@pytest.mark.asyncio
async def test_email_in_allowlist():
    assert (
        await admin_access.is_platform_admin(
            _SESSION,
            make_session_factory(_db("op@example.com")),
            _settings("op@example.com"),
        )
        is True
    )


@pytest.mark.asyncio
async def test_email_not_in_allowlist():
    assert (
        await admin_access.is_platform_admin(
            _SESSION,
            make_session_factory(_db("user@example.com")),
            _settings("op@example.com"),
        )
        is False
    )


@pytest.mark.asyncio
async def test_no_user_row():
    """A deleted/non-existent user with a live cookie is not an operator."""
    assert (
        await admin_access.is_platform_admin(
            _SESSION, make_session_factory(_db(None)), _settings("op@example.com")
        )
        is False
    )


@pytest.mark.asyncio
async def test_normalization_matches_case_and_whitespace():
    assert (
        await admin_access.is_platform_admin(
            _SESSION,
            make_session_factory(_db("Op@X.com ")),
            _settings(" op@x.com , other@example.com"),
        )
        is True
    )


@pytest.mark.asyncio
async def test_sets_tenant_guc_before_select():
    """ADR-002: set_tenant is the FIRST action, otherwise RLS returns 0 rows."""
    fs = _db("op@example.com")
    await admin_access.is_platform_admin(
        _SESSION, make_session_factory(fs), _settings("op@example.com")
    )
    assert "app.current_tenant_id" in fs.executed[0][0]
    assert "SELECT email FROM users" in fs.executed[1][0]


@pytest.mark.asyncio
async def test_info_inbox_is_not_platform_admin():
    """info@nab.rs receives the operator’s emails, but we do NOT give it admin login:
    it is not in CABINET_ADMIN_EMAILS, and resolve must not let it in by address."""
    assert "info@nab.rs" not in admin_access.parse_allowlist(
        _settings("op@example.com")
    )
    assert (
        await admin_access.is_platform_admin(
            _SESSION,
            make_session_factory(_db("info@nab.rs")),
            _settings("op@example.com"),
        )
        is False
    )
