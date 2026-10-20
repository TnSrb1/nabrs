"""The common Jinja2 renderer of the cabinet (ADR-007: FastAPI + Jinja2 + HTMX + Tailwind)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from src.api.pager import pager_pages, pager_tokens
from src.api.public import news
from src.api.public.i18n import (
    CONTACT,
    CONTACT_MESSAGE_MAX,
    lang_switch_href,
    procedure_label,
    status_label,
)
from src.api.static_assets import static_url
from src.db.models.tender_status import STATUS_LABELS, STATUSES
from src.domain.delivery_cap import daily_cap
from src.domain.description_render import is_docs_only, split_description_blocks
from src.domain.regions import okrug_label
from src.domain.seo_text import clean_seo_text
from src.domain.text_norm import cyr_to_lat_strict
from src.domain.tz import calendar_days_until, to_local
from src.i18n_common import common_t
from src.parsers.capabilities import deadline_has_time
from src.plans.catalog import ENTERPRISE_CONTACT_EMAIL, TG_CHANNEL_URL

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _dt_local(dt: datetime | None) -> str:
    """The moment from the DB → “DD.MM.YYYY HH:MM” by Belgrade time; ``None`` → empty.

    The presentation boundary: the DB time is UTC, here the conversion into
    ``Europe/Belgrade`` (:func:`to_local`). The only way to print a moment with the hours
    in the cabinet templates — a raw ``strftime`` would show UTC time as local.
    """
    local = to_local(dt)
    return local.strftime("%d.%m.%Y %H:%M") if local is not None else ""


def _d_local(dt: datetime | None) -> str:
    """The moment from the DB → “DD.MM.YYYY” (the date by Belgrade time); ``None`` → empty."""
    local = to_local(dt)
    return local.strftime("%d.%m.%Y") if local is not None else ""


def _dl_local(
    dt: datetime | None, source_key: str | None = None, lang: str | None = None
) -> str:
    """The deadline of a tender for the template: with an hour, or the date + a mark — if the source does not give the hour.

    ``source_key`` — the source of the row; for CFKU (``deadline_time=False``) the hour in the deadline is substituted by us (the end of day), we do not show it to the client. ``lang`` — the language of the page for the mark; without it there is no mark. For the other sources — as ``dt_local``.
    """
    if deadline_has_time(source_key):
        return _dt_local(dt)
    date_text = _d_local(dt)
    if not (date_text and lang):
        return date_text
    return f"{date_text} ({common_t(lang, 'deadline_hour_unknown')})"


def _days_left(deadline: datetime | None) -> int | None:
    """The number of CALENDAR days (by Belgrade time) until ``deadline``.

    A presentation helper of the card / the feed: the template decides the accent itself by the sign / size (expired / today / tomorrow / N d.). ``None`` — there is no deadline. ``0`` — the deadline is today and has not passed yet, ``1`` — tomorrow (the rule — :func:`src.domain.tz.calendar_days_until`). An already passed deadline is always a negative number, even if it was this morning: otherwise an expired tender would show as “ističe danas”. Naive datetimes are treated as UTC.
    """
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    days = calendar_days_until(deadline, now)
    if deadline <= now:
        return min(days, -1)
    return days


def _description_html(description: str | None) -> Markup:
    """The description of a tender → safe HTML for the containers with ``white-space:
    pre-wrap`` (the public card, ``tender.html``).

    The headings of the documents (the marker ``### file name``, see :mod:`src.domain.description_render`) are rendered as an inline ``<strong>``, not as a block element — the container uses ``-webkit-line-clamp`` to collapse the long text, and this technique counts the lines of the inline flow and breaks on nested block children. The line breaks are literal ``\\n`` (text nodes), which ``pre-wrap`` shows as a line break, without introducing new markup elements. The file name is escaped — it comes from the portal, do not apply `safe` to it.
    """
    blocks = split_description_blocks(description)
    if not blocks:
        # Empty or only service markers: the raw text is NOT given out (otherwise `# Sheet1` would go to the client).
        return Markup("")
    parts: list[str] = []
    for block in blocks:
        if block.heading:
            if parts:
                parts.append("\n\n")
            parts.append(
                f'<strong class="tp-desc__doc-title">{escape(block.heading)}</strong>'
            )
            if block.text:
                parts.append("\n")
        parts.append(str(escape(block.text)))
    return Markup("".join(parts))


def _auth_context(request: Request) -> dict:
    """The global authorization flags for all templates (SITE-02).

    It decodes the session cookie by the same contract as :func:`src.api.deps.get_optional_session` (the HMAC parsing, does not touch the DB). It is needed by the base template so that the cabinet navigation is shown only to a logged-in user, and “Login / Sign up” to an anonymous one. Previously the cabinet items (Profiles / Library / Logout) leaked to the anonymous user on /login and /signup.

    ``is_platform_admin`` — the operator flag of the platform for the “Admin” link in the header. The resolution requires the DB (the session carries only uid / tid), so it is done by the async dependency :func:`src.api.deps.resolve_platform_admin_flag` and put into ``request.state``; here — only the reading. The absence of the flag (the page rendered bypassing the dependency, for example by the 404 handler) → no link.

    The imports are lazy — so that the templates module does not pull in config / security at an early import by the routers.
    """
    from src.api import security
    from src.api.cabinet.i18n import cabinet_context, cabinet_context_for_lang
    from src.api.config import get_cabinet_settings
    from src.api.public.i18n import UI
    from src.api.public.i18n import resolve_lang as public_resolve_lang
    from src.api.public.i18n import resolve_page_lang

    settings = get_cabinet_settings()
    raw = request.cookies.get("tb_session")
    session = security.verify_session(
        raw, settings.cabinet_secret_key, max_age=settings.session_max_age
    )
    ctx: dict = {
        "authed": session is not None,
        # The CSRF token of the cabinet’s POST forms (logout, account deletion); for an anonymous user "".
        "csrf_token": security.csrf_token(session, settings.cabinet_secret_key),
        "is_platform_admin": bool(getattr(request.state, "is_platform_admin", False)),
        # The session identification for the header (whoami, owner 20.09.2026) — puts
        # deps.resolve_cabinet_whoami; anonymous / a resolution without a marker → both None,
        # then the template does not draw the identity row.
        "whoami_email": getattr(request.state, "cabinet_whoami_email", None),
        "whoami_tenant_name": getattr(
            request.state, "cabinet_whoami_tenant_name", None
        ),
        "access_frozen": bool(getattr(request.state, "cabinet_access_frozen", False)),
        "nav_access": getattr(request.state, "cabinet_nav_access", None),
        "extension_pending": bool(
            getattr(request.state, "cabinet_extension_pending", False)
        ),
    }
    # The cabinet i18n chrome (Phase 1.1). We inject it ONLY on the cabinet renders — they are marked by
    # the dependency deps.resolve_cabinet_locale, which sets request.state.
    # cabinet_user_locale (see its docstring). The public routes do not carry this dependency →
    # there is no marker → we do NOT touch their `t` / `lang` (the showcase passes its own
    # catalog `t` from public.i18n, and the processor in Starlette overrides the context
    # of the route — if we injected `t` unconditionally, the public pages would break).
    if hasattr(request.state, "cabinet_user_locale"):
        # ‼️ A route with ``?lang=`` in the role of ANOTHER axis (the translation of the tender content:
        # sr = the original / ru = the machine translation — the htmx fragments /title and /description)
        # sets ``request.state.cabinet_ignore_query_lang``: then the chrome of the fragment
        # resolves WITHOUT the query and follows the interface language of the user, rather than
        # the translation toggle (do not mix the two axes — CLAUDE.md).
        include_query = not getattr(request.state, "cabinet_ignore_query_lang", False)
        user_locale = request.state.cabinet_user_locale
        if user_locale is None and getattr(
            request.state, "cabinet_anon_public_lang", False
        ):
            # HIGH-2 (audit 2026-08-02): the anonymous login screens (signup / login /
            # resend) resolve the language by the contour of the SHOWCASE — ?lang= → cookie →
            # Accept-Language → sr, and not by the cabinet fallback ru. The client comes
            # here directly from the public pages, and the default must match. The fallback
            # is stated EXPLICITLY by this resolver (CLAUDE.md §i18n), and not by the module
            # DEFAULT of the cabinet. The string catalog is the cabinet one; only the language changes.
            # The marker is set by deps.use_public_lang_for_anon, hanging on these routes.
            cab = cabinet_context_for_lang(public_resolve_lang(request))
        else:
            cab = cabinet_context(request, user_locale, include_query=include_query)
        ctx["t"] = cab["t"]
        ctx["lang"] = cab["lang"]
        ctx["lang_names"] = cab["lang_names"]
        # The login screens render in the frame of the SHOWCASE (public_base.html, F-10): the header
        # and the footer read the `ui` of the showcase in the same language as `t`. For a logged-in user the
        # header is the cabinet one (_cabinet_nav), which needs the cabinet catalog `nav_t`.
        ctx["ui"] = UI[cab["lang"]]
        ctx["nav_t"] = cab["t"]
    elif session is not None:
        # The showcase page of a logged-in user: `t` / `lang` — the route’s catalog, must not be touched,
        # while the cabinet header (_cabinet_nav.html) needs the cabinet catalog. We put
        # it SEPARATELY (`nav_t`) at the language of the page — the menu strings are not duplicated.
        ctx["nav_t"] = cabinet_context_for_lang(resolve_page_lang(request))["t"]
    # The “Novo” mark and the banner of the dashboard: the freshest news item, if it is newer than the
    # “read” cookie (news_seen). Only for the logged-in user; we do not touch the DB.
    if session is not None:
        ctx["news_unread"] = news.latest_unread(request.cookies.get(news.SEEN_COOKIE))
    return ctx


#: Everything that goes back to the server stays as in the DB (the owner’s review of 10.10.2026
#: PR #647): ``value=`` of any tag (the customer name ``customers_include``
#: is compared letter by letter; ``<option value>`` — the code / key), ``data-*`` (except
#: the labels that the user sees), and the CONTENT of ``<textarea>`` — from it
#: the profile’s keywords are saved: the word for “gravel” → “ščeben” would glue
#: ``keyword_expand`` and rewrites the Cyrillic in the DB. The visible text (labels,
#: the text of ``<option>``) is transliterated as before.
_TAG_RE = re.compile(r"<[a-zA-Z][^<>]*>")
_ATTR_KEEP_RE = re.compile(
    r'(\s(?:value|data-(?!confirm\b|nothing-found\b|[a-z-]*label\b)[a-z-]+)=")([^"]*)(")'
)
_TEXTAREA_RE = re.compile(r"(<textarea\b[^>]*>)(.*?)(</textarea>)", re.S)


def _sr_latin_html(html: str) -> str:
    """The whole page → Latin, except the values returned to the server."""
    kept: list[str] = []

    def _stash_text(text: str) -> str:
        kept.append(text)
        return f"\x00{len(kept) - 1}\x00"

    def _stash_attrs(m: re.Match[str]) -> str:
        return _ATTR_KEEP_RE.sub(
            lambda a: a.group(1) + _stash_text(a.group(2)) + a.group(3), m.group(0)
        )

    html = _TEXTAREA_RE.sub(
        lambda m: m.group(1) + _stash_text(m.group(2)) + m.group(3), html
    )
    out = cyr_to_lat_strict(_TAG_RE.sub(_stash_attrs, html))
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], out)


class _SrLatinTemplate(jinja2.Template):
    """A template that at ``lang == "sr"`` gives the page ONLY in Latin.

    owner 10.10.2026: Serbian everywhere in Latin. The data in the DB (headings, customers, the CPV reference, districts) lie in Serbian Cyrillic, therefore the transliteration is at the display boundary — with one mechanism for the WHOLE output of the template (including macros, includes and HTMX fragments), and not a filter in each place. ``ru`` / ``en`` are not touched: ru sees the Cyrillic as is.
    """

    def render(self, *args: object, **kwargs: object) -> str:
        out = super().render(*args, **kwargs)
        ctx = dict(*args, **kwargs)  # type: ignore[call-overload]
        return _sr_latin_html(out) if ctx.get("lang") == "sr" else out


templates = Jinja2Templates(
    directory=str(_TEMPLATES_DIR), context_processors=[_auth_context]
)
templates.env.template_class = _SrLatinTemplate
templates.env.filters["days_left"] = _days_left
# The conversion of a moment from the DB (UTC) into local time (Europe/Belgrade) — the presentation
# boundary. `dt_local` — with the hours, `d_local` — the date only, `localtime` —
# returns an aware datetime for a non-standard strftime (the agenda “%d.%m” + “%H:%M”,
# the delivery date “%d.%m”). A raw strftime in the cabinet templates would print UTC.
templates.env.filters["dt_local"] = _dt_local
templates.env.filters["d_local"] = _d_local
# `dl_local` — the deadline of a tender taking the source into account (for CFKU — the date and a mark, without the hour).
templates.env.filters["dl_local"] = _dl_local
templates.env.filters["dl_has_time"] = deadline_has_time
# The mark “hour not specified” separately from the deadline — for templates where the deadline is printed
# otherwise (the agenda: the date in a narrow column, the mark — on the line below).
templates.env.globals["common_t"] = common_t
# The daily delivery limit (Telegram / email) for the texts: “{cap}”, not a literal.
templates.env.globals["delivery_daily_cap"] = daily_cap
templates.env.filters["localtime"] = to_local
# The “Contact us” form (the partial _contact_form.html): the catalog of strings by the language of the
# page and the contact address — ONE constant (src/plans/catalog.py), not a literal.
templates.env.globals["contact_ui"] = lambda lang: CONTACT[lang]
templates.env.globals["contact_email"] = ENTERPRISE_CONTACT_EMAIL
templates.env.globals["tg_channel_url"] = TG_CHANNEL_URL
templates.env.globals["contact_message_max"] = CONTACT_MESSAGE_MAX
templates.env.globals["pager_pages"] = pager_pages
templates.env.globals["pager_tokens"] = pager_tokens

# The machine codes of tenders_normalized (region / status / procedure type) → human
# labels (the public tender card, web-ux-6). okrug_label — the SSOT reference of the
# districts (src/domain/regions.py, in Serbian Cyrillic in all locales —
# we do not invent a translation); status_label / procedure_label — from public.i18n,
# lang as the second argument of the filter ({{ value | status_label(lang) }}).
templates.env.filters["okrug_label"] = okrug_label
templates.env.filters["status_label"] = status_label
templates.env.filters["procedure_label"] = procedure_label

# The service marker of the documents (### file name), which the parser inserts between
# the texts of the specification documents — we render it as the document heading, not as text
# (CLAUDE.md: the client must not see our separator). A common filter for both
# display places of the description (the cabinet and the public card) — not two copies of the parsing.
templates.env.filters["description_blocks"] = split_description_blocks
templates.env.filters["description_html"] = _description_html
templates.env.filters["description_docs_only"] = lambda d: is_docs_only(
    split_description_blocks(d)
)

# GEO / SEO: removes the Excel artifact _x000D_ from title / customer_name at the render
# (src/domain/seo_text — we do not touch ingest; see the docstring of the module).
templates.env.filters["clean_seo"] = clean_seo_text


def _tojson_ld(value: object) -> Markup:
    """JSON for ``<script type="application/ld+json">`` — not HTML-escape.

    ``</`` is escaped into ``<\\/``, so that a value with this substring does not
    close the script tag early (XSS safety of the inline JSON-LD).
    """
    return Markup(json.dumps(value, ensure_ascii=False).replace("</", "<\\/"))


templates.env.filters["tojson_ld"] = _tojson_ld

# The funnel stages of the tender (VG-17w) — as a global, and not through the context of each route:
# the stage switch lives in the partial `_tender_actions.html`, which is included by
# the feed, the card and “My tenders”. Passing the same list through
# three contexts — three places where it would be forgotten. The order and the labels — from the model
# (`STATUSES` / `STATUS_LABELS`), the single source of the funnel.
templates.env.globals["stage_choices"] = [(s, STATUS_LABELS[s]) for s in STATUSES]


#: The static preview image for the social networks (1200×630, brand colors). The source —
#: ``scripts/shots/og_default.html``, the render ``scripts/shots/og_render.js``.
OG_IMAGE_PATH = "og/default.png"


def _og_image_url() -> str:
    """The absolute URL of og:image — from ``CABINET_BASE_URL`` (behind the uvicorn proxy it sees
    http, so not ``request.url_for``; the same reason as for the canonical)."""
    from src.api.config import get_cabinet_settings

    return get_cabinet_settings().cabinet_base_url.rstrip("/") + static_url(
        OG_IMAGE_PATH
    )


templates.env.globals["og_image_url"] = _og_image_url
#: The static address with ``?v=<hash>`` — in ALL templates instead of the literal ``/static/…``
#: (otherwise the browser keeps the old file with a new SRI; TASK static-cache).
templates.env.globals["static_url"] = static_url

# The defaults of the cabinet’s i18n chrome (Phase 1.1) as the ENVIRONMENT GLOBALS: base.html accesses
# `t.*` / `lang` / `lang_names`, and Jinja of this environment falls over on the attribute access of an
# undefined `t`. On live cabinet requests they are overridden by the context processor
# `_auth_context` (the user’s language); on the public ones — by their own catalog `t` from the route.
# The globals are needed for the renders BYPASSING the processor — isolated tests of the partials
# (`templates.get_template(...).render(...)`) and any forgotten-context path: the chrome
# then degrades to the default language, rather than breaking the page with a 500.
from src.api.cabinet import i18n as _cabinet_i18n  # noqa: E402 — after templates

templates.env.globals["t"] = {
    **_cabinet_i18n.COMMON[_cabinet_i18n.DEFAULT],
    **_cabinet_i18n.CABINET[_cabinet_i18n.DEFAULT],
}
templates.env.globals["lang"] = _cabinet_i18n.DEFAULT
templates.env.globals["lang_names"] = _cabinet_i18n.LANG_NAMES
templates.env.globals["lang_switch_href"] = lang_switch_href

# The pluralisation by locale (the cockpit, Phase 1.2) — as an environment global: the template calls
# `plural(lang, key, n)`; the forms and the selection rule live in the cabinet.i18n catalog
# (the Slavic three-form for ru / sr, the two-form for en). The global is visible also inside the
# imported macros (unlike the context variables), therefore
# `_agenda_item.html` can call it directly.
templates.env.globals["plural"] = _cabinet_i18n.plural

# How many agenda items are visible without expansion — as a global for the same reason as
# `stage_choices`: the slicing is used by TWO blocks of the dashboard (the calendar and "do
# now"), and a number passed through the route’s context is the place where it would be
# forgotten at a third block. Plus the isolated renders of the partials in the tests: without the
# global `calendar_events[:limit]` would fail on the slice by Undefined.
from src.api.cabinet.feed_service import (  # noqa: E402 — after templates
    AGENDA_PREVIEW_LIMIT as _AGENDA_PREVIEW_LIMIT,
)

templates.env.globals["agenda_preview_limit"] = _AGENDA_PREVIEW_LIMIT
