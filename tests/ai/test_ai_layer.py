"""Unit tests of the AI layer ``src/ai`` (Sprint-18 + STAGE 3 L3).

Without a network to Anthropic and without the installed ``anthropic`` package: the SDK is injected
as a fake. We cover:

- **prompts** — the parametrisation of ``{country_name}`` / ``{target_language}`` without
  a hard-code and without “leaked” placeholders; the user part of L2 includes
  the header, the description, the context and the subscriber's questions (§14.2, §14.3, ADR-033).
- **schemas** — the validation of the L1/L2/L3 output; invalid JSON → ``ValidationError``;
  'deep' is now registered (STAGE 3) → KeyError is no longer expected.
- **client** — tier→slug from ``infra/config.yaml`` (ADR-029), prompt caching
  (§14.6, ``cache_control: ephemeral``), failover/retry 429/5xx (§14.7),
  the return of tokens and the approximate cost.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai import (
    AiError,
    AiResponseError,
    AiRetryableError,
    AnalysisRequest,
    load_model_config,
)
from src.ai import client as client_mod
from src.ai import prompts, schemas
from src.ai.client import AiClient

# Config tier→slug for the injection (without reading the file).
_CONFIG = {
    "cheap": "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "premium": "claude-opus-4-7",
}


# --------------------------------------------------------------------------- #
#  Fake Anthropic SDK
# --------------------------------------------------------------------------- #


def _resp(json_text: str, ti: int = 100, to: int = 40, cached: int = 10):
    """SDK response: .content (a list of text blocks) + .usage (tokens)."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json_text)],
        usage=SimpleNamespace(
            input_tokens=ti,
            output_tokens=to,
            cache_read_input_tokens=cached,
            cache_creation_input_tokens=0,
        ),
    )


class _FakeMessages:
    def __init__(self, side_effects):
        self._side = list(side_effects)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        nxt = self._side.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class _FakeSDK:
    """Stub of ``anthropic.AsyncAnthropic``: .messages.create over a list of effects."""

    def __init__(self, side_effects):
        self.messages = _FakeMessages(side_effects)
        self.beta = SimpleNamespace(messages=self.messages)


class _RetryableErr(Exception):
    """Emulates a 5xx of the SDK (there is status_code → _is_retryable True)."""

    def __init__(self, status_code: int):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def _client(side_effects, **kw) -> AiClient:
    sleeps: list[float] = []

    async def _fake_sleep(d):
        sleeps.append(d)

    c = AiClient(
        config=dict(_CONFIG),
        sdk_client=_FakeSDK(side_effects),
        sleep=_fake_sleep,
        backoff=kw.pop("backoff", (1, 2, 3)),
        **kw,
    )
    c._sleeps = sleeps  # type: ignore[attr-defined]  # for checking the backoff
    return c


_L2_JSON = (
    '{"summary": "подходит", "match_score": 4, "match_reasons": ["профиль"],'
    ' "red_flags": [], "required_qualifications": [], '
    '"estimated_effort_days": 30, "competition_estimate": "medium",'
    ' "focus_answers": [{"question": "лицензия?", "answer": "да"}]}'
)
_L1_JSON = (
    '{"language": "sr-Cyrl", "predicted_classifications":'
    ' [{"system_code": "eu_cpv", "code": "45000000"}],'
    ' "procedure_type": "open", "is_subcontract": false,'
    ' "is_consulting_services": false, "is_construction": true,'
    ' "topic_tags": ["строительство"], "quality_score": 0.8}'
)
_L3_JSON = (
    '{"summary": "глубокий разбор", "match_score": 5, "match_reasons": [],'
    ' "red_flags": [], "required_qualifications": [], "estimated_effort_days": 30,'
    ' "competition_estimate": "low", "work_breakdown": ["этап 1"],'
    ' "participation_checklist": ["шаг 1"], "focus_answers": []}'
)


# --------------------------------------------------------------------------- #
#  prompts — parametrisation without a hard-code (§14.2, §14.3, ADR-033)
# --------------------------------------------------------------------------- #


def test_l1_system_injects_country_no_leftover_placeholder():
    s = prompts.build_l1_system("Сербия")
    assert "Сербия" in s
    assert "{country_name}" not in s


def test_l2_system_injects_country_and_language():
    s = prompts.build_l2_system("Хорватия", "en")
    assert "Хорватия" in s
    assert "en" in s
    assert "{country_name}" not in s and "{target_language}" not in s


def test_l2_system_defaults_language_to_ru():
    s = prompts.build_l2_system("Сербия", None)
    assert "ru" in s


