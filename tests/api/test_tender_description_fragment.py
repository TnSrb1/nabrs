"""The background fragment of the tender description — ``GET /app/tenders/{id}/description`` (VG-26).

DB/session/translation are mocked (ADR-013), the template renders for real. The chunking logic is not duplicated (tests/ai/test_translation.py) — here exactly the contour of the fragment:

- ``lang=sr`` gives the original and does NOT call the model;
- ``lang=ru`` calls ``translate_description`` with the text FROM THE SERVER, substitutes the translation + the “machine” mark;
- a partial refusal (translate_description → None) → the ORIGINAL IN FULL, not a mix and not a 500;
- the gate ``get_tender_analyses`` (someone else’s/broken tender/profile) → 404 BEFORE the translation;
- an anonymous user via HTMX → 401 + HX-Redirect.
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
_DESC_RU = "Предмет закупки — поставка компьютерного оборудования."

_REDIS = object()  # marker: the route must pass the cache client into the translation
_FRAG = f"/app/tenders/{_TENDER}/description?profile_id={_PROFILE}"


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


class _DescSpy:
    """Substitutes translate_description: counts the calls, remembers the arguments.

    It returns a PAIR ``(translation, usage)`` — the contract of translate_description after the accounting was introduced (``TranslationUsage``); the gate/charge are checked separately in tests/api/test_translation_gates.py.
    """

    def __init__(self, result, usage=TranslationUsage(calls=1, cost_usd=0.002)):
        self.result = result
        self.usage = usage
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result, self.usage


_DEFAULT = object()  # “data not set” ≠ “data=None” (None = the gate refused)


def _setup(monkeypatch, *, result=_DESC_RU, data=_DEFAULT):
    monkeypatch.setattr(
        routes_tender_ai.svc,
        "get_tender_analyses",
        _async(_data() if data is _DEFAULT else data),
    )
    spy = _DescSpy(result)
    monkeypatch.setattr(routes_tender_ai.translation, "translate_description", spy)
    return spy


# --------------------------------------------------------------------------- #
#  Original (lang=sr) — without the model
# --------------------------------------------------------------------------- #


def test_lang_sr_returns_original_without_calling_model(monkeypatch):
    spy = _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&lang=sr")

    assert resp.status_code == 200
    assert _DESC_SR in resp.text
    assert _DESC_RU not in resp.text
    assert spy.calls == [], "showing the original must not call the model"
    assert "машинный" not in resp.text
    # The original offers the “Translate description” button (translation on click).
    assert "Перевести описание" in resp.text
    assert "lang=ru" in resp.text


def test_default_lang_is_original_and_does_not_call_model(monkeypatch):
    """‼️ The owner’s rule: without ?lang= the fragment gives the ORIGINAL and does not call the model."""
    spy = _setup(monkeypatch)
    resp = _client().get(_FRAG)

    assert resp.status_code == 200
    assert _DESC_SR in resp.text
    assert _DESC_RU not in resp.text
    assert spy.calls == [], "the default — the original, the model is not called"
    assert "Перевести описание" in resp.text


# --------------------------------------------------------------------------- #
#  Translation (lang=ru)
# --------------------------------------------------------------------------- #


def test_lang_ru_calls_translate_and_shows_translation(monkeypatch):
    spy = _setup(monkeypatch)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert _DESC_RU in resp.text
    assert _DESC_SR not in resp.text
    assert len(spy.calls) == 1
    assert "Перевод машинный" in resp.text  # the mark about the machine translation
    # The reverse toggle: “Show original” returns lang=sr (without the model).
    assert "Показать оригинал (sr)" in resp.text
    assert "lang=sr" in resp.text


def test_translated_text_comes_from_server_not_from_request(monkeypatch):
    """‼️ The text that goes into the model comes FROM THE DB, not from the query string of the client."""
    spy = _setup(monkeypatch)
    _client().get(f"{_FRAG}&lang=ru&description=ПОДСУНУТО")

    call = spy.calls[0]
    assert call["description"] == _DESC_SR
    assert call["tender_id"] == _TENDER
    assert call["redis"] is _REDIS
    assert "ПОДСУНУТО" not in str(call)


def test_partial_failure_falls_back_to_whole_original_not_mix(monkeypatch):
    """‼️ A partial refusal (translate_description → None) → the original IN FULL."""
    _setup(monkeypatch, result=None)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 200
    assert _DESC_SR in resp.text  # the original in full
    assert (
        "Перевод машинный" not in resp.text
    )  # this is NOT a translation — there is no mark
    assert "недоступен" in resp.text  # a quiet mark that the original is shown


# --------------------------------------------------------------------------- #
#  The access gate is the same as on the detail page
# --------------------------------------------------------------------------- #


def test_foreign_or_missing_tender_404_and_never_translates(monkeypatch):
    """‼️ The gate BEFORE the translation: someone else’s tender is neither translated nor served from the cache."""
    spy = _setup(monkeypatch, data=None)
    resp = _client().get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 404
    assert spy.calls == []


def test_tender_without_description_does_not_500(monkeypatch):
    """A direct visit to the fragment of a tender without a description — 200, not 500.

    The detail does not draw a skeleton if there is no description, but the endpoint must survive a direct request (None.split() would break the template).
    """
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
    _setup(monkeypatch, result=None, data=data)
    resp = _client().get(f"{_FRAG}&lang=ru")
    assert resp.status_code == 200


def test_missing_profile_id_404(monkeypatch):
    _setup(monkeypatch)
    resp = _client().get(f"/app/tenders/{_TENDER}/description?lang=ru")

    assert resp.status_code == 404


def test_anonymous_gets_401_hx_redirect(monkeypatch):
    _setup(monkeypatch)
    resp = _client(sess=None).get(f"{_FRAG}&lang=ru")

    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"
