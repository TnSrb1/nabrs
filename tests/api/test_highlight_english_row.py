"""Highlighting of examples in an ENGLISH tender — only with a word boundary (#801, review).

Before the fix the sr-term (and the raw include-word) was highlighted as a substring inside an English
word: "put" in "Output", "road" in "Broad". English-text matching does not find such words
(``_english_forms`` + word boundary), so the highlighting was lying. Expectations are
literals from the title, not via highlight_parts.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.api.cabinet.preview_service import _example, _highlight_terms_english_rows


def _en_row(title: str):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        title=title,
        title_normalized=None,
        customer_name=None,
        deadline_at=None,
        archived=False,
        language="en",
        source_key="worldbank",
    )


def test_sr_term_is_not_painted_inside_english_word():
    ex = _example(_en_row("Output analysis for the agency"), ["put"], [], [])
    assert ex.title_parts == [("Output analysis for the agency", False)]


def test_raw_include_word_respects_word_boundary_on_english_row():
    row = _en_row("Broad consultation and regional road works")
    ex = _example(row, ["road"], [], ["road"])
    assert ex.title_parts == [
        ("Broad consultation and regional ", False),
        ("road", True),
        (" works", False),
    ]


def test_english_row_highlight_list_includes_raw_include_words():
    payload = {
        "keywords_include": [{"all_of": ["road"]}],
        "keyword_dict": False,
    }
    assert "road" in _highlight_terms_english_rows(payload)
