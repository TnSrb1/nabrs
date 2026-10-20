"""E-F8 (audit 07.10): the bot — the verdict, the checklist and the draft carry a disclaimer in the locale’s language.

The assertions are literals from the catalog `AI_RENDER`. The data (the header, the model answers) are not translated.
"""

from __future__ import annotations

import pytest

from src.bots.bid_verdict_service import _render_bid_verdict
from src.bots.compliance_checklist_service import _render_checklist
from src.bots.deep_analysis_service import _render_l3_chunks
from src.bots.draft_service import _render_draft

pytestmark = pytest.mark.sr_default

_DISCLAIMER = {
    None: "AI može da greši — proverite sa dokumentacijom nabavke.",
    "sr": "AI može da greši — proverite sa dokumentacijom nabavke.",
    "ru": "AI может ошибаться — сверяйте с документацией закупки.",
    "en": "AI can make mistakes — check against the procurement documentation.",
}

_VERDICT = {"verdict": "go", "score": 80, "pros": ["x"], "cons": []}
_CHECKLIST = {
    "documents": [{"title": "Doc", "mandatory": True}],
    "deadlines": [],
    "disqualification_risks": [],
    "summary": "",
}
_DRAFT = {"sections": [{"heading": "H", "body": "B"}], "criteria_mapping": []}


@pytest.mark.parametrize("locale", [None, "sr", "ru", "en"])
def test_bid_verdict_carries_disclaimer(locale):
    assert _DISCLAIMER[locale] in _render_bid_verdict("T", _VERDICT, locale)


@pytest.mark.parametrize("locale", [None, "sr", "ru", "en"])
def test_checklist_carries_disclaimer(locale):
    assert _DISCLAIMER[locale] in _render_checklist("T", _CHECKLIST, locale)


@pytest.mark.parametrize("locale", [None, "sr", "ru", "en"])
def test_draft_carries_disclaimer(locale):
    assert _DISCLAIMER[locale] in _render_draft("T", _DRAFT, locale)


def test_draft_disclaimer_survives_trim_of_long_draft():
    """A long draft is cut from the tail (up to 4096) — the disclaimer stands under the header and is not lost."""
    long_draft = {"sections": [{"heading": "H", "body": "x" * 500} for _ in range(20)]}
    result = _render_draft("T", long_draft, "ru")
    assert len(result) <= 4096
    assert result.endswith("…")
    assert _DISCLAIMER["ru"] in result


@pytest.mark.parametrize("locale", [None, "sr", "ru", "en"])
def test_deep_analysis_carries_disclaimer_exactly_once(locale):
    """The deep analysis (the bot) earlier went out without a disclaimer at all — the client saw the bare model text."""
    output = {"summary": "Резюме", "match_score": 4, "work_breakdown": ["Шаг"]}
    text = "\n".join(_render_l3_chunks("T", output, locale))
    assert text.count(_DISCLAIMER[locale]) == 1


def test_deep_analysis_disclaimer_once_across_pages():
    """A long analysis is split into several messages — the disclaimer is in exactly one of them."""
    output = {"summary": "x" * 3000, "work_breakdown": ["y" * 500 for _ in range(12)]}
    chunks = _render_l3_chunks("T", output, "ru")
    assert len(chunks) > 1
    assert "\n".join(chunks).count(_DISCLAIMER["ru"]) == 1


@pytest.mark.parametrize("locale", [None, "sr", "ru", "en"])
def test_checklist_disclaimer_exactly_once(locale):
    assert _render_checklist("T", _CHECKLIST, locale).count(_DISCLAIMER[locale]) == 1


def test_checklist_disclaimer_survives_trim_of_long_checklist():
    """A 6000-character checklist is cut to 4096 from the tail — the disclaimer in the tail was lost (E-F8)."""
    long_checklist = dict(_CHECKLIST, summary="x" * 6000)
    result = _render_checklist("T", long_checklist, "ru")
    assert len(result) <= 4096
    assert result.endswith("…")
    assert result.count(_DISCLAIMER["ru"]) == 1
