"""Tests of tender_ai_service (ADR-013: mock session without a real DB).

We check the read-only reading of the AI artifacts:
- the mapping of all levels (bid/checklist/draft/standard) into the output JSON;
- set_tenant is called FIRST (RLS invariant);
- someone else’s profile (0 rows under RLS) → None (the route → 404);
- a missing tender → None;
- deep_analysis prefers deep → standard → quick;
- the output string (JSON text) is coerced into a dict;
- an empty set → has_any False.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet.tender_ai_service import get_tender_analyses

_TID = "11111111-1111-1111-1111-111111111111"
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"


class _Result:
    def __init__(self, *, rows=None):
        self._rows = list(rows) if rows is not None else []

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _Session:
    """The AsyncSession mock: set_config (RLS) does NOT touch the results queue, but
    the fact of the call is recorded in ``executed`` to check the set_tenant invariant.
    """

    def __init__(self, results):
        self._results = list(results)
        self.executed: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append(sql)
        if "set_config" in sql or "app.current_tenant_id" in sql:
            return _Result()
        return self._results.pop(0) if self._results else _Result()


def _profile_row(name="ИТ набавке"):
    return _Result(rows=[SimpleNamespace(name=name)])


def _tender_row(**kw):
    base = dict(
        title="Набавка рачунара",
        description="Предмет јавне набавке је испорука рачунарске опреме.",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
    )
    base.update(kw)
    return _Result(rows=[SimpleNamespace(**base)])


def _analyses(rows):
    return _Result(rows=[SimpleNamespace(level=lvl, output=out) for lvl, out in rows])


# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_maps_all_levels_and_meta():
    s = _Session(
        [
            _profile_row(),
            _tender_row(),
            _analyses(
                [
                    ("bid", {"verdict": "go", "score": 80, "pros": ["a"], "cons": []}),
                    ("checklist", {"documents": [{"title": "APR"}]}),
                    ("draft", {"sections": [{"heading": "Uvod", "body": "tekst"}]}),
                    ("standard", {"summary": "рез", "match_score": 4}),
                ]
            ),
        ]
    )
    data = await get_tender_analyses(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE
    )
    assert data is not None
    assert data.title == "Набавка рачунара"
    # The original description (sr) — the source for the translation of the card (VG-26).
    assert data.description == "Предмет јавне набавке је испорука рачунарске опреме."
    assert data.customer_name == "Општина Нови Сад"
    assert data.source_url == "https://portal.rs/t/1"
    assert data.profile_name == "ИТ набавке"
    assert data.bid["verdict"] == "go"
    assert data.checklist["documents"][0]["title"] == "APR"
    assert data.draft["sections"][0]["heading"] == "Uvod"
    assert data.deep_analysis["summary"] == "рез"
    assert data.has_any is True


@pytest.mark.asyncio
async def test_set_tenant_called_first():
    s = _Session([_profile_row(), _tender_row(), _analyses([])])
    await get_tender_analyses(s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE)
    # The first execute — the RLS set_config (set_tenant is the FIRST action of the transaction).
    assert "set_config" in s.executed[0] or "app.current_tenant_id" in s.executed[0]


@pytest.mark.asyncio
async def test_foreign_profile_returns_none():
    # A profile of another tenant → 0 rows (RLS + an explicit WHERE tenant_id) → None.
    s = _Session([_Result(rows=[])])
    data = await get_tender_analyses(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE
    )
    assert data is None


@pytest.mark.asyncio
async def test_missing_tender_returns_none():
    s = _Session([_profile_row(), _Result(rows=[])])
    data = await get_tender_analyses(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE
    )
    assert data is None


@pytest.mark.asyncio
async def test_deep_analysis_prefers_deep_over_standard():
    s = _Session(
        [
            _profile_row(),
            _tender_row(),
            _analyses(
                [
                    ("standard", {"summary": "L2"}),
                    ("deep", {"summary": "L3"}),
                ]
            ),
        ]
    )
    data = await get_tender_analyses(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE
    )
    assert data.deep_analysis["summary"] == "L3"


@pytest.mark.asyncio
async def test_output_coerced_from_json_string():
    s = _Session(
        [
            _profile_row(),
            _tender_row(),
            _analyses([("bid", '{"verdict": "no_go", "score": 10}')]),
        ]
    )
    data = await get_tender_analyses(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE
    )
    assert data.bid == {"verdict": "no_go", "score": 10}


@pytest.mark.asyncio
async def test_empty_analyses_has_any_false():
    s = _Session([_profile_row(), _tender_row(), _analyses([])])
    data = await get_tender_analyses(
        s, tid=_TID, tender_id=_TENDER, profile_id=_PROFILE
    )
    assert data is not None
    assert data.has_any is False
    assert data.bid is None
    assert data.deep_analysis is None
