"""The text of the post, the escaping, the secrets in the logs (without a DB) — src/workers/news_autopost.py.

The expectations — a literal from a real news item ``prethodna-obavestenja``, not through the code. The token is synthetic.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx
import pytest

from src.api.public.news import NEWS, NewsItem
from src.workers import news_autopost as na

SYNTH_TOKEN = "123456:AAF_SYNTH_TEST_TOKEN"

REAL = next(n for n in NEWS if n.slug == "prethodna-obavestenja")

REAL_POST = (
    "<b>New notice types in TenderSRB</b>\n\n"
    "TenderSRB now also shows new notice types from the public procurement portal: "
    "prior information notices (PIN), defence and security notices (F17), "
    "social services calls (F21) and qualification system notices (F07).\n\n"
    "A PIN lets you see a procurement plan in advance, so you can prepare before the "
    "tender is published. PIN and qualification systems have no submission deadline, "
    "so the card says the deadline is not specified for them.\n\n"
    '<a href="https://app.nab.rs/novosti/prethodna-obavestenja?lang=en">'
    "https://app.nab.rs/novosti/prethodna-obavestenja?lang=en</a>"
)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calendar "today" in Belgrade is fixed: tests do not depend on the clock."""
    monkeypatch.setattr(na, "belgrade_today", lambda: date(2026, 10, 18))


def _item(title: str, paragraphs: tuple[str, ...], slug: str = "x") -> NewsItem:
    return NewsItem(
        slug=slug,
        published=date(2026, 10, 16),
        title={"sr": title},
        body={"sr": paragraphs},
        link="/x",
        link_label={"sr": "x"},
    )


def _item_en(slug: str = "x") -> NewsItem:
    """Fixture with sr and en: the post must take en."""
    return NewsItem(
        slug=slug,
        published=date(2026, 10, 16),
        title={"sr": "Naslov sr", "en": "English title"},
        body={"sr": ("Srpski tekst.",), "en": ("English text. Second.",)},
        link="/x",
        link_label={"sr": "x", "en": "x"},
    )


def test_post_uses_en_and_lang_link_literal() -> None:
    assert na.build_post(_item_en("x")) == (
        "<b>English title</b>\n\n"
        "English text. Second.\n\n"
        '<a href="https://app.nab.rs/novosti/x?lang=en">'
        "https://app.nab.rs/novosti/x?lang=en</a>"
    )


def test_post_falls_back_to_sr_explicitly_when_en_empty() -> None:
    item = NewsItem(
        slug="x",
        published=date(2026, 10, 16),
        title={"sr": "Naslov sr", "en": "  "},
        body={"sr": ("Srpski tekst.",), "en": ()},
        link="/x",
        link_label={"sr": "x"},
    )
    assert na.build_post(item) == (
        "<b>Naslov sr</b>\n\n"
        "Srpski tekst.\n\n"
        '<a href="https://app.nab.rs/novosti/x?lang=en">'
        "https://app.nab.rs/novosti/x?lang=en</a>"
    )


def test_post_text_real_news_literal() -> None:
    assert na.build_post(REAL) == REAL_POST


def test_post_takes_only_first_three_sentences() -> None:
    item = _item("T", ("Jedan. Dva! Tri? Četiri.", "Pet."))
    assert na.build_post(item) == (
        "<b>T</b>\n\nJedan. Dva! Tri?\n\n"
        '<a href="https://app.nab.rs/novosti/x?lang=en">'
        "https://app.nab.rs/novosti/x?lang=en</a>"
    )


def test_post_escapes_html() -> None:
    item = _item("A <b>&</b> B", ("Cena < 5 & > 3. <script>x</script>",), slug="a&b")
    post = na.build_post(item)
    assert post.startswith("<b>A &lt;b&gt;&amp;&lt;/b&gt; B</b>\n\n")
    assert "Cena &lt; 5 &amp; &gt; 3. &lt;script&gt;x&lt;/script&gt;" in post
    assert post.endswith(
        '<a href="https://app.nab.rs/novosti/a&amp;b?lang=en">'
        "https://app.nab.rs/novosti/a&amp;b?lang=en</a>"
    )
    assert "<script>" not in post


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_NEWS_AUTOPOST", raising=False)
    assert na.autopost_enabled() is False
    monkeypatch.setenv("TG_NEWS_AUTOPOST", "1")
    assert na.autopost_enabled() is True
    monkeypatch.delenv("TG_NEWS_CHANNEL", raising=False)
    assert na.news_channel() == "@nabrs_news"


class _Resp:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body


def _fake_client(resp: _Resp | Exception, seen: list):
    class _Client:
        def __init__(self, *a, **k) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            seen.append((url, json))
            if isinstance(resp, Exception):
                raise resp
            return resp

    return _Client


async def test_send_ok_returns_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_BOT_TOKEN", SYNTH_TOKEN)
    seen: list = []
    resp = _Resp(200, {"ok": True, "result": {"message_id": 77}})
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(resp, seen))
    assert await na.send_via_admin_bot("@nabrs_news", "hi") == 77
    assert seen == [
        (
            f"https://api.telegram.org/bot{SYNTH_TOKEN}/sendMessage",
            {
                "chat_id": "@nabrs_news",
                "text": "hi",
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
    ]


async def test_send_without_token_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_BOT_TOKEN", raising=False)
    with pytest.raises(na.TelegramSendError) as ei:
        await na.send_via_admin_bot("@nabrs_news", "hi")
    assert ei.value.code is None


@pytest.mark.parametrize(
    "resp,code",
    [
        (_Resp(403), 403),
        (_Resp(429), 429),
        (httpx.ConnectError(f"boom https://api.telegram.org/bot{SYNTH_TOKEN}/x"), None),
    ],
)
async def test_send_failure_hides_token(
    monkeypatch: pytest.MonkeyPatch, resp, code
) -> None:
    monkeypatch.setenv("ADMIN_BOT_TOKEN", SYNTH_TOKEN)
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(resp, []))
    with pytest.raises(na.TelegramSendError) as ei:
        await na.send_via_admin_bot("@nabrs_news", "hi")
    assert ei.value.code == code
    assert SYNTH_TOKEN not in str(ei.value)
    assert ei.value.__cause__ is None


async def test_no_token_in_caplog_on_failed_cycle(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The full cycle with a real sender and a network failure: there is no token in the log, there are the class name and the code."""
    monkeypatch.setenv("ADMIN_BOT_TOKEN", SYNTH_TOKEN)
    err = httpx.ConnectError(f"https://api.telegram.org/bot{SYNTH_TOKEN}/sendMessage")
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(err, []))

    class _Sess:
        def __init__(self) -> None:
            self.n = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, params=None):
            self.n += 1

            class _R:
                def scalar_one(self_):
                    return 1  # the log is not empty → there is no seeding

                def all(self_):
                    return []  # nothing was published

                def first(self_):
                    return None

            return _R()

        async def rollback(self) -> None: ...
        async def commit(self) -> None:
            raise AssertionError("commit on a Telegram failure is forbidden")

    with caplog.at_level(logging.DEBUG):
        sent = await na.publish_once(
            na.send_via_admin_bot,
            session_factory=_Sess,
            news=(REAL,),
            chat="@nabrs_news",
        )
    assert sent == 0
    assert SYNTH_TOKEN not in caplog.text
    assert (
        "news_autopost: не опубликовано slug=prethodna-obavestenja: "
        "TelegramSendError code=None"
    ) in caplog.text
