"""The news publisher against PostgreSQL (migration 0056): seeding of the archive, repeat, rights.

Requires ``TEST_RLS_DATABASE_URL`` / ``TEST_SUPERUSER_DATABASE_URL`` (a throwaway PG;
gate.sh brings it up itself). The sender is a stub, no network is needed. The table ``channel_posts``
is global: the fixture cleans it before and after.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.public.news import NewsItem
from src.workers import news_autopost as na

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")
SUPER_URL = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)

pytestmark = pytest.mark.skipif(
    not TEST_URL, reason="TEST_RLS_DATABASE_URL не задан — news_autopost пропускается"
)

CHAT = "@nabrs_news"


def _n(slug: str, published: date = date(2026, 10, 16)) -> NewsItem:
    return NewsItem(
        slug=slug,
        published=published,
        title={"sr": f"Naslov {slug}"},
        body={"sr": ("Prva rečenica. Druga rečenica.",)},
        link="/x",
        link_label={"sr": "x"},
    )


# NEWS: fresh ones first → published from the oldest to the newest.
ARCHIVE = (_n("c"), _n("b"), _n("a"))


class FakeSender:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.sent: list[str] = []

    async def __call__(self, chat: str, text_html: str) -> int | None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise na.TelegramSendError(500)
        self.sent.append(text_html)
        return 1000 + len(self.sent)


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Today" in Belgrade is fixed: tests do not depend on the clock."""
    monkeypatch.setattr(na, "belgrade_today", lambda: date(2026, 10, 18))


@pytest_asyncio.fixture
async def env():
    eng = create_async_engine(SUPER_URL, poolclass=NullPool, future=True)
    async with eng.begin() as c:
        await c.execute(text("DELETE FROM channel_posts"))
    factory = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    yield eng, factory
    async with eng.begin() as c:
        await c.execute(text("DELETE FROM channel_posts"))
    await eng.dispose()


async def _rows(eng):
    async with eng.connect() as c:
        r = await c.execute(
            text(
                "SELECT news_slug, chat, message_id FROM channel_posts ORDER BY news_slug"
            )
        )
        return [tuple(x) for x in r.all()]


async def test_first_run_seeds_archive_and_posts_nothing(env) -> None:
    eng, factory = env
    sender = FakeSender()
    sent = await na.publish_once(
        sender, session_factory=factory, news=ARCHIVE, chat=CHAT
    )
    assert sent == 0
    assert sender.sent == []
    assert await _rows(eng) == [("a", CHAT, None), ("b", CHAT, None), ("c", CHAT, None)]


async def test_new_news_after_seed_is_posted_once(env) -> None:
    eng, factory = env
    sender = FakeSender()
    await na.publish_once(sender, session_factory=factory, news=ARCHIVE, chat=CHAT)
    fresh = (_n("d"), *ARCHIVE)
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 1
    )
    assert len(sender.sent) == 1 and "Naslov d" in sender.sent[0]
    # a repeated cycle — no duplicate
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 0
    )
    assert len(sender.sent) == 1
    assert (await _rows(eng))[-1] == ("d", CHAT, 1001)


async def test_telegram_error_not_recorded_and_retried(env) -> None:
    eng, factory = env
    sender = FakeSender()
    await na.publish_once(sender, session_factory=factory, news=ARCHIVE, chat=CHAT)
    fresh = (_n("e"), _n("d"), *ARCHIVE)
    sender.fail_times = 1
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 0
    )
    assert sender.sent == []
    assert {r[0] for r in await _rows(eng)} == {"a", "b", "c"}  # nothing is written
    # the next cycle: both go out, the old one first
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 2
    )
    assert ["Naslov d" in sender.sent[0], "Naslov e" in sender.sent[1]] == [True, True]


async def test_app_role_has_select_only_on_channel_posts(env) -> None:
    eng = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    try:
        async with eng.begin() as c:
            await c.execute(text("SELECT count(*) FROM channel_posts"))
        with pytest.raises(DBAPIError):
            async with eng.begin() as c:
                await c.execute(
                    text("INSERT INTO channel_posts (news_slug, chat) VALUES ('z','c')")
                )
    finally:
        await eng.dispose()


async def test_future_news_seed_skipped_and_not_recorded(env) -> None:
    eng, factory = env
    sender = FakeSender()
    tomorrow = _n("t", date(2026, 10, 19))
    sent = await na.publish_once(
        sender, session_factory=factory, news=(tomorrow, *ARCHIVE), chat=CHAT
    )
    assert sent == 0
    assert await _rows(eng) == [("a", CHAT, None), ("b", CHAT, None), ("c", CHAT, None)]


async def test_news_dated_tomorrow_not_sent_then_sent_on_its_day(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng, factory = env
    sender = FakeSender()
    await na.publish_once(sender, session_factory=factory, news=ARCHIVE, chat=CHAT)
    fresh = (_n("t", date(2026, 10, 19)), *ARCHIVE)
    # 18.10 Belgrade time: "tomorrow" is not sent and there is no record
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 0
    )
    assert sender.sent == []
    assert "t" not in {r[0] for r in await _rows(eng)}
    # 19.10 has come — it goes out exactly once.
    monkeypatch.setattr(na, "belgrade_today", lambda: date(2026, 10, 19))
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 1
    )
    assert len(sender.sent) == 1 and "Naslov t" in sender.sent[0]
    assert ("t", CHAT, 1001) in await _rows(eng)


async def test_news_dated_today_is_sent(env) -> None:
    eng, factory = env
    sender = FakeSender()
    await na.publish_once(sender, session_factory=factory, news=ARCHIVE, chat=CHAT)
    fresh = (_n("s", date(2026, 10, 18)), *ARCHIVE)
    assert (
        await na.publish_once(sender, session_factory=factory, news=fresh, chat=CHAT)
        == 1
    )
    assert ("s", CHAT, 1001) in await _rows(eng)
