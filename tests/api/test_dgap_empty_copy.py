"""Warning "channel not selected" (dgap_empty_*): honest text about the cabinet (#820).

The web_only row is already visible in the cabinet, and for Telegram/e-mail a template without a channel
does not send — the old "tenders do not arrive" was a lie. Expectations are literals from the text,
not via ``t()``.
"""

from __future__ import annotations

import pytest

from src.api.cabinet.i18n import CABINET


@pytest.mark.parametrize(
    ("lang", "title"),
    [
        ("ru", "Канал доставки не выбран"),
        ("sr", "Kanal isporuke nije izabran"),
        ("en", "No delivery channel selected"),
    ],
)
def test_empty_channel_title_is_short_literal(lang, title):
    # Heading — one short line at 375px (measurement: a long one gave 3 lines);
    # the detail "only in the cabinet" moved into the body.
    assert CABINET[lang]["dgap_empty_title"] == title


@pytest.mark.parametrize(
    ("lang", "fragment"),
    [
        ("ru", "видны только в кабинете, в Telegram и на e-mail не приходят"),
        ("sr", "vidljivi samo u kabinetu, na Telegram i e-poštu ne stižu"),
        ("en", "visible only in the cabinet"),
        ("en", "aren’t sent to Telegram or email"),
    ],
)
def test_empty_channel_body_says_cabinet_only(lang, fragment):
    assert fragment in CABINET[lang]["dgap_empty_body"]


@pytest.mark.parametrize("lang", ["ru", "sr", "en"])
def test_empty_channel_copy_no_longer_claims_never_arrive(lang):
    title = CABINET[lang]["dgap_empty_title"]
    body = CABINET[lang]["dgap_empty_body"]
    # "tenders do not arrive" as a whole (a lie about the cabinet) — forbidden; "in Telegram and by e-mail
    # do not arrive" — an honest wording, so the guard catches only the bare phrase.
    assert "тендеры не приходят" not in title + body
    assert "tenderi ne stižu" not in title + body
    assert "tenders aren’t arriving" not in title + body
