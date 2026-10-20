"""Tests of EPIC 3 — the template builder (template-builder).

Covers:
- the round-trip groups_to_textarea <-> _parse_keyword_groups (idempotence)
- the round-trip classifications_to_textarea <-> _parse_classifications
- preview_count: A+B gives fewer than A alone (the keywords filter)
- regression guard A: preview_service imports PREVIEW_WHERE_A from matching
  (not a copy — identity of the object; a divergence of predicates → the test falls)
- regression guard B: keywords_match from matching, not a copy
- SQL regression guard: the structure of PREVIEW_SQL contains all the fixed predicates
  (bugs 1–6 of the review of PR #188)
- behavioral tests of the parameter passing: two sources, include_no_amount, several include patterns, exclude, the default value
- render context: _profile_form_ctx serializes an ORM-like object
"""

from __future__ import annotations

import json
import re
import types

import pytest

from src.api.forms import (
    _parse_classifications,
    _parse_keyword_groups,
    classifications_to_textarea,
    groups_to_textarea,
)
from src.api.cabinet.routes_profiles import _profile_form_ctx

# ─── Round-trip: keywords_include ───────────────────────────────────────────


# The default source of the new profile (production fact 30.09: jnportal enabled).
# With SRC-0b the preview without a source = 0 (an empty choice ≠ “all sources”), therefore
# we check the mechanics of the preview on a payload WITH a source — as the form sends it.
SRC = ["jnportal"]


def test_groups_to_textarea_roundtrip_single_group():
    groups = [{"all_of": ["видеонаблюдение", "монтаж"]}]
    text = groups_to_textarea(groups)
    assert text == "видеонаблюдение, монтаж"
    assert _parse_keyword_groups(text) == groups


def test_groups_to_textarea_roundtrip_multi_group():
    groups = [
        {"all_of": ["видеонаблюдение", "монтаж"]},
        {"all_of": ["сигнализация"]},
    ]
    text = groups_to_textarea(groups)
    assert _parse_keyword_groups(text) == groups


def test_groups_to_textarea_empty():
    assert groups_to_textarea(None) == ""
    assert groups_to_textarea([]) == ""


def test_groups_to_textarea_idempotent():
    groups = [{"all_of": ["a", "b"]}, {"all_of": ["c"]}]
    text1 = groups_to_textarea(groups)
    groups2 = _parse_keyword_groups(text1)
    text2 = groups_to_textarea(groups2)
    assert text1 == text2
    assert groups2 == groups


# ─── Round-trip: classifications ─────────────────────────────────────────────


def test_classifications_to_textarea_roundtrip():
    entries = [
        {"system_code": "eu_cpv", "code": "45000000"},
        {"system_code": "eu_cpv", "code": "32*"},
    ]
    text = classifications_to_textarea(entries)
    errors: list[str] = []
    parsed = _parse_classifications(text, errors, "include")
    assert errors == []
    assert parsed == entries


def test_classifications_to_textarea_empty():
    assert classifications_to_textarea(None) == ""
    assert classifications_to_textarea([]) == ""


# ─── Regression guards: import from matching (not copies) ─────────────────────────


def test_preview_service_uses_matching_keywords_match():
    """preview_service must import keywords_match from matching, not copy it."""
    import src.api.cabinet.preview_service as ps
    import src.workers.matching as m

    assert ps.keywords_match is m.keywords_match, (
        "preview_service.keywords_match must be the same object as "
        "src.workers.matching.keywords_match — not a copy of the logic"
    )


def test_preview_service_imports_preview_where_a_from_matching():
    """PREVIEW_WHERE_A must be the same object from matching — shifting the predicates
    through a duplicated string breaks this test at once."""
    import src.api.cabinet.preview_service as ps
    import src.workers.matching as m

    assert ps.PREVIEW_WHERE_A is m.PREVIEW_WHERE_A, (
        "preview_service.PREVIEW_WHERE_A must be imported from matching, "
        "and not be a self-written copy — otherwise the predicates will diverge again"
    )


# ─── SQL regression guard: the structure of PREVIEW_SQL (bugs 1–6 of the review of PR #188) ──


