"""Unit tests of the single builder of the profile context (:mod:`src.ai.profile_context`).

We cover:
- Each field separately (CPV / regions / keywords / budget /
  procedure_types / ai_extra_context / track record).
- A SNAPSHOT regression: for the previous set of fields (without budget/procedures/track record)
  the output matches the old four copies byte for byte — there is no regression in the four
  calls.
- An empty profile → fallback.
- A mutation of the track record: the track-record line must get into the output (if its
  append is removed from the builder — this test turns red).
"""

from __future__ import annotations

from src.ai.profile_context import _EMPTY, format_profile_context

_CPV = [{"scheme": "CPV", "code": "45000000", "label": "Строительство"}]


# --------------------------------------------------------------------------- #
#  Snapshot regression: the previous set of fields → the previous output
# --------------------------------------------------------------------------- #


def test_snapshot_existing_fields_unchanged():
    """The old 4 copies gave exactly this line for these fields — there is no regression."""
    ctx = format_profile_context(
        classifications=_CPV,
        regions=["Белград", "Нови Сад"],
        keywords_include={"keywords": ["строительство", "ремонт"]},
        ai_extra_context="Промышленные объекты",
    )
    assert ctx == (
        "CPV-коды профиля: 45000000 (Строительство)\n"
        "Регионы: Белград, Нови Сад\n"
        "Ключевые слова: строительство, ремонт\n"
        "Дополнительный контекст:\n"
        "Промышленные объекты"
    )


def test_empty_profile_fallback():
    """No field at all → the canonical fallback."""
    ctx = format_profile_context()
    assert ctx == _EMPTY
    assert "профиль не заполнен" in ctx


# --------------------------------------------------------------------------- #
#  Each field separately
# --------------------------------------------------------------------------- #


def test_cpv_only():
    assert format_profile_context(classifications=_CPV) == (
        "CPV-коды профиля: 45000000 (Строительство)"
    )


def test_regions_only():
    assert format_profile_context(regions=["Белград"]) == "Регионы: Белград"


def test_keywords_only():
    assert (
        format_profile_context(keywords_include={"keywords": ["ремонт"]})
        == "Ключевые слова: ремонт"
    )


def test_ai_extra_context_only():
    assert format_profile_context(ai_extra_context="  О компании  ") == (
        "Дополнительный контекст:\nО компании"
    )


def test_budget_both_bounds():
    assert format_profile_context(min_amount_eur=1000, max_amount_eur=50000) == (
        "Бюджет профиля: от 1000 до 50000 EUR"
    )


def test_budget_min_only():
    assert format_profile_context(min_amount_eur=1000) == "Бюджет профиля: от 1000 EUR"


def test_budget_max_only():
    assert (
        format_profile_context(max_amount_eur=50000) == "Бюджет профиля: до 50000 EUR"
    )


def test_budget_decimal_kept():
    """Non-integer amounts — 2 digits; integers — without a tail."""
    assert format_profile_context(min_amount_eur=1500.5) == (
        "Бюджет профиля: от 1500.50 EUR"
    )


def test_budget_absent_when_none():
    """No min and no max → there is no budget line at all."""
    assert "Бюджет" not in format_profile_context(classifications=_CPV)


def test_procedure_types_only():
    assert format_profile_context(procedure_types=["open", "restricted"]) == (
        "Типы процедур: open, restricted"
    )


def test_procedure_types_blank_skipped():
    """A list of empty strings → there is no procedures line."""
    assert format_profile_context(procedure_types=["", "  "]) == _EMPTY


# --------------------------------------------------------------------------- #
#  The order of the blocks and the track record (the mutation target)
# --------------------------------------------------------------------------- #


def test_full_block_order():
    """A stable order: CPV → regions → keywords → budget → procedures →
    additional context → track record."""
    ctx = format_profile_context(
        classifications=_CPV,
        regions=["Белград"],
        keywords_include={"keywords": ["ремонт"]},
        min_amount_eur=1000,
        max_amount_eur=50000,
        procedure_types=["open"],
        ai_extra_context="О компании",
        track_record="Трек-рекорд компании: подано 3, выиграно 1, проиграно 0.",
    )
    lines = ctx.split("\n")
    assert lines[0].startswith("CPV-коды профиля:")
    assert lines[1].startswith("Регионы:")
    assert lines[2].startswith("Ключевые слова:")
    assert lines[3].startswith("Бюджет профиля:")
    assert lines[4].startswith("Типы процедур:")
    assert lines[5].startswith("Дополнительный контекст:")
    assert lines[-1].startswith("Трек-рекорд компании:")


def test_track_record_appended():
    """Mutation: the track record must get into the output (remove the append → red)."""
    tr = "Трек-рекорд компании: подано 5, выиграно 2, проиграно 1."
    ctx = format_profile_context(classifications=_CPV, track_record=tr)
    assert tr in ctx


def test_track_record_none_not_added():
    """An empty track record (None) adds no lines and does not interfere with the fallback."""
    assert format_profile_context(track_record=None) == _EMPTY
    assert format_profile_context(track_record="   ") == _EMPTY
