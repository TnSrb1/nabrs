"""On-demand bid/no-bid verdict (VG-08) — the service for the Telegram button “🎯 Is it worth it?”.

Called from :func:`src.bots.telegram.on_bid_verdict` when the button
callback ``ai:bid:{tender_id}:{profile_id}`` is pressed.

Key differences from the L3 deep (:mod:`src.bots.deep_analysis_service`):
- Cache per (tender_id, profile_id) — the verdict depends on the profile, NEVER shared per tender.
- The level “bid” → the balanced tier (claude-haiku-5-5 per infra/config.yaml) — cheaper than premium (opus).
- Loads the profile (CPV, regions, keywords, extra_context) for assessing the match.
- Output: JSON {verdict, score, pros, cons} instead of the detailed L3 analysis.

Pro+ gate and freeze: the same predicates as in L3 (is_frozen / _TIER_LIMITS['bid']).
"""

from __future__ import annotations

import html
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from src.ai import AiClient, AiRetryableError, AnalysisRequest
from src.ai.profile_context import format_profile_context
from src.ai.track_record import build_track_record_context
from src.ai.usage import (
    _TIER_LIMITS,
    cache_lookup,
    finalize_with_insert,
    limit_decision,
    record_error_spend,
)
from src.bots.i18n import bot_t
from src.bots.status_service import resolve_tenant_id
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier

logger = logging.getLogger(__name__)

_LOAD_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = :tenant_id
    """)

_LOAD_TENDER_SQL = text("""
    SELECT tn.title, tn.description, c.name AS country_name
    FROM tenders_normalized tn
    JOIN tenants t ON t.id = :tenant_id
    LEFT JOIN countries c ON c.code = t.country_code
    WHERE tn.id = :tender_id
    LIMIT 1
    """)

_LOAD_PROFILE_SQL = text("""
    SELECT
        p.classifications  AS classifications,
        p.regions          AS regions,
        p.keywords_include AS keywords_include,
        p.min_amount_eur   AS min_amount_eur,
        p.max_amount_eur   AS max_amount_eur,
        p.procedure_types  AS procedure_types,
        p.ai_extra_context AS ai_extra_context,
        p.target_language  AS target_language
    FROM profiles p
    WHERE p.id = :profile_id
      AND p.deleted_at IS NULL
    LIMIT 1
    """)

_LOAD_CACHED_OUTPUT_SQL = text("""
    SELECT output FROM tender_ai_analyses WHERE id = :id LIMIT 1
    """)


@dataclass(frozen=True)
class BidVerdictResult:
    """The result of the on-demand bid verdict for rendering in Telegram."""

    status: str  # 'success' | 'no_account' | 'gate_pro' | 'limit_exhausted'
    # | 'model_busy' | 'model_error' | 'no_tender' | 'no_profile' | 'frozen'
    html: str = ""  # a ready HTML on success


_DEFAULT_AI_CLIENT: AiClient | None = None


def _get_default_ai_client() -> AiClient:
    global _DEFAULT_AI_CLIENT
    if _DEFAULT_AI_CLIENT is None:
        _DEFAULT_AI_CLIENT = AiClient()
    return _DEFAULT_AI_CLIENT


async def run_bid_verdict(
    session: Any,
    *,
    tender_id: str,
    profile_id: str,
    chat_id: int,
    ai_client: AiClient | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> BidVerdictResult:
    """On-demand bid/no-bid verdict for a tender for a specific profile.

    Steps:
        a. resolve_tenant_id(chat_id) → no binding → no_account.
        b. Freeze (access_until expired) → frozen BEFORE the tier gate and the model (FIX-SG-04).
           Tier < Pro → gate_pro.
        c. Cache lookup (profile_id is non-zero — the verdict is profile-specific).
        d. The §14.5 limit for the level “bid”.
        e. Loading the tender (description with the text of the specification) + the profile (CPV/regions/keywords).
        f. AiClient.analyze(level='bid') → the balanced model from infra/config.yaml.
        g. finalize (INSERT + UPSERT usage).
        h. Rendering JSON → an HTML message.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    client = ai_client or _get_default_ai_client()

    # a. Resolve tenant_id by chat_id.
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return BidVerdictResult(status="no_account")

    # b. Plan, freeze and the Pro+ gate.
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return BidVerdictResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    tier_row = limits.get(tier, {})
    if tier_row.get("bid", 0) == 0:
        return BidVerdictResult(status="gate_pro")

    # c. Cache (profile_id is required — the verdict depends on the profile).
    cached_id = await cache_lookup(session, tender_id, profile_id, "bid")
    if cached_id is not None:
        tender_row = (
            await session.execute(
                _LOAD_TENDER_SQL, {"tender_id": tender_id, "tenant_id": tenant_id}
            )
        ).first()
        title = str(tender_row.title) if tender_row and tender_row.title else ""
        cached_output = await _load_cached_output(session, cached_id)
        if cached_output is not None:
            logger.info(
                "bid: cache hit analysis=%s tender=%s profile=%s chat=%s",
                cached_id,
                tender_id,
                profile_id,
                chat_id,
            )
            return BidVerdictResult(
                status="success", html=_render_bid_verdict(title, cached_output, locale)
            )

    # d. The §14.5 limit.
    decision = await limit_decision(
        session,
        tenant_id=tenant_id,
        tier=tier,
        level="bid",
        tier_limits=limits,
    )
    if decision == "exhausted":
        return BidVerdictResult(status="limit_exhausted")
    if decision != "ok":
        return BidVerdictResult(status="gate_pro")

    # e. Loading the tender.
    tender_row = (
        await session.execute(
            _LOAD_TENDER_SQL, {"tender_id": tender_id, "tenant_id": tenant_id}
        )
    ).first()
    if tender_row is None:
        return BidVerdictResult(status="no_tender")
    title = str(tender_row.title) if tender_row.title else ""
    description = str(tender_row.description) if tender_row.description else ""
    country_name = str(tender_row.country_name) if tender_row.country_name else "Сербия"

    # Loading the profile.
    profile_row = (
        await session.execute(_LOAD_PROFILE_SQL, {"profile_id": profile_id})
    ).first()
    if profile_row is None:
        return BidVerdictResult(status="no_profile")
    target_language = (
        str(profile_row.target_language) if profile_row.target_language else "ru"
    )
    # The track record under the same tenant GUC (resolve_tenant_id has already set it in
    # step a) — the behavioral memory of the company goes into the same profile_context.
    track_record = await build_track_record_context(session, tenant_id)
    profile_context = format_profile_context(
        classifications=getattr(profile_row, "classifications", None),
        regions=getattr(profile_row, "regions", None),
        keywords_include=getattr(profile_row, "keywords_include", None),
        min_amount_eur=getattr(profile_row, "min_amount_eur", None),
        max_amount_eur=getattr(profile_row, "max_amount_eur", None),
        procedure_types=getattr(profile_row, "procedure_types", None),
        ai_extra_context=getattr(profile_row, "ai_extra_context", None),
        track_record=track_record,
    )

    # f. The model call (balanced — from the config, not hardcoded).
    request = AnalysisRequest(
        level="bid",
        title=title,
        description=description,
        country_name=country_name,
        target_language=target_language,
        extra_context=profile_context,
    )
    try:
        result = await client.analyze(request)
    except AiRetryableError:
        logger.warning("bid: model busy tender=%s profile=%s", tender_id, profile_id)
        return BidVerdictResult(status="model_busy")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "bid: analysis error tender=%s profile=%s", tender_id, profile_id
        )
        await record_error_spend(session, tenant_id=tenant_id, level="bid", exc=exc)
        return BidVerdictResult(status="model_error")

    # g. INSERT + UPSERT usage.
    analysis_id = await finalize_with_insert(
        session,
        tender_id=tender_id,
        profile_id=profile_id,  # per-profile, NOT NULL
        tenant_id=tenant_id,
        level="bid",
        result=result,
    )
    if analysis_id is None:
        logger.error("bid: finalize returned None — we display without saving")

    # h. Rendering.
    return BidVerdictResult(
        status="success", html=_render_bid_verdict(title, result.output, locale)
    )


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #


