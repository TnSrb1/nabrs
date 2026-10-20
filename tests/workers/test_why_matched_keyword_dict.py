"""Слова, найденные через словарь (``keyword_dict=True``), попадают в «Совпало».

Регрессия (REWORK why-matched): ``match_reason`` искал include только дословно и
без словаря, поэтому тендер, прошедший матч через ``keyword_dict.json``, выходил
с пустым ``matched_keywords`` — строка «Совпало» теряла слово (ревью прода: 34 из
125 доставок с include-словами, 27%).

Ожидания — литералами из факта: термин ``строительство`` → форма словаря
``грађевински`` (сербская кириллица, после свёртки ``gradjevinski``).

Проверяем:
- карточка Telegram со словарным флагом показывает ОРИГИНАЛ слова клиента;
- без флага слова нет (словарь не расширяет матч, которого профиль не включал);
- свойство-зеркало: непустой ``matched_keywords`` ⇔ ``keywords_match`` по словам.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.telegram import TelegramAdapter
from src.workers.matching import keywords_match, match_reason

_HAY_DICT_ONLY = "Грађевински радови на објекту"


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
        title="Радови на објекту",
        source_url="https://jnportal.rs/tender/1",
        amount_eur=50000.0,
        deadline_at=datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc),
        published_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        source_key="jnportal",
        classifications=None,
        profile_name="Строительство",
        profile_keywords_include=[{"all_of": ["строительство"]}],
        profile_keywords_exclude=None,
        profile_classifications=None,
        tender_haystack="gradjevinski radovi na objektu",
        locale="ru",
    )
    base.update(over)
    return DeliveryJob(**base)


def test_dict_matched_word_is_shown_in_telegram_card():
    job = _job(profile_keyword_dict=True)
    text = (
        TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs").format(job).text
    )
    assert "Совпало: строительство" in text


def test_dict_word_is_not_shown_without_dict_flag():
    job = _job(profile_keyword_dict=False)
    text = (
        TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs").format(job).text
    )
    assert "Совпало" not in text


def test_match_reason_keeps_original_word_for_dict_match():
    reason = match_reason(
        [{"all_of": ["строительство"]}],
        None,
        _HAY_DICT_ONLY,
        keyword_dict_enabled=True,
    )
    assert reason.matched_keywords == ["строительство"]


# Pairs (include, exclude, haystack, flag). The set covers both outcomes and dict/without dict.
_MIRROR_CASES = [
    ([{"all_of": ["строительство"]}], None, _HAY_DICT_ONLY, True),
    ([{"all_of": ["строительство"]}], None, _HAY_DICT_ONLY, False),
    ([{"all_of": ["строительство"]}], None, "nabavka opreme", True),
    ([{"all_of": ["строительство"]}], None, "nabavka opreme", False),
    ([{"all_of": ["gips"]}], None, "gips radovi", True),
    ([{"all_of": ["gips"]}], None, "suvi malter", True),
    ([{"all_of": ["gips"]}], None, "suvi malter", False),
    ([{"all_of": ["строительство", "радови"]}], None, _HAY_DICT_ONLY, True),
    ([{"all_of": ["строительство", "радови"]}], None, "radovi na putu", True),
    ([{"all_of": ["строительство", "радови"]}], None, "radovi na putu", False),
    ([{"all_of": ["строительство"]}], ["строительство"], _HAY_DICT_ONLY, True),
    ([{"all_of": ["gips"]}], ["put"], "gips i put", True),
    ([{"all_of": ["gips"]}], ["put"], "gips radovi", True),
    ([{"all_of": ["gips"]}], ["put"], "gips radovi", False),
    ([{"all_of": ["vrata"]}], None, "prozori i vrata", True),
    ([{"all_of": ["vrata"]}], None, "prozori", True),
    ([{"all_of": ["vrata"]}], None, "prozori", False),
]


def test_match_reason_mirrors_keywords_match_on_words():
    outcomes = set()
    for include, exclude, hay, flag in _MIRROR_CASES:
        by_words = keywords_match(include, exclude, hay, keyword_dict_enabled=flag)
        reason = match_reason(include, exclude, hay, keyword_dict_enabled=flag)
        assert bool(reason.matched_keywords) == by_words, (include, exclude, hay, flag)
        outcomes.add((flag, by_words))
    # The set is not degenerate: both outcomes for both values of the flag occur.
    assert outcomes == {(True, True), (True, False), (False, True), (False, False)}
