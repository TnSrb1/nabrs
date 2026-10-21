"""The translation of the card goes into the interface LANGUAGE of the client, not always into Russian.

The defect (found by the owner on the live site 25.07): on the English page “My matches” the button offered “Show translation (ru)” and pulled the fragment with a hardcoded ``lang=ru`` — an English-speaking client got a Russian translation of a Serbian tender. The captions in the catalogs also lied: in the en branch there stood “Machine translation of the title into Russian”.

The owner’s decision: the translation language = the interface language (``users.locale``). On the Serbian interface there is no button at all — the original of the tender is already Serbian, there is nothing to translate into.

‼️ The separation of the axes is kept (the project rules): ``?lang=`` on the content routes still does NOT repaint the chrome (the route sets ``cabinet_ignore_query_lang``), it only says into which language to translate the CONTENT.
"""

from __future__ import annotations

import re

import pytest

from src.api.cabinet.i18n import CABINET
from tests.api.test_card_title_fragment import _FRAG, SESS, _client, _setup

_TOGGLE_RE = re.compile(r'hx-get="([^"]*/title[^"]*)"')


def _render(monkeypatch, locale: str) -> str:
    """The heading fragment under a session with the given interface language."""
    _setup(monkeypatch)
    sess = {**SESS, "locale": locale}
    resp = _client(sess=sess).get(_FRAG)
    assert resp.status_code == 200
    return resp.text


def test_catalog_labels_do_not_promise_russian():
    """The captions do not name the language — it equals the interface language.

    Mutation: return “Show translation (ru)” / “into Russian” → the test goes red.
    """
    for lang in ("sr", "ru", "en"):
        for key in ("ct_show_translation", "ct_translate_title", "td_translate_title"):
            value = CABINET[lang][key].lower()
            assert "(ru)" not in value, (lang, key, value)
            assert "русск" not in value, (lang, key, value)
            assert "ruski" not in value, (lang, key, value)
            assert "russian" not in value, (lang, key, value)


@pytest.mark.parametrize("locale", ["ru", "en"])
def test_toggle_requests_interface_language(monkeypatch, locale):
    """The translation button pulls the fragment with the interface language, not with ru.

    Mutation: return `lang=ru` in the template → on en the test goes red.
    """
    html = _render(monkeypatch, locale)
    urls = _TOGGLE_RE.findall(html)
    assert urls, "the translation toggle is not found"
    assert any(f"lang={locale}" in u for u in urls), urls
    if locale != "ru":
        assert not any("lang=ru" in u for u in urls), urls


def test_serbian_interface_has_no_translate_button(monkeypatch):
    """On the sr interface there is no translation button: the original of the tender is already Serbian.

    Mutation: remove the condition `lang != original_lang` → the button will appear, the test goes red.
    """
    html = _render(monkeypatch, "sr")
    assert CABINET["sr"]["ct_show_translation"] not in html
    assert not _TOGGLE_RE.findall(html), "on sr there must be no translation toggle"
