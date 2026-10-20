"""Guard for the CPV directory data/search (t2b).

The directory’s original language is Serbian: codes covered by ingest must carry a non-empty ``l_sr``. The widget search matches by code AND by the sr original AND by the locale translation — here we mirror the normalization (cyr→lat transliteration without diacritics) and check that a Serbian substring finds the record regardless of script.

No DB/browser needed — only static json and source dictionaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CODES = _ROOT / "src" / "api" / "static" / "cpv_codes.json"
_SR = _ROOT / "scripts" / "data" / "cpv_sr_labels.json"

# Transliteration cyr (sr+ru)→lat without diacritics — 1:1 with _TRANSLIT in cpv_widget.js.
_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "ђ": "dj",
    "е": "e",
    "ё": "e",
    "ж": "z",
    "з": "z",
    "и": "i",
    "й": "j",
    "ј": "j",
    "к": "k",
    "л": "l",
    "љ": "lj",
    "м": "m",
    "н": "n",
    "њ": "nj",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "ћ": "c",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "c",
    "џ": "dz",
    "ш": "s",
    "щ": "sc",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "ju",
    "я": "ja",
    "і": "i",
    "đ": "dj",
    "ž": "z",
    "ć": "c",
    "č": "c",
    "š": "s",
}


def _norm(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in (s or "").lower())


def _match(entry: dict, raw: str) -> bool:
    q = _norm(raw.strip())
    digits = "".join(c for c in raw if c.isdigit())
    if digits and entry["c"].startswith(digits):
        return True
    for f in ("l_sr", "l_ru", "l"):
        if q and q in _norm(entry.get(f, "")):
            return True
    return False


@pytest.fixture(scope="module")
def codes():
    return json.loads(_CODES.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sr_map():
    return json.loads(_SR.read_text(encoding="utf-8"))


def test_full_classifier_present(codes):
    assert len(codes) == 9454
    assert all("c" in e and "l" in e for e in codes)


def test_covered_codes_have_nonempty_sr(codes, sr_map):
    by_code = {e["c"]: e for e in codes}
    # each code from the ingest snapshot → non-empty l_sr in the static data
    assert sr_map, "the sr snapshot must not be empty"
    for code in sr_map:
        assert by_code[code].get("l_sr"), f"covered code {code} has no l_sr"


def test_search_matches_serbian_substring_any_script(codes):
    # sr-specific substring (absent in English labels) — the match requires l_sr
    assert any(_match(e, "одржавањ") for e in codes)  # "usluge održavanja…"
    # the same label in Latin/ascii via transliteration — the sr Latin form also finds it
    med = next(e for e in codes if e["c"] == "33140000")
    assert _match(med, "Медицински")  # cyr → medicinski, not in the English "Medical…"
    assert _match(med, "potrosni")  # ascii Latin “potrošni”


def test_search_matches_code_prefix(codes):
    assert any(_match(e, "50000000") for e in codes)
    assert any(_match(e, "331") for e in codes)


def test_search_matches_russian_translation(codes):
    # on the ru site the translation (l_ru) also takes part in the search
    with_ru = [e for e in codes if e.get("l_ru")]
    assert with_ru, "there must be codes with a Russian translation"
    assert _match(with_ru[0], with_ru[0]["l_ru"][:6])


def test_browse_roots_exist(codes):
    roots = [e for e in codes if e.get("lv") == 1]
    assert 30 <= len(roots) <= 60  # top-level CPV sections ~45
    # each root has children by p (hierarchy for drilling down)
    codes_with_parent = {e.get("p") for e in codes if e.get("p")}
    assert any(r["c"] in codes_with_parent for r in roots)
