"""Telegram bot (aiogram 3) — ARCHITECTURE §9, §12, ADR-006.

Sprint-15: the FSM onboarding + the CRUD of the subscriber's profile.
Chat 26 (T3): the deep-link binding cabinet↔bot (C-04) — ``/start <token>``.
Chat 38+: the buttons of the tender status funnel on the card (a port of v6 tracker_bot).
STAGE 3: the on-demand L3 “🧠 Deep analysis” — callback ``ai:deep:{tender_id}``.

Implemented in this sprint:
  * the FSM onboarding (:class:`OnboardingFSM`): language → keywords → region →
    confirmation.
  * saving the subscriber on confirmation (upsert, one transaction): a new
    user → we create ``tenants`` + ``users`` + ``user_channels`` +
    ``profiles``; an existing one (after /reset) → we update the profile in place.
  * /profile — show the current settings (a read from the DB).
  * /reset — restart the onboarding; the settings are overwritten in place,
    NOTHING is deleted (owner's decision, Sprint-15 chat).
  * handling of duplicates on /start.

Chat 26 — the cabinet↔bot stitching (C-04):
  * ``/start <token>`` with a deep-link payload: we spend the one-time ``channel_bind``
    token (created by the cabinet for its ``user_id``, see
    :mod:`src.api.cabinet.channel_service`) and create/update
    ``user_channels(channel='telegram')`` for the SAME ``user_id``. So the subscriber
    from the web cabinet gets a binding that the delivery reads.
  * ``/start`` without a payload — the former behavior (a profile exists → a hint; none → the
    FSM onboarding), nothing breaks.

The funnel of the tender statuses (a port of v6 tracker_bot):
  * The keyboards by stage (:func:`keyboard_for_status`) and the labels
    (:data:`STATUS_LABELS`) for the buttons on the delivered tender card.
  * The callback handler :func:`on_tender_status` (``st:{status}:{tender_id}``):
    UPSERT into ``tender_user_status`` (migration 0008) via
    :mod:`src.bots.status_service` (the RLS SET LOCAL inside the service), a toast with the
    label, a redraw of the keyboard + the status line.
  * :func:`build_tender_keyboard` — the keyboard of stage 'new' for the delivery
    (wired in :mod:`src.workers.adapters.telegram`).

The on-demand L3 “🧠 Deep analysis” (STAGE 3):
  * The callback handler :func:`on_deep_analysis` (``ai:deep:{tender_id}``):
    an immediate callback.answer() BEFORE the model call (anti-staleness 10 s),
    then :func:`src.bots.deep_analysis_service.run_deep_analysis`.
    The button on the card — in :mod:`src.workers.adapters.telegram`.
    The Pro+ gate and the cache — inside deep_analysis_service (on the press, not on the formatting).

The freeze of the subscription (FIX-SG-04, Chat 60):
  * A frozen tenant (``access_until`` expired OR a manual revoke) cannot
    change the funnel status (``st:``) and does not get the deep analysis (``ai:deep:``):
    the buttons answer with the refusal “extend the access in the cabinet”, opus is NOT called,
    the status is NOT written. The single predicate — :func:`src.domain.access.is_frozen`
    (SSOT), the freeze check of the ``st:`` buttons — :func:`is_tenant_frozen`;
    for ``ai:deep:`` the check is inside
    :func:`src.bots.deep_analysis_service.run_deep_analysis` (``status='frozen'``).
    The cabinet is NOT blocked by the freeze (the extension of the subscription is available).

The mapping “brief → schema” (DB_SCHEMA v1.1, ADR-033). In the brief the fields are named
conditionally; the source of truth is the ORM models. The correspondence:
  * telegram_id      -> ``user_channels.external_id`` (``channel='telegram'``).
  * language         -> ``users.locale`` + ``profiles.target_language``.
  * country_code/tier-> live on ``tenants`` (NOT NULL parent FK for users/
    profiles), here ``country_code='RS'``, ``tier='lite'`` (the internal canon, R8).
  * keyword_filter   -> ``profiles.keywords_include`` (JSONB ``{"keywords":[]}``.
  * region_codes     -> ``profiles.regions`` (ARRAY(Text)).

What it does NOT do (separate sprints):
  * Billing/subscriptions (``subscriptions``) — a later sprint.
  * Deadline reminders and the funnel summary — separate PRs.
  * Wiring the webhook into FastAPI (``src/api/main.py``) — Track F, Sprint-17.

The UI language of the replies — Russian only. The sr/en languages are saved in the profile, but the
interface texts for them are stubs (i18n_todo, localization later, ARCHITECTURE §12.1).

No network calls on importing the module: Bot is instantiated only inside the
factories/at the start of polling; the DB is touched only inside the handlers.

Local start (polling):
    TG_BOT_TOKEN=<token> DATABASE_URL=<url> python -m src.bots.telegram
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select, text

from src.api.cabinet import source_banner
from src.api.security import hash_token  # SHA-256 of the one-time token (stdlib-only)
from src.bots.sr_latin import SrLatinMiddleware, bot_locale
from src.bots.tg_html import split_tg_html
from src.bots.card_stash import drop_card, load_card, stash_card
from src.bots.config import BotSettings, get_bot_settings
from src.bots.i18n import BOT, SUPPORTED, bot_t, common_t, norm_locale
from src.bots.trial_handlers import trial_router
from src.bots.status_service import (
    clear_tender_status,
    get_status_summary,
    get_tender_status,
    is_tenant_frozen,
    resolve_tenant_id,
    set_tender_status,
)
from src.db.models import AuthToken, Profile, Tenant, User, UserChannel
from src.i18n_common import COMMON
from src.db.session import session_scope, set_current_tenant
from src.domain.profile_channels import adopt_channel_into_empty_profiles
from src.domain.access import is_frozen
from src.domain.delivery_cap import daily_cap
from src.domain.regions import okrug_label
from src.domain.tiers import effective_tier
from src.domain.sources import (
    SourceInfo,
    apply_source_selection,
    default_source_keys_for_new_profile,
    list_sources_for_selection,
)
from src.bots import manage, source_choice
from src.plans.catalog import level_limit_for_plan, tier_to_plan_key, upsell_for_level

logger = logging.getLogger(__name__)

router = Router(name="telegram-commands")

# The purpose of the cabinet’s bind token (migration 0007). See channel_service.
CHANNEL_BIND_PURPOSE = "channel_bind"

# The text of the polite refusal in token_only mode (BOT_SIGNUP_MODE) and the “expired” line
# for the gates of the card buttons — in the chrome catalog (``bot_t(locale, ...)``): the keys
# ``signup_closed`` and ``frozen``. The AI/analytics handlers (``ai:*``/``an:*``) from
# phase 2c take ``frozen`` by ``users.locale`` directly — the ru fallback is removed.

# Bootstrap: called before the GUC is set — SECURITY DEFINER (0021) bypasses RLS.
_RESOLVE_TENANT_SQL = text("SELECT app_resolve_tenant_id(:eid, 'telegram')")
# Resolution of the tenant by user_id — for bind_channel_by_token (C-04, SECURITY DEFINER 0022).
_RESOLVE_TENANT_BY_USER_SQL = text("SELECT app_resolve_tenant_id_by_user_id(:uid)")

# --------------------------------------------------------------------------- #
# VG-31: the command menu (setMyCommands) + /help. The list is CHECKED against the real
# @router.message(Command(...)) handlers below, nothing is invented.
# Owner’s decision 30.09.2026: the menu — sr by default (without language_code = the client’s language,
# which is not in the sets below), plus the sets ``language_code=ru|en``. setMyCommands
# is keyed by the language of the TELEGRAM CLIENT (language_code), not by our axis
# users.locale — these are DIFFERENT axes, and a per-locale menu by users.locale Telegram does not
# support at all. The bot’s locale goes into the text of the handlers' replies
# (resolve_bot_locale + bot_t), not into the global menu; the same after /language.
# --------------------------------------------------------------------------- #
_PUBLIC_COMMANDS: list[tuple[str, str]] = [
    ("start", "Регистрация и настройка шаблона поиска"),
    ("help", "Список команд и возможностей бота"),
    ("profile", "Показать текущие настройки шаблона"),
    ("reset", "Перенастроить шаблон заново"),
    ("watching", "Подписки на победителей (🔔 Следить)"),
    ("list", "Мои тендеры: в работе и последние присланные"),
    ("status", "Статус подписки и тарифа"),
    ("language", "Язык сообщений бота в этом чате"),
    ("izvori", "Источники тендеров (порталы) шаблона"),
    ("pause", "Пауза и включение шаблонов"),
]
# The menu descriptions in sr/en (ru — in ``_PUBLIC_COMMANDS``/``_ADMIN_COMMANDS`` above:
# the tests of the single term are tied to them). Keys = the command names; the parity is under test.
_COMMAND_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "sr": {
        "start": "Registracija i podešavanje šablona pretrage",
        "help": "Spisak komandi i mogućnosti bota",
        "profile": "Prikaži trenutna podešavanja šablona",
        "reset": "Ponovo podesi šablon",
        "watching": "Praćenje pobednika (🔔 Prati)",
        "list": "Moji tenderi: u radu i poslednji poslati",
        "status": "Status pretplate i plana",
        "language": "Jezik poruka bota u ovom četu",
        "izvori": "Izvori tendera (portali) šablona",
        "pause": "Pauza i uključivanje šablona",
        "trials": "Zahtevi za probni pristup (samo admin)",
    },
    "en": {
        "start": "Sign up and set up a search template",
        "help": "List of commands and bot features",
        "profile": "Show current template settings",
        "reset": "Reconfigure the template from scratch",
        "watching": "Winner subscriptions (🔔 Watch)",
        "list": "My tenders: in progress and recently sent",
        "status": "Subscription and plan status",
        "language": "Bot message language in this chat",
        "izvori": "Tender sources (portals) of the template",
        "pause": "Pause and resume templates",
        "trials": "Trial access requests (admin only)",
    },
}
# /trials — only for ADMIN_CHAT_ID (see src.bots.trial_handlers), it does not get into the common
# menu — ordinary users do not need it and it does not work for them.
_ADMIN_COMMANDS: list[tuple[str, str]] = [
    ("trials", "Заявки на тестовый доступ (только админ)"),
]


async def register_bot_commands(bot: Bot, settings: BotSettings | None = None) -> None:
    """Registers the Telegram command menu (``setMyCommands``, VG-31 item 1).

    The default scope — the public commands, visible to all users. If
    ``ADMIN_CHAT_ID`` is set, the extended set (+ ``/trials``) is additionally registered for this chat
    via ``BotCommandScopeChat`` — ordinary users do not see /trials in the autocomplete.
    """
    settings = settings or get_bot_settings()

    def _menu(lang: str, *, admin: bool) -> list[BotCommand]:
        ru_pairs = (*_PUBLIC_COMMANDS, *(_ADMIN_COMMANDS if admin else ()))
        if lang == "ru":
            return [BotCommand(command=c, description=d) for c, d in ru_pairs]
        texts = _COMMAND_DESCRIPTIONS[lang]
        return [BotCommand(command=c, description=texts[c]) for c, _ in ru_pairs]

    # sr — by default (without language_code), then the explicit sets ru/en.
    for lang, code in (("sr", None), ("ru", "ru"), ("en", "en")):
        await bot.set_my_commands(
            _menu(lang, admin=False),
            scope=BotCommandScopeDefault(),
            language_code=code,
        )

    if settings.admin_chat_id:
        for lang, code in (("sr", None), ("ru", "ru"), ("en", "en")):
            await bot.set_my_commands(
                _menu(lang, admin=True),
                scope=BotCommandScopeChat(chat_id=settings.admin_chat_id),
                language_code=code,
            )


# --------------------------------------------------------------------------- #
# The FSM states and the reference data of the onboarding.
# --------------------------------------------------------------------------- #


class OnboardingFSM(StatesGroup):
    """The states of the subscriber’s onboarding (ARCHITECTURE §12)."""

    waiting_language = State()
    waiting_keywords = State()
    waiting_region = State()
    waiting_confirmation = State()


# The supported interface languages. The UI texts are only ru for now; sr/en partially.
# SITE-26: the service label “(i18n_todo)” was removed from the production labels — the
# end user saw it in the onboarding. The localization status — in ARCHITECTURE §12.1.
_LANGUAGES: dict[str, str] = {
    "ru": "Русский",
    "sr": "Srpski",
    "en": "English",
}

# “RS-All” — the synthetic option “all regions”: an empty regions filter.
# SITE-14: the web form offers the districts again (the NUTS→district blocker was removed, #364), while
# the bot’s wizard — for now only “All regions”: choosing a district in the bot is a separate
# keyboard of 30 buttons, a task of its own. But the bot MUST show ANY code,
# including one set from the cabinet — otherwise a profile with a region would be read as “RS-05”.
# Therefore the labels are taken from the common reference, and the wizard’s list of options is its own.
_REGION_OPTIONS: list[tuple[str, str]] = [
    ("RS-All", "Все регионы"),
]

_ALL_REGIONS = "RS-All"


def _region_label(code: str, locale: str | None = None) -> str:
    if code == _ALL_REGIONS:
        return bot_t(locale, "all_regions")
    return okrug_label(code)


# --------------------------------------------------------------------------- #
# Inline keyboards.
# --------------------------------------------------------------------------- #


def _language_keyboard(prefix: str = "ob:lang:") -> InlineKeyboardMarkup:
    """The choice of the language. ONE keyboard for both entry points, only the prefix differs.

    ``ob:lang:`` — a step of the onboarding (caught ONLY in the FSM state
    ``waiting_language``), ``lang:set:`` — the /language command outside the onboarding.
    Different prefixes, and not a shared one — so that the handlers do not catch each other's
    callbacks by the order of registration: /language writes the DB, the onboarding step does not.
    """
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}{code}")]
        for code, label in _LANGUAGES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _region_keyboard(locale: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_region_label(code, locale), callback_data=f"ob:region:{code}"
            )
        ]
        for code, _label in _REGION_OPTIONS
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard(locale: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "onb_confirm_btn"), callback_data="ob:confirm"
                ),
                InlineKeyboardButton(
                    text=bot_t(locale, "onb_restart_btn"), callback_data="ob:restart"
                ),
            ]
        ]
    )


# --------------------------------------------------------------------------- #
# The format of keywords_include (DB_SCHEMA §2.6).
# The matcher (:func:`src.workers.matching._include_groups` / ``keywords_match``) expects
# a LIST of groups ``[{"all_of": [...]}, ...]`` (OR between groups, AND inside
# ``all_of``). The onboarding — the semantics “any of the entered words” → each word goes into
# its own group. The old format ``{"keywords": [...]}`` the matcher did NOT understand — the words
# of the onboarding did not filter the tenders at all.
# --------------------------------------------------------------------------- #


def _keywords_to_groups(keywords: list[str]) -> list[dict]:
    """The list of the onboarding words → the matcher format ``[{"all_of": [w]}]`` (OR by the words).

    Empty input → ``[]`` (there is no filter by words — any tender passes).
    """
    return [{"all_of": [w]} for w in keywords if w]


def _keywords_to_words(keywords_include) -> list[str]:
    """A flat list of words for display from ANY format of ``keywords_include``.

    It understands: the new format — a list of groups ``[{"all_of": [...]}]``; the “raw”
    list of words from the FSM state (``[“w1”, “w2”]``); the old dict
    ``{"keywords": [...]}``; ``None``/empty. The order is kept, duplicates
    are collapsed.
    """
    if not keywords_include:
        return []
    if isinstance(keywords_include, dict):
        raw = keywords_include.get("keywords") or []
    elif isinstance(keywords_include, list):
        raw = []
        for item in keywords_include:
            if isinstance(item, dict):
                raw.extend(item.get("all_of") or [])
            elif isinstance(item, str):
                raw.append(item)
    else:
        return []
    words: list[str] = []
    for w in raw:
        if w and w not in words:
            words.append(w)
    return words


def _build_summary(data: dict, locale: str | None = None) -> str:
    """The summary text for the confirmation step (the chrome by ``locale``; the values are data)."""
    lang = data.get("language", "ru")
    keywords = _keywords_to_words(data.get("keywords", []))
    region = data.get("region", _ALL_REGIONS)
    region_str = _region_label(region, locale)
    return bot_t(locale, "onb_summary").format(
        lang=_LANGUAGES.get(lang, lang),
        keywords=html.escape(", ".join(keywords)),
        region=region_str,
    )


# --------------------------------------------------------------------------- #
# The funnel of the tender statuses (a port of v6 tracker_bot — the buttons on the card).
# tender_user_status (migration 0008): 7 statuses. Here — the UI labels (a port of v6
# STATUS_LABELS), the keyboards by stage and the funnel transitions. The write to the DB — in
# src.bots.status_service; the handling of presses — on_tender_status (below).
# --------------------------------------------------------------------------- #

# The status labels for the toast and the “Status: …” line (a port of v6 STATUS_LABELS).
# The keys match STATUSES of the model tender_user_status (the 7 values of the funnel).
STATUS_LABELS: dict[str, str] = {
    "new": "🆕 Новый",
    "watching": "👀 Изучаю",
    "active": "✅ Участвую",
    "submitted": "📨 Заявка подана",
    "won": "🏆 Выиграли",
    "lost": "😔 Проиграли",
    "skipped": "❌ Пропущен",
}


def _status_button(
    action_label: str, new_status: str, tender_id: str
) -> InlineKeyboardButton:
    """The funnel transition button: callback ``st:{new_status}:{tender_id}``."""
    return InlineKeyboardButton(
        text=action_label, callback_data=f"st:{new_status}:{tender_id}"
    )


def _kb_new(tender_id: str, locale: str | None = None) -> InlineKeyboardMarkup:
    """Stage 'new' (and 'watching'): 3 buttons in ONE row (a port of v6)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _status_button(bot_t(locale, "act_participate"), "active", tender_id),
                _status_button(bot_t(locale, "act_review"), "watching", tender_id),
                _status_button(bot_t(locale, "act_skip"), "skipped", tender_id),
            ]
        ]
    )


