"""Management from the bot: “📋 My tenders” (``/list``) and the template pause (``/pause``).

The logic and keyboards are separate from the handlers (:mod:`src.bots.telegram`), like
:mod:`src.bots.source_choice`: there is no aiogram router here, only the DB session and
pure functions — tested with a stub of the session.

RLS: the tenant is resolved by the caller (``resolve_tenant_id`` — SECURITY DEFINER by
chat_id, the same order of channels as the delivery), then every function itself
sets the GUC (``repo.set_tenant``) — ``set_config(..., is_local)`` lives until
the end of the transaction, and the toggle commits, so the GUC is set again after the write.
In all queries — an explicit ``tenant_id`` on a par with the policy.

The pause of a template — the axis ``profiles.is_active`` (the matching takes only
``is_active = true``). The write goes through the SAME service as the toggle of the cabinet dashboard
(:func:`src.api.cabinet.profile_service.set_profile_active`), and with the same gate
of activation by the plan limit as ``POST /profiles/{id}/activate``: after
a plan downgrade there are legitimately more templates than the limit, and activating beyond it is not allowed
either in the cabinet or in the bot. The cabinet toggle does not write ``profile_revisions`` (a revision is a snapshot
of the FORM, not of the switch), and neither does the bot — one semantics for both entries.

Addressing of the buttons (``callback_data`` ≤64 bytes):

* ``pz:t:<profile_uuid>`` — the toggle of one template (41 bytes);
* ``pz:a:0`` / ``pz:a:1`` — “pause all” / “resume all”;
* ``ls:<offset>`` — the page of “My tenders”;
* ``mg:pause`` / ``mg:list`` / ``mg:src`` — the buttons under ``/profile`` and ``/status``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, text

from src.api import repo, tiers
from src.bots.i18n import bot_t, common_t
from src.db.models import Profile
from src.workers.adapters.base import format_amount_pair, format_deadline

CB_TOGGLE = "pz:t:"
CB_ALL = "pz:a:"
CB_LIST = "ls:"
CB_MENU_PAUSE = "mg:pause"
CB_MENU_LIST = "mg:list"
CB_MENU_SOURCES = "mg:src"

#: The outcomes of the toggle (the keys of the toast — ``pause_toast_<outcome>`` in the catalog).
PAUSED, RESUMED, LIMIT, GONE = "paused", "resumed", "limit", "gone"

#: Tenders on a page of “My tenders”.
PAGE_SIZE = 10
#: The window of “delivered” — for how many days to show what was sent to this channel.
SENT_WINDOW_DAYS = 30
_TELEGRAM_MAX_MESSAGE = 4096
_TITLE_MAX = 90


def _svc():
    """The cabinet template service — by a lazy import.

    The module pulls ``channel_service`` → ``emailer`` (SMTP env), which the bot’s container
    does not have and does not need: the toggle needs only ``get_profile`` /
    ``set_profile_active`` / the counters. At the top level the import would count as a
    startup dependency of the bot (the guard ``test_env_contract``).
    """
    from src.api.cabinet import profile_service

    return profile_service


def _uuid(raw: str) -> str | None:
    try:
        return str(UUID(raw))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Templates: the list and the pause.
# --------------------------------------------------------------------------- #


async def ordered_profiles(session, tenant_id) -> list[Profile]:
    """The non-deleted templates of the tenant in the order of ``/profile`` (GUC — on the caller).

    The active first, then the disabled ones (``NULL`` — also “disabled”, the matching is silent for it),
    inside — by name and id: the order is deterministic, the buttons do not
    jump between redraws.
    """
    result = await session.execute(
        select(Profile)
        .where(Profile.tenant_id == tenant_id, Profile.deleted_at.is_(None))
        .order_by(Profile.is_active.desc().nullslast(), Profile.name, Profile.id)
    )
    return list(result.scalars().all())


async def load_profiles(session, tenant_id) -> list[Profile]:
    """The same as :func:`ordered_profiles`, but sets the GUC itself (after commit)."""
    await repo.set_tenant(session, str(tenant_id))
    return await ordered_profiles(session, tenant_id)


def toggle_callback(profile_id) -> str:
    return f"{CB_TOGGLE}{profile_id}"


def parse_toggle_callback(data: str | None) -> str | None:
    if not data or not data.startswith(CB_TOGGLE):
        return None
    return _uuid(data[len(CB_TOGGLE) :])


def parse_all_callback(data: str | None) -> bool | None:
    """``pz:a:1`` → ``True`` (resume all), ``pz:a:0`` → ``False``; otherwise ``None``."""
    if data == f"{CB_ALL}1":
        return True
    if data == f"{CB_ALL}0":
        return False
    return None


async def _enable_budget(session, tenant_id: str) -> int | None:
    """How many more templates can be activated by the plan (``None`` — no limit).

    The same gate as ``POST /profiles/{id}/activate`` in the cabinet: the plan limit minus
    the templates already working.
    """
    limit = tiers.profile_limit(await _svc().get_tenant_tier(session, tenant_id))
    if limit is None:
        return None
    enabled = await _svc().count_enabled_profiles(session, tenant_id)
    return max(limit - enabled, 0)


async def toggle_profile(session, tenant_id, profile_id: str) -> str:
    """Switch the pause of one template of ONE’S OWN tenant.

    A foreign/deleted/non-existent ``profile_id`` is not found (``tenant_id`` in the
    WHERE on a par with the RLS) → ``GONE`` without a write. Activation beyond the plan limit →
    ``LIMIT`` without a write.
    """
    tid = str(tenant_id)
    profile = await _svc().get_profile(session, tid, profile_id)
    if profile is None:
        return GONE
    if profile.is_active is True:
        await _svc().set_profile_active(
            session, tenant_id=tid, profile_id=profile_id, is_active=False
        )
        return PAUSED
    budget = await _enable_budget(session, tid)
    if budget is not None and budget <= 0:
        return LIMIT
    ok = await _svc().set_profile_active(
        session, tenant_id=tid, profile_id=profile_id, is_active=True
    )
    return RESUMED if ok else GONE


@dataclass(frozen=True)
class BulkResult:
    changed: int
    blocked: int  # not activated because of the plan limit


async def set_all_profiles(session, tenant_id, active: bool) -> BulkResult:
    """“⏸ Pause all” / “▶ Resume all” — one template at a time by the same service.

    Activation goes in the order of :func:`ordered_profiles` and stops at the
    plan limit; the rest are counted in ``blocked`` (the refusal is visible in the toast).
    """
    tid = str(tenant_id)
    profiles = await load_profiles(session, tid)
    targets = [p for p in profiles if (p.is_active is True) != active]
    budget = await _enable_budget(session, tid) if active else None
    changed = blocked = 0
    for profile in targets:
        if active and budget is not None and changed >= budget:
            blocked += 1
            continue
        if await _svc().set_profile_active(
            session, tenant_id=tid, profile_id=str(profile.id), is_active=active
        ):
            changed += 1
    return BulkResult(changed=changed, blocked=blocked)


def _profile_name(profile, locale: str | None) -> str:
    return profile.name or bot_t(locale, "no_profile_name")


def pause_text(profiles, locale: str | None) -> str:
    """The pause screen: which template works, which is paused."""
    lines = [bot_t(locale, "pause_title"), "", bot_t(locale, "pause_hint"), ""]
    for p in profiles:
        mark = "✅" if p.is_active is True else "⏸"
        state = bot_t(
            locale, "pause_state_on" if p.is_active is True else "pause_state_off"
        )
        # The template name is client input: into the HTML message only escaped.
        lines.append(f"{mark} {html.escape(_profile_name(p, locale))} — {state}")
    return "\n".join(lines)


def pause_keyboard(
    profiles, locale: str | None, cabinet_url: str
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if p.is_active is True else '⏸'} "
                f"{_profile_name(p, locale)[:60]}",
                callback_data=toggle_callback(p.id),
            )
        ]
        for p in profiles
    ]
    bulk = []
    if any(p.is_active is True for p in profiles):
        bulk.append(
            InlineKeyboardButton(
                text=bot_t(locale, "pause_all_btn"), callback_data=f"{CB_ALL}0"
            )
        )
    if any(p.is_active is not True for p in profiles):
        bulk.append(
            InlineKeyboardButton(
                text=bot_t(locale, "resume_all_btn"), callback_data=f"{CB_ALL}1"
            )
        )
    if bulk:
        rows.append(bulk)
    rows.append(
        [InlineKeyboardButton(text=bot_t(locale, "reset_cta"), url=cabinet_url)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bulk_toast(result: BulkResult, active: bool, locale: str | None) -> str:
    if active and result.blocked:
        return bot_t(locale, "pause_toast_all_limit").format(
            n=result.changed, blocked=result.blocked
        )
    key = "pause_toast_all_on" if active else "pause_toast_all_off"
    return bot_t(locale, key).format(n=result.changed)


# --------------------------------------------------------------------------- #
# “📋 My tenders”.
# --------------------------------------------------------------------------- #

# Two feeds in one query: (1) the tenders “in progress” — the funnel status
# watching/active of the TENANT; (2) what was sent to Telegram over the window — by the profiles
# of the tenant (the delivery_outbox policy — the same link through profiles). The hidden
# (“skipped”) ones are not shown from the sent. First “in progress”, then the sent;
# inside — the fresh ones on top, tender_id — the determinizing tail.
# profile_id is needed for the “Open in the cabinet” link (the tender page without it — 404):
# the delivery profile, if it is not deleted, otherwise the latest live template of the tenant.
_MY_TENDERS_SQL = text("""
    WITH sent AS (
        SELECT DISTINCT ON (o.tender_id)
               o.tender_id,
               CASE WHEN p.deleted_at IS NULL THEN p.id END AS profile_id,
               o.sent_at
          FROM delivery_outbox o
          JOIN profiles p ON p.id = o.profile_id
         WHERE p.tenant_id = CAST(:tenant_id AS uuid)
           AND o.channel = 'telegram'
           AND o.status = 'sent'
           AND o.sent_at >= now() - make_interval(days => :days)
         ORDER BY o.tender_id, o.sent_at DESC, o.id DESC
    ),
    st AS (
        SELECT tender_id, status, updated_at
          FROM tender_user_status
         WHERE tenant_id = CAST(:tenant_id AS uuid)
    ),
    items AS (
        SELECT st.tender_id, st.status, NULL::uuid AS profile_id,
               0 AS grp, st.updated_at AS ts
          FROM st
         WHERE st.status IN ('watching', 'active')
        UNION ALL
        SELECT sent.tender_id, st.status, sent.profile_id,
               1 AS grp, sent.sent_at AS ts
          FROM sent
          LEFT JOIN st ON st.tender_id = sent.tender_id
         WHERE st.status IS NULL
            OR st.status NOT IN ('watching', 'active', 'skipped')
    )
    SELECT i.tender_id,
           i.status,
           COALESCE(
               i.profile_id,
               (SELECT s.profile_id FROM sent s
                 WHERE s.tender_id = i.tender_id AND s.profile_id IS NOT NULL),
               (SELECT p2.id FROM profiles p2
                 WHERE p2.tenant_id = CAST(:tenant_id AS uuid)
                   AND p2.deleted_at IS NULL
                 ORDER BY p2.created_at DESC, p2.id
                 LIMIT 1)
           ) AS profile_id,
           t.title,
           t.source_url,
           t.deadline_at,
           t.source_key,
           t.amount_original,
           t.currency_original,
           t.amount_eur,
           t.is_amount_estimated
      FROM items i
      JOIN tenders_normalized t ON t.id = i.tender_id
     ORDER BY i.grp, i.ts DESC, i.tender_id
     LIMIT :limit OFFSET :offset
    """)


@dataclass(frozen=True)
class MyTender:
    tender_id: str
    status: str | None
    profile_id: str | None
    title: str | None
    source_url: str | None
    deadline_at: datetime | None
    amount_original: Decimal | float | None = None
    currency_original: str | None = None
    amount_eur: Decimal | float | None = None
    is_amount_estimated: bool | None = None
    source_key: str | None = None


async def load_my_tenders(
    session, tenant_id, *, offset: int = 0, limit: int = PAGE_SIZE
) -> tuple[list[MyTender], bool]:
    """The page of “My tenders” and the “there is more” flag (we take one row more)."""
    await repo.set_tenant(session, str(tenant_id))
    rows = (
        await session.execute(
            _MY_TENDERS_SQL,
            {
                "tenant_id": str(tenant_id),
                "days": SENT_WINDOW_DAYS,
                "limit": limit + 1,
                "offset": max(offset, 0),
            },
        )
    ).all()
    items = [
        MyTender(
            tender_id=str(r.tender_id),
            status=r.status,
            profile_id=str(r.profile_id) if r.profile_id else None,
            title=r.title,
            source_url=r.source_url,
            deadline_at=r.deadline_at,
            amount_original=r.amount_original,
            currency_original=r.currency_original,
            amount_eur=r.amount_eur,
            is_amount_estimated=r.is_amount_estimated,
            source_key=r.source_key,
        )
        for r in rows[:limit]
    ]
    return items, len(rows) > limit


def _list_callback(offset: int, history: tuple[int, ...] = ()) -> str:
    """``ls:<offset>[:<h1>.<h2>…]`` — the page and the stack of the starts of the previous pages.

    The pages are cut by the Telegram limit with WHOLE records, therefore the page length
    floats and “offset − PAGE_SIZE” for “◂ Back” would repeat the records. We carry the starts of
    the passed pages in the callback itself (≤64 bytes): what does not fit — we drop
    the oldest, and with an empty stack “Back” falls back to ``offset − PAGE_SIZE``.
    """
    hist = list(history)
    while True:
        data = f"{CB_LIST}{offset}" + (":" + ".".join(map(str, hist)) if hist else "")
        if len(data.encode()) <= 64 or not hist:
            return data
        hist.pop(0)


def parse_list_callback(data: str | None) -> tuple[int, tuple[int, ...]] | None:
    """``(offset, stack of the starts of the previous pages)`` or ``None`` for a foreign/broken one."""
    if not data or not data.startswith(CB_LIST):
        return None
    head, _, tail = data[len(CB_LIST) :].partition(":")
    if not head.isdigit():
        return None
    parts = tail.split(".") if tail else []
    if not all(x.isdigit() for x in parts):
        return None
    return int(head), tuple(int(x) for x in parts)


def _item_lines(n: int, item: MyTender, locale: str | None) -> list[str]:
    title = item.title or common_t(locale, "no_title")
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1].rstrip() + "…"
    title_html = html.escape(title)
    if item.source_url:
        href = html.escape(item.source_url, quote=True)
        title_html = f'<a href="{href}">{title_html}</a>'
    meta = []
    if item.status in ("watching", "active"):
        meta.append(common_t(locale, f"status_{item.status}"))
    deadline = format_deadline(
        item.deadline_at,
        source_key=item.source_key,
        note=common_t(locale, "deadline_hour_unknown"),
    )
    if deadline:
        meta.append(f"🗓 {html.escape(deadline)}")
    amount = format_amount_pair(
        item.amount_original,
        item.currency_original,
        item.amount_eur,
        item.is_amount_estimated,
        locale=locale,
    )
    if amount:
        meta.append(f"💰 {html.escape(amount)}")
    lines = [f"{n}. {title_html}"]
    if meta:
        lines.append("   " + " · ".join(meta))
    return lines


def render_my_tenders(
    items: list[MyTender],
    *,
    offset: int,
    has_more: bool,
    locale: str | None,
    cabinet_base: str,
    history: tuple[int, ...] = (),
    max_len: int = _TELEGRAM_MAX_MESSAGE,
) -> tuple[str, InlineKeyboardMarkup]:
    """The text and the keyboard of the “My tenders” page.

    Empty → an explanation + a button to the cabinet. Otherwise a numbered list; the buttons
    “📂 N” open the tender in the cabinet (like “Open in the cabinet” on the card), “More ▸” —
    the next page. The Telegram limit (4096) — we cut by WHOLE records.
    """
    from src.bots.telegram import cabinet_tender_url  # lazy: telegram → manage

    base = cabinet_base.rstrip("/")
    my_url = f"{base}/app/my"
    if not items and offset == 0:
        empty = bot_t(locale, "list_empty").format(
            days=SENT_WINDOW_DAYS,
            follow=common_t(locale, "btn_follow"),
            participate=bot_t(locale, "act_participate"),
        )
        return empty, InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=bot_t(locale, "list_cabinet_btn"), url=my_url
                    )
                ]
            ]
        )

    head = [
        bot_t(locale, "list_title"),
        bot_t(locale, "list_subtitle").format(days=SENT_WINDOW_DAYS),
    ]
    lines = list(head)
    open_buttons: list[InlineKeyboardButton] = []
    shown = 0
    for i, item in enumerate(items):
        n = offset + i + 1
        candidate = lines + [""] + _item_lines(n, item, locale)
        if shown and len("\n".join(candidate)) > max_len:
            break
        lines = candidate
        shown += 1
        if item.profile_id:
            open_buttons.append(
                InlineKeyboardButton(
                    text=f"📂 {n}",
                    url=cabinet_tender_url(base, item.tender_id, item.profile_id),
                )
            )
    if not items:
        lines += ["", bot_t(locale, "list_no_more")]

    rows = [open_buttons[i : i + 5] for i in range(0, len(open_buttons), 5)]
    nav = []
    if offset > 0:
        prev, rest = (
            (history[-1], history[:-1]) if history else (max(offset - PAGE_SIZE, 0), ())
        )
        nav.append(
            InlineKeyboardButton(
                text=bot_t(locale, "list_prev_btn"),
                callback_data=_list_callback(prev, rest),
            )
        )
    if has_more or shown < len(items):
        nav.append(
            InlineKeyboardButton(
                text=bot_t(locale, "list_more_btn"),
                callback_data=_list_callback(offset + shown, (*history, offset)),
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text=bot_t(locale, "list_cabinet_btn"), url=my_url)]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


# --------------------------------------------------------------------------- #
# Buttons under /profile and /status.
# --------------------------------------------------------------------------- #


def manage_keyboard(locale: str | None, cabinet_url: str) -> InlineKeyboardMarkup:
    """“Templates in the cabinet” (URL with a scheme) · “⏸ Pause” · “📋 My tenders” · “Sources”."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=bot_t(locale, "reset_cta"), url=cabinet_url)],
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "pause_btn"), callback_data=CB_MENU_PAUSE
                ),
                InlineKeyboardButton(
                    text=bot_t(locale, "list_btn"), callback_data=CB_MENU_LIST
                ),
            ],
            [
                InlineKeyboardButton(
                    text=bot_t(locale, "sources_btn"), callback_data=CB_MENU_SOURCES
                )
            ],
        ]
    )
