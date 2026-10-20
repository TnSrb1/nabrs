"""Routes of the tender AI artifacts in the cabinet (web-ai-1 view + web-ai-2 generation).

- ``GET  /app/tenders/{tender_id}?profile_id=...`` — the artifacts page (a read-only render of what is already generated; for bid / checklist / draft — the generation start buttons). Content “as is” (owner’s decision): the title and description open on the ORIGINAL, the model is NOT called on render; translation — by button.
- ``GET  /app/tenders/{tender_id}/title?...&lang=...&view=...`` — the htmx fragment of the title (the translation toggle, ``_card_title.html``): default ``lang=sr`` (original, no model), ``lang=ru`` — a title-only translation.
- ``GET  /app/tenders/{tender_id}/description?profile_id=...&lang=...`` — the htmx fragment of the description (``_tender_description.html``): default ``lang=sr`` (original, no model); ``lang=ru`` — translation sr→ru in chunks by button; the whole original for ``lang=sr`` / a partial refusal.
- ``POST /app/tenders/{tender_id}/generate/{level}?profile_id=...`` — the start of generation of the level ``level`` ∈ {bid, checklist, draft}. It synchronously checks the cheap things (session, profile ownership, frozen, the Pro+ gate, cache, limit) and immediately returns the corresponding partial; otherwise it puts a ``BackgroundTasks`` task (WITH ITS OWN session — the route’s session is closed by the time it runs) and returns 202 + “generating…” with HTMX polling of the status.
- ``GET  /app/tenders/{tender_id}/status/{level}?profile_id=...&polls=N`` — the status fragment for HTMX polling: the artifact appeared → render the section and stop polling; not yet → “generating…” (polls+1); the poll ceiling exceeded → “try later”.

Require-session (like ``routes_profiles``): anonymous on GET → /login; on POST / status via HTMX → 401 + ``HX-Redirect``. The AI primitives (cache / limit / analyze / finalize) are shared (:mod:`src.ai.usage`, :mod:`src.ai`), the logic is in :mod:`src.api.cabinet.tender_ai_generate`; the bot files are untouched.

‼️ BOTH translation fragments are paid model calls, and they have ONE gate (``_translation_allowed`` → ``_charge_translation``, level ``translate``): the freeze, the plan, the monthly limit §14.5. Until now the translation was the only AI action outside the metering contour — a frozen tenant translated for free and without a limit.
"""

from __future__ import annotations

from collections.abc import Callable

from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.ai import translation
from src.ai.usage import limit_decision, record_usage
from src.api import deps, repo
from src.api.cabinet import ai_limits_service
from src.api.cabinet import customer_analytics_service as cust
from src.api.cabinet import competition_service
from src.api.cabinet import tender_ai_generate as gen
from src.api.cabinet import tender_ai_service as svc
from src.api.cabinet import workflow_service
from src.api.cabinet.i18n import CABINET, COMMON, SUPPORTED, resolve_authed_lang
from src.api.cabinet.tender_path import canonical_tender_id, tender_path_redirect
from src.api.templating import templates
from src.plans import catalog

router = APIRouter(tags=["cabinet"])

# The status poll ceiling: 3 s × 20 ≈ 60 s (the upper bound of an AI call) + margin.
_MAX_POLLS = 25

# The original language from the source (eNabavke delivers content in Serbian) — the value of
# the “Original (sr)” toggle in the card (VG-26).
_ORIGINAL_LANG = "sr"

# The metering level of the translation in ai_usage / _TIER_LIMITS (§14.5).
_TRANSLATE_LEVEL = "translate"


def _interface_ctx(request: Request) -> dict:
    """The interface language and its catalog for the translation toggle (owner’s decision).

    ‼️ The target language of the “Translate” button is the interface language of the USER, whatever it is (sr / ru / en). It is taken WITHOUT ``?lang=`` (``include_query=False``): on this endpoint the query means the TARGET of the translation, not “switch the interface”. Otherwise a click on “Show original” (``?lang=sr``) would silently move the button’s language to Serbian, and the next click would translate sr→sr.

    The chrome’s context processor puts its own ``lang`` / ``t`` (they do take ``?lang=`` into account), so the template gets them UNDER OTHER NAMES — ``ui_lang`` / ``ui_t``.
    """
    ui_lang = resolve_authed_lang(
        request,
        getattr(request.state, "cabinet_user_locale", None),
        include_query=False,
    )
    return {"ui_lang": ui_lang, "ui_t": {**COMMON[ui_lang], **CABINET[ui_lang]}}