def _kb_active(tender_id: str, locale: str | None = None) -> InlineKeyboardMarkup:
    """After 'active': bid submitted / withdraw (a port of v6)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _status_button(bot_t(locale, "act_submitted"), "submitted", tender_id),
                _status_button(bot_t(locale, "act_decline"), "skipped", tender_id),
            ]
        ]
    )


def _kb_submitted(tender_id: str, locale: str | None = None) -> InlineKeyboardMarkup:
    """After 'submitted': won / lost (a port of v6)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                _status_button(bot_t(locale, "act_won"), "won", tender_id),
                _status_button(bot_t(locale, "act_lost"), "lost", tender_id),
            ]
        ]
    )


def keyboard_for_status(
    status: str, tender_id: str, locale: str | None = None
) -> InlineKeyboardMarkup | None:
    """The funnel keyboard for the current status (a port of v6).

    new/watching → 3 buttons (I'm in / study / skip); active → bid/withdraw;
    submitted → won/lost; won/lost/skipped → ``None`` (terminal —
    there are no buttons). 'watching' shows the same keyboard as 'new': the subscriber
    can still move to participation or skip (as in v6).

    ``locale`` controls ONLY the button labels (the chrome); ``callback_data`` and
    the funnel statuses do not depend on the language. The default (``None``) → ru — both for
    the interactive bot and for the delivery path (:func:`build_tender_keyboard`,
    the recipient's language = ``DeliveryJob.locale``).
    """
    if status in ("new", "watching"):
        return _kb_new(tender_id, locale)
    if status == "active":
        return _kb_active(tender_id, locale)
    if status == "submitted":
        return _kb_submitted(tender_id, locale)
    return None  # won / lost / skipped — terminal


def build_tender_keyboard(
    tender_id: str, status: str = "new", locale: str | None = None
) -> InlineKeyboardMarkup | None:
    """The funnel keyboard for a delivered tender (by default the stage 'new').

    Used by the Telegram delivery adapter to hang the 3 funnel buttons on
    each delivered tender (see :mod:`src.workers.adapters.telegram`).
    ``locale`` — the interface language of the recipient (``DeliveryJob.locale`` =
    ``users.locale``), passed into :func:`keyboard_for_status`: it affects
    ONLY the button labels. ``None``/an unknown code → ru.
    """
    return keyboard_for_status(status, tender_id, locale)


# --------------------------------------------------------------------------- #
# The CARD keyboard: ≤2 rows of ≤3 buttons. The AI buttons are hidden behind “🧠 AI analysis ▾”.
# --------------------------------------------------------------------------- #

# The cabinet (REGISTRY §2, the same domain as ``_UPSELL_URL``). The bot’s container has no
# ``CABINET_BASE_URL``, so it is a constant, not a config; the delivery worker takes its
# base from ENV (``TelegramAdapter``).
CABINET_BASE_URL = "https://app.nab.rs"

# ai:menu:{tender_id}:{full} — expand the AI variants; ai:back:{tender_id}:{full} —
# collapse back. ``full`` = 1 if the card has a profile (then the
# verdict/checklist/draft are available, as before). 8+36+2 = 46 bytes (≤64).
_AI_MENU_PREFIX = "ai:menu:"
_AI_BACK_PREFIX = "ai:back:"
# The callback_data of the buttons that live ONLY in the expanded AI menu.
_AI_EXPANDED_PREFIXES = ("ai:deep:", "ai:bid:", "ai:chk:", "ai:drf:", "an:cust:")
# cd:show:{tender_id} — “↩ Restore” the collapsed card (3+5+36 = 44 bytes).
_CARD_RESTORE_PREFIX = "cd:show:"


def cabinet_tender_url(base: str, tender_id: str, profile_id: str) -> str:
    """The URL of the tender card in the cabinet (the route ``/app/tenders/{id}?profile_id=``).

    An anonymous user gets ``/login?next=<this path>`` and returns here after the login.
    """
    return f"{base.rstrip('/')}/app/tenders/{tender_id}?profile_id={profile_id}"


def _ai_reply_chunks(
    html_text: str, locale: str | None, tender_id: str, profile_id: str | None
) -> list[str]:
    """The AI reply (verdict/checklist/draft) → Telegram messages ≤4096 (#634).

    A long reply is split by paragraphs (:func:`src.bots.tg_html.split_tg_html`);
    the last piece gets the mark “…” and a link to the full analysis in the cabinet.
    Without a ``profile_id`` there is no link: the cabinet route without a profile returns 404.
    """
    if profile_id is None:
        footer = "…"
    else:
        url = cabinet_tender_url(CABINET_BASE_URL, tender_id, profile_id)
        footer = f'… <a href="{url}">{bot_t(locale, "ai_full_cabinet")}</a>'
    return split_tg_html(html_text, footer=footer)


async def _send_ai_chunks(
    message, chunks: list[str], *, label: str, chat_id: int, tender_id: str
) -> None:
    """Sending the pieces of the AI reply: a failure of one piece does not lose the tail (#634)."""
    for chunk in chunks:
        try:
            await message.answer(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s: piece not sent chat=%s tender=%s err=%s",
                label,
                chat_id,
                tender_id,
                type(exc).__name__,
            )


async def _cabinet_profile_id(session, chat_id: int) -> str | None:
    """profile_id for the link to the cabinet: the latest profile of the tenant of the chat.

    The same order as for the checklist and the draft (``created_at DESC``, without
    the deleted ones): their service saves the analysis under this profile, and the cabinet
    looks up the pair tender+profile.
    """
    tenant_id = await resolve_tenant_id(session, chat_id)
    if tenant_id is None:
        return None
    profile = await _latest_profile(session, tenant_id)
    return str(profile.id) if profile is not None else None


def _funnel_buttons(
    tender_id: str, status: str, locale: str | None
) -> list[InlineKeyboardButton]:
    """The funnel buttons in the top row of the card under the current status."""
    if status == "new":
        return [
            _status_button(common_t(locale, "btn_follow"), "watching", tender_id),
            _status_button(common_t(locale, "btn_hide"), "skipped", tender_id),
        ]
    if status == "watching":
        return [
            _status_button(bot_t(locale, "act_participate"), "active", tender_id),
            _status_button(common_t(locale, "btn_hide"), "skipped", tender_id),
        ]
    if status == "active":
        return [
            _status_button(bot_t(locale, "act_submitted"), "submitted", tender_id),
            _status_button(bot_t(locale, "act_decline"), "skipped", tender_id),
        ]
    if status == "submitted":
        return [
            _status_button(bot_t(locale, "act_won"), "won", tender_id),
            _status_button(bot_t(locale, "act_lost"), "lost", tender_id),
        ]
    return []  # won / lost / skipped — the funnel is closed


