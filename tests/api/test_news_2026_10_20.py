"""Three news items from 20.10.2026 (BiH, AI reads all formats, open source).

Expectations are LITERALS from the fact (slug, date, link), not via ``NEWS``.
"""

from datetime import date

from src.api.public.news import NEWS, NEWS_BY_SLUG
from src.workers.news_autopost import build_post

# Telegram sendMessage limit — 4096 characters; the post is checked on the finished HTML.
_TG_LIMIT = 4096


def test_three_new_items_head_the_list_with_literal_slugs():
    assert [n.slug for n in NEWS[:3]] == [
        "bih-tenderi",
        "ai-cita-sve-dokumente",
        "otvoreni-kod",
    ]


def test_slugs_are_unique():
    assert len(NEWS_BY_SLUG) == len(NEWS)


def test_new_items_are_dated_2026_10_20():
    for slug in ("bih-tenderi", "ai-cita-sve-dokumente", "otvoreni-kod"):
        assert NEWS_BY_SLUG[slug].published == date(2026, 10, 20)


def test_new_items_have_all_languages_filled():
    for slug in ("bih-tenderi", "ai-cita-sve-dokumente", "otvoreni-kod"):
        item = NEWS_BY_SLUG[slug]
        for lang in ("sr", "en", "ru"):
            assert item.title[lang].strip(), (slug, lang)
            assert item.link_label[lang].strip(), (slug, lang)
            assert len(item.body[lang]) == 2, (slug, lang)
            assert all(p.strip() for p in item.body[lang]), (slug, lang)


def test_github_item_is_external_link():
    item = NEWS_BY_SLUG["otvoreni-kod"]
    assert item.link == "https://github.com/TnSrb1/nabrs"
    assert item.is_external


def test_bih_item_links_cabinet_and_ai_item_links_feed():
    assert NEWS_BY_SLUG["bih-tenderi"].link == "/app"
    assert NEWS_BY_SLUG["ai-cita-sve-dokumente"].link == "/app/feed"


def test_new_items_autopost_en_text_fits_telegram_limit():
    for slug in ("bih-tenderi", "ai-cita-sve-dokumente", "otvoreni-kod"):
        assert len(build_post(NEWS_BY_SLUG[slug])) <= _TG_LIMIT, slug