def test_preview_sql_contains_all_stage_a_predicates():
    """Checks that _PREVIEW_SQL contains the right predicates for each of the 6 fixed bugs. If PREVIEW_WHERE_A changes incompatibly, the test falls with a specific indication of the missing construction."""
    from src.api.cabinet.preview_service import _PREVIEW_SQL

    sql = re.sub(r"\s+", " ", str(_PREVIEW_SQL)).strip().lower()

    # Bug 1: source — the full ANY array, not [0]
    assert (
        "tn.source_key = any(cast(:sources as text[]))" in sql
    ), "bug 1: the source filter must be ANY(:sources::text[]), not [0]"

    # Bug 2: cls include — EXISTS with a cross-join on tn.classifications AND inc.
    # ‼️ The comparison of systems — by the CANON of both sides (src/domain/classifications):
    # the former assertion pinned `tc->>'system_code' = inc->>'system_code'`, but
    # the tenders have no such key at all (ingest writes `scheme`) — the predicate
    # NEVER matched, and the test legitimised it.
    assert "exists (" in sql
    assert (
        "coalesce(tc->>'system_code', tc->>'scheme')" in sql
    ), "bug 2: the tender side must read both scheme and system_code"
    assert (
        sql.count("'^cpv$', 'eu_cpv'") >= 3
    ), "bug 2: both sides of the comparison must be canonicalised (inc/exc + tc)"
    assert (
        "inc->>'system_code'" in sql and "tc->>'code' like replace(" in sql
    ), "bug 2: include must be EXISTS over ALL inc-patterns and tn.classifications"

    # Bug 3: cls exclude — NOT EXISTS
    assert "not exists (" in sql
    assert (
        "exc->>'system_code'" in sql
    ), "bug 3: exclude must be NOT EXISTS; it did not exist at all"

    # Bug 4: min_days — COALESCE(..., 0), not a bare parameter
    assert (
        "coalesce(cast(:min_days as int), 0)" in sql
    ), "bug 4: min_days must be COALESCE(:min_days, 0)"

    # Bug 5: status — both branches NULL/NOT NULL as in matching
    assert (
        "tn.status is null or tn.status not in" in sql
    ), "bug 5: the status IS NULL branch is absent"
    assert (
        "tn.deadline_at is not null and (tn.status is not null or tn.deadline_at > now())"
        in sql
    ), "bug 5: the deadline safeguard for a NULL status is absent"
    # No deadline → current for 30 days from publication (owner 06.10.2026), not forever.
    assert (
        "tn.deadline_at is null and coalesce(tn.published_at, tn.first_seen_at) "
        ">= now() - interval '30 days'" in sql
    ), "no deadline = current for 30 days: the predicate of src/domain/actuality.py is gone"

    # Bug 6: include_no_amount is taken into account
    assert (
        "cast(:include_no_amount as boolean) = true" in sql
    ), "bug 6: include_no_amount is not taken into account in the amount predicate"


# ─── keywords_match by itself ──────────────────────────────────────────────


def test_keywords_match_basic():
    from src.workers.matching import keywords_match

    assert keywords_match([{"all_of": ["видео"]}], [], "видеонаблюдение монтаж")
    assert not keywords_match([{"all_of": ["видео"]}], [], "охрана")
    assert keywords_match([], ["стоп"], "чистый текст")
    assert not keywords_match([], ["стоп"], "стоп-слово здесь")


# ─── preview_count logic (mock DB) ───────────────────────────────────────────


class _FakeRow:
    def __init__(self, title, desc):
        self.title_normalized = title
        self.description_normalized = desc


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.last_params = None

    async def execute(self, query, params):
        self.last_params = params
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_preview_count_no_keywords_returns_all():
    from src.api.cabinet.preview_service import preview_count

    rows = [
        _FakeRow("ремонт дороги", "укладка асфальта"),
        _FakeRow("видеонаблюдение монтаж", "охрана"),
    ]
    db = _FakeSession(rows)
    # The preview mechanics is checked in the “All tenders” mode (0043): a profile without a
    # topic and without the flag does not match routing, therefore the preview must also give 0 —
    # see test_match_all_mode.py, where this is pinned separately.
    payload = {
        "enabled_source_keys": SRC,
        "keywords_include": [],
        "keywords_exclude": [],
        "match_all": True,
    }
    result = await preview_count(db, payload)
    assert result.count == 2
    assert result.unfound == []
    assert result.region_coverage_pct is None