def build_card_keyboard(
    tender_id: str,
    *,
    status: str = "new",
    locale: str | None = None,
    cabinet_url: str | None = None,
    ai_full: bool | None = True,
) -> InlineKeyboardMarkup | None:
    """The card keyboard: row 1 — [Open in the cabinet] + the funnel, row 2 — [🧠 AI analysis ▾].

    ``ai_full=None`` — do not show the AI menu (a redraw of a message that did not have
    it); ``True``/``False`` — show it, ``False`` = without a profile (only the
    deep analysis and the analytics). The funnel is closed and there is nothing to open → ``None``.
    The ``callback_data`` of the funnel and of the AI do not depend on the language.
    """
    top: list[InlineKeyboardButton] = []
    if cabinet_url:
        top.append(
            InlineKeyboardButton(
                text=common_t(locale, "btn_open_cabinet"), url=cabinet_url
            )
        )
    top.extend(_funnel_buttons(tender_id, status, locale))
    rows: list[list[InlineKeyboardButton]] = [top] if top else []
    if ai_full is not None and status not in ("won", "lost", "skipped"):
        rows.append(
            [
                InlineKeyboardButton(
                    text=common_t(locale, "btn_ai_menu"),
                    callback_data=f"{_AI_MENU_PREFIX}{tender_id}:{int(bool(ai_full))}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _markup_buttons(markup) -> list[InlineKeyboardButton]:
    if markup is None:
        return []
    return [b for row in markup.inline_keyboard for b in row]


_CABINET_TENDER_PATH = "/app/tenders/"


def _extra_url_rows(markup) -> list[list[InlineKeyboardButton]]:
    """The rows with the url buttons NOT from the cabinet (debug “⚙️ Set up the template”) — do not lose them on a redraw."""
    if markup is None:
        return []
    return [
        list(row)
        for row in markup.inline_keyboard
        if row and all(b.url and _CABINET_TENDER_PATH not in b.url for b in row)
    ]


def _card_context(markup) -> tuple[str | None, bool | None]:
    """From the current keyboard of the card: (the cabinet URL, the ``ai_full`` flag | None)."""
    cabinet_url: str | None = None
    ai_full: bool | None = None
    for btn in _markup_buttons(markup):
        # We look up the cabinet link by the URL pattern, not by the position: in the debug card
        # the first url button is “⚙️ Set up the template”.
        if btn.url and cabinet_url is None and _CABINET_TENDER_PATH in btn.url:
            cabinet_url = btn.url
        data = btn.callback_data or ""
        if data.startswith((_AI_MENU_PREFIX, _AI_BACK_PREFIX)):
            ai_full = data.rsplit(":", 1)[-1] == "1"
    return cabinet_url, ai_full


# The prefixes of the status line in all languages — to cut out the former line when
# redrawing (the user could have changed the locale between messages).
_STATUS_LINE_PREFIXES: tuple[str, ...] = tuple(
    f"{BOT[lang]['status_line']}:" for lang in SUPPORTED
)


def _apply_status_line(message_text: str, label: str, locale: str | None = None) -> str:
    """Updates the “Status: …” line in the message text (a port of v6).

    It cuts out the former status line in ANY of the supported languages (the locale
    could have changed between messages), then appends the new one in the
    language ``locale``. A pure function (no network/DB) — tested directly.
    """
    lines = [
        ln
        for ln in (message_text or "").split("\n")
        if not ln.startswith(_STATUS_LINE_PREFIXES)
    ]
    while lines and lines[-1] == "":
        lines.pop()
    lines.append("")
    lines.append(f"{bot_t(locale, 'status_line')}: {label}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The CRUD layer (takes an AsyncSession — tested on a mock session).
# --------------------------------------------------------------------------- #


async def _find_channel(session, telegram_id: int) -> UserChannel | None:
    """Finds the Telegram channel binding by ``external_id``.

    external_id is NOT unique (several tenants on one chat_id — by design).
    We take the active binding, otherwise the most recent by linked_at — the same order
    as resolve_tenant_id in status_service (the consistency of bot↔delivery).
    """
    result = await session.execute(
        select(UserChannel)
        .where(
            UserChannel.channel == "telegram",
            UserChannel.external_id == str(telegram_id),
        )
        .order_by(
            (UserChannel.status == "active").desc(),
            UserChannel.linked_at.desc(),
        )
    )
    return result.scalars().first()


async def _latest_profile(session, tenant_id) -> Profile | None:
    """The latest non-deleted profile of the tenant."""
    result = await session.execute(
        select(Profile)
        .where(Profile.tenant_id == tenant_id, Profile.deleted_at.is_(None))
        .order_by(Profile.created_at.desc())
    )
    return result.scalars().first()


async def _list_profiles(session, tenant_id) -> list[Profile]:
    """ALL the non-deleted profiles of the tenant for ``/profile`` (not only the latest).

    The order is deterministic (it must not change from run to run, it affects the
    pagination/cut by the Telegram limit): the active first, then the disabled ones,
    inside a group — by name and id. ``is_active IS NULL`` (a profile created
    bypassing the web form, without a ``server_default``) is treated as “disabled” —
    the matching (`src/workers/matching.py`, ``WHERE p.is_active = true``) silently gives
    the profile nothing on NULL, and the list must show this truth, and not declare
    the profile active. ``NULLS LAST`` in the ``DESC`` sorting puts
    NULL right after ``False`` — the same block “disabled”.
    """
    return await manage.ordered_profiles(session, tenant_id)


# The name of the template created by the bot’s onboarding. Neutral and without a language: it is the data
# of the profile (visible in the cabinet and in the card “found by template”), not the bot’s chrome.
_ONBOARDING_PROFILE_NAME = "Telegram"


async def create_subscriber(
    session,
    *,
    telegram_id: int,
    language: str,
    keywords: list[str],
    region_codes: list[str],
) -> None:
    """Creates tenant+user+channel+profile in one transaction (a new subscriber).

    All 4 writes are committed together: on an error the whole onboarding is rolled back.
    The UUID is generated in Python in advance, in order to set the GUC before the INSERT:
    under FORCE RLS (0021) the policy ``tenants.tenant_isolation`` checks
    ``id = current_setting(...)::uuid`` — the GUC must match the new id.
    """
    new_tenant_id = uuid4()
    await set_current_tenant(session, new_tenant_id)  # GUC before the first flush (RLS)
    tenant = Tenant(
        id=new_tenant_id,
        country_code="RS",
        tier="lite",  # the internal canon of tiers (R8); 'free' — the storefront, not the storage
        status="trial",
        locale=language,
        tz="Europe/Belgrade",
    )
    session.add(tenant)
    await session.flush()  # INSERT of the tenant with an explicit id (server_default is not used)

    user = User(tenant_id=tenant.id, locale=language, role="admin")
    session.add(user)
    await session.flush()  # we get user.id

    channel = UserChannel(
        user_id=user.id,
        channel="telegram",
        external_id=str(telegram_id),
        status="active",
        linked_at=datetime.now(timezone.utc),
    )
    session.add(channel)

    profile = Profile(
        tenant_id=tenant.id,
        country_code="RS",
        target_language=language,
        name=_ONBOARDING_PROFILE_NAME,
        is_active=True,
        delivery_channels=["telegram"],
        keywords_include=_keywords_to_groups(keywords),
        # ‼️ The bot’s onboarding allows NOT entering keywords, and earlier such a
        # profile received the whole flow “by itself” (empty = any
        # tender passes). From 0043 the whole flow goes only by an EXPLICIT mode, therefore
        # we declare it here: otherwise the subscriber who skipped the step would silently
        # receive NOTHING and would not see the reason.
        match_all=not _keywords_to_groups(keywords),
        languages=[],
        procedure_types=[],
        regions=region_codes,
        include_no_amount=True,
        min_days_until_deadline=0,
        classifications={"include": [], "exclude": []},
        created_by_user_id=user.id,
        version=1,
    )
    # The sources — from the registry `sources` (SRC-0a, R1), and not a literal; the moment of the
    # subscription to each one = the moment of creating the profile (R5).
    apply_source_selection(
        profile,
        await default_source_keys_for_new_profile(session),
        datetime.now(timezone.utc),
    )
    session.add(profile)

    await session.commit()


async def save_subscriber(
    session,
    *,
    telegram_id: int,
    language: str,
    keywords: list[str],
    region_codes: list[str],
) -> str:
    """The onboarding of a new subscriber; a bound account is NOT overwritten.

    The bootstrap order under FORCE RLS + the join policy of user_channels (0022):
    1. app_resolve_tenant_id — SECURITY DEFINER, without a GUC (bypasses RLS).
    2. If the tenant is found: set_current_tenant (GUC) → _find_channel (the join policy).
    3. The channel is found → ``"exists"``: the templates of the bound account live in the cabinet
       (keywords, regions, ``is_active``, ``target_language``), and the bot’s onboarding that
       reached the confirmation already after the binding would silently overwrite
       the last template (the former ``update_subscriber``, removed). Otherwise →
       create_subscriber (generates the UUID of the tenant and sets the GUC before the INSERT).
    Returns ``"exists"`` or ``"created"``.
    """
    result = await session.execute(_RESOLVE_TENANT_SQL, {"eid": str(telegram_id)})
    tenant_id = result.scalar()
    if tenant_id is not None:
        await set_current_tenant(session, tenant_id)
        channel = await _find_channel(session, telegram_id)
        if channel is not None:
            return "exists"
    # A new subscriber: create_subscriber generates the UUID and sets the GUC before the INSERT.
    await create_subscriber(
        session,
        telegram_id=telegram_id,
        language=language,
        keywords=keywords,
        region_codes=region_codes,
    )
    return "created"


async def get_subscriber(session, telegram_id: int) -> dict | None:
    """Returns the tenant/user/channel/profile tuple or ``None``.

    The bootstrap order under FORCE RLS + the join policy of user_channels (0022):
    1. app_resolve_tenant_id — SECURITY DEFINER, bypasses RLS, resolves the tenant.
    2. set_current_tenant — the GUC before reading the tables under the join policy.
    3. _find_channel — user_channels (the join policy 0022, requires the GUC).
    4. session.get(User/Tenant) + _latest_profile — already with the GUC.
    """
    result = await session.execute(_RESOLVE_TENANT_SQL, {"eid": str(telegram_id)})
    tenant_id = result.scalar()
    if tenant_id is None:
        return None
    await set_current_tenant(session, tenant_id)
    channel = await _find_channel(session, telegram_id)
    if channel is None:
        return None
    user = await session.get(User, channel.user_id)
    tenant = await session.get(Tenant, user.tenant_id) if user else None
    profile = (
        await _latest_profile(session, user.tenant_id) if user is not None else None
    )
    return {
        "tenant": tenant,
        "user": user,
        "channel": channel,
        "profile": profile,
    }


async def _resolve_bot_locale(session, telegram_id: int) -> str:
    """The language of the bot’s chrome: ``user_channels.locale`` → ``users.locale`` → ru.

    Two AXES (the project rules): it returns ONLY the interface language of the bot’s chrome, NOT the language of the
    AI content (``profiles.target_language``). The channel language (0038) is a refinement of
    the same first axis for a SPECIFIC chat: ``NULL`` in the channel means
    "inherit ``users.locale``", therefore it is a fallback, not a third axis.
    The same priority as the delivery (``COALESCE(uc.locale, u.locale)`` in
    ``_LOAD_JOBS_SQL``) — otherwise the choice in the bot would change the cards but not the replies
    of the bot, and vice versa.

    The bootstrap order under FORCE RLS is the same as :func:`get_subscriber`:
    app_resolve_tenant_id (SECURITY DEFINER, without a GUC) → set_current_tenant →
    _find_channel → session.get(User). No account/channel/locale → ru.
    The locale resolution must never break the handler’s reply: any read error
    → ru (into the log — only the CLASS of the error, without PII).
    """
    try:
        result = await session.execute(_RESOLVE_TENANT_SQL, {"eid": str(telegram_id)})
        tenant_id = result.scalar()
        if tenant_id is None:
            return norm_locale(None)
        await set_current_tenant(session, tenant_id)
        channel = await _find_channel(session, telegram_id)
        if channel is None:
            return norm_locale(None)
        channel_locale = getattr(channel, "locale", None)
        if channel_locale:
            return norm_locale(channel_locale)
        user = await session.get(User, channel.user_id)
        return norm_locale(user.locale if user is not None else None)
    except Exception as exc:  # noqa: BLE001 — the chrome must not break the handler
        logger.warning("bot locale resolve failed: %s", type(exc).__name__)
        return norm_locale(None)


async def resolve_bot_locale(session, telegram_id: int) -> str:
    """The language of the bot’s chrome (see :func:`_resolve_bot_locale`) + remembers it for the
    outgoing of the current update (:mod:`src.bots.sr_latin`: sr → Latin)."""
    locale = await _resolve_bot_locale(session, telegram_id)
    bot_locale.set(locale)
    return locale


async def set_channel_locale(session, telegram_id: int, locale: str) -> str:
    """Writes the language INTO THE CHANNEL from which the command came (``/language``).

    Returns ``"ok"`` | ``"no_account"`` (the chat is not bound) | ``"bad_locale"``.

    ‼️ We do NOT touch ``users.locale``: the command changes the language of this delivery channel only.
    The cabinet and the emails stay on their own axis — otherwise the choice of the language
    in Telegram would silently switch the interface language of the cabinet and of the emails.

    ‼️ One chat_id may have SEVERAL active channels of different tenants
    (an MVP assumption of ``bind_channel_by_token``, in production it is so). We write exactly
    into the row that :func:`_find_channel` returned under the GUC of the resolved
    tenant — that is, into the channel from which the command came; a foreign channel of the same
    chat_id is not visible under the join policy 0022 and is not changed.

    The bootstrap order under FORCE RLS is the same as for :func:`get_subscriber`:
    app_resolve_tenant_id (SECURITY DEFINER, without a GUC) → set_current_tenant →
    _find_channel → the write.
    """
    if locale not in SUPPORTED:
        return "bad_locale"
    result = await session.execute(_RESOLVE_TENANT_SQL, {"eid": str(telegram_id)})
    tenant_id = result.scalar()
    if tenant_id is None:
        return "no_account"
    await set_current_tenant(session, tenant_id)
    channel = await _find_channel(session, telegram_id)
    if channel is None:
        return "no_account"
    channel.locale = locale
    await session.commit()
    return "ok"


# --------------------------------------------------------------------------- #
# The binding of a channel from the cabinet (C-04, Chat 26).
# --------------------------------------------------------------------------- #


async def _find_channel_for_user(session, user_id) -> UserChannel | None:
    """The Telegram channel of a specific ``user_id`` (for the idempotent upsert)."""
    result = await session.execute(
        select(UserChannel).where(
            UserChannel.user_id == user_id,
            UserChannel.channel == "telegram",
        )
    )
    return result.scalars().first()


async def bind_channel_by_token(
    session,
    *,
    raw_token: str,
    telegram_id: int,
    now: datetime | None = None,
) -> str:
    """Spends the ``channel_bind`` token and creates/updates ``user_channels``.

    Returns ``"bound"`` on success, ``"invalid"`` if the token is empty/unknown/
    expired/already used (in this case nothing is written and the token is not spent).

    The binding is made to the ``user_id`` FROM THE TOKEN (the cabinet user). The column
    ``user_channels.external_id`` is NOT unique in the schema (0001) — if this
    ``telegram_id`` is already bound to another ``user`` (e.g. it went through the FSM onboarding),
    we do NOT touch the foreign record and do NOT fall: we create/update the binding strictly for
    the cabinet ``user_id``. The delivery resolves the channel by the ``user_id`` of its
    tenant (``_LOAD_JOBS_SQL``), therefore one tg-chat may serve
    several tenants (an MVP assumption).
    """
    moment = now or datetime.now(timezone.utc)
    if not raw_token:
        return "invalid"

    result = await session.execute(
        select(AuthToken).where(
            AuthToken.token_hash == hash_token(raw_token),
            AuthToken.purpose == CHANNEL_BIND_PURPOSE,
        )
    )
    token = result.scalars().first()
    if token is None or token.consumed_at is not None:
        return "invalid"
    if token.expires_at is not None and token.expires_at <= moment:
        return "invalid"

    # Resolving the tenant by the user_id of the token (SECURITY DEFINER 0022) — the GUC before user_channels.
    resolve_result = await session.execute(
        _RESOLVE_TENANT_BY_USER_SQL, {"uid": str(token.user_id)}
    )
    user_tenant_id = resolve_result.scalar()
    if user_tenant_id is None:
        return "invalid"
    await set_current_tenant(session, user_tenant_id)

    channel = await _find_channel_for_user(session, token.user_id)
    if channel is not None:
        # idempotently: we update the existing binding of this user, we do not create a duplicate
        channel.external_id = str(telegram_id)
        channel.status = "active"
        channel.linked_at = moment
    else:
        session.add(
            UserChannel(
                user_id=token.user_id,
                channel="telegram",
                external_id=str(telegram_id),
                status="active",
                linked_at=moment,
            )
        )
    # A template created in the cabinet BEFORE the bot binding, otherwise it would remain without delivery.
    await adopt_channel_into_empty_profiles(
        session, tenant_id=user_tenant_id, channel="telegram"
    )

    token.consumed_at = moment
    await session.commit()
    return "bound"


# The time zone for showing the access date. In the DB and the logic — UTC; the local zone appears
# only at the boundary of display (as in the delivery and the reminders). `tenants.tz` is not
# connected yet — the single zone of the project.
DISPLAY_TZ = ZoneInfo("Europe/Belgrade")


def _access_line(
    access_until: datetime | None, now: datetime, locale: str | None = None
) -> str:
    """The access-status line with the DATE — one for ``/profile`` and ``/status``.

    Earlier the branching was duplicated in the two commands, and the date was not shown
    anywhere: the client saw “active for another 4 days” and did not know until which day, and for
    an expired access they did not know when exactly it ended (owner, 06.09.2026).
    The freeze — the SSOT predicate :func:`src.domain.access.is_frozen`; the date
    is shown in :data:`DISPLAY_TZ`.
    """
    if access_until is None:
        return bot_t(locale, "status_access_unlimited")
    date_str = access_until.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y")
    if is_frozen(access_until, now):
        return bot_t(locale, "status_access_expired").format(date=date_str)
    days = (access_until - now).days
    if days > 0:
        return bot_t(locale, "status_access_days").format(days=days, date=date_str)
    return bot_t(locale, "status_access_soon").format(date=date_str)


# The link to the cabinet for “the full list is here” when cut by the Telegram limit
# (the same domain as the rest of the bot’s chrome, see ``app.nab.rs`` in i18n.py).
# The cabinet dashboard (`/app`) — there is the list of the templates; the GET `/profiles` route has none.
_PROFILES_CABINET_URL = f"{CABINET_BASE_URL}/app"
_TELEGRAM_MAX_MESSAGE = 4096


def _sources_line(
    profile: Profile, sources: dict[str, SourceInfo], locale: str | None
) -> str:
    """The profile’s sources with labels (SRC-0b): the one switched off by the admin — with a mark.

    If the key is not in the registry at all → the key itself (an explicit fallback, not an empty value).
    """
    keys = getattr(profile, "enabled_source_keys", None) or []
    labels = [
        (
            source_choice.source_label(sources[k], locale)
            if k in sources
            else f"{k} ({common_t(locale, 'src_paused')})"
        )
        for k in keys
    ]
    # The source labels are the registry data (the admin panel): into the HTML message only escaped.
    return (
        f"   {bot_t(locale, 'lbl_sources')}: "
        f"{html.escape(', '.join(labels)) or '—'}"
    )


def _profile_entry_lines(
    profile: Profile,
    locale: str | None = None,
    sources: dict[str, SourceInfo] | None = None,
) -> list[str]:
    """The lines of ONE profile in the /profile list: the name, keywords, region,
    sources (if the ``sources`` registry is passed — SRC-0b).

    ``match_all`` is declared explicitly in words (the client is not obliged to know the term) —
    otherwise a profile without keywords would look like a forgotten one, and not like "the whole
    flow" (0043).
    """
    # The name and the keywords are entered by the client: into the HTML message — only escaped.
    name = html.escape(profile.name or bot_t(locale, "no_profile_name"))
    # is_active IS True → active; False OR NULL → disabled (see _list_profiles:
    # NULL — an oversight of create_profile bypassing the web form, the matching does not take it).
    state = (
        bot_t(locale, "profile_state_active")
        if profile.is_active is True
        else bot_t(locale, "profile_state_disabled")
    )
    if profile.match_all:
        keywords_str = bot_t(locale, "profile_all_tenders")
    else:
        keywords = _keywords_to_words(profile.keywords_include)
        keywords_str = html.escape(", ".join(keywords)) if keywords else "—"
    regions = profile.regions if profile.regions else []
    region_str = (
        ", ".join(_region_label(c, locale) for c in regions)
        if regions
        else bot_t(locale, "all_regions")
    )
    lines = [
        f"<b>{name}</b> — {state}",
        f"   {bot_t(locale, 'lbl_keywords')}: {keywords_str}",
        f"   {bot_t(locale, 'lbl_region')}: {region_str}",
    ]
    if sources is not None:
        lines.append(_sources_line(profile, sources, locale))
    return lines


def cap_line(locale: str | None) -> str:
    """The line about the daily delivery limit (``""`` if the limit is off).

    The figure — from the common module (:func:`src.domain.delivery_cap.daily_cap`), the text —
    from the common vocabulary; in /help and in the /profile card.
    """
    cap = daily_cap()
    if cap <= 0:
        return ""
    return common_t(locale, "cap_help_line").format(cap=cap)


def help_text(locale: str | None) -> str:
    """/help with the substituted line about the daily limit (an empty one — the line is removed)."""
    text = bot_t(locale, "help")
    line = cap_line(locale)
    return text.replace("{cap_line}\n", f"{line}\n" if line else "")


def _render_profile(
    data: dict,
    profiles: list[Profile],
    locale: str | None = None,
    now: datetime | None = None,
    max_len: int = _TELEGRAM_MAX_MESSAGE,
    sources: dict[str, SourceInfo] | None = None,
) -> str:
    """A human-readable summary of ALL the profiles of the tenant for /profile.

    The common part (account/plan/access) is printed ONCE — not duplicated
    for each profile. The active profiles — a separate block before
    the disabled ones; the disabled block explicitly warns that tenders by it do NOT
    arrive (incl. with ``is_active IS NULL`` — see :func:`_list_profiles`,
    where the deterministic order lies, which is simply printed here).
    Telegram cuts the message at 4096 characters — the list is cut BY THE NUMBER OF
    profiles (not by characters inside a record) and explicitly names how many are hidden and
    where to see the rest. The first line — the bound account (email):
    the chat may be bound to a foreign account (several ``user_channels`` for
    one ``chat_id``), and without this line the owner does not see whose list it is.
    The access-freeze predicate — the same SSOT as for ``/status``
    (:func:`src.domain.access.is_frozen`); ``now`` is accepted explicitly, so that
    the test does not substitute the system clock.
    """
    now = now or datetime.now(timezone.utc)
    tenant = data.get("tenant")
    user = data.get("user")
    email = html.escape(user.email) if user is not None and user.email else "—"
    tier = tenant.tier if tenant else "free"
    status = getattr(tenant, "status", None) if tenant else None
    tier_label = _tier_label(tier, locale, status)

    lines = [
        bot_t(locale, "profile_title"),
        "",
        f"• {bot_t(locale, 'lbl_account')}: {email}",
        f"• {bot_t(locale, 'lbl_tier')}: {tier_label}",
    ]
    if tenant is not None:
        lines.append(_access_line(getattr(tenant, "access_until", None), now, locale))
    limit_line = cap_line(locale)

    active = [p for p in profiles if p.is_active is True]
    disabled = [p for p in profiles if p.is_active is not True]  # NULL — goes here too
    groups = []
    if active:
        groups.append((bot_t(locale, "profile_active_section"), active))
    if disabled:
        groups.append((bot_t(locale, "profile_disabled_section"), disabled))

    # A block = the lines for adding one profile (+ the header of the section for the
    # first profile of the section), so that the list is cut by WHOLE profiles.
    chunks: list[list[str]] = []
    for section_label, section_profiles in groups:
        for i, profile in enumerate(section_profiles):
            block = [""]
            if i == 0:
                block.append(section_label)
            block.extend(_profile_entry_lines(profile, locale, sources))
            chunks.append(block)

    total = len(profiles)
    footer = bot_t(locale, "profile_hidden_count").format(
        n=total, url=_PROFILES_CABINET_URL
    )
    # room for "" + the tail line in the worst case + the line about the daily limit
    reserve = len(footer) + 2 + (len(limit_line) + 4 if limit_line else 0)

    shown = 0
    for block in chunks:
        candidate = lines + block
        if shown > 0 and len("\n".join(candidate)) > max_len - reserve:
            break
        lines = candidate
        shown += 1

    hidden = total - shown
    if hidden > 0:
        lines.append("")
        lines.append(
            bot_t(locale, "profile_hidden_count").format(
                n=hidden, url=_PROFILES_CABINET_URL
            )
        )
    if limit_line:
        lines.append("")
        lines.append(f"📬 {html.escape(limit_line)}")
    return "\n".join(lines)


def _tier_label(tier: str, locale: str | None = None, status: str | None = None) -> str:
    """The localized name of the tier from the COMMON vocabulary (``COMMON``).

    ‼️ The label — by the EFFECTIVE tier, as the site and the gates: on trial the raw
    ``tier`` (the sign-up writes ``lite``) is not shown. Trial — the key
    ``plan_trial`` (“Trial” / “Probni” / “Trial”), the same as in the common vocabulary
    of the cabinet; a non-trial — the storefront key (``tier_to_plan_key``: lite → free). An unknown
    code is returned as is.
    """
    if (status or "").strip().lower() == "trial":
        return common_t(locale, "plan_trial")
    key = f"plan_{tier_to_plan_key(effective_tier(status, tier))}"
    if key in COMMON[norm_locale(locale)]:
        return common_t(locale, key)
    return tier


# --------------------------------------------------------------------------- #
# The handlers of the commands and the FSM.
# --------------------------------------------------------------------------- #


async def _start_onboarding(
    message: Message, state: FSMContext, locale: str | None = None
) -> None:
    """Starts the onboarding from the language choice step.

    ``locale`` — the interface language (``users.locale``, the axis of the chrome) — goes into the FSM
    (``ui_locale``), so that the subsequent steps take the chrome from there without a repeated
    DB read. NOT to be confused with the language the user chooses in the wizard: that one
    is the axis of the AI content (``profiles.target_language``), see the project rules.
    """
    await state.set_state(OnboardingFSM.waiting_language)
    await state.update_data(ui_locale=locale)
    await message.answer(
        bot_t(locale, "onb_welcome"),
        reply_markup=_language_keyboard(),
    )


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    state: FSMContext,
    command: CommandObject | None = None,
) -> None:
    """/start — the entry point.

    If there is a deep-link payload (``/start <bind-token>``) — we bind the
    Telegram channel to the cabinet account (C-04). Otherwise the former behavior: if
    a profile exists, a hint /profile/reset; without a profile — the FSM onboarding.

    In the mode ``BOT_SIGNUP_MODE=token_only`` (the default) a bare ``/start`` without a
    valid token and without an already bound channel is rejected (the subscription is arranged
    on the site); ``open`` restores the free onboarding.
    """
    await state.clear()
    telegram_id = message.from_user.id

    payload = (command.args or "").strip() if command is not None else ""

    if payload:
        # Any non-empty payload — the cabinet bind token (the subscription only from the site).
        async with session_scope() as session:
            outcome = await bind_channel_by_token(
                session, raw_token=payload, telegram_id=telegram_id
            )
            # After the bind the channel of this user is already bound → the locale is readable;
            # with invalid — there is no binding, resolve_bot_locale will give ru (the default).
            locale = await resolve_bot_locale(session, telegram_id)
        if outcome == "bound":
            await message.answer(bot_t(locale, "bind_ok"))
        else:
            await message.answer(bot_t(locale, "bind_invalid"))
        return

    async with session_scope() as session:
        # We check the binding through SECURITY DEFINER (without a GUC) — the join policy 0022.
        _exist_res = await session.execute(
            _RESOLVE_TENANT_SQL, {"eid": str(telegram_id)}
        )
        existing = _exist_res.scalar()

        # The interface locale is resolved on the same session (by the users.locale of the same
        # user). The onboarding below — the chrome of scope 2b, it remains ru for now.
        locale = await resolve_bot_locale(session, telegram_id)

    if existing is not None:
        await message.answer(bot_t(locale, "start_exists") + bot_t(locale, "help_hint"))
        return
    if get_bot_settings().bot_signup_mode.strip().lower() != "open":
        await message.answer(
            bot_t(locale, "signup_closed"), reply_markup=_unlinked_markup(locale)
        )
        return
    await _start_onboarding(message, state, locale)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """/help — the structured reference of the bot’s capabilities (VG-31 item 2).

    Groups the commands by meaning + separately explains the buttons on the tender card
    (deep-analysis/bid-verdict/checklist/draft/customer-analytics) —
    they are not commands but inline buttons, yet exactly there the main
    value of the bot is hidden, which the customer “did not see” (the motivation of VG-31).
    """
    locale = norm_locale(None)  # ru by default (no from_user → without a DB read)
    if message.from_user is not None:
        async with session_scope() as session:
            locale = await resolve_bot_locale(session, message.from_user.id)
    await message.answer(help_text(locale))


@router.callback_query(OnboardingFSM.waiting_language, F.data.startswith("ob:lang:"))
async def on_language(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 1 → 2: the language is chosen, we ask for the keywords."""
    code = callback.data.split(":")[-1]
    data = await state.get_data()
    locale = data.get("ui_locale")
    bot_locale.set(
        locale
    )  # The FSM steps do not resolve the language via the DB: sr → Latin
    await state.update_data(language=code)
    await state.set_state(OnboardingFSM.waiting_keywords)
    await callback.message.edit_text(
        bot_t(locale, "onb_lang_chosen").format(lang=_LANGUAGES.get(code, code))
    )
    await callback.answer()


@router.message(OnboardingFSM.waiting_keywords)
async def on_keywords(message: Message, state: FSMContext) -> None:
    """Step 2 → 3: the keywords are accepted, we show the choice of the region."""
    data = await state.get_data()
    locale = data.get("ui_locale")
    bot_locale.set(
        locale
    )  # The FSM steps do not resolve the language via the DB: sr → Latin
    raw = message.text or ""
    keywords = [w.strip() for w in raw.split(",") if w.strip()]
    if not keywords:
        await message.answer(bot_t(locale, "onb_keywords_empty"))
        return
    await state.update_data(keywords=keywords)
    await state.set_state(OnboardingFSM.waiting_region)
    await message.answer(
        bot_t(locale, "onb_keywords_saved"),
        reply_markup=_region_keyboard(locale),
    )


@router.callback_query(OnboardingFSM.waiting_region, F.data.startswith("ob:region:"))
async def on_region(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 3 → 4: the region is chosen, we show the summary and the confirmation."""
    code = callback.data.split(":")[-1]
    await state.update_data(region=code)
    data = await state.get_data()
    locale = data.get("ui_locale")
    bot_locale.set(
        locale
    )  # The FSM steps do not resolve the language via the DB: sr → Latin
    await state.set_state(OnboardingFSM.waiting_confirmation)
    await callback.message.edit_text(
        _build_summary(data, locale), reply_markup=_confirm_keyboard(locale)
    )
    await callback.answer()


@router.callback_query(OnboardingFSM.waiting_confirmation, F.data == "ob:confirm")
async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 4: confirmation → upsert of the subscriber into the DB (create or update)."""
    data = await state.get_data()
    locale = data.get("ui_locale")
    bot_locale.set(
        locale
    )  # The FSM steps do not resolve the language via the DB: sr → Latin
    language = data.get("language", "ru")
    keywords = data.get("keywords", [])
    region = data.get("region", _ALL_REGIONS)
    region_codes: list[str] = [] if region == _ALL_REGIONS else [region]
    telegram_id = callback.from_user.id

    async with session_scope() as session:
        outcome = await save_subscriber(
            session,
            telegram_id=telegram_id,
            language=language,
            keywords=keywords,
            region_codes=region_codes,
        )

    await state.clear()
    # “exists”: the chat was bound to an account while the wizard was running — its templates live in the
    # cabinet and are not overwritten; “saved” would be untrue.
    await callback.message.edit_text(
        bot_t(locale, "reset_intro" if outcome == "exists" else "onb_saved")
    )
    await callback.answer()


@router.callback_query(OnboardingFSM.waiting_confirmation, F.data == "ob:restart")
async def on_restart(callback: CallbackQuery, state: FSMContext) -> None:
    """Step 4: “Start over” → a reset of the FSM data and a return to the language choice."""
    # state.clear() erases ui_locale — we resolve the interface language again from the DB.
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, callback.from_user.id)
    await state.clear()
    await state.set_state(OnboardingFSM.waiting_language)
    await state.update_data(ui_locale=locale)
    await callback.message.edit_text(
        bot_t(locale, "onb_restart"),
        reply_markup=_language_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("st:"))
async def on_tender_status(callback: CallbackQuery) -> None:
    """The funnel button: ``st:{status}:{tender_id}`` → UPSERT of the status + a redraw.

    A port of v6 ``handle_callback`` to aiogram 3:
      1. parses ``st:{new_status}:{tender_id}`` (tender_id — a UUID, without colons);
      2. by ``callback.message.chat.id`` resolves ``tenant_id``
         (:func:`src.bots.status_service.resolve_tenant_id`); if there is no binding —
         a toast “First /start” and exit;
      3. the freeze gate (FIX-SG-04): if the tenant is frozen — a refusal "extend
         the access", WITHOUT a status write;
      4. UPSERT into ``tender_user_status`` (RLS SET LOCAL inside the service);
      5. a short toast with the label of the new status;
      6. redraws the keyboard for the new status and updates the
         “Status: …” line in the text of the card.
    """
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or not parts[2]:
        await callback.answer()
        return
    _, new_status, tender_id = parts
    if new_status not in STATUS_LABELS:
        await callback.answer()
        return

    chat_id = callback.message.chat.id
    async with session_scope() as session:
        tenant_id = await resolve_tenant_id(session, chat_id)
        if tenant_id is None:
            # The chat is not bound → there is no locale of an account: the language of the Telegram chat, not ru.
            await callback.answer(
                bot_t(_tg_locale(callback), "toast_start_first"), show_alert=True
            )
            return
        # The locale of the chrome (users.locale) — the toast, the buttons, the status line; it does not affect
        # the callback_data/the funnel statuses (the same axis as the bot handlers above).
        locale = await resolve_bot_locale(session, chat_id)
        # The freeze gate (FIX-SG-04): a frozen one cannot change the funnel status.
        if await is_tenant_frozen(session, tenant_id):
            await callback.answer(bot_t(locale, "frozen"), show_alert=True)
            return
        # The former status — BEFORE the write: “↩ Restore” will restore it from the DB, and not
        # guess by the buttons (new and watching have the same ones).
        prior_status = await get_tender_status(
            session, tenant_id=tenant_id, tender_id=tender_id
        )
        await set_tender_status(
            session, tenant_id=tenant_id, tender_id=tender_id, status=new_status
        )

    # The status label — the common vocabulary of the funnel (COMMON), unified with the cabinet (the project rules).
    label = common_t(locale, f"status_{new_status}")
    logger.info("tender status: chat=%s tender=%s → %s", chat_id, tender_id, new_status)
    await callback.answer(label)

    old_markup = getattr(callback.message, "reply_markup", None)
    base_text = (callback.message.html_text or "") if callback.message else ""

    if new_status == "skipped" and await _collapse_card(
        callback, tender_id, base_text, old_markup, locale, prior_status
    ):
        return

    cabinet_url, ai_full = _card_context(old_markup)
    new_markup = build_card_keyboard(
        tender_id,
        status=new_status,
        locale=locale,
        cabinet_url=cabinet_url,
        ai_full=ai_full,
    )
    extra_rows = _extra_url_rows(old_markup)
    if extra_rows:
        new_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                *(new_markup.inline_keyboard if new_markup else []),
                *extra_rows,
            ]
        )
    updated_text = _apply_status_line(base_text, label, locale)
    try:
        await callback.message.edit_text(updated_text, reply_markup=new_markup)
    except (
        Exception
    ):  # noqa: BLE001 — an old/unchanged message: the toast has already been shown
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=new_markup)


def _unlinked_markup(locale: str | None) -> InlineKeyboardMarkup:
    """The button of an unbound chat → “Channels → Connect Telegram” in the cabinet.

    ``/cabinet/telegram`` for an unauthenticated user sends to the login and returns back
    (``next=``), so that the path “the account already exists” does not end in a dead end.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "link_cta"),
                    url=f"{CABINET_BASE_URL}/cabinet/telegram",
                )
            ]
        ]
    )


