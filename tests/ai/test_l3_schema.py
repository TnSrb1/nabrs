"""Tests of the L3Output schema and the registration of 'deep' in _SCHEMA_BY_LEVEL (STAGE 3 L3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.ai import schemas
from src.ai.schemas import L3Output, validate_output


def test_validate_output_deep_registered():
    """'deep' is now registered in _SCHEMA_BY_LEVEL — there must be no KeyError."""
    result = validate_output("deep", {})
    assert isinstance(result, dict)


def test_l3_output_all_optional_default_empty():
    """L3Output with an empty dict — without errors, all lists are empty, the numbers are null."""
    out = L3Output.model_validate({})
    assert out.match_score is None
    assert out.summary is None
    assert out.match_reasons == []
    assert out.red_flags == []
    assert out.required_qualifications == []
    assert out.work_breakdown == []
    assert out.participation_checklist == []
    assert out.focus_answers == []
    assert out.estimated_effort_days is None
    assert out.competition_estimate is None


def test_l3_output_full_valid():
    """L3Output accepts the full correct model response."""
    data = {
        "summary": "Детальный разбор тендера",
        "match_score": 4,
        "match_reasons": ["Профиль совпадает"],
        "red_flags": ["Короткий срок"],
        "required_qualifications": ["Лицензия строителя"],
        "estimated_effort_days": 45,
        "competition_estimate": "medium",
        "work_breakdown": ["Проектирование", "Строительство", "Сдача"],
        "participation_checklist": ["Получить лицензию", "Подготовить заявку"],
        "focus_answers": [{"question": "Нужна ли лицензия?", "answer": "Да"}],
    }
    out = validate_output("deep", data)
    assert out["match_score"] == 4
    assert out["work_breakdown"] == ["Проектирование", "Строительство", "Сдача"]
    assert out["participation_checklist"] == ["Получить лицензию", "Подготовить заявку"]
    assert len(out["focus_answers"]) == 1
    assert out["focus_answers"][0]["question"] == "Нужна ли лицензия?"


def test_l3_output_extra_fields_ignored():
    """extra='ignore' — extra fields of the model response do not fail."""
    out = validate_output("deep", {"summary": "s", "unknown_field": 42, "另外": "xx"})
    assert out["summary"] == "s"
    assert "unknown_field" not in out


def test_l3_output_invalid_match_score_type():
    """match_score — int; a string must raise ValidationError."""
    with pytest.raises(ValidationError):
        L3Output.model_validate({"match_score": "пять"})


def test_l3_output_invalid_competition_estimate():
    """competition_estimate — Optional[str], accepts any string (not an enum)."""
    out = L3Output.model_validate({"competition_estimate": "very_high"})
    assert out.competition_estimate == "very_high"


def test_schema_by_level_contains_all_three():
    """_SCHEMA_BY_LEVEL must contain quick, standard and deep."""
    assert "quick" in schemas._SCHEMA_BY_LEVEL
    assert "standard" in schemas._SCHEMA_BY_LEVEL
    assert "deep" in schemas._SCHEMA_BY_LEVEL


def test_validate_output_deep_returns_dict():
    """validate_output('deep', ...) → dict (not a Pydantic object)."""
    result = validate_output("deep", {"summary": "OK"})
    assert isinstance(result, dict)
    assert result["summary"] == "OK"
