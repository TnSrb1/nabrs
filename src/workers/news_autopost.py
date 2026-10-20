"""Automatic publication of news ``/novosti`` to the Telegram channel (owner decision 16.10.2026).

Source — :data:`src.api.public.news.NEWS`. The channel is run in **English** (owner decision 17.10.2026):
the post takes ``title["en"]`` and ``body["en"]``, the link is ``/novosti/<slug>?lang=en``. If ``en``
is empty, the post falls back to ``sr`` — the fallback is EXPLICIT in :func:`build_post`, not a module-level default.
Every N minutes the publisher takes slugs that are not in ``channel_posts`` (0056) and sends each
``sendMessage`` to the channel (``TG_NEWS_CHANNEL``, default ``@nabrs_news``): **title** +
the first sentences + link. Enabled by the ``TG_NEWS_AUTOPOST`` flag (off by default).

**Publication date.** A news item with ``published`` LATER than "today" on the Belgrade calendar
(:func:`belgrade_today`) is not published and not recorded in ``channel_posts`` — it goes out on its own
day. Seeding an empty journal also skips such news items.

Why in the ``ingest`` service: the SERVICE bot @tendersrb_bot publishes (owner decision 16.10 —
to separate publishing from the user-facing ``@nabrs_bot``), and ``ADMIN_BOT_TOKEN`` is already passed in compose to
``ingest`` (and ``ai_spend_report``/``flow_report``; the ``bot`` service does NOT get it).
``ingest`` is a long-running process; a separate service is not needed. ``TG_NEWS_*`` are passed into
the ``environment:`` of the ``ingest`` service in ``deploy/docker-compose.yml`` (without them the variables do not reach the container).
``ingest`` connects as the superuser ``tendersrb`` (table owner); ``tendersrb_app`` on
``channel_posts`` has ``SELECT`` only (0056).

Guarantees:

* **The archive is not dumped into the channel.** Empty ``channel_posts`` → all slugs already due
  are seeded as "already published" (``message_id`` NULL); only the new ones after that are published.
* **A future date does not go out early** (the date rule above).
* **Idempotency.** Unit of work = one news item in one transaction: advisory lock →
  re-check → ``sendMessage`` → ``INSERT`` → commit. Telegram error → rollback, the record
  is not created, retry in the next cycle; the cycle stops at the first error (publication
  order is preserved). A process crash BETWEEN sending and commit gives a duplicate post — a window
  of milliseconds, accepted.
* **Secrets.** The token is part of the URL (``/bot<token>/sendMessage``). Only
  ``type(exc).__name__`` and the response code go to the log; the httpx exception text (contains the URL) does not go to the log, and
  ``raise_for_status`` is not used.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import text

from src.api.public.news import NEWS, NewsItem
from src.db.session import session_scope
from src.domain.tz import to_local

logger = logging.getLogger(__name__)

DEFAULT_CHANNEL = "@nabrs_news"
DEFAULT_INTERVAL_MIN = 15
_TG_API = "https://api.telegram.org"
_HTTP_TIMEOUT = 15.0
NEWS_BASE_URL = "https://app.nab.rs/novosti"
#: Channel language: both the post text and the site-link parameter.
POST_LANG = "en"
#: Sentences in the post and the ceiling per cycle (protection against a flood on a manual cleaning of the log).
SENTENCES_IN_POST = 3
MAX_POSTS_PER_CYCLE = 3
#: An arbitrary constant key of the publisher's advisory lock.
_LOCK_KEY = 5_160_016

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_TRUTHY = {"1", "true", "yes", "on"}

_SEED_SQL = text(
    "INSERT INTO channel_posts (news_slug, chat, message_id) "
    "VALUES (:slug, :chat, NULL) ON CONFLICT (news_slug) DO NOTHING"
)
_COUNT_SQL = text("SELECT count(*) FROM channel_posts")
_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:k)")
_EXISTS_SQL = text("SELECT 1 FROM channel_posts WHERE news_slug = :slug")
_INSERT_SQL = text(
    "INSERT INTO channel_posts (news_slug, chat, message_id) "
    "VALUES (:slug, :chat, :mid) ON CONFLICT (news_slug) DO NOTHING"
)
_POSTED_SQL = text("SELECT news_slug FROM channel_posts")


class TelegramSendError(Exception):
    """A Bot API refusal; ``code`` — the HTTP status (None — network/timeout). The text without the URL/token."""

    def __init__(self, code: int | None) -> None:
        super().__init__(f"telegram send failed code={code}")
        self.code = code


Sender = Callable[[str, str], Awaitable[int | None]]


def admin_token() -> str | None:
    return (os.getenv("ADMIN_BOT_TOKEN") or "").strip() or None


def autopost_enabled() -> bool:
    return (os.getenv("TG_NEWS_AUTOPOST") or "").strip().lower() in _TRUTHY


def news_channel() -> str:
    return (os.getenv("TG_NEWS_CHANNEL") or "").strip() or DEFAULT_CHANNEL


def interval_seconds() -> float:
    try:
        minutes = float(os.getenv("TG_NEWS_INTERVAL_MIN") or DEFAULT_INTERVAL_MIN)
    except ValueError:
        minutes = DEFAULT_INTERVAL_MIN
    return max(minutes, 1.0) * 60.0


def news_url(slug: str) -> str:
    return f"{NEWS_BASE_URL}/{slug}"


def belgrade_today() -> date:
    """Calendar date "today" in Belgrade — the publication boundary (see the module docstring)."""
    return to_local(datetime.now(timezone.utc)).date()


def _due(items: tuple[NewsItem, ...], today: date) -> list[NewsItem]:
    """News that may already be published: ``published`` no later than ``today``."""
    return [n for n in items if n.published <= today]


def _lead(paragraphs: tuple[str, ...], limit: int = SENTENCES_IN_POST) -> list[str]:
    """The first ``limit`` sentences; the paragraphs are kept (the sentences of a paragraph — through a space)."""
    left = limit
    out: list[str] = []
    for paragraph in paragraphs:
        if left <= 0:
            break
        sentences = [s for s in _SENTENCE_SPLIT.split(paragraph.strip()) if s]
        taken = sentences[:left]
        left -= len(taken)
        if taken:
            out.append(" ".join(taken))
    return out


def build_post(item: NewsItem) -> str:
    """Post text (HTML): bold title, beginning of the text, link. en; fallback to sr if en is empty."""
    title_en = item.title.get(POST_LANG, "").strip()
    title = html.escape(title_en or item.title["sr"], quote=False)
    body_en = tuple(p for p in item.body.get(POST_LANG, ()) if p.strip())
    lead = [html.escape(p, quote=False) for p in _lead(body_en or item.body["sr"])]
    link = f"{news_url(item.slug)}?lang={POST_LANG}"
    url = html.escape(link, quote=True)
    return "\n\n".join([f"<b>{title}</b>", *lead, f'<a href="{url}">{url}</a>'])


async def send_via_admin_bot(chat: str, text_html: str) -> int | None:
    """``sendMessage`` from @tendersrb_bot. Returns the message_id; a failure → :class:`TelegramSendError`.

    The httpx/JSON exceptions are swallowed entirely: their text may contain a URL with a token.
    """
    token = admin_token()
    if token is None:
        raise TelegramSendError(None)
    url = f"{_TG_API}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            raise TelegramSendError(resp.status_code)
        return int(resp.json()["result"]["message_id"])
    except TelegramSendError:
        raise
    except Exception:  # noqa: BLE001 — the httpx exception carries a URL with a token
        raise TelegramSendError(None) from None


async def seed_if_empty(
    session_factory: Callable[[], Any] = session_scope,
    *,
    chat: str | None = None,
    news: tuple[NewsItem, ...] | None = None,
) -> int:
    """Empty journal → seed the due slugs as published. Returns the count."""
    chat = chat or news_channel()
    items = _due(NEWS if news is None else news, belgrade_today())
    async with session_factory() as session:
        await session.execute(_LOCK_SQL, {"k": _LOCK_KEY})
        if (await session.execute(_COUNT_SQL)).scalar_one():
            await session.rollback()
            return 0
        for item in items:
            await session.execute(_SEED_SQL, {"slug": item.slug, "chat": chat})
        await session.commit()
    logger.info("news_autopost: the log is empty — slugs seeded: %d", len(items))
    return len(items)


async def publish_once(
    send: Sender = send_via_admin_bot,
    *,
    session_factory: Callable[[], Any] = session_scope,
    news: tuple[NewsItem, ...] | None = None,
    chat: str | None = None,
) -> int:
    """One cycle: seeding (if empty) → publishing the new ones, the old ones first. Returns the number of posts."""
    chat = chat or news_channel()
    items = NEWS if news is None else news
    await seed_if_empty(session_factory, chat=chat, news=items)
    async with session_factory() as session:
        posted = {r[0] for r in (await session.execute(_POSTED_SQL)).all()}
    due = _due(items, belgrade_today())
    todo = [n for n in reversed(due) if n.slug not in posted][:MAX_POSTS_PER_CYCLE]
    sent = 0
    for item in todo:
        async with session_factory() as session:
            # One news item — one transaction: lock → recheck → sending → record.
            await session.execute(_LOCK_SQL, {"k": _LOCK_KEY})
            if (await session.execute(_EXISTS_SQL, {"slug": item.slug})).first():
                await session.rollback()
                continue
            try:
                message_id = await send(chat, build_post(item))
            except asyncio.CancelledError:
                raise
            except (
                Exception
            ) as exc:  # noqa: BLE001 — a failure does not break the bot; a retry in the next cycle
                await session.rollback()
                logger.warning(
                    "news_autopost: не опубликовано slug=%s: %s code=%s",
                    item.slug,
                    type(exc).__name__,
                    getattr(exc, "code", None),
                )
                break
            await session.execute(
                _INSERT_SQL,
                {
                    "slug": item.slug,
                    "chat": chat,
                    "mid": message_id,
                },
            )
            await session.commit()
        sent += 1
        logger.info("news_autopost: published slug=%s", item.slug)
    return sent


async def run_forever(send: Sender = send_via_admin_bot) -> None:
    """The background loop of the bot: ``publish_once`` every interval; a cycle failure does not break the process."""
    interval = interval_seconds()
    logger.info(
        "news_autopost: start (channel=%s, interval=%.0fs)", news_channel(), interval
    )
    while True:
        try:
            await publish_once(send)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("news_autopost: cycle error: %s", type(exc).__name__)
        await asyncio.sleep(interval)
