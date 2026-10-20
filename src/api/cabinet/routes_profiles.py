"""Cabinet profile routes (ARCHITECTURE §12).

Full CRUD with the plan gate and RLS — the logic is in
:mod:`src.api.cabinet.profile_service`. All routes require a session; an anonymous user
is redirected to /login. The dashboard also shows the status of the Telegram channel binding
(C-04, Chat 26) — the “Connect Telegram” / “already connected” button.
"""

from __future__ import annotations

import dataclasses
import math
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import text

from src.api import deps, repo, security, tiers
from src.api.cabinet import ai_limits_service
from src.api.cabinet import channel_service as channel_svc
from src.api.cabinet import customer_suggest
from src.api.cabinet import feed_service
from src.api.cabinet import market_service
from src.api.cabinet import preview_service
from src.api.cabinet import profile_service as svc
from src.api.cabinet import routes_account
from src.api.cabinet import routes_library
from src.api.cabinet.source_choices import source_choice_rows
from src.api.cabinet import source_banner
from src.api.cabinet import stats_service
from src.api.cabinet import subscription_service
from src.api.cabinet import template_library_service as lib_svc
from src.api.cabinet import template_prefill
from src.api.cabinet import trial_service
from src.api.cabinet import vertical_packages
from src.api.cabinet import workflow_service
from src.api.cabinet.i18n import CABINET, cabinet_context, resolve_authed_lang
from src.api.config import CabinetSettings
from src.api.forms import (
    AVAILABLE_CHANNELS,
    classifications_to_textarea,
    groups_to_textarea,
    parse_profile_form,
)
from src.api.pager import page_param
from src.api.templating import templates
from src.api.public import news
from src.api.public.portal_filters import parse_filters
from src.domain.regions import OKRUG_CHOICES, okrug_label
from src.domain.text_norm import sr_display
from src.domain.sources import (
    default_source_keys_for_new_profile,
    list_sources_for_selection,
    source_label_of,
    validate_source_keys,
)

router = APIRouter(tags=["cabinet"])

# Refusal to enable a template over the plan limit: a flag in the query, the banner — on the dashboard
# or in the cabinet (see ``_safe_next`` below). A query rather than a DB write, following the pattern of
# ``?no_channels=1``: the signal lives for one display (it is the response to an ACTION), while the
# lasting state “X of Y in use” already stands in the subscription block above the
# banner. The constants are in profile_service (svc), the shared point for routes_account,
# which reads the same flag for the banner in the cabinet.
PROFILE_LIMIT_FLAG = svc.PROFILE_LIMIT_FLAG
PROFILE_LIMIT_VALUE = svc.PROFILE_LIMIT_VALUE

# A return to where “Disable” / “Enable” was pressed (the hidden field ``next``):
# an allow-list, not an arbitrary path — otherwise an open redirect would accept
# ``next=https://evil`` verbatim into ``Location``. Anything not in the list
# (including no field at all) returns to the dashboard — the previous behavior.
_ALLOWED_NEXT_PATHS = frozenset({"/app", "/cabinet/account"})


def _safe_next(next_param: str | None) -> str:
    return next_param if next_param in _ALLOWED_NEXT_PATHS else "/app"


# The stream pulse on the dashboard: how many matches came in over the last N days.
# A client reads silence as a breakage — it is a recurring complaint, so the figure
# stands in plain view, even when it is zero.
PULSE_WINDOW_DAYS = 7

# The ROKOVI agenda windows (7 / 30 day tabs) and the number of news items in the Vesti block.
AGENDA_DAYS_SHORT = 7
AGENDA_DAYS_LONG = 30
NEWS_PREVIEW_LIMIT = 3
# The «Tržište» tab of the Vesti block: ?vesti=trziste (no JS — CSP; like ?agenda=30).
VESTI_TAB_MARKET = "trziste"

# ‼️ A thin wrapper OVER the feed, not a second source of truth: the set of rows is
# a verbatim mirror of ``feed_service._FEED_COUNT_SQL`` (the same JOINs, the same dedup by
# (tender_id, profile_id), BOTH constant predicates of the feed and the set of
# statuses ``FEED_VISIBLE_STATUSES``), plus one window by the match moment. The predicates are taken FROM feed_service,
# not copied as text: if they diverged, the dashboard would say “12 came in”, while the feed would show
# a different number. feed_service itself is not touched (a parallel branch).
# A consequence to know: ``_CURRENT_ONLY_PREDICATE`` does not count tenders
# with an expired deadline — “N came in” means exactly "N of them are still current and
# are in the feed“, not ”N emails were sent".
_PULSE_SQL = text(f"""
    SELECT COUNT(*) FROM (
        SELECT DISTINCT o.tender_id, o.profile_id
        FROM delivery_outbox o
        JOIN tenders_normalized tn ON tn.id = o.tender_id
        JOIN profiles p ON p.id = o.profile_id
        WHERE p.tenant_id = CAST(:tid AS uuid)
          AND {feed_service._STATUS_IN}
          AND {feed_service._EVENT_TS} >= now() - CAST(:days || ' days' AS interval)
          AND {feed_service._CURRENT_ONLY_PREDICATE}
          AND {feed_service._PROFILE_FILTER_PREDICATE}
    ) c
""")


# A new template (younger than this term) without deliveries: routing takes only the tenders
# that came in AFTER the template was created (NOTIFY), so “no deliveries” here is not
# a breakage but an expectation. Instead of zero we show how many matching ones are already open.
NEW_TEMPLATE_DAYS = 7


async def _open_now_hints(
    db, profiles, delivered: dict[str, int], *, now: datetime
) -> dict[str, int]:
    """profile_id → how many matching ones are already open, for new active templates without deliveries.

    Read-only: ``delivery_outbox`` and matching are not touched. It is counted in ONE batch by :func:`preview_service.preview_counts` (a loop over the templates costs 19.7 s per view, see ``library_counts``). A zero preview does not get into the result: then the usual text “no deliveries yet” remains.
    """
    cutoff = now - timedelta(days=NEW_TEMPLATE_DAYS)
    field = preview_service.profile_field
    fresh = [
        p
        for p in profiles
        if field(p, "is_active")
        and not delivered.get(str(field(p, "id")))
        and field(p, "created_at") is not None
        and field(p, "created_at") >= cutoff
    ]
    if not fresh:
        return {}
    counts = await preview_service.preview_counts(
        db, [preview_service.payload_from_profile(p) for p in fresh]
    )
    return {str(field(p, "id")): n for p, n in zip(fresh, counts) if n > 0}


async def _flow_pulse(db, tenant_id: str, *, days: int = PULSE_WINDOW_DAYS) -> int:
    """How many matches came to the tenant over the last ``days`` days.

    RLS: called AFTER ``repo.set_tenant`` in the same transaction (like the other dashboard reads); ``p.tenant_id`` in the WHERE is the second isolation axis. ``:profile_id`` is always bound (NULL = all profiles) — the contract of the feed predicate reused here. ``:actual_only`` is bound too (#536 made the actuality predicate a parameter): ``True``, as for the dashboard teaser — the pulse counts what lies in the working feed. ‼️ Without this bind the query fails on the live DB (``A value is required for bind parameter 'actual_only'``), while the mocks do not see it — the guard is ``test_flow_pulse_binds_every_sql_parameter``.
    """
    total = (
        await db.execute(
            _PULSE_SQL,
            {
                "tid": str(tenant_id),
                "days": str(days),
                "profile_id": None,
                "actual_only": True,
            },
        )
    ).scalar_one()
    return int(total or 0)


