"""Tests of building the CSV “My tenders” (F2) — pure functions, without DB.

What is protected here: the file opens in Excel as a table with readable Cyrillic (BOM + ``;``), an empty list gives a valid CSV rather than an exception, and the build goes through the COMMON contour with the award export (there is no second contour).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from src.api.cabinet import my_tenders_export as ex
from src.api.cabinet.workflow_service import MyTenderExportRow

_DL = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)


def _row(**kw) -> MyTenderExportRow:
    base = dict(
        tender_id="t1",
        title="Набавка канцеларијског материјала",
        customer_name="Град Београд",
        amount=Decimal("1250000.00"),
        currency="RSD",
        deadline_at=_DL,
        tender_url="https://x/1",
        user_status="watching",
        folder_names="Приоритет",
    )
    base.update(kw)
    return MyTenderExportRow(**base)


def _text(raw: bytes) -> str:
    """Decodes the CSV the way Excel does: the BOM is mandatory and is consumed."""
    assert raw.startswith(b"\xef\xbb\xbf"), "no UTF-8 BOM — Excel will show gibberish"
    return raw.decode("utf-8-sig")


def _lines(raw: bytes) -> list[str]:
    return _text(raw).rstrip("\r\n").split("\r\n")


# --------------------------------------------------------------------------- #
# File format
# --------------------------------------------------------------------------- #


def test_headers_present_and_ordered():
    lines = _lines(ex.build_my_tenders_csv([], "ru"))
    assert lines == ["Название;Заказчик;Сумма;Валюта;Дедлайн;Статус;Папка;Ссылка"]


def test_empty_list_gives_header_only_csv():
    """An empty list — a valid CSV with headers (not a 500, not an empty file)."""
    raw = ex.build_my_tenders_csv([])
    assert _text(raw).count("\r\n") == 1


def test_cyrillic_survives_roundtrip():
    raw = ex.build_my_tenders_csv([_row()], "ru")
    text = _text(raw)
    assert "Набавка канцеларијског материјала" in text
    assert "Град Београд" in text
    assert "Приоритет" in text


def test_row_columns_in_order():
    # The deadline _DL = 12:30Z (August, CEST +2) is printed in Belgrade time → 14:30.
    line = _lines(ex.build_my_tenders_csv([_row()], "ru"))[1]
    assert line == (
        "Набавка канцеларијског материјала;Град Београд;1250000;RSD;"
        "01.08.2026 14:30;Изучаю;Приоритет;https://x/1"
    )


def test_uses_shared_csv_contour():
    """The build — the same function as the award export (there is no second contour)."""
    from src.bots.analytics_export import csv_bytes

    from src.api.cabinet.i18n import CABINET

    headers = [CABINET["ru"][k] for k in ex._HEADER_KEYS]
    assert ex.build_my_tenders_csv([], "ru") == csv_bytes([headers])


def test_semicolon_in_value_is_quoted():
    """A value with a separator must not drift across the columns."""
    line = _lines(ex.build_my_tenders_csv([_row(title="Ремонт; фаза 2")], "ru"))[1]
    assert line.startswith('"Ремонт; фаза 2";')


# --------------------------------------------------------------------------- #
# Empty / missing fields
# --------------------------------------------------------------------------- #


def test_missing_fields_render_empty_not_none():
    line = _lines(
        ex.build_my_tenders_csv(
            [
                _row(
                    customer_name=None,
                    amount=None,
                    currency=None,
                    deadline_at=None,
                    tender_url=None,
                    user_status=None,
                    folder_names=None,
                )
            ],
            "ru",
        )
    )[1]
    assert line == "Набавка канцеларијског материјала;;;;;;;"


def test_absent_amount_is_blank_not_zero():
    """A None amount — an empty cell: “0 RSD” would be a lie about the tender."""
    line = _lines(ex.build_my_tenders_csv([_row(amount=None)]))[1]
    assert ";;RSD;" in line


# --------------------------------------------------------------------------- #
# Statuses
# --------------------------------------------------------------------------- #


def test_status_labels_cover_funnel():
    """Stage captions — from the SSOT of the common lexicon (COMMON), without emoji.

    ‼️ Previously the export kept ITS OWN dictionary (“watching”, “participating”), and it diverged from what the client sees in the cabinet and the bot (“Studying”, “Participating”). One meaning — one catalog: now we take COMMON and strip the emoji prefix.
    """
    assert ex.status_label("active") == "Участвую"
    assert ex.status_label("watching") == "Изучаю"
    assert ex.status_label("skipped") == "Пропущен"


def test_status_new_and_none_are_blank():
    """A tender only in a folder (no funnel row) / new — without an invented status."""
    assert ex.status_label(None) == ""
    assert ex.status_label("new") == ""
