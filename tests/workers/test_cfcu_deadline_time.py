"""A deadline without an hour (CFKU, ``deadline_time=False``): the date + a mark, without an invented hour.

The expectations — literals from the fact of the run (not through ``common_t``/``email_t``, so that the test is not a tautology). The conversion: 21:59Z = 23:59 CEST (20.10.2026, summer time).
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.parsers.capabilities import capabilities_by_key, deadline_has_time
from src.workers.adapters.base import format_deadline
from src.workers.reminders import build_reminder_text

# closingDate «2026-10-20» → our substitution 23:59 Belgrade = 21:59Z.
CFCU_DEADLINE = datetime(2026, 10, 20, 21, 59, tzinfo=timezone.utc)
# An ordinary source with an hour: 21:30Z = 23:30 Belgrade.
TIMED_DEADLINE = datetime(2026, 10, 20, 21, 30, tzinfo=timezone.utc)

NOTE_RU = "Час закрытия источник не указывает"
NOTE_SR = "Čas zatvaranja izvor ne navodi"
NOTE_EN = "Closing hour is not stated by the source"


def test_cfcu_declares_date_only_and_other_sources_keep_time():
    caps = capabilities_by_key()
    assert caps["cfcu"].deadline_time is False
    others = {k: c.deadline_time for k, c in caps.items() if k != "cfcu"}
    assert others, "the registry of adapters is empty — the test makes no sense"
    assert all(others.values()), {k: v for k, v in others.items() if not v}


def test_unknown_or_missing_source_keeps_time():
    assert deadline_has_time("cfcu") is False
    assert deadline_has_time("jnportal") is True
    assert deadline_has_time("no-such-source") is True
    assert deadline_has_time(None) is True


def test_format_deadline_cfcu_is_date_with_note_ru():
    assert (
        format_deadline(CFCU_DEADLINE, source_key="cfcu", note=NOTE_RU)
        == "20.10.2026 (Час закрытия источник не указывает)"
    )


def test_format_deadline_cfcu_without_note_is_bare_date():
    assert format_deadline(CFCU_DEADLINE, source_key="cfcu") == "20.10.2026"


def test_format_deadline_timed_source_keeps_hour_and_ignores_note():
    assert (
        format_deadline(TIMED_DEADLINE, source_key="jnportal", note=NOTE_RU)
        == "20.10.2026 23:30"
    )
    assert format_deadline(TIMED_DEADLINE) == "20.10.2026 23:30"


def test_reminder_cfcu_shows_date_and_note_per_locale():
    def line(locale: str) -> str:
        msg = build_reminder_text(
            3,
            title="Nabavka",
            deadline_at=CFCU_DEADLINE,
            source_url=None,
            locale=locale,
            source_key="cfcu",
        )
        return next(x for x in msg.split("\n") if x.startswith("🗓"))

    assert line("ru") == "🗓 Приём до 20.10.2026 (Час закрытия источник не указывает)"
    assert line("sr") == "🗓 Rok do 20.10.2026 (Čas zatvaranja izvor ne navodi)"
    assert (
        line("en") == "🗓 Deadline 20.10.2026 (Closing hour is not stated by the source)"
    )


def test_reminder_timed_source_unchanged():
    msg = build_reminder_text(
        3,
        title="Nabavka",
        deadline_at=TIMED_DEADLINE,
        source_url=None,
        locale="ru",
        source_key="jnportal",
    )
    assert "🗓 Приём до 20.10.2026 23:30" in msg
    assert NOTE_RU not in msg


def test_web_filter_cfcu_date_with_note_and_timed_source_unchanged():
    from src.api.templating import templates

    tpl = templates.env.from_string("{{ dl | dl_local(src, lang) }}")
    assert (
        tpl.render(dl=CFCU_DEADLINE, src="cfcu", lang="ru")
        == "20.10.2026 (Час закрытия источник не указывает)"
    )
    assert tpl.render(dl=CFCU_DEADLINE, src="cfcu", lang="sr") == (
        "20.10.2026 (Čas zatvaranja izvor ne navodi)"
    )
    assert (
        tpl.render(dl=TIMED_DEADLINE, src="jnportal", lang="ru") == "20.10.2026 23:30"
    )


def test_web_filter_cfcu_without_lang_has_no_hour():
    from src.api.templating import templates

    tpl = templates.env.from_string("{{ dl | dl_local(src) }}")
    assert tpl.render(dl=CFCU_DEADLINE, src="cfcu") == "20.10.2026"