# The feed / teaser card → how the title-link looks in the translation fragment
# (portal, Stage 5). We keep it HERE, not in the fragment template: the HTMX response must
# match what the card rendered initially, otherwise after the swap the feed
# falls out of line. The links differ between the kinds in MEANING, and this is not carelessness: the feed leads
# to the source (a new tab), the dashboard teaser — to the AI analysis in the cabinet (test_design_
# system::test_dashboard_teaser_links_tender_to_cabinet_card).
_CARD_TITLE_VIEWS = {
    "feed": {"title_class": "fd-card__title", "title_blank": True},
    "teaser": {"title_class": "font-medium hover:underline", "title_blank": False},
    # Tender detail: the title is not a link (you are already on the card), but an h1-like
    # text (title_url="" → <span>). The same translation toggle as the feed.
    "detail": {"title_class": "text-xl font-semibold", "title_blank": False},
    # The cockpit’s agenda row (“My tasks” and the calendar): the same toggle as in
    # the feed, but the title is a compact link inside the row.
    "agenda": {"title_class": "agenda__title-link", "title_blank": False},
    # The “My tenders” row (_my_tender_row.html): the same inner link and
    # the same class as the agenda, but TWO different kinds, not one. The funnel board
    # and the folder render one tender on ONE page /app/my at the same time
    # (the tender lies both in a stage and in a folder) — a shared view would give identical
    # id="card-title-{view}-{tender_id}-{profile_id}" to two rows
    # (_card_title.html), and the hx-target of the second button would land in the first row.
    # ``view`` already travels in the fragment URL and is returned by the server — cheaper
    # to tell them apart with it than to add a separate DOM parameter without a round-trip.
    "my_board": {"title_class": "fn-row__title", "title_blank": False},
    "my_folder": {"title_class": "fn-row__title", "title_blank": False},
}

# Generation status → the mode of the _ai_slot.html fragment (terminal branches).
_STATUS_TO_MODE = {
    gen.STATUS_GATE_PRO: "gate_pro",
    gen.STATUS_FROZEN: "frozen",
    gen.STATUS_LIMIT_EXHAUSTED: "limit_exhausted",
    gen.STATUS_MODEL_BUSY: "model_busy",
    gen.STATUS_MODEL_ERROR: "model_error",
    gen.STATUS_NO_TENDER: "no_tender",
    gen.STATUS_NO_PROFILE: "no_profile",
}


def _not_found(request: Request) -> HTMLResponse:
    # KEYS of the catalog (message.html localises them via ``t``), not a ready text.
    return templates.TemplateResponse(
        request,
        "message.html",
        {
            "title_key": "msg_tender_nf_title",
            "body_key": "msg_tender_nf_body",
            "success": False,
            "home_href": "/app",
        },
        status_code=404,
    )


def _slot(
    request: Request,
    *,
    level: str,
    tender_id: str,
    profile_id: str,
    mode: str,
    output=None,
    polls: int = 0,
    status_code: int = 200,
    flash: dict | None = None,
) -> HTMLResponse:
    """Render of the swap container ``_ai_slot.html`` (one id for all states).

    ``flash`` — the data of the “limit exhausted” flash (``_flash_for_level``). It is set ONLY in response to the client’s action: a flash on every opening of the card would be an irritant, not a message.
    """
    return templates.TemplateResponse(
        request,
        "_ai_slot.html",
        {
            "level": level,
            "tender_id": str(tender_id),
            "profile_id": str(profile_id),
            "mode": mode,
            "output": output,
            "polls": polls,
            "flash": flash,
        },
        status_code=status_code,
    )


