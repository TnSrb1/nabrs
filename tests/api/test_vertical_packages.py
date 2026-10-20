"""Tests of the catalog of the vertical packages (VG-14) and their output/prefill.

The catalog is data-as-code (src/api/cabinet/vertical_packages), without DB. We check:
the i18n parity of sr/ru/en, the correctness of the CPV form, the output for the showcase and
the pre-filling of the profile form through the same payload contract as the templates.
"""

from __future__ import annotations

from src.api.cabinet import vertical_packages as vp

# The required VG-14 verticals (one representative of each slug).
REQUIRED_SLUGS = {
    "gradjevina",
    "it",
    "medoprema",
    "obezbedjenje",
    "ciscenje",
    "elenergija",
}


def test_catalog_covers_required_verticals():
    slugs = {p.slug for p in vp.all_packages()}
    assert REQUIRED_SLUGS <= slugs
    assert len(vp.all_packages()) >= 6


def test_slugs_are_unique():
    slugs = [p.slug for p in vp.all_packages()]
    assert len(slugs) == len(set(slugs))


def test_i18n_parity_name_and_description():
    for p in vp.all_packages():
        assert set(p.name) == {"sr", "ru", "en"}, p.slug
        assert set(p.description) == {"sr", "ru", "en"}, p.slug
        assert all(v.strip() for v in p.name.values()), p.slug
        assert all(v.strip() for v in p.description.values()), p.slug


def test_every_package_has_keywords_and_cpv():
    for p in vp.all_packages():
        assert p.keywords_include, p.slug
        assert p.cpv, p.slug
        # keywords_include — OR-groups with a non-empty all_of (the profile/template form).
        for group in p.keywords_include:
            assert group.get("all_of"), p.slug
        # CPV — numeric codes (not invented alphabetic ones), 8 digits of EU CPV.
        for code in p.cpv:
            assert code.isdigit() and len(code) == 8, (p.slug, code)


def test_get_package_known_and_unknown():
    assert vp.get_package("it") is not None
    assert vp.get_package("does-not-exist") is None
    assert vp.get_package(None) is None
    assert vp.get_package("") is None


def test_display_packages_flattens_terms_and_cpv():
    items = vp.display_packages("ru")
    assert len(items) == len(vp.all_packages())
    it = next(i for i in items if i["slug"] == "it")
    assert it["name"] == "ИТ и технологии"
    assert it["description"]
    # a flat list of terms for the tags
    assert "программное обеспечение" in it["keywords"]
    assert all(isinstance(c, str) for c in it["cpv"])


def test_display_packages_lang_fallback_to_ru():
    # An unknown language → the ru fallback.
    items = vp.display_packages("de")
    it = next(i for i in items if i["slug"] == "it")
    assert it["name"] == "ИТ и технологии"


def test_to_profile_prefill_shape_matches_profile_contract():
    pkg = vp.get_package("gradjevina")
    prefill = vp.to_profile_prefill(pkg)
    assert prefill["name"] == "Строительство"
    # keywords_include — a list of OR-groups
    assert isinstance(prefill["keywords_include"], list)
    assert prefill["keywords_include"][0]["all_of"]
    # classifications — {include:[{system_code,code}], exclude:[]}
    cls = prefill["classifications"]
    assert cls["exclude"] == []
    assert cls["include"]
    for entry in cls["include"]:
        assert entry["system_code"] == "eu_cpv"
        assert entry["code"].isdigit()


def test_to_profile_prefill_copies_groups_defensively():
    pkg = vp.get_package("it")
    prefill = vp.to_profile_prefill(pkg)
    # a mutation of the result must not affect the immutable catalog
    prefill["keywords_include"][0]["all_of"].append("HACK")
    assert "HACK" not in pkg.keywords_include[0]["all_of"]


# --- VG-14 “Remote services” (slug=remote) -------------------------------
#
# Expectations — literals (not computed from the catalog itself), as required by
# .claude/rules/tests-known-defects.md: an assertion that mirrors the source
# is always green and proves nothing.


def test_remote_package_cpv_literal_no_cpv71():
    pkg = vp.get_package("remote")
    assert pkg is not None
    assert set(pkg.cpv) == {
        "72000000",
        "79000000",
        "73000000",
        "80000000",
        "92000000",
    }
    # 71 (design with supervision on site) is deliberately not included.
    assert not any(c.startswith("71") for c in pkg.cpv)


def test_remote_package_stopwords_literal():
    pkg = vp.get_package("remote")
    assert list(pkg.keywords_exclude) == [
        "remont",
        "rekonstrukcij",
        "održavanj",
        "popravk",
        "sanacij",
        "montaž",
        "ugradnj",
        "oprem",
        "uređaj",
        "server",
        "nabavka dobara",
        "nabavke dobara",
        "radov",
        "materijal",
    ]
    # “supply”/“maintenance” from the owner’s literal list are deliberately not
    # used — their sr forms (“nabavka”, “servis”) are too general
    # (see TESTING_vertical_package_remote.md §2).
    assert "поставка" not in pkg.keywords_exclude
    assert "обслуживание" not in pkg.keywords_exclude


