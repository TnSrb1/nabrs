"""The news item “Telegram channel” takes the link from the catalog constant, not from a literal.

The expectation — a literal from the fact (src/plans/catalog.py:22): https://t.me/nabrs_news.
"""

from src.api.public.news import NEWS
from src.plans.catalog import TG_CHANNEL_URL


def test_telegram_news_link_is_channel_constant():
    tg = [n for n in NEWS if "t.me/" in n.link]
    assert len(tg) == 1
    assert tg[0].link == TG_CHANNEL_URL == "https://t.me/nabrs_news"
