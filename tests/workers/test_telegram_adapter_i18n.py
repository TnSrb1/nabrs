"""The localisation of the Telegram delivery: the card, the buttons and the digest by ``job.locale``.

Before this fix the notifications in Telegram always went in Russian: the adapter assembled the card with Russian literals and called ``build_tender_keyboard`` without the language, although ``users.locale`` already reached the adapter via the field ``DeliveryJob.locale`` (only the Email adapter used it).

We check exactly the contract of the fix:

- the chrome (the labels of the rows, the labels of the type/procedure, the mark “estimated”, the labels of the buttons) goes in the recipient’s language;
- the DATA (the title, the region, the CPV, the amounts, the dates, the template name) are NOT translated;
- ``callback_data`` does not depend on the language (the same handler catches the presses);
- ``locale=None`` — the previous ru behavior (backward compatibility).

The tender data in the fixture are Serbian (as in production), therefore the predicate “there are no Russian-specific letters U+044B U+044D U+0449 U+044A U+0451 U+0439” measures EXACTLY the chrome, not the content.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.telegram import TelegramAdapter

# The letters that are absent in the Serbian Cyrillic and in the Latin: their presence in the render
# = a Russian string (the same acceptance metric as the render audit of the cabinet).
_RU_ONLY_LETTERS = "ыэщъёй"


class _FakeBot:
    async def send_message(self, **kwargs):  # pragma: no cover — is not called
        return object()


def _job(**over) -> DeliveryJob:
    """A realistic job: the data are Serbian, only the ``locale`` differs."""
    base = dict(
        outbox_id=1,
        tender_id="abc-123",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="555",
        channel_status="active",
        title="Набавка рачунарске опреме",
        source_url="https://jnportal.rs/tender/1",
        amount_eur=1_234_567.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        source_uid="374138",
        source_key="jnportal",
        amount_original=144_809_500.0,
        currency_original="RSD",
        is_amount_estimated=True,
        published_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        procedure_type="open",
        regions=["Београдски округ"],
        contract_type=None,
        classifications=[
            {"scheme": "CPV", "code": "30200000", "label": "Рачунарска опрема"},
            {"scheme": "PLAN", "quarter": "2. квартал", "year": 2026},
        ],
        profile_id="prof-1",
        profile_name="ИТ-опрема",
        locale=None,
    )
    base.update(over)
    return DeliveryJob(**base)


def _buttons(markup) -> list:
    return [b for row in markup.inline_keyboard for b in row]


def _ru_only_letters(text: str) -> set[str]:
    return {ch for ch in text.lower() if ch in _RU_ONLY_LETTERS}


# --------------------------------------------------------------------------- #
#  The card: three languages
# --------------------------------------------------------------------------- #


def test_card_chrome_is_serbian_for_sr_locale():
    """sr: all the labels of the card are Serbian, there are no Russian-specific letters."""
    adapter = TelegramAdapter(_FakeBot())
    text = adapter.format(_job(locale="sr")).text

    assert "📍 Region: Београдски округ" in text
    assert "📦 Postupak: Otvoreni postupak" in text
    assert "🏷 CPV: 30200000 — Рачунарска опрема" in text
    assert "📅 Objavljen: 20.05.2026" in text
    assert "📅 Plan: 2. квартал 2026" in text
    assert "🗓 Rok do: 01.06.2026 16:30" in text
    assert "🏛 Platforma: jnportal" in text
    assert ">Izvor</a>" in text
    assert "🔎 Pronađeno po šablonu: «ИТ-опрема»" in text
    assert "(procenjeno)" in text

    assert not _ru_only_letters(text), f"Russian chrome in the sr card: {text!r}"


def test_card_chrome_is_english_for_en_locale():
    """en: the labels are English (the second language — not only “non-Russian”)."""
    adapter = TelegramAdapter(_FakeBot())
    text = adapter.format(_job(locale="en")).text

    assert "📍 Region: Београдски округ" in text
    assert "📦 Procedure: Open procedure" in text
    assert "📅 Published: 20.05.2026" in text
    assert "🗓 Deadline: 01.06.2026 16:30" in text
    assert "🏛 Platform: jnportal" in text
    assert ">Source</a>" in text
    assert "🔎 Matched by template: «ИТ-опрема»" in text
    assert "(estimated)" in text


def test_card_chrome_stays_russian_without_locale():
    """locale=None → ru: backward compatibility (the previous strings of the card)."""
    adapter = TelegramAdapter(_FakeBot())
    text = adapter.format(_job(locale=None)).text

    assert "📍 Регион: Београдски округ" in text
    assert "📦 Процедура: Открытая процедура" in text
    assert "📅 Опубликован: 20.05.2026" in text
    assert "🗓 Приём до: 01.06.2026 16:30" in text
    assert "🏛 Площадка: jnportal" in text
    assert ">Источник</a>" in text
    assert "🔎 Найдено по шаблону: «ИТ-опрема»" in text
    assert "(оценочно)" in text


def test_card_data_is_not_translated_across_locales():
    """The data of the tender are the same in all the languages — ONLY the chrome is translated."""
    adapter = TelegramAdapter(_FakeBot())
    for locale in (None, "ru", "sr", "en"):
        text = adapter.format(_job(locale=locale)).text
        assert "Набавка рачунарске опреме" in text  # title
        assert "Београдски округ" in text  # region
        assert "Рачунарска опрема" in text  # CPV title
        assert "144 809 500 RSD" in text  # the amount and the currency
        assert "01.06.2026 16:30" in text  # deadline
        assert "ИТ-опрема" in text  # the template name


def test_card_fallbacks_are_localized():
    """The fallbacks “No title”/“(no name)” are chrome too — also by language."""
    adapter = TelegramAdapter(_FakeBot())
    sr = adapter.format(_job(locale="sr", title=None, profile_name=None)).text
    assert "<b>Bez naziva</b>" in sr
    assert "«(bez naziva)»" in sr

    ru = adapter.format(_job(locale=None, title=None, profile_name=None)).text
    assert "<b>Без названия</b>" in ru
    assert "«(без имени)»" in ru


def test_contract_type_label_localized_when_present():
    """The subject type (it displaces the procedure) is translated together with its label."""
    adapter = TelegramAdapter(_FakeBot())
    assert (
        "📦 Vrsta: Radovi"
        in adapter.format(_job(locale="sr", contract_type="works")).text
    )
    assert "📦 Тип: Работы" in adapter.format(_job(contract_type="works")).text


def test_debug_match_block_localized():
    """The debug block of the profile is chrome too (it is visible to the subscriber in the debug mode)."""
    adapter = TelegramAdapter(_FakeBot())
    text = adapter.format(
        _job(locale="sr", debug_match=True, tender_haystack="racunarska oprema")
    ).text
    assert "🐞 Dijagnostika podudaranja:" in text
    assert "• ključne reči:" in text
    assert not _ru_only_letters(text), f"Russian chrome in the sr card: {text!r}"


# --------------------------------------------------------------------------- #
#  The buttons: the labels by language, callback_data — no
# --------------------------------------------------------------------------- #


def test_buttons_are_localized_and_callbacks_unchanged():
    """sr: the labels of the buttons are Serbian, the callback_data are the same as they were in ru."""
    adapter = TelegramAdapter(_FakeBot())
    ru_msg = adapter.format(_job(locale=None))
    sr_msg = adapter.format(_job(locale="sr"))

    sr_texts = [b.text for b in _buttons(sr_msg.reply_markup)]
    # the collapsed card (the common catalog): the funnel + the AI-menu button
    assert sr_texts == [
        "📂 Otvori u kabinetu",
        "⭐ Prati",
        "✖ Sakrij",
        "🧠 AI analiza ▾",
    ]
    assert not _ru_only_letters(" ".join(sr_texts))

    def _datas(msg):
        return [b.callback_data for b in _buttons(msg.reply_markup) if b.callback_data]

    assert _datas(sr_msg) == _datas(ru_msg)
    assert all(len(cb.encode()) <= 64 for cb in _datas(sr_msg))


def test_debug_configure_button_localized():
    """The link-button “Configure the template” (the debug mode) — also by language."""
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    buttons = _buttons(adapter.format(_job(locale="sr", debug_match=True)).reply_markup)
    configure = [b for b in buttons if b.url and "/edit" in b.url]
    assert [b.text for b in configure] == ["⚙️ Podesi ovaj šablon"]
    assert configure[0].url == "https://nab.rs/profiles/prof-1/edit"


def test_buttons_stay_russian_without_locale():
    """locale=None → the previous ru labels of the buttons (backward compatibility)."""
    adapter = TelegramAdapter(_FakeBot())
    texts = [b.text for b in _buttons(adapter.format(_job()).reply_markup)]
    assert texts == ["📂 Открыть в ЛК", "⭐ Следить", "✖ Скрыть", "🧠 AI-разбор ▾"]


# --------------------------------------------------------------------------- #
#  Digest
# --------------------------------------------------------------------------- #


def test_digest_chrome_is_serbian_for_sr_locale():
    """The digest takes the language from the first job of the batch (the batch has one recipient)."""
    adapter = TelegramAdapter(_FakeBot())
    jobs = [
        _job(outbox_id=1, locale="sr", title="Први"),
        _job(outbox_id=2, locale="sr", title="Други"),
    ]
    text = adapter.format_digest(jobs).text

    assert "📬 Sažetak: 2 nova tendera" in text
    assert "Iznos:" in text and "Rok do:" in text
    assert "Pronađeno po šablonu: ИТ-опрема" in text
    assert "Први" in text and "Други" in text  # the names are not translated
    assert not _ru_only_letters(text), f"Russian chrome in the sr digest: {text!r}"


def test_digest_stays_russian_without_locale():
    """A batch without a language → the previous ru digest."""
    adapter = TelegramAdapter(_FakeBot())
    text = adapter.format_digest([_job(title="Первый"), _job(title="Второй")]).text
    assert "📬 Дайджест: 2 новых тендера" in text
    assert "Найдено по шаблону: ИТ-опрема" in text


def test_digest_of_empty_batch_does_not_crash():
    """An empty batch → the ru fallback without an access to jobs[0] (the boundary)."""
    adapter = TelegramAdapter(_FakeBot())
    assert "Дайджест" in adapter.format_digest([]).text


@pytest.mark.parametrize("locale", ["de", "", "SR"])
def test_unknown_locale_falls_back_to_russian(locale):
    """An unknown language code → ru explicitly (the norm of i18n_email.norm_locale)."""
    adapter = TelegramAdapter(_FakeBot())
    assert "📍 Регион:" in adapter.format(_job(locale=locale)).text
