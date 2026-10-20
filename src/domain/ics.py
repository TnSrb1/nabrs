"""An iCalendar (RFC 5545) generator for “Add to calendar” — the tender submission deadline.

Pure functions: no DB or HTTP. The routes (the public card ``/tender/{id}.ics`` and
the cabinet ``/app/tenders/calendar.ics``) assemble :class:`TenderEvent` and return
the result of :func:`build_calendar`. The format is here, so it can be checked
without a running application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.domain.tz import to_local

CRLF = "\r\n"
#: RFC 5545 §3.1: a content line is no longer than 75 octets (before CRLF).
MAX_LINE_OCTETS = 75
#: A line continuation starts with a space — it holds one useful octet fewer.
MAX_CONTINUATION_OCTETS = MAX_LINE_OCTETS - 1
#: The event duration: the deadline is a moment, the calendar needs a visible block.
EVENT_DURATION = timedelta(hours=1)
#: Reminders N days before the deadline (TRIGGER -P<N>D relative to DTSTART).
ALARM_DAYS_BEFORE = (3, 1)
SUMMARY_TITLE_MAX = 120
PRODID = "-//nab.rs//tendersrb deadline-ics//SR"
#: Control characters that do not belong in TEXT (except \t, \n, \r — escaping will handle them).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


@dataclass(frozen=True)
class TenderEvent:
    """The submission deadline of one tender — everything a calendar event needs."""

    tender_id: str
    title: str
    deadline: datetime  # aware UTC (in the DB deadline_at is stored in UTC)
    url: str
    customer: str | None = None
    #: ``False`` — the source gives only a date (CFCU): an all-day event, without an hour.
    has_time: bool = True


def escape_text(value: str) -> str:
    """Escaping a TEXT value (RFC 5545 §3.3.11): ``\\ ; , `` and a line break.

    Control characters (except line breaks) are stripped: they are not allowed in TEXT.
    """
    value = _CONTROL_CHARS.sub("", value)
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def fold_line(line: str) -> str:
    """Line folding for lines longer than 75 octets (RFC 5545 §3.1).

    One may cut between any characters, but not inside a UTF-8 sequence:
    Cyrillic and “č/š/ž” are multi-byte, so we count octets, not characters.
    """
    parts: list[str] = []
    current = ""
    size = 0
    limit = MAX_LINE_OCTETS
    for char in line:
        char_size = len(char.encode("utf-8"))
        if size + char_size > limit:
            parts.append(current)
            current = char
            size = char_size
            limit = MAX_CONTINUATION_OCTETS
        else:
            current += char
            size += char_size
    parts.append(current)
    return (CRLF + " ").join(parts)


def _utc_stamp(moment: datetime) -> str:
    """A moment → ``YYYYMMDDTHHMMSSZ`` (the UTC form with the suffix ``Z``)."""
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _summary(event: TenderEvent) -> str:
    title = (event.title or "").strip()[:SUMMARY_TITLE_MAX]
    return f"Rok: {title}" if title else "Rok"


def _timing_lines(event: TenderEvent) -> list[str]:
    """DTSTART/DTEND: a moment with an hour or a whole day in Belgrade (a deadline without an hour).

    An “all-day” event is a DATE value (RFC 5545 §3.3.4), DTEND is an exclusive
    boundary, i.e. the deadline day + 1. We do not invent an hour.
    """
    if not event.has_time:
        day = to_local(event.deadline).date()
        return [
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
        ]
    return [
        f"DTSTART:{_utc_stamp(event.deadline)}",
        f"DTEND:{_utc_stamp(event.deadline + EVENT_DURATION)}",
    ]


def _event_lines(event: TenderEvent, stamp: datetime) -> list[str]:
    description = [f"Naručilac: {event.customer}"] if event.customer else []
    description.append(event.url)
    summary = escape_text(_summary(event))
    lines = [
        "BEGIN:VEVENT",
        f"UID:tender-{event.tender_id}@nab.rs",
        f"DTSTAMP:{_utc_stamp(stamp)}",
        *_timing_lines(event),
        f"SUMMARY:{summary}",
        f"DESCRIPTION:{escape_text(chr(10).join(description))}",
        # A URI value (RFC 5545 §3.3.13) — without TEXT escaping.
        f"URL:{event.url}",
    ]
    for days in ALARM_DAYS_BEFORE:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{summary}",
            f"TRIGGER:-P{days}D",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_calendar(events: list[TenderEvent], now: datetime) -> str:
    """A VCALENDAR with a VEVENT per tender; ``now`` is the DTSTAMP stamp (RFC: mandatory).

    An empty list is a valid calendar with no events, not an error.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for event in events:
        lines += _event_lines(event, now)
    lines.append("END:VCALENDAR")
    return "".join(fold_line(line) + CRLF for line in lines)