@pytest.mark.asyncio
async def test_preview_count_keywords_filter():
    from src.api.cabinet.preview_service import preview_count

    rows = [
        _FakeRow("ремонт дороги", "укладка асфальта"),
        _FakeRow("видеонаблюдение монтаж", "охрана"),
        _FakeRow("монтаж видеонаблюдение объект", ""),
    ]
    db = _FakeSession(rows)
    payload = {
        "enabled_source_keys": SRC,
        "keywords_include": [{"all_of": ["видеонаблюдение", "монтаж"]}],
        "keywords_exclude": [],
    }
    result = await preview_count(db, payload)
    assert result.count == 2


@pytest.mark.asyncio
async def test_preview_count_stage_b_fewer_than_stage_a():
    from src.api.cabinet.preview_service import preview_count

    rows = [_FakeRow(f"тендер {i}", "текст") for i in range(10)]
    db = _FakeSession(rows)

    count_no_kw = await preview_count(
        db, {"enabled_source_keys": SRC, "keywords_include": [], "keywords_exclude": []}
    )
    count_with_kw = await preview_count(
        db,
        {
            "enabled_source_keys": SRC,
            "keywords_include": [{"all_of": ["видео"]}],
            "keywords_exclude": [],
        },
    )
    assert count_no_kw.count >= count_with_kw.count


# ─── region-ux-honest: honest coverage of the region ───────────────────────────────
# The main query filters by region, the second (without a region) counts the share
# of tenders with regions set. We route by the presence of has_region in the SQL.


class _CovRow:
    def __init__(self, title, desc, has_region):
        self.title_normalized = title
        self.description_normalized = desc
        self.has_region = has_region


class _RoutingSession:
    """preview_rows — the main query; coverage_rows — the coverage query (has_region in the SQL)."""

    def __init__(self, preview_rows, coverage_rows):
        self._preview = preview_rows
        self._coverage = coverage_rows
        self.last_params = None

    async def execute(self, query, params):
        self.last_params = params
        rows = self._coverage if "has_region" in str(query) else self._preview
        return _FakeResult(rows)


@pytest.mark.asyncio
async def test_preview_region_coverage_low_when_region_selected():
    """A region is selected → we count the share of tenders with regions set and the hidden tail."""
    from src.api.cabinet.preview_service import preview_count

    # 3 tenders passed the region filter; without the region 10 would fit, of them
    # only 3 have a region set → coverage 30%, hidden 7.
    preview_rows = [_FakeRow(f"тендер {i}", "текст") for i in range(3)]
    coverage_rows = [_CovRow(f"тендер {i}", "текст", i < 3) for i in range(10)]
    db = _RoutingSession(preview_rows, coverage_rows)

    result = await preview_count(
        db, {"enabled_source_keys": SRC, "regions": ["RS-01"], "match_all": True}
    )
    assert result.count == 3
    assert result.region_coverage_pct == 30
    assert result.hidden_by_region == 7


@pytest.mark.asyncio
async def test_preview_region_coverage_none_without_region():
    """No region selected → the coverage fields are None and the second query is not executed."""
    from src.api.cabinet.preview_service import preview_count

    # coverage_rows are set, but with an empty regions the coverage query does not go.
    preview_rows = [_FakeRow("тендер", "текст")]
    coverage_rows = [_CovRow("t", "x", False) for _ in range(5)]
    db = _RoutingSession(preview_rows, coverage_rows)

    result = await preview_count(
        db, {"enabled_source_keys": SRC, "regions": [], "match_all": True}
    )
    assert result.count == 1
    assert result.region_coverage_pct is None
    assert result.hidden_by_region is None


@pytest.mark.asyncio
async def test_preview_region_coverage_respects_keywords():
    """The coverage denominator takes stage B into account: those not passing by words are not counted."""
    from src.api.cabinet.preview_service import preview_count

    preview_rows = [_FakeRow("монтаж видео объект", "")]
    # 2 of 4 coverage rows pass by the key “video”; of them 1 with a region.
    coverage_rows = [
        _CovRow("видео камера", "", True),
        _CovRow("видео монтаж", "", False),
        _CovRow("асфальт дорога", "", True),
        _CovRow("уборка снега", "", True),
    ]
    db = _RoutingSession(preview_rows, coverage_rows)

    result = await preview_count(
        db,
        {
            "enabled_source_keys": SRC,
            "regions": ["RS-01"],
            "keywords_include": [{"all_of": ["видео"]}],
        },
    )
    # denom=2 (video rows), numer=1 (with a region) → 50%.
    assert result.region_coverage_pct == 50


