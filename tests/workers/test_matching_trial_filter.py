"""Tests of the filter of the test access in the STAGE A matching (STAGE T).

The filter ``access_until`` lives in the SQL (executed by Postgres), therefore by the contract of the project (ADR-013, without a live DB) we check: (1) the SQL contains the guard condition ``access_until``; (2) ``stage_a_candidates`` still builds the parameters and maps the rows (regression — the filter did not break the assembly of the candidates). access_until is a literal SQL condition (not a bind parameter), it is not in ``params``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.workers.matching import STAGE_A_SQL, Candidate, TenderView, stage_a_candidates


def test_stage_a_sql_has_access_until_guard():
    sql = str(STAGE_A_SQL)
    # the guard condition: NULL = permanent, otherwise the term must not be in the past
    assert "access_until IS NULL" in sql
    assert "access_until > now()" in sql


@pytest.mark.asyncio
async def test_access_until_is_not_a_bind_param():
    # access_until — a literal condition, not a parameter (it must not be in params)
    tender = TenderView(id="t", country_code="RS", source_key="jnportal")
    captured = {}

    class _Result:
        def __iter__(self):
            return iter(())

    async def _execute(statement, params=None):
        captured.update(params or {})
        return _Result()

    session = AsyncMock()
    session.execute = _execute

    await stage_a_candidates(session, tender)
    assert "access_until" not in captured


@pytest.mark.asyncio
async def test_stage_a_candidates_maps_rows():
    tender = TenderView(id="t", country_code="RS", source_key="jnportal")

    row = type(
        "Row",
        (),
        {
            "id": "p1",
            "tenant_id": "ten1",
            "keywords_include": [{"all_of": ["строительство"]}],
            "keywords_exclude": [],
            "ai_analysis_level": "none",
            "delivery_channels": ["telegram"],
            "keyword_dict": False,
        },
    )()

    class _Result:
        def __iter__(self):
            return iter([row])

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_Result())
    candidates = await stage_a_candidates(session, tender)
    assert candidates == [
        Candidate(
            profile_id="p1",
            tenant_id="ten1",
            keywords_include=[{"all_of": ["строительство"]}],
            keywords_exclude=[],
            ai_analysis_level="none",
            delivery_channels=["telegram"],
            keyword_dict=False,
        )
    ]
