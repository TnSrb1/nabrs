"""One showcase by sectors + the contents of a template (owner complaint 01.10.2026).

1. “Templates all over the place”: /library and the first screen /profiles/new lay out
   packages and templates by sector (``library_sectors``) in ONE order for ru and
   sr — the order is set by a dictionary, not by sorting the translated headings.
2. “Cannot look at the contents”: /library/{package|template}/{key} — all the words,
   CPV with names, the package’s stop words, tender examples.
3. “Cannot make your own based on it”: the buttons lead to the existing prefill.

Expectations — literals from the dictionary/directory, not via the same ``t()``.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from src.api.cabinet import library_counts, preview_service, routes_library
from src.api.cabinet import vertical_packages
from tests.api.test_cabinet_i18n import _TID, _UID, _async, _client

SESS = {"uid": _UID, "tid": _TID}
_SECTION_RE = re.compile(r'<section class="tpl-group" id="sector-([a-z]+)"')
_TPL_LINK_RE = re.compile(r'href="/library/template/00000016-000([0-9a-f])-')

# The order of sectors — a literal from library_sectors.SECTORS (+ “other” at the end).
EXPECTED_ORDER = [
    "it",
    "construction",
    "medical",
    "supplies",
    "energy",
    "cleaning",
    "security",
    "other",
]
# The numbers of the SEED templates in the showcase order: IT(1), construction(6), medicine(5),
# supplies — stationery(3), transport(7), energy(8), other(9).
# Templates 4 (“Čišćenje” = package “Čišćenje”) and 2 (“Obezbeđenje” = “Obezbeđenje”)
# — duplicates of packages, hidden.
EXPECTED_TEMPLATES = ["1", "6", "5", "3", "7", "8", "9"]


def _tpl(n, kind, sr_kind, name, sr_name):
    return SimpleNamespace(
        id=f"00000016-000{n:x}-0000-0000-000000000001",
        name=name,
        description=None,
        activity_kind=kind,
        keywords_include=[{"all_of": ["uborka"]}],
        classifications={"include": [{"system_code": "eu_cpv", "code": "45000000"}]},
        i18n={"sr": {"name": sr_name, "activity_kind": sr_kind}},
    )


# One template per sector of activity of seed 0016 (+ a user one without
# a sector). The list is DELIBERATELY in alphabetical order of the Russian column — as the route gives it
# list_public_templates (ORDER BY activity_kind, name).
SEED = [
    _tpl(1, "IT и технологии", "ИТ и технологије", "IT-услуги", "ИТ услуге"),
    _tpl(2, "Безопасность", "Безбедност", "Охрана", "Обезбеђење"),
    _tpl(3, "Канцелярия и расходники", "Канцеларија", "Канцелярия", "Канцеларија"),
    _tpl(4, "Клининг и уборка", "Чишћење", "Клининг", "Чишћење"),
    _tpl(5, "Медицина и фармацевтика", "Медицина", "Лекарства", "Лекови"),
    _tpl(6, "Строительство", "Грађевина", "Дороги", "Путеви"),
    _tpl(7, "Транспорт", "Транспорт", "Транспорт", "Возила"),
    _tpl(8, "Электрика", "Електро", "Электрооборудование", "Електроопрема"),
    _tpl(9, None, None, "Мой шаблон", "Мој шаблон"),
]


def _sections(html):
    return _SECTION_RE.findall(html)


@pytest.mark.parametrize("path", ["/library", "/profiles/new"])
def test_sector_order_is_same_for_ru_and_sr(monkeypatch, path):
    """The order of sectors — from the dictionary, the same in ru and sr.

    Mutation: sort the sectors by the translated heading (or return the grouping by localized activity_kind) — sr/ru will diverge from the literal.
    """
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async(SEED))
    for locale in ("ru", "sr"):
        resp = _client(sess=SESS, locale=locale).get(path)
        assert resp.status_code == 200
        assert _sections(resp.text) == EXPECTED_ORDER, locale
        # And the order of the template cards: by sector by the SOURCE activity_kind, with
        # equal numbers — by id (the template number from SEED).
        ids = _TPL_LINK_RE.findall(resp.text)
        assert ids == EXPECTED_TEMPLATES, locale


def test_package_first_then_templates_by_count(monkeypatch):
    """In a sector: the “whole sector” package first, then the templates by descending “≈N”."""
    small = _tpl(1, "Строительство", "Грађевина", "Сантехника", "Водовод")
    big = _tpl(2, "Строительство", "Грађевина", "Здания", "Зграде")
    monkeypatch.setattr(
        routes_library.svc, "list_public_templates", _async([small, big])
    )
    n_pkg = len(vertical_packages.PACKAGES)
    counts = [100] + [10] * n_pkg + [3, 34]  # small=3, big=34

    async def _counts(db, payloads):
        assert len(payloads) == len(counts)
        return counts

    monkeypatch.setattr(library_counts, "counts_for", _counts)
    html = _client(sess=SESS, locale="sr").get("/library").text
    section = html[html.index('id="sector-construction"') :]
    section = section[: section.index("</section>")]
    titles = re.findall(r'class="tpl-card__title">([^<]*)<', section)
    assert titles == ["Građevina", "Zgrade", "Vodovod"]


def test_card_links_to_contents_page(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async(SEED))
    html = _client(sess=SESS, locale="ru").get("/library").text
    assert 'href="/library/template/00000016-0006-0000-0000-000000000001"' in html
    assert 'href="/library/package/gradjevina"' in html


def test_search_filters_cards(monkeypatch):
    """?q= folds the same way as search: Latin finds Cyrillic."""
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async(SEED))
    html = _client(sess=SESS, locale="sr").get("/library?q=putevi").text
    assert _sections(html) == ["construction"]
    assert "Putevi" in html
    assert "Lekovi" not in html


def test_howto_open_for_new_and_closed_for_returning(monkeypatch):
    monkeypatch.setattr(routes_library.svc, "list_public_templates", _async([]))
    html = _client(sess=SESS, locale="ru").get("/library").text
    assert '<details class="tpl-howto" open>' in html
    assert "Как работают шаблоны" in html

    monkeypatch.setattr(
        routes_library.profile_service, "list_profiles", _async([object()])
    )
    html = _client(sess=SESS, locale="ru").get("/library").text
    assert '<details class="tpl-howto">' in html


# --------------------------------------------------------------------------- #
# Template / package contents
# --------------------------------------------------------------------------- #


def _preview(count=12):
    async def _stub(db, payload, window_days=30, examples_limit=0):
        assert examples_limit == 3
        ex = [
            preview_service.PreviewExample(
                id=f"t-{i}",
                title_parts=[(f"Primer tendera {i}", False)],
                customer_name=None,
                deadline_at=None,
            )
            for i in range(examples_limit)
        ]
        return preview_service.PreviewResult(count=count, unfound=[], examples=ex)

    return _stub


def test_package_contents_show_cpv_and_stop_words(monkeypatch):
    monkeypatch.setattr(routes_library.preview_service, "preview_count", _preview())
    resp = _client(sess=SESS, locale="ru").get("/library/package/remote")
    assert resp.status_code == 200
    html = resp.text
    # CPV — by code and name (ru from cpv_codes.json).
    assert "72000000" in html
    assert "Услуги информационных технологий" in html
    # Package stop words (vertical_packages.remote.keywords_exclude).
    assert '<span class="tpl-tag tpl-tag--stop">remont</span>' in html
    assert '<span class="tpl-tag tpl-tag--stop">nabavka dobara</span>' in html
    # Region and examples of tenders.
    assert "вся Сербия" in html
    assert html.count('href="/tender/t-') == 3
    # “Based on” → the full form with prefill, “as is” → confirmation.
    assert '<form method="get" action="/profiles/new"' in html
    assert '<input type="hidden" name="from_package" value="remote">' in html
    assert 'formaction="/profiles/new/confirm"' in html
    assert "оригинал в библиотеке не изменится" in html.lower()


def test_template_contents_show_cpv_label_in_interface_language(monkeypatch):
    tpl = SEED[5]
    monkeypatch.setattr(routes_library.svc, "get_template", _async(tpl))
    monkeypatch.setattr(routes_library.preview_service, "preview_count", _preview())
    html = _client(sess=SESS, locale="sr").get(f"/library/template/{tpl.id}").text
    assert "Putevi" in html
    assert "45000000" in html
    assert "Građevinski radovi" in html  # the directory’s l_sr, in Latin
    assert '<form method="get" action="/profiles/new"' in html
    assert f'<input type="hidden" name="from_template" value="{tpl.id}">' in html
    assert 'formaction="/profiles/new/confirm"' in html
    # template_library templates have no stop words — stated explicitly.
    assert "tpl-tag--stop" not in html


def test_contents_survive_preview_failure(monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(routes_library.preview_service, "preview_count", _boom)
    resp = _client(sess=SESS, locale="ru").get("/library/package/it")
    assert resp.status_code == 200
    assert "tpl-card__count" not in resp.text


def test_contents_limit_reached_routes_to_account(monkeypatch):
    monkeypatch.setattr(routes_library.preview_service, "preview_count", _preview())
    monkeypatch.setattr(routes_library.tiers, "profile_limit", lambda tier: 1)
    monkeypatch.setattr(
        routes_library.profile_service, "list_profiles", _async([object()])
    )
    html = _client(sess=SESS, locale="ru").get("/library/package/it").text
    assert "from_package=it" not in html
    assert 'href="/cabinet/account"' in html


@pytest.mark.parametrize(
    "path", ["/library/package/nope", "/library/template/nope", "/library/x/it"]
)
def test_contents_unknown_is_404(monkeypatch, path):
    monkeypatch.setattr(routes_library.svc, "get_template", _async(None))
    assert _client(sess=SESS, locale="ru").get(path).status_code == 404


def test_contents_anonymous_redirects_to_login():
    resp = _client(sess=None).get("/library/package/it")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