# ─── kw-serbian-forms: Serbian forms of the entered words in the preview ─────────────────
# The user enters words in Russian, tenders are in Serbian → they must SEE
# which Serbian forms the search will really go by. The forms mirror expand_term
# of the matching; the display renders the partial _kw_forms.html (form/endpoint — one partial).


def _dict_word_with_forms():
    """A real word from keyword_dict.json that has sr forms."""
    from src.workers.keyword_expand import _load_dict

    for word, entry in _load_dict().items():
        forms = [f for f in entry.get("sr", []) if f != word]
        if forms:
            return word, forms
    raise AssertionError("there is not a single word with sr forms in the dictionary")


@pytest.mark.asyncio
async def test_preview_include_forms_reach_template_context():
    """(a) A word with forms → its Serbian forms reached the template context."""
    from src.api.cabinet.preview_service import preview_count
    from src.api.templating import templates

    word, forms = _dict_word_with_forms()
    db = _FakeSession([])
    result = await preview_count(
        db,
        {
            "enabled_source_keys": SRC,
            "keywords_include": [{"all_of": [word]}],
            "keyword_dict": True,
        },
    )
    entry = next(f for f in result.include_forms if f["term"] == word)
    # The forms — exactly those the matching searches by (expand_term without the original).
    assert entry["forms"] == forms
    # And they reach the RENDER of the partial (the template context), not only the dataclass.
    html = templates.get_template("_kw_forms.html").render(
        include_forms=result.include_forms,
        exclude_forms=result.exclude_forms,
        keyword_dict_enabled=result.keyword_dict_enabled,
    )
    assert word in html
    assert forms[0] in html


@pytest.mark.asyncio
async def test_preview_word_without_forms_not_lost():
    """(b) A word without forms → in both unfound and include_forms (with empty forms)."""
    from src.api.cabinet.preview_service import preview_count

    missing = "зюзюблинация"  # certainly not in the dictionary
    db = _FakeSession([])
    result = await preview_count(
        db,
        {
            "enabled_source_keys": SRC,
            "keywords_include": [{"all_of": [missing]}],
            "keyword_dict": True,
        },
    )
    assert missing in result.unfound
    entry = next(f for f in result.include_forms if f["term"] == missing)
    assert entry["forms"] == []


@pytest.mark.asyncio
async def test_preview_forms_marks_dict_disabled():
    """(c) keyword_dict is off → the flag is False and the partial marks the disabled extension."""
    from src.api.cabinet.preview_service import preview_count
    from src.api.templating import templates

    word, forms = _dict_word_with_forms()
    db = _FakeSession([])
    result = await preview_count(
        db,
        {
            "enabled_source_keys": SRC,
            "keywords_include": [{"all_of": [word]}],
            "keyword_dict": False,
        },
    )
    assert result.keyword_dict_enabled is False
    # The word is still shown (the entered one is visible), but with the mark about the disabling.
    assert any(f["term"] == word for f in result.include_forms)
    html = templates.get_template("_kw_forms.html").render(
        include_forms=result.include_forms,
        exclude_forms=result.exclude_forms,
        keyword_dict_enabled=result.keyword_dict_enabled,
    )
    assert "выключен" in html
    # With the extension off, the Serbian forms are NOT offered as active.
    assert forms[0] not in html


# ─── Behavioral tests of the preview parameters (bugs 1–6, PR #188) ─────────────
# The DB mock returns rows without filtering; we check only the parameters
# that go into execute() — that is enough to pin
# the fixes 1–6 at the level of parameter binding.