def test_remote_package_keywords_include_literal():
    pkg = vp.get_package("remote")
    terms = {g["all_of"][0] for g in pkg.keywords_include}
    assert terms == {
        "razvoj",
        "izrad",
        "razvija",
        "softver",
        "program",
        "informacion",
        "obuk",
        "obrazovanj",
        "trening",
        "hosting",
        "oblak",
        "oblačn",
        "projekat",
        "projekt",
        "licenc",
    }


def test_remote_package_ai_extra_context_i18n_parity_and_content():
    pkg = vp.get_package("remote")
    assert pkg.ai_extra_context is not None
    assert set(pkg.ai_extra_context) == {"sr", "ru", "en"}
    assert all(v.strip() for v in pkg.ai_extra_context.values())
    # The AI analysis needs explicit markers of “what does not fit”, not only the positive.
    assert "поставка" in pkg.ai_extra_context["ru"]
    assert "монтаж" in pkg.ai_extra_context["ru"]


def test_remote_package_stopwords_cut_measured_waste():
    """Stop-words really cut “održavanje”/“radov” — the list is not empty.

    Regression on a measured fact (TESTING §2): without the right set of stop-words
    this test catches both the absence of the effect (empty keywords_exclude) and
    corruption of the set (“remont”/“radov” dropped from the list).
    """
    from src.domain.text_norm import fold
    from src.workers.keyword_expand import expand_term

    pkg = vp.get_package("remote")
    hay = fold("Одржавање информационог система (GIS система)")
    hit = any(
        fold(f) and fold(f) in hay
        for kw in pkg.keywords_exclude
        for f in expand_term(kw)
    )
    assert hit, "stop-words must cut “održavanje” (93 of 97 measured junk items)"

    hay2 = fold("Стручни надзор изводјењем радова на реализацији пројекта")
    hit2 = any(
        fold(f) and fold(f) in hay2
        for kw in pkg.keywords_exclude
        for f in expand_term(kw)
    )
    assert hit2, "stop-words must cut “radov” (supervision on site)"


def test_remote_package_stopwords_do_not_cut_generic_procurement():
    """“nabavka” (procurement in general) must not end up in the exclusions.

    Regression on a defect found by measurement: the dict form “postavka”→“nabavka”
    would cut “Javna nabavka usluga prevođenja” (procurement of TRANSLATION services —
    exactly the package niche). See TESTING §2.
    """
    from src.domain.text_norm import fold
    from src.workers.keyword_expand import expand_term

    pkg = vp.get_package("remote")
    hay = fold("Јавна набавка услуга превођења")
    hit = any(
        fold(f) and fold(f) in hay
        for kw in pkg.keywords_exclude
        for f in expand_term(kw)
    )
    assert not hit, "generic “nabavka” must not cut legitimate procurement of services"


def test_remote_package_to_profile_prefill_carries_exclude_and_context():
    pkg = vp.get_package("remote")
    prefill = vp.to_profile_prefill(pkg)
    assert set(prefill["keywords_exclude"]) == set(pkg.keywords_exclude)
    assert prefill["ai_extra_context"] == pkg.ai_extra_context["ru"]


def test_to_profile_prefill_defaults_for_packages_without_extras():
    # The existing 6 packages do not set keywords_exclude/ai_extra_context —
    # the passing of the new fields must not break them.
    pkg = vp.get_package("gradjevina")
    prefill = vp.to_profile_prefill(pkg)
    assert prefill["keywords_exclude"] == []
    assert prefill["ai_extra_context"] is None


# --- The owner’s decision of 30.09.2026: packages — Serbian Latin, words — stems -------


def test_package_sr_texts_and_keywords_have_no_cyrillic():
    import re

    cyr = re.compile("[А-Яа-яЁёЂђЈјЉљЊњЋћЏџ]")
    for pkg in vp.all_packages():
        assert not cyr.search(pkg.name["sr"]), pkg.slug
        assert not cyr.search(pkg.description["sr"]), pkg.slug
        if pkg.ai_extra_context:
            assert not cyr.search(pkg.ai_extra_context["sr"]), pkg.slug
        for g in pkg.keywords_include:
            assert not any(cyr.search(t) for t in g["all_of"]), (pkg.slug, g)
        assert not any(cyr.search(t) for t in pkg.keywords_exclude), pkg.slug


def test_package_stems_catch_all_cases_after_fold():
    """The stems catch the cases: «građevin» — građevina/građevinskih/građevinarstvo."""
    from src.domain.text_norm import fold

    pkg = vp.get_package("gradjevina")
    stems = {fold(t) for g in pkg.keywords_include for t in g["all_of"]}
    for word in ("građevinskih radova", "izgradnja škole", "građevinarstvo"):
        assert any(s in fold(word) for s in stems), word


def test_package_names_are_latin_in_serbian():
    names = {p["slug"]: p["name"] for p in vp.display_packages("sr")}
    assert names["gradjevina"] == "Građevina"
    assert names["ciscenje"] == "Čišćenje"
    assert names["obezbedjenje"] == "Obezbeđenje"
