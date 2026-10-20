"""On-demand AI bid draft (VG-10) — the service for the button “📝 Bid draft”.

Called from :func:`src.bots.telegram.on_draft_application` when the
callback ``ai:drf:{tender_id}`` is pressed.

Mirrors the architecture of :mod:`src.bots.compliance_checklist_service` (VG-09).
New relative to VG-09: the model is chosen by the tenant’s plan (DEVIATION VG-10):
    pro  → balanced (claude-haiku-5-5 per infra/config.yaml)
    business/enterprise → premium (claude-opus-5-5)

The level “draft” → cache per (tender_id, profile_id). The body of the draft (sections.body,
criteria_mapping.response) is generated in Serbian (the language of the bid submission).
The service summary/disclaimer — in the target_language of the profile.
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

# The mapping of plan → model plan (VG-10: the choice of the model by plan).
_DRAFT_TIER_MODEL: dict[str, str] = {
    "pro": "balanced",
    "business": "premium",
    "enterprise": "premium",
}

_LOAD_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = :tenant_id
    """)

_LOAD_LATEST_PROFILE_SQL = text("""
    SELECT
        p.id               AS id,
        p.classifications  AS classifications,
        p.regions          AS regions,
        p.keywords_include AS keywords_include,
        p.min_amount_eur   AS min_amount_eur,
        p.max_amount_eur   AS max_amount_eur,
        p.procedure_types  AS procedure_types,
        p.ai_extra_context AS ai_extra_context,
        p.target_language  AS target_language
    FROM profiles p
    WHERE p.tenant_id = :tenant_id
      AND p.deleted_at IS NULL
    ORDER BY p.created_at DESC
    LIMIT 1
    """)

_LOAD_TENDER_SQL = text("""
    SELECT tn.title, tn.description, c.name AS country_name
    FROM tenders_normalized tn
    JOIN tenants t ON t.id = :tenant_id
    LEFT JOIN countries c ON c.code = t.country_code
    WHERE tn.id = :tender_id
    LIMIT 1
    """)

_LOAD_CACHED_OUTPUT_SQL = text("""
    SELECT output FROM tender_ai_analyses WHERE id = :id LIMIT 1
    """)


@dataclass(frozen=True)
class DraftResult:
    """The result of the on-demand bid draft for rendering in Telegram."""

    status: str  # 'success' | 'no_account' | 'gate_pro' | 'limit_exhausted'
    # | 'model_busy' | 'model_error' | 'no_tender' | 'no_profile' | 'frozen'
    html: str = ""  # a ready HTML on success


_DEFAULT_AI_CLIENT: AiClient | None = None


def _get_default_ai_client() -> AiClient:
    global _DEFAULT_AI_CLIENT
    if _DEFAULT_AI_CLIENT is None:
        _DEFAULT_AI_CLIENT = AiClient()
    return _DEFAULT_AI_CLIENT


