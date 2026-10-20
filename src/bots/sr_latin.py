"""The Serbian in the bot — Latin only (owner 10.10.2026).

The tender data (title, contracting authority, winner) is stored in the DB in Serbian
Cyrillic, while the bot’s chrome for ``sr`` is already Latin. The transliteration — at the boundary
of sending, by ONE mechanism, and not in each handler:

* the bot’s handlers: the language is resolved by :func:`src.bots.telegram.resolve_bot_locale`,
  which puts it into :data:`bot_locale` (a ``ContextVar`` — one update = one
  task); :class:`SrLatinMiddleware` on the ``Bot`` session rewrites the outgoing
  method (``text``/``caption``/the labels of the inline buttons);
* the delivery worker: the adapter knows the language from ``job.locale`` and calls
  :func:`latinize_markup`/``sr_display`` itself.

ru/en are not affected.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.domain.text_norm import sr_display

#: The interface language of the current update (set by ``resolve_bot_locale``); None — not resolved.
bot_locale: ContextVar[str | None] = ContextVar("bot_locale", default=None)


def latinize_markup(markup: Any, locale: str | None) -> Any:
    """An inline keyboard with the button labels in Latin (sr); otherwise — as is."""
    if locale != "sr" or not isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows = [
        [
            (
                btn.model_copy(update={"text": sr_display(btn.text, locale)})
                if isinstance(btn, InlineKeyboardButton)
                else btn
            )
            for btn in row
        ]
        for row in markup.inline_keyboard
    ]
    return markup.model_copy(update={"inline_keyboard": rows})


def latinize_method(method: TelegramMethod[Any], locale: str | None) -> Any:
    """A copy of the outgoing method with text/caption/reply_markup in Latin (sr)."""
    if locale != "sr":
        return method
    update: dict[str, Any] = {}
    for field in ("text", "caption"):
        value = getattr(method, field, None)
        if isinstance(value, str):
            update[field] = sr_display(value, locale)
    markup = getattr(method, "reply_markup", None)
    if markup is not None:
        update["reply_markup"] = latinize_markup(markup, locale)
    return method.model_copy(update=update) if update else method


class SrLatinMiddleware(BaseRequestMiddleware):
    """The ``Bot`` session: the outgoing of an sr user’s update goes out in Latin."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Any,
        method: TelegramMethod[Any],
    ) -> Any:
        return await make_request(bot, latinize_method(method, bot_locale.get()))