def test_l2_user_includes_title_description_context_and_questions():
    u = prompts.build_l2_user(
        "Видеонаблюдение",
        "Поставка камер",
        "специализируемся на CCTV",
        ["нужна ли лицензия?", "сроки?"],
    )
    assert "Видеонаблюдение" in u
    assert "Поставка камер" in u
    assert "CCTV" in u
    assert "лицензия" in u and "сроки" in u


def test_l1_user_truncates_description_to_500_chars():
    u = prompts.build_l1_user("T", "x" * 1000)
    # header + tail, but the description is cut to 500
    assert u.count("x") == 500


# --------------------------------------------------------------------------- #
#  schemas — validation of the model output (§14.2 L1, §14.3 L2, §14.4 L3)
# --------------------------------------------------------------------------- #


def test_validate_output_quick_valid():
    import json

    out = schemas.validate_output("quick", json.loads(_L1_JSON))
    assert out["procedure_type"] == "open"
    assert out["predicted_classifications"][0]["code"] == "45000000"


def test_validate_output_standard_valid():
    import json

    out = schemas.validate_output("standard", json.loads(_L2_JSON))
    assert out["match_score"] == 4
    assert out["focus_answers"][0]["answer"] == "да"


def test_validate_output_invalid_raises():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        schemas.validate_output("standard", {"match_score": "не-число"})


def test_validate_output_deep_now_supported():
    """L3 is implemented (STAGE 3) — KeyError is no longer expected."""
    import json

    out = schemas.validate_output("deep", json.loads(_L3_JSON))
    assert out["match_score"] == 5
    assert "work_breakdown" in out
    assert "participation_checklist" in out


def test_validate_output_ignores_extra_fields():
    out = schemas.validate_output("standard", {"summary": "s", "лишнее": 1})
    assert "лишнее" not in out


# --------------------------------------------------------------------------- #
#  load_model_config — reading ai_models from YAML (ADR-029)
# --------------------------------------------------------------------------- #


def test_load_model_config_reads_ai_models(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ai_models:\n  cheap: slug-a\n  balanced: slug-b\n  premium: slug-c\n",
        encoding="utf-8",
    )
    models = load_model_config(cfg)
    assert models == {"cheap": "slug-a", "balanced": "slug-b", "premium": "slug-c"}


