"""On-demand checklist “what to prepare” (VG-09) — the service for the button “📋 What to prepare?”.

Called from :func:`src.bots.telegram.on_compliance_checklist` when the
callback ``ai:chk:{tender_id}`` is pressed.

Mirrors the architecture of :mod:`src.bots.bid_verdict_service` (VG-08):
- Cache per (tender_id, profile_id) — the checklist is profile-specific.
- The level “checklist” → the balanced tier (claude-haiku-5-5 per infra/config.yaml) — cheaper than premium (opus).
- Profile-aware: highlights the gaps “required vs present in the profile”.
- Output: JSON {documents, deadlines, disqualification_risks, summary}.

Pro+ gate and freeze: the same predicates as VG-08 (is_frozen / _TIER_LIMITS['checklist']).
profile_id is resolved on the server (chat_id → resolve_tenant_id → _latest_profile_for_tenant).
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
class ChecklistResult:
    """The result of the on-demand checklist for rendering in Telegram."""

    status: str  # 'success' | 'no_account' | 'gate_pro' | 'limit_exhausted'
    # | 'model_busy' | 'model_error' | 'no_tender' | 'no_profile' | 'frozen'
    html: str = ""  # a ready HTML on success


_DEFAULT_AI_CLIENT: AiClient | None = None


def _get_default_ai_client() -> AiClient:
    global _DEFAULT_AI_CLIENT
    if _DEFAULT_AI_CLIENT is None:
        _DEFAULT_AI_CLIENT = AiClient()
    return _DEFAULT_AI_CLIENT


async def run_compliance_checklist(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    ai_client: AiClient | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> ChecklistResult:
    """On-demand checklist for a tender for a specific profile.

    Steps:
        a. resolve_tenant_id(chat_id) → no binding → no_account.
        b. Freeze (access_until expired) → frozen BEFORE the tier gate and the model (FIX-SG-04).
           Tier < Pro → gate_pro.
        c. The latest profile of the tenant → no_profile.
        d. Cache lookup (profile_id is non-zero — the checklist is profile-specific).
        e. The §14.5 limit for the level “checklist”.
        f. Loading the tender (description with the text of the specification).
        g. AiClient.analyze(level='checklist') → the balanced model from infra/config.yaml.
        h. finalize (INSERT + UPSERT usage).
        i. Rendering JSON → an HTML message.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    client = ai_client or _get_default_ai_client()

    # a. Resolve tenant_id by chat_id.
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return ChecklistResult(status="no_account")

    # b. Plan, freeze and the Pro+ gate.
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return ChecklistResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    tier_row = limits.get(tier, {})
    if tier_row.get("checklist", 0) == 0:
        return ChecklistResult(status="gate_pro")

    # c. The latest profile of the tenant (profile_id is resolved here, not in the callback).
    profile_row = (
        await session.execute(_LOAD_LATEST_PROFILE_SQL, {"tenant_id": tenant_id})
    ).first()
    if profile_row is None:
        return ChecklistResult(status="no_profile")
    profile_id = str(profile_row.id)
    target_language = (
        str(profile_row.target_language) if profile_row.target_language else "ru"
    )

    # d. Cache (profile_id is required — the checklist depends on the profile).
    cached_id = await cache_lookup(session, tender_id, profile_id, "checklist")
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
                "chk: cache hit analysis=%s tender=%s profile=%s chat=%s",
                cached_id,
                tender_id,
                profile_id,
                chat_id,
            )
            return ChecklistResult(
                status="success", html=_render_checklist(title, cached_output, locale)
            )

    # e. The §14.5 limit.
    decision = await limit_decision(
        session,
        tenant_id=tenant_id,
        tier=tier,
        level="checklist",
        tier_limits=limits,
    )
    if decision == "exhausted":
        return ChecklistResult(status="limit_exhausted")
    if decision != "ok":
        return ChecklistResult(status="gate_pro")

    # f. Loading the tender.
    tender_row = (
        await session.execute(
            _LOAD_TENDER_SQL, {"tender_id": tender_id, "tenant_id": tenant_id}
        )
    ).first()
    if tender_row is None:
        return ChecklistResult(status="no_tender")
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

    # g. The model call (balanced — from the config, not hardcoded).
    request = AnalysisRequest(
        level="checklist",
        title=title,
        description=description,
        country_name=country_name,
        target_language=target_language,
        extra_context=profile_context,
    )
    try:
        result = await client.analyze(request)
    except AiRetryableError:
        logger.warning("chk: model busy tender=%s profile=%s", tender_id, profile_id)
        return ChecklistResult(status="model_busy")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "chk: analysis error tender=%s profile=%s", tender_id, profile_id
        )
        await record_error_spend(
            session, tenant_id=tenant_id, level="checklist", exc=exc
        )
        return ChecklistResult(status="model_error")

    # h. INSERT + UPSERT usage.
    analysis_id = await finalize_with_insert(
        session,
        tender_id=tender_id,
        profile_id=profile_id,  # per-profile, NOT NULL
        tenant_id=tenant_id,
        level="checklist",
        result=result,
    )
    if analysis_id is None:
        logger.error("chk: finalize returned None — we display without saving")

    # i. Rendering.
    return ChecklistResult(
        status="success", html=_render_checklist(title, result.output, locale)
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


# The order of the categories for rendering.
_CAT_ORDER = ["квалификация", "финансы", "техника", "юридич.", "прочее"]
_CAT_ICONS = {
    "квалификация": "📜",
    "финансы": "💰",
    "техника": "⚙️",
    "юридич.": "⚖️",
    "прочее": "📄",
}


# The category key (the enum value of the model, in Russian) → the key of the label catalog.
_CAT_LABEL_KEYS = {
    "квалификация": "cl_cat_qualification",
    "финансы": "cl_cat_finance",
    "техника": "cl_cat_tech",
    "юридич.": "cl_cat_legal",
    "прочее": "cl_cat_other",
}


def _render_checklist(title: str, output: dict, locale: str | None = None) -> str:
    """Formats ChecklistOutput → an HTML string for Telegram (parse_mode=HTML)."""
    documents = output.get("documents") or []
    deadlines = output.get("deadlines") or []
    risks = output.get("disqualification_risks") or []
    summary_text = output.get("summary") or ""

    parts: list[str] = [
        f"📋 <b>{bot_t(locale, 'cl_title')}</b>\n<b>{html.escape(title or '—')}</b>",
        # Under the heading, not in the tail: the trim below cuts the tail of a long checklist.
        f"⚠️ <i>{bot_t(locale, 'ai_disclaimer')}</i>",
    ]

    # Grouping of the documents by category.
    categories: dict[str, list[dict]] = {}
    for doc in documents:
        cat = str(doc.get("category") or "прочее")
        categories.setdefault(cat, []).append(doc)

    # First in the established order, then the rest.
    all_cats = _CAT_ORDER + [c for c in categories if c not in _CAT_ORDER]
    for cat in all_cats:
        items = categories.get(cat)
        if not items:
            continue
        icon = _CAT_ICONS.get(cat, "📄")
        cat_key = _CAT_LABEL_KEYS.get(cat)
        cat_label = bot_t(locale, cat_key) if cat_key else cat.capitalize()
        lines = [f"{icon} <b>{html.escape(cat_label)}</b>"]
        for item in items:
            mandatory = bool(item.get("mandatory", True))
            item_title = html.escape(str(item.get("title") or ""))
            note = item.get("note") or ""
            checkbox = "⬜" if mandatory else "◽"
            line = f"  {checkbox} {item_title}"
            if note:
                line += f"\n    <i>{html.escape(str(note))}</i>"
            lines.append(line)
        parts.append("\n".join(lines))

    if deadlines:
        lines = [f"⏰ <b>{bot_t(locale, 'cl_deadlines')}</b>"]
        for d in deadlines:
            label = html.escape(str(d.get("label") or ""))
            value = html.escape(str(d.get("value") or ""))
            lines.append(f"  • {label}: {value}")
        parts.append("\n".join(lines))

    if risks:
        lines = [f"🚫 <b>{bot_t(locale, 'cl_risks')}</b>"]
        for r in risks:
            lines.append(f"  • {html.escape(str(r))}")
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
