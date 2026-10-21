"""The accounting of the spend in ``ai_usage``: ``record_usage`` + the common UPSERT (VG-SPEND).

``record_usage`` appeared for the sake of translation: it is paid, but it does not create a
``tender_ai_analyses`` row, therefore ``finalize_with_insert`` does not fit it, and without a record in
``ai_usage`` the level is invisible both to the limit and to the report.

Separately it is pinned that BOTH callers of the common ``_UPSERT_USAGE_SQL`` pass the
FULL set of bind parameters: the counter increment became the parameter ``:calls``, and a
forgotten parameter in asyncpg is a failure already on production (the log will show «2
values instead of 3»), not on the import.

Without a DB (mock session, ADR-013).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.ai.usage import _UPSERT_USAGE_SQL, finalize_with_insert, record_usage

_TENANT = "11111111-1111-1111-1111-111111111111"
_TENDER = "22222222-2222-2222-2222-222222222222"


class _RecSession:
    """Mock session: writes (sql, params) into ``calls``, counts the commits."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.committed = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return SimpleNamespace(first=lambda: SimpleNamespace(id="analysis-1"))

    async def commit(self) -> None:
        self.committed += 1

    async def rollback(self) -> None:  # pragma: no cover — the conflict is not modelled
        pass


def _bind_names(sql: str) -> set[str]:
    """The names of ``:param`` in the query text (``CAST(:p AS type)`` is a parameter too)."""
    return set(re.findall(r":(\w+)", sql))


def _upsert_call(session: _RecSession) -> tuple[str, dict]:
    sql, params = next(c for c in session.calls if "INSERT INTO ai_usage" in c[0])
    return sql, dict(params or {})


@pytest.mark.asyncio
async def test_record_usage_writes_level_counter_and_commits():
    s = _RecSession()
    await record_usage(
        s, tenant_id=_TENANT, level="translate", calls=1, tokens=900, cost=0.0025
    )

    sql, params = _upsert_call(s)
    assert params["tenant_id"] == _TENANT
    assert params["level"] == "translate"
    assert params["calls"] == 1
    assert params["tokens"] == 900
    assert params["cost"] == 0.0025
    assert re.fullmatch(
        r"\d{4}-\d{2}", params["period"]
    )  # the accounting period YYYY-MM
    # The counter grows BY the passed value, not by the constant 1.
    assert "COALESCE(ai_usage.count, 0) + EXCLUDED.count" in sql
    assert (
        s.committed == 1
    ), "without a commit the spend will not survive the closing of the session"


def test_record_usage_accumulates_cost_and_tokens_not_overwrites():
    """The monthly total is the sum, not the last value."""
    sql = str(_UPSERT_USAGE_SQL)
    assert "COALESCE(ai_usage.tokens_total, 0) + EXCLUDED.tokens_total" in sql
    assert "COALESCE(ai_usage.cost_usd_total, 0) + EXCLUDED.cost_usd_total" in sql


@pytest.mark.asyncio
async def test_record_usage_binds_every_parameter_of_the_shared_upsert():
    """‼️ asyncpg: a missing bind parameter fails at runtime, not at the import."""
    s = _RecSession()
    await record_usage(s, tenant_id=_TENANT, level="translate")

    sql, params = _upsert_call(s)
    assert _bind_names(sql) <= set(params)


@pytest.mark.asyncio
async def test_finalize_with_insert_still_charges_exactly_one_call():
    """The analysis writes off ONE unit — the parametrisation of the counter did not change it."""
    s = _RecSession()
    result = SimpleNamespace(
        model="claude-sonnet-4-6",
        output={"verdict": "go"},
        tokens_input=100,
        tokens_output=50,
        tokens_cached=0,
        cost_usd=0.02,
    )

    analysis_id = await finalize_with_insert(
        s,
        tender_id=_TENDER,
        profile_id=None,
        tenant_id=_TENANT,
        level="bid",
        result=result,
    )

    assert analysis_id == "analysis-1"
    sql, params = _upsert_call(s)
    assert params["calls"] == 1
    assert params["tokens"] == 150
    assert _bind_names(sql) <= set(params)