async def _flash_for_level(
    session_factory: Callable, tid: str, level: str
) -> dict | None:
    """The data of the “limit exhausted” flash for the level ``level`` (or ``None``).

    The spend figures are taken by THE SAME predicate as the gate (:func:`src.ai.usage.used_count` via ``usage_snapshot``), and the limit of the target plan — from the gating via :func:`src.plans.catalog.level_limit_for_plan`. The plan name and price — from the showcase catalog. There are no numbers of its own here: otherwise the flash would promise a limit that the gate would not give.

    ``upsell_name is None`` (the client is on the top plan) is no reason to stay silent: the template shows the fact of exhaustion and when the limit renews.
    """
    async with session_factory() as db:
        # RLS: the web runs as tendersrb_app (NOBYPASSRLS), ai_usage is under FORCE RLS —
        # set_tenant FIRST, as an action, otherwise the counter silently shows zeros.
        await repo.set_tenant(db, tid)
        limits = await ai_limits_service.get_ai_limits(db, tid=tid)
    row = limits.by_level(level)
    if row is None:
        return None
    plan = catalog.upsell_for_level(level, limits.tier)
    upsell_limit = None if plan is None else catalog.level_limit_for_plan(plan, level)
    return {
        "level": level,  # the level's label is resolved by the template: t['ai_level_name_'~level]
        "used": row.used,
        "limit": row.limit,
        "upsell_name": None if plan is None else plan.key,
        "upsell_limit": upsell_limit,
        "upsell_price": None if plan is None else plan.price_eur,
    }


async def _translation_reason(db, tid: str) -> str:
    """Why the translation can / cannot be done: freeze → plan → monthly limit.

    Translation is a paid call, and until now it was the only AI action outside the metering contour: a frozen tenant translated freely, and the level had no limit at all. The order of the axes is as in artifact generation (``tender_ai_generate.load_tenant_access``, ONE selection for both gates): the freeze FIRST, the effective plan after it, the §14.5 limit last.

    Returns ``'ok'`` / ``'frozen'`` / ``'exhausted'`` / ``'unavailable'``. Not ``'ok'`` → do NOT call the model, show the original.

    ‼️ It returns the REASON, not a ``bool`` (until 15.08.2026). Checked by render: with an exhausted limit the client saw exactly the same “translation unavailable” as with a model failure — that is, read a monetary refusal as a temporary glitch and pressed the button again. The owner's requirement: “nothing must switch off silently, the subscriber must see that they have exhausted the limit”.

    ‼️ There is no SECOND AUTHORISATION gate here: the tender's ownership by the tenant has already been checked by ``svc.get_tender_analyses`` (None → 404). Only money here.
    ‼️ RLS: ``set_tenant`` FIRST as the action — ``ai_usage`` is tenant-scoped, otherwise the counter is read / written as 0 rows, fail-closed.
    """
    await repo.set_tenant(db, tid)
    frozen, tier = await gen.load_tenant_access(db, tid)
    if frozen:
        return "frozen"
    return await limit_decision(db, tenant_id=tid, tier=tier, level=_TRANSLATE_LEVEL)


async def _charge_translation(
    db, tid: str, usage: translation.TranslationUsage
) -> None:
    """Writes off ONE unit of the translation limit if the model was really called.

    The unit of the limit = one user action (the title OR the description), regardless of the number of chunks; ``tokens`` / ``cost`` carry the actual sum over the chunks. A cache hit (``calls == 0``) writes off nothing — otherwise the repeated display of an already translated card would eat the quota. A partial refusal of the description does write off (the money spent on the chunks that succeeded is spent — see ``TranslationUsage``).
    """
    if usage.calls <= 0:
        return
    await record_usage(
        db,
        tenant_id=tid,
        level=_TRANSLATE_LEVEL,
        calls=1,
        tokens=usage.tokens_input + usage.tokens_output,
        cost=usage.cost_usd,
    )


def _hx_login() -> HTMLResponse:
    """Anonymous on an HTMX request: 401 + HX-Redirect to /login (a full navigation)."""
    resp = HTMLResponse("", status_code=401)
    resp.headers["HX-Redirect"] = "/login"
    return resp


# --------------------------------------------------------------------------- #
# GET of the artifacts page (web-ai-1)
# --------------------------------------------------------------------------- #