def _tg_locale(event) -> str:
    """The chat language by the Telegram client (``language_code``), when there is no account.

    For the bound chats the language is resolved by :func:`resolve_bot_locale` (channel →
    cabinet); this is a fallback for an unbound chat, where there is no other
    source. An unknown/empty code → ru (the default of the catalog).
    """
    user = getattr(event, "from_user", None)
    raw = getattr(user, "language_code", None)
    code = raw.split("-")[0].lower() if isinstance(raw, str) else ""
    return norm_locale(code or None)


def _first_line(html_text: str) -> str:
    """The first line of the HTML text of the card (the title with stars)."""
    return (html_text or "").split("\n", 1)[0]


async def _collapse_card(
    callback: CallbackQuery,
    tender_id: str,
    base_text: str,
    old_markup,
    locale: str,
    prior_status: str | None,
) -> bool:
    """“Hide”: the card → one line “title — hidden” + “↩ Restore”.

    The original is put into the stash (:mod:`src.bots.card_stash`); if it could not be
    saved → ``False`` and the card stays as it is (the former behavior:
    only the status line changes), because a collapsed card without a stash cannot be
    restored. The funnel status is already written by this moment.
    """
    message = callback.message
    if message is None or getattr(message, "message_id", None) is None:
        return False
    stashed = await stash_card(
        message.chat.id,
        message.message_id,
        {
            "text": base_text,
            "markup": (
                old_markup.model_dump_json(exclude_none=True)
                if old_markup is not None
                else None
            ),
            # None — there was no status (on “Restore” we remove the line)
            "prior_status": prior_status,
        },
    )
    if not stashed:
        return False
    collapsed = f"{_first_line(base_text)} — <i>{common_t(locale, 'card_hidden')}</i>"
    restore = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=common_t(locale, "btn_restore"),
                    callback_data=f"{_CARD_RESTORE_PREFIX}{tender_id}",
                )
            ]
        ]
    )
    try:
        await message.edit_text(collapsed, reply_markup=restore)
    except Exception:  # noqa: BLE001 — could not collapse: the card is intact
        await drop_card(message.chat.id, message.message_id)
        return False
    return True


