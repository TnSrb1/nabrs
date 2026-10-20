"""The RFC 5545 format of the calendar of the submission deadline (src/domain/ics.py).

The expectations — literals from the fact, not from the same function as the code.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.domain import ics
from src.domain.ics import TenderEvent, build_calendar, escape_text, fold_line

NOW = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)
TID = "11111111-1111-4111-8111-111111111111"
URL = f"https://nab.rs/tender/{TID}"


def _event(**kw) -> TenderEvent:
    base = dict(
        tender_id=TID,
        title="Nabavka opreme",
        deadline=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc),
        url=URL,
        customer=None,
    )
    base.update(kw)
    return TenderEvent(**base)


def _unfold(text: str) -> list[str]:
    """The reverse operation of RFC: CRLF + a space is glued into nothing."""
    return text.replace("\r\n ", "").split("\r\n")


def test_dtstart_is_utc_with_z_suffix():
    out = build_calendar([_event()], NOW)
    assert "DTSTART:20261105T100000Z" in out.split("\r\n")


def test_dtend_is_one_hour_after_deadline():
    out = build_calendar([_event()], NOW)
    assert "DTEND:20261105T110000Z" in out.split("\r\n")


def test_uid_and_url_literal():
    lines = build_calendar([_event()], NOW).split("\r\n")
    assert f"UID:tender-{TID}@nab.rs" in lines
    assert f"URL:{URL}" in lines


def test_summary_escapes_comma_and_semicolon():
    out = _unfold(build_calendar([_event(title="Oprema, sto; test")], NOW))
    assert r"SUMMARY:Rok: Oprema\, sto\; test" in out


def test_summary_title_capped_at_120_chars():
    out = _unfold(build_calendar([_event(title="a" * 300)], NOW))
    summary = next(x for x in out if x.startswith("SUMMARY:"))
    assert summary == "SUMMARY:Rok: " + "a" * 120


def test_description_has_customer_and_card_link():
    out = _unfold(build_calendar([_event(customer="ОШ Вук, Београд")], NOW))
    assert rf"DESCRIPTION:Naručilac: ОШ Вук\, Београд\n{URL}" in out


def test_description_without_customer_has_only_link():
    out = _unfold(build_calendar([_event()], NOW))
    assert f"DESCRIPTION:{URL}" in out


def test_two_alarms_one_and_three_days_before():
    out = build_calendar([_event()], NOW).split("\r\n")
    assert out.count("BEGIN:VALARM") == 2
    assert "TRIGGER:-P3D" in out
    assert "TRIGGER:-P1D" in out
    assert all(x == "ACTION:DISPLAY" for x in out if x.startswith("ACTION:"))


def test_calendar_envelope_and_dtstamp():
    lines = build_calendar([_event()], NOW).split("\r\n")
    assert lines[0] == "BEGIN:VCALENDAR"
    assert "VERSION:2.0" in lines
    assert "DTSTAMP:20261017T080000Z" in lines
    assert lines[-2] == "END:VCALENDAR"


def test_empty_list_is_valid_calendar_without_events():
    out = build_calendar([], NOW)
    assert out.startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" not in out
    assert out.endswith("END:VCALENDAR\r\n")


def test_lines_use_crlf_only():
    out = build_calendar([_event()], NOW)
    assert "\n" not in out.replace("\r\n", "")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a,b", r"a\,b"),
        ("a;b", r"a\;b"),
        ("a\\b", r"a\\b"),
        ("a\nb", r"a\nb"),
        ("a\r\nb", r"a\nb"),
    ],
)
def test_escape_text(raw, expected):
    assert escape_text(raw) == expected


def test_escape_backslash_first_no_double_escape():
    # the backslash is escaped before the comma: «\,» does not turn into «\\\,»
    assert escape_text("\\,") == r"\\\,"


def test_fold_short_line_untouched():
    assert fold_line("DTSTART:20261105T100000Z") == "DTSTART:20261105T100000Z"


def test_fold_long_ascii_line_at_75_octets():
    line = "X" * 100
    folded = fold_line(line)
    first, second = folded.split("\r\n ")
    assert len(first.encode()) == 75
    assert len(second.encode()) == 25
    assert _unfold(folded) == [line]


def test_fold_never_splits_utf8_sequence():
    line = "ž" * 60  # 120 octets, 2 per character
    folded = fold_line(line)
    for part in folded.split("\r\n "):
        part.encode("utf-8")  # decodes without errors
        assert len(part.encode()) <= 75
    assert "".join(folded.split("\r\n ")) == line


def test_every_output_line_within_75_octets():
    out = build_calendar(
        [_event(title="Набавка опреме и услуге ž š č " * 10, customer="Č" * 90)],
        NOW,
    )
    for raw in out.split("\r\n"):
        assert len(raw.encode()) <= 75


def test_module_exposes_fixed_limits():
    # protection against a silent edit of the constants: the contract of RFC and of the reminders
    assert ics.MAX_LINE_OCTETS == 75
    assert ics.ALARM_DAYS_BEFORE == (3, 1)


# --- a deadline without an hour (CFKU: deadline_time=False) → the event for the whole day -------

CFCU_DEADLINE = datetime(2026, 11, 17, 22, 59, tzinfo=timezone.utc)  # 23:59 Belgrade


def test_date_only_deadline_is_all_day_event():
    """The deadline 2026-11-17 23:59 Europe/Belgrade without an hour → DATE, without an invented hour."""
    out = build_calendar([_event(deadline=CFCU_DEADLINE, has_time=False)], NOW)
    lines = out.split("\r\n")
    assert "DTSTART;VALUE=DATE:20261117" in lines
    assert "DTEND;VALUE=DATE:20261118" in lines
    assert not any(x.startswith("DTSTART:") for x in lines)
    assert not any(x.startswith("DTEND:") for x in lines)
    assert "T235900" not in out
    assert "T225900Z" not in out


def test_date_only_day_is_belgrade_day_not_utc_day():
    """00:30 Belgrade time on the 18th = 23:30Z on the 17th: the day of the event — the 18th in Belgrade."""
    deadline = datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc)
    lines = build_calendar([_event(deadline=deadline, has_time=False)], NOW).split(
        "\r\n"
    )
    assert "DTSTART;VALUE=DATE:20261118" in lines


def test_date_only_keeps_reminders_and_uid():
    lines = build_calendar([_event(deadline=CFCU_DEADLINE, has_time=False)], NOW).split(
        "\r\n"
    )
    assert "TRIGGER:-P3D" in lines
    assert "TRIGGER:-P1D" in lines
    assert f"UID:tender-{TID}@nab.rs" in lines


def test_deadline_with_hour_keeps_utc_datetime():
    """jnportal (with an hour): the previous view of DTSTART/DTEND in UTC with the suffix Z."""
    out = build_calendar([_event(has_time=True)], NOW).split("\r\n")
    assert "DTSTART:20261105T100000Z" in out
    assert "DTEND:20261105T110000Z" in out
    assert not any("VALUE=DATE" in x for x in out)


# --- escaping of TEXT: control characters are stripped ----------------------


def test_escape_text_strips_control_characters():
    assert escape_text("a\x00b\x08c\x0bd\x0ce\x0ef\x1fg") == "abcdefg"


def test_escape_text_keeps_tab_and_newline_escaped():
    assert escape_text("x\ty\nz") == "x\ty\\nz"
