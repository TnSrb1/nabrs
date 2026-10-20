"""Tests of parsing and validation of the HTMX profile forms (:mod:`src.api.forms`).

Pure functions without DB/network. We check field normalization, both paths of the
multi-value fields (``getlist`` and the CSV string), and error accumulation.
"""

from __future__ import annotations

from src.api import forms
from src.api.forms import parse_profile_form


class FakeMultiDict:
    """Imitation of ``request.form()`` (Starlette FormData / Werkzeug MultiDict).

    Supports ``get`` (the first value) and ``getlist`` (all values) —
    like the multi-value container of the web framework.
    """

    def __init__(self, data: dict[str, list[str]]):
        self._data = {k: list(v) for k, v in data.items()}

    def get(self, key, default=None):
        values = self._data.get(key)
        return values[0] if values else default

    def getlist(self, key):
        return list(self._data.get(key, []))


# --------------------------------------------------------------------------- #
# _getlist — two paths: a getlist container and a flat CSV dictionary
# --------------------------------------------------------------------------- #


def test_getlist_uses_getlist_when_available():
    form = FakeMultiDict({"regions": ["RS", " BA ", ""]})
    assert forms._getlist(form, "regions") == ["RS", "BA"]


def test_getlist_falls_back_to_csv_for_plain_dict():
    form = {"regions": "RS, BA , ,ME"}
    assert forms._getlist(form, "regions") == ["RS", "BA", "ME"]


def test_getlist_missing_key_returns_empty():
    assert forms._getlist({}, "regions") == []
    assert forms._getlist(FakeMultiDict({}), "regions") == []


def test_getlist_getlist_empty_falls_back_to_get():
    # getlist returns empty → the get/CSV branch is used
    class OnlyEmptyGetlist:
        def getlist(self, key):
            return []

        def get(self, key, default=None):
            return "RS,BA"

    assert forms._getlist(OnlyEmptyGetlist(), "regions") == ["RS", "BA"]


# --------------------------------------------------------------------------- #
# _csv
# --------------------------------------------------------------------------- #


def test_csv_splits_and_strips():
    assert forms._csv({"k": "a, b ,,c"}, "k") == ["a", "b", "c"]


def test_csv_empty():
    assert forms._csv({}, "k") == []
    assert forms._csv({"k": ""}, "k") == []


# --------------------------------------------------------------------------- #
# _parse_keyword_groups
# --------------------------------------------------------------------------- #


def test_parse_keyword_groups_one_group_per_line():
    raw = "видеонаблюдение, монтаж\nохрана"
    assert forms._parse_keyword_groups(raw) == [
        {"all_of": ["видеонаблюдение", "монтаж"]},
        {"all_of": ["охрана"]},
    ]


def test_parse_keyword_groups_skips_blank_lines():
    raw = "\na, b\n   \n, ,\nc\n"
    assert forms._parse_keyword_groups(raw) == [
        {"all_of": ["a", "b"]},
        {"all_of": ["c"]},
    ]


def test_parse_keyword_groups_none():
    assert forms._parse_keyword_groups(None) == []


# --------------------------------------------------------------------------- #
# _parse_classifications
# --------------------------------------------------------------------------- #


def test_parse_classifications_ok():
    errors: list[str] = []
    out = forms._parse_classifications("eu_cpv:32*\ncpv : 45", errors, "include")
    assert out == [
        {"system_code": "eu_cpv", "code": "32*"},
        {"system_code": "cpv", "code": "45"},
    ]
    assert errors == []


def test_parse_classifications_missing_colon_records_error():
    errors: list[str] = []
    out = forms._parse_classifications("badline", errors, "include")
    assert out == []
    assert len(errors) == 1
    assert errors[0] == "err_cls_format"  # a catalog key, not a ready phrase


def test_parse_classifications_empty_system_or_code():
    errors: list[str] = []
    out = forms._parse_classifications(":45\neu_cpv:", errors, "exclude")
    assert out == []
    assert len(errors) == 2
    assert all(e == "err_cls_empty" for e in errors)


def test_parse_classifications_skips_blank_and_none():
    errors: list[str] = []
    assert forms._parse_classifications(None, errors, "include") == []
    assert forms._parse_classifications("\n   \n", errors, "include") == []
    assert errors == []


# --------------------------------------------------------------------------- #
# _to_decimal / _to_int / _to_bool / _norm_time
# --------------------------------------------------------------------------- #


def test_to_decimal_variants():
    errors: list[str] = []
    assert forms._to_decimal("1 234,50", "f", errors) == 1234.5
    assert forms._to_decimal("10", "f", errors) == 10.0
    assert errors == []


def test_to_decimal_empty_is_none():
    errors: list[str] = []
    assert forms._to_decimal(None, "f", errors) is None
    assert forms._to_decimal("   ", "f", errors) is None
    assert errors == []


