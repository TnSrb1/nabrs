"""The “Translate” button translates into the INTERFACE LANGUAGE, not always into Russian (owner).

Owner requirement (23.07): when the site language is chosen, the translation of the tender body by the button goes into THIS language — symmetrically for sr/ru/en. Russian stops being privileged. The “Translate” button is not rendered exactly when the interface language matches the language of the original tender (sr→sr — there is nothing to translate).

DB/session/translation are mocked (ADR-013), templates render for real. The interface language is set via ``session[“locale”]`` (it is put by ``deps.resolve_cabinet_locale`` into ``request.state.cabinet_user_locale``). The logic of the translation itself is not duplicated (tests/ai/test_translation.py) — here exactly the contour “target = the interface language”:

- interface ``en`` → the button pulls the fragment with ``lang=en`` and ``en`` went into the model;
- interface ``ru`` → target ``ru`` (a regression of the former behavior);
- interface ``sr`` → there is no translation button, the model is not called even once;
- an unknown ``lang`` in the request → the original, the model is not called.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.ai.translation import TenderTranslation, TranslationUsage
from src.api import deps
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeSession

_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"
_SOURCE_URL = "https://portal.rs/t/1"

_TITLE_SR = "Набавка рачунара"
_DESC_SR = "Предмет јавне набавке је испорука рачунарске опреме."

_REDIS = object()
_TR = TenderTranslation(title="TRANSLATED-TITLE", description="")
_DESC_OUT = "TRANSLATED-DESCRIPTION"

_TITLE_FRAG = f"/app/tenders/{_TENDER}/title?profile_id={_PROFILE}"
_DESC_FRAG = f"/app/tenders/{_TENDER}/description?profile_id={_PROFILE}"


def _sess(locale: str) -> dict:
    """A session with the saved interface language (as after login/language change)."""
    return {"uid": "u-1", "tid": "t-1", "locale": locale}


def _data():
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title=_TITLE_SR,
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url=_SOURCE_URL,
        outputs={},
        description=_DESC_SR,
    )


def _client(*, sess):
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
    """Substitutes translate_tender/translate_description: counts the calls, catches the kwargs.

    It returns a PAIR ``(translation, usage)`` — the contract of both translators after the accounting was introduced (``TranslationUsage``).
    """

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result, TranslationUsage(calls=1, cost_usd=0.001)


def _setup_title(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data()))
    spy = _Spy(_TR)
    monkeypatch.setattr(routes_tender_ai.translation, "translate_tender", spy)
    return spy


def _setup_desc(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data()))
    spy = _Spy(_DESC_OUT)
    monkeypatch.setattr(routes_tender_ai.translation, "translate_description", spy)
    return spy


# --------------------------------------------------------------------------- #
#  Interface en → the translation target en (the main case of the defect)
# --------------------------------------------------------------------------- #


def test_en_interface_title_button_targets_en_and_model_gets_en(monkeypatch):
    """‼️ The owner's core: with the English interface the button leads to en and the model is called with en.

    Mutation: return a hardcoded ``&amp;lang=ru`` to the template — the href check falls; remove ``target_lang=lang`` in the route — the check ``target_lang == 'en'`` falls.
    """
    spy = _setup_title(monkeypatch)
    client = _client(sess=_sess("en"))

    # The original (without ?lang): the translation button must lead to the interface language — en.
    original = client.get(_TITLE_FRAG)
    assert original.status_code == 200
    assert _TITLE_SR in original.text
    assert "&amp;lang=en" in original.text, "the button must pull the translation into en"
    assert "&amp;lang=ru" not in original.text, "Russian is no longer privileged"
    assert spy.calls == [], "the model is not called on the original"

    # A click on the button (its own lang=en) → the target en goes into the model.
    translated = client.get(f"{_TITLE_FRAG}&lang=en")
    assert translated.status_code == 200
    assert len(spy.calls) == 1
    assert spy.calls[0]["target_lang"] == "en"
    assert "TRANSLATED-TITLE" in translated.text


def test_en_interface_description_button_targets_en_and_model_gets_en(monkeypatch):
    """The same for the body of the description: the target = the interface language (en)."""
    spy = _setup_desc(monkeypatch)
    client = _client(sess=_sess("en"))

    original = client.get(_DESC_FRAG)
    assert original.status_code == 200
    assert _DESC_SR in original.text
    assert "&amp;lang=en" in original.text
    assert "&amp;lang=ru" not in original.text
    assert spy.calls == []

    translated = client.get(f"{_DESC_FRAG}&lang=en")
    assert translated.status_code == 200
    assert spy.calls[0]["target_lang"] == "en"
    assert "TRANSLATED-DESCRIPTION" in translated.text


# --------------------------------------------------------------------------- #
#  Interface ru → target ru (a regression of the former behavior)
# --------------------------------------------------------------------------- #


def test_ru_interface_title_targets_ru(monkeypatch):
    """Regression: the Russian interface still translates into ru."""
    spy = _setup_title(monkeypatch)
    client = _client(sess=_sess("ru"))

    original = client.get(_TITLE_FRAG)
    assert "&amp;lang=ru" in original.text
    assert "&amp;lang=en" not in original.text

    client.get(f"{_TITLE_FRAG}&lang=ru")
    assert spy.calls[0]["target_lang"] == "ru"


def test_ru_interface_description_targets_ru(monkeypatch):
    spy = _setup_desc(monkeypatch)
    client = _client(sess=_sess("ru"))

    original = client.get(_DESC_FRAG)
    assert "&amp;lang=ru" in original.text

    client.get(f"{_DESC_FRAG}&lang=ru")
    assert spy.calls[0]["target_lang"] == "ru"


# --------------------------------------------------------------------------- #
#  Interface sr → there is no button, the model is not called even once
# --------------------------------------------------------------------------- #


def test_sr_interface_hides_title_button_and_never_calls_model(monkeypatch):
    """‼️ Symmetry: with the Serbian interface there is nothing to translate sr→sr — there is no button.

    We compare the interface language with the language of the original, rather than hard-coding “sr = a special case”: the rule will survive the appearance of non-Serbian sources.
    """
    spy = _setup_title(monkeypatch)
    client = _client(sess=_sess("sr"))

    resp = client.get(_TITLE_FRAG)
    assert resp.status_code == 200
    assert "Nabavka računara" in resp.text  # the sr interface is in Latin (owner 10.10)
    # Not a single one of the translate button captions (in all three languages) on the sr interface.
    assert "Prikaži prevod" not in resp.text
    assert "Показать перевод" not in resp.text
    assert "Show translation" not in resp.text
    # And no href to the translation — there is nothing to swap.
    assert "&amp;lang=" not in resp.text
    assert spy.calls == [], "sr→sr: the model is not called even once"


def test_sr_interface_hides_description_button_and_never_calls_model(monkeypatch):
    spy = _setup_desc(monkeypatch)
    client = _client(sess=_sess("sr"))

    resp = client.get(_DESC_FRAG)
    assert resp.status_code == 200
    assert "Predmet javne nabavke je isporuka računarske opreme." in resp.text
    assert "Prevedi opis" not in resp.text
    assert "Перевести описание" not in resp.text
    assert "Translate description" not in resp.text
    assert "&amp;lang=" not in resp.text
    assert spy.calls == []


# --------------------------------------------------------------------------- #
#  An unknown lang → the original, the model is not called
# --------------------------------------------------------------------------- #


def test_unknown_lang_returns_original_without_model_title(monkeypatch):
    """An unknown ?lang → the original (without the model), not a 500 and not “a translation somewhere”.

    With the en interface the translate button stays and leads back to en.
    """
    spy = _setup_title(monkeypatch)
    resp = _client(sess=_sess("en")).get(f"{_TITLE_FRAG}&lang=zz")

    assert resp.status_code == 200
    assert _TITLE_SR in resp.text
    assert "TRANSLATED-TITLE" not in resp.text
    assert spy.calls == [], "an unknown language does not call the model"
    assert "&amp;lang=en" in resp.text  # the button leads to the interface language


def test_unknown_lang_returns_original_without_model_description(monkeypatch):
    spy = _setup_desc(monkeypatch)
    resp = _client(sess=_sess("en")).get(f"{_DESC_FRAG}&lang=zz")

    assert resp.status_code == 200
    assert _DESC_SR in resp.text
    assert "TRANSLATED-DESCRIPTION" not in resp.text
    assert spy.calls == []
    assert "&amp;lang=en" in resp.text
