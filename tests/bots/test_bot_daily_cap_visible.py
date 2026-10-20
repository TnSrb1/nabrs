"""The daily limit is visible in Telegram: /help and the /profile card (owner 16.10).

Assertions — literals; the number comes from ``src.domain.delivery_cap`` (env
``DELIVERY_DAILY_CAP``), therefore the test fixes it with a monkeypatch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.bots.telegram as tg


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setenv("DELIVERY_DAILY_CAP", "30")


@pytest.mark.parametrize(
    ("locale", "line"),
    [
        (
            "ru",
            "До 30 тендеров в день на шаблон в Telegram и на почту; на сайте — все.",
        ),
        (
            "sr",
            "Do 30 tendera dnevno po šablonu u Telegramu i na e-pošti; na sajtu — svi.",
        ),
        (
            "en",
            "Up to 30 tenders a day per template in Telegram and by email; on the website — all of them.",
        ),
    ],
)
def test_help_and_profile_show_cap(locale, line):
    assert line in tg.help_text(locale)
    assert "{cap_line}" not in tg.help_text(locale)
    data = {"tenant": SimpleNamespace(tier="pro", access_until=None)}
    assert line in tg._render_profile(data, [], locale)


def test_cap_zero_hides_line_without_leftover_placeholder(monkeypatch):
    monkeypatch.setenv("DELIVERY_DAILY_CAP", "0")
    for locale in ("ru", "sr", "en"):
        text = tg.help_text(locale)
        assert "{cap_line}" not in text
        assert "\n\n\n" not in text
    data = {"tenant": SimpleNamespace(tier="pro", access_until=None)}
    assert "📬" not in tg._render_profile(data, [], "ru")
