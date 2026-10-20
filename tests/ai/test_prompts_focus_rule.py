"""A single constant _FOCUS_RULE for focus_answers in L2/L3 (DRY, without a DB).

A guard against divergence of the focus_answers wording between the levels:
the rule is moved to one constant and substituted into both prompts, the canon —
the stronger L2 version (“exactly once” + “NOT a null object”).
"""

from __future__ import annotations

from src.ai import prompts
from src.ai.prompts import _FOCUS_RULE, build_l2_system, build_l3_system


def test_l2_and_l3_carry_the_strong_focus_rule() -> None:
    l2 = build_l2_system("Сербия", "ru")
    l3 = build_l3_system("Сербия", "ru")
    for rendered in (l2, l3):
        assert "ровно один раз" in rendered
        assert "НЕ null-объект" in rendered
        assert "question копируй дословно" in rendered


def test_focus_rule_is_single_source() -> None:
    # Both selections — from one constant, not a copy.
    assert "ровно один раз" in _FOCUS_RULE
    assert "НЕ null-объект" in _FOCUS_RULE


def test_old_l3_wording_gone_from_templates() -> None:
    # The old weak L3 wording must not remain in the source outside the
    # single constant (its text is absent from _FOCUS_RULE).
    assert "в документации не указано" not in prompts.SYSTEM_L2_TEMPLATE
    assert "в документации не указано" not in prompts.SYSTEM_L3_TEMPLATE
    assert "в документации не указано" not in _FOCUS_RULE
