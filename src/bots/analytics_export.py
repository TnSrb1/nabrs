"""VG-06 STAGE 3a — export of the analytics of the contracting authority/competitor to CSV.

Pure functions without network/DB/AI; they use only the stdlib ``csv``/``io``.
CSV UTF-8 with a BOM, separator ``;`` (RS/EU locale of Excel), CRLF.

The export deliberately does NOT format the amounts (raw numbers) — for further
processing in Excel/LibreOffice without loss of data.
"""

from __future__ import annotations

import csv
import io
import re
from decimal import Decimal

from src.analytics.awards_analytics import CompetitorStats, CustomerStats
from src.bots.i18n import bot_t, norm_locale
from src.domain.text_norm import sr_display

EXPORT_TOP_N = 50  # the row limit of the top when exporting (against 5 in the report)

# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _safe_ascii(text: str | None) -> str:
    """Transliteration/replacement of unsafe characters for the file name."""
    if not text:
        return "noname"
    t = text.strip()
    # We remove the characters that are unsafe in file names
    t = re.sub(r"[^\w\s\-]", "", t, flags=re.ASCII)
    t = re.sub(r"[\s]+", "_", t)
    t = t.strip("_")
    return t[:40] if t else "noname"


def _period_suffix(months: int | None) -> str:
    if months is None:
        return "all"
    return f"{months}m"


def dec_int(value: Decimal) -> str:
    """A raw integer for CSV (without spaces)."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "0"


def csv_bytes(rows: list[list[str]], lang: str | None = None) -> bytes:
    """Assembles a list of rows into a UTF-8 BOM CSV (separator ';', CRLF).

    ``lang == "sr"`` — every cell in Latin (owner 10.10.2026); ``None`` — as is
    (the caller passes the already normalized file language).
    """
    if lang == "sr":
        rows = [[sr_display(c, lang) for c in r] for r in rows]
    buf = io.StringIO()
    writer = csv.writer(
        buf, delimiter=";", lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL
    )
    writer.writerows(rows)
    return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- #
#  Customer CSV
# --------------------------------------------------------------------------- #


def build_customer_csv(
    stats: CustomerStats, *, customer_name: str = "", locale: str | None = None
) -> bytes:
    """CSV export of the analytics of the contracting authority.

    Structure: a summary block, an empty row, a table of the top winners.
    """

    def t(key: str) -> str:
        return bot_t(locale, key)

    period = (
        t("ax_all_time")
        if stats.months is None
        else t("ax_months").format(n=stats.months)
    )
    name = customer_name or stats.customer_pib or "—"

    rows: list[list[str]] = [
        [t("ax_customer"), name],
        ["PIB", stats.customer_pib or ""],
        [t("ax_period"), period],
        [t("ax_total_awards"), str(stats.total_awards)],
        [t("ax_distinct_winners"), str(stats.distinct_winners)],
        [t("ax_with_amount"), str(stats.with_amount)],
        [t("ax_sum_rsd"), dec_int(stats.total_rsd)],
        [t("ax_sum_eur"), dec_int(stats.total_eur)],
        [],
        [t("ax_winner"), "PIB", t("ax_wins_col"), t("ax_sum_rsd")],
    ]
    for w in stats.top_winners:
        rows.append([w.name or "", w.pib or "", str(w.wins), dec_int(w.sum_rsd)])

    return csv_bytes(rows, norm_locale(locale))


def customer_csv_filename(stats: CustomerStats, *, customer_name: str = "") -> str:
    """The file name for the CSV of the customer: ``awards_customer_<pib|name>_<period>.csv``."""
    ident = (
        _safe_ascii(stats.customer_pib)
        if stats.customer_pib
        else _safe_ascii(customer_name)
    )
    period = _period_suffix(stats.months)
    return f"awards_customer_{ident}_{period}.csv"


# --------------------------------------------------------------------------- #
#  Competitor CSV
# --------------------------------------------------------------------------- #


def build_competitor_csv(
    stats: CompetitorStats, *, winner_name: str = "", locale: str | None = None
) -> bytes:
    """CSV export of the competitor profile.

    Structure: a summary block, an empty row, a table of the top customers.
    """

    def t(key: str) -> str:
        return bot_t(locale, key)

    period = (
        t("ax_all_time")
        if stats.months is None
        else t("ax_months").format(n=stats.months)
    )
    name = winner_name or stats.winner_pib or "—"

    rows: list[list[str]] = [
        [t("ax_winner"), name],
        ["PIB", stats.winner_pib or ""],
        [t("ax_period"), period],
        [t("ax_total_wins"), str(stats.total_wins)],
        [t("ax_distinct_customers"), str(stats.distinct_customers)],
        [t("ax_with_amount"), str(stats.with_amount)],
        [t("ax_sum_rsd"), dec_int(stats.total_rsd)],
        [t("ax_sum_eur"), dec_int(stats.total_eur)],
        [],
        [t("ax_customer_col"), "PIB", t("ax_wins_col"), t("ax_sum_rsd")],
    ]
    for c in stats.top_customers:
        rows.append([c.name or "", c.pib or "", str(c.wins), dec_int(c.sum_rsd)])

    return csv_bytes(rows, norm_locale(locale))


def competitor_csv_filename(stats: CompetitorStats, *, winner_name: str = "") -> str:
    """The file name for the CSV of the competitor: ``awards_competitor_<pib|name>_<period>.csv``."""
    ident = (
        _safe_ascii(stats.winner_pib) if stats.winner_pib else _safe_ascii(winner_name)
    )
    period = _period_suffix(stats.months)
    return f"awards_competitor_{ident}_{period}.csv"
