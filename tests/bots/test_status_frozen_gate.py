"""Tests of the freeze predicate of the funnel buttons ``st:`` (FIX-SG-04).

``status_service.is_tenant_frozen`` — a light SELECT of ``access_until`` + the SSOT predicate
:func:`src.domain.access.is_frozen`. Without a network and DB (ADR-013). We cover:
- access_until expired → True (the tenant is frozen, we gate the button).
- access_until = NULL (permanent access) → False.
- access_until in the future → False.
- no row (a tenant without a record) → False (permanent, we do not block).

No write is made in any branch (without ``commit``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bots.status_service import is_tenant_frozen

_TENANT_ID = "22222222-2222-2222-2222-222222222222"

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)


def _session_returning(row: SimpleNamespace | None) -> AsyncMock:
    """Mock session: ``execute`` returns a result with ``.first() == row``."""
    result = MagicMock()
    result.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_expired_access_is_frozen():
    """access_until in the past → frozen (True)."""
    session = _session_returning(SimpleNamespace(access_until=_PAST))
    assert await is_tenant_frozen(session, _TENANT_ID) is True
    session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_null_access_is_not_frozen():
    """access_until = NULL (permanent access) → NOT frozen (False)."""
    session = _session_returning(SimpleNamespace(access_until=None))
    assert await is_tenant_frozen(session, _TENANT_ID) is False


@pytest.mark.asyncio
async def test_future_access_is_not_frozen():
    """access_until in the future → NOT frozen (False)."""
    session = _session_returning(SimpleNamespace(access_until=_FUTURE))
    assert await is_tenant_frozen(session, _TENANT_ID) is False


@pytest.mark.asyncio
async def test_missing_row_is_not_frozen():
    """There is no tenants row → NOT frozen (False), we do not block the button."""
    session = _session_returning(None)
    assert await is_tenant_frozen(session, _TENANT_ID) is False
