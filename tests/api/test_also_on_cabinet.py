"""The “same tender on other portals” mark in the cabinet: feed, teaser, card.

The same fork as on the public card (``tender.html``): ``probable`` →
“probably”, otherwise “same”; N = 0 — no mark. The expectations are catalog literals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.i18n import cabinet_context_for_lang
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from src.api.templating import templates
from tests.api.conftest import FakeSession

_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"


def _item(**over):
    base = dict(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        title="Набавка рачунара",
        tender_url="https://portal.rs/t/1",
        customer_name="Општина Нови Сад",
        profile_name="ИТ набавке",
        deadline_at=None,
        sent_at=None,
        source_label="TED",
        canonical_confidence="dup",
        also_on_n=1,
        ai_actions=SimpleNamespace(
            delivered_analysis_level=None,
            has_bid_verdict=False,
            has_checklist=False,
            has_draft=False,
        ),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _render(template: str, item, lang: str) -> str:
    return templates.get_template(template).render(
        item=item,
        feed_items=[item],
        profiles=[object()],
        telegram_linked=True,
        **cabinet_context_for_lang(lang),
    )


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
def test_feed_and_teaser_mark_dup(template):
    html = _render(template, _item(), "sr")
    assert "🔁 Isti tender je i na drugim platformama: 1" in html


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
def test_feed_and_teaser_mark_probable(template):
    html = _render(template, _item(canonical_confidence="probable"), "ru")
    assert "🔁 Вероятно, тот же тендер есть и на других площадках: 1" in html


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
def test_feed_and_teaser_without_cluster_have_no_mark(template):
    html = _render(template, _item(canonical_confidence=None, also_on_n=0), "sr")
    assert "data-also-on" not in html


def _detail(monkeypatch, **over) -> str:
    data = TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs={},
        **over,
    )

    async def _stub(*_a, **_kw):
        return data

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _stub)
    app.dependency_overrides[deps.get_session_factory] = lambda: FakeSession
    app.dependency_overrides[deps.get_optional_session] = lambda: {
        "uid": "u-1",
        "tid": "t-1",
        "locale": "en",
    }
    try:
        resp = TestClient(app, follow_redirects=False).get(
            f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"
        )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    return resp.text


def test_cabinet_card_marks_dup(monkeypatch):
    html = _detail(monkeypatch, canonical_confidence="dup", also_on_n=2)
    # Session with locale=en → the cabinet language is en.
    assert "🔁 The same tender is also on other platforms: 2" in html


def test_cabinet_card_without_cluster_has_no_mark(monkeypatch):
    html = _detail(monkeypatch)
    assert "data-also-on" not in html
