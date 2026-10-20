"""The web card of the tender (tender.html → the procedure_label filter) shows the PIN label.

The public dictionary TENDER_PROCEDURE knows only open/restricted; the other codes must go to the common lexicon pt_<code>, otherwise the client sees the raw "pin".
"""

import pytest

from src.api.public.i18n import procedure_label


@pytest.mark.parametrize(
    "lang,label",
    [
        ("sr", "Prethodno informativno obaveštenje"),
        ("ru", "Предварительное информационное уведомление (PIN)"),
        ("en", "Prior information notice"),
    ],
)
def test_public_card_shows_pin_label(lang, label):
    assert procedure_label("pin", lang) == label


def test_public_card_keeps_own_open_label():
    assert procedure_label("open", "ru") == "Открытая процедура"


def test_public_card_unknown_code_is_returned_as_is():
    assert procedure_label("art27_exempt", "ru") == "art27_exempt"
