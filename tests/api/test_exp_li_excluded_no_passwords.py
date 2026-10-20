"""Data export: the “will not be included” item does not mention passwords — there are no passwords in the product.

Regression to #713: the sr line wrote “lozinke/linkovi za prijavu”.
"""

from src.api.cabinet.i18n import CABINET

_PASSWORD_WORDS = ("lozink", "парол", "passw")


def test_exp_li_excluded_sr_literal():
    assert CABINET["sr"]["exp_li_excluded"] == (
        "Ne ulaze: linkovi za prijavu i heševi zaštite od zloupotrebe (samo činjenica da postoje), podaci drugih korisnika."
    )


def test_exp_li_excluded_no_password_wording_in_any_locale():
    for lang in ("sr", "ru", "en"):
        text = CABINET[lang]["exp_li_excluded"].lower()
        for word in _PASSWORD_WORDS:
            assert word not in text, (lang, word)