@router.callback_query(F.data.startswith(_CARD_RESTORE_PREFIX))
async def on_card_restore(callback: CallbackQuery) -> None:
    """“↩ Restore”: ``cd:show:{tender_id}`` → the card from the stash + the former status."""
    message = callback.message
    tender_id = (callback.data or "")[len(_CARD_RESTORE_PREFIX) :]
    if not isinstance(message, Message) or not tender_id:
        await callback.answer()
        return
    chat_id = message.chat.id
    card = await load_card(chat_id, message.message_id)
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        if card is None or tenant_id is None:
            gone = tenant_id is not None
            await callback.answer(
                bot_t(locale, "card_restore_gone" if gone else "toast_start_first"),
                show_alert=True,
            )
            return
        if await is_tenant_frozen(session, tenant_id):
            await callback.answer(bot_t(locale, "frozen"), show_alert=True)
            return
        # A stash of the old format (without the key) → the former behavior “new”.
        prior = card["prior_status"] if "prior_status" in card else "new"
        if prior:
            await set_tender_status(
                session, tenant_id=tenant_id, tender_id=tender_id, status=prior
            )
        else:
            await clear_tender_status(session, tenant_id=tenant_id, tender_id=tender_id)
    markup = (
        InlineKeyboardMarkup.model_validate_json(card["markup"])
        if card.get("markup")
        else None
    )
    await callback.answer()
    try:
        await message.edit_text(card.get("text") or "", reply_markup=markup)
    except Exception:  # noqa: BLE001 — could not restore: we keep the stash
        logger.warning("card restore edit failed")
        return
    await drop_card(chat_id, message.message_id)


@router.callback_query(F.data.startswith(_AI_MENU_PREFIX))
async def on_ai_menu(callback: CallbackQuery) -> None:
    """“🧠 AI analysis ▾”: expands the AI variants under the card (``edit_reply_markup``).

    The buttons themselves carry the former ``callback_data`` (``ai:deep:``/``ai:bid:``/…), their
    limit gates are not affected — the expansion calls and counts nothing.
    """
    message = callback.message
    parts = (callback.data or "").split(":")
    if not isinstance(message, Message) or len(parts) != 4 or not parts[2]:
        await callback.answer()
        return
    tender_id, full = parts[2], parts[3] == "1"
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, message.chat.id)
    rows = [
        r
        for r in (message.reply_markup.inline_keyboard if message.reply_markup else [])
        if not any((b.callback_data or "").startswith(_AI_MENU_PREFIX) for b in r)
    ]

    def _cb(key: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=common_t(locale, key), callback_data=data)

    rows.append(
        [_cb("btn_deep", f"ai:deep:{tender_id}")]
        + ([_cb("btn_bid", f"ai:bid:{tender_id}")] if full else [])
    )
    if full:
        rows.append(
            [
                _cb("btn_checklist", f"ai:chk:{tender_id}"),
                _cb("btn_draft", f"ai:drf:{tender_id}"),
            ]
        )
    rows.append([_cb("btn_customer_analytics", f"an:cust:{tender_id}")])
    rows.append([_cb("btn_back", f"{_AI_BACK_PREFIX}{tender_id}:{int(full)}")])
    await callback.answer()
    with contextlib.suppress(Exception):
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


@router.callback_query(F.data.startswith(_AI_BACK_PREFIX))
async def on_ai_back(callback: CallbackQuery) -> None:
    """“↩ Back”: collapses the AI variants back into one button “🧠 AI analysis ▾”."""
    message = callback.message
    parts = (callback.data or "").split(":")
    if not isinstance(message, Message) or len(parts) != 4 or not parts[2]:
        await callback.answer()
        return
    tender_id, full = parts[2], parts[3] == "1"
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, message.chat.id)
    rows = [
        r
        for r in (message.reply_markup.inline_keyboard if message.reply_markup else [])
        if not any(
            (b.callback_data or "").startswith(
                _AI_EXPANDED_PREFIXES + (_AI_BACK_PREFIX,)
            )
            for b in r
        )
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=common_t(locale, "btn_ai_menu"),
                callback_data=f"{_AI_MENU_PREFIX}{tender_id}:{int(full)}",
            )
        ]
    )
    await callback.answer()
    with contextlib.suppress(Exception):
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


# The target page of the upsell (#513) — the same cabinet block “AI limits” that
# the project rules name as the place of visibility of the remainder/CTA. The domain — REGISTRY §2
# (app.nab.rs), already used as a text hint in the gate_* lines above
# of this module. No new ENV: a constant, not a config.
_UPSELL_URL = f"{CABINET_BASE_URL}/cabinet/subscription"


async def _reply_limit_exhausted(
    callback: CallbackQuery,
    locale: str,
    *,
    level: str,
    text_key: str,
    tier: str | None,
) -> None:
    """An early refusal by the limit (:mod:`src.bots.ai_gate`): an alert BEFORE the first
    ``callback.answer`` + a persistent message with the upsell (#513).

    The text of the limit — the existing chrome catalog (``limit_deep``/``limit_ai``/
    ``limit_draft``, short and without HTML — safe both for the popup and for the
    message). The upsell — ONLY from :mod:`src.plans.catalog`
    (``upsell_for_level``/``level_limit_for_plan``): the figures of the plans are not duplicated
    in a third place. No plan (the client is at the ceiling of the ladder) → a message without
    a button, but without the upsell one cannot stay silent — the base text already names when
    the limit will be renewed.
    """
    text = bot_t(locale, text_key)
    await callback.answer(text, show_alert=True)

    message = callback.message
    if message is None:
        return

    plan = upsell_for_level(level, tier)
    if plan is None:
        await message.answer(text)
        return

    limit = level_limit_for_plan(plan, level)
    limit_str = "∞" if limit is None else str(limit)
    upsell_body = bot_t(locale, "limit_upsell_body").format(
        plan=plan.key.capitalize(), limit=limit_str, price=plan.price_eur
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "limit_upsell_cta"), url=_UPSELL_URL
                )
            ]
        ]
    )
    await message.answer(f"{text}\n\n{upsell_body}", reply_markup=markup)


@router.callback_query(F.data.startswith("ai:deep:"))
async def on_deep_analysis(callback: CallbackQuery) -> None:
    """The button “🧠 Deep analysis”: ``ai:deep:{tender_id}`` → on-demand L3.

    The order of actions (the anti-staleness of Telegram, 10 s):
      1. An immediate callback.answer() with the toast “⏳ Starting…” BEFORE the model call.
         Without it Telegram will show the “clock” and after 10 s — an error.
      2. session_scope → run_deep_analysis (the opus call, may take 10–60 s).
      3. On success — send the L3 analysis in chunks (each ≤ 4096 characters).
         On gate/limit/busy/frozen/error — show_alert=True with an explanation.

    The Pro+ gate, the freeze (FIX-SG-04), the cache (profile_id NULL, shared per tender) and
    the §14.5 limit are checked inside
    :func:`src.bots.deep_analysis_service.run_deep_analysis` on the press, NOT on the formation
    of the card. The frozen one (``status='frozen'``) does NOT call opus.
    """
    # We parse the tender_id from the callback data “ai:deep:{tender_id}”.
    data = callback.data or ""
    prefix = "ai:deep:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    tender_id = data[len(prefix) :]
    if not tender_id:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    # Lazy import: do not pull ai_client on the import of the bot module.
    from src.bots.ai_gate import ai_gate
    from src.bots.deep_analysis_service import run_deep_analysis

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — the toast/gates; the body of the L3 — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        # The gate as the FIRST action (#513): we learn the limit BEFORE the only
        # callback.answer() — Telegram answers the press exactly once.
        gate = await ai_gate(session, chat_id=chat_id, level="deep")
        if gate.status == "limit_exhausted":
            await _reply_limit_exhausted(
                callback, locale, level="deep", text_key="limit_deep", tier=gate.tier
            )
            return
        # An immediate toast BEFORE the model call is mandatory (the Telegram 10 s limit).
        await callback.answer(bot_t(locale, "ai_deep_progress"))
        result = await run_deep_analysis(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            locale=locale,
        )

    if result.status == "success" and result.html_chunks:
        # We send the analysis in chunks (each ≤ 4096 characters).
        try:
            for chunk in result.html_chunks:
                await callback.message.answer(chunk)
        except (
            Exception
        ):  # noqa: BLE001 — the message was deleted / the chat is unavailable
            logger.warning(
                "deep: could not send L3 chat=%s tender=%s", chat_id, tender_id
            )
    elif result.status == "frozen":
        # The freeze gate (FIX-SG-04): the subscription has expired — opus was NOT called.
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_deep"))
    elif result.status == "limit_exhausted":
        await callback.message.answer(bot_t(locale, "limit_deep"))
    elif result.status == "model_busy":
        await callback.message.answer(bot_t(locale, "model_busy"))
    else:
        # model_error / no_tender / other
        await callback.message.answer(bot_t(locale, "err_deep"))


