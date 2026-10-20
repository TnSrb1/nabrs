"""The handlers of the trial access for the Telegram bot (STAGE T).

A separate ``trial_router`` (connected in
:func:`src.bots.telegram.build_dispatcher`) — so that the main router of the commands does not swell and the
logic of the decisions lives apart.

What it does:
  * The callback ``tr:<request_id>:<3|15|30|deny>`` (:func:`on_trial_decision`) —
    ONLY from ``ADMIN_CHAT_ID`` (a stranger → a toast “no rights”, no changes).
    approve N → the extension of ``tenants.access_until`` by N days from the larger of the dates
    (the current term / ``now()``) + the request ``approved``;
    deny → the request ``rejected``. Idempotent (a repeat → “already processed”). After the
    decision it edits the admin message and removes the buttons; it notifies the
    user (if the Telegram is bound — a message to the bot).
  * The command ``/trials`` (:func:`cmd_trials`, only ``ADMIN_CHAT_ID``) — the list of
    the pending requests with the same buttons (insurance against lost notifications).

The DB write — :mod:`src.bots.trial_service` (a privileged cross-tenant
operation under the role ``tendersrb_app``, only through SECURITY DEFINER). The handlers are
run on a mock session/mock callback (ADR-013), without the network.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.bots.config import get_bot_settings
from src.bots.i18n import bot_t
from src.bots.trial_service import decide, fmt_date, list_pending
from src.db.session import session_scope

logger = logging.getLogger(__name__)

trial_router = Router(name="telegram-trial")

# The approval terms (the buttons). The callback token → the number of days.
_DAYS = {"3": 3, "15": 15, "30": 30}


def trial_admin_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """The inline keyboard of the decision: 3/15/30 days + decline.

    callback_data: ``tr:<request_id>:<3|15|30|deny>`` (the same format as the cabinet sends in
    :func:`src.api.cabinet.trial_service.notify_admin`).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ 3 дня", callback_data=f"tr:{request_id}:3"
                ),
                InlineKeyboardButton(
                    text="✅ 15 дней", callback_data=f"tr:{request_id}:15"
                ),
                InlineKeyboardButton(
                    text="✅ 30 дней", callback_data=f"tr:{request_id}:30"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"tr:{request_id}:deny"
                )
            ],
        ]
    )


def _is_admin(user_id: int | None) -> bool:
    """The user — the configured ADMIN_CHAT_ID (0 = not configured → nobody)."""
    admin_chat_id = get_bot_settings().admin_chat_id
    return bool(admin_chat_id) and user_id == admin_chat_id


async def _notify_user(callback: CallbackQuery, outcome) -> None:
    """The best-effort notification of the initiator about the decision (if the Telegram is bound).

    Not bound → the status is visible in the cabinet (we send nothing). Never raises.
    An upgrade request (``outcome.plan_key``) names the activated subscription and
    the term; an ordinary trial access — the former text.
    """
    if not outcome.chat_id:
        return
    # The language — of the recipient’s chat (channel → cabinet), not of the operator who pressed the button.
    # The import is lazy: src.bots.telegram imports this module itself.
    from src.bots.telegram import resolve_bot_locale

    try:
        async with session_scope() as session:
            locale = await resolve_bot_locale(session, int(outcome.chat_id))
    except Exception:  # noqa: BLE001 — the chrome must not break the notification
        locale = None
    if outcome.status == "approved":
        until = fmt_date(outcome.access_until)
        if outcome.plan_key:
            text = bot_t(locale, "trial_notify_plan").format(
                plan=outcome.plan_key.capitalize(), date=until
            )
        else:
            text = bot_t(locale, "trial_notify_trial").format(date=until)
    else:
        text = bot_t(locale, "trial_notify_denied")
    try:
        await callback.bot.send_message(int(outcome.chat_id), text)
    except Exception:  # noqa: BLE001 — best-effort, the status is in the cabinet anyway
        logger.warning("trial: could not notify the user", exc_info=True)


@trial_router.callback_query(F.data.startswith("tr:"))
async def on_trial_decision(callback: CallbackQuery) -> None:
    """The decision on the request: ``tr:<request_id>:<3|15|30|deny>`` (ADMIN only)."""
    if not _is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Нет прав", show_alert=True)
        return

    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        await callback.answer()
        return
    _, request_id, action = parts

    approve = action in _DAYS
    if not approve and action != "deny":
        await callback.answer()
        return
    days = _DAYS.get(action)

    admin_id = callback.from_user.id if callback.from_user else None
    async with session_scope() as session:
        outcome = await decide(
            session,
            request_id=request_id,
            approve=approve,
            days=days,
            actor=f"telegram:{admin_id}",
        )

    if outcome.status == "already":
        await callback.answer("Уже обработан", show_alert=True)
        return

    if outcome.status == "approved":
        if outcome.plan_key:
            admin_text = (
                f"✅ Подписка {outcome.plan_key.capitalize()} на {days} дн. "
                f"(до {fmt_date(outcome.access_until)})"
            )
        else:
            admin_text = (
                f"✅ Одобрено на {days} дн. (до {fmt_date(outcome.access_until)})"
            )
        await callback.answer("Одобрено")
    else:
        admin_text = "❌ Отклонено"
        await callback.answer("Отклонено")

    if callback.message is not None:
        try:
            await callback.message.edit_text(admin_text, reply_markup=None)
        except Exception:  # noqa: BLE001 — an old message: at least remove the buttons
            import contextlib

            with contextlib.suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)

    await _notify_user(callback, outcome)


@trial_router.message(Command("trials"))
async def cmd_trials(message: Message) -> None:
    """``/trials`` — the list of the pending requests with buttons (ADMIN only)."""
    if not _is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Нет прав.")
        return
    async with session_scope() as session:
        pending = await list_pending(session)
    if not pending:
        await message.answer("Нет ожидающих заявок на тестовый доступ.")
        return
    for req in pending:
        when = fmt_date(req.requested_at)
        if req.plan == "reregister":
            # A repeated registration is decided ONLY in the web admin
            # (app_admin_decide_reregister): the bot button answered “already decided”,
            # while the request hung. Without a keyboard.
            await message.answer(
                "🆕 Запрос повторной регистрации — решение в /admin/requests\n"
                f"Email: {req.email or '—'}\n"
                f"Дата: {when}"
            )
            continue
        # The type of the request — as in the web admin: a subscription (upgrade_request:<plan>) or
        # an ordinary trial access. The operator sees the free comment of the subscriber
        # in the push notification of the cabinet; SECURITY DEFINER 0034 does not return it.
        kind = f"Подписка: {req.plan.capitalize()}" if req.plan else "Тестовый доступ"
        body = (
            "🆕 Запрос доступа\n"
            f"Email: {req.email or '—'}\n"
            f"{kind}\n"
            f"Дата: {when}"
        )
        await message.answer(body, reply_markup=trial_admin_keyboard(req.request_id))
