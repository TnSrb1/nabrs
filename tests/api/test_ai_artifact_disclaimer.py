"""E-F8 (audit 07.10): the verdict, the checklist and the draft show the STATIC disclaimer exactly once.

The rendering goes through the same import as `_ai_slot.html` (`with context`), otherwise the macro
sees the global `t` instead of the language catalog. The asserts are literals from the catalog.
"""

from __future__ import annotations

import pytest

from src.api.cabinet.i18n import cabinet_context_for_lang
from src.api.templating import templates

_TEMPLATE = (
    '{% from "_ai_artifact.html" import render with context %}'
    "{{ render(level, output) }}"
)

_DISCLAIMER = {
    "sr": "AI može da greši — proverite sa dokumentacijom nabavke.",
    "ru": "AI может ошибаться — сверяйте с документацией закупки.",
    "en": "AI can make mistakes — check against the procurement documentation.",
}

_OUTPUT = {
    "bid": {"verdict": "go", "score": 80, "pros": ["x"], "cons": []},
    "checklist": {
        "documents": [{"title": "Doc", "mandatory": True}],
        "deadlines": [],
        "disqualification_risks": [],
        "summary": "",
    },
    "draft": {
        "sections": [{"heading": "H", "body": "B"}],
        "criteria_mapping": [],
        "disclaimer": "MODEL-NOTE",
    },
}


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
@pytest.mark.parametrize("level", ["bid", "checklist", "draft"])
def test_artifact_has_static_disclaimer_in_each_language(lang, level):
    t = cabinet_context_for_lang(lang)["t"]
    html = templates.env.from_string(_TEMPLATE).render(
        level=level, output=_OUTPUT[level], t=t, lang=lang
    )
    assert html.count(_DISCLAIMER[lang]) == 1


@pytest.mark.parametrize("lang", ["sr", "ru", "en"])
def test_draft_shows_only_static_disclaimer(lang):
    """Two ⚠️ in a row (static + the model’s text) — we keep one, the static one: it is under our control."""
    t = cabinet_context_for_lang(lang)["t"]
    html = templates.env.from_string(_TEMPLATE).render(
        level="draft", output=_OUTPUT["draft"], t=t, lang=lang
    )
    assert html.count(_DISCLAIMER[lang]) == 1
    assert "MODEL-NOTE" not in html