@pytest.mark.asyncio
async def test_preview_count_two_sources_sends_full_array():
    """Bug 1: with two sources the filter is NOT switched off — the whole array is passed."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([])
    await preview_count(
        db, {"enabled_source_keys": ["jnportal", "eproc"], "match_all": True}
    )
    assert db.last_params["sources"] == [
        "jnportal",
        "eproc",
    ], "bug 1: with >1 source sources must contain all keys, not None"


@pytest.mark.asyncio
async def test_preview_count_include_no_amount_false_with_bounds_in_params():
    """Bug 6: include_no_amount=False + the amount bounds → the correct parameters."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([])
    payload = {
        "enabled_source_keys": SRC,
        "include_no_amount": False,
        "min_amount_eur": 1000,
        "max_amount_eur": 50000,
        "match_all": True,
    }
    await preview_count(db, payload)
    assert db.last_params["include_no_amount"] is False
    assert db.last_params["min_amount"] == 1000.0
    assert db.last_params["max_amount"] == 50000.0


@pytest.mark.asyncio
async def test_preview_count_cls_include_all_patterns_sent():
    """Bug 2: all include patterns go into cls_include, not only [0]."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([])
    inc_list = [
        {"system_code": "eu_cpv", "code": "45*"},
        {"system_code": "eu_cpv", "code": "32*"},
    ]
    payload = {
        "enabled_source_keys": SRC,
        "classifications": {"include": inc_list, "exclude": []},
    }
    await preview_count(db, payload)
    parsed = json.loads(db.last_params["cls_include"])
    assert len(parsed) == 2, "bug 2: both include patterns must go into the parameter"
    codes = {e["code"] for e in parsed}
    assert codes == {"45*", "32*"}


@pytest.mark.asyncio
async def test_preview_count_cls_exclude_sent_in_params():
    """Bug 3: the exclude pattern is passed into cls_exclude (it was not passed before)."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([])
    exc_list = [{"system_code": "eu_cpv", "code": "99*"}]
    payload = {
        "enabled_source_keys": SRC,
        "classifications": {"include": [], "exclude": exc_list},
        "match_all": True,
    }
    await preview_count(db, payload)
    parsed = json.loads(db.last_params["cls_exclude"])
    assert len(parsed) == 1, "bug 3: the exclude pattern must be present in the parameters"
    assert parsed[0]["code"] == "99*"


