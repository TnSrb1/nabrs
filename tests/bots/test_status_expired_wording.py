"""The text of /status with an expired access does not promise a free extension for 30 days.

The code fact (#657, #705): the extension for up to 30 days — by request, the operator decides.
The wording must match in meaning sub_ended_btn in src/api/cabinet/i18n.py.
"""

import pytest

from src.bots.i18n import bot_t


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("ru", "запросите продление рассылки до 30 дней (решение принимает оператор)"),
        ("sr", "zatražite produženje slanja do 30 dana (odluku donosi operater)"),
        (
            "en",
            "request an extension of delivery by up to 30 days (the operator decides)",
        ),
    ],
)
def test_status_expired_says_request_not_free(locale, expected):
    text = bot_t(locale, "status_access_expired")
    assert expected in text
    assert "бесплатн" not in text.lower()
    assert "free" not in text.lower()
    assert "besplatno" not in text.lower()