def test_load_model_config_missing_block_raises(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("other: 1\n", encoding="utf-8")
    with pytest.raises(AiError):
        load_model_config(cfg)


def test_load_model_config_default_path_matches_repo_config():
    # The default path points to infra/config.yaml in the repo root and it is read.
    models = load_model_config()
    assert models["cheap"] == "claude-haiku-5-5"
    assert models["balanced"] == "claude-haiku-5-5"
    assert models["premium"] == "claude-opus-5-5"


# --------------------------------------------------------------------------- #
#  client.slug_for — tier→slug (ADR-029)
# --------------------------------------------------------------------------- #


def test_slug_for_maps_level_to_slug():
    c = AiClient(config=dict(_CONFIG))
    assert c.slug_for("quick") == "claude-haiku-4-5-20251001"
    assert c.slug_for("standard") == "claude-sonnet-4-6"
    assert c.slug_for("deep") == "claude-opus-4-7"


def test_slug_for_unknown_level_raises():
    c = AiClient(config=dict(_CONFIG))
    with pytest.raises(AiError):
        c.slug_for("turbo")


def test_slug_for_missing_tier_in_config_raises():
    c = AiClient(config={"cheap": "x"})  # no balanced
    with pytest.raises(AiError):
        c.slug_for("standard")


# --------------------------------------------------------------------------- #
#  client.analyze — success, slug, prompt caching, cost
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_analyze_standard_returns_validated_result():
    c = _client([_resp(_L2_JSON, ti=100, to=40, cached=10)])
    res = await c.analyze(
        AnalysisRequest(
            level="standard",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    assert res.model == "claude-sonnet-4-6"
    assert res.output["match_score"] == 4
    assert res.tokens_input == 100 and res.tokens_output == 40
    assert res.tokens_cached == 10
    assert res.cost_usd > 0  # a known slug → a non-zero estimate


@pytest.mark.asyncio
async def test_analyze_passes_model_slug_and_enables_prompt_cache():
    c = _client([_resp(_L2_JSON)])
    await c.analyze(
        AnalysisRequest(
            level="standard",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    kwargs = c._sdk.messages.calls[0]  # type: ignore[attr-defined]
    assert kwargs["model"] == "claude-sonnet-4-6"
    # the system prompt is marked for caching (§14.6)
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.asyncio
async def test_analyze_quick_uses_cheap_slug():
    c = _client([_resp(_L1_JSON)])
    res = await c.analyze(
        AnalysisRequest(
            level="quick", title="T", description="D", country_name="Сербия"
        )
    )
    assert res.model == "claude-haiku-4-5-20251001"
    assert res.output["procedure_type"] == "open"


@pytest.mark.asyncio
async def test_analyze_strips_json_fence():
    fenced = "```json\n" + _L2_JSON + "\n```"
    c = _client([_resp(fenced)])
    res = await c.analyze(
        AnalysisRequest(
            level="standard",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    assert res.output["summary"] == "подходит"


@pytest.mark.asyncio
async def test_analyze_unknown_slug_cost_zero():
    c = AiClient(
        config={"balanced": "mystery-model"},
        sdk_client=_FakeSDK([_resp(_L2_JSON)]),
    )
    res = await c.analyze(
        AnalysisRequest(
            level="standard",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    assert res.model == "mystery-model"
    assert res.cost_usd == 0.0


@pytest.mark.asyncio
async def test_analyze_invalid_json_is_response_error():
    c = _client([_resp("это не json")])
    with pytest.raises(AiResponseError):
        await c.analyze(
            AnalysisRequest(
                level="standard",
                title="T",
                description="D",
                country_name="Сербия",
                target_language="ru",
            )
        )


@pytest.mark.asyncio
async def test_analyze_deep_uses_opus_and_returns_l3():
    """deep works now (STAGE 3): slug=opus, L3Output with work_breakdown."""
    c = _client([_resp(_L3_JSON)])
    res = await c.analyze(AnalysisRequest(level="deep", title="T", description="D"))
    assert res.model == "claude-opus-4-7"
    assert "work_breakdown" in res.output
    assert "participation_checklist" in res.output


# --------------------------------------------------------------------------- #
#  failover/retry — 429/5xx (§14.7)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_retry_on_5xx_then_success():
    c = _client([_RetryableErr(503), _resp(_L2_JSON)])
    res = await c.analyze(
        AnalysisRequest(
            level="standard",
            title="T",
            description="D",
            country_name="Сербия",
            target_language="ru",
        )
    )
    assert res.output["match_score"] == 4
    assert c._sleeps == [1]  # type: ignore[attr-defined]  # one backoff (the first)


@pytest.mark.asyncio
async def test_retry_exhausted_raises_retryable():
    # 4 attempts (a backoff of 3 steps) — all 503 → AiRetryableError
    c = _client([_RetryableErr(503)] * 4)
    with pytest.raises(AiRetryableError):
        await c.analyze(
            AnalysisRequest(
                level="standard",
                title="T",
                description="D",
                country_name="Сербия",
                target_language="ru",
            )
        )
    assert c._sleeps == [1, 2, 3]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_non_retryable_error_propagates():
    # 400 (status_code not 429/5xx) — not retried, propagated as is.
    err = _RetryableErr(400)
    c = _client([err])
    with pytest.raises(Exception) as ei:
        await c.analyze(
            AnalysisRequest(
                level="standard",
                title="T",
                description="D",
                country_name="Сербия",
                target_language="ru",
            )
        )
    assert ei.value is err
    assert c._sleeps == []  # type: ignore[attr-defined]  # without retries


def test_backoff_default_matches_failover_spec():
    # §14.7: 30s / 2m / 10m.
    assert client_mod._BACKOFF_SECONDS == (30, 120, 600)


# --------------------------------------------------------------------------- #
#  Cost (audit) — the sync of prices with PRICING_PROPOSAL §0
# --------------------------------------------------------------------------- #


def test_opus_pricing_is_5_25_per_million():
    """premium (opus-4-7) = $5 in / $25 out / $0.50 cached per 1M (PRICING_PROPOSAL §0.2).

    Earlier the code held $15/$75 — 3× higher, which broke the Business payback.
    """
    assert client_mod._PRICING["claude-opus-4-7"] == (5.00, 25.00, 0.50)


def test_cost_usd_opus_matches_5_25():
    """_cost_usd for opus counts at $5/$25/$0.50: 1M in (without cache) + 1M out = $30."""
    cost = client_mod._cost_usd("claude-opus-4-7", ti=1_000_000, to=1_000_000, cached=0)
    assert cost == 30.0
    # 1M cached-read + 1M out = $0.50 + $25 = $25.50.
    cost_cached = client_mod._cost_usd(
        "claude-opus-4-7", ti=1_000_000, to=1_000_000, cached=1_000_000
    )
    assert cost_cached == 25.5
