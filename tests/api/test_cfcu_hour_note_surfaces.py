"""A note “the source does not specify the closing hour” where the deadline without an hour is shown only as a date.

CFKU (``deadline_time=False``): the hour is not given to us, the DB holds a substituted end
of day. The calendar (agenda) and the CSV “My tenders” print only the date — without a note the
client would take 23:59 for the real deadline. The note is the same key and the same form as in
``tender_detail.html`` (``date (note)``).

The expectations are literals from the fact of the run (not via ``common_t``, so that the test is not a
tautology). Conversion: 21:59Z = 23:59 CEST (20.10.2026, summer time).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from src.api.cabinet import my_tenders_export as ex
from src.api.cabinet.workflow_service import MyTenderExportRow
from src.api.templating import templates

CFCU_DEADLINE = datetime(2026, 10, 20, 21, 59, tzinfo=timezone.utc)
# An ordinary source with an hour: 21:30Z = 23:30 Belgrade.
TIMED_DEADLINE = datetime(2026, 10, 20, 21, 30, tzinfo=timezone.utc)

NOTE_RU = "(Час закрытия источник не указывает)"


def _csv_row(source_key: str, deadline: datetime) -> MyTenderExportRow:
    return MyTenderExportRow(
        tender_id="t1",
        title="Набавка",
        customer_name="Град Београд",
        amount=Decimal("1000.00"),
        currency="RSD",
        deadline_at=deadline,
        tender_url="https://x/1",
        user_status="watching",
        folder_names="",
        source_key=source_key,
    )


def _csv_deadline_cell(raw: bytes) -> str:
    # The deadline column is the fifth (after the title, buyer, amount, currency).
    line = raw.decode("utf-8-sig").split("\r\n")[1]
    return line.split(";")[4]


def test_csv_cfcu_deadline_cell_carries_note_ru():
    raw = ex.build_my_tenders_csv([_csv_row("cfcu", CFCU_DEADLINE)], "ru")
    assert _csv_deadline_cell(raw) == "20.10.2026 (Час закрытия источник не указывает)"


def test_csv_cfcu_deadline_cell_carries_note_sr():
    raw = ex.build_my_tenders_csv([_csv_row("cfcu", CFCU_DEADLINE)], "sr")
    assert _csv_deadline_cell(raw) == "20.10.2026 (Čas zatvaranja izvor ne navodi)"


def test_csv_timed_source_deadline_cell_has_hour_and_no_note():
    raw = ex.build_my_tenders_csv([_csv_row("jnportal", TIMED_DEADLINE)], "ru")
    assert _csv_deadline_cell(raw) == "20.10.2026 23:30"


def _render_agenda(source_key: str, lang: str) -> str:
    e = SimpleNamespace(
        kind="deadline",
        icon="🏛",
        day=CFCU_DEADLINE.date(),
        at=CFCU_DEADLINE,
        source_key=source_key,
        days_left=3,
        title="Тендер",
        title_key=None,
        subtitle="Наручилац",
        subtitle_key=None,
        url=None,
        tender_id=None,
        profile_id=None,
    )
    module = templates.get_template("_agenda_item.html").make_module({})
    return module.agenda_item(e, lang, {})


def test_agenda_cfcu_deadline_shows_note_ru():
    assert "Час закрытия источник не указывает" in _render_agenda("cfcu", "ru")


def test_agenda_cfcu_deadline_shows_note_sr():
    assert "Čas zatvaranja izvor ne navodi" in _render_agenda("cfcu", "sr")


def test_agenda_timed_source_has_no_note():
    html = _render_agenda("jnportal", "ru")
    assert "Час закрытия источник не указывает" not in html
