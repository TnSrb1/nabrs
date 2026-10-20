"""The list of enabled sources for the showcase and help (SRC-0d, owner’s decision R7).

The public texts “we scan Portal javnih nabavki” are a perishable doc: as soon as a second source is enabled, the promise “one portal” becomes a lie. Therefore the i18n catalogs contain the placeholder ``{sources}``, and the list of names is taken from the ``sources`` table (:func:`src.domain.sources.list_enabled_sources`) and substituted at render (:func:`fill_sources`). A per-process cache with a short TTL — the showcase is public, the table changes rarely (the admin’s button).

The check frequency (“na svakih 30 min – 3 h”) — also from the table, not a literal: the placeholders ``{interval}`` and ``{interval_note}`` are built from ``sources.polling_interval_sec`` of the ENABLED sources (:func:`interval_phrase`). Enable a source with a different interval — the text changes together with the table.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from src.domain.sources import list_enabled_sources

logger = logging.getLogger(__name__)

_TTL_SECONDS = 60.0
_cache: tuple[float, "SourcesView"] | None = None

PLACEHOLDER = "{sources}"
INTERVAL_PLACEHOLDER = "interval"  # → “{interval}” in the catalogs
NOTE_PLACEHOLDER = "interval_note"  # → “{interval_note}” in the catalogs

_POLLING_SQL = text("SELECT polling_interval_sec FROM sources WHERE enabled = true")

#: The leading phrase of the frequency: “na svakih {…}” / “every {…}” / “every {…}”.
_INTERVAL_LEAD = {"sr": "na svakih {}", "ru": "каждые {}", "en": "every {}"}
#: Exactly one hour: “na svakih 1 h” / “every 1 hour” / “every 1 h” — not a phrase.
_INTERVAL_ONE_HOUR = {
    "sr": "na svakih sat vremena",
    "ru": "каждый час",
    "en": "every hour",
}
#: The fallback without data (the DB is unavailable, no enabled platforms): without an invented number.
_INTERVAL_FALLBACK = {"sr": "redovno", "ru": "регулярно", "en": "regularly"}
#: A permanent caveat about degradation (on failures of a source the interval grows up to 6 h).
_INTERVAL_NOTE = {
    "sr": "Ako je izvor privremeno nedostupan, proveravamo ređe.",
    "ru": "Если источник временно недоступен, проверяем реже.",
    "en": "If a source is temporarily unavailable, we check it less often.",
}


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    """1 hour / 2 hours / 5 hours (with the exception for 11–14)."""
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return forms[1]
    return forms[2]


def _one_interval(sec: int, lang: str) -> str:
    """One interval in human units: whole hours → h, otherwise min."""
    if sec % 3600 == 0:
        n = sec // 3600
        if lang == "ru":
            return f"{n} {_ru_plural(n, ('час', 'часа', 'часов'))}"
        return f"{n} h"
    n = max(1, round(sec / 60))
    if lang == "ru":
        return f"{n} {_ru_plural(n, ('минута', 'минуты', 'минут'))}"
    return f"{n} min"


def interval_phrase(intervals: Iterable[int | None], lang: str) -> str:
    """The frequency phrase by the intervals of the enabled sources (seconds).

    min == max → “na svakih 30 min” / “na svakih 3 h”; otherwise a range “na svakih 30 min – 3 h”. No interval at all → a neutral fallback.
    """
    secs = [s for s in intervals if s and s > 0]
    if not secs:
        return _INTERVAL_FALLBACK[lang]
    lo, hi = min(secs), max(secs)
    if lo == hi == 3600:
        return _INTERVAL_ONE_HOUR[lang]
    body = _one_interval(lo, lang)
    if hi != lo:
        body = f"{body} – {_one_interval(hi, lang)}"
    return _INTERVAL_LEAD[lang].format(body)


#: The countries of the platforms by the showcase languages; an unknown code is shown as is.
COUNTRY_NAMES: dict[str, dict[str, str]] = {
    "sr": {"RS": "Srbija", "BA": "Bosna i Hercegovina", "MK": "Severna Makedonija",
           "ME": "Crna Gora", "HR": "Hrvatska", "SI": "Slovenija", "XK": "Kosovo",
           "AL": "Albanija"},
    "ru": {"RS": "Сербия", "BA": "Босния и Герцеговина", "MK": "Северная Македония",
           "ME": "Черногория", "HR": "Хорватия", "SI": "Словения", "XK": "Косово",
           "AL": "Албания"},
    "en": {"RS": "Serbia", "BA": "Bosnia and Herzegovina", "MK": "North Macedonia",
           "ME": "Montenegro", "HR": "Croatia", "SI": "Slovenia", "XK": "Kosovo",
           "AL": "Albania"},
}  # fmt: skip


@dataclass(frozen=True)
class SourceItem:
    name: str
    country_codes: tuple[str, ...]

    def countries(self, lang: str) -> str:
        names = COUNTRY_NAMES.get(lang) or COUNTRY_NAMES["sr"]
        return ", ".join(names.get(c, c) for c in self.country_codes)


@dataclass(frozen=True)
class SourcesView:
    items: tuple[SourceItem, ...] = ()
    #: ``sources.polling_interval_sec`` of the enabled platforms (seconds; None — not set).
    intervals: tuple[int | None, ...] = ()

    @property
    def names(self) -> str:
        return ", ".join(i.name for i in self.items)

    def placeholders(self, lang: str) -> dict[str, str]:
        """The values of the frequency placeholders for the catalog language (for :func:`fill_sources`)."""
        return {
            INTERVAL_PLACEHOLDER: interval_phrase(self.intervals, lang),
            NOTE_PLACEHOLDER: _INTERVAL_NOTE[lang],
        }


async def placeholders_for(session_factory: Any, lang: str) -> dict[str, str]:
    """The frequency placeholders for a page without its own list of sources.

    A DB error → a neutral fallback (:func:`interval_phrase` without data); the page lives on.
    """
    try:
        async with session_factory() as db:
            return (await get_sources_view(db)).placeholders(lang)
    except Exception as exc:  # noqa: BLE001 — the page lives without the frequency from the DB
        logger.warning("izvori: the frequency is unavailable (%s)", type(exc).__name__)
        return SourcesView().placeholders(lang)


def clear_cache() -> None:
    """Resets the cache (tests)."""
    global _cache
    _cache = None


async def get_sources_view(session: Any) -> SourcesView:
    """The enabled sources; a DB error → an empty view (the page lives without the list)."""
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    rows = await list_enabled_sources(session)
    polling_rows = (await session.execute(_POLLING_SQL)).all()
    view = SourcesView(
        tuple(SourceItem(s.label, s.country_codes) for s in rows),
        tuple(r.polling_interval_sec for r in polling_rows),
    )
    _cache = (now + _TTL_SECONDS, view)
    return view


def fill_sources(
    obj: Any, names: str, placeholders: dict[str, str] | None = None
) -> Any:
    """Substitute the source names into ``{sources}`` across the whole catalog (a copy).

    ``placeholders`` — the values of the frequency placeholders (:meth:`SourcesView.placeholders`): ``{interval}``, ``{interval_note}``. Without it they remain as is — the caller must pass it (the guard — ``tests/api/test_sources_public_render.py``).

    An empty list → “( {sources})” together with the space is cut out: the phrase stays grammatical without the list. ``str.replace``, not ``format``: the catalogs contain other curly braces (``{n}``, ``{date}``).
    """
    if isinstance(obj, str):
        if placeholders:
            for key, value in placeholders.items():
                obj = obj.replace("{" + key + "}", value)
        if PLACEHOLDER not in obj:
            return obj
        if names:
            return obj.replace(PLACEHOLDER, names)
        return obj.replace(f" ({PLACEHOLDER})", "").replace(PLACEHOLDER, "")
    if isinstance(obj, dict):
        return {k: fill_sources(v, names, placeholders) for k, v in obj.items()}
    if isinstance(obj, list):
        return [fill_sources(v, names, placeholders) for v in obj]
    if isinstance(obj, tuple):
        return tuple(fill_sources(v, names, placeholders) for v in obj)
    return obj