def test_to_decimal_invalid_records_error():
    errors: list[str] = []
    assert forms._to_decimal("abc", "err_num_min_amount", errors) is None
    assert len(errors) == 1
    assert errors[0] == "err_num_min_amount"  # a catalog key, resolved by the render


def test_to_int_variants():
    errors: list[str] = []
    assert forms._to_int(" 7 ", "f", errors) == 7
    assert forms._to_int(None, "f", errors) is None
    assert forms._to_int("", "f", errors) is None
    assert errors == []


def test_to_int_invalid_records_error():
    errors: list[str] = []
    assert forms._to_int("3.5", "err_int_min_days", errors) is None
    assert len(errors) == 1
    assert errors[0] == "err_int_min_days"


def test_to_bool_truthy_and_falsy():
    for v in ("1", "true", "On", "YES", " yes "):
        assert forms._to_bool(v) is True
    for v in ("0", "false", "off", "no", "", None):
        assert forms._to_bool(v) is False


def test_norm_time():
    assert forms._norm_time(" 22:00 ") == "22:00"
    assert forms._norm_time("") is None
    assert forms._norm_time(None) is None


# --------------------------------------------------------------------------- #
# parse_profile_form — integration
# --------------------------------------------------------------------------- #


def test_parse_profile_form_full_valid():
    form = FakeMultiDict(
        {
            "name": ["  Мой профиль  "],
            "target_language": ["sr-Latn"],
            "is_active": ["on"],
            "enabled_source_keys": ["enabavke", "portal"],
            "keywords_include": ["видеонаблюдение, монтаж\nохрана"],
            "keywords_exclude": ["б/у", "демонтаж"],
            "cls_include": ["eu_cpv:32*"],
            "cls_exclude": ["eu_cpv:99"],
            "regions": ["RS", "BA"],
            "min_amount_eur": ["1 000,50"],
            "max_amount_eur": ["50000"],
            "include_no_amount": ["on"],
            "procedure_types": ["open"],
            "languages": ["sr-Latn", "ru"],
            "min_days_until_deadline": ["5"],
            # The auto-analysis level via the form is no longer accepted (0044): AI
            # is started by a person with a button. The value is deliberately left in the form —
            # we check that the parser ignores it, rather than trusting the request.
            "ai_analysis_level": ["Deep"],
            "ai_focus_questions": ["Срок поставки?\nГарантия?"],
            "ai_extra_context": ["  важный контекст  "],
            "delivery_channels": ["email", "telegram", "unknown"],
            "delivery_schedule": ["digest_morning"],
            "digest_cron": ["0 8 * * *"],
            "quiet_hours_start": ["22:00"],
            "quiet_hours_end": ["08:00"],
        }
    )
    payload, errors = parse_profile_form(form)

    assert errors == []
    assert payload["name"] == "Мой профиль"
    assert payload["target_language"] == "sr-Latn"
    assert payload["is_active"] is True
    assert payload["enabled_source_keys"] == ["enabavke", "portal"]
    assert payload["keywords_include"] == [
        {"all_of": ["видеонаблюдение", "монтаж"]},
        {"all_of": ["охрана"]},
    ]
    assert payload["keywords_exclude"] == ["б/у", "демонтаж"]
    assert payload["classifications"] == {
        "include": [{"system_code": "eu_cpv", "code": "32*"}],
        "exclude": [{"system_code": "eu_cpv", "code": "99"}],
    }
    assert payload["regions"] == ["RS", "BA"]
    assert payload["min_amount_eur"] == 1000.5
    assert payload["max_amount_eur"] == 50000.0
    assert payload["include_no_amount"] is True
    assert payload["procedure_types"] == ["open"]
    assert payload["languages"] == ["sr-Latn", "ru"]
    assert payload["min_days_until_deadline"] == 5
    # ai_level is normalized to lower case
    assert payload["ai_analysis_level"] == "none"
    assert payload["ai_focus_questions"] == ["Срок поставки?", "Гарантия?"]
    assert payload["ai_extra_context"] == "важный контекст"
    # an unknown channel is filtered out
    assert payload["delivery_channels"] == ["email", "telegram"]
    assert payload["delivery_schedule"] == "digest_morning"
    assert payload["digest_cron"] == "0 8 * * *"
    assert payload["quiet_hours_start"] == "22:00"
    assert payload["quiet_hours_end"] == "08:00"


