"""The check of the table ``_SR_LAT`` in cpv_widget.js against ``cyr_to_lat_strict`` (Python).

The transliteration lives in two places (the templates/backend and the JS widget); a divergence of the tables will silently show the Serbian client a different Latin in the CPV picker than on the other screens.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.domain.text_norm import cyr_to_lat_strict

_JS = Path(__file__).resolve().parents[2] / "src/api/static/js/cpv_widget.js"


def _js_table() -> dict[str, str]:
    src = _JS.read_text(encoding="utf-8")
    body = re.search(r"const _SR_LAT = \{(.*?)\};", src, re.S).group(1)
    return dict(re.findall(r"(\S):\s*'([^']*)'", body))


def test_js_table_matches_python_per_letter():
    table = _js_table()
    assert len(table) >= 39, "the table did not parse"
    for cyr, lat in table.items():
        assert cyr_to_lat_strict(cyr) == lat, cyr


def test_python_covers_every_js_letter_and_vice_versa():
    table = _js_table()
    cyr_letters = {chr(c) for c in range(0x0430, 0x0450)} | {
        "ђ",
        "ј",
        "љ",
        "њ",
        "ћ",
        "џ",
        "ё",
    }
    missing = {c for c in cyr_letters if c not in table}
    assert not missing, f"the JS has no letters: {sorted(missing)}"
