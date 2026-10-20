"""CSV export “My tenders” — in the cabinet language, stages from the common lexicon.

Defect: the file header (“Name”, “Naručilac”, “Amount”…) and the stage captions were hardcoded in Russian, and the file goes to the CLIENT. Plus, the export kept ITS OWN stage dictionary (“watching”, “participating”), which diverged from what the client sees in the cabinet and in the bot (“Studying”, “Participating”) — exactly the case the canon forbids: the common lexicon lives in ONE catalog ``i18n_common.COMMON``.
"""

from __future__ import annotations

import re

import pytest

from src.api.cabinet import my_tenders_export as ex
from src.api.cabinet.i18n import CABINET
from src.i18n_common import COMMON

_CYR = re.compile(r"[а-яА-ЯёЁ]")


def _header_line(lang: str | None) -> str:
    raw = ex.build_my_tenders_csv([], lang).decode("utf-8-sig")
    return raw.splitlines()[0]


@pytest.mark.parametrize("lang", ["sr", "en"])
def test_headers_have_no_russian(lang):
    """Header in the client’s language: not a single Russian letter.

    Mutation: return the literal ``_HEADERS`` → the test goes red.
    """
    line = _header_line(lang)
    assert not _CYR.search(line), line
    assert line.split(";")[0] == CABINET[lang]["csv_h_title"]


def test_headers_ru_unchanged():
    """The Russian client gets the previous header — the canon is intact."""
    assert _header_line("ru").split(";") == [
        "Название",
        "Заказчик",
        "Сумма",
        "Валюта",
        "Дедлайн",
        "Статус",
        "Папка",
        "Ссылка",
    ]


def test_unknown_lang_falls_back_to_sr():
    """None/unknown language → sr (owner 30.09.2026), not a KeyError on export."""
    assert _header_line(None) == _header_line("sr")
    assert _header_line("de") == _header_line("sr")


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_status_labels_come_from_common_catalog(lang):
    """Stages — from the SSOT of the common lexicon, emoji removed (Excel/Calc read text).

    There must be no second stage dictionary: the bot, the cabinet and the export must name one stage identically.
    """
    for status, key in ex._STATUS_KEYS.items():
        label = ex.status_label(status, lang)
        expected = COMMON[lang][key]
        assert label in expected, (status, label, expected)
        assert not label.startswith("👀") and not label.startswith("✅")


@pytest.mark.parametrize("lang", ["sr", "en"])
def test_status_label_has_no_russian(lang):
    """A stage in the export does not stay Russian in a Serbian/English cabinet."""
    for status in ex._STATUS_KEYS:
        assert not _CYR.search(ex.status_label(status, lang)), status


def test_no_status_stays_blank():
    """A tender without a stage (came in via a folder) — an empty cell, not an invention."""
    assert ex.status_label(None, "sr") == ""
    assert ex.status_label("new", "sr") == ""


def test_ru_catalog_has_no_serbian_customer_word():
    """The ru catalog of the cabinet has no Serbian “naručilac/naručioci” and no anglicism “average”.

    Mutation: return “Naručioci” to ``nav_narucilac`` → the test goes red.
    """
    bad = re.compile(r"наруч|\baverage\b", re.IGNORECASE)
    hits = {
        k: v for k, v in CABINET["ru"].items() if isinstance(v, str) and bad.search(v)
    }
    assert hits == {}
