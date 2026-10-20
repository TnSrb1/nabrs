"""F2 — assembly of the CSV export “My tenders”.

Pure functions without DB or network: the rows are taken by
:func:`src.api.cabinet.workflow_service.get_my_tenders_export`, and the CSV itself is written by the reused bot contour (:func:`src.bots.analytics_export.csv_bytes`) —
UTF-8 **with BOM**, separator ``;``, CRLF. There must be no second CSV-assembly contour in the project: BOM+``;`` is what makes Cyrillic and the columns open in
Excel/LibreOffice under the RS/EU locale without fiddling with the import.

Amounts go as a raw integer (``dec_int``), as in the award export: with the ``;`` separator
Excel recognizes such a number as a number, not as text.
"""

from __future__ import annotations

from src.api.cabinet.i18n import CABINET
from src.api.cabinet.workflow_service import MyTenderExportRow
from src.bots.analytics_export import csv_bytes, dec_int
from src.domain.tz import to_local
from src.i18n_common import common_t
from src.parsers.capabilities import deadline_has_time

CSV_FILENAME = "my_tenders.csv"

# Header keys (CABINET catalog). The file goes to the CLIENT, so the header is in
# the language of their cabinet, not always in Russian.
_HEADER_KEYS = [
    "csv_h_title",
    "csv_h_customer",
    "csv_h_amount",
    "csv_h_currency",
    "csv_h_deadline",
    "csv_h_status",
    "csv_h_folder",
    "csv_h_link",
]

# Statuses of the tender_user_status pipeline (0008) → label for the export. A tender without
# a row in the pipeline (it got into the export via a folder) and the status new — an empty cell:
# “no mark” is more honest than an invented status.
# Stage labels are taken from the SSOT of the shared vocabulary (``i18n_common.COMMON``) — the same one
# that draws the pipeline in the cabinet and in the bot: one meaning lives in one place. In COMMON
# the strings come with emoji (“👀 Pratim”) — for CSV we strip the emoji so that the cell
# reads normally in Excel/Calc.
_STATUS_KEYS = {
    "watching": "status_watching",
    "active": "status_active",
    "submitted": "status_submitted",
    "won": "status_won",
    "lost": "status_lost",
    "skipped": "status_skipped",
}


def _strip_emoji(value: str) -> str:
    """Strips the leading emoji prefix of a stage label (“👀 Pratim” → “Pratim”)."""
    parts = value.split(" ", 1)
    return parts[1].strip() if len(parts) == 2 and not parts[0].isalnum() else value


_DEADLINE_FMT = "%d.%m.%Y %H:%M"


def status_label(status: str | None, lang: str | None = None) -> str:
    """Pipeline stage label for the export; no stage — an empty cell."""
    key = _STATUS_KEYS.get(status or "")
    return _strip_emoji(common_t(lang, key)) if key else ""


def _deadline_cell(r: MyTenderExportRow, lang: str | None) -> str:
    """Deadline for the cell: Belgrade time (the presentation boundary; the DB is UTC).

    A source without an hour (source CFKU): the hour in the DB is our substituted end of day; the client sees the date and the note “the closing hour is not given by the source” — the same form
    as in ``tender_detail.html``. The note goes in the cell, not in a separate column: the column order of the file (the export contract) does not change.
    """
    if r.deadline_at is None:
        return ""
    local = to_local(r.deadline_at)
    if deadline_has_time(r.source_key):
        return local.strftime(_DEADLINE_FMT)
    return f"{local.strftime('%d.%m.%Y')} ({common_t(lang, 'deadline_hour_unknown')})"


def _row(r: MyTenderExportRow, lang: str | None = None) -> list[str]:
    return [
        r.title,
        r.customer_name or "",
        "" if r.amount is None else dec_int(r.amount),
        r.currency or "",
        _deadline_cell(r, lang),
        status_label(r.user_status, lang),
        r.folder_names or "",
        r.tender_url or "",
    ]


def build_my_tenders_csv(
    rows: list[MyTenderExportRow], lang: str | None = None
) -> bytes:
    """CSV “My tenders” in the cabinet language. An empty list → headers only.

    ``lang`` — the interface language of the export owner; None/unknown → sr
    (catalog contract, owner decision 30.09.2026). The client reads the file, so the header and stages are translated,
    while the tender data (title, customer) stays “as is” — as in the feed.
    """
    lang = lang or "sr"
    catalog = CABINET.get(lang, CABINET["sr"])
    headers = [catalog[key] for key in _HEADER_KEYS]
    return csv_bytes([headers] + [_row(r, lang) for r in rows], lang)
