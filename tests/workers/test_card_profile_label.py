"""Unit tests of the label of the profile and of the debug mode in the tender card (EPIC 2).

We check:
- match_reason: the keywords from the first matched group, the CPV prefix via fnmatch
- format: the label of the profile is always present, the fallback when profile_name=None
- format: the debug mode outputs the block of the reason of the match + the button of the template configuration
- format: without debug_match there is no debug block and no configuration button
"""

from __future__ import annotations

from src.workers.adapters.base import DeliveryJob
from src.workers.adapters.telegram import TelegramAdapter
from src.workers.matching import match_reason


class _FakeBot:
    """A mock of aiogram.Bot: records send_message, the network is not called."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
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
        title="Набавка видео опреме",
        source_url=None,
        amount_eur=None,
        deadline_at=None,
    )
    base.update(over)
    return DeliveryJob(**base)


# --------------------------------------------------------------------------- #
#  match_reason
# --------------------------------------------------------------------------- #


def test_match_reason_keywords_from_first_matching_group():
    reason = match_reason(
        [{"all_of": ["video", "montaza"]}], None, "nabavka video opreme montaza"
    )
    assert reason.matched_keywords == ["video", "montaza"]


def test_match_reason_empty_include_returns_empty_keywords():
    reason = match_reason([], None, "anything")
    assert reason.matched_keywords == []


def test_match_reason_cpv_prefix_match():
    profile_cls = {"include": [{"system_code": "eu_cpv", "code": "32*"}]}
    tender_cls = [{"scheme": "CPV", "system_code": "eu_cpv", "code": "32200000"}]
    reason = match_reason([], None, "", tender_cls, profile_cls)
    assert "32*" in reason.matched_cpv


def test_match_reason_no_cpv_when_no_profile_classifications():
    reason = match_reason([], None, "", [{"scheme": "CPV", "code": "32200000"}], None)
    assert reason.matched_cpv == []


# --------------------------------------------------------------------------- #
#  format — the label of the profile
# --------------------------------------------------------------------------- #


def test_format_profile_label_always_shown():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    msg = adapter.format(_job(profile_name="Видеонаблюдение", debug_match=False))
    assert "🔎 Найдено по шаблону: «Видеонаблюдение»" in msg.text


def test_format_profile_label_fallback_when_name_none():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    msg = adapter.format(_job(profile_name=None, debug_match=False))
    assert "🔎 Найдено по шаблону: «(без имени)»" in msg.text


# --------------------------------------------------------------------------- #
#  format — the debug mode
# --------------------------------------------------------------------------- #


def test_format_debug_mode_shows_debug_block_and_button():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    job = _job(
        debug_match=True,
        profile_name="Test",
        profile_id="pid-1",
        profile_keywords_include=[{"all_of": ["video"]}],
        profile_keywords_exclude=None,
        tender_haystack="nabavka video sistem",
        profile_classifications=None,
    )
    msg = adapter.format(job)
    assert "🐞 Отладка совпадения" in msg.text
    assert "ключевые слова: video" in msg.text
    assert "CPV: —" in msg.text

    assert msg.reply_markup is not None
    buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
    configure_buttons = [b for b in buttons if b.text == "⚙️ Настроить этот шаблон"]
    assert configure_buttons, "the button of the template configuration is absent"
    assert "/profiles/pid-1/edit" in configure_buttons[0].url


def test_format_no_debug_mode_no_debug_block_no_configure_button():
    adapter = TelegramAdapter(_FakeBot(), cabinet_base_url="https://nab.rs")
    job = _job(
        debug_match=False,
        profile_name="Test",
        profile_id="pid-1",
    )
    msg = adapter.format(job)
    assert "🔎 Найдено по шаблону" in msg.text
    assert "🐞 Отладка совпадения" not in msg.text

    if msg.reply_markup is not None:
        buttons = [b for row in msg.reply_markup.inline_keyboard for b in row]
        assert all(b.text != "⚙️ Настроить этот шаблон" for b in buttons)
