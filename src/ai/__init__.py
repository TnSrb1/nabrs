"""AI Service Layer (ARCHITECTURE §9, §14).

Wrappers over the Anthropic SDK: tier→slug (ADR-029), prompt caching, failover,
validation of the JSON output. Used by the AI worker :mod:`src.workers.ai`.
"""

from __future__ import annotations

from src.ai.client import (
    AiClient,
    AiError,
    AiResponseError,
    AiRetryableError,
    AnalysisRequest,
    AnalysisResult,
    load_model_config,
)

__all__ = [
    "AiClient",
    "AiError",
    "AiResponseError",
    "AiRetryableError",
    "AnalysisRequest",
    "AnalysisResult",
    "load_model_config",
]
