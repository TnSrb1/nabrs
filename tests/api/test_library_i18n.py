"""The template library showcase speaks the interface language (0036).

The defect these tests close: the names, descriptions and groups of 16 public templates lived only in Russian (seed 0016), so a Serbian client opened the onboarding showcase in someone else’s language — a render measurement of ``/library`` under the session ``locale=sr`` gave 34 Russian letters even AFTER the localisation of the packages (#443).

The metric is the same as in the rest of the i18n contour: the Russian-specific letters ``U+044B U+044D U+0449 U+044A U+0451 U+0439`` in the RENDER (not in the template) — Serbian Cyrillic does not contain them.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.api.cabinet import routes_library
from src.api.cabinet import template_library_service as svc
from tests.api.test_cabinet_i18n import _TID, _UID, _async, _client

_RU_SPECIFIC = re.compile(r"[ыэщъёЫЭЩЪЁ]")
_CARD_TEXT_RE = re.compile(r'class="tpl-card__(?:title|desc)">([^<]*)<')
_GROUP_TITLE_RE = re.compile(r'class="tpl-group__title">([^<]*)<')
_TAG_RE = re.compile(r'class="tpl-tag">([^<]*)<')


def _tpl_with_i18n():
    """Public seed template: Russian columns + translations in i18n (as after 0036)."""
    return SimpleNamespace(
        id="00000016-000f-0000-0000-000000000001",
        name="Клининг и уборка помещений",
        description="Услуги уборки, клининг объектов",
        activity_kind="Клининг и уборка",
        keywords_include=[{"all_of": ["уборка"]}],
        classifications={"include": [{"system_code": "eu_cpv", "code": "90910000"}]},
        i18n={
            "sr": {
                "name": "Чишћење и одржавање хигијене",
                "description": "Услуге чишћења, одржавање објеката",
                "activity_kind": "Чишћење и одржавање",
            },
            "en": {
                "name": "Premises cleaning and upkeep",
                "description": "Cleaning services, facility maintenance",
                "activity_kind": "Cleaning",
            },
        },
    )


def _tpl_without_i18n():
    """A user template (EPIC 5): there are no translations — we show the original."""
    return SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        name="Мой личный шаблон",
        description="Описание от пользователя",
        activity_kind="Моя категория",
        keywords_include=[{"all_of": ["щебень"]}],
        classifications=None,
        i18n=None,
    )


@pytest.mark.parametrize("locale", ["sr", "en"])
def test_seeded_templates_render_without_russian(monkeypatch, locale):
    """Cards and group headings of the seed — in the interface language, without Russian letters.

    Mutation: remove the reading of ``i18n`` in ``localized_view`` (return ``tpl.name``)
    → the render again shows “Cleaning and premises janitorial”, the test goes red.
    """
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([_tpl_with_i18n()])
    )
    resp = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/library")
    assert resp.status_code == 200

    cards = _CARD_TEXT_RE.findall(resp.text)
    groups = _GROUP_TITLE_RE.findall(resp.text)
    assert cards, "template cards not found in the render"
    assert groups, "group headings not found in the render"

    leaks = [t for t in cards + groups if _RU_SPECIFIC.search(t)]
    assert not leaks, f"Russian text of the template showcase in {locale}: {leaks}"


def test_ru_locale_keeps_original_texts(monkeypatch):
    """On ru the showcase stays as before — the translations do not replace the source columns."""
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([_tpl_with_i18n()])
    )
    html = _client(sess={"uid": _UID, "tid": _TID}, locale="ru").get("/library").text
    assert "Клининг и уборка помещений" in html
    assert "Чишћење и одржавање хигијене" not in html


@pytest.mark.parametrize("locale", ["sr", "en"])
def test_user_template_without_translation_shows_original(monkeypatch, locale):
    """A template without ``i18n`` (the text was written by a person) is served as is — fail-open."""
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([_tpl_without_i18n()])
    )
    html = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/library").text
    # sr — Latin (owner 10.10.2026), en — as is
    assert ("Moj ličnyj šablon" if locale == "sr" else "Мой личный шаблон") in html


@pytest.mark.parametrize("locale", ["sr", "en"])
def test_tags_show_search_forms_not_russian_terms(monkeypatch, locale):
    """The tags are what matching REALLY searches by, not the Russian caption.

    “cleaning” is searched by its Serbian forms (`čišćenje`…), and those are what we show — otherwise the showcase lies about how search works. Mutation: return the source terms into ``_localized_keywords`` → the tags show “cleaning” again, the test goes red.
    """
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([_tpl_with_i18n()])
    )
    html = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/library").text
    tags = _TAG_RE.findall(html)
    assert tags, "keyword tags not found"
    assert "уборка" not in tags
    assert any("čišćenj" in t.lower() for t in tags), tags


@pytest.mark.parametrize("locale", ["sr", "en"])
def test_tags_are_deduplicated(monkeypatch, locale):
    """Different Russian terms give one Serbian form — the tag must not be duplicated.

    “cleaning” / “janitorial” / “cleaning” → all “čišćenje”: without dedup the showcase showed “čišćenje čišćenje čišćenje” and read like a layout bug (found by eye on production after 0036). Mutation: remove the dedup → the tag repeats, the test goes red.
    """
    tpl = _tpl_with_i18n()
    tpl.keywords_include = [
        {"all_of": ["уборка"]},
        {"all_of": ["клининг"]},
        {"all_of": ["чистка"]},
    ]
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([tpl]))
    html = _client(sess={"uid": _UID, "tid": _TID}, locale=locale).get("/library").text
    # Uniqueness is checked INSIDE a card: the same tag on different
    # cards (the package “Čišćenje” and the cleaning template) is the norm, not a duplicate.
    cards = re.findall(r'<article class="tpl-card">.*?</article>', html, flags=re.S)
    assert cards, "cards not found"
    for card in cards:
        tags = _TAG_RE.findall(card)
        assert len(tags) == len(set(tags)), f"duplicate tags in the card: {tags}"


def test_package_tags_are_deduplicated():
    """The same for vertical packages (their tags are prepared by display_packages)."""
    from src.api.cabinet import vertical_packages as vp

    for pkg in vp.display_packages("sr"):
        assert len(pkg["keywords"]) == len(
            set(pkg["keywords"])
        ), f"duplicate tags of the package {pkg['slug']}: {pkg['keywords']}"


def test_localized_view_falls_back_per_field():
    """Partial translation: what is missing in ``i18n`` is taken from the source column."""
    tpl = _tpl_with_i18n()
    tpl.i18n = {"sr": {"name": "Само назив"}}
    view = svc.localized_view(tpl, "sr")
    assert view["name"] == "Samo naziv"
    assert view["description"] == "Услуги уборки, клининг объектов"
    assert view["activity_kind"] == "Клининг и уборка"


def test_sr_template_texts_are_latin(monkeypatch):
    """sr interface — all in Latin: the translation 0036 (Cyrillic) renders as “Čišćenje…”.

    The expectation — a literal from the fact (owner screenshot 01.10.2026: “Računarska oprema” next to the Latin packages). Mutation: remove ``_sr_latin`` in ``localized_view`` → the render again shows “Čišćenje…”, the test goes red.
    """
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([_tpl_with_i18n()])
    )
    html = _client(sess={"uid": _UID, "tid": _TID}, locale="sr").get("/library").text
    assert "Čišćenje i održavanje higijene" in html
    assert "Usluge čišćenja, održavanje objekata" in html
    assert "Чишћење" not in html
    # The ru interface in Cyrillic is not Latinised.
    ru = _client(sess={"uid": _UID, "tid": _TID}, locale="ru").get("/library").text
    assert "Čišćenje i održavanje higijene" not in ru


def test_sr_russian_fallback_not_transliterated():
    """A template without an sr translation gives the Russian original, not “Moj ličnyj šablon”."""
    view = svc.localized_view(_tpl_without_i18n(), "sr")
    assert view["name"] == "Мой личный шаблон"


def _seed_tpl(tid, name_ru, name_sr, name_en):
    return SimpleNamespace(
        id=tid,
        name=name_ru,
        description="d",
        activity_kind="Медицина и фармацевтика",
        keywords_include=[{"all_of": ["opрема"]}],
        classifications=None,
        i18n={
            "sr": {"name": name_sr, "description": "d", "activity_kind": "Медицина"},
            "en": {"name": name_en, "description": "d", "activity_kind": "Medicine"},
        },
    )


def test_template_duplicating_package_is_hidden(monkeypatch):
    """“Medicinska oprema” (package) and “Medicinska oprema” (template) — the same: the template is hidden.

    Mutation: remove the ``duplicates_of_packages`` filter in ``showcase`` → the template is drawn again as a second card, the test goes red.
    """
    dup = _seed_tpl(
        "00000016-0008-0000-0000-000000000001",
        "Медицинское оборудование",
        "Медицинска опрема",
        "Medical equipment",
    )
    keep = _seed_tpl(
        "00000016-0009-0000-0000-000000000001",
        "Лекарства и фармацевтика",
        "Лекови и фармација",
        "Medicines and pharmaceuticals",
    )
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([dup, keep])
    )
    html = _client(sess={"uid": _UID, "tid": _TID}, locale="sr").get("/library").text
    cards = _CARD_TEXT_RE.findall(html)
    assert "Medicinska oprema" in cards  # package
    assert "Lekovi i farmacija" in cards  # another template remained
    assert "Medicinska oprema" == next(c for c in cards if "edicinsk" in c)
    assert cards.count("Medicinska oprema") == 1
    assert not any("опрема" in c for c in cards)


def test_package_tags_are_words_not_stems(monkeypatch):
    """Package tags are words, not search stems (“gradjevin”, “izgradnj”, “oprem”)."""
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    html = _client(sess={"uid": _UID, "tid": _TID}, locale="sr").get("/library").text
    tags = _TAG_RE.findall(html)
    for word in ("građevina", "izgradnja", "medicinska oprema", "snabdevanje"):
        assert any(word in t for t in tags) or word in html, word
    for stem in ("građevin<", "izgradnj<", "medicinsk<", "oprem<"):
        assert f">{stem}" not in html.replace("</span>", "<"), stem
