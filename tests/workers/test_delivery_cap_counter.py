"""The counter line “Sent today N of M” in the card/letter/digest (owner 16.10).

It is shown from the 80% threshold of the limit (the 24th tender at cap=30), until then — silence. The expectations are LITERALS, not through ``common_t``/``email_t`` (otherwise a tautology).
"""

from __future__ import annotations

import pytest

from src.domain.delivery_cap import should_show_counter, warn_threshold
from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.email import EmailAdapter
from src.workers.adapters.telegram import TelegramAdapter


def _job(sent: int, cap: int = 30, locale: str = "ru", **over) -> DeliveryJob:
    base = dict(
        outbox_id=1,
        tender_id="t1",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc",
        target="555",
        channel_status="active",
        title="Тендер",
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        profile_id="P",
        profile_name="Сервера",
        locale=locale,
        daily_sent=sent,
        daily_cap=cap,
    )
    base.update(over)
    return DeliveryJob(**base)


def test_threshold_is_24_of_30():
    assert warn_threshold(30) == 24
    assert not should_show_counter(23, 30)
    assert should_show_counter(24, 30)
    assert not should_show_counter(24, 0)  # the limit is off — there is no counter


def test_telegram_no_counter_on_23rd_counter_on_24th():
    adapter = TelegramAdapter(object(), cabinet_base_url="https://nab.rs")
    assert "Сегодня отправлено" not in adapter.format(_job(23)).text
    text = adapter.format(_job(24)).text
    assert "Сегодня отправлено 24 из 30 по шаблону «Сервера»" in text


@pytest.mark.parametrize(
    ("locale", "line"),
    [
        ("sr", "Danas poslato 25 od 30 po šablonu „Serveri“"),
        ("en", "Sent today: 25 of 30 for template “Serveri”"),
        ("ru", "Сегодня отправлено 25 из 30 по шаблону «Serveri»"),
    ],
)
def test_telegram_counter_locales(locale, line):
    adapter = TelegramAdapter(object(), cabinet_base_url="https://nab.rs")
    text = adapter.format(_job(25, locale=locale, profile_name="Serveri")).text
    assert line in text


def test_job_without_counter_fields_has_no_line():
    adapter = TelegramAdapter(object(), cabinet_base_url="https://nab.rs")
    assert "отправлено" not in adapter.format(_job(0, cap=0)).text


def test_email_counter_in_footer_from_24th():
    adapter = EmailAdapter(None, cabinet_base_url="https://nab.rs")
    quiet = adapter.format(_job(23, channel="email"))
    assert "Сегодня отправлено" not in quiet.text
    assert "Сегодня отправлено" not in quiet.html_body
    loud = adapter.format(_job(24, channel="email"))
    assert "Сегодня отправлено 24 из 30 по шаблону «Сервера»" in loud.text
    assert "Сегодня отправлено 24 из 30 по шаблону «Сервера»" in loud.html_body
    # the footer: the line goes AFTER the main text of the letter
    assert loud.text.index("Сегодня отправлено") > loud.text.index("Отметить участие")


def test_digest_counter_from_last_job():
    adapter = TelegramAdapter(object(), cabinet_base_url="https://nab.rs")
    jobs = [_job(24, tender_id="a"), _job(24, tender_id="b")]
    assert "Сегодня отправлено 24 из 30" in adapter.format_digest(jobs).text
    quiet = [_job(10, tender_id="a")]
    assert "Сегодня отправлено" not in adapter.format_digest(quiet).text
    mail = EmailAdapter(None, cabinet_base_url="https://nab.rs")
    out = mail.format_digest(jobs)
    assert "Сегодня отправлено 24 из 30" in out.text
    assert "Сегодня отправлено 24 из 30" in out.html_body
