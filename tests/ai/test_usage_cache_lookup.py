"""Tests of cache_lookup from src/ai/usage.py (hotfix profile_id=NULL).

We check:
- profile_id=None → the NULL query branch, the parameters only (tender_id, level).
- profile_id=<uuid> → the PROFILE query branch, the parameters (tender_id, level, profile_id).
- Both paths return str(UUID) on a hit and None on a miss.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai.usage import (
    _CACHE_LOOKUP_NULL_SQL,
    _CACHE_LOOKUP_PROFILE_SQL,
    cache_lookup,
)

_TENDER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ANALYSIS_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _session_returning(row) -> AsyncMock:
    """Mock session: the single execute returns a row via .first()."""
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = row

    # We record the calls to check the SQL and the parameters.
    captured = {}

    async def _exec(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params or {}
        return result

    session.execute = _exec
    session._captured = captured  # type: ignore[attr-defined]
    return session


# --------------------------------------------------------------------------- #
#  profile_id=None → the NULL branch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_lookup_null_uses_null_sql_on_hit():
    """profile_id=None, cache hit → returns a UUID, uses the NULL query."""
    row = SimpleNamespace(id=_ANALYSIS_ID)
    session = _session_returning(row)

    result = await cache_lookup(session, _TENDER_ID, None, "deep")

    assert result == _ANALYSIS_ID
    # We make sure: the NULL query was used, not PROFILE
    assert session._captured["sql"] is _CACHE_LOOKUP_NULL_SQL  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cache_lookup_null_no_profile_id_in_params():
    """profile_id=None → the parameters do NOT contain 'profile_id'."""
    session = _session_returning(None)

    await cache_lookup(session, _TENDER_ID, None, "quick")

    params = session._captured["params"]  # type: ignore[attr-defined]
    assert "profile_id" not in params
    assert params["tender_id"] == _TENDER_ID
    assert params["level"] == "quick"


@pytest.mark.asyncio
async def test_cache_lookup_null_miss_returns_none():
    """profile_id=None, miss → None."""
    session = _session_returning(None)
    result = await cache_lookup(session, _TENDER_ID, None, "deep")
    assert result is None


# --------------------------------------------------------------------------- #
#  profile_id=<uuid> → the PROFILE branch
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cache_lookup_profile_uses_profile_sql_on_hit():
    """profile_id=UUID, cache hit → returns a UUID, uses the PROFILE query."""
    row = SimpleNamespace(id=_ANALYSIS_ID)
    session = _session_returning(row)

    result = await cache_lookup(session, _TENDER_ID, _PROFILE_ID, "standard")

    assert result == _ANALYSIS_ID
    assert session._captured["sql"] is _CACHE_LOOKUP_PROFILE_SQL  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cache_lookup_profile_includes_profile_id_in_params():
    """profile_id=UUID → the parameters contain 'profile_id'."""
    session = _session_returning(None)

    await cache_lookup(session, _TENDER_ID, _PROFILE_ID, "standard")

    params = session._captured["params"]  # type: ignore[attr-defined]
    assert params["profile_id"] == _PROFILE_ID
    assert params["tender_id"] == _TENDER_ID
    assert params["level"] == "standard"


@pytest.mark.asyncio
async def test_cache_lookup_profile_miss_returns_none():
    """profile_id=UUID, miss → None."""
    session = _session_returning(None)
    result = await cache_lookup(session, _TENDER_ID, _PROFILE_ID, "standard")
    assert result is None
