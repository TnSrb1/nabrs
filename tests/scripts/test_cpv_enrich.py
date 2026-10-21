"""The unit of the enrichment of cpv_codes.json (t2b): l_sr by the code, l_ru by the sr label.

No DB is needed — the pure function ``enrich``.
"""

from __future__ import annotations

from scripts.cpv_enrich import enrich


def _records():
    return [
        {"c": "33140000", "lv": 2, "p": "33000000", "l": "Medical consumables"},
        {"c": "03221270", "lv": 4, "p": "03221200", "l": "Cucumbers"},
    ]


def test_enrich_adds_sr_and_ru_by_code_and_label():
    sr = {"33140000": "Медицински потрошни материјал"}
    ru = {"Медицински потрошни материјал": "Медицинские расходные материалы"}
    out = enrich(_records(), sr, ru)
    covered = out[0]
    assert covered["l_sr"] == "Медицински потрошни материјал"
    assert covered["l_ru"] == "Медицинские расходные материалы"
    assert covered["l"] == "Medical consumables"  # the English is not lost
    # a code outside the ingest → only the English l, without l_sr/l_ru (the documented fallback)
    assert "l_sr" not in out[1]
    assert "l_ru" not in out[1]


def test_enrich_key_order_primary_sr_before_english():
    sr = {"33140000": "Медицински потрошни материјал"}
    ru = {"Медицински потрошни материјал": "Медицинские расходные материалы"}
    keys = list(enrich(_records(), sr, ru)[0].keys())
    assert keys == ["c", "lv", "p", "l_sr", "l", "l_ru"]


def test_enrich_sr_without_ru_leaves_ru_absent():
    sr = {"03221270": "Краставци"}
    out = enrich(_records(), sr, ru_map={})
    rec = out[1]
    assert rec["l_sr"] == "Краставци"
    assert (
        "l_ru" not in rec
    )  # there is no translation → the widget falls back to the English l


def test_enrich_is_idempotent():
    sr = {"33140000": "Медицински потрошни материјал"}
    ru = {"Медицински потрошни материјал": "Медицинские расходные материалы"}
    once = enrich(_records(), sr, ru)
    twice = enrich(once, sr, ru)
    assert once == twice


def test_enrich_stale_sr_ru_are_dropped_on_rerun():
    # the record already carries the old l_sr/l_ru, while the sources no longer cover them
    stale = [{"c": "03221270", "lv": 4, "l": "Cucumbers", "l_sr": "X", "l_ru": "Y"}]
    out = enrich(stale, sr_map={}, ru_map={})
    assert "l_sr" not in out[0]
    assert "l_ru" not in out[0]
