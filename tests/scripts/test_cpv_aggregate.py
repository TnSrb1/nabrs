"""The unit of the aggregator of the Serbian names of CPV (t2b): parsing of the line and dedup of the divergences.

No DB is needed — the pure functions ``parse_cpv_line`` and ``pick_labels`` are checked.
"""

from __future__ import annotations

from scripts.cpv_aggregate_sr import parse_cpv_line, pick_labels


def test_parse_cpv_line_ok():
    assert parse_cpv_line("03221270 - Краставци") == ("03221270", "Краставци")
    # a separator with different spaces
    assert parse_cpv_line("33140000  -  Медицински потрошни материјал") == (
        "33140000",
        "Медицински потрошни материјал",
    )


def test_parse_cpv_line_rejects_garbage():
    assert parse_cpv_line("нет кода") is None
    assert parse_cpv_line("1234 - слишком короткий код") is None  # not 8 digits
    assert parse_cpv_line("03221270 - ") is None  # an empty label
    assert parse_cpv_line(None) is None  # type: ignore[arg-type]


def test_pick_labels_picks_most_frequent_variant():
    # one code with diverging forms → take the most frequent
    rows = [
        ("50000000", "Услуге одржавања", 3),
        ("50000000", "Usluge održavanja", 10),
        ("33140000", "Медицински потрошни материјал", 5),
    ]
    out = pick_labels(rows)
    assert out["50000000"] == "Usluge održavanja"
    assert out["33140000"] == "Медицински потрошни материјал"


def test_pick_labels_deterministic_on_tie():
    # an equal frequency → the lexicographically smaller (the stability of the snapshot)
    rows = [("11111111", "Бета", 2), ("11111111", "Алфа", 2)]
    assert pick_labels(rows)["11111111"] == "Алфа"


def test_pick_labels_skips_empty():
    rows = [("22222222", "", 4), ("", "label", 4), ("33333333", "OK", 1)]
    out = pick_labels(rows)
    assert out == {"33333333": "OK"}
