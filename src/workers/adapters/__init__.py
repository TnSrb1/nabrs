"""Channel delivery adapters (ARCHITECTURE §15.1, ADR-025).

Telegram and Email are implemented; Viber — a stub (``NotImplementedError``), Web
is postponed. :func:`default_adapters` builds the registry ``channel -> adapter`` for
:class:`src.workers.delivery.DeliveryWorker`.

The mail transport for the Email adapter is chosen by the same
:func:`src.api.emailer.make_email_sender` (SMTP → Postmark by env) as for the other mail workers — there is no second mail contour. The delivery differs from the other mail workers in one thing: the dev fallback to ``MockEmailSender`` is not given to it
(:func:`email_sender_from_env` returns ``None``) — delivery must fail loudly, not report success.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from src.api.emailer import EmailSender, make_email_sender
from src.workers.adapters.base import (
    DeliveryAdapter,
    DeliveryJob,
    DeliveryResult,
    FormattedMessage,
)
from src.workers.adapters.email import EmailAdapter
from src.workers.adapters.telegram import TelegramAdapter
from src.workers.adapters.viber import ViberAdapter

if TYPE_CHECKING:  # pragma: no cover
    from aiogram import Bot

__all__ = [
    "DeliveryAdapter",
    "DeliveryJob",
    "DeliveryResult",
    "FormattedMessage",
    "TelegramAdapter",
    "ViberAdapter",
    "EmailAdapter",
    "default_adapters",
]


def email_sender_from_env() -> EmailSender | None:
    """The mail transport by env (SMTP → Postmark), as for the mail workers.

    ``None`` if neither ``SMTP_HOST`` nor ``POSTMARK_SERVER_TOKEN`` is set: for
    :func:`src.api.emailer.make_email_sender` in this case there is a fallback to
    ``MockEmailSender`` (dev mode: e-mails pile up in memory), and returning it to
    the delivery means reporting success for an unsent e-mail. The delivery
    gets ``None`` and honestly fails into retryable (see :mod:`.email`).
    """
    if not os.getenv("SMTP_HOST") and not os.getenv("POSTMARK_SERVER_TOKEN"):
        return None
    return make_email_sender(
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        postmark_token=os.getenv("POSTMARK_SERVER_TOKEN", ""),
        email_from=os.getenv("EMAIL_FROM", "noreply@nab.rs"),
    )


def default_adapters(
    bot: "Bot",
    *,
    cabinet_base_url: str | None = None,
    email_sender: EmailSender | None = None,
) -> dict[str, DeliveryAdapter]:
    """The registry ``channel -> adapter``. Telegram and Email are working, Viber — a stub.

    ``email_sender`` can be passed explicitly (tests/reuse of the transport);
    by default :func:`email_sender_from_env` is taken — and its ``None`` (mail not configured)
    reaches the adapter as is: the Email channel will be fail-closed.
    """
    return {
        "telegram": TelegramAdapter(bot, cabinet_base_url=cabinet_base_url),
        "viber": ViberAdapter(),
        "email": EmailAdapter(
            email_sender or email_sender_from_env(),
            cabinet_base_url=cabinet_base_url,
        ),
    }
