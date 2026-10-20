"""Column layout of the right block of the rows “deadline + value” (tender-row-columns, owner 13.10).

The deadline badge and the value (amount / delivery date) stand in fixed columns
``fd-card__side--cols``, otherwise the badge floats by the length of the neighbouring text (measurement ``scripts/shots/dash_teaser_probe.js``: a spread of the left edge of 56.7 px at 1280).

The list of partials is NOT hardcoded: the row “deadline + value” is a partial (without
``extends``) that draws the deadline via the filter ``days_left`` and the value via ``delivered_prefix`` / ``amount_eur`` / ``amount_original``. Such a predicate finds new rows by itself; ``tender.html`` (a page, not a row) is cut off by ``extends``.

Mutation: remove ``fd-card__side--cols`` from any partial → the test goes red.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "api" / "templates"

_VALUE_MARKERS = ("delivered_prefix", "amount_eur", "amount_original")
_COLS_CLASS = re.compile(r'class="[^"]*\bfd-card__side--cols\b[^"]*"')


def _row_partials() -> list[Path]:
    rows = []
    for path in sorted(TEMPLATES_DIR.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if "{% extends" in text:
            continue
        if "days_left" in text and any(m in text for m in _VALUE_MARKERS):
            rows.append(path)
    return rows


def test_predicate_finds_row_partials():
    # A guard against a silent empty predicate: without rows the test proves nothing.
    assert (
        _row_partials()
    ), "the predicate of the “deadline + value” rows found no partial"


def test_every_row_partial_has_value_columns():
    missing = [
        p.name
        for p in _row_partials()
        if not _COLS_CLASS.search(p.read_text(encoding="utf-8"))
    ]
    assert (
        not missing
    ), f"the partials “deadline + value” without fd-card__side--cols (the badge will float): {missing}"
