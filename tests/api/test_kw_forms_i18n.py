"""`_kw_forms.html` — the captions of the keyword form previews from the catalog, three languages."""

import re

import pytest

from src.api.cabinet.i18n import cabinet_context_for_lang
from src.api.templating import templates

_FORMS = [{"term": "škola", "forms": ["škola", "škole", "školi", "školu", "školom"]}]


def _render(lang, enabled=True, forms=_FORMS):
    return templates.get_template("_kw_forms.html").render(
        **cabinet_context_for_lang(lang),
        include_forms=forms,
        exclude_forms=[{"term": "x", "forms": []}],
        keyword_dict_enabled=enabled,
    )


@pytest.mark.parametrize(
    "lang,intro,title,more,none",
    [
        ("sr", "Tenderi su na srpskom", "Ključne reči", "još 1", "nema oblika"),
        ("ru", "Тендеры на сербском", "Ключевые слова", "ещё 1", "форм нет"),
        ("en", "Tenders are in Serbian", "Keywords", "1 more", "no forms"),
    ],
)
def test_kw_forms_labels_follow_language(lang, intro, title, more, none):
    html = _render(lang)
    assert intro in html and title in html and more in html and none in html


def test_kw_forms_serbian_has_no_cyrillic():
    for enabled in (True, False):
        assert not re.search("[А-Яа-яЁё]", _render("sr", enabled))


def test_kw_forms_disabled_expansion_literal():
    assert "proširenje je isključeno" in _render("sr", enabled=False)