@router.callback_query(F.data.startswith("ai:bid:"))
async def on_bid_verdict(callback: CallbackQuery) -> None:
    """The button “🎯 Is it worth it?”: ``ai:bid:{tender_id}`` → the bid/no-bid verdict.

    profile_id is resolved on the server (chat_id → tenant → the latest profile).
    A defensive parsing of the old format ``ai:bid:{tender_id}:{profile_id}``: if
    there is a “:” in the tail, the part up to the first “:” is taken as tender_id.
    Order: an immediate callback.answer() BEFORE the model call (anti-staleness 10 s),
    then run_bid_verdict (balanced, profile-aware).
    The Pro+ gate, the freeze (FIX-SG-04) and the cache — inside bid_verdict_service.
    """
    data = callback.data or ""
    prefix = "ai:bid:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    rest = data[len(prefix) :]
    if not rest:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_callback"), show_alert=True
        )
        return
    # The new format: {tender_id} (43 bytes). Defensively: the old {tender_id}:{profile_id}.
    tender_id = rest.split(":", 1)[0]
    if not tender_id:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_callback"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    from src.bots.ai_gate import ai_gate
    from src.bots.bid_verdict_service import run_bid_verdict

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/gates/hints; the verdict — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        # The gate as the FIRST action (#513): we learn the limit BEFORE the only
        # callback.answer() — Telegram answers the press exactly once.
        gate = await ai_gate(session, chat_id=chat_id, level="bid")
        if gate.status == "limit_exhausted":
            await _reply_limit_exhausted(
                callback, locale, level="bid", text_key="limit_ai", tier=gate.tier
            )
            return
        await callback.answer(bot_t(locale, "ai_bid_progress"))
        tenant_id = await resolve_tenant_id(session, chat_id)
        if tenant_id is None:
            await callback.message.answer(
                bot_t(locale, "watching_no_account"),
                reply_markup=_unlinked_markup(locale),
            )
            return
        profile = await _latest_profile(session, tenant_id)
        if profile is None:
            await callback.message.answer(bot_t(locale, "bid_no_profile"))
            return
        profile_id = str(profile.id)
        result = await run_bid_verdict(
            session,
            tender_id=tender_id,
            profile_id=profile_id,
            chat_id=chat_id,
            locale=locale,
        )

    if result.status == "success" and result.html:
        await _send_ai_chunks(
            callback.message,
            _ai_reply_chunks(result.html, locale, tender_id, profile_id),
            label="bid",
            chat_id=chat_id,
            tender_id=tender_id,
        )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_bid"))
    elif result.status == "limit_exhausted":
        await callback.message.answer(bot_t(locale, "limit_ai"))
    elif result.status == "model_busy":
        await callback.message.answer(bot_t(locale, "model_busy"))
    else:
        # model_error / no_tender / no_profile / other
        await callback.message.answer(bot_t(locale, "err_bid"))


@router.callback_query(F.data.startswith("ai:chk:"))
async def on_compliance_checklist(callback: CallbackQuery) -> None:
    """The button “📋 What to prepare?”: ``ai:chk:{tender_id}`` → the document checklist.

    profile_id is resolved on the server inside compliance_checklist_service
    (chat_id → resolve_tenant_id → the latest profile of the tenant).
    Order: an immediate callback.answer() BEFORE the model call (anti-staleness 10 s),
    then run_compliance_checklist (balanced, profile-aware).
    The Pro+ gate, the freeze (FIX-SG-04) and the cache — inside compliance_checklist_service.
    """
    data = callback.data or ""
    prefix = "ai:chk:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    tender_id = data[len(prefix) :]
    if not tender_id:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    from src.bots.ai_gate import ai_gate
    from src.bots.compliance_checklist_service import run_compliance_checklist

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/gates; the body of the checklist — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        # The gate as the FIRST action (#513): we learn the limit BEFORE the only
        # callback.answer() — Telegram answers the press exactly once.
        gate = await ai_gate(session, chat_id=chat_id, level="checklist")
        if gate.status == "limit_exhausted":
            await _reply_limit_exhausted(
                callback,
                locale,
                level="checklist",
                text_key="limit_ai",
                tier=gate.tier,
            )
            return
        await callback.answer(bot_t(locale, "ai_chk_progress"))
        result = await run_compliance_checklist(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            locale=locale,
        )
        profile_id = (
            await _cabinet_profile_id(session, chat_id)
            if result.status == "success"
            else None
        )

    if result.status == "success" and result.html:
        await _send_ai_chunks(
            callback.message,
            _ai_reply_chunks(result.html, locale, tender_id, profile_id),
            label="chk",
            chat_id=chat_id,
            tender_id=tender_id,
        )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_chk"))
    elif result.status == "limit_exhausted":
        await callback.message.answer(bot_t(locale, "limit_ai"))
    elif result.status == "model_busy":
        await callback.message.answer(bot_t(locale, "model_busy"))
    else:
        # model_error / no_tender / no_profile / other
        await callback.message.answer(bot_t(locale, "err_chk"))


@router.callback_query(F.data.startswith("ai:drf:"))
async def on_draft_application(callback: CallbackQuery) -> None:
    """The button “📝 Bid draft”: ``ai:drf:{tender_id}`` → the AI bid draft.

    profile_id is resolved on the server inside draft_service
    (chat_id → resolve_tenant_id → the latest profile of the tenant).
    Order: an immediate callback.answer() BEFORE the model call (anti-staleness 10 s),
    then run_draft_application (the model is chosen by the plan).
    The Pro+ gate, the freeze (FIX-SG-04) and the cache — inside draft_service.
    """
    data = callback.data or ""
    prefix = "ai:drf:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    tender_id = data[len(prefix) :]
    if not tender_id:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    from src.bots.ai_gate import ai_gate
    from src.bots.draft_service import run_draft_application

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/gates; the body of the draft — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        # The gate as the FIRST action (#513): we learn the limit BEFORE the only
        # callback.answer() — Telegram answers the press exactly once.
        gate = await ai_gate(session, chat_id=chat_id, level="draft")
        if gate.status == "limit_exhausted":
            await _reply_limit_exhausted(
                callback, locale, level="draft", text_key="limit_draft", tier=gate.tier
            )
            return
        await callback.answer(bot_t(locale, "ai_drf_progress"))
        result = await run_draft_application(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            locale=locale,
        )
        profile_id = (
            await _cabinet_profile_id(session, chat_id)
            if result.status == "success"
            else None
        )

    if result.status == "success" and result.html:
        await _send_ai_chunks(
            callback.message,
            _ai_reply_chunks(result.html, locale, tender_id, profile_id),
            label="drf",
            chat_id=chat_id,
            tender_id=tender_id,
        )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_drf"))
    elif result.status == "limit_exhausted":
        await callback.message.answer(bot_t(locale, "limit_draft"))
    elif result.status == "model_busy":
        await callback.message.answer(bot_t(locale, "model_busy"))
    else:
        # model_error / no_tender / no_profile / other
        await callback.message.answer(bot_t(locale, "err_drf"))


def _parse_months(raw: str) -> int | None:
    """Parsing of the :months suffix of the VG-06 callback. '' or '0' → None (all time)."""
    if not raw or raw == "0":
        return None
    try:
        v = int(raw)
        return v if v > 0 else None
    except ValueError:
        return None