# SITE-14: the region filter is switched on. The blocker “no NUTS→district mapping” (Q-29) is lifted
# (#364): ingest resolves the district by the municipality name, and tenders_normalized.regions
# is filled. The list of districts comes from the domain reference, and NOT a copy here: the same
# list is read by the bot, and the codes must match what ingest emits (otherwise
# the form would offer a district that does not occur in the data → 0 matches silently).
# A profile with no regions selected still matches all — the default “all regions”.
REGION_CHOICES: list[tuple[str, str]] = list(OKRUG_CHOICES)
# The form’s sources come from the `sources` registry (SRC-0a, R1): the enabled sources,
# the label = `sources.display_name`. See :func:`_source_choices`.
# SITE-15: the delivery channels are limited to those that really work at MVP. Viber / Web
# (stubs without delivery) are removed from the form, so as not to mislead the user. Email is a reserve for post-release channels;
# the main MVP channel —
# Telegram (ARCHITECTURE §9).
# We show exactly what the form parser accepts (:data:`forms.AVAILABLE_CHANNELS`):
# here only labels and order. Otherwise the axes drift apart — the form already offered
# a channel that validation silently dropped, and vice versa (E2).
_CHANNEL_LABELS = [
    ("email", "Email"),
    ("telegram", "Telegram"),
    ("viber", "Viber"),
    ("web", "Веб"),
]
CHANNEL_CHOICES = [
    (value, label) for value, label in _CHANNEL_LABELS if value in AVAILABLE_CHANNELS
]
# (value, the i18n key of the label). The template renders the label via `t[key]` by the locale
# of the interface — a Russian literal no longer gets into the form (Phase 1.4b). The values
# (the left element) are the form’s contract; they must not be changed.
# ‼️ There are TWO levels, not four (15.08.2026, owner’s decision): auto-parsing of the stream —
# this is a paid call for EVERY delivered tender, and the product allows for it
# exactly one mode (``src.domain.tiers.AUTO_ANALYSIS_LEVELS``). ``quick`` gives
# service metadata that no template renders; ``deep`` in the auto path
# is forbidden by the safety guard and lives only behind the button in the card. Offering in the
# form what the worker will not run is lying to the subscriber.
AI_LEVEL_CHOICES = [
    ("none", "ai_lvl_none"),
    ("standard", "ai_lvl_standard"),
]
SCHEDULE_CHOICES = [
    ("immediate", "sched_immediate"),
    ("digest_morning", "sched_morning"),
    ("digest_evening", "sched_evening"),
    ("digest_custom", "sched_custom"),
]


def _redirect_login(request: Request | None = None) -> RedirectResponse:
    """For login; on the GET page — with a return here (``deps.cabinet_login_url``)."""
    url = deps.cabinet_login_url(request) if request is not None else "/login"
    return RedirectResponse(url=url, status_code=303)


def _auto_name_prefix(request: Request) -> str:
    """The localised prefix of the profile’s auto-name (the name is optional, variant A).

    The service does not know the locale (the two i18n axes), so the route resolves the prefix by the interface locale and passes it to :func:`profile_service.create_profile`.
    """
    lang = resolve_authed_lang(request, None)
    return CABINET[lang]["pf_name_auto"]


def _gate_flags(tier: str) -> dict:
    """Availability flags of the gated fields for reflecting in the form (presentation only).

    They decide nothing — the gate is enforced on the backend (:func:`tiers.gate_profile`); here are only derivatives of the SSOT resolver, to mark the unavailable fields with the note “available on Pro” + a CTA to /plans, rather than breaking or silently hiding them.
    """
    return {
        "tier": tier,
        "gate_starter": tiers.at_least(tier, "starter"),
        "gate_pro": tiers.at_least(tier, "pro"),
        "gate_lite": tiers.normalize_tier(tier) == "lite",
    }


async def _gate_ctx(session_factory: Callable, tenant_id: str) -> dict:
    """Gate flags + the remaining auto-analysis limit for the form (read-only).

    The remainder is shown BEFORE exhaustion, not only at the moment of refusal: the owner’s requirement “nothing must switch off silently”. It is read by the same predicate as the worker’s gate (``src.ai.usage.usage_snapshot`` → ``used_count``).
    """
    async with session_factory() as db:
        # RLS: set_tenant FIRST — ai_usage / tenants are under FORCE RLS, the web runs
        # under the NOBYPASSRLS role and without the GUC would get zeros.
        await repo.set_tenant(db, tenant_id)
        tier = await svc.get_tenant_tier(db, tenant_id)
        limits = await ai_limits_service.get_ai_limits(db, tid=tenant_id)
    return {**_gate_flags(tier), "ai_auto_usage": limits.auto}


async def _vesti_market(session_factory: Callable, tid: str, vesti_tab: str):
    """Data of the “Tržište” tab of the Vesti block; ``None`` on the TenderSRB tab.

    The shared calculation for /app and the /app/vesti partial (one truth for two entries). The personal row is its own transaction with ``set_tenant`` first: the partial does not build the dashboard, so it has no /app transaction.
    """
    if vesti_tab != VESTI_TAB_MARKET:
        return None
    async with session_factory() as db:
        await repo.set_tenant(db, tid)
        personal = await market_service.get_personal(db, tid)
    return market_service.build_view(
        await market_service.get_shared(session_factory, deps.get_redis()),
        personal,
        today=market_service.local_today(),
    )


def _login_url_for(target: str) -> str:
    """Login with a return to ``target`` (a relative path; the same kind as deps)."""
    return f"/login?next={quote(target, safe='')}"