async def run_draft_application(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    ai_client: AiClient | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> DraftResult:
    """On-demand AI bid draft for a tender for a specific profile.

    Steps:
        a. resolve_tenant_id(chat_id) → no binding → no_account.
        b. Freeze (access_until expired) → frozen BEFORE the tier gate and the model (FIX-SG-04).
           Tier < Pro → gate_pro (draft=0).
        c. The latest profile of the tenant → no_profile.
        d. Cache lookup per (tender_id, profile_id, “draft”).
        e. The §14.5 limit for the level “draft”.
        f. Loading the tender (description with the text of the specification).
        g. The choice of the model by tier (_DRAFT_TIER_MODEL); AiClient.analyze(level='draft').
        h. finalize (INSERT + UPSERT usage).
        i. Rendering JSON → an HTML message.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    client = ai_client or _get_default_ai_client()

    # a. Resolve tenant_id by chat_id.
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return DraftResult(status="no_account")

    # b. Plan, freeze and the Pro+ gate.
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return DraftResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    # It also feeds the model choice _DRAFT_TIER_MODEL below (trial → balanced as Pro).
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    tier_row = limits.get(tier, {})
    if tier_row.get("draft", 0) == 0:
        return DraftResult(status="gate_pro")

    # c. The latest profile of the tenant (profile_id is resolved here, not in the callback).
    profile_row = (
        await session.execute(_LOAD_LATEST_PROFILE_SQL, {"tenant_id": tenant_id})
    ).first()
    if profile_row is None:
        return DraftResult(status="no_profile")
    profile_id = str(profile_row.id)
    target_language = (
        str(profile_row.target_language) if profile_row.target_language else "ru"
    )

    # d. Cache (profile_id is required — the draft depends on the profile).
    cached_id = await cache_lookup(session, tender_id, profile_id, "draft")
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
                "drf: cache hit analysis=%s tender=%s profile=%s chat=%s",
                cached_id,
                tender_id,
                profile_id,
                chat_id,
            )
            return DraftResult(
                status="success", html=_render_draft(title, cached_output, locale)
            )

    # e. The §14.5 limit.
    decision = await limit_decision(
        session,
        tenant_id=tenant_id,
        tier=tier,
        level="draft",
        tier_limits=limits,
    )
    if decision == "exhausted":
        return DraftResult(status="limit_exhausted")
    if decision != "ok":
        return DraftResult(status="gate_pro")

    # f. Loading the tender.
    tender_row = (
        await session.execute(
            _LOAD_TENDER_SQL, {"tender_id": tender_id, "tenant_id": tenant_id}
        )
    ).first()
    if tender_row is None:
        return DraftResult(status="no_tender")
    title = str(tender_row.title) if tender_row.title else ""
    description = str(tender_row.description) if tender_row.description else ""
    country_name = str(tender_row.country_name) if tender_row.country_name else "Сербия"

    # The profile context is built ONLY on the path to the model (after a cache miss), so that the
    # track record is not read on a cache hit. The track record under the same tenant GUC
    # (resolve_tenant_id has already set it in step a).
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

    # g. The choice of the model by tier (VG-10: override tier); the model call.
    model_tier = _DRAFT_TIER_MODEL.get(tier or "")
    request = AnalysisRequest(
        level="draft",
        title=title,
        description=description,
        country_name=country_name,
        target_language=target_language,
        extra_context=profile_context,
        model_tier=model_tier,
    )
    try:
        result = await client.analyze(request)
    except AiRetryableError:
        logger.warning("drf: model busy tender=%s profile=%s", tender_id, profile_id)
        return DraftResult(status="model_busy")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "drf: analysis error tender=%s profile=%s", tender_id, profile_id
        )
        await record_error_spend(session, tenant_id=tenant_id, level="draft", exc=exc)
        return DraftResult(status="model_error")

    # h. INSERT + UPSERT usage.
    analysis_id = await finalize_with_insert(
        session,
        tender_id=tender_id,
        profile_id=profile_id,  # per-profile, NOT NULL
        tenant_id=tenant_id,
        level="draft",
        result=result,
    )
    if analysis_id is None:
        logger.error("drf: finalize returned None — we display without saving")

    # i. Rendering.
    return DraftResult(
        status="success", html=_render_draft(title, result.output, locale)
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


def _render_draft(title: str, output: dict, locale: str | None = None) -> str:
    """Formats DraftOutput → an HTML string for Telegram (parse_mode=HTML).

    A draft longer than 4096 characters is cut at the section boundary with “…”
    (RISK: the information may be incomplete — the AI caveat stands under the heading).
    """
    sections = output.get("sections") or []
    criteria_mapping = output.get("criteria_mapping") or []
    summary_text = output.get("summary") or ""

    parts: list[str] = [
        f"📝 <b>{bot_t(locale, 'dr_title')}</b>\n<b>{html.escape(title or '—')}</b>",
        # Under the heading, not in the tail: the trim below cuts the tail of a long draft.
        f"⚠️ <i>{bot_t(locale, 'ai_disclaimer')}</i>",
    ]

    # The sections of the draft.
    for section in sections:
        heading = html.escape(str(section.get("heading") or ""))
        body = html.escape(str(section.get("body") or ""))
        if heading or body:
            parts.append(f"<b>{heading}</b>\n{body}" if heading else body)

    # The mapping to the evaluation criteria.
    if criteria_mapping:
        lines = [f"🎯 <b>{bot_t(locale, 'dr_mapping')}</b>"]
        for item in criteria_mapping:
            criterion = html.escape(str(item.get("criterion") or ""))
            response = html.escape(str(item.get("response") or ""))
            if criterion:
                lines.append(f"  • <b>{criterion}</b>\n    {response}")
        parts.append("\n".join(lines))

    if summary_text:
        parts.append(f"💡 {html.escape(str(summary_text))}")

    result = "\n\n".join(parts)

    # A safe trim to the Telegram limit of 4096 (the boundary by section).
    if len(result) > 4090:
        cut = result.rfind("\n\n", 0, 4080)
        if cut == -1:
            cut = 4080
        result = result[:cut] + "\n\n…"

    return result
