"""Tests of the on-demand AI draft of the application (VG-10).

Without a network and without a real DB: a mock session via AsyncMock. We cover:
- Gate Pro+: starter/lite → gate_pro, the model is not called.
- Frozen: access_until expired → frozen, the model is not called.
- no_account: no link → no_account.
- Success: the model is called, the JSON is parsed, the HTML is non-empty.
- Cache: a repeated call per (tender_id, profile_id) does not hit the model.
- _render_draft: sections, the mapping of the criteria, the disclaimer, the summary.
- The choice of the model by plan: pro → balanced (sonnet); business/enterprise → premium (opus).
- Limits: pro draft=5 → the 6th = limit_exhausted; business draft=30.
- Callback ai:drf:{tender_id} ≤ 64 bytes.
- schemas: validate_output("draft", ...) normalizes/is tolerant of extra fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.ai.client import AiClient, AnalysisRequest
from src.ai.usage import _TIER_LIMITS

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PROFILE_ID_A = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_ANALYSIS_ID = "eeee5555-eeee-eeee-eeee-eeeeeeeeeeee"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)

_DRAFT_OUTPUT = {
    "sections": [
        {
            "heading": "Uvod",
            "body": "Kompanija XYZ podnosi ponudu za izgradnju puta.",
        },
        {
            "heading": "Tehnička ponuda",
            "body": "Predlažemo upotrebu materijala klase A i moderne opreme.",
        },
        {
            "heading": "Kvalifikacije",
            "body": "Posedujemo licencu kategorije C za građevinske radove.",
        },
    ],
    "criteria_mapping": [
        {
            "criterion": "Tehnička sposobnost",
            "response": "Raspolažemo opremom i stručnim kadrom za realizaciju projekta.",
        },
        {
            "criterion": "Finansijska stabilnost",
            "response": "Prihodujemo 5M EUR godišnje, bez dugova.",
        },
    ],
    "disclaimer": "Это черновик, сгенерированный AI. Требует проверки перед отправкой.",
    "summary": "Черновик готов. Необходимо заполнить плейсхолдеры [POPUNITI].",
}


@dataclass
class _FakeAnalysisResult:
    output: dict
    model: str = "claude-sonnet-4-6"
    tokens_input: int = 800
    tokens_output: int = 400
    tokens_cached: int = 0
    cost_usd: float = 0.010


def _make_ai_client(
    output: dict | None = None,
    raise_exc: Exception | None = None,
    capture_requests: list | None = None,
) -> AiClient:
    client = MagicMock(spec=AiClient)

    async def _analyze(request: AnalysisRequest):
        if capture_requests is not None:
            capture_requests.append(request)
        if raise_exc:
            raise raise_exc
        return _FakeAnalysisResult(output=output or _DRAFT_OUTPUT)

    client.analyze = _analyze
    return client


def _session_returning(row) -> AsyncMock:
    result = MagicMock()
    result.first.return_value = row
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _make_session_seq(calls: list) -> AsyncMock:
    """Mock session with sequential responses to execute."""
    call_idx = [0]
    session = AsyncMock()

    async def _exec(sql, params=None):
        # The track record (tender_user_status) — an auxiliary query, reads .all();
        # does not take a position in the sequence, so as not to shift the calls.
        if "tender_user_status" in str(sql):
            r = MagicMock()
            r.all.return_value = []
            r.first.return_value = None
            return r
        idx = call_idx[0]
        call_idx[0] += 1
        row = calls[idx] if idx < len(calls) else None
        r = MagicMock()
        r.first.return_value = row
        return r

    session.execute = _exec
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


_PROFILE_ROW = SimpleNamespace(
    id=_PROFILE_ID_A,
    classifications=[{"scheme": "CPV", "code": "45000000", "label": "Грађевина"}],
    regions=["Белград"],
    keywords_include={"keywords": ["грађевина"]},
    ai_extra_context=None,
    target_language="ru",
)


# --------------------------------------------------------------------------- #
#  Gate Pro+: starter → gate_pro
# --------------------------------------------------------------------------- #


async def test_gate_pro_starter_rejected():
    """starter: draft=0 → gate_pro, the model is not called."""
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="starter", access_until=_FUTURE),
        _PROFILE_ROW,
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    assert result.html == ""
    client.analyze.assert_not_called()


async def test_gate_pro_lite_rejected():
    """lite: draft=0 → gate_pro."""
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="lite", access_until=None),
        _PROFILE_ROW,
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "gate_pro"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  Frozen: access_until expired → frozen
# --------------------------------------------------------------------------- #


async def test_frozen_returns_frozen_without_calling_model():
    """access_until expired → status='frozen', the model is NOT called."""
    from src.bots.draft_service import run_draft_application

    session = _session_returning(SimpleNamespace(tier="pro", access_until=_PAST))
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
        )

    assert result.status == "frozen"
    assert result.html == ""
    client.analyze.assert_not_called()
    assert session.execute.await_count == 1


# --------------------------------------------------------------------------- #
#  no_account: resolve_tenant_id returns None
# --------------------------------------------------------------------------- #


async def test_no_account_when_no_tenant():
    """No chat_id link → no_account."""
    from src.bots.draft_service import run_draft_application

    session = AsyncMock()
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch("src.bots.draft_service.resolve_tenant_id", return_value=None):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=999999,
            ai_client=client,
        )

    assert result.status == "no_account"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  no_profile: no profile → no_profile
# --------------------------------------------------------------------------- #


async def test_no_profile_returns_no_profile():
    """The tenant has no profile → no_profile."""
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        None,  # latest profile → None
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "no_profile"
    client.analyze.assert_not_called()


# --------------------------------------------------------------------------- #
#  Success: the model is called, the HTML contains the sections and the mapping
# --------------------------------------------------------------------------- #


async def test_ok_valid_json_parsed_and_rendered():
    """Pro+, no cache → a model call, the HTML is non-empty, contains the sections."""
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),  # 0 tier
        _PROFILE_ROW,  # 1 latest profile
        None,  # 2 cache miss
        SimpleNamespace(count=0),  # 3 limit_decision usage count (pro.draft=5)
        SimpleNamespace(  # 4 tender
            title="Тендер на строительство дороги",
            description="Потребан извођач радова са лиценцом категорије C.",
            country_name="Сербия",
        ),
        SimpleNamespace(id=_ANALYSIS_ID),  # 5 INSERT
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_DRAFT_OUTPUT),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "success"
    assert len(result.html) > 0
    assert "📝" in result.html
    assert "Тендер на строительство дороги" in result.html
    assert "Uvod" in result.html
    assert "Tehnička ponuda" in result.html


# --------------------------------------------------------------------------- #
#  Cache: a repeated call does not hit the model
# --------------------------------------------------------------------------- #


async def test_cache_hit_no_model_call():
    """A cache hit → the model is NOT called, the HTML is returned from the cache."""
    from src.bots.draft_service import run_draft_application

    cached_output = {
        "sections": [{"heading": "Uvod", "body": "Kešovano"}],
        "criteria_mapping": [],
        "disclaimer": None,
        "summary": "Кешировано",
    }

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),  # 0 tier
        _PROFILE_ROW,  # 1 latest profile
        SimpleNamespace(id=_ANALYSIS_ID),  # 2 cache HIT
        SimpleNamespace(
            title="Тендер из кеша", description="", country_name="Сербия"
        ),  # 3 tender
        SimpleNamespace(output=cached_output),  # 4 _load_cached_output
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "success"
    client.analyze.assert_not_called()
    assert "Кешировано" in result.html


# --------------------------------------------------------------------------- #
#  The choice of the model by plan (VG-10)
# --------------------------------------------------------------------------- #


async def test_pro_uses_balanced_model():
    """pro → model_tier='balanced' is passed into AnalysisRequest."""
    from src.bots.draft_service import run_draft_application

    captured: list[AnalysisRequest] = []

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        _PROFILE_ROW,
        None,  # cache miss
        SimpleNamespace(count=0),  # limit_decision usage count (pro.draft=5)
        SimpleNamespace(title="T", description="D", country_name="Сербия"),
        SimpleNamespace(id=_ANALYSIS_ID),
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_DRAFT_OUTPUT, capture_requests=captured),
            tier_limits=_TIER_LIMITS,
        )

    assert len(captured) == 1
    assert captured[0].model_tier == "balanced"
    assert captured[0].level == "draft"


async def test_business_uses_premium_model():
    """business → model_tier='premium' is passed into AnalysisRequest."""
    from src.bots.draft_service import run_draft_application

    captured: list[AnalysisRequest] = []

    calls = [
        SimpleNamespace(tier="business", access_until=_FUTURE),
        _PROFILE_ROW,
        None,  # cache miss
        SimpleNamespace(count=0),  # limit_decision usage count (business.draft=30)
        SimpleNamespace(title="T", description="D", country_name="Сербия"),
        SimpleNamespace(id=_ANALYSIS_ID),
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_DRAFT_OUTPUT, capture_requests=captured),
            tier_limits=_TIER_LIMITS,
        )

    assert len(captured) == 1
    assert captured[0].model_tier == "premium"


async def test_enterprise_uses_premium_model():
    """enterprise → model_tier='premium' is passed into AnalysisRequest."""
    from src.bots.draft_service import run_draft_application

    captured: list[AnalysisRequest] = []

    calls = [
        SimpleNamespace(tier="enterprise", access_until=_FUTURE),
        _PROFILE_ROW,
        None,  # cache miss
        SimpleNamespace(title="T", description="D", country_name="Сербия"),
        SimpleNamespace(id=_ANALYSIS_ID),
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(_DRAFT_OUTPUT, capture_requests=captured),
            tier_limits=_TIER_LIMITS,
        )

    assert len(captured) == 1
    assert captured[0].model_tier == "premium"


# --------------------------------------------------------------------------- #
#  Limits: pro draft=5, business draft=30
# --------------------------------------------------------------------------- #


async def test_limit_exhausted_pro_draft():
    """pro draft=5 → limit_decision returns 'exhausted' → limit_exhausted."""
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        _PROFILE_ROW,
        None,  # cache miss
    ]
    session = _make_session_seq(calls)
    client = MagicMock(spec=AiClient)
    client.analyze = AsyncMock()

    with (
        patch("src.bots.draft_service.resolve_tenant_id", return_value=_TENANT_ID),
        patch("src.bots.draft_service.limit_decision", return_value="exhausted"),
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=client,
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "limit_exhausted"
    client.analyze.assert_not_called()


def test_tier_limits_draft_values():
    """_TIER_LIMITS contains the correct values for the level 'draft'."""
    assert _TIER_LIMITS["lite"]["draft"] == 0
    assert _TIER_LIMITS["starter"]["draft"] == 0
    assert _TIER_LIMITS["pro"]["draft"] == 5
    assert _TIER_LIMITS["business"]["draft"] == 30
    assert _TIER_LIMITS["enterprise"]["draft"] is None


# --------------------------------------------------------------------------- #
#  model_busy / model_error
# --------------------------------------------------------------------------- #


async def test_model_busy_returns_model_busy():
    """AiRetryableError → model_busy."""
    from src.ai.client import AiRetryableError
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        _PROFILE_ROW,
        None,  # cache miss
        SimpleNamespace(count=0),  # limit_decision usage count (pro.draft=5)
        SimpleNamespace(title="T", description="D", country_name="С"),
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(raise_exc=AiRetryableError("busy")),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "model_busy"


async def test_model_error_returns_model_error():
    """An arbitrary exception → model_error."""
    from src.bots.draft_service import run_draft_application

    calls = [
        SimpleNamespace(tier="pro", access_until=_FUTURE),
        _PROFILE_ROW,
        None,  # cache miss
        SimpleNamespace(count=0),  # limit_decision usage count (pro.draft=5)
        SimpleNamespace(title="T", description="D", country_name="С"),
    ]
    session = _make_session_seq(calls)

    with patch(
        "src.bots.draft_service.resolve_tenant_id",
        return_value=_TENANT_ID,
    ):
        result = await run_draft_application(
            session,
            tender_id=_TENDER_ID,
            chat_id=_CHAT_ID,
            ai_client=_make_ai_client(raise_exc=ValueError("bad")),
            tier_limits=_TIER_LIMITS,
        )

    assert result.status == "model_error"


# --------------------------------------------------------------------------- #
#  _render_draft
# --------------------------------------------------------------------------- #


def test_render_draft_with_sections():
    """Draft sections → HTML with headings and the body."""
    from src.bots.draft_service import _render_draft

    result = _render_draft("Тестовый тендер", _DRAFT_OUTPUT)
    assert "📝" in result
    assert "Тестовый тендер" in result
    assert "Uvod" in result
    assert "Tehnička ponuda" in result


def test_render_draft_with_criteria_mapping():
    """The mapping of the criteria → the section 🎯."""
    from src.bots.draft_service import _render_draft

    result = _render_draft("Тест", _DRAFT_OUTPUT)
    assert "🎯" in result
    assert "Tehnička sposobnost" in result
    assert "Finansijska stabilnost" in result


def test_render_draft_with_disclaimer():
    """Disclaimer → the section ⚠️."""
    from src.bots.draft_service import _render_draft

    result = _render_draft("Тест", _DRAFT_OUTPUT)
    assert "⚠️" in result
    assert "черновик" in result.lower()


def test_render_draft_with_summary():
    """Summary → the section 💡."""
    from src.bots.draft_service import _render_draft

    result = _render_draft("Тест", _DRAFT_OUTPUT)
    assert "💡" in result
    assert "POPUNITI" in result


def test_render_draft_empty_sections():
    """Empty sections are not output, the base heading is present."""
    from src.bots.draft_service import _render_draft

    empty_output = {
        "sections": [],
        "criteria_mapping": [],
        "disclaimer": None,
        "summary": None,
    }
    result = _render_draft("Пустой", empty_output)
    assert "📝" in result
    assert "🎯" not in result
    # E-F8: the static disclaimer — always; the only ⚠️ is it (there is no model one).
    assert result.count("⚠️") == 1


def test_render_draft_trim_long():
    """A long output is cut to ≤4096 characters with «…»."""
    from src.bots.draft_service import _render_draft

    big_body = "X" * 2000
    output = {
        "sections": [
            {"heading": "A", "body": big_body},
            {"heading": "B", "body": big_body},
            {"heading": "C", "body": big_body},
        ],
        "criteria_mapping": [],
        "disclaimer": None,
        "summary": None,
    }
    result = _render_draft("Тест", output)
    assert len(result) <= 4096
    assert result.endswith("…")


# --------------------------------------------------------------------------- #
#  DraftOutput schema (Pydantic)
# --------------------------------------------------------------------------- #


def test_draft_output_valid_json():
    """DraftOutput parses a correct JSON."""
    from src.ai.schemas import DraftOutput

    parsed = DraftOutput.model_validate(_DRAFT_OUTPUT)
    assert len(parsed.sections) == 3
    assert parsed.sections[0].heading == "Uvod"
    assert len(parsed.criteria_mapping) == 2
    assert parsed.disclaimer is not None
    assert parsed.summary is not None


def test_draft_output_ignores_extra_fields():
    """DraftOutput is tolerant of extra fields (extra='ignore')."""
    from src.ai.schemas import DraftOutput

    data = dict(_DRAFT_OUTPUT)
    data["unexpected_field"] = "should be ignored"
    parsed = DraftOutput.model_validate(data)
    assert len(parsed.sections) == 3


def test_validate_output_draft_level():
    """validate_output('draft', ...) normalizes DraftOutput."""
    from src.ai.schemas import validate_output

    result = validate_output("draft", _DRAFT_OUTPUT)
    assert isinstance(result, dict)
    assert "sections" in result
    assert "criteria_mapping" in result


# --------------------------------------------------------------------------- #
#  Callback ai:drf:{tender_id} ≤ 64 bytes
# --------------------------------------------------------------------------- #


def test_callback_ai_drf_within_64_bytes():
    """ai:drf:{tender_id} ≤ 64 bytes (the TG limit of callback_data)."""
    cb = f"ai:drf:{_TENDER_ID}"
    assert len(cb.encode()) <= 64, f"callback '{cb}' = {len(cb.encode())} bytes > 64"


def test_all_ai_callbacks_within_64_bytes():
    """All AI callbacks ≤ 64 bytes."""
    for prefix in ("ai:deep:", "ai:bid:", "ai:chk:", "ai:drf:"):
        cb = f"{prefix}{_TENDER_ID}"
        assert (
            len(cb.encode()) <= 64
        ), f"callback '{cb}' = {len(cb.encode())} bytes > 64"