def test_parse_profile_form_defaults_on_empty():
    payload, errors = parse_profile_form({})
    # Nothing is required: an empty form → 0 errors (the name is auto-generated by the backend).
    assert errors == []
    assert payload["name"] == ""
    assert payload["target_language"] == "ru"
    # is_active by default "on" → True
    assert payload["is_active"] is True
    assert payload["keywords_include"] == []
    assert payload["classifications"] == {"include": [], "exclude": []}
    assert payload["min_amount_eur"] is None
    assert payload["max_amount_eur"] is None
    assert payload["include_no_amount"] is False
    assert payload["min_days_until_deadline"] is None
    assert payload["ai_analysis_level"] == "none"
    assert payload["ai_focus_questions"] == []
    assert payload["ai_extra_context"] is None
    assert payload["delivery_channels"] == []
    assert payload["delivery_schedule"] == "immediate"
    assert payload["digest_cron"] is None
    assert payload["quiet_hours_start"] is None
    assert payload["quiet_hours_end"] is None


def test_parse_profile_form_unknown_schedule_resets_with_error():
    payload, errors = parse_profile_form({"name": "p", "delivery_schedule": "weird"})
    assert payload["delivery_schedule"] == "immediate"
    assert "err_bad_schedule" in errors  # catalog key


def test_parse_profile_form_ignores_any_ai_level_from_request():
    """Any value of the level from the request is ignored — the auto-analysis is removed (0044).

    There is no error here on purpose: the field does not exist in the form, and there is no reason to complain about a forged POST — it is enough that it will not enable a paid call.
    """
    payload, errors = parse_profile_form({"name": "p", "ai_analysis_level": "ultra"})
    assert payload["ai_analysis_level"] == "none"
    assert errors == []


def test_parse_profile_form_is_active_off():
    payload, _ = parse_profile_form({"name": "p", "is_active": "off"})
    assert payload["is_active"] is False


def test_parse_profile_form_accumulates_multiple_errors():
    payload, errors = parse_profile_form(
        {
            "min_amount_eur": "abc",
            "max_amount_eur": "xyz",
            "min_days_until_deadline": "not-int",
            "cls_include": "badline",
        }
    )
    # 2 decimal + 1 int + 1 classification = 4 errors (an empty name is NOT an error)
    assert len(errors) == 4
    assert payload["min_amount_eur"] is None
    assert payload["max_amount_eur"] is None
    assert payload["min_days_until_deadline"] is None


# --------------------------------------------------------------------------- #
# E2: the form accepts only the channels that actually deliver
# --------------------------------------------------------------------------- #


def test_available_channels_are_only_the_deliverable_ones():
    """viber — a stub (NotImplementedError), there is no web adapter: they must not be here.

    A literal, not an output from the code: a divergence “the form promises / delivery cannot” must be caught by a failing test, not by silence in production.
    """
    assert forms.AVAILABLE_CHANNELS == {"telegram", "email"}


def test_parse_profile_form_drops_undeliverable_channels():
    form = FakeMultiDict(
        {
            "name": ["Профиль"],
            "delivery_channels": ["email", "viber", "telegram", "web"],
        }
    )
    payload, errors = parse_profile_form(form)

    # viber/web do not reach the payload → no POST will write them to the DB,
    # even one assembled by hand bypassing the form (there are no checkboxes for them).
    assert payload["delivery_channels"] == ["email", "telegram"]
    assert errors == []


def test_parse_profile_form_survives_legacy_channels_from_db():
    """Backward compatibility: a profile with an old viber/web does not break parsing.

    There are no such profiles in production, but if the value lingered in the DB — the form
    simply will not accept it back, without an exception and without a validation error.
    """
    form = FakeMultiDict({"name": ["Профиль"], "delivery_channels": ["viber", "web"]})
    payload, errors = parse_profile_form(form)

    assert payload["delivery_channels"] == []
    assert errors == []


def test_getlist_splits_comma_text_inside_getlist_values():
    # The text field of stop-words in a real FormData is ONE comma-separated string.
    form = FakeMultiDict({"keywords_exclude": ["a, b, c"]})
    assert forms._getlist(form, "keywords_exclude") == ["a", "b", "c"]


def test_parse_profile_form_exclude_words_split_by_comma():
    form = FakeMultiDict({"keywords_exclude": ["ремонт, монтаж , сервер,"]})
    payload, _ = parse_profile_form(form)
    assert payload["keywords_exclude"] == ["ремонт", "монтаж", "сервер"]


def test_getlist_checkbox_values_unchanged():
    form = FakeMultiDict({"regions": ["RS", "BA"]})
    assert forms._getlist(form, "regions") == ["RS", "BA"]


def test_parse_profile_form_real_starlette_formdata():
    from starlette.datastructures import FormData

    form = FormData([("keywords_exclude", "a, b, c"), ("regions", "RS")])
    payload, _ = parse_profile_form(form)
    assert payload["keywords_exclude"] == ["a", "b", "c"]
    assert payload["regions"] == ["RS"]
