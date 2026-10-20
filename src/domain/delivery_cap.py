"""The daily delivery limit: ONE place for the figure and the warning threshold.

The limit cuts ONLY sending to Telegram and by email (owner 16.10.2026): the site
shows all matches of a template. It is counted in TENDERS per template per UTC day,
not in outbox rows (a row is written per channel — otherwise with TG+email one
tender would spend two units).

The figure is read by the delivery worker (``DELIVERY_DAILY_CAP``), the bot, the cabinet and emails —
the texts take it from here via ``{cap}``, not as a literal.
"""

from __future__ import annotations

import math
import os

#: The default matches the production value of ``DELIVERY_DAILY_CAP`` (30 as of 16.10.2026).
DEFAULT_DAILY_CAP = 30

#: From what share of the limit the line “Sent today N of M” is added to messages.
#: 80% of 30 → from the 24th tender; before the threshold the line makes no noise.
WARN_FRACTION = 0.8


def daily_cap() -> int:
    """The limit of tenders per day per template (env ``DELIVERY_DAILY_CAP``; 0 — off)."""
    raw = os.getenv("DELIVERY_DAILY_CAP")
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_CAP
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_DAILY_CAP


def warn_threshold(cap: int) -> int:
    """The tender number within a day from which the counter is shown (24 at cap=30)."""
    return max(1, math.ceil(cap * WARN_FRACTION))


def should_show_counter(sent_today: int, cap: int) -> bool:
    """Whether to show the counter line in the message about the ``sent_today``-th tender."""
    return cap > 0 and sent_today >= warn_threshold(cap)


def counter_text(t, locale: str | None, sent_today: int, cap: int, name: str) -> str:
    """The line “Sent today N of M for the template „name“” or ``""`` below the threshold.

    ``t`` is ``common_t`` (Telegram) or ``email_t`` (email): the catalogs hold
    the same text of the key ``cap_counter`` (guard ``CARD_LABEL_KEYS``).
    """
    if not should_show_counter(sent_today, cap):
        return ""
    return t(locale, "cap_counter").format(sent=sent_today, cap=cap, name=name)