@router.get("/app/vesti", response_class=HTMLResponse)
async def vesti_block(
    request: Request,
    vesti: str = "",
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Only the Vesti block (``_vesti.html``) — switching the tab without a reload.

    Calls htmx from the block’s tabs (``hx-get``, outerHTML swap by ``#vesti``). The gate is as for /app; GET without mutations. Anonymous: an ordinary request → 303 to login (as /app); an htmx request → ``HX-Redirect``, otherwise XHR would rewrite the login page inside the block.
    """
    vesti_tab = VESTI_TAB_MARKET if vesti == VESTI_TAB_MARKET else "news"
    if session is None:
        # The return goes to the parent block’s page, not to the fragment itself: after login
        # a bare block without a header would be a dead end.
        login = _login_url_for(
            "/app?vesti=trziste" if vesti_tab == VESTI_TAB_MARKET else "/app"
        )
        if request.headers.get("HX-Request"):
            response = Response(status_code=200)
            response.headers["HX-Redirect"] = login
            return response
        return RedirectResponse(url=login, status_code=303)
    return templates.TemplateResponse(
        request,
        "_vesti.html",
        {
            "vesti_tab": vesti_tab,
            # autofocus on the active tab — only in the response to a switch:
            # on the full /app it would pull the focus and scroll to the block on load.
            "vesti_focus": True,
            "market": await _vesti_market(session_factory, session["tid"], vesti_tab),
            "news_latest": news.NEWS[:NEWS_PREVIEW_LIMIT],
        },
    )


@router.get("/app", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    agenda: str = "",
    vesti: str = "",
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """The subscriber’s cockpit: “my week” + deadline calendar + what needs action.

    The dashboard answers “what is burning for me”, not “which tenders exist”: the catalog of matches is at ``/app/feed``, here only a teaser of the latest deliveries.
    Secondary things (profiles, channels, source statistics) are below and compact.
    RLS: ``set_tenant`` as the first action of the transaction; all reads are services that repeat it themselves (double isolation).
    """
    if session is None:
        return _redirect_login(request)
    tid = session["tid"]
    agenda_days = (
        AGENDA_DAYS_LONG if agenda == str(AGENDA_DAYS_LONG) else AGENDA_DAYS_SHORT
    )
    vesti_tab = VESTI_TAB_MARKET if vesti == VESTI_TAB_MARKET else "news"
    async with session_factory() as db:
        await repo.set_tenant(db, tid)
        profiles = await svc.list_profiles(db, tid)
        notices = {}
        for p in profiles:
            sup = await svc.downgrade_notice(db, tid, p)
            if sup:
                notices[str(p.id)] = sup
        telegram_linked = await channel_svc.has_active_telegram(db, session["uid"])
        # A channel was connected, but now it is dead → tenders silently do not arrive.
        # The banner (unlike the onboarding checklist) says this directly.
        dead_channels = await channel_svc.dead_channels(db, tid)
        # “Setting ≠ fact”: the profile asks for a channel for which there is no live
        # addressee (email not confirmed / channel not connected) → delivery would go
        # nowhere. has_live_channel closes the onboarding step "connect
        # a channel", gaps — a quiet nudge in the cabinet. One SELECT of the states for both (a mirror of
        # the delivery selection).
        delivery = await channel_svc.delivery_channel_status(db, tid, profiles)
        access_status = await trial_service.get_access_status(db, tid)
        tenant_stats = await stats_service.get_tenant_delivery_stats(db, tid)
        open_now_hints = await _open_now_hints(
            db,
            profiles,
            {str(d.profile_id): d.count for d in tenant_stats.by_profile},
            now=datetime.now(timezone.utc),
        )
        access_days = await stats_service.get_days_left(db, tid)
        # The cockpit calendar: deadlines on a 30-day horizon (the same predicate as
        # the burning deadlines, only the window is wider) + the funnel of “my tenders” +
        # the subscription end date. The calendar has no SQL of its own — the layout by
        # dates is done by pure functions of feed_service, so there are no extra queries on
        # the dashboard beyond the number of sources. The “do now” block
        # is a DERIVATIVE of the same events (`urgent_events`), not a second query:
        # the calendar and “needs action” must not diverge.
        # “Collecting from N platforms” — the enabled sources, the same set as on /izvori.
        # “Your sources: K of N” — K by the tenant’s templates, N — all enabled
        # sources (the same set as the banner below and /izvori).
        source_entries = await source_banner.load_enabled_sources(db)
        sources_used, sources_count = source_banner.coverage(
            profiles, [s.source_key for s, _ in source_entries]
        )
        # The banner “enable a source in all templates”: the most recent source
        # that is missing from at least one template and was not dismissed with “not now”.
        sources_banner = await source_banner.dashboard_banner(
            db,
            profiles,
            source_banner.parse_seen(request.cookies.get(source_banner.SEEN_COOKIE)),
            entries=source_entries,
        )
        calendar_deadlines = await feed_service.get_calendar_deadlines(db, tid)
        my_tenders = await workflow_service.get_my_tenders(db, tid)
        work_summary = feed_service.build_work_summary(my_tenders.funnel)
        # The subscription term is NOT put into the calendar (owner 13.10, dashboard v2): it already
        # stands as a header badge, and at ≤7 days / expired it is a call-to-action strip; a fifth
        # repetition of one fact was eating the agenda row and the grid dot.
        calendar_events = feed_service.build_calendar_events(
            deadlines=calendar_deadlines,
            my_tenders=my_tenders.funnel,
            access_until=None,
        )
        calendar_month = feed_service.build_calendar_month(calendar_events)
        urgent = feed_service.urgent_events(calendar_events)
        # Tile count — a separate COUNT with the same predicate, without a limit.
        urgent_count = await feed_service.count_urgent_deadlines(
            db, tid, my_tenders.funnel
        )
        # ROKOVI — ONE agenda with a 7/30-day window switch (instead of the calendar’s agenda
        # + “Do now” + the teaser tabs: three times the same
        # rows). The same set as the month grid; the window is only for display.
        agenda_events = calendar_events if agenda_days == AGENDA_DAYS_LONG else urgent
        # The dashboard is a cockpit, not a catalog: the full feed lives at /app/feed, here a
        # short list of the latest deliveries. Tenders already standing in the visible
        # part of ROKOVI are removed from it (one tender — one place on the screen),
        # so we take a margin and cut to the limit.
        shown_ids = {
            e.tender_id
            for e in agenda_events[: feed_service.AGENDA_PREVIEW_LIMIT]
            if e.tender_id
        }
        feed_items = [
            i
            for i in await feed_service.get_feed_items(
                db,
                tid,
                limit=feed_service.DASHBOARD_TEASER_LIMIT
                + feed_service.AGENDA_PREVIEW_LIMIT,
                sort="date",
            )
            if str(i.tender_id) not in shown_ids
        ][: feed_service.DASHBOARD_TEASER_LIMIT]
        # The honest banner “hidden by the district filter”: if the profile narrowed the district, some of
        # the matching tenders (without a resolved district) silently did not get into the feed.
        # Counted ONLY with a non-empty regions, reusing the preview logic.
        region_hidden = await feed_service.get_region_hidden_notice(db, tid, profiles)
        # The “subscription and limit” block on the dashboard — the SAME data and the same method
        # as on /cabinet/account (subscription_service + resolve_plan_key +
        # tiers.profile_limit from the EFFECTIVE plan): the figure “X of Y in use” and
        # the plan name must match on the two screens, otherwise the client sees
        # a mismatch. "In use" = all non-deleted templates (the CREATION limit axis,
        # len(profiles)), not only the enabled ones: that is exactly what
        # create_profile checks and exactly what the cabinet shows.
        flow_pulse = await _flow_pulse(db, tid)
        subscription = await subscription_service.get_subscription(db, tid)
        tenant_tier, tenant_status = await subscription_service.get_tenant_tier(db, tid)
        is_trial = (tenant_status or "").strip().lower() == "trial"
        plan_key = routes_account.resolve_plan_key(subscription, tenant_tier, is_trial)
        profile_limit = tiers.profile_limit(
            tiers.effective_tier(tenant_status, tenant_tier)
        )
    # Tržište (the Vesti tab) — outside the dashboard transaction: see _vesti_market.
    market = await _vesti_market(session_factory, tid, vesti_tab)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "profiles": profiles,
            "notices": notices,
            # Profiles that will find NOTHING: there is no topic and the “All tenders” mode
            # is not on (0043). Without this banner such a profile is silent, and the client
            # sees only “no deliveries” and does not know the reason — exactly the
            # state from which the whole edit grew. The predicate is one and the same
            # (`matches_anything`), so that the banner does not diverge from the routing.
            "silent_profiles": {
                str(preview_service.profile_field(p, "id"))
                for p in profiles
                if not preview_service.profile_matches_anything(p)
            },
            "telegram_linked": telegram_linked,
            "dead_channels": dead_channels,
            "has_live_channel": delivery["has_live_channel"],
            "live_channels": delivery.get("live_channels", []),
            "delivery_gaps": delivery["gaps"],
            # “Template exists, no deliveries”: no channel at all / the template has none selected.
            "delivery_warning": channel_svc.build_delivery_warning(
                profiles, delivery["has_live_channel"], delivery["channelless"]
            ),
            "access_status": access_status,
            "tenant_stats": tenant_stats,
            "open_now_hints": open_now_hints,
            "access_days": access_days,
            "feed_items": feed_items,
            "agenda_days": agenda_days,
            "agenda_events": agenda_events,
            "urgent_count": urgent_count,
            "news_latest": news.NEWS[:NEWS_PREVIEW_LIMIT],
            "vesti_tab": vesti_tab,
            "market": market,
            "sources_count": sources_count,
            "sources_used": sources_used,
            "sources_banner": sources_banner,
            "work_summary": work_summary,
            "calendar_events": calendar_events,
            "calendar_month": calendar_month,
            "region_hidden": region_hidden,
            "flow_pulse": flow_pulse,
            "pulse_window_days": PULSE_WINDOW_DAYS,
            "plan_key": plan_key,
            "is_trial": is_trial,
            "profile_limit": profile_limit,
            "profile_used": len(profiles),
            # The banner’s denominator “not enabled in M of N”: only the live templates.
            "live_profile_count": len(source_banner.live_profiles(profiles)),
            # The refusal to enable a template over the limit: a one-off signal after the redirect
            # from /profiles/{id}/activate, not a state (the state is "X of
            # M" in the subscription block).
            "profile_limit_notice": (
                request.query_params.get(PROFILE_LIMIT_FLAG) == PROFILE_LIMIT_VALUE
            ),
        },
    )


def _source_banner_login(request: Request) -> Response:
    """No session: an ordinary form — 303 to login; an htmx request — HX-Redirect.

    htmx would follow the 303 and insert the login page inside ``#src-banner``.
    """
    if request.headers.get("HX-Request"):
        return Response(
            status_code=200, headers={"HX-Redirect": deps.cabinet_login_url(request)}
        )
    return _redirect_login(request)


@router.post("/app/izvori/vklyuchit", response_class=HTMLResponse)
async def sources_banner_enable(
    request: Request,
    source_key: str = Form(default=""),
    csrf: str = Form(default=""),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """The banner button “Enable in all templates”: the source — into all live templates.

    Written only on click (CSRF is mandatory; without a token nothing is written). The HTMX request gets the fragment “Enabled in K”; the ordinary form — a redirect to /app.
    """
    if session is None:
        return _source_banner_login(request)
    if not security.verify_csrf(csrf, session, settings.cabinet_secret_key):
        # htmx will insert the response into #src-banner: a 303 to /app would put the whole page inside the
        # block, so for an HX request we return a fragment with the refusal.
        if request.headers.get("HX-Request"):
            return templates.TemplateResponse(
                request,
                "_source_banner_done.html",
                {"done_count": 0, "total_count": 0},
            )
        return RedirectResponse(url="/app", status_code=303)
    async with session_factory() as db:
        total, with_key = await source_banner.enable_everywhere(
            db,
            session["tid"],
            source_key.strip(),
            datetime.now(timezone.utc),
        )
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url="/app", status_code=303)
    return templates.TemplateResponse(
        request,
        "_source_banner_done.html",
        {"done_count": with_key, "total_count": total},
    )


@router.post("/app/izvori/skip")
async def sources_banner_skip(
    request: Request,
    csrf: str = Form(default=""),
    session=Depends(deps.get_optional_session),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """“Not now”: a cookie with the moment of refusal — the banner will return for platforms newer than it."""
    if session is None:
        return _source_banner_login(request)
    if not security.verify_csrf(csrf, session, settings.cabinet_secret_key):
        # The same case as the enabling: an htmx request gets an empty block, not /app.
        if request.headers.get("HX-Request"):
            return Response(status_code=200)
        return RedirectResponse(url="/app", status_code=303)
    response: Response
    if request.headers.get("HX-Request"):
        response = Response(status_code=200)
    else:
        response = RedirectResponse(url="/app", status_code=303)
    response.set_cookie(
        source_banner.SEEN_COOKIE,
        datetime.now(timezone.utc).isoformat(),
        max_age=source_banner.SEEN_COOKIE_MAX_AGE,
        samesite="lax",
        httponly=True,
        path="/",
    )
    return response


# The require_active_access gate — POINT-WISE on the feed, not on the whole router: the feed is
# the same content as the cards and “My tenders” (the other routers are mounted
# in main.py). The gate itself only closes a deleted user and a DB error;
# the freeze does not close the content (owner 10.10), so the `/app` dashboard with the banner
# “access expired” and the profile forms stay open without reservations.
@router.get(
    "/app/feed",
    response_class=HTMLResponse,
    dependencies=[Depends(deps.require_active_access)],
)
async def feed_archive(
    request: Request,
    page: int = Depends(page_param),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """The tenant’s working feed of matches: search, filters, sorting, pagination.

    Parameters (allow-list, unknown silently ignored; parsing and chips are the same :func:`portal_filters.parse_filters` as for ``/tenderi``): ``q`` (title / description / customer), ``customer``, ``region``, ``source`` (only the sources enabled in the tenant’s templates), ``min_amount`` / ``max_amount`` + ``no_amount``, ``published_from`` / ``published_to``, ``deadline_to``, ``profile_id`` (the template), ``actual`` (``0`` — including the expired), ``sort`` ∈ date (default) | deadline | amount_desc | amount_asc | profile. Free text is not substituted into SQL: ORDER BY comes from an allow-list, the values are bind parameters with ``CAST`` (``feed_service.feed_filter_sql``). A foreign ``profile_id`` is discarded by the parser (it is not in the tenant’s template list), and the ``p.tenant_id`` predicate in SQL stands next to the filters, not instead of them. ``total`` and the page are counted by the SAME filters (``get_feed_listing``) under ``statement_timeout``: a timeout — a page with a hint, not a 500.
    ``?lang=`` is not read here: the cabinet language comes from the session, while the query ``lang`` is the content-translation axis (see cabinet_ignore_query_lang).
    RLS: ``set_tenant`` first (inside the service functions) + an explicit WHERE p.tenant_id.
    """
    if session is None:
        return _redirect_login(request)
    tid = session["tid"]
    page_size = feed_service.FEED_PAGE_SIZE
    async with session_factory() as db:
        await repo.set_tenant(db, tid)
        # The tenant’s profiles are needed by both the filter (template) and the district banner — we read
        # them ONCE, the same way as the dashboard, not with two queries.
        profiles = await svc.list_profiles(db, tid)
        tenant_source_keys = {
            k for p in profiles for k in (getattr(p, "enabled_source_keys", None) or ())
        }
        source_labels = {
            s.source_key: source_label_of(s.display_name, s.source_key)
            for s in await list_sources_for_selection(db, tenant_source_keys)
            if s.source_key in tenant_source_keys
        }
        pf = dataclasses.replace(
            parse_filters(
                request.query_params,
                source_keys=source_labels,
                sort_keys=feed_service.FEED_SORT_KEYS,
                default_sort="date",
                profile_ids=frozenset(str(p.id) for p in profiles),
                allow_deadline_to=True,
            ),
            lang="",
        )
        active_profile = next((p for p in profiles if str(p.id) == pf.profile_id), None)
        # The district banner is read BEFORE the list: after a listing timeout the transaction
        # is rolled back, and statement_timeout applies until the end of the transaction.
        region_hidden = await feed_service.get_region_hidden_notice(db, tid, profiles)
        warning = await channel_svc.delivery_warning(db, tid, profiles)
        items, total, timed_out = await feed_service.get_feed_listing(
            db, tid, pf, page=page, page_size=page_size
        )
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        if page > total_pages and not timed_out:
            # Beyond the last page — the last one (as the showcase and the cabinet do), not an empty one.
            page = total_pages
            items, total, timed_out = await feed_service.get_feed_listing(
                db, tid, pf, page=page, page_size=page_size
            )
        # The “By template” group started on the previous page gets a “continued” mark.
        group_carry = None
        if pf.sort == "profile" and page > 1 and not timed_out:
            group_carry = await feed_service.get_group_carry(
                db,
                tid,
                page=page,
                page_size=page_size,
                profile_id=pf.profile_id or None,
                sort=pf.sort,
                actual_only=pf.actual,
                filters=pf,
            )
    # Urgency is a PRESENTATION over the page already obtained: a pure function
    # splits the same list into “burning” / the rest, without touching the WHERE, the ORDER,
    # or the LIMIT/OFFSET window. `items` stays in the context — the template decides by it
    # whether there is something to show or an empty state, and the page counter does not depend on the groups.
    groups = feed_service.split_feed_by_urgency(items)
    return templates.TemplateResponse(
        request,
        "feed_archive.html",
        {
            "items": items,
            "urgent_items": groups.urgent,
            "rest_items": groups.rest,
            "group_carry": group_carry,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "timed_out": timed_out,
            "region_hidden": region_hidden,
            "delivery_warning": warning,
            "profiles": profiles,
            "active_profile": active_profile,
            "pf": pf,
            "sort_keys": [
                k
                for k in feed_service.FEED_SORT_KEYS
                if k != "profile" or len(profiles) > 1
            ],
            "region_options": [(code, okrug_label(code)) for code, _ in OKRUG_CHOICES],
            "source_options": sorted(source_labels.items(), key=lambda kv: kv[1]),
        },
    )


# Below this district coverage we warn: a noticeable share of fresh tenders still without
# a set district, and choosing a district hides them. The threshold is a DISPLAY decision; the
# percentage in the text is taken from the response, not hard-coded (it used to live in cpv_widget.js, where
# the label was nailed down in Russian outside the catalog).
REGION_COVERAGE_WARN_PCT = 90


@router.get("/profiles/preview-count")
async def preview_count(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """GET /profiles/preview-count — how many tenders would match over 30 days (stages A+B).

    Accepts the same query parameters as the profile form (form encoding or JSON body). Anonymous → 401. Returns {count, window_days, unfound, region_coverage_pct, hidden_by_region}. The regional coverage fields are None if no district is selected in the profile.

    ‼️ TWO representations of one response, not two counters: an htmx request of the live form counter (header ``HX-Request``) gets the ready fragment ``_preview_count.html`` with the labels from the cabinet catalog; everything else gets the former JSON (the route’s contract). In both cases the figure comes from one :func:`preview_service.preview_count`.
    """
    if session is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # GET: form data arrives as query params (sent by JS URLSearchParams)
    payload, _ = parse_profile_form(request.query_params)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        # The examples — only to the form fragment: by the same pass as the figure.
        result = await preview_service.preview_count(
            db,
            payload,
            examples_limit=(
                preview_service.FORM_EXAMPLES_LIMIT
                if request.headers.get("hx-request")
                else 0
            ),
        )
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(
            request,
            "_preview_count.html",
            {
                "count": result.count,
                "no_sources": result.no_sources,
                "unfound": result.unfound,
                "region_coverage_pct": result.region_coverage_pct,
                "hidden_by_region": result.hidden_by_region,
                "region_warn": (
                    result.region_coverage_pct is not None
                    and result.region_coverage_pct < REGION_COVERAGE_WARN_PCT
                ),
                "include_forms": result.include_forms,
                "exclude_forms": result.exclude_forms,
                "keyword_dict_enabled": result.keyword_dict_enabled,
                "examples": result.examples,
            },
        )
    # The Serbian forms of the entered words are rendered by the server (with the same partial as in the form) —
    # the labels remain template strings, the terms are auto-escaped by Jinja;
    # JS only inserts the ready fragment. Empty → the front end hides the block.
    forms_html = templates.get_template("_kw_forms.html").render(
        # A render bypassing the context processor: we pass the label catalog ourselves.
        **cabinet_context(request, getattr(request.state, "cabinet_user_locale", None)),
        include_forms=result.include_forms,
        exclude_forms=result.exclude_forms,
        keyword_dict_enabled=result.keyword_dict_enabled,
    )
    return JSONResponse(
        {
            "count": result.count,
            "window_days": 30,
            "unfound": result.unfound,
            "region_coverage_pct": result.region_coverage_pct,
            "hidden_by_region": result.hidden_by_region,
            "forms_html": forms_html,
        }
    )


@router.get("/profiles/customer-suggest")
async def customer_suggest_route(
    request: Request,
    q: str = "",
    profile_id: str | None = None,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """GET /profiles/customer-suggest?q=…[&profile_id=…] — customer suggestions (0046).

    JSON ``{"items": [{"name", "tenders"}]}``: the customers of the profile’s country for the year (:mod:`src.api.cabinet.customer_suggest`). The country is that of the saved profile (RLS: a foreign ``profile_id`` will not be found → the country of the new profile), otherwise the country of the new profile. Anonymous → 401. The names are given as data; the markup is built by ``customer_picker.js`` via ``textContent`` (no HTML from the response).
    """
    if session is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    country = svc.NEW_PROFILE_COUNTRY
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        if profile_id and _is_uuid(profile_id):
            profile = await svc.get_profile(db, session["tid"], profile_id)
            if profile is not None and profile.country_code:
                country = profile.country_code
        hits = await customer_suggest.suggest(db, country, q)
    lang = resolve_authed_lang(
        request, getattr(request.state, "cabinet_user_locale", None)
    )
    # ``name`` — the exact name (the filter value, compared letter by letter);
    # ``label`` — the label for display: sr → Latin (owner 10.10.2026).
    return JSONResponse(
        {
            "items": [
                {
                    "name": h.name,
                    "label": sr_display(h.name, lang),
                    "tenders": h.tenders,
                }
                for h in hits
            ]
        }
    )


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@router.get("/profiles/new", response_class=HTMLResponse)
async def new_profile_form(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    from_template: str | None = None,
    from_package: str | None = None,
    preset: str | None = None,
    manual: str | None = None,
    enabled_source_keys: list[str] = Query(default=[]),
    src_sent: str | None = None,
):
    """The new-profile form; ``from_template`` / ``from_package`` / ``preset=all`` — prefill.

    Without a prefill and without ``manual`` — the first screen “Čime se bavite?” (``profile_start.html``): the showcase package tiles + “Sam ću podesiti” (``?manual=1`` → the wizard). The tile data — :func:`routes_library.showcase`, the same set and figure cache as ``/library``.

    The field values come from :mod:`src.api.cabinet.template_prefill` — the same payload by which the showcase counted the figure on the card. A client who presses “Use template” must see in the preview exactly the figure that was on the card; for this the set of fields must be ONE, not two similar ones.
    """
    if session is None:
        return _redirect_login(request)
    if not (from_template or from_package or preset or manual):
        lang = resolve_authed_lang(
            request, getattr(request.state, "cabinet_user_locale", None)
        )
        async with session_factory() as db:
            ctx = await routes_library.showcase(db, session["tid"], lang)
        return templates.TemplateResponse(request, "profile_start.html", ctx)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        tier = await svc.get_tenant_tier(db, session["tid"])
        ai_limits = await ai_limits_service.get_ai_limits(db, tid=session["tid"])
        profile_count = await svc.count_active_profiles(db, session["tid"])
        picked = _picked_sources(enabled_source_keys, src_sent)
        profile_prefill, kind = await _new_profile_prefill(
            db,
            request,
            tid=session["tid"],
            from_template=from_template,
            from_package=from_package,
            preset=preset,
            picked=picked,
        )

    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "profile": profile_prefill,
            "errors": (
                [svc.SOURCES_REQUIRED_ERROR]
                if picked is not None and not profile_prefill["enabled_source_keys"]
                else []
            ),
            "is_first_profile": profile_count == 0,
            # The banner “what the template is assembled from”: the client must see WHAT exactly
            # was substituted, rather than guess where the words and codes in the form came from.
            "prefill_source": _prefill_source(profile_prefill, kind=kind),
            # Step 3: not a single live channel — connect right on the step.
            # Channel prefill = the tenant’s live channels (_new_profile_prefill).
            "no_live_channels": not profile_prefill.get("delivery_channels"),
            **_gate_flags(tier),
            "ai_auto_usage": ai_limits.auto,
            **(await _form_choices(session_factory, profile_prefill)),
            **_profile_form_ctx(profile_prefill),
        },
    )


async def _new_profile_prefill(
    db,
    request: Request,
    *,
    tid: str,
    from_template: str | None,
    from_package: str | None,
    preset: str | None,
    picked: list[str] | None = None,
) -> tuple[dict, str | None]:
    """The new template’s payload from the showcase + the kind of source (template / package / all / None).

    ONE path for the form and the confirmation screen: if they diverged in even one field, the client would see on the confirmation screen something other than what goes to ``POST /profiles``.

    ``picked`` — the sources ticked on “Composition” (GET parameters); ``None`` — not sent, the RS default remains. The keys go through the registry (an unknown one is discarded).
    """
    # The new profile’s sources — from the `sources` registry (SRC-0a): the enabled RS.
    source_keys = await default_source_keys_for_new_profile(db)
    tmpl = await lib_svc.get_template(db, from_template) if from_template else None
    # VG-14: a package — the same flow as a template, only the source is data-as-code.
    pkg = vertical_packages.get_package(from_package) if tmpl is None else None
    if tmpl is not None:
        profile_prefill = template_prefill.from_template(tmpl, source_keys=source_keys)
        kind: str | None = "template"
    elif pkg is not None:
        # The package name is localised, so the language is taken the SAME way that
        # the chrome and the showcase use (`users.locale` from the state), not by falling back to ru:
        # otherwise a client who picked a Serbian card would get a Russian name.
        profile_prefill = template_prefill.from_package(
            pkg,
            resolve_authed_lang(
                request, getattr(request.state, "cabinet_user_locale", None)
            ),
            source_keys=source_keys,
        )
        kind = "package"
    elif preset == "all":
        profile_prefill = template_prefill.unfiltered_payload(source_keys=source_keys)
        kind = "all"
    else:
        profile_prefill = template_prefill.base_payload(source_keys=source_keys)
        kind = None

    # Channel inheritance: a new profile must not silently stay WITHOUT delivery
    # if the account already has an email / Telegram connected, without editing the checkboxes
    # (see .claude/rules/money-ai-tiers.md — “setting ≠ fact”, symptom profile
    # b0d433a4/f2cc3032: 300 matching tenders, 0 deliveries). An override on top of
    # ``template_prefill.*`` — the same payload is counted by preview_service.preview_count,
    # which does not read delivery_channels (PREVIEW_WHERE_A), so the figure
    # “≈ N in 30 days” does not change.
    profile_prefill["delivery_channels"] = await _live_delivery_channels(db, tid)
    if picked is not None:
        profile_prefill["enabled_source_keys"] = await validate_source_keys(db, picked)
    return profile_prefill, kind


def _picked_sources(keys: list[str], src_sent: str | None) -> list[str] | None:
    """The platform choice from the GET form “Composition”: ``None`` — not sent (default), otherwise a list.

    ``src_sent`` is needed so that an EMPTY choice (all ticks removed) does not look like “not sent” and is not silently replaced by the RS default.
    """
    return list(keys) if (keys or src_sent) else None


@router.get("/profiles/new/confirm", response_class=HTMLResponse)
async def confirm_profile_screen(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    from_template: str | None = None,
    from_package: str | None = None,
    preset: str | None = None,
    enabled_source_keys: list[str] = Query(default=[]),
    src_sent: str | None = None,
):
    """The confirmation screen “what you will get” between the showcase and saving.

    Two clicks from the package to the template (owner 22.09.2026): the showcase leads HERE, not to a questionnaire of 18 fields. The screen restates the prefill in human words (“all of Serbia”, “any amount”) and shows the same figure “≈ N in 30 days” as the showcase card — it is counted by one :func:`preview_service.preview_count`.

    ‼️ The “Create” button sends the SAME ``POST /profiles`` with ALL the prefill fields as hidden form fields: a trimmed payload would mean the created template is not equal to the one shown. The full questionnaire — via the “Change settings” link.
    """
    if session is None:
        return _redirect_login(request)
    picked = _picked_sources(enabled_source_keys, src_sent)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        profile_prefill, kind = await _new_profile_prefill(
            db,
            request,
            tid=session["tid"],
            from_template=from_template,
            from_package=from_package,
            preset=preset,
            picked=picked,
        )
        preview = await preview_service.preview_count(db, profile_prefill)
    # “Change settings” carries the platform choice onwards — otherwise the full form
    # would open with the RS default and silently lose the choice from “Composition”.
    params = [
        (k, v)
        for k, v in (
            ("from_template", from_template),
            ("from_package", from_package),
            ("preset", preset),
        )
        if v
    ]
    if picked is not None:
        params += [
            ("enabled_source_keys", k) for k in profile_prefill["enabled_source_keys"]
        ]
        params.append(("src_sent", "1"))
    query = urlencode(params)
    return templates.TemplateResponse(
        request,
        "profile_confirm.html",
        {
            "profile": profile_prefill,
            "count": preview.count,
            "prefill_source": _prefill_source(profile_prefill, kind=kind),
            "edit_query": ("?" + query) if query else "",
            **(await _form_choices(session_factory, profile_prefill)),
            **_profile_form_ctx(profile_prefill),
        },
    )


async def _live_delivery_channels(db, tid: str) -> list[str]:
    """The tenant’s live channels, in the order of :data:`CHANNEL_CHOICES`.

    The same source as the /cabinet/account panel (``channel_svc.*_channel_state``, the sign of "live" — ``state.is_live``) — a single point of truth about what really delivers, not a second SELECT.
    """
    return await channel_svc.live_delivery_channels(db, tid)


def _prefill_source(payload: dict, *, kind: str | None) -> dict | None:
    """The data of the “assembled from …” banner: the kind of source, its name and WHAT was substituted."""
    if kind is None:
        return None
    terms = 0
    for group in payload.get("keywords_include") or []:
        terms += len((group or {}).get("all_of") or [])
    cls = payload.get("classifications") or {}
    return {
        "kind": kind,
        "name": payload.get("name") or "",
        "keywords": terms,
        "cpv": len(cls.get("include") or []),
    }


@router.post("/profiles", response_class=HTMLResponse)
async def create_profile(
    request: Request,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login()
    form = await request.form()
    payload, errors = parse_profile_form(form)
    gate = await _gate_ctx(session_factory, session["tid"])
    if errors:
        return templates.TemplateResponse(
            request,
            "profile_form.html",
            {
                "profile": payload,
                "errors": errors,
                **gate,
                **(await _form_choices(session_factory, payload)),
                **_profile_form_ctx(payload),
            },
            status_code=400,
        )
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        result = await svc.create_profile(
            db,
            tenant_id=session["tid"],
            user_id=session["uid"],
            raw_payload=payload,
            name_prefix=_auto_name_prefix(request),
        )
    if not result.ok:
        return templates.TemplateResponse(
            request,
            "profile_form.html",
            {
                "profile": payload,
                "errors": result.errors,
                **gate,
                **(await _form_choices(session_factory, payload)),
                **_profile_form_ctx(payload),
            },
            status_code=400,
        )
    # Step 2 profile_channels: a saved profile without a single delivery channel
    # would look working — symptom: profile b0d433a4 / tenant f2cc3032, 300
    # matching tenders, 0 deliveries. The flag reads the FINAL list AFTER
    # tiers.gate_profile (the lite plan could have extended it to ["email"]), and not
    # the raw ``payload`` of the form — otherwise the banner would falsely pop up where a channel is
    # already substituted.
    # The result screen instead of a silent redirect to /app (step 5, owner 22.09.2026):
    # “nothing happened” — how the client read the former return to the cabinet.
    # ‼️ The “no channels” branch from #531 is kept: the flag travels on and becomes, on the new screen,
    # a visible banner with a link to connecting a channel.
    if result.profile is None:
        return RedirectResponse(url="/app", status_code=303)
    no_channels = not (result.profile.delivery_channels or [])
    url = f"/profiles/{result.profile.id}/created" + (
        "?no_channels=1" if no_channels else ""
    )
    return RedirectResponse(url=url, status_code=303)


@router.get("/profiles/{profile_id}/created", response_class=HTMLResponse)
async def created_profile_screen(
    request: Request,
    profile_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """The result screen right after the template is created.

    It shows WHAT came out: “N matched over 30 days” for the template and the first three examples. The figure and the examples come from one :mod:`preview_service` (one A+B predicate), so “N” and the list cannot diverge.

    ``?no_channels=1`` — the same sign that previously led to ``/app?no_channels=1`` (#531): the template is saved, but there is nowhere to deliver it. The refusal must be VISIBLE, so here it is a banner with a link to connecting a channel.
    """
    if session is None:
        return _redirect_login(request)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        profile = await svc.get_profile(db, session["tid"], profile_id)
        if profile is None:
            return templates.TemplateResponse(
                request,
                "message.html",
                {
                    "title_key": "pf_not_found_title",
                    "success": False,
                    "home_href": "/app",
                },
                status_code=404,
            )
        payload = preview_service.payload_from_profile(profile)
        preview = await preview_service.preview_count(db, payload, examples_limit=3)
        open_now = await preview_service.open_now_examples(db, payload, str(profile.id))
        warning = await channel_svc.delivery_warning(
            db, session["tid"], await svc.list_profiles(db, session["tid"])
        )
    return templates.TemplateResponse(
        request,
        "profile_created.html",
        {
            "profile": profile,
            "count": preview.count,
            "examples": preview.examples,
            "open_now": open_now,
            "delivery_warning": warning,
        },
    )


@router.get("/profiles/{profile_id}", response_class=HTMLResponse)
async def profile_detail(
    request: Request,
    profile_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login(request)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        profile = await svc.get_profile(db, session["tid"], profile_id)
        if profile is None:
            return templates.TemplateResponse(
                request,
                "message.html",
                {
                    "title_key": "pf_not_found_title",
                    "success": False,
                    "home_href": "/app",
                },
                status_code=404,
            )
        suppressed = await svc.downgrade_notice(db, session["tid"], profile)
        card_sources = await _card_sources(db, list(profile.enabled_source_keys or []))
        warning = await channel_svc.delivery_warning(
            db, session["tid"], await svc.list_profiles(db, session["tid"])
        )
        # ``?updated=1`` — the screen after the template was edited (see update_profile): the block
        # “Already open tenders” is shown only there, not on every opening.
        open_now = []
        if request.query_params.get("updated") == "1":
            open_now = await preview_service.open_now_examples(
                db, preview_service.payload_from_profile(profile), str(profile.id)
            )
    return templates.TemplateResponse(
        request,
        "profile_detail.html",
        {
            "profile": profile,
            "suppressed": suppressed,
            "open_now": open_now,
            "delivery_warning": warning,
            "profile_id": str(profile.id),
            **card_sources,
        },
    )


@router.post("/profiles/{profile_id}/sources/add", response_class=HTMLResponse)
async def add_profile_source(
    request: Request,
    profile_id: str,
    source_key: str = Form(default=""),
    csrf: str = Form(default=""),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    settings: CabinetSettings = Depends(deps.get_settings),
):
    """The “+ source” button on the card: ONE source — into THIS template.

    Written only on click (CSRF is mandatory; without a token nothing is written). The source must be enabled in the registry: an unknown or disabled key is discarded by :func:`validate_source_keys` without a write. The subscription moment is the same as the banner’s (:func:`source_banner.add_source`). A foreign or deleted template — 404: ``get_profile`` isolates by tenant under RLS.
    htmx gets the updated sources block; the ordinary form — 303 to the card.
    """
    if session is None:
        return _redirect_login(request)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        profile = await svc.get_profile(db, session["tid"], profile_id)
        if profile is None:
            raise HTTPException(status_code=404)
        pid = str(profile.id)
        keys = list(profile.enabled_source_keys or [])
        if security.verify_csrf(csrf, session, settings.cabinet_secret_key):
            allowed = await validate_source_keys(db, [source_key.strip()])
            if allowed and source_banner.add_source(
                profile, allowed[0], datetime.now(timezone.utc)
            ):
                # Keys — BEFORE the commit: after commit the ORM attributes expire, and
                # re-reading in an async session does not work (as in enable_everywhere).
                keys = list(profile.enabled_source_keys or [])
                await db.commit()
        card = await _card_sources(db, keys)
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url=f"/profiles/{pid}", status_code=303)
    return templates.TemplateResponse(
        request, "_profile_sources.html", {"profile_id": pid, **card}
    )


@router.get("/profiles/{profile_id}/edit", response_class=HTMLResponse)
async def edit_profile_form(
    request: Request,
    profile_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login(request)
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        profile = await svc.get_profile(db, session["tid"], profile_id)
        tier = await svc.get_tenant_tier(db, session["tid"])
        ai_limits = await ai_limits_service.get_ai_limits(db, tid=session["tid"])
    if profile is None:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title_key": "pf_not_found_title", "success": False, "home_href": "/app"},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "profile_form.html",
        {
            "profile": profile,
            "errors": [],
            "profile_id": profile_id,
            **_gate_flags(tier),
            "ai_auto_usage": ai_limits.auto,
            **(await _form_choices(session_factory, profile)),
            **_profile_form_ctx(profile),
        },
    )


@router.post("/profiles/{profile_id}", response_class=HTMLResponse)
async def update_profile(
    request: Request,
    profile_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login()
    form = await request.form()
    payload, errors = parse_profile_form(form)
    gate = await _gate_ctx(session_factory, session["tid"])
    if errors:
        return templates.TemplateResponse(
            request,
            "profile_form.html",
            {
                "profile": payload,
                "errors": errors,
                "profile_id": profile_id,
                **gate,
                **(await _form_choices(session_factory, payload)),
                **_profile_form_ctx(payload),
            },
            status_code=400,
        )
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        result = await svc.update_profile(
            db,
            tenant_id=session["tid"],
            profile_id=profile_id,
            user_id=session["uid"],
            raw_payload=payload,
        )
    if not result.ok:
        return templates.TemplateResponse(
            request,
            "profile_form.html",
            {
                "profile": payload,
                "errors": result.errors,
                "profile_id": profile_id,
                **gate,
                **(await _form_choices(session_factory, payload)),
                **_profile_form_ctx(payload),
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/profiles/{profile_id}?updated=1", status_code=303)


@router.post("/profiles/{profile_id}/propose")
async def propose_to_library(
    request: Request,
    profile_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login()
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        profile = await svc.get_profile(db, session["tid"], profile_id)
        if profile is None:
            return templates.TemplateResponse(
                request,
                "message.html",
                {
                    "title_key": "pf_not_found_title",
                    "success": False,
                    "home_href": "/app",
                },
                status_code=404,
            )
        await lib_svc.propose_from_profile(
            db,
            user_id=session["uid"],
            tenant_id=session["tid"],
            profile_id=str(profile.id),
            name=profile.name or "Без названия",
        )
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "title_key": "pd_proposed_title",
            "body_key": "pd_proposed_body",
            "success": True,
            "home_href": "/app",
        },
    )


@router.post("/profiles/{profile_id}/delete")
async def delete_profile(
    profile_id: str,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _redirect_login()
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        await svc.soft_delete_profile(
            db, tenant_id=session["tid"], profile_id=profile_id
        )
    return RedirectResponse(url="/app", status_code=303)


@router.post("/profiles/{profile_id}/deactivate")
async def deactivate_profile(
    profile_id: str,
    next: str | None = Form(default=None),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Disables a template (reversibly), WITHOUT deleting it.

    ‼️ Do not confuse with ``POST /profiles/{id}/delete``: that one sets ``deleted_at``, the template disappears from the lists altogether, and there is no way to turn it back on. The dashboard / cabinet toggle must be reversible, so it has its own route. ``next`` (a hidden form field) returns to where the button was pressed — the dashboard does not pass it at all (default None → ``/app``, the previous behavior).
    """
    if session is None:
        return _redirect_login()
    async with session_factory() as db:
        await repo.set_tenant(db, session["tid"])
        await svc.set_profile_active(
            db, tenant_id=session["tid"], profile_id=profile_id, is_active=False
        )
    return RedirectResponse(url=_safe_next(next), status_code=303)


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(
    profile_id: str,
    next: str | None = Form(default=None),
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """Enables a template back — the pair of ``/deactivate``, with the limit gate.

    ‼️ The limit is checked BEFORE the write, and the refusal is VISIBLE (a redirect with ``?limit=profiles`` → a banner with a link to the plans), not a silent no-op: after a plan downgrade there are legitimately more non-deleted templates than the limit, and the client must understand why the toggle did not work. The banner is rendered where ``next`` leads (the dashboard or the cabinet) — both read the same flag.

    The gate fires only for a DISABLED template: enabling an already enabled one is idempotent and must not run into the limit that it itself belongs to.
    """
    if session is None:
        return _redirect_login()
    tid = session["tid"]
    target = _safe_next(next)
    async with session_factory() as db:
        await repo.set_tenant(db, tid)
        # A foreign / nonexistent id is not found (tenant in the WHERE) → 0 changes.
        profile = await svc.get_profile(db, tid, profile_id)
        if profile is None:
            return RedirectResponse(url=target, status_code=303)
        if not profile.is_active:
            limit = tiers.profile_limit(await svc.get_tenant_tier(db, tid))
            enabled = await svc.count_enabled_profiles(db, tid)
            if limit is not None and enabled >= limit:
                return RedirectResponse(
                    url=f"{target}?{PROFILE_LIMIT_FLAG}={PROFILE_LIMIT_VALUE}",
                    status_code=303,
                )
            await svc.set_profile_active(
                db, tenant_id=tid, profile_id=profile_id, is_active=True
            )
    return RedirectResponse(url=target, status_code=303)


async def _source_choices(session_factory: Callable, profile=None) -> list[dict]:
    """The sources for the form’s checkboxes — from the ``sources`` registry (SRC-0a/0b).

    The enabled sources + the keys already in the profile (a source switched off by the admin stays visible, ticked and removable — with the mark ``paused``, owner 30.09 evening item 3). The countries are CODES: the template takes the country label from the catalog (``country_XX``), not the code. ``sources`` has no RLS — ``set_tenant`` is not needed.
    """
    selected = (
        profile.get("enabled_source_keys")
        if isinstance(profile, dict)
        else getattr(profile, "enabled_source_keys", None)
    )
    async with session_factory() as db:
        return await source_choice_rows(db, selected or [])


async def _card_sources(db, keys: list[str]) -> dict:
    """The “Sources” block of the template card: what is set and what can be added in one click.

    “Set” — the template’s keys (a source switched off by the admin stays, marked ``paused``). “Can add” — the enabled sources that are not in the template. The rows are the same as the form checkboxes (:func:`source_choice_rows`).
    """
    rows = await source_choice_rows(db, keys)
    return {
        "sources_in": [r for r in rows if r["key"] in keys],
        "sources_missing": [
            r for r in rows if not r["paused"] and r["key"] not in keys
        ],
    }


async def _form_choices(session_factory: Callable, profile=None) -> dict:
    return {
        "region_choices": REGION_CHOICES,
        "source_choices": await _source_choices(session_factory, profile),
        "channel_choices": CHANNEL_CHOICES,
        "ai_level_choices": AI_LEVEL_CHOICES,
        "schedule_choices": SCHEDULE_CHOICES,
    }


def _profile_form_ctx(profile) -> dict:
    """Serializes the profile (ORM or dict after an error) into the template context.

    Returns additional keys for the pre-fill of the form fields:
    - keywords_include_text / keywords_exclude_text — strings for the textarea
    - cls_include_text / cls_exclude_text — strings 'eu_cpv:CODE' for the CPV widget
    """
    if profile is None:
        return {
            "keywords_include_text": "",
            "keywords_exclude_text": "",
            "cls_include_text": "",
            "cls_exclude_text": "",
        }

    def _getattr(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    kw_inc = _getattr(profile, "keywords_include") or []
    kw_exc = _getattr(profile, "keywords_exclude") or []
    cls = _getattr(profile, "classifications") or {}

    if isinstance(kw_exc, list):
        kw_exc_text = ", ".join(str(k) for k in kw_exc)
    else:
        kw_exc_text = str(kw_exc)

    if isinstance(cls, dict):
        inc_entries = cls.get("include") or []
        exc_entries = cls.get("exclude") or []
    else:
        inc_entries, exc_entries = [], []

    return {
        "keywords_include_text": groups_to_textarea(
            kw_inc if isinstance(kw_inc, list) else []
        ),
        "keywords_exclude_text": kw_exc_text,
        "cls_include_text": classifications_to_textarea(inc_entries),
        "cls_exclude_text": classifications_to_textarea(exc_entries),
    }