@pytest.mark.asyncio
async def test_preview_count_include_no_amount_defaults_to_false():
    """Bug 6: if include_no_amount is not set in the payload — False is passed (not NULL)."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([])
    await preview_count(db, {"enabled_source_keys": SRC, "match_all": True})
    assert db.last_params["include_no_amount"] is False


# ─── _profile_form_ctx: serialization of an ORM-like object ──────────────────


def _make_orm_like(**kw):
    obj = types.SimpleNamespace(**kw)
    return obj


def test_profile_form_ctx_from_orm():
    profile = _make_orm_like(
        keywords_include=[{"all_of": ["видео", "монтаж"]}, {"all_of": ["охрана"]}],
        keywords_exclude=["б/у", "демонтаж"],
        classifications={
            "include": [{"system_code": "eu_cpv", "code": "45000000"}],
            "exclude": [{"system_code": "eu_cpv", "code": "99000000"}],
        },
    )
    ctx = _profile_form_ctx(profile)
    assert ctx["keywords_include_text"] == "видео, монтаж\nохрана"
    assert ctx["keywords_exclude_text"] == "б/у, демонтаж"
    assert ctx["cls_include_text"] == "eu_cpv:45000000"
    assert ctx["cls_exclude_text"] == "eu_cpv:99000000"


def test_profile_form_ctx_from_dict():
    profile = {
        "keywords_include": [{"all_of": ["сигнализация"]}],
        "keywords_exclude": ["б/у"],
        "classifications": {
            "include": [{"system_code": "eu_cpv", "code": "32*"}],
            "exclude": [],
        },
    }
    ctx = _profile_form_ctx(profile)
    assert ctx["keywords_include_text"] == "сигнализация"
    assert ctx["keywords_exclude_text"] == "б/у"
    assert ctx["cls_include_text"] == "eu_cpv:32*"
    assert ctx["cls_exclude_text"] == ""


def test_profile_form_ctx_none():
    ctx = _profile_form_ctx(None)
    assert ctx["keywords_include_text"] == ""
    assert ctx["cls_include_text"] == ""


# ─── expand_terms ─────────────────────────────────────────────────────────────


def test_expand_terms_disabled_returns_original():
    from src.workers.keyword_expand import expand_terms

    result, unfound = expand_terms(["компьютер", "видеонаблюдение"], enabled=False)
    assert result == ["компьютер", "видеонаблюдение"]
    assert unfound == set()


def test_expand_terms_known_word_includes_sr_forms():
    from src.workers.keyword_expand import expand_terms

    result, unfound = expand_terms(["компьютер"], enabled=True)
    assert "компьютер" in result
    assert any("рачунар" in f for f in result), "Serbian forms must be present"
    assert "компьютер" not in unfound


def test_expand_terms_unknown_word_in_unfound():
    from src.workers.keyword_expand import expand_terms

    result, unfound = expand_terms(["ксилофон123"], enabled=True)
    assert "ксилофон123" in result
    assert "ксилофон123" in unfound


def test_expand_terms_mixed():
    from src.workers.keyword_expand import expand_terms

    result, unfound = expand_terms(["computer", "ксилофон123"], enabled=True)
    assert "computer" in result
    assert any("рачунар" in f for f in result)
    assert "ксилофон123" in unfound
    assert "computer" not in unfound


# ─── keywords_match with keyword_dict_enabled ────────────────────────────────────


def test_keywords_match_dict_ru_matches_serbian_form():
    """A Russian word + the dictionary toggle → a match on the Serbian word form."""
    from src.workers.matching import keywords_match

    # «рачунар» — сербская форма слова «компьютер»
    assert keywords_match(
        [{"all_of": ["компьютер"]}],
        [],
        "набавка рачунара и опреме",
        keyword_dict_enabled=True,
    )


def test_keywords_match_dict_en_matches_serbian_form():
    """An English word + the dictionary toggle → a match on the Serbian word form."""
    from src.workers.matching import keywords_match

    assert keywords_match(
        [{"all_of": ["computer"]}], [], "набавка рачунара", keyword_dict_enabled=True
    )


def test_keywords_match_dict_disabled_no_match():
    """Without the dictionary toggle the Russian word does NOT match the Serbian text."""
    from src.workers.matching import keywords_match

    assert not keywords_match(
        [{"all_of": ["компьютер"]}], [], "набавка рачунара", keyword_dict_enabled=False
    )


def test_keywords_match_dict_exclude_expands():
    """Exclude with the toggle: the Serbian form of a Russian stop-word excludes the tender."""
    from src.workers.matching import keywords_match

    # «аутомобил» — сербская форма «автомобиль»
    assert not keywords_match(
        [], ["автомобиль"], "набавка аутомобила", keyword_dict_enabled=True
    )


def test_keywords_match_dict_original_preserved():
    """The original word is kept with the dictionary on."""
    from src.workers.matching import keywords_match

    assert keywords_match(
        [{"all_of": ["computer"]}], [], "computer rental", keyword_dict_enabled=True
    )


# ─── END-TO-END contour: the Cyrillic dictionary ↔ the Latin haystack ─────────────────
# A regression for the real production defect: title_normalized is stored in Latin without
# diacritics (the ingest fold), while the keywords/dictionary are in Serbian Cyrillic.
# Before the fix not a single Cyrillic form matched the Latin haystack (0
# matches). Mutation check: replace the fold() in keywords_match with
# haystack.lower()/term.lower() → all four tests below go red.


def test_keywords_match_cyrillic_kw_vs_normalized_latin_haystack():
    """A keyword in Serbian Cyrillic ↔ the already normalized (Latin) haystack of the tender → there IS a match (without the dictionary — by one fold of both sides)."""
    from src.workers.matching import keywords_match

    # haystack — exactly as in tenders_normalized (Latin, without diacritics)
    # “čišćenje” was entered by the user in Serbian Cyrillic; it is NOT in the ru/en dictionary.
    # “čišćenje” → folding → «ciscenje» (see the task acceptance).
    assert keywords_match(
        [{"all_of": ["чишћење"]}], [], "javna nabavka usluga ciscenje prostorija"
    )


def test_keywords_match_latin_diacritic_kw_vs_normalized_haystack():
    """A word in Latin WITH DIACRITICS ↔ the normalized haystack → a match (the fold removes the diacritics from both sides)."""
    from src.workers.matching import keywords_match

    assert keywords_match(
        [{"all_of": ["čišćenje"]}], [], "usluga ciscenje i odrzavanje"
    )


def test_keywords_exclude_cyrillic_kw_vs_normalized_latin_haystack():
    """A stop-word in Serbian Cyrillic ↔ the Latin haystack → excludes in the same way."""
    from src.workers.matching import keywords_match

    # include is empty → without exclude there would be a match; “održavanje”→«odrzavanje» excludes.
    assert not keywords_match([], ["одржавање"], "nabavka usluga odrzavanje lifta")


def test_keywords_match_full_ingest_contour_cyrillic_title():
    """The full contour: the raw Serbian Cyrillic heading → the ingest fold (:func:`normalize_text`) gives the haystack → the Cyrillic keyword matches."""
    from src.domain.text_norm import normalize_text
    from src.workers.matching import keywords_match

    # As it would come from the source and be written by the ingest into title_normalized:
    haystack = normalize_text("Јавна набавка услуга чишћење пословног простора")
    assert haystack is not None
    assert all(ord(c) < 128 for c in haystack)  # sanity: haystack — pure ASCII
    assert "ciscenje" in haystack
    assert keywords_match([{"all_of": ["чишћење"]}], [], haystack)


# ─── forms: keyword_dict is parsed ────────────────────────────────────────────


def test_parse_profile_form_keyword_dict_true():
    from src.api.forms import parse_profile_form

    payload, errors = parse_profile_form({"name": "test", "keyword_dict": "on"})
    assert errors == []
    assert payload["keyword_dict"] is True


def test_parse_profile_form_keyword_dict_false():
    from src.api.forms import parse_profile_form

    payload, errors = parse_profile_form({"name": "test"})
    assert payload["keyword_dict"] is False


# ─── preview_count with keyword_dict ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_count_keyword_dict_matches_serbian():
    """keyword_dict=True → the Russian word matches the Serbian word form in the tender text."""
    from src.api.cabinet.preview_service import preview_count

    rows = [
        _FakeRow("набавка рачунара и пратеће опреме", "испорука рачунара"),
        _FakeRow("ремонт дорога", "асфалтирање"),
    ]
    db = _FakeSession(rows)
    payload = {
        "enabled_source_keys": SRC,
        "keywords_include": [{"all_of": ["компьютер"]}],
        "keywords_exclude": [],
        "keyword_dict": True,
    }
    result = await preview_count(db, payload)
    assert result.count == 1
    assert result.unfound == []


@pytest.mark.asyncio
async def test_preview_count_keyword_dict_unfound():
    """A word not in the dictionary → falls into unfound."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([_FakeRow("нешто", "")])
    payload = {
        "enabled_source_keys": SRC,
        "keywords_include": [{"all_of": ["ксилофон123"]}],
        "keywords_exclude": [],
        "keyword_dict": True,
    }
    result = await preview_count(db, payload)
    assert "ксилофон123" in result.unfound


