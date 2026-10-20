"""On-demand L3 deep analysis (§14.4) — the service for the Telegram button “🧠 Deep analysis”.

Called from :func:`src.bots.telegram.on_deep_analysis` when the
callback ``ai:deep:{tender_id}`` is pressed.

Location: ``src/bots/deep_analysis_service.py`` — the rationale:
the service encapsulates the bot-specific logic (resolving chat_id → tenant_id through
the status service, rendering the L3 analysis into HTML for Telegram). It is not the AI layer
(``src/ai/``) nor a worker (``src/workers/``), but the service layer of the bot. An analogue of
``src/bots/status_service.py``.

The on-demand deep is cached as SHARED per tender (profile_id=NULL, as L1), because:
- the deep analysis does not depend on a specific profile at MVP (extra_context/focus are not
  mixed in — the general tender analysis);
- one model call (expensive: opus) covers everyone who wants this tender;
- a cache by profile_id would multiply N records for N Pro+ users with an identical
  prompt, which is wasteful.

Pro+ gate: deep is available only if _TIER_LIMITS[tier]['deep'] != 0
(pro/business/enterprise). It is checked when the button is pressed, not when the
card is formed.

Limit §14.5: before the model call we check ai_usage for the period YYYY-MM.
Tenant limit of deep: pro=10, business=50, enterprise=None (no limit).

The Telegram limit of 4096 characters: the L3 analysis may be long; we send it in several
chunks (pagination _TG_MAX_CHARS). DeepAnalysisResult carries html_chunks (tuple[str, ...]);
the handler on_deep_analysis sends them in a loop.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from src.ai import AiClient, AiRetryableError, AnalysisRequest
from src.ai.usage import (
    _TIER_LIMITS,
    cache_lookup,
    finalize_with_insert,
    limit_decision,
    record_error_spend,
)
from src.bots.i18n import bot_t
from src.bots.tg_html import split_tg_html
from src.bots.status_service import resolve_tenant_id
from src.domain.access import is_frozen
from src.domain.tiers import effective_tier

logger = logging.getLogger(__name__)

# The maximum number of characters of one Telegram message (used by the pagination _paginate_l3).
_TG_MAX_CHARS = 4096

# SQL queries for loading the tender and the tenant data.
_LOAD_TENDER_SQL = text("""
    SELECT tn.title, tn.description, c.name AS country_name
    FROM tenders_normalized tn
    JOIN tenants t ON t.id = :tenant_id
    LEFT JOIN countries c ON c.code = t.country_code
    WHERE tn.id = :tender_id
    LIMIT 1
    """)

_LOAD_TENANT_TIER_SQL = text("""
    SELECT tier, access_until, status FROM tenants WHERE id = :tenant_id
    """)


@dataclass(frozen=True)
class DeepAnalysisResult:
    """The result of the on-demand deep analysis for rendering in Telegram."""

    status: str  # 'success' | 'no_account' | 'gate_pro' | 'limit_exhausted'
    # | 'model_busy' | 'model_error' | 'no_tender' | 'frozen'
    html_chunks: tuple[
        str, ...
    ] = ()  # ready HTML chunks for cyclic sending on success


# The default AI client (lazy singleton for on-demand).
_DEFAULT_AI_CLIENT: AiClient | None = None


def _get_default_ai_client() -> AiClient:
    global _DEFAULT_AI_CLIENT
    if _DEFAULT_AI_CLIENT is None:
        _DEFAULT_AI_CLIENT = AiClient()
    return _DEFAULT_AI_CLIENT


async def run_deep_analysis(
    session: Any,
    *,
    tender_id: str,
    chat_id: int,
    ai_client: AiClient | None = None,
    tier_limits: dict[str, dict[str, int | None]] | None = None,
    locale: str | None = None,
) -> DeepAnalysisResult:
    """Runs the on-demand L3 deep analysis for a tender at the request from the Telegram chat.

    Parameters:
        session: AsyncSession (already open from session_scope).
        tender_id: the tender UUID from the callback.
        chat_id: the Telegram chat_id for resolving tenant_id.
        ai_client: injection for tests; by default the global AiClient().
        tier_limits: injection for tests; by default _TIER_LIMITS.

    Steps:
        a. resolve_tenant_id(chat_id) → no binding → no_account.
        b. Freeze (access_until expired) → frozen; then resolve the tier → the Pro+ gate.
        c. Cache lookup (profile_id=NULL, level='deep').
        d. The §14.5 limit.
        e. Loading the tender + country_name.
        f. AiClient.analyze(level='deep').
        g. finalize (INSERT + UPSERT usage).
        h. Rendering L3Output → HTML chunks.
    """
    limits = tier_limits if tier_limits is not None else _TIER_LIMITS
    client = ai_client or _get_default_ai_client()

    # a. Resolve tenant_id by chat_id.
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return DeepAnalysisResult(status="no_account")

    # b. Plan, freeze and the Pro+ gate.
    row = (
        await session.execute(_LOAD_TENANT_TIER_SQL, {"tenant_id": tenant_id})
    ).first()
    # Freeze (FIX-SG-04): access expired → refusal BEFORE the tier gate, the cache, the limit and
    # BEFORE the opus call. The SSOT predicate is_frozen (src/domain/access.py).
    access_until = getattr(row, "access_until", None) if row is not None else None
    if is_frozen(access_until, datetime.now(timezone.utc)):
        return DeepAnalysisResult(status="frozen")
    # The effective tier: trial → the rights of Pro (ARCHITECTURE §4), AFTER the frozen gate.
    status_val = str(row.status) if row and getattr(row, "status", None) else None
    raw_tier = str(row.tier) if row and row.tier else None
    tier = effective_tier(status_val, raw_tier)
    tier_row = limits.get(tier, {})
    deep_limit = tier_row.get("deep", 0)
    if deep_limit == 0:
        return DeepAnalysisResult(status="gate_pro")

    # c. Cache (profile_id=NULL — shared per tender).
    cached_id = await cache_lookup(session, tender_id, None, "deep")
    if cached_id is not None:
        # Cache hit: we render without a model call.
        tender_row = (
            await session.execute(
                _LOAD_TENDER_SQL, {"tender_id": tender_id, "tenant_id": tenant_id}
            )
        ).first()
        tender_title = str(tender_row.title) if tender_row and tender_row.title else ""
        # We load the output from the cache for rendering.
        cached_output = await _load_cached_output(session, cached_id)
        if cached_output is not None:
            html_chunks = tuple(_render_l3_chunks(tender_title, cached_output, locale))
            logger.info(
                "deep: cache hit analysis=%s tender=%s chat=%s",
                cached_id,
                tender_id,
                chat_id,
            )
            return DeepAnalysisResult(status="success", html_chunks=html_chunks)

    # d. The §14.5 limit — we check BEFORE the model call.
    decision = await limit_decision(
        session,
        tenant_id=tenant_id,
        tier=tier,
        level="deep",
        tier_limits=limits,
    )
    if decision == "exhausted":
        return DeepAnalysisResult(status="limit_exhausted")
    # 'unavailable' is already cut off at step b, but just in case:
    if decision != "ok":
        return DeepAnalysisResult(status="gate_pro")

    # e. Loading the tender + country_name.
    tender_row = (
        await session.execute(
            _LOAD_TENDER_SQL, {"tender_id": tender_id, "tenant_id": tenant_id}
        )
    ).first()
    if tender_row is None:
        return DeepAnalysisResult(status="no_tender")
    title = str(tender_row.title) if tender_row.title else ""
    description = str(tender_row.description) if tender_row.description else ""
    country_name = str(tender_row.country_name) if tender_row.country_name else "Сербия"

    # f. The model call (expensive, opus, outside a transaction by time).
    request = AnalysisRequest(
        level="deep",
        title=title,
        description=description,
        country_name=country_name,
        target_language="ru",  # at MVP shared — the profile language is not available without profile_id
    )
    try:
        result = await client.analyze(request)
    except AiRetryableError:
        logger.warning("deep: model busy tender=%s", tender_id)
        return DeepAnalysisResult(status="model_busy")
    except Exception as exc:  # noqa: BLE001 — an invalid answer or another failure
        logger.exception("deep: analysis error tender=%s", tender_id)
        await record_error_spend(session, tenant_id=tenant_id, level="deep", exc=exc)
        return DeepAnalysisResult(status="model_error")

    # g. INSERT tender_ai_analyses + UPSERT ai_usage.
    analysis_id = await finalize_with_insert(
        session,
        tender_id=tender_id,
        profile_id=None,  # shared per tender
        tenant_id=tenant_id,
        level="deep",
        result=result,
    )
    if analysis_id is None:
        # IntegrityError without a found cache — an edge case, we do not block the display.
        logger.error("deep: finalize returned None — we display without saving")

    # h. Rendering.
    html_chunks = tuple(_render_l3_chunks(title, result.output, locale))
    return DeepAnalysisResult(status="success", html_chunks=html_chunks)


# --------------------------------------------------------------------------- #
# Helper SQL/rendering
# --------------------------------------------------------------------------- #

_LOAD_CACHED_OUTPUT_SQL = text("""
    SELECT output FROM tender_ai_analyses WHERE id = :id LIMIT 1
    """)


async def _load_cached_output(session: Any, analysis_id: str) -> dict | None:
    """Loads the JSONB output from the cache tender_ai_analyses."""
    row = (await session.execute(_LOAD_CACHED_OUTPUT_SQL, {"id": analysis_id})).first()
    if row is None:
        return None
    out = row.output
    if isinstance(out, dict):
        return out
    import json

    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


def _paginate_l3(parts: list[str], max_chars: int = _TG_MAX_CHARS) -> list[str]:
    """Packs the sections of the L3 analysis into Telegram messages ≤ max_chars characters.

    The sections are joined by ``\\n\\n``; the splitting is by the common helper
    :func:`src.bots.tg_html.split_tg_html` (the same as for the verdict, the checklist and
    the draft): by paragraph, then by line, then by space, the tags are not broken.
    Returns a list of ≥ 1 string (an empty output → ['']).
    """
    return split_tg_html("\n\n".join(parts), max_chars=max_chars)


def _render_l3_chunks(title: str, output: dict, locale: str | None = None) -> list[str]:
    """Formats L3Output → a list of HTML chunks for Telegram (parse_mode=HTML).

    Each chunk ≤ _TG_MAX_CHARS characters (pagination via _paginate_l3).
    The section logic is kept 1:1 relative to the former _render_l3_html.
    """

    def t(key: str) -> str:
        return bot_t(locale, key)

    parts: list[str] = []

    # Heading
    parts.append(f"🧠 <b>{t('da_title')}</b>\n<b>{html.escape(title or '—')}</b>")
    # Under the heading, not in the tail: a long analysis is split into messages, the caveat — in the first one.
    parts.append(f"⚠️ <i>{t('ai_disclaimer')}</i>")

    # summary
    summary = output.get("summary")
    if summary:
        parts.append(html.escape(str(summary)))

    # match_score
    score = output.get("match_score")
    if score is not None:
        stars = "⭐" * max(0, min(5, int(score)))
        parts.append(f"📊 <b>{t('da_relevance')}</b> {stars} ({score}/5)")

    # match_reasons
    reasons = output.get("match_reasons") or []
    if reasons:
        items = "\n".join(f"  • {html.escape(str(r))}" for r in reasons)
        parts.append(f"✅ <b>{t('da_pros')}</b>\n{items}")

    # red_flags
    flags = output.get("red_flags") or []
    if flags:
        items = "\n".join(f"  • {html.escape(str(f))}" for f in flags)
        parts.append(f"⚠️ <b>{t('da_risks')}</b>\n{items}")

    # required_qualifications
    quals = output.get("required_qualifications") or []
    if quals:
        items = "\n".join(f"  • {html.escape(str(q))}" for q in quals)
        parts.append(f"🎓 <b>{t('da_quals')}</b>\n{items}")

    # estimated_effort_days
    effort = output.get("estimated_effort_days")
    if effort is not None:
        parts.append(f"⏱ <b>{t('da_effort')}</b> {t('da_effort_val').format(n=effort)}")

    # competition_estimate
    comp = output.get("competition_estimate")
    if comp:
        _comp_labels = {
            "low": t("da_comp_low"),
            "medium": t("da_comp_medium"),
            "high": t("da_comp_high"),
        }
        parts.append(
            f"🏆 <b>{t('da_comp')}</b> {_comp_labels.get(comp, html.escape(comp))}"
        )

    # work_breakdown
    breakdown = output.get("work_breakdown") or []
    if breakdown:
        items = "\n".join(
            f"  {i + 1}. {html.escape(str(s))}" for i, s in enumerate(breakdown)
        )
        parts.append(f"📋 <b>{t('da_breakdown')}</b>\n{items}")

    # participation_checklist
    checklist = output.get("participation_checklist") or []
    if checklist:
        items = "\n".join(f"  ☐ {html.escape(str(s))}" for s in checklist)
        parts.append(f"📝 <b>{t('da_checklist')}</b>\n{items}")

    # focus_answers
    focus = output.get("focus_answers") or []
    if focus:
        fa_parts: list[str] = []
        for fa in focus:
            q = str(fa.get("question", "")).strip()
            a = str(fa.get("answer", "")).strip()
            if q:
                fa_parts.append(f"  ❓ {html.escape(q)}\n  💬 {html.escape(a or '—')}")
        if fa_parts:
            parts.append(f"🔍 <b>{t('da_focus')}</b>\n" + "\n\n".join(fa_parts))

    return _paginate_l3(parts)
