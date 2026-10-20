"""The unit of the filling of l_sr from the labels of the portal (cpv-sr-labels).

No DB is needed: the pure functions ``portal_snapshot`` and ``fill_missing`` plus a check of the file ``cpv_codes.json`` itself by literals (45 sections, code 34).
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.cpv_sr_from_portal import LV1_TRANSLATIONS, fill_missing, portal_snapshot

_CODES = (
    Path(__file__).resolve().parents[2] / "src" / "api" / "static" / "cpv_codes.json"
)


def test_portal_snapshot_keeps_cyrillic_and_most_frequent():
    tsv = [
        "34000000\tОпрема и помоћни производи за превоз\t5",
        "34000000\tOprema latinica\t9",  # Latin — not a label (the Cyrillic filter)
        "45000000\tГрађевински радови\t2",
        "45000000\tГрађевински радови (други)\t1",
    ]
    assert portal_snapshot(tsv) == {
        "34000000": "Опрема и помоћни производи за превоз",
        "45000000": "Грађевински радови",
    }


def test_fill_missing_portal_then_lv1_translation_and_keeps_existing():
    records = [
        {"c": "34000000", "lv": 1, "l": "Transport"},
        {"c": "14000000", "lv": 1, "l": "Mining"},
        {"c": "33140000", "lv": 2, "l_sr": "Постојеће", "l": "Consumables"},
        {"c": "33141000", "lv": 3, "l": "Nothing"},
    ]
    portal = {"34000000": "Портал 34", "33140000": "Портал друго"}
    out, filled, lv1_left = fill_missing(records, portal, {"14000000": "Превод 14"})
    assert out[0] == {"c": "34000000", "lv": 1, "l_sr": "Портал 34", "l": "Transport"}
    assert out[1]["l_sr"] == "Превод 14"
    assert out[2]["l_sr"] == "Постојеће"  # the already filled one is not overwritten
    assert "l_sr" not in out[3]  # below lv1 without a label — the EN fallback
    assert dict(filled) == {1: 2}
    assert lv1_left == []


def test_fill_missing_reports_lv1_left_without_translation():
    out, _, lv1_left = fill_missing([{"c": "41000000", "lv": 1, "l": "Water"}], {}, {})
    assert "l_sr" not in out[0]
    assert lv1_left == ["41000000"]


def test_lv1_translations_cover_only_portal_gaps_in_json():
    # the translation must not overlap the label of the portal (34, 35, 43, 90 — from the portal)
    assert not {"34000000", "35000000", "43000000", "90000000"} & set(LV1_TRANSLATIONS)


def test_json_all_45_lv1_have_nonempty_l_sr():
    records = json.loads(_CODES.read_text(encoding="utf-8"))
    lv1 = [r for r in records if r["lv"] == 1]
    assert len(lv1) == 45
    assert [r["c"] for r in lv1 if not r.get("l_sr")] == []


def test_json_code_34_sr_literal():
    records = json.loads(_CODES.read_text(encoding="utf-8"))
    rec = next(r for r in records if r["c"] == "34000000")
    assert rec["l_sr"] == "Опрема и помоћни производи за превоз"


def test_json_lv1_translations_match_portal_terms():
    # The expectations — literals from the portal (classifications of jnportal, SELECT 14.10.2026):
    # 14700000 “Prosti metali”, 19500000 “Gumeni i plastični materijali”,
    # 51500000/51700000/51800000 “Usluge instaliranja …”.
    records = json.loads(_CODES.read_text(encoding="utf-8"))
    sr = {
        r["c"]: r["l_sr"]
        for r in records
        if r["c"] in {"14000000", "19000000", "51000000"}
    }
    assert sr == {
        "14000000": "Рударски производи, прости метали и сродни производи",
        "19000000": "Кожа и текстилне тканине, гумени и пластични материјали",
        "51000000": "Услуге инсталирања (осим софтвера)",
    }
