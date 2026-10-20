"""The prefill of the profile from a template/package — one payload for the form and the showcase.

The client who pressed “Use template” got a form in which the dictionary of forms was off, the sources were not ticked, and the CPV stood as a section code. Each of the three little things separately gave 0 matches (a measurement on production 04.08): the template “Software and licenses” — 0 without the dictionary against 83 with it, 0 with the exact CPV against 10 with the prefix one, and 0 with an empty ``enabled_source_keys``.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import routes_profiles, template_prefill, vertical_packages
from src.api.forms import parse_profile_form
from src.api.main import app

SESS = {"uid": "u-1", "tid": "t-1"}


def _tpl(**kw):
    base = dict(
        id="00000016-0004-0000-0000-000000000001",
        name="Программное обеспечение и лицензии",
        description="ПО, лицензии, поддержка",
        activity_kind="IT и технологии",
        keywords_include=[{"all_of": ["программное"]}, {"all_of": ["лицензия"]}],
        classifications={
            "include": [{"system_code": "eu_cpv", "code": "48000000"}],
            "exclude": [],
        },
        i18n=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _client(*, sess=SESS):
    def factory():
        from tests.api.conftest import FakeSession

        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


# --------------------------------------------------------------------------- #
# The payload itself
# --------------------------------------------------------------------------- #


def test_template_prefill_enables_form_dictionary():
    """The bridge “Russian words of the template ↔ Serbian notices” — the dictionary of forms."""
    assert template_prefill.from_template(_tpl())["keyword_dict"] is True


def test_template_prefill_checks_sources():
    """An empty enabled_source_keys = zero matches (stage A requires a source).

    The sources come from the caller (the ``sources`` registry, SRC-0a) and are carried into the payload as they are.
    """
    payload = template_prefill.from_template(_tpl(), source_keys=["jnportal"])
    assert payload["enabled_source_keys"] == ["jnportal"]


def test_template_prefill_turns_division_cpv_into_prefix():
    payload = template_prefill.from_template(_tpl())
    assert payload["classifications"]["include"] == [
        {"system_code": "eu_cpv", "code": "48*"}
    ]


def test_template_prefill_copies_keywords_without_sharing_state():
    tpl = _tpl()
    payload = template_prefill.from_template(tpl)
    payload["keywords_include"][0]["all_of"].append("мутация")
    assert tpl.keywords_include[0]["all_of"] == ["программное"]


def test_package_prefill_matches_template_prefill_shape():
    pkg = vertical_packages.get_package("it")
    payload = template_prefill.from_package(pkg, source_keys=["jnportal"])
    assert payload["keyword_dict"] is True
    assert payload["enabled_source_keys"] == ["jnportal"]
    assert all(
        entry["code"].endswith("*") for entry in payload["classifications"]["include"]
    )


def test_package_prefill_carries_stop_words_to_profile_payload():
    """The package “Remote services” promises CPV + stop-words; #525 added the stop-words to the catalog, but from_package() did not carry them — the card’s figure (198) did not match the client’s real selection (307). The expectation — a literal from the fact of the run (vertical_packages.PACKAGES, slug=“remote”), not from the catalog itself, so that the test does not turn green on emptiness from both sides."""
    pkg = vertical_packages.get_package("remote")
    payload = template_prefill.from_package(pkg)
    assert payload["keywords_exclude"] == [
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


def test_package_prefill_without_stop_words_stays_empty():
    """Counter-fact: a package without stop-words (any of the original six) gives an empty keywords_exclude — it proves that the previous test sees the difference, rather than always being green."""
    pkg = vertical_packages.get_package("it")
    payload = template_prefill.from_package(pkg)
    assert payload["keywords_exclude"] == []


def test_unfiltered_payload_has_no_keywords_and_no_cpv():
    payload = template_prefill.unfiltered_payload()
    assert payload["keywords_include"] == []
    assert payload["classifications"]["include"] == []


def test_prefill_keys_are_the_form_contract():
    """The keys of the prefill = the keys of the form parsing: otherwise the field silently does not arrive."""
    parsed, errors = parse_profile_form({})
    assert not errors
    missing = set(template_prefill.base_payload()) - set(parsed)
    assert not missing, missing


# --------------------------------------------------------------------------- #
# The new profile form
# --------------------------------------------------------------------------- #


def test_form_from_template_has_dictionary_checked(monkeypatch):
    monkeypatch.setattr(routes_profiles.lib_svc, "get_template", _async(_tpl()))
    resp = _client().get("/profiles/new?from_template=00000016-0004")
    assert resp.status_code == 200
    body = resp.text
    marker = body.index('name="keyword_dict"')
    # the dictionary checkbox is rendered ticked
    assert "checked" in body[marker : marker + 120]
    # and the source is ticked too
    src = body.index('name="enabled_source_keys"')
    assert "checked" in body[src : src + 120]


def test_form_from_template_shows_prefill_note(monkeypatch):
    monkeypatch.setattr(routes_profiles.lib_svc, "get_template", _async(_tpl()))
    resp = _client().get("/profiles/new?from_template=00000016-0004")
    assert "prefill-note" in resp.text
    assert "Собрано из шаблона «Программное обеспечение и лицензии»" in resp.text
    # WHAT was substituted — in numbers, not “we filled something in”
    assert "2 ключевых слова" in resp.text
    assert "1 код CPV" in resp.text


def test_form_preset_all_shows_no_filter_note():
    resp = _client().get("/profiles/new?preset=all")
    assert resp.status_code == 200
    assert "Без фильтра: показываем все тендеры" in resp.text
    # there are no keywords in the form
    assert (
        '<textarea id="pf-kw-inc" name="keywords_include" rows="3" class="field-control font-mono"></textarea>'
        in resp.text
    )


def test_plain_new_profile_has_no_prefill_note():
    resp = _client().get("/profiles/new?manual=1")
    assert resp.status_code == 200
    assert "prefill-note" not in resp.text


def test_base_payload_declares_is_active_true():
    """‼️ Without an explicit ``is_active`` in the payload, `_apply_payload` (profile_service.py)
    skips the key, and the column ``profiles.is_active`` without a server_default gives
    NULL on insert — then the matching’s ``WHERE p.is_active = true`` (matching.py:194)
    silently does not find the profile (NULL = true → NULL, not true). The only call of create_profile today goes through the form (it always puts a bool), but
    the payload here is a contract also for a hypothetical direct call.
    """
    assert template_prefill.base_payload()["is_active"] is True
    assert template_prefill.from_template(_tpl())["is_active"] is True
    assert (
        template_prefill.from_package(vertical_packages.all_packages()[0])["is_active"]
        is True
    )
    assert template_prefill.unfiltered_payload()["is_active"] is True
