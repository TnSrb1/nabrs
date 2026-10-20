"""Unit tests of the behavioral memory — the track record (:mod:`src.ai.track_record`).

Without a network and without a real DB (a mock session). We cover:
- ``load_track_record``: the counters submitted/won/lost + the top CPV among won (frequency).
- ``format_track_record``: a line with the history; empty (0 rows) → ``None``.
- ``build_track_record_context``: degradation to ``None`` on a query error.
- A structural RLS invariant WITHOUT a DB: the query is scoped by ``tenant_id`` (an explicit
  predicate + only the productive statuses) — there is no cross-tenant read.

Mutation: see ``tests/ai/test_profile_context.py::test_track_record_appended`` —
if the track-record append is removed from the builder, it turns red. Here the mutation target is
``format_track_record``: if an empty track record starts returning a non-empty
string, ``test_empty_history_returns_none`` turns red.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ai.track_record import (
    _LOAD_TRACK_RECORD_SQL,
    TrackRecord,
    build_track_record_context,
    format_track_record,
    load_track_record,
)

_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_CPV_BUILD = [{"scheme": "CPV", "code": "45000000", "label": "Строительство"}]
_CPV_DESIGN = [{"scheme": "CPV", "code": "71000000", "label": "Проектирование"}]


def _session_with_rows(rows: list) -> AsyncMock:
    result = MagicMock()
    result.all.return_value = rows
    session = AsyncMock()
    session.execute.return_value = result
    return session


# --------------------------------------------------------------------------- #
#  load_track_record: aggregation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_counts_and_top_cpv():
    """submitted/won/lost are counted; the top CPV is by frequency among the won ones."""
    rows = [
        SimpleNamespace(status="submitted", classifications=None),
        SimpleNamespace(status="submitted", classifications=None),
        SimpleNamespace(status="lost", classifications=_CPV_DESIGN),
        SimpleNamespace(status="won", classifications=_CPV_BUILD),
        SimpleNamespace(status="won", classifications=_CPV_BUILD),
        SimpleNamespace(status="won", classifications=_CPV_DESIGN),
    ]
    tr = await load_track_record(_session_with_rows(rows), _TENANT_ID)
    assert tr.submitted == 2
    assert tr.won == 3
    assert tr.lost == 1
    # 45000000 occurs twice among won, 71000000 — once → order by frequency.
    assert tr.top_cpv[0].startswith("45000000")
    assert any(c.startswith("71000000") for c in tr.top_cpv)


@pytest.mark.asyncio
async def test_load_empty_is_empty():
    """0 rows → an empty TrackRecord (is_empty)."""
    tr = await load_track_record(_session_with_rows([]), _TENANT_ID)
    assert tr.is_empty
    assert tr.submitted == tr.won == tr.lost == 0
    assert tr.top_cpv == []


@pytest.mark.asyncio
async def test_load_passes_tenant_id_param():
    """tenant_id goes into the query as a parameter (not relying blindly on the GUC)."""
    session = _session_with_rows([])
    await load_track_record(session, _TENANT_ID)
    _, kwargs = session.execute.call_args
    # execute(sql, params) — params as the second positional argument.
    params = session.execute.call_args[0][1]
    assert params == {"tenant_id": str(_TENANT_ID)}


# --------------------------------------------------------------------------- #
#  format_track_record
# --------------------------------------------------------------------------- #


def test_format_with_history():
    tr = TrackRecord(submitted=5, won=2, lost=1, top_cpv=["45000000 (Строительство)"])
    out = format_track_record(tr)
    assert out is not None
    assert "подано 5" in out
    assert "выиграно 2" in out
    assert "проиграно 1" in out
    assert "CPV 45000000 (Строительство)" in out


def test_format_without_cpv():
    """There are counters, but won without classifiers → no “Wins more often” block."""
    out = format_track_record(TrackRecord(submitted=3, won=0, lost=2, top_cpv=[]))
    assert out is not None
    assert "подано 3" in out
    assert "Чаще выигрывает" not in out


def test_empty_history_returns_none():
    """The mutation target: an empty track record → None (it will not get into the context)."""
    assert format_track_record(TrackRecord()) is None


# --------------------------------------------------------------------------- #
#  build_track_record_context: degradation to emptiness
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_build_context_happy():
    rows = [SimpleNamespace(status="won", classifications=_CPV_BUILD)]
    out = await build_track_record_context(_session_with_rows(rows), _TENANT_ID)
    assert out is not None and "выиграно 1" in out


@pytest.mark.asyncio
async def test_build_context_empty_returns_none():
    out = await build_track_record_context(_session_with_rows([]), _TENANT_ID)
    assert out is None


@pytest.mark.asyncio
async def test_build_context_degrades_on_error():
    """A query failure does not break the AI feature — the context is simply None."""
    session = AsyncMock()
    session.execute.side_effect = RuntimeError("boom")
    out = await build_track_record_context(session, _TENANT_ID)
    assert out is None


# --------------------------------------------------------------------------- #
#  A structural RLS invariant WITHOUT a DB (the fallback to the DB-gated test)
# --------------------------------------------------------------------------- #


def test_query_scoped_by_tenant_id():
    """The query must filter by tenant_id (RLS + defense-in-depth)."""
    sql = str(_LOAD_TRACK_RECORD_SQL)
    assert "tender_user_status" in sql
    assert "tus.tenant_id = CAST(:tenant_id AS uuid)" in sql
    # Only the productive funnel stages feed the track record.
    assert "'submitted'" in sql and "'won'" in sql and "'lost'" in sql
    # Not a single global read without the tenant predicate.
    assert "WHERE" in sql
