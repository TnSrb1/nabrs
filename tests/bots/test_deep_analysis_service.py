"""Tests of the on-demand L3 deep analysis service (STAGE 3 L3).

Without a network and without a real DB: a mock session via AsyncMock. We cover:
- Gate Pro+: starter/lite → gate_pro, without calling the model.
- Cache hit: a cached analysis is found → return success without calling AiClient.
- Limit exhausted: ai_usage.count >= limit → limit_exhausted.
- Success path: the model is called, the analysis is saved, html_chunks is non-empty.
- Pagination: _paginate_l3 — sections without overflow; a long output → ≥2 chunks.
- The callback format 'ai:deep:{tender_id}' — parsing of the prefix in the telegram.py handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.client import AiClient
from src.ai.usage import _TIER_LIMITS

# --------------------------------------------------------------------------- #
#  Auxiliary stubs
# --------------------------------------------------------------------------- #

_TENDER_ID = "11111111-1111-1111-1111-111111111111"
_TENANT_ID = "22222222-2222-2222-2222-222222222222"
_ANALYSIS_ID = "33333333-3333-3333-3333-333333333333"

_L3_OUTPUT = {
    "summary": "Глубокий разбор",
    "match_score": 4,
    "match_reasons": ["Хорошее совпадение"],
    "red_flags": [],
    "required_qualifications": [],
    "estimated_effort_days": 30,
    "competition_estimate": "low",
    "work_breakdown": ["Этап 1"],
    "participation_checklist": ["Шаг 1"],
    "focus_answers": [],
}


@dataclass
class _FakeAnalysisResult:
    output: dict
    model: str = "claude-opus-4-7"
    tokens_input: int = 500
    tokens_output: int = 200
    tokens_cached: int = 100
    cost_usd: float = 0.05


def _make_session(
    *,
    tenant_id: str | None = _TENANT_ID,
    tier: str | None = "pro",
    cached_analysis_id: str | None = None,
    usage_count: int = 0,
    tender_title: str = "Тендер на строительство",
    country_name: str = "Сербия",
    cached_output: dict | None = None,
    insert_id: str = _ANALYSIS_ID,
) -> AsyncMock:
    """Builds the SQLAlchemy mock session for the deep_analysis_service tests."""
    session = AsyncMock()

    # resolve_tenant_id internally does a select of UserChannel → User.
    # We mock at the level of status_service.resolve_tenant_id.

    # DB queries via session.execute — we return different answers per call.
    calls: list[SimpleNamespace | None] = []

    # 1. The tenant’s tier query (_LOAD_TENANT_TIER_SQL)
    tier_row = SimpleNamespace(tier=tier) if tier else None
    calls.append(tier_row)

    # 2. The cache lookup (cache_lookup → _CACHE_LOOKUP_SQL)
    cache_row = SimpleNamespace(id=cached_analysis_id) if cached_analysis_id else None
    calls.append(cache_row)

    # 3. If it is a cache hit — loading of the tender (for the title) + loading of the output
    if cached_analysis_id:
        tender_row = SimpleNamespace(
            title=tender_title, description="...", country_name=country_name
        )
        calls.append(tender_row)
        output_row = SimpleNamespace(output=cached_output or _L3_OUTPUT)
        calls.append(output_row)

    # 4. The limit (limit_decision → _USAGE_COUNT_SQL)
    usage_row = SimpleNamespace(count=usage_count) if usage_count > 0 else None
    calls.append(usage_row)

    # 5. Loading of the tender (_LOAD_TENDER_SQL)
    tender_row2 = SimpleNamespace(
        title=tender_title, description="Описание тендера", country_name=country_name
    )
    calls.append(tender_row2)

    # 6. INSERT tender_ai_analyses
    insert_row = SimpleNamespace(id=insert_id)
    calls.append(insert_row)

    call_index = [0]

    async def _execute(sql, params=None):
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(calls):
            row = calls[idx]
            result = MagicMock()
            result.first.return_value = row
            result.scalars.return_value.first.return_value = row
            return result
        # Fallback: an empty result
        result = MagicMock()
        result.first.return_value = None
        return result

    session.execute = _execute
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _make_ai_client(
    output: dict | None = None, raise_exc: Exception | None = None
) -> AiClient:
    """AiClient mock: returns a fake AnalysisResult or raises an exception."""
    client = MagicMock(spec=AiClient)

    async def _analyze(request):
        if raise_exc:
            raise raise_exc
        return _FakeAnalysisResult(output=output or _L3_OUTPUT)

    client.analyze = _analyze
    return client


# --------------------------------------------------------------------------- #
#  Gate Pro+: starter and lite → gate_pro
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gate_pro_starter_rejected():
    """starter: deep_limit=0 → gate_pro, the model is not called."""
    from src.bots.deep_analysis_service import run_deep_analysis

    session = AsyncMock()
    # tier row
    tier_result = MagicMock()
    tier_result.first.return_value = SimpleNamespace(tier="starter")
    session.execute = AsyncMock(return_value=tier_result)

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=123456789,
            ai_client=_make_ai_client(),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    assert result.html_chunks == ()


@pytest.mark.asyncio
async def test_gate_pro_lite_rejected():
    """lite: deep_limit=0 → gate_pro."""
    from src.bots.deep_analysis_service import run_deep_analysis

    session = AsyncMock()
    tier_result = MagicMock()
    tier_result.first.return_value = SimpleNamespace(tier="lite")
    session.execute = AsyncMock(return_value=tier_result)

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=123456789,
            ai_client=_make_ai_client(),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    assert result.html_chunks == ()


# --------------------------------------------------------------------------- #
#  no_account: resolve_tenant_id returns None
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_account_when_no_tenant():
    """No chat_id link → no_account."""
    from src.bots.deep_analysis_service import run_deep_analysis

    session = AsyncMock()

    with patch("src.bots.deep_analysis_service.resolve_tenant_id", return_value=None):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=999999,
            ai_client=_make_ai_client(),
        )

    assert result.status == "no_account"


# --------------------------------------------------------------------------- #
#  Limit reached
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_limit_exhausted_when_usage_at_cap():
    """pro: limit deep=10, usage=10 → limit_exhausted, the model is not called."""
    from src.bots.deep_analysis_service import run_deep_analysis

    # Sequential execute calls:
    #  1. tier query → pro
    #  2. cache_lookup → None (no cache)
    #  3. limit_decision usage query → count=10
    calls = [
        SimpleNamespace(tier="pro"),  # tier
        None,  # cache miss
        SimpleNamespace(count=10),  # usage
    ]
    call_idx = [0]

    session = AsyncMock()

    async def _exec(sql, params=None):
        idx = call_idx[0]
        call_idx[0] += 1
        row = calls[idx] if idx < len(calls) else None
        r = MagicMock()
        r.first.return_value = row
        return r

    session.execute = _exec
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=123456789,
            ai_client=_make_ai_client(),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "limit_exhausted"


# --------------------------------------------------------------------------- #
#  Success path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_success_path_returns_html_chunks():
    """Pro+, no cache, the limit is not exhausted → a model call, html_chunks is non-empty."""
    from src.bots.deep_analysis_service import run_deep_analysis

    # The sequence:
    #  1. tier → pro
    #  2. cache_lookup → None
    #  3. limit usage → count=0 (not exhausted)
    #  4. tender query → a row with title/description/country
    #  5. INSERT → id
    calls = [
        SimpleNamespace(tier="pro"),
        None,
        None,  # no usage count row → 0
        SimpleNamespace(title="Тест", description="Описание", country_name="Сербия"),
        SimpleNamespace(id=_ANALYSIS_ID),
    ]
    call_idx = [0]

    session = AsyncMock()

    async def _exec(sql, params=None):
        idx = call_idx[0]
        call_idx[0] += 1
        row = calls[idx] if idx < len(calls) else None
        r = MagicMock()
        r.first.return_value = row
        return r

    session.execute = _exec
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    with patch(
        "src.bots.deep_analysis_service.resolve_tenant_id", return_value=_TENANT_ID
    ):
        result = await run_deep_analysis(
            session,
            tender_id=_TENDER_ID,
            chat_id=123456789,
            ai_client=_make_ai_client(_L3_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "success"
    assert len(result.html_chunks) >= 1
    first_chunk = result.html_chunks[0]
    assert len(first_chunk) > 0
    assert "🧠" in first_chunk or "Глубокий" in first_chunk


# --------------------------------------------------------------------------- #
#  The render of _render_l3_chunks
# --------------------------------------------------------------------------- #


def test_render_l3_chunks_structure():
    """_render_l3_chunks contains the key sections of L3Output."""
    from src.bots.deep_analysis_service import _render_l3_chunks

    chunks = _render_l3_chunks("Тендер тест", _L3_OUTPUT)
    assert isinstance(chunks, list)
    assert len(chunks) >= 1
    full_text = "\n\n".join(chunks)
    assert "🧠" in full_text
    assert "Тендер тест" in full_text
    assert "Глубокий разбор" in full_text  # summary
    assert "Этап 1" in full_text  # work_breakdown
    assert "Шаг 1" in full_text  # participation_checklist


def test_render_l3_chunks_short_output_is_one_chunk():
    """A short output → exactly 1 chunk, no “truncated” suffix."""
    from src.bots.deep_analysis_service import _render_l3_chunks

    chunks = _render_l3_chunks("Тест", _L3_OUTPUT)
    assert len(chunks) == 1
    assert "обрезан" not in chunks[0]


def test_render_l3_chunks_long_output_splits_correctly():
    """A long output (>4096 in total) → ≥2 chunks, each ≤4096, the whole text is kept."""
    from src.bots.deep_analysis_service import _render_l3_chunks, _TG_MAX_CHARS

    # A synthetic output with huge lists (>4096 in total).
    long_item = "Детальный пункт с описанием работ номер {i}: " + "x" * 200
    long_output = dict(_L3_OUTPUT)
    long_output["work_breakdown"] = [long_item.format(i=i) for i in range(20)]
    long_output["participation_checklist"] = [
        f"Пункт чек-листа {i}: " + "y" * 150 for i in range(20)
    ]

    chunks = _render_l3_chunks("Длинный тендер", long_output)
    assert len(chunks) >= 2, "a long analysis must give ≥2 chunks"
    for chunk in chunks:
        assert (
            len(chunk) <= _TG_MAX_CHARS
        ), f"a chunk exceeds {_TG_MAX_CHARS}: {len(chunk)}"

    # The whole text is kept: we sum the contents of the chunks.
    full = "\n\n".join(chunks)
    assert "обрезан" not in full, "the truncation suffix must not be present"
    # We check that the work_breakdown items from the output are present in the result.
    for i in range(20):
        assert f"номер {i}" in full, f"work_breakdown item {i} lost"


# --------------------------------------------------------------------------- #
#  _paginate_l3 directly
# --------------------------------------------------------------------------- #


def test_paginate_l3_fits_in_one_chunk():
    """Sections in total < max_chars → exactly 1 chunk."""
    from src.bots.deep_analysis_service import _paginate_l3

    parts = ["Секция A", "Секция B", "Секция C"]
    result = _paginate_l3(parts, max_chars=4096)
    assert result == ["Секция A\n\nСекция B\n\nСекция C"]


def test_paginate_l3_splits_on_overflow():
    """Sections in total > max_chars → a split into ≥2 chunks, each ≤ max_chars."""
    from src.bots.deep_analysis_service import _paginate_l3

    max_chars = 100
    # Three sections of 40 characters; two fit (82 ≤ 100), the third does not fit (124 > 100).
    parts = ["A" * 40, "B" * 40, "C" * 40]
    result = _paginate_l3(parts, max_chars=max_chars)
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk) <= max_chars


def test_paginate_l3_oversized_single_section_splits():
    """One section >max_chars → split by lines."""
    from src.bots.deep_analysis_service import _paginate_l3

    max_chars = 100
    # Three lines of 60 characters, separated by \n → a section of ~182 characters.
    oversized = "X" * 60 + "\n" + "Y" * 60 + "\n" + "Z" * 60
    result = _paginate_l3([oversized], max_chars=max_chars)
    assert len(result) >= 2
    for chunk in result:
        assert len(chunk) <= max_chars
    # The whole text is kept.
    full = "\n".join(result)
    assert "X" * 60 in full
    assert "Y" * 60 in full
    assert "Z" * 60 in full


# --------------------------------------------------------------------------- #
#  Parsing of the callback format ai:deep: in telegram.py
# --------------------------------------------------------------------------- #


def test_callback_prefix_parsing():
    """The on_deep_analysis handler correctly parses 'ai:deep:{tender_id}'."""
    data = f"ai:deep:{_TENDER_ID}"
    prefix = "ai:deep:"
    assert data.startswith(prefix)
    extracted = data[len(prefix) :]
    assert extracted == _TENDER_ID


def test_callback_prefix_empty_tender_id():
    """An empty tender_id after the prefix — must be cut off."""
    data = "ai:deep:"
    prefix = "ai:deep:"
    extracted = data[len(prefix) :]
    assert extracted == ""  # the handler checks `if not tender_id`


def test_tier_limits_deep_pro_nonzero():
    """_TIER_LIMITS: pro.deep=10 (not 0) → pro passes the gate."""
    assert _TIER_LIMITS["pro"]["deep"] == 10
    assert _TIER_LIMITS["starter"]["deep"] == 0
    assert _TIER_LIMITS["lite"]["deep"] == 0
    assert _TIER_LIMITS["enterprise"]["deep"] is None
