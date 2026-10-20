"""The profile form errors are catalog keys, not ready Russian phrases.

Defect: `parse_profile_form` composed the text itself (“Field ”Min. amount“: a number is expected”, “Classifier (…) ”…“: empty system or code”, “Unknown schedule mode: …”). These strings go to the client AFTER the POST — outside the GET render that the usual i18n measurements check, which is why they survived five localisation sessions.

The form parser knows no language (it has no `request`), so it returns KEYS,
and `profile_form.html` resolves them via `t.get(e, e)` by locale — the same contour
that `profile_service` errors already use (`err_profile_*`).
"""

from __future__ import annotations

import ast
import pathlib
import re

from src.api import forms
from src.api.cabinet.i18n import CABINET

_CYR = re.compile(r"[а-яА-ЯёЁ]")
_KEYS = (
    "err_num_min_amount",
    "err_num_max_amount",
    "err_int_min_days",
    "err_bad_schedule",
    "err_bad_ai_level",
    "err_cls_format",
    "err_cls_empty",
)


def test_all_error_keys_exist_in_three_languages():
    """Every key that the parser may return exists in all locales.

    Otherwise the client sees a raw key instead of text (the render does `t.get(e, e)`).
    """
    for key in _KEYS:
        for lang in ("sr", "ru", "en"):
            assert key in CABINET[lang], (key, lang)


def test_translations_are_not_russian_in_sr_and_en():
    """The sr/en variants contain no Cyrillic — otherwise the translation is formal."""
    for key in _KEYS:
        for lang in ("sr", "en"):
            assert not _CYR.search(CABINET[lang][key]), (key, lang, CABINET[lang][key])


def test_parser_returns_keys_not_phrases():
    """The parser returns keys: they can be translated, a ready phrase cannot.

    Mutation: return `errors.append(f"Field "{field}": …")` → the test goes red.
    """
    errors: list[str] = []
    assert forms._to_decimal("abc", "err_num_min_amount", errors) is None
    assert forms._to_int("3.5", "err_int_min_days", errors) is None
    assert errors == ["err_num_min_amount", "err_int_min_days"]
    for e in errors:
        assert not _CYR.search(e), e


def test_no_russian_literals_left_in_form_parser():
    """Guard: no Russian string literals remain in `forms.py`.

    A predicate over the ast, not a list: the next error written as a Russian phrase
    will go red by itself. Docstrings and comments do not count — they do not go to the client.
    """
    src = pathlib.Path(forms.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docs.add(doc)
    leaks = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and n.value not in docs
        and _CYR.search(n.value)
    ]
    assert not leaks, f"Russian literals in the form parser: {leaks}"