@router.get("/app/tenders/{tender_id}", response_class=HTMLResponse)
async def tender_detail(
    request: Request,
    tender_id: str,
    profile_id: str | None = None,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    # The canonical id (as on the showcase): urn:uuid/{…}/UPPER → 301, garbage → 404.
    # A raw string into CAST(... AS uuid) gave a 500 (production, 08.10).
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return _not_found(request)
    if canon != tender_id:
        return tender_path_redirect(request, tender_id, canon)
    if session is None:
        # A link from the Telegram card: after login, return to THIS page.
        # A relative path — /login itself passes it through safe_next_path.
        back = f"/app/tenders/{tender_id}"
        if profile_id:
            back += f"?profile_id={profile_id}"
        return RedirectResponse(
            url=f"/login?next={quote(back, safe='')}", status_code=303
        )
    if not profile_id:
        return _not_found(request)
    tid = session["tid"]
    async with session_factory() as db:
        data = await svc.get_tender_analyses(
            db,
            tid=tid,
            tender_id=tender_id,
            profile_id=profile_id,
        )
        # Naručilac analytics (web-ai-3/4) — pure SQL, the same db session (tender
        # already confirmed — get_tender_analyses ≠ None). Synchronous: the SQL is fast.
        # All three sections (customer / competitors / re-procurements) in one pass
        # (one set_tenant + gate + resolve), see get_narucilac_analytics.
        bundle = (
            None
            if data is None
            else await cust.get_narucilac_analytics(db, tid=tid, tender_id=tender_id)
        )
        # Konkurencija: winners at the customer / in the CPV group (no gate, SQL).
        competition = (
            None
            if data is None
            else await competition_service.get_competition(
                db, tid=tid, tender_id=tender_id
            )
        )
        # VG-17: the state of the workflow buttons (watch / folder / note) for the card.
        wf_state = (
            None
            if data is None
            else await workflow_service.get_tender_state(
                db, tenant_id=tid, tender_id=tender_id
            )
        )
        wf_folders = (
            [] if data is None else await workflow_service.list_folders(db, tid)
        )
        # Why there is no analysis: if the profile asked to analyze the stream and the tender
        # arrived without an analysis, the subscriber must see the REASON and the figures, not
        # an empty block (owner’s requirement of 15.08.2026). The same spend predicate
        # as the worker’s gate — src.ai.usage.usage_snapshot → used_count.
        auto_analysis = (
            None
            if data is None
            else await ai_limits_service.get_auto_analysis_state(
                db, tid=tid, profile_id=profile_id
            )
        )
        # The remainder by level — so that the button knows its state BEFORE the click
        # (exhausted → “Limit reached → upgrade the plan” instead of “Generate”;
        # close to the threshold → a label with the remainder). The same snapshot as in the
        # “AI limits” block on the subscription page: one predicate per meaning.
        ai_limits = (
            None if data is None else await ai_limits_service.get_ai_limits(db, tid=tid)
        )
    if data is None:
        return _not_found(request)

    # Content “as is” (owner’s decision, 23.07): the detail page OPENS on the original, and NEITHER
    # the title NOR the description goes to the model on render. Translation — only by the
    # explicit user button:
    #   • the title — by the ``_card_title.html`` toggle (view="detail", fragment
    #     ``GET .../title``), by the same mechanism as in the feed;
    #   • the description — by the “Translate description” button (fragment ``GET .../description``).
    # Therefore translate_tender / translate_description are NOT called here at all.
    return templates.TemplateResponse(
        request,
        "tender_detail.html",
        {
            "data": data,
            "analytics": None if bundle is None else bundle.customer,
            "competitor": None if bundle is None else bundle.competitor,
            "reprocurement": None if bundle is None else bundle.reprocurement,
            "competition": competition,
            "incumbent": None if bundle is None else bundle.incumbent,
            "wf_state": wf_state,
            "wf_folders": wf_folders,
            "auto_analysis": auto_analysis,
            "ai_limits": ai_limits,
            "original_lang": _ORIGINAL_LANG,
            **_interface_ctx(request),
        },
    )


# --------------------------------------------------------------------------- #
# GET fragment “tender description” — background translation + chunking (VG-26)
# --------------------------------------------------------------------------- #


@router.get("/app/tenders/{tender_id}/description", response_class=HTMLResponse)
async def tender_description(
    request: Request,
    tender_id: str,
    profile_id: str | None = None,
    lang: str = _ORIGINAL_LANG,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    redis=Depends(deps.get_redis),
):
    """HTML fragment of the tender description: the original by default, translation by button.

    Content “as is” (owner’s decision): the default ``lang`` = ``sr`` (the original, WITHOUT a model call). The detail page renders this same partial in the original, and the “Translate description” button pulls the fragment with ``lang=ru`` and swaps the container for the translation; the reverse button “Show original” returns ``lang=sr``. There is no skeleton on page load any more — the indicator appears only on the pressed button (``.htmx-request``).

    Authorisation — THE SAME gate as on the detail page (``get_tender_analyses`` → ``None`` → 404): there is no second authorization. ‼️ The text to translate is taken ONLY from ``data`` (the server), NEVER from the request.

    ``lang == sr`` — the original as is, WITHOUT a model call. A partial refusal of the translation (any chunk not translated / the ceiling) → the WHOLE ORIGINAL, not a mix of languages and not a 500.

    ‼️ Translation is a PAID call: before the model stands the freeze / plan / limit gate (``_translation_allowed``); after it — the write-off of one ``translate`` unit (``_charge_translation``). If the gate did not pass → the same “translation unavailable” branch as for a model refusal.
    """
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return _not_found(request)
    if canon != tender_id:
        return tender_path_redirect(request, tender_id, canon)
    if session is None:
        return _hx_login()
    if not profile_id:
        return _not_found(request)
    # ‼️ ``lang`` here is the axis of the CONTENT translation (sr = original / ru = translation), NOT the
    # interface language. So that the chrome of the fragment (buttons / labels) does not “move” to ru on
    # pressing “Translate”, we ask the processor to ignore ?lang= (two axes, CLAUDE.md).
    request.state.cabinet_ignore_query_lang = True
    # The target language — only sr / ru / en. An unknown value → the original (without
    # a model call): not a 500 and not “a translation into something”.
    if lang not in SUPPORTED:
        lang = _ORIGINAL_LANG

    show_original = lang == _ORIGINAL_LANG
    translated = None
    usage = None
    # Session #1 (short): the tender’s ownership + the paid gate. The data needed later
    # we take into variables and CLOSE the session BEFORE the model call.
    async with session_factory() as db:
        data = await svc.get_tender_analyses(
            db,
            tid=session["tid"],
            tender_id=tender_id,
            profile_id=profile_id,
        )
        if data is None:
            return _not_found(request)
        # The paid gate BEFORE the model: freeze / plan / limit. If not passed → we show
        # the original with the same “translation unavailable” branch; the model is not called.
        # The refusal reason is needed FOR DISPLAY: “limit exhausted” and "the model did not
        # answer" are different news for the subscriber (see _translation_reason).
        reason = (
            "ok" if show_original else await _translation_reason(db, session["tid"])
        )
        allowed = not show_original and reason == "ok"

    # ‼️ The model call — WITHOUT an open DB session (up to 60 s for a description translation):
    # the connection pool is small, and a dozen parallel presses of “Translate”
    # each holding the session for the duration of the call exhaust the pool and bring the whole cabinet down
    # (CLAUDE.md, analysis of 26.07).
    if allowed:
        translated, usage = await translation.translate_description(
            tender_id=data.tender_id,
            description=data.description,
            redis=redis,
            # The translation language = the client’s interface language (owner’s decision, 25.07).
            target_lang=lang,
        )

    # Session #2 (short) — only if the model was really called (a cache hit does not
    # open it at all). ‼️ A deliberate trade-off: between the limit check (session #1)
    # and the write-off (session #2) there is a window, so parallel translations may
    # exceed the quota by a few units — exact accounting would require a lock across
    # the model call, and that is exactly the connection-holding time we are moving away from.
    # It is cheaper than holding the session for the model call; exact accounting
    # is not in this task.
    if usage is not None and usage.calls > 0:
        async with session_factory() as db:
            await repo.set_tenant(db, session["tid"])
            await _charge_translation(db, session["tid"], usage)

    # No translation (the sr toggle, an AI / cache refusal, a partial failure, the gate) → the original.
    machine = not show_original and translated is not None
    return templates.TemplateResponse(
        request,
        "_tender_description.html",
        {
            "tender_id": str(data.tender_id),
            "profile_id": str(data.profile_id),
            "description": translated if machine else data.description,
            "machine": machine,
            "unavailable": not show_original and translated is None,
            # The reason for the block (limit / freeze / plan) — or None if
            # there is no translation for another reason (model failure, timeout).
            "block_reason": None if reason == "ok" else reason,
            "original_lang": _ORIGINAL_LANG,
            **_interface_ctx(request),
        },
    )


# --------------------------------------------------------------------------- #
# GET fragment “card title” — the translation toggle in the feeds (portal, Stage 5)
# --------------------------------------------------------------------------- #


@router.get("/app/tenders/{tender_id}/title", response_class=HTMLResponse)
async def card_title(
    request: Request,
    tender_id: str,
    profile_id: str | None = None,
    lang: str = _ORIGINAL_LANG,
    view: str = "feed",
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
    redis=Depends(deps.get_redis),
):
    """HTML fragment of the card title: translation sr→ru on click and back.

    The LinkedIn pattern “See translation ↔ See original” for the cards of ``/app/feed``, the dashboard teaser and the detail title (``view=“detail”``). The default ``lang`` = ``sr`` (the content “as is”, owner’s decision): without ?lang= the fragment gives the original and does NOT call the model. Translation — ONLY by button: on the list render the model is not called at all, and the ``translate_tender`` cache is shared by all tenants (keyed by the text fingerprint, without tenant_id), so a repeated display of an already translated title is free.

    ‼️ ONLY the title goes to the model (``description=None``) — see the comment at the call. The cache key is computed from the fingerprint of the (title, description) pair, so the fragment has ITS OWN key, and it deliberately does not match the detail key (``tender_detail``, where the description is really translated): sharing the cache with it would mean dragging in its timeout.

    Authorisation — THE SAME gate as on the detail page (``get_tender_analyses`` → ``None`` → 404): it checks that the tender was delivered to this tenant under this profile. There is no second authorization here on purpose. ‼️ The text to translate is taken ONLY from ``data`` (the server), NEVER from the request: otherwise anyone could push arbitrary text into the model at our expense and put the result into the shared cache.

    ``lang == sr`` — the original as is, WITHOUT a model call. No translation (an AI refusal / no cache) is not an error either: the original + a quiet note, not a 500.

    ‼️ Translation is a PAID call: the same gate (``_translation_allowed``) and the same write-off (``_charge_translation``) as in the description fragment — one level ``translate`` for both fragments, the limit unit = one press.
    """
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return _not_found(request)
    if canon != tender_id:
        return tender_path_redirect(request, tender_id, canon)
    if session is None:
        return _hx_login()
    if not profile_id or view not in _CARD_TITLE_VIEWS:
        return _not_found(request)
    # ‼️ ``lang`` here is the axis of the CONTENT translation (sr = original / ru = translation), NOT the
    # interface. The chrome of the fragment follows the user’s interface language, so the
    # processor ignores ?lang= (do not mix the two axes — CLAUDE.md).
    request.state.cabinet_ignore_query_lang = True
    # The target language — only sr / ru / en. An unknown value → the original (without
    # a model call): not a 500 and not “a translation into something”.
    if lang not in SUPPORTED:
        lang = _ORIGINAL_LANG

    show_original = lang == _ORIGINAL_LANG
    tr = None
    usage = None
    # Session #1 (short): the tender’s ownership + the paid gate. The data needed later
    # we take into variables and CLOSE the session BEFORE the model call.
    async with session_factory() as db:
        data = await svc.get_tender_analyses(
            db,
            tid=session["tid"],
            tender_id=tender_id,
            profile_id=profile_id,
        )
        if data is None:
            return _not_found(request)
        # The paid gate BEFORE the model: freeze / plan / limit. If not passed → the original with
        # the same quiet note as for a model refusal.
        # The refusal reason is needed FOR DISPLAY: “limit exhausted” and "the model did not
        # answer" are different news for the subscriber (see _translation_reason).
        reason = (
            "ok" if show_original else await _translation_reason(db, session["tid"])
        )
        allowed = not show_original and reason == "ok"

    # ‼️ The model call — WITHOUT an open DB session (up to 20 s for a title translation):
    # the connection pool is small, and a dozen parallel presses of “Translate”
    # each holding the session for the duration of the call exhaust the pool and bring the whole cabinet down
    # (CLAUDE.md, analysis of 26.07).
    if allowed:
        tr, usage = await translation.translate_tender(
            tender_id=data.tender_id,
            title=data.title,
            # ‼️ ONLY the title: description=None on purpose. The fragment draws
            # one <a> with the title — the description is not used in the response at all,
            # but it used to go to the model in full (_MAX_INPUT_CHARS = 6000).
            # On real specifications (46–50 KB of raw text) the generation of 6000 characters
            # did not fit into the translate_tender ceiling (20 s) → TimeoutError
            # → None → the card silently stayed in Serbian: "the button gets pressed
            # and does nothing". Translating one title takes 1–2 s.
            description=None,
            redis=redis,
            # The translation language = the client’s interface language (owner’s decision, 25.07).
            target_lang=lang,
        )

    # Session #2 (short) — only if the model was really called (a cache hit does not
    # open it at all). ‼️ A deliberate trade-off: between the limit check (session #1)
    # and the write-off (session #2) there is a window, so parallel translations may
    # exceed the quota by a few units — exact accounting would require a lock across
    # the model call, and that is exactly the connection-holding time we are moving away from.
    # It is cheaper than holding the session for the model call; exact accounting
    # is not in this task.
    if usage is not None and usage.calls > 0:
        async with session_factory() as db:
            await repo.set_tenant(db, session["tid"])
            await _charge_translation(db, session["tid"], usage)
    # No translation happened — we fall back to the original with a note (the button
    # stays: a repeat that hits the cache costs nothing).
    unavailable = not show_original and tr is None
    if view == "feed":
        title_url = data.source_url
    elif view == "detail":
        # On the card itself the title is not a link (there is nowhere to lead), but an
        # h1-like text: title_url="" → the partial draws a <span>.
        title_url = ""
    else:
        title_url = f"/app/tenders/{data.tender_id}?profile_id={data.profile_id}"
    return templates.TemplateResponse(
        request,
        "_card_title.html",
        {
            "tender_id": str(data.tender_id),
            "profile_id": str(data.profile_id),
            "view": view,
            "view_title": tr.title if tr else data.title,
            "title_url": title_url,
            "show_original": show_original or unavailable,
            "unavailable": unavailable,
            "block_reason": None if reason == "ok" else reason,
            "original_lang": _ORIGINAL_LANG,
            **_CARD_TITLE_VIEWS[view],
        },
    )


# --------------------------------------------------------------------------- #
# GET CSV export of the customer analytics (web-ai-5)
# --------------------------------------------------------------------------- #

# Export kinds → the service function that builds the CSV (the same award layer as the sections).
_EXPORT_KINDS = ("customer", "competitor")


@router.get("/app/tenders/{tender_id}/analytics.csv")
async def analytics_csv(
    request: Request,
    tender_id: str,
    profile_id: str | None = None,
    kind: str = "customer",
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    """CSV export of the customer / competitor analytics of the tender (web-ai-5).

    A direct download by link (not HTMX): anonymous → /login (303). ``profile_id`` and a valid ``kind`` are mandatory. The tender’s ownership by the tenant’s profile is checked by the same ``get_tender_analyses`` (None → 404), then the CSV is built from the shared award layer under the Pro+ gate (``set_tenant`` FIRST inside the service). The gate / freeze → a redirect to /plans (CTA); no data / tender → 404. No AI call, no limits — pure SQL.
    """
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return _not_found(request)
    if canon != tender_id:
        return tender_path_redirect(request, tender_id, canon)
    if session is None:
        return RedirectResponse(url="/login", status_code=303)
    if not profile_id or kind not in _EXPORT_KINDS:
        return _not_found(request)
    tid = session["tid"]
    csv_locale = resolve_authed_lang(
        request, getattr(request.state, "cabinet_user_locale", None)
    )

    async with session_factory() as db:
        # The tender’s ownership by the tenant’s profile (RLS + an explicit check) → 404.
        data = await svc.get_tender_analyses(
            db, tid=tid, tender_id=tender_id, profile_id=profile_id
        )
        if data is None:
            return _not_found(request)
        if kind == "customer":
            export = await cust.get_customer_export(
                db, tid=tid, tender_id=tender_id, locale=csv_locale
            )
        else:
            export = await cust.get_competitor_export(
                db, tid=tid, tender_id=tender_id, locale=csv_locale
            )

    if export.status == "success":
        # The file name is ASCII-safe (see _safe_ascii in analytics_export).
        return Response(
            content=export.content,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{export.filename}"'
            },
        )
    # The gate / freeze — we send to the plans (CTA), not a bare 403.
    if export.status in ("gate_pro", "frozen"):
        return RedirectResponse(url="/plans", status_code=303)
    # no_data | no_tender | no_customer — nothing to export.
    return _not_found(request)


# --------------------------------------------------------------------------- #
# POST start of generation (web-ai-2)
# --------------------------------------------------------------------------- #


@router.post("/app/tenders/{tender_id}/generate/{level}", response_class=HTMLResponse)
async def generate(
    request: Request,
    tender_id: str,
    level: str,
    background_tasks: BackgroundTasks,
    profile_id: str | None = None,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    if session is None:
        return _hx_login()
    if level not in gen.GENERATABLE_LEVELS or not profile_id:
        return _not_found(request)
    # POST: 301 won’t do (the browser would repeat the form as a GET) — the canonical is taken here.
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return _not_found(request)
    tender_id = canon
    tid = session["tid"]

    # Synchronous — only the cheap things (gate / cache / limit / existence), WITHOUT a model call.
    async with session_factory() as db:
        status = await gen.precheck_generation(
            db, tid=tid, tender_id=tender_id, profile_id=profile_id, level=level
        )

    # Already there — show the saved artifact at once (no write-off, no background task).
    if status == gen.STATUS_ALREADY:
        return await _render_status(
            request, session_factory, tid, tender_id, profile_id, level, polls=0
        )
    # Already being generated by another request (the advisory lock is taken) — the same UX
    # “generating…”; the polling will pick up the result of the first one. A new background task is NOT
    # started (the first request already started it). The source of the status is the core
    # generate_artifact; the precheck may minimally return it too.
    if status == gen.STATUS_IN_PROGRESS:
        return _slot(
            request,
            level=level,
            tender_id=tender_id,
            profile_id=profile_id,
            mode="pending",
            polls=1,
            status_code=202,
        )
    # The terminal branches — at once the corresponding partial. The refusal BY THE LIMIT additionally
    # flashes a banner with an upsell (owner’s requirement of 15.08.2026): previously the client
    # got the same dim notice as on a model failure and did not understand that the limit was hit,
    # not a glitch. The flash is only here — on the client’s ACTION.
    if status in _STATUS_TO_MODE:
        flash = (
            await _flash_for_level(session_factory, tid, level)
            if status == gen.STATUS_LIMIT_EXHAUSTED
            else None
        )
        return _slot(
            request,
            level=level,
            tender_id=tender_id,
            profile_id=profile_id,
            mode=_STATUS_TO_MODE[status],
            flash=flash,
        )

    # OK — we put a background task with ITS OWN session and return “generating…”.
    background_tasks.add_task(
        _run_generation, session_factory, tid, tender_id, profile_id, level
    )
    return _slot(
        request,
        level=level,
        tender_id=tender_id,
        profile_id=profile_id,
        mode="pending",
        polls=1,
        status_code=202,
    )


async def _run_generation(
    session_factory: Callable,
    tid: str,
    tender_id: str,
    profile_id: str,
    level: str,
) -> None:
    """The background task: ITS OWN session (the route’s session is closed), ``set_tenant`` inside."""
    async with session_factory() as db:
        await gen.generate_artifact(
            db, tid=tid, tender_id=tender_id, profile_id=profile_id, level=level
        )


# --------------------------------------------------------------------------- #
# GET status fragment for HTMX polling (web-ai-2)
# --------------------------------------------------------------------------- #


@router.get("/app/tenders/{tender_id}/status/{level}", response_class=HTMLResponse)
async def generation_status(
    request: Request,
    tender_id: str,
    level: str,
    profile_id: str | None = None,
    polls: int = 0,
    session=Depends(deps.get_optional_session),
    session_factory: Callable = Depends(deps.get_session_factory),
):
    canon = canonical_tender_id(tender_id)
    if canon is None:
        return _not_found(request)
    if canon != tender_id:
        return tender_path_redirect(request, tender_id, canon)
    if session is None:
        return _hx_login()
    if level not in gen.GENERATABLE_LEVELS or not profile_id:
        return _not_found(request)
    return await _render_status(
        request,
        session_factory,
        session["tid"],
        tender_id,
        profile_id,
        level,
        polls=polls,
    )


async def _render_status(
    request: Request,
    session_factory: Callable,
    tid: str,
    tender_id: str,
    profile_id: str,
    level: str,
    *,
    polls: int,
) -> HTMLResponse:
    """The artifact is ready → render the section (stop polling); not yet → pending / timeout."""
    async with session_factory() as db:
        data = await svc.get_tender_analyses(
            db, tid=tid, tender_id=tender_id, profile_id=profile_id
        )
    if data is None:
        return _not_found(request)
    output = data.outputs.get(level)
    if output:
        # The artifact appeared — the final render without hx-trigger (polling stops).
        return _slot(
            request,
            level=level,
            tender_id=tender_id,
            profile_id=profile_id,
            mode="content",
            output=output,
        )
    if polls >= _MAX_POLLS:
        return _slot(
            request,
            level=level,
            tender_id=tender_id,
            profile_id=profile_id,
            mode="timeout",
        )
    # Still not — we continue polling (the next poll: polls+1).
    return _slot(
        request,
        level=level,
        tender_id=tender_id,
        profile_id=profile_id,
        mode="pending",
        polls=polls + 1,
    )