async def _load_cached_output(session: Any, analysis_id: str) -> dict | None:
    row = (await session.execute(_LOAD_CACHED_OUTPUT_SQL, {"id": analysis_id})).first()
    if row is None:
        return None
    out = row.output
    if isinstance(out, dict):
        return out
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


_VERDICT_ICONS = {"go": "🟢", "caution": "🟡", "no_go": "🔴"}
_VERDICT_LABEL_KEYS = {"go": "bv_go", "caution": "bv_caution", "no_go": "bv_no_go"}


def _render_bid_verdict(title: str, output: dict, locale: str | None = None) -> str:
    """Formats BidVerdictOutput → an HTML string for Telegram (parse_mode=HTML)."""
    verdict = output.get("verdict") or ""
    score = output.get("score")
    pros = output.get("pros") or []
    cons = output.get("cons") or []

    icon = _VERDICT_ICONS.get(verdict, "⚪")
    label_key = _VERDICT_LABEL_KEYS.get(verdict)
    label = bot_t(locale, label_key) if label_key else html.escape(verdict or "—")
    score_str = f" {score}/100" if score is not None else ""

    parts: list[str] = [
        f"🎯 <b>{bot_t(locale, 'bv_verdict')}</b> {icon} {label}{score_str}",
        f"<b>{html.escape(title or '—')}</b>",
    ]

    if pros:
        items = "\n".join(f"  • {html.escape(str(p))}" for p in pros)
        parts.append(f"✅ <b>{bot_t(locale, 'bv_pros')}</b>\n{items}")

    if cons:
        items = "\n".join(f"  • {html.escape(str(c))}" for c in cons)
        parts.append(f"⚠️ <b>{bot_t(locale, 'bv_cons')}</b>\n{items}")

    parts.append(f"⚠️ <i>{bot_t(locale, 'ai_disclaimer')}</i>")
    return "\n\n".join(parts)
