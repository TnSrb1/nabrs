"""Wrapper over the Anthropic SDK (ARCHITECTURE §9 “AI Service Layer”, §14.6–14.7).

Responsibilities of the ``src/ai`` layer:

- **tier→slug** — reads ``infra/config.yaml`` (``ai_models``, ADR-029) and
  turns the analysis level (``quick``/``standard``/``deep``) into a concrete
  model slug. Level ↔ tier: ``quick→cheap`` (L1), ``standard→balanced`` (L2),
  ``deep→premium`` (L3).
- **prompt caching** (§14.6) — the static system prompt and the profile context
  are marked ``cache_control: ephemeral``; the tail (tender text) is not.
- **failover/retry** (§14.7) — on 429/5xx retry with backoff 30s/2m/10m; after
  the retries are exhausted — :class:`AiRetryableError` (the worker leaves the
  ``ai_pending`` row, §8 AI fail path). Non-retryable errors (parsing/validation
  of the response) — :class:`AiResponseError`, terminal (the worker neither loops
  nor burns the budget).
- **return** of :class:`AnalysisResult` with ``output`` (validated by
  :mod:`src.ai.schemas`), ``model`` (the slug at call time — for
  ``tender_ai_analyses.model``, ADR-029) and tokens/cost.

The SDK is imported lazily (as ``asyncpg`` in the routing worker): unit tests
inject a fake client and do not need ``anthropic`` installed.

DEVIATION (documented per the task): level ``deep`` (L3) is implemented as an
on-demand call by the “🧠 Deep analysis” button on the Telegram tender card.
Auto-routing of deep in :mod:`src.workers.ai` is still guarded by a circuit
breaker (NotImplementedError → ai_rejected). The price table by slug is approximate
(for cost audit, not for billing) — an unknown slug gives ``cost_usd=0.0`` +
a log line; TODO: move prices to config when the number of models grows.

DEVIATION (VG-10): the model of the ``draft`` level is chosen by the tenant's tier
(``AnalysisRequest.model_tier`` override), not by the fixed ``LEVEL_TIER``.
Matrix: pro → balanced (haiku), business/enterprise → premium (opus).

DEVIATION (14.10, ai-models-upgrade): models of the 5.5 generation (Haiku 5.5 / Opus 5.5)
think by default (Opus 5.5 — cannot be turned off), thinking tokens count
toward ``max_tokens``. Therefore: ceilings are raised, depth is set by
``output_config.effort`` per tier (``_TIER_EFFORT``), ``stop_reason`` ``max_tokens``/``refusal`` is an error, not
an answer, and text is taken only from blocks ``type == “text”``. For premium the
server-side fallback is enabled (``fallbacks: “default”``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from src.ai import prompts, schemas, translate_prompts

logger = logging.getLogger(__name__)

# infra/config.yaml relative to the repository root (src/ai/client.py → ../../).
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "infra" / "config.yaml"

# Analysis level (profiles.ai_analysis_level / analyses.level) → tier (§14.1).
LEVEL_TIER: dict[str, str] = {
    "quick": "cheap",
    "standard": "balanced",
    "deep": "premium",
    "bid": "balanced",  # on-demand bid/no-bid verdict (VG-08)
    "checklist": "balanced",  # on-demand document checklist (VG-09)
    "draft": "balanced",  # on-demand bid draft (VG-10); the model is overridden by the tenant’s plan
    "translate": "cheap",  # translation of a tender card sr→ru (VG-26) — text transfer, not analysis
}

# backoff failover (sec): 30s / 2m / 10m (§14.7).
_BACKOFF_SECONDS = (30, 120, 600)

# Ceilings include thinking tokens (thinking is always on for Haiku/Opus 5.5).
_DEFAULT_MAX_TOKENS = 8000
_DEFAULT_DEEP_MAX_TOKENS = (
    16000  # L3 deep/draft/translate — longer, a separate ceiling.
)

# Thinking depth per tier (output_config.effort). Next to LEVEL_TIER — one table.
_TIER_EFFORT: dict[str, str] = {
    "cheap": "low",
    "balanced": "medium",
    "premium": "high",
}

# Server-side fallback on the refusal classifier (only premium/Opus 5.5).
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_TIERS = frozenset({"premium"})

# Approximate cost per 1M tokens, USD (input / output / cached-read).
# For audit (ai_usage / tender_ai_analyses.cost_usd), not for billing.
# Unknown slug → zeros (cost 0.0) + log. TODO: move to config.yaml.
_PRICING: dict[str, tuple[float, float, float]] = {
    # cached-read Haiku 5.5 — 0.1× of input (estimate, not stated in the reference).
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-haiku-4-5-20251001": (1.00, 5.00, 0.10),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
}


class AiError(Exception):
    """Base error of the AI layer."""


class AiRetryableError(AiError):
    """Temporary error after the retries are exhausted (429/5xx) — row → ai_pending."""


@dataclass(frozen=True)
class Spend:
    """Spent on the call even if the answer was not accepted: tokens and price."""

    tokens: int
    cost_usd: float


class AiResponseError(AiError):
    """The model’s answer did not parse / did not pass validation — terminal.

    ``spend`` — the cost of the call if the model still answered (truncated/refused);
    it is counted in ``ai_usage`` without a limit unit (see ``record_error_spend``).
    """

    def __init__(self, message: str = "", *, spend: Spend | None = None) -> None:
        super().__init__(message)
        self.spend = spend


class AiTruncatedError(AiResponseError):
    """``stop_reason == "max_tokens"``: the answer was truncated (JSON incomplete) — terminal."""


class AiRefusalError(AiResponseError):
    """``stop_reason == "refusal"``: the model refused (classifier) — terminal."""


@dataclass(frozen=True)
class AnalysisRequest:
    """Request for the analysis of one tender for one level."""

    level: str  # quick / standard / deep / bid / checklist / draft
    title: str | None
    description: str | None
    country_name: str | None = None
    target_language: str | None = None
    extra_context: str | None = None
    focus_questions: Sequence[str] = field(default_factory=tuple)
    model_tier: str | None = None  # tier override for choosing the slug (VG-10 draft)


@dataclass(frozen=True)
class AnalysisResult:
    """Analysis result: validated output + slug + tokens + cost."""

    output: dict[str, Any]
    model: str
    tokens_input: int
    tokens_output: int
    tokens_cached: int
    cost_usd: float


def load_model_config(path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Reads ``ai_models`` (tier→slug) from ``infra/config.yaml`` (ADR-029)."""
    import yaml  # lazy import — tests with an injected config do not need PyYAML

    cfg_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    models = data.get("ai_models") or {}
    if not models:
        raise AiError(f"no ai_models block in {cfg_path} (ADR-029)")
    return {str(k): str(v) for k, v in models.items()}