def _analytics_period_keyboard(
    tender_id: str,
    months: int | None,
    *,
    cb_prefix: str,
    show_competitor: bool = False,
    show_watch: bool = False,
    show_reprocurement: bool = False,
    locale: str | None = None,
) -> InlineKeyboardMarkup:
    """The inline buttons of the period (and optionally of the competitor/watch/reprocurement) for the output of the analytics VG-06/07."""

    def _btn(label: str, m_val: int | None) -> InlineKeyboardButton:
        active = months == m_val
        m_str = "0" if m_val is None else str(m_val)
        return InlineKeyboardButton(
            text=("✓ " + label) if active else label,
            callback_data=f"an:{cb_prefix}:{tender_id}:{m_str}",
        )

    # The period suffix for the export callback (0 = all time).
    m_str = "0" if months is None else str(months)
    # an:exc: = customer export; an:exw: = competitor export
    exc_cb = cb_prefix == "cust"

    rows: list[list[InlineKeyboardButton]] = [
        [
            _btn(bot_t(locale, "an_btn_12m"), 12),
            _btn(bot_t(locale, "an_btn_36m"), 36),
            _btn(bot_t(locale, "an_btn_all"), None),
        ]
    ]
    if show_competitor:
        rows.append(
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "an_btn_comp"),
                    callback_data=f"an:comp:{tender_id}",
                )
            ]
        )
    # The button “🔔 Watch” — only for the competitor profile (cb_prefix=“comp”)
    # an:wch:{tender_id} = 7 + 36 = 43 bytes (≤64)
    if show_watch:
        rows.append(
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "an_btn_watch"),
                    callback_data=f"an:wch:{tender_id}",
                )
            ]
        )
    # an:repr:{tender_id} = 8 + 36 = 44 bytes (≤64)
    if show_reprocurement:
        rows.append(
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "an_btn_repr"),
                    callback_data=f"an:repr:{tender_id}",
                )
            ]
        )
    # The CSV export button — for the customer (cust) and the competitor (comp).
    export_prefix = "exc" if exc_cb else "exw"
    rows.append(
        [
            InlineKeyboardButton(
                text=bot_t(locale, "an_btn_export"),
                callback_data=f"an:{export_prefix}:{tender_id}:{m_str}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("an:cust:"))
async def on_customer_analytics(callback: CallbackQuery) -> None:
    """The button “🏛 Who wins?”: ``an:cust:{tender_id}[:{months}]`` → the customer analytics (VG-06).

    The callback format: ``an:cust:{tender_id}`` (all time, backward compatibility)
    or ``an:cust:{tender_id}:{months}`` (12 / 36 / 0=all time).
    An immediate callback.answer() BEFORE the request (anti-staleness 10 s), then
    run_customer_analytics (read-only SQL, no model). The Pro+ gate and the freeze —
    inside the service.
    """
    data = callback.data or ""
    prefix = "an:cust:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    after = data[len(prefix) :]
    if not after:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return
    if ":" in after:
        tender_id, months_raw = after.split(":", 1)
        months = _parse_months(months_raw)
    else:
        tender_id = after
        months = None

    chat_id = callback.message.chat.id

    from src.bots.awards_analytics_service import run_customer_analytics

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/gates; the body of the analytics — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        await callback.answer(bot_t(locale, "an_cust_progress"))
        result = await run_customer_analytics(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            months=months,
            locale=locale,
        )

    if result.status == "success" and result.html:
        try:
            await callback.message.answer(
                result.html,
                reply_markup=_analytics_period_keyboard(
                    tender_id,
                    months,
                    cb_prefix="cust",
                    show_competitor=True,
                    show_reprocurement=True,
                    locale=locale,
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "analytics: could not send chat=%s tender=%s", chat_id, tender_id
            )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_cust"))
    elif result.status == "no_customer":
        await callback.message.answer(bot_t(locale, "no_customer"))
    elif result.status == "no_data":
        await callback.message.answer(bot_t(locale, "no_data_cust"))
    else:
        # no_tender / other
        await callback.message.answer(bot_t(locale, "err_cust"))


@router.callback_query(F.data.startswith("an:comp:"))
async def on_competitor_analytics(callback: CallbackQuery) -> None:
    """The button “🥊 Strongest winner”: ``an:comp:{tender_id}[:{months}]`` → the competitor profile (VG-06).

    The callback format: ``an:comp:{tender_id}`` (all time)
    or ``an:comp:{tender_id}:{months}`` (12 / 36 / 0=all time).
    """
    data = callback.data or ""
    prefix = "an:comp:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    after = data[len(prefix) :]
    if not after:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return
    if ":" in after:
        tender_id, months_raw = after.split(":", 1)
        months = _parse_months(months_raw)
    else:
        tender_id = after
        months = None

    chat_id = callback.message.chat.id

    from src.bots.awards_analytics_service import run_competitor_analytics

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/gates; the body of the analytics — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        await callback.answer(bot_t(locale, "an_comp_progress"))
        result = await run_competitor_analytics(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            months=months,
            locale=locale,
        )

    if result.status == "success" and result.html:
        try:
            await callback.message.answer(
                result.html,
                reply_markup=_analytics_period_keyboard(
                    tender_id,
                    months,
                    cb_prefix="comp",
                    show_watch=True,
                    locale=locale,
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "analytics: competitor — could not send chat=%s tender=%s",
                chat_id,
                tender_id,
            )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_comp"))
    elif result.status == "no_customer":
        await callback.message.answer(bot_t(locale, "no_customer"))
    elif result.status == "no_data":
        await callback.message.answer(bot_t(locale, "no_data_comp"))
    else:
        # no_tender / other
        await callback.message.answer(bot_t(locale, "err_comp"))


@router.callback_query(F.data.startswith("an:repr:"))
async def on_reprocurement_analytics(callback: CallbackQuery) -> None:
    """The button “🔮 Expected re-procurements”: ``an:repr:{tender_id}`` → the forecast (VG-07).

    The same gate rules as VG-06 (Pro+, freeze).
    Graceful-empty: if the customer has no data on the terms — an explanatory message.
    """
    data = callback.data or ""
    prefix = "an:repr:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    tender_id = data[len(prefix) :]
    if not tender_id:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    from src.bots.awards_analytics_service import run_reprocurement_forecast

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/gates; the body of the forecast — the content axis.
        locale = await resolve_bot_locale(session, chat_id)
        await callback.answer(bot_t(locale, "an_repr_progress"))
        result = await run_reprocurement_forecast(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            locale=locale,
        )

    if result.status == "success" and result.html:
        try:
            await callback.message.answer(result.html)
        except Exception:  # noqa: BLE001
            logger.warning(
                "reprocurement: could not send chat=%s tender=%s",
                chat_id,
                tender_id,
            )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_repr"))
    elif result.status == "no_customer":
        await callback.message.answer(bot_t(locale, "no_customer"))
    elif result.status == "no_data":
        await callback.message.answer(bot_t(locale, "no_data_repr"))
    else:
        await callback.message.answer(bot_t(locale, "err_repr"))


def _parse_export_callback(data: str, prefix: str) -> tuple[str, int | None] | None:
    """Parses ``an:{prefix}:{tender_id}[:{months}]``. None on incorrect data."""
    if not data.startswith(prefix):
        return None
    after = data[len(prefix) :]
    if not after:
        return None
    if ":" in after:
        tender_id, months_raw = after.split(":", 1)
        months = _parse_months(months_raw)
    else:
        tender_id = after
        months = None
    return tender_id, months


async def _send_export_error(
    callback: CallbackQuery, status: str, kind: str, locale: str
) -> None:
    """Sends the user the error text by the status of the export (the chrome by locale)."""
    if status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif status == "gate_pro":
        key = "gate_export_cust" if kind == "customer" else "gate_export_comp"
        await callback.message.answer(bot_t(locale, key))
    elif status == "no_customer":
        await callback.message.answer(bot_t(locale, "no_customer_export"))
    elif status == "no_data":
        await callback.message.answer(bot_t(locale, "no_data_export"))
    else:
        await callback.message.answer(bot_t(locale, "err_export"))


@router.callback_query(F.data.startswith("an:exc:"))
async def on_customer_export(callback: CallbackQuery) -> None:
    """The button “📊 Export CSV” (the customer): ``an:exc:{tender_id}[:{months}]`` (VG-06 E3a)."""
    parsed = _parse_export_callback(callback.data or "", "an:exc:")
    if parsed is None:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return
    tender_id, months = parsed

    chat_id = callback.message.chat.id

    from src.bots.awards_analytics_service import run_customer_export

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/label/gates; the body of the CSV — data.
        locale = await resolve_bot_locale(session, chat_id)
        await callback.answer(bot_t(locale, "an_export_progress"))
        result = await run_customer_export(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            months=months,
            locale=locale,
        )

    if result.status == "success":
        try:
            await callback.message.answer_document(
                BufferedInputFile(result.content, filename=result.filename),
                caption=bot_t(locale, "export_caption"),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "export: customer — could not send chat=%s tender=%s",
                chat_id,
                tender_id,
            )
    else:
        await _send_export_error(callback, result.status, "customer", locale)


@router.callback_query(F.data.startswith("an:exw:"))
async def on_competitor_export(callback: CallbackQuery) -> None:
    """The button “📊 Export CSV” (the competitor): ``an:exw:{tender_id}[:{months}]`` (VG-06 E3a)."""
    parsed = _parse_export_callback(callback.data or "", "an:exw:")
    if parsed is None:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return
    tender_id, months = parsed

    chat_id = callback.message.chat.id

    from src.bots.awards_analytics_service import run_competitor_export

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/label/gates; the body of the CSV — data.
        locale = await resolve_bot_locale(session, chat_id)
        await callback.answer(bot_t(locale, "an_export_progress"))
        result = await run_competitor_export(
            session,
            tender_id=tender_id,
            chat_id=chat_id,
            months=months,
            locale=locale,
        )

    if result.status == "success":
        try:
            await callback.message.answer_document(
                BufferedInputFile(result.content, filename=result.filename),
                caption=bot_t(locale, "export_caption"),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "export: competitor — could not send chat=%s tender=%s",
                chat_id,
                tender_id,
            )
    else:
        await _send_export_error(callback, result.status, "competitor", locale)


# --------------------------------------------------------------------------- #
#  VG-06 E3b — subscriptions to winners (“🔔 Watch”, /watching, unsubscribe)
# --------------------------------------------------------------------------- #


@router.callback_query(F.data.startswith("an:wch:"))
async def on_watch_winner(callback: CallbackQuery) -> None:
    """The button “🔔 Watch the winner”: ``an:wch:{tender_id}`` → the subscription (VG-06 E3b).

    The winner’s PIB is resolved on the server (tender_id → customer → top winner),
    as in run_competitor_analytics. The Pro+/freeze gate. The watermark: the existing
    award PIBs are written into sent at once → the alerts only about NEW wins.
    """
    data = callback.data or ""
    prefix = "an:wch:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    tender_id = data[len(prefix) :]
    if not tender_id:
        await callback.answer(
            bot_t(_tg_locale(callback), "ai_bad_tender"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    from src.bots.award_watch_service import run_watch_winner

    async with session_scope() as session:
        # The locale of the chrome (users.locale) — toast/confirmation/gates; the name/PIB — data.
        locale = await resolve_bot_locale(session, chat_id)
        await callback.answer(bot_t(locale, "an_wch_progress"))
        result = await run_watch_winner(session, tender_id=tender_id, chat_id=chat_id)

    if result.status == "success":
        name = result.winner_name or result.winner_pib
        pib_short = (
            result.winner_pib[:12] + "…"
            if len(result.winner_pib) > 12
            else result.winner_pib
        )
        await callback.message.answer(
            bot_t(locale, "watch_subscribed").format(
                name=html.escape(name), pib=html.escape(pib_short)
            )
        )
    elif result.status == "frozen":
        await callback.message.answer(bot_t(locale, "frozen"))
    elif result.status == "no_account":
        await callback.message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
    elif result.status == "gate_pro":
        await callback.message.answer(bot_t(locale, "gate_watch"))
    elif result.status == "no_data":
        await callback.message.answer(bot_t(locale, "no_data_watch"))
    else:
        await callback.message.answer(bot_t(locale, "err_watch"))


@router.message(Command("watching"))
async def cmd_watching(message: Message) -> None:
    """/watching — the list of active subscriptions + the unsubscribe buttons (VG-06 E3b)."""
    telegram_id = message.from_user.id

    from src.bots.award_watch_service import run_list_watching

    async with session_scope() as session:
        result = await run_list_watching(session, chat_id=telegram_id)
        locale = await resolve_bot_locale(session, telegram_id)

    if result.status == "no_account":
        await message.answer(
            bot_t(locale, "watching_no_account"), reply_markup=_unlinked_markup(locale)
        )
        return

    if not result.items:
        await message.answer(bot_t(locale, "watching_empty"))
        return

    lines = [bot_t(locale, "watching_title")]
    kb_rows = []
    for item in result.items:
        name = item.winner_name
        pib_short = (
            item.winner_pib[:12] + "…" if len(item.winner_pib) > 12 else item.winner_pib
        )
        lines.append(f"• {html.escape(name)} (PIB {html.escape(pib_short)})")
        # an:unw:{sub_id} — a numeric id, ≤64 bytes
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "watching_unsub_btn").format(name=name[:20]),
                    callback_data=f"an:unw:{item.sub_id}",
                )
            ]
        )

    markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.startswith("an:unw:"))
async def on_unwatch(callback: CallbackQuery) -> None:
    """The button “❌ Unsubscribe”: ``an:unw:{subscription_id}`` → deactivation of the subscription.

    Checks that the subscription belongs to the tenant — one cannot unsubscribe someone else's.
    """
    data = callback.data or ""
    prefix = "an:unw:"
    if not data.startswith(prefix):
        await callback.answer()
        return
    sub_id_raw = data[len(prefix) :]
    try:
        sub_id = int(sub_id_raw)
    except (TypeError, ValueError):
        await callback.answer(
            bot_t(_tg_locale(callback), "unwatch_bad_id"), show_alert=True
        )
        return

    chat_id = callback.message.chat.id

    from src.bots.award_watch_service import run_unwatch

    async with session_scope() as session:
        result = await run_unwatch(session, sub_id=sub_id, chat_id=chat_id)
        locale = await resolve_bot_locale(session, chat_id)

    if result.status == "success":
        await callback.answer(bot_t(locale, "unwatch_ok"), show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
    elif result.status == "no_account":
        await callback.answer(bot_t(locale, "watching_no_account"), show_alert=True)
    else:
        await callback.answer(bot_t(locale, "unwatch_not_found"), show_alert=True)


@router.message(Command("profile"))
async def cmd_profile(message: Message) -> None:
    """/profile — shows ALL the subscriber’s profiles (a read from the DB), not only
    the latest: a tenant may keep several templates at the same time (owner
    22.09), and the owner must see all of them, not silently one."""
    telegram_id = message.from_user.id
    async with session_scope() as session:
        data = await get_subscriber(session, telegram_id)
        locale = await resolve_bot_locale(session, telegram_id)
        profiles = (
            await _list_profiles(session, data["tenant"].id)
            if data is not None and data.get("tenant") is not None
            else []
        )
        # The sources of all the profiles — in one query to the registry (without RLS).
        keys = {
            k for p in profiles for k in (getattr(p, "enabled_source_keys", None) or [])
        }
        sources = {
            s.source_key: s
            for s in (
                await list_sources_for_selection(session, sorted(keys))
                if profiles
                else []
            )
        }
    if data is None or not profiles:
        await message.answer(bot_t(locale, "profile_not_found"))
        return
    await message.answer(
        _render_profile(data, profiles, locale, sources=sources),
        reply_markup=manage.manage_keyboard(locale, _PROFILES_CABINET_URL),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    """/reset — for a bound account changes NOTHING, sends to the cabinet.

    The templates (keywords, regions, activity, the language of the AI content) are set up
    in the cabinet. Earlier /reset overwrote the latest profile right from the bot and mixed
    the language axes; now only a reply + a button to the dashboard of the templates (``/app``).

    An unbound chat: the same BOT_SIGNUP_MODE gate as in /start — in
    ``token_only`` a refusal (the onboarding would create a profile bypassing the site); in ``open``
    the onboarding of a NEW subscriber is started.
    """
    await state.clear()
    telegram_id = message.from_user.id
    async with session_scope() as session:
        # We check the binding through SECURITY DEFINER (without a GUC) — the join policy 0022.
        _reset_res = await session.execute(
            _RESOLVE_TENANT_SQL, {"eid": str(telegram_id)}
        )
        existing = _reset_res.scalar()
        locale = await resolve_bot_locale(session, telegram_id)
    if existing is not None:
        await message.answer(
            bot_t(locale, "reset_intro"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=bot_t(locale, "reset_cta"),
                            url=f"{CABINET_BASE_URL}/app",
                        )
                    ]
                ]
            ),
        )
        return
    if get_bot_settings().bot_signup_mode.strip().lower() != "open":
        await message.answer(
            bot_t(locale, "signup_closed"), reply_markup=_unlinked_markup(locale)
        )
        return
    await _start_onboarding(message, state, locale)


async def _list_screen(
    chat_id: int, offset: int = 0, history: tuple[int, ...] = ()
) -> tuple[str, InlineKeyboardMarkup, bool]:
    """The “📋 My tenders” screen: (text, keyboard, is the chat bound?)."""
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        if tenant_id is None:
            return (
                bot_t(locale, "watching_no_account"),
                _unlinked_markup(locale),
                False,
            )
        items, has_more = await manage.load_my_tenders(
            session, tenant_id, offset=offset
        )
    text_, markup = manage.render_my_tenders(
        items,
        offset=offset,
        has_more=has_more,
        locale=locale,
        cabinet_base=CABINET_BASE_URL,
        history=history,
    )
    return text_, markup, True


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    """/list — “📋 My tenders”: in progress (``tender_user_status``) + the sent ones.

    The list and the markup — :mod:`src.bots.manage`; here only the resolution of the chat and the
    reply. The buttons “📂 N” lead to the tender card in the cabinet, “More ▸” — ``ls:<offset>``.
    """
    if message.from_user is None:
        return
    text_, markup, _ = await _list_screen(message.from_user.id)
    await message.answer(text_, reply_markup=markup, disable_web_page_preview=True)


@router.callback_query(F.data.startswith(manage.CB_LIST))
async def on_list_page(callback: CallbackQuery) -> None:
    """“More ▸”/“◂ Back” — redraw the list on another page."""
    message = callback.message
    page = manage.parse_list_callback(callback.data)
    if not isinstance(message, Message) or page is None:
        await callback.answer()
        return
    offset, history = page
    text_, markup, _ = await _list_screen(message.chat.id, offset, history)
    await callback.answer()
    try:
        await message.edit_text(
            text_, reply_markup=markup, disable_web_page_preview=True
        )
    except Exception as exc:  # noqa: BLE001 — “message is not modified” etc.
        logger.info("list redraw skipped: %s", type(exc).__name__)


async def _pause_screen(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """The pause screen of the tenant’s templates of THIS channel (the keyboard ``None`` — nothing to show)."""
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        if tenant_id is None:
            return bot_t(locale, "watching_no_account"), _unlinked_markup(locale)
        profiles = await manage.load_profiles(session, tenant_id)
    if not profiles:
        return bot_t(locale, "profile_not_found"), None
    return manage.pause_text(profiles, locale), manage.pause_keyboard(
        profiles, locale, _PROFILES_CABINET_URL
    )


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    """/pause — the toggles ✅/⏸ of the templates (``profiles.is_active``) + “all”.

    The tenant — of the channel from which the command came (``resolve_tenant_id``): a chat_id
    may have several channels of different tenants, the foreign templates are not
    visible and are not switched.
    """
    if message.from_user is None:
        return
    text_, markup = await _pause_screen(message.from_user.id)
    await message.answer(text_, reply_markup=markup)


async def _redraw_pause(message: Message, chat_id: int) -> None:
    text_, markup = await _pause_screen(chat_id)
    try:
        await message.edit_text(text_, reply_markup=markup)
    except Exception as exc:  # noqa: BLE001 — “message is not modified” etc.
        logger.info("pause redraw skipped: %s", type(exc).__name__)


@router.callback_query(F.data.startswith(manage.CB_TOGGLE))
async def on_pause_toggle(callback: CallbackQuery) -> None:
    """The toggle of one template: pause ↔ work; a refusal (limit/not found) — a window."""
    message = callback.message
    profile_id = manage.parse_toggle_callback(callback.data)
    if not isinstance(message, Message) or profile_id is None:
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        outcome = (
            await manage.toggle_profile(session, tenant_id, profile_id)
            if tenant_id
            else manage.GONE
        )
    await callback.answer(
        bot_t(locale, f"pause_toast_{outcome}"),
        show_alert=outcome in (manage.LIMIT, manage.GONE),
    )
    await _redraw_pause(message, chat_id)


@router.callback_query(F.data.startswith(manage.CB_ALL))
async def on_pause_all(callback: CallbackQuery) -> None:
    """“⏸ Pause all” / “▶ Resume all” (resume — within the plan limit)."""
    message = callback.message
    active = manage.parse_all_callback(callback.data)
    if not isinstance(message, Message) or active is None:
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        if tenant_id is None:
            await callback.answer(bot_t(locale, "watching_no_account"), show_alert=True)
            return
        result = await manage.set_all_profiles(session, tenant_id, active)
    await callback.answer(
        manage.bulk_toast(result, active, locale), show_alert=bool(result.blocked)
    )
    await _redraw_pause(message, chat_id)


@router.callback_query(F.data.in_({manage.CB_MENU_PAUSE, manage.CB_MENU_LIST}))
async def on_manage_menu(callback: CallbackQuery) -> None:
    """The buttons “⏸ Pause”/“📋 My tenders” under /profile and /status — in a new message."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    chat_id = message.chat.id
    await callback.answer()
    if callback.data == manage.CB_MENU_PAUSE:
        text_, markup = await _pause_screen(chat_id)
        await message.answer(text_, reply_markup=markup)
    else:
        text_, list_markup, _ = await _list_screen(chat_id)
        await message.answer(
            text_, reply_markup=list_markup, disable_web_page_preview=True
        )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """/status — the subscription status and the plan (SITE-28).

    Shows the real plan/access term from ``tenants`` through
    :func:`src.bots.status_service.get_status_summary` (the ``access_until`` axis, the same
    freeze boundary as for the funnel and the deep analysis). If the chat is not bound to an
    account (no /start) — a hint to go through the onboarding. Billing is out of the MVP scope.
    """
    telegram_id = message.from_user.id
    async with session_scope() as session:
        tenant_id = await resolve_tenant_id(session, telegram_id)
        if tenant_id is None:
            locale = await resolve_bot_locale(session, telegram_id)
            await message.answer(
                bot_t(locale, "status_no_account"),
                reply_markup=_unlinked_markup(locale),
            )
            return
        summary = await get_status_summary(session, tenant_id)
        locale = await resolve_bot_locale(session, telegram_id)
    # The name of the plan — the common vocabulary (COMMON/bot), not an ru string of status_service.
    tier_label = _tier_label(summary["tier"], locale, summary.get("status"))
    lines = [bot_t(locale, "status_title"), ""]
    # The account — as the first line, as in /profile: one chat_id may be bound to
    # several accounts, and without the email the subscriber does not know whose status they read.
    lines.append(
        f"• {bot_t(locale, 'lbl_account')}: {html.escape(summary.get('email') or '—')}"
    )
    lines.append(f"• {bot_t(locale, 'lbl_tier')}: <b>{tier_label}</b>")
    lines.append(
        _access_line(summary["access_until"], datetime.now(timezone.utc), locale)
    )
    await message.answer(
        "\n".join(lines) + bot_t(locale, "help_hint"),
        reply_markup=manage.manage_keyboard(locale, _PROFILES_CABINET_URL),
    )


@router.message(Command("language"))
async def cmd_language(message: Message) -> None:
    """/language — the language of the bot’s messages and cards IN THIS chat (owner, stage 2c).

    The cabinet language (``users.locale``) is NOT changed by the command — it refines the language
    of exactly this delivery channel (``user_channels.locale``, 0038). Until the choice is
    made, the channel inherits the cabinet language, therefore “before” and “after” the first change
    the subscriber sees the same thing — exactly what the owner wanted.
    """
    locale = norm_locale(None)
    if message.from_user is not None:
        async with session_scope() as session:
            locale = await resolve_bot_locale(session, message.from_user.id)
    await message.answer(
        bot_t(locale, "lang_prompt"),
        reply_markup=_language_keyboard("lang:set:"),
    )


@router.callback_query(F.data.startswith("lang:set:"))
async def on_language_set(callback: CallbackQuery) -> None:
    """The language-choice button from ``/language``: writes the language INTO THE CHANNEL, replies in it.

    A separate prefix from the onboarding (``ob:lang:``, caught only in the FSM state):
    there the language goes into the data of the wizard and travels into the profile at the
    confirmation step, here — straight into ``user_channels.locale``.
    """
    code = (callback.data or "").split(":")[-1]
    # Narrowing, and not `# type: ignore`: InaccessibleMessage (a message older than
    # 48 hours) has neither .chat nor .answer — there is nowhere to reply, we dismiss the callback itself.
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        outcome = await set_channel_locale(session, chat_id, code)
        # The confirmation — already in the NEW language: we re-read the locale by the same
        # resolver as the other handlers (it now looks at the channel).
        locale = await resolve_bot_locale(session, chat_id)
    if outcome == "ok":
        await message.answer(
            bot_t(locale, "lang_changed").format(lang=_LANGUAGES.get(code, code))
        )
    elif outcome == "no_account":
        await message.answer(
            bot_t(locale, "lang_no_account"), reply_markup=_unlinked_markup(locale)
        )
    else:
        await message.answer(bot_t(locale, "lang_unknown"))
    await callback.answer()


async def _send_everywhere_notice(message: Message) -> None:
    """One message “a new source — in all templates”, if there is one (step 4 of the brief).

    Only on “🗂 Sources” — the user came to the sources himself; on /start we do not send
    the offer (without pushiness, 16.10). There is no “not now” refusal in the bot
    (there is no place for the state without a migration), therefore the message repeats
    on “🗂 Sources” until the source is turned on — see REPORT.
    """
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, message.chat.id)
        tenant_id = await resolve_tenant_id(session, message.chat.id)
        if not tenant_id:
            return
        profiles = source_banner.live_profiles(await _list_profiles(session, tenant_id))
        pending = await source_banner.dashboard_banner(session, profiles, None)
    if pending is None:
        return
    text_, markup = source_choice.everywhere_notice(
        pending, profile_total=len(profiles), locale=locale
    )
    await message.answer(text_, reply_markup=markup)


async def _izvori_screen(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    """The screen of the sources: one profile → the toggles, several → the choice of the profile."""
    sources: list[SourceInfo] = []
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        profiles = await _list_profiles(session, tenant_id) if tenant_id else []
        if len(profiles) == 1:
            sources = await list_sources_for_selection(
                session, profiles[0].enabled_source_keys or []
            )
    if not profiles:
        return bot_t(locale, "profile_not_found"), None
    if len(profiles) > 1:
        return bot_t(locale, "src_pick_profile"), source_choice.profiles_keyboard(
            profiles, locale
        )
    profile = profiles[0]
    return source_choice.menu_text(
        profile, sources, locale
    ), source_choice.menu_keyboard(profile, sources, locale, with_back=False)


@router.message(Command("izvori"))
async def cmd_izvori(message: Message) -> None:
    """/izvori — the sources (portals) of the template: the toggles ☑/☐ (SRC-0b, R6).

    One profile → the toggles at once; several → first the choice of the profile (in production on
    30.09 this was so for 1 tenant out of 6). The language — of the channel (``resolve_bot_locale``).
    """
    if message.from_user is None:
        return
    text_, markup = await _izvori_screen(message.from_user.id)
    await message.answer(text_, reply_markup=markup)


@router.callback_query(F.data == manage.CB_MENU_SOURCES)
async def on_manage_sources(callback: CallbackQuery) -> None:
    """The button “🗂 Sources” under /profile and /status — the /izvori screen in a new message."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    await callback.answer()
    text_, markup = await _izvori_screen(message.chat.id)
    await message.answer(text_, reply_markup=markup)
    await _send_everywhere_notice(message)


@router.callback_query(F.data == "src:l")
async def on_izvori_list(callback: CallbackQuery) -> None:
    """“⬅ Templates” from the sources screen — back to the choice of the profile."""
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        profiles = await _list_profiles(session, tenant_id) if tenant_id else []
    if not profiles:
        await callback.answer(bot_t(locale, "profile_not_found"), show_alert=True)
        return
    await message.edit_text(
        bot_t(locale, "src_pick_profile"),
        reply_markup=source_choice.profiles_keyboard(profiles, locale),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(source_choice.CB_PROFILE))
async def on_izvori_profile(callback: CallbackQuery) -> None:
    """A profile is chosen → the screen of its sources."""
    message = callback.message
    profile_id = source_choice.parse_profile_callback(callback.data)
    if not isinstance(message, Message) or profile_id is None:
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        profile = (
            await source_choice.load_profile(session, tenant_id, profile_id)
            if tenant_id
            else None
        )
        sources = (
            await list_sources_for_selection(session, profile.enabled_source_keys or [])
            if profile is not None
            else []
        )
    if profile is None:
        await callback.answer(bot_t(locale, "profile_not_found"), show_alert=True)
        return
    await message.edit_text(
        source_choice.menu_text(profile, sources, locale),
        reply_markup=source_choice.menu_keyboard(
            profile, sources, locale, with_back=True
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(source_choice.CB_TOGGLE))
async def on_izvori_toggle(callback: CallbackQuery) -> None:
    """The toggle of a source: the write through ``apply_source_selection`` + a redraw.

    The confirmation — a toast in the language of the channel; a refusal (the last source,
    a stale button) — a pop-up window, so that it cannot be missed.
    """
    message = callback.message
    ref = source_choice.parse_toggle_callback(callback.data)
    if not isinstance(message, Message) or ref is None:
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        profile = (
            await source_choice.load_profile(session, tenant_id, ref.profile_id)
            if tenant_id
            else None
        )
        if profile is None:
            await callback.answer(bot_t(locale, "profile_not_found"), show_alert=True)
            return
        outcome, source, _ = await source_choice.toggle_source(
            session, profile, ref, datetime.now(timezone.utc)
        )
        # The redraw — by the current registry and the number of profiles (the “back” button).
        sources = await list_sources_for_selection(
            session, profile.enabled_source_keys or []
        )
        tenant_id = await resolve_tenant_id(session, chat_id)
        with_back = len(await _list_profiles(session, tenant_id)) > 1
    name = source_choice.source_label(source, locale) if source is not None else ""
    await callback.answer(
        bot_t(locale, f"src_toast_{outcome}").format(name=name),
        show_alert=outcome in (source_choice.LAST, source_choice.STALE),
    )
    try:
        await message.edit_text(
            source_choice.menu_text(profile, sources, locale),
            reply_markup=source_choice.menu_keyboard(
                profile, sources, locale, with_back=with_back
            ),
        )
    except Exception as exc:  # noqa: BLE001 — “message is not modified” etc.
        logger.info("izvori redraw skipped: %s", type(exc).__name__)


@router.callback_query(F.data.startswith(source_choice.CB_ENABLE_ALL))
async def on_enable_all_templates(callback: CallbackQuery) -> None:
    """“Turn on in all templates” from the notification — the same service as the cabinet banner.

    The write only on the press. The source must be in the registry (otherwise a refusal);
    a repeated press changes nothing (the templates where it is set are not touched).
    """
    message = callback.message
    key = source_choice.parse_enable_all_callback(callback.data)
    if not isinstance(message, Message) or key is None:
        await callback.answer()
        return
    chat_id = message.chat.id
    async with session_scope() as session:
        locale = await resolve_bot_locale(session, chat_id)
        tenant_id = await resolve_tenant_id(session, chat_id)
        if not tenant_id:
            await callback.answer(bot_t(locale, "profile_not_found"), show_alert=True)
            return
        _total, with_key = await source_banner.enable_everywhere(
            session, tenant_id, key, datetime.now(timezone.utc)
        )
    if not with_key:
        await callback.answer(bot_t(locale, "src_banner_failed"), show_alert=True)
        return
    await callback.answer(bot_t(locale, "src_banner_done").format(n=with_key))
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 — “message is not modified” etc.
        logger.info("enable-all redraw skipped: %s", type(exc).__name__)


# --------------------------------------------------------------------------- #
# The factories — lazy instantiation (no network on the import of the module).
# --------------------------------------------------------------------------- #


def build_bot(settings: BotSettings | None = None) -> Bot:
    """Creates an instance of :class:`Bot`.

    The token is taken from the settings (ENV ``TG_BOT_TOKEN``). There are no network calls on
    the creation — Bot contacts Telegram only on the first API method.
    """
    settings = settings or get_bot_settings()
    if not settings.tg_bot_token:
        raise RuntimeError(
            "TG_BOT_TOKEN is not set. Set the token in ENV (or infra/secrets) "
            "before starting the bot."
        )
    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # owner 10.10.2026: sr — Latin only; the tender data is in Cyrillic in the DB.
    bot.session.middleware(SrLatinMiddleware())
    return bot


def build_dispatcher() -> Dispatcher:
    """Creates a :class:`Dispatcher` with the commands router connected.

    The FSM storage is the default ``MemoryStorage`` of aiogram 3 (enough for
    one process; we will connect a Redis storage at horizontal scaling).
    """
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(trial_router)
    return dp


# --------------------------------------------------------------------------- #
# Webhook (ARCHITECTURE §9). It is raised by a separate aiohttp application.
# NOT connected in src/api/main.py — this is Track F, the wiring in Sprint-17.
# --------------------------------------------------------------------------- #


def build_webhook_app(
    bot: Bot | None = None,
    dispatcher: Dispatcher | None = None,
    settings: BotSettings | None = None,
):
    """Assembles the aiohttp application that accepts the Telegram updates by webhook.

    Returns an ``aiohttp.web.Application`` with the registered
    ``SimpleRequestHandler`` on ``settings.tg_webhook_path``.

    The import of the aiohttp/aiogram webhook utilities is local (inside the function), so that
    the import of the module :mod:`src.bots.telegram` does not pull the aiohttp stack without need
    (the polling mode and the unit tests do not require it).

    TODO(Sprint-17, Track F): mount this handler into the main
    FastAPI application (``src/api/main.py``) instead of a separate aiohttp server,
    or proxy through Caddy — it is decided in Track F.
    """
    from aiogram.webhook.aiohttp_server import (
        SimpleRequestHandler,
        setup_application,
    )
    from aiohttp import web

    settings = settings or get_bot_settings()
    bot = bot or build_bot(settings)
    dispatcher = dispatcher or build_dispatcher()

    app = web.Application()
    handler = SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.tg_webhook_secret or None,
    )
    handler.register(app, path=settings.tg_webhook_path)
    setup_application(app, dispatcher, bot=bot)
    return app


async def setup_webhook(
    bot: Bot | None = None,
    settings: BotSettings | None = None,
) -> None:
    """Registers the webhook in Telegram (``setWebhook``).

    Makes a real network call, therefore it is called at the start of the webhook service,
    and not on the import. If ``webhook_url`` is empty — it does nothing (the bot will
    work in the polling mode).
    """
    settings = settings or get_bot_settings()
    if not settings.webhook_url:
        logger.warning(
            "TG_WEBHOOK_BASE_URL is not set — the webhook is not configured "
            "(the polling mode is expected)."
        )
        return
    bot = bot or build_bot(settings)
    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.tg_webhook_secret or None,
        drop_pending_updates=True,
    )
    await register_bot_commands(bot, settings)
    logger.info("Webhook is set: %s", settings.webhook_url)


# --------------------------------------------------------------------------- #
# Polling — the owner’s local test (without a webhook/public URL).
# --------------------------------------------------------------------------- #


async def run_polling() -> None:
    """Runs long-polling. Convenient for a local test without a public URL."""
    settings = get_bot_settings()
    bot = build_bot(settings)
    dispatcher = build_dispatcher()
    # In case a webhook was set earlier — we remove it, otherwise the polling conflicts.
    await bot.delete_webhook(drop_pending_updates=True)
    await register_bot_commands(bot, settings)
    logger.info("Starting polling…")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    from src.log_secrets import install_secret_log_filter

    logging.basicConfig(level=logging.INFO)
    install_secret_log_filter()
    asyncio.run(run_polling())