# ─── Regression: preview_service uses the same expand_term as keywords_match ─


def test_preview_uses_same_expand_as_matching():
    """expand_terms from preview_service and keywords_match — one object."""
    import src.api.cabinet.preview_service as ps
    import src.workers.keyword_expand as ke

    assert ps.expand_terms is ke.expand_terms


# ─── Regression: the stage A predicates are not broken ──────────────────────────────────


def test_stage_a_predicates_intact_after_iteration2():
    """We make sure that PREVIEW_WHERE_A is still imported from matching (not a copy)."""
    import src.api.cabinet.preview_service as ps
    import src.workers.matching as m

    assert ps.PREVIEW_WHERE_A is m.PREVIEW_WHERE_A


@pytest.mark.asyncio
async def test_serbian_word_present_in_data_is_not_reported_unfound():
    """A word outside the dictionary but living in the tenders is Serbian, not “untranslated”."""
    from src.api.cabinet.preview_service import preview_count

    db = _FakeSession([_FakeRow("osnovna škola vrtić", ""), _FakeRow("put", "")])
    payload = {
        "enabled_source_keys": SRC,
        "keywords_include": [{"all_of": ["škola", "ксилофон123"]}],
        "keywords_exclude": [],
        "keyword_dict": True,
    }
    result = await preview_count(db, payload)
    assert "škola" not in result.unfound
    assert "ксилофон123" in result.unfound
