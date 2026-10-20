"""The trial period length in the showcase texts: one source — ``settings.trial_days``.

In the ``i18n`` / ``legal_texts`` catalogs the number is not written as a literal: there is a placeholder ``{trial}`` (the number + the word in the correct form: “30 dana” / “30 days”) or ``{trial_n}`` (a bare number — for “{trial_n}-day trial”). A change of the length is one edit of ``TRIAL_DAYS``. ``str.replace``, not ``format``: the catalogs contain other curly braces.
"""

from __future__ import annotations

from typing import Any

from src.api.cabinet.i18n import plural
from src.api.config import get_cabinet_settings
from src.domain.delivery_cap import daily_cap

TRIAL = "{trial}"
TRIAL_N = "{trial_n}"
CAP = "{cap}"


def trial_days() -> int:
    return get_cabinet_settings().trial_days


def trial_phrase(lang: str, days: int | None = None) -> str:
    """“30 dana” / “30 days” — the number with the word in the form of the language."""
    return plural(lang, "days_full", trial_days() if days is None else days)


def fill_trial(obj: Any, lang: str, days: int | None = None) -> Any:
    """Substitute the trial length into ``{trial}`` / ``{trial_n}`` across the whole structure (a copy).

    Also ``{cap}`` — the daily delivery limit to Telegram / email (:func:`src.domain.delivery_cap.daily_cap`, one source for all texts): so that it gets into every place of the showcase that already goes through this pass.
    """
    n = trial_days() if days is None else days
    cap = str(daily_cap())
    phrase = plural(lang, "days_full", n)

    def _walk(o: Any) -> Any:
        if isinstance(o, str):
            if "{trial" not in o and CAP not in o:
                return o
            return o.replace(TRIAL_N, str(n)).replace(TRIAL, phrase).replace(CAP, cap)
        if isinstance(o, dict):
            return {k: _walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [_walk(v) for v in o]
        if isinstance(o, tuple):
            return tuple(_walk(v) for v in o)
        return o

    return _walk(obj)