def _is_retryable(exc: Exception) -> bool:
    """429/5xx/network — retryable (§14.7). Classifies without importing the SDK."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    name = type(exc).__name__
    return name in {
        "RateLimitError",
        "InternalServerError",
        "APIConnectionError",
        "APITimeoutError",
        "APIStatusError",
    }


def _strip_json(text: str) -> str:
    """Strips the markdown fence ```json … ``` around the model’s answer."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _extract_text(response: Any) -> str:
    """Joins the text blocks of the SDK answer (.content: list[block])."""
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    parts: list[str] = []
    for block in content or []:
        btype = getattr(block, "type", None)
        if btype is None and isinstance(block, dict):
            btype = block.get("type")
        if btype is not None and btype != "text":
            continue  # thinking / redacted_thinking / fallback — not an answer
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _stop_reason(response: Any) -> str | None:
    reason = getattr(response, "stop_reason", None)
    if reason is None and isinstance(response, dict):
        reason = response.get("stop_reason")
    return reason if isinstance(reason, str) else None


def _served_model(response: Any, requested: str) -> str:
    """The model that actually answered (with a server-side fallback it differs from the requested one)."""
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    return model if isinstance(model, str) and model in _PRICING else requested


def _usage_tokens(response: Any) -> tuple[int, int, int]:
    """Extracts (input, output, cached) tokens from ``response.usage``."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")

    def _get(obj: Any, key: str) -> int:
        val = getattr(obj, key, None)
        if val is None and isinstance(obj, dict):
            val = obj.get(key)
        return int(val or 0)

    ti = _get(usage, "input_tokens")
    to = _get(usage, "output_tokens")
    cached = _get(usage, "cache_read_input_tokens") + _get(
        usage, "cache_creation_input_tokens"
    )
    return ti, to, cached


def _spend_of(response: Any, requested: str) -> Spend:
    """Spend of an answer that was not accepted as a result (truncation/refusal): tokens and price."""
    ti, to, cached = _usage_tokens(response)
    served = _served_model(response, requested)
    return Spend(tokens=ti + to, cost_usd=_cost_usd(served, ti, to, cached))


def _cost_usd(slug: str, ti: int, to: int, cached: int) -> float:
    """Approximate cost of the call in USD by slug (for audit)."""
    pricing = _PRICING.get(slug)
    if pricing is None:
        logger.warning("ai: no pricing for slug=%s, cost=0.0", slug)
        return 0.0
    in_price, out_price, cached_price = pricing
    fresh_in = max(ti - cached, 0)
    return round(
        fresh_in / 1e6 * in_price + cached / 1e6 * cached_price + to / 1e6 * out_price,
        6,
    )


class AiClient:
    """Anthropic client with tier→slug, prompt caching, failover (§14).

    Dependencies are injected (config, SDK client, sleep) — for unit tests with a
    fake SDK, without network and without ``anthropic`` installed.

    Parameters:
        max_tokens: ceiling for quick/standard/bid/checklist (default 8000,
            including thinking tokens).
        deep_max_tokens: ceiling for deep/draft/translate (default 16000).
    """

    def __init__(
        self,
        *,
        config: dict[str, str] | None = None,
        config_path: str | os.PathLike[str] | None = None,
        sdk_client: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        deep_max_tokens: int = _DEFAULT_DEEP_MAX_TOKENS,
        backoff: Sequence[int] = _BACKOFF_SECONDS,
    ) -> None:
        self._config = config
        self._config_path = config_path
        self._sdk = sdk_client
        self._sleep = sleep
        self._max_tokens = max_tokens
        self._deep_max_tokens = deep_max_tokens
        self._backoff = tuple(backoff)

    # -- config and SDK ------------------------------------------------------ #

    def _models(self) -> dict[str, str]:
        if self._config is None:
            self._config = load_model_config(self._config_path)
        return self._config

    def slug_for(self, level: str, *, override_tier: str | None = None) -> str:
        """slug of the model for the analysis level (via tier, ADR-029).

        If ``override_tier`` is set — the slug is taken by it (VG-10 draft: choice
        by the tenant’s tier), otherwise by ``LEVEL_TIER[level]``.
        """
        tier: str | None
        if override_tier is not None:
            tier = override_tier
        else:
            tier = LEVEL_TIER.get(level)
            if tier is None:
                raise AiError(f"unknown analysis level: {level!r}")
        models = self._models()
        if tier not in models:
            raise AiError(f"config.yaml has no slug for tier={tier!r}")
        return models[tier]

    def tier_for(self, level: str, *, override_tier: str | None = None) -> str:
        """tier of the analysis level (with the tier override, VG-10)."""
        if override_tier is not None:
            return override_tier
        tier = LEVEL_TIER.get(level)
        if tier is None:
            raise AiError(f"unknown analysis level: {level!r}")
        return tier

    def _get_sdk(self) -> Any:
        if self._sdk is None:  # lazy import of the real SDK
            import anthropic

            self._sdk = anthropic.AsyncAnthropic()
        return self._sdk

    def warm(self) -> None:
        """Import of the SDK and creation of the client in advance (warm-up at start). Does not touch the network."""
        self._get_sdk()

    # -- public API ----------------------------------------------------- #

    async def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        """Analyzes a tender for the request level (quick / standard / deep / bid / checklist / draft).

        L3 deep — on-demand by the button; auto-routing of deep is still guarded
        by the NotImplementedError circuit breaker in :mod:`src.workers.ai`.
        """
        if request.level not in (
            "quick",
            "standard",
            "deep",
            "bid",
            "checklist",
            "draft",
            "translate",
        ):
            raise AiError(f"unsupported level: {request.level!r}")

        slug = self.slug_for(request.level, override_tier=request.model_tier)
        tier = self.tier_for(request.level, override_tier=request.model_tier)
        # Deep and draft use the raised token ceiling; translate too:
        # the translation output is proportional to the input (the tender description), not to a short summary.
        max_tokens = (
            self._deep_max_tokens
            if request.level in ("deep", "draft", "translate")
            else self._max_tokens
        )
        system, messages = self._build_prompt(request)
        response = await self._call_with_retry(
            slug, system, messages, max_tokens, tier=tier
        )

        stop = _stop_reason(response)
        if stop == "refusal":
            logger.warning("ai: refusal slug=%s level=%s", slug, request.level)
            raise AiRefusalError(
                f"model refused (refusal), slug={slug}",
                spend=_spend_of(response, slug),
            )
        if stop == "max_tokens":
            logger.warning("ai: max_tokens slug=%s level=%s", slug, request.level)
            raise AiTruncatedError(
                f"answer truncated by max_tokens={max_tokens}, slug={slug}",
                spend=_spend_of(response, slug),
            )

        raw_text = _extract_text(response)
        try:
            parsed = json.loads(_strip_json(raw_text))
            output = schemas.validate_output(request.level, parsed)
        except Exception as exc:  # noqa: BLE001 — any parsing failure is terminal
            raise AiResponseError(f"invalid model answer: {exc}") from exc

        ti, to, cached = _usage_tokens(response)
        served = _served_model(response, slug)
        return AnalysisResult(
            output=output,
            model=served,
            tokens_input=ti,
            tokens_output=to,
            tokens_cached=cached,
            cost_usd=_cost_usd(served, ti, to, cached),
        )

    # -- internal -------------------------------------------------------- #

    def _build_prompt(
        self, request: AnalysisRequest
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Builds system + messages with prompt caching (§14.6).

        The system prompt is cached (shared per (country, language)); the profile
        context — as a separate cacheable block; the tail (tender) is not cached.
        """
        cache = {"type": "ephemeral"}
        # Some blocks carry cache_control (nested dict), some are plain text.
        user_blocks: list[dict[str, Any]]
        if request.level == "quick":
            system_text = prompts.build_l1_system(request.country_name)
            user_blocks = [
                {
                    "type": "text",
                    "text": prompts.build_l1_user(request.title, request.description),
                }
            ]
        elif request.level == "standard":
            system_text = prompts.build_l2_system(
                request.country_name, request.target_language
            )
            user_blocks = []
            if request.extra_context:
                user_blocks.append(
                    {
                        "type": "text",
                        "text": f"КОНТЕКСТ ПОСТАВЩИКА:\n{request.extra_context}",
                        "cache_control": cache,  # cached at the profile level
                    }
                )
            user_blocks.append(
                {
                    "type": "text",
                    "text": prompts.build_l2_user(
                        request.title,
                        request.description,
                        None,
                        request.focus_questions,
                    ),
                }
            )
        elif request.level == "bid":
            system_text = prompts.build_bid_system(
                request.country_name, request.target_language
            )
            user_blocks = [
                {
                    "type": "text",
                    "text": prompts.build_bid_user(
                        request.title,
                        request.description,
                        request.extra_context,  # contains the profile parameters
                    ),
                }
            ]
        elif request.level == "checklist":
            system_text = prompts.build_checklist_system(
                request.country_name, request.target_language
            )
            user_blocks = [
                {
                    "type": "text",
                    "text": prompts.build_checklist_user(
                        request.title,
                        request.description,
                        request.extra_context,  # contains the profile parameters
                    ),
                }
            ]
        elif request.level == "translate":
            # The translation prompt comes from a separate module (see src/ai/translate_prompts).
            system_text = translate_prompts.build_translate_system(
                request.target_language or "русский"
            )
            user_blocks = [
                {
                    "type": "text",
                    "text": translate_prompts.build_translate_user(
                        request.title, request.description
                    ),
                }
            ]
        elif request.level == "draft":
            system_text = prompts.build_draft_system(
                request.country_name, request.target_language
            )
            user_blocks = [
                {
                    "type": "text",
                    "text": prompts.build_draft_user(
                        request.title,
                        request.description,
                        request.extra_context,  # contains the profile parameters
                    ),
                }
            ]
        else:  # deep
            system_text = prompts.build_l3_system(
                request.country_name, request.target_language
            )
            user_blocks = []
            if request.extra_context:
                user_blocks.append(
                    {
                        "type": "text",
                        "text": f"КОНТЕКСТ ПОСТАВЩИКА:\n{request.extra_context}",
                        "cache_control": cache,
                    }
                )
            user_blocks.append(
                {
                    "type": "text",
                    "text": prompts.build_l3_user(
                        request.title,
                        request.description,
                        None,  # At MVP extra_context is not in the user block (shared cache).
                        request.focus_questions or None,
                    ),
                }
            )

        system = [{"type": "text", "text": system_text, "cache_control": cache}]
        messages = [{"role": "user", "content": user_blocks}]
        return system, messages

    async def _call_with_retry(
        self,
        slug: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        *,
        tier: str | None = None,
    ) -> Any:
        """Model call with retries 30s/2m/10m on 429/5xx (§14.7)."""
        sdk = self._get_sdk()
        effective_max_tokens = (
            max_tokens if max_tokens is not None else self._max_tokens
        )
        params: dict[str, Any] = {
            "model": slug,
            "max_tokens": effective_max_tokens,
            "system": system,
            "messages": messages,
        }
        effort = _TIER_EFFORT.get(tier) if tier else None
        if effort:
            params["output_config"] = {"effort": effort}
        use_fallback = tier in _FALLBACK_TIERS
        attempts = len(self._backoff) + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                if use_fallback:
                    return await sdk.beta.messages.create(
                        **params,
                        betas=[_FALLBACK_BETA],
                        extra_body={"fallbacks": "default"},
                    )
                return await sdk.messages.create(**params)
            except Exception as exc:  # noqa: BLE001 — classified below
                if not _is_retryable(exc):
                    raise
                last_exc = exc
                if attempt < len(self._backoff):
                    delay = self._backoff[attempt]
                    logger.warning(
                        "ai: retryable error (%s), retry in %ss",
                        type(exc).__name__,
                        delay,
                    )
                    await self._sleep(delay)
        raise AiRetryableError(
            f"model {slug} retries exhausted: {last_exc}"
        ) from last_exc
