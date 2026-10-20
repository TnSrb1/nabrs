"""Tests of the language layer of the tender card (owner decision, ``routes_tender_ai``).

Content “as is”: the detail OPENS on the original, and the model on the render is NOT called at all (neither the heading nor the description). The translation — only by a button:

- the heading — by the toggle ``_card_title.html`` (view=“detail”): the original + the “Show translation” button, the translation itself — in the fragment ``GET .../title`` (the contour in tests/api/test_card_title_fragment.py);
- the description — the original inline + the “Translate description” button, the translation itself — in the fragment ``GET .../description`` (tests/api/test_tender_description_fragment.py);
- on the default render ``translate_tender``/``translate_description`` are NOT called (assert_not_called) — the model is expensive and the owner’s rule forbids it directly;
- the original in the DB is not overwritten by the translation.

DB/session/translation are mocked (ADR-013), the template renders for real.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.ai.translation import TranslationUsage
from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"

_TITLE_SR = "Набавка рачунара"
_DESC_SR = "Предмет јавне набавке је испорука рачунарске опреме."
_TITLE_RU = "Закупка компьютеров"
_DESC_RU = "Предмет закупки — поставка компьютерного оборудования."

_REDIS = object()  # marker: the route must pass the cache client into the translation


def _data():
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs={},
        description=_DESC_SR,
    )


def _client(*, sess=SESS):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    app.dependency_overrides[deps.get_redis] = lambda: _REDIS
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


class _Spy:
    """Counts the translation calls: on the default render there must be none.

    It returns a PAIR ``(None, zero usage)`` — the contract of the translators after the accounting was introduced (no translation → and no money).
    """

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return None, TranslationUsage()


def _setup(monkeypatch, *, data=None):
    monkeypatch.setattr(
        routes_tender_ai.svc, "get_tender_analyses", _async(data or _data())
    )
    monkeypatch.setattr(routes_tender_ai.cust, "get_narucilac_analytics", _async(None))
    title_spy, desc_spy = _Spy(), _Spy()
    monkeypatch.setattr(routes_tender_ai.translation, "translate_tender", title_spy)
    monkeypatch.setattr(routes_tender_ai.translation, "translate_description", desc_spy)
    return title_spy, desc_spy


# --------------------------------------------------------------------------- #
#  Content “as is”: the default render does NOT call the model
# --------------------------------------------------------------------------- #


def test_default_render_shows_original_and_never_calls_the_model(monkeypatch):
    """‼️ The owner’s rule: the card opens on the original, the model is silent."""
    title_spy, desc_spy = _setup(monkeypatch)
    resp = _client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert _TITLE_SR in resp.text  # the heading as is
    assert _DESC_SR in resp.text  # the description as is, inline
    assert _TITLE_RU not in resp.text and _DESC_RU not in resp.text
    # Neither the heading nor the description went to the model (assert_not_called).
    assert title_spy.calls == [], "the heading must not be translated on the render"
    assert desc_spy.calls == [], "the description must not be translated on the render"


def test_default_render_offers_translate_buttons_not_page_reload(monkeypatch):
    """The translation — by a button (an htmx toggle), not by a reload with ?lang=."""
    _setup(monkeypatch)
    resp = _client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    # The heading: the _card_title toggle (view=detail) — the original + the translation button.
    assert "Показать перевод" in resp.text
    assert f"/app/tenders/{_TENDER}/title?profile_id={_PROFILE}" in resp.text
    assert "view=detail" in resp.text
    # The description: the “Translate description” button → the fragment lang=ru into the same container.
    assert "Перевести описание" in resp.text
    assert f"/app/tenders/{_TENDER}/description?profile_id={_PROFILE}" in resp.text
    assert 'hx-target="#tender-description-body"' in resp.text
    # There is NO translation skeleton on loading — only after the button is pressed.
    assert "Переводим описание" not in resp.text
    # The old mechanism of reloading the page by ?lang= — there is none either.
    assert 'hx-trigger="load"' not in resp.text
    assert f"/app/tenders/{_TENDER}?profile_id={_PROFILE}&amp;lang=" not in resp.text


def test_no_lang_query_param_still_no_model_call(monkeypatch):
    """Even an explicit ?lang=ru on the detail translates nothing: the translation goes through the fragments."""
    title_spy, desc_spy = _setup(monkeypatch)
    resp = _client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}&lang=ru")

    assert resp.status_code == 200
    assert title_spy.calls == [] and desc_spy.calls == []


# --------------------------------------------------------------------------- #
#  The rest of the card does not depend on the translation
# --------------------------------------------------------------------------- #


def test_rest_of_card_renders(monkeypatch):
    """The sections of artifacts/analytics are independent of the language layer."""
    data = TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs={"bid": {"verdict": "go", "score": 80, "pros": ["профильно"]}},
        description=_DESC_SR,
    )
    _setup(monkeypatch, data=data)
    resp = _client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert "профильно" in resp.text
    assert "Општина Нови Сад" in resp.text


def test_tender_without_description_renders(monkeypatch):
    """An empty description — not a reason to draw an empty section."""
    data = TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name=None,
        deadline_at=None,
        source_url=None,
        outputs={},
        description=None,
    )
    _setup(monkeypatch, data=data)
    resp = _client().get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert "📄 Описание закупки" not in resp.text
    # The heading is still available as is.
    assert _TITLE_SR in resp.text


# --------------------------------------------------------------------------- #
#  The original in the DB is not touched
# --------------------------------------------------------------------------- #


def test_translation_never_writes_to_tenders_normalized():
    """The translation is a layer ON TOP: the read-only card service stays read-only.

    A guard against the “optimisation” of the kind “let’s save the translation into tenders_normalized” — it would overwrite the original on which the matching works, and the “Show original” button would have nothing to show.
    """
    from pathlib import Path

    for module in ("tender_ai_service.py", "routes_tender_ai.py"):
        src = Path("src/api/cabinet") / module
        text = src.read_text(encoding="utf-8").upper()
        assert "UPDATE TENDERS_NORMALIZED" not in text
        assert "INSERT INTO TENDERS_NORMALIZED" not in text

    tr_src = Path("src/ai/translation.py").read_text(encoding="utf-8").upper()
    assert "UPDATE" not in tr_src and "INSERT" not in tr_src
