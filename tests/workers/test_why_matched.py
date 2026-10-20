"""The line “Why this tender is for you” (Matched: words · CPV codes) in the letter and in Telegram.

The literals of the expectations — from the fact (the template word ``gips``, the CPV of the tender ``45421000``), and not through ``t()``/``email_t()``, so that the test is not a tautology.

We check:
- the single letter and the Telegram card show the template word and the CPV of the tender;
- the digest: the line for EACH tender of the batch (Telegram and the letter);
- match_all (no words, no CPV) → there is no line;
- the words/name with ``<b>`` are escaped in Telegram and in the HTML of the letter;
- the limit: no more than 3 words and 2 CPV, the rest «+N».
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.api.emailer import MockEmailSender
from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.email import EmailAdapter
from src.workers.adapters.telegram import TelegramAdapter

_PROFILE_CPV = {"include": [{"system_code": "CPV", "code": "45*"}]}
_TENDER_CPV = [{"scheme": "CPV", "code": "45421000", "label": "Радови на столарији"}]


class _FakeBot:
    async def send_message(self, **kwargs):
        return object()


def _job(**over) -> DeliveryJob:
    base = dict(
        outbox_id=1,
        tender_id="t-1",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="555",
        channel_status="active",
        title="Набавка радова на столарији",
        source_url="https://jnportal.rs/tender/1",
        amount_eur=50000.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        published_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        source_key="jnportal",
        classifications=_TENDER_CPV,
        profile_name="Столарија",
        profile_keywords_include=[{"all_of": ["gips"]}],
        profile_keywords_exclude=None,
        profile_classifications=_PROFILE_CPV,
        tender_haystack="gips radovi na stolariji 45421000",
        locale="ru",
    )
    base.update(over)
    return DeliveryJob(**base)


def _tg() -> TelegramAdapter:
    return TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")


def _email() -> EmailAdapter:
    return EmailAdapter(MockEmailSender(), cabinet_base_url="https://nab.rs")


# --------------------------------------------------------------------------- #
#  A single card
# --------------------------------------------------------------------------- #


def test_telegram_card_shows_matched_word_and_tender_cpv():
    text = _tg().format(_job()).text
    assert "Совпало: gips · CPV 45421000" in text


def test_email_single_shows_matched_word_and_tender_cpv_in_text_and_html():
    msg = _email().format(_job(channel="email"))
    assert "Совпало: gips · CPV 45421000" in msg.text
    assert "gips" in msg.html_body and "45421000" in msg.html_body


def test_match_all_has_no_why_line_in_either_channel():
    job = _job(
        profile_keywords_include=None,
        profile_classifications=None,
        channel="email",
    )
    assert "Совпало" not in _tg().format(job).text
    msg = _email().format(job)
    assert "Совпало" not in msg.text
    assert "Совпало" not in msg.html_body


def test_word_and_name_are_escaped_in_telegram_and_email_html():
    over = dict(
        profile_keywords_include=[{"all_of": ["<b>x</b>"]}],
        tender_haystack="<b>x</b> radovi",
        profile_name="<b>Имя</b>",
    )
    tg_text = _tg().format(_job(**over)).text
    assert "&lt;b&gt;x&lt;/b&gt;" in tg_text
    assert "<b>x</b>" not in tg_text
    assert "&lt;b&gt;Имя&lt;/b&gt;" in tg_text

    html_body = _email().format(_job(channel="email", **over)).html_body
    assert "&lt;b&gt;x&lt;/b&gt;" in html_body
    assert "<b>x</b>" not in html_body


def test_why_line_caps_words_at_three_and_cpv_at_two():
    job = _job(
        profile_keywords_include=[{"all_of": ["gips", "beton", "staklo", "zid"]}],
        tender_haystack="gips beton staklo zid radovi",
        classifications=[
            {"scheme": "CPV", "code": "45421000"},
            {"scheme": "CPV", "code": "45421100"},
            {"scheme": "CPV", "code": "45421200"},
        ],
        profile_classifications={"include": [{"system_code": "CPV", "code": "454*"}]},
    )
    text = _tg().format(job).text
    assert "Совпало: gips, beton, staklo +1 · CPV 45421000, 45421100 +1" in text


# --------------------------------------------------------------------------- #
#  The digest: the line for each tender of the batch
# --------------------------------------------------------------------------- #


def test_telegram_digest_has_why_line_for_each_tender():
    jobs = [
        _job(tender_id="t-1", profile_keywords_include=[{"all_of": ["gips"]}]),
        _job(
            tender_id="t-2",
            title="Креч радови",
            tender_haystack="krec radovi 45421000",
            profile_keywords_include=[{"all_of": ["krec"]}],
        ),
    ]
    text = _tg().format_digest(jobs).text
    assert "Совпало: gips · CPV 45421000" in text
    assert "Совпало: krec · CPV 45421000" in text


def test_email_digest_has_why_line_for_each_tender_in_text_and_html():
    jobs = [
        _job(channel="email", tender_id="t-1"),
        _job(
            channel="email",
            tender_id="t-2",
            tender_haystack="krec radovi 45421000",
            profile_keywords_include=[{"all_of": ["krec"]}],
        ),
    ]
    msg = _email().format_digest(jobs)
    assert "Совпало: gips · CPV 45421000" in msg.text
    assert "Совпало: krec · CPV 45421000" in msg.text
    assert "Совпало: gips · CPV 45421000" in msg.html_body
    assert "Совпало: krec · CPV 45421000" in msg.html_body


def test_digest_match_all_has_no_why_line():
    jobs = [_job(profile_keywords_include=None, profile_classifications=None)]
    assert "Совпало" not in _tg().format_digest(jobs).text
