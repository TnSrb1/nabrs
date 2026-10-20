"""The agenda rows of the cockpit lead INSIDE the product, not to the external portal.

The defect (found by the owner on 25.07): on the dashboard the tender title translation existed only in the
“Current bids” block — there the `_card_title.html` partial is wired in with a toggle.
In “My tasks” and the calendar the title is drawn by the `agenda_item` macro, and the row itself led to
`source_url` (eNabavke). So there was NOWHERE to reach the translation from these
blocks: neither the toggle nor a link to our card.

Now the event leads to `/app/tenders/{id}?profile_id=…` — there are both the
translation toggle, and the AI artifacts, and the “open on the portal” button.
"""

from __future__ import annotations

import pathlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.api.cabinet import feed_service as fs


def test_tender_url_builds_internal_link():
    """The helper builds a link to the card ONLY when both identifiers are present.

    The first version of the test allowed a link without profile_id — and exactly so a 404 got to
    prod (the card route requires a profile). The test was fixed together with the code.
    """
    assert fs._tender_url("t-1", "p-1") == "/app/tenders/t-1?profile_id=p-1"
    assert fs._tender_url("t-1", None) is None
    assert fs._tender_url(None, "p-1") is None


def _rows(now: datetime):
    deadline = now + timedelta(days=3)
    my = [
        SimpleNamespace(
            tender_id="t-work",
            profile_id="p-1",
            title="Радови на путу",
            status="active",
            deadline_at=deadline,
            tender_url="https://jnportal.src-ujn.example.invalid/tender-eo/1",
        )
    ]
    hot = [
        SimpleNamespace(
            tender_id="t-hot",
            profile_id="p-2",
            title="Медицинска опрема",
            customer_name="КЦ Србије",
            deadline_at=deadline,
            tender_url="https://jnportal.src-ujn.example.invalid/tender-eo/2",
        )
    ]
    return my, hot


def test_events_point_to_our_card_not_portal():
    """The “in progress” and “hot deadline” events lead to our card.

    Mutation: return `url=getattr(row, "tender_url", …)` → the test turns red,
    because the link becomes external again.
    """
    now = datetime.now(timezone.utc)
    my, hot = _rows(now)
    events = fs.build_calendar_events(my_tenders=my, deadlines=hot, now=now)

    tender_events = [
        e for e in events if e.kind in (fs.EVENT_ACTION, fs.EVENT_DEADLINE)
    ]
    assert tender_events, "the events for tenders were not collected"
    for e in tender_events:
        assert e.url and e.url.startswith("/app/tenders/"), e.url
        assert "jnportal" not in e.url, "the link must not lead to the portal"


def test_profile_id_is_carried_into_link():
    """profile_id reaches the link — the card opens bound to the profile."""
    now = datetime.now(timezone.utc)
    my, hot = _rows(now)
    events = fs.build_calendar_events(my_tenders=my, deadlines=hot, now=now)
    urls = [e.url for e in events if e.url and e.url.startswith("/app/tenders/")]
    assert any("profile_id=p-1" in u for u in urls), urls
    assert any("profile_id=p-2" in u for u in urls), urls


def test_no_internal_link_without_profile_id():
    """‼️ Without profile_id we do NOT build the internal link — the card returns 404.

    Incident 25.07: profile_id was added to the SQL but not to the HotDeadline dataclass —
    getattr returned None, the link was built as /app/tenders/{id}, and all 100 agenda rows
    on the live prod led to 404. Now such a case falls back to the external address: going to the portal is
    worse than staying in the product, but better than a 404.
    """
    assert fs._tender_url("t-1", None) is None
    assert fs._tender_url("t-1", "") is None

    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=2)
    hot = [
        SimpleNamespace(
            tender_id="t-x",
            profile_id=None,  # as it was in the incident
            title="Без профиля",
            customer_name=None,
            deadline_at=deadline,
            tender_url="https://jnportal.src-ujn.example.invalid/tender-eo/9",
        )
    ]
    events = fs.build_calendar_events(my_tenders=[], deadlines=hot, now=now)
    urls = [e.url for e in events if e.kind == fs.EVENT_DEADLINE]
    assert urls == ["https://jnportal.src-ujn.example.invalid/tender-eo/9"], urls


def test_hot_deadline_carries_profile_id_field():
    """HotDeadline has a profile_id field — otherwise the link would again be built without it."""
    assert "profile_id" in fs.HotDeadline.__dataclass_fields__


def test_event_carries_ids_for_translate_toggle():
    """The events for a tender have tender_id/profile_id — without them there is no toggle.

    Owner’s requirement (25.07): the “Translation” badge is needed IN the dashboard itself, not only in
    the “Current bids” block. The `_card_title.html` partial builds it from these
    identifiers, so the event must carry them.
    """
    now = datetime.now(timezone.utc)
    my, hot = _rows(now)
    events = fs.build_calendar_events(my_tenders=my, deadlines=hot, now=now)
    tender_events = [
        e for e in events if e.kind in (fs.EVENT_ACTION, fs.EVENT_DEADLINE)
    ]
    assert tender_events
    for e in tender_events:
        assert e.tender_id, e
        assert e.profile_id, e


def test_agenda_row_is_not_a_link_and_includes_toggle():
    """An agenda row is a div with the title partial: a button inside <a> is invalid.

    Mutation: return the link row `<a class="agenda__item"` → the test turns red.
    """
    tpl = pathlib.Path("src/api/templates/_agenda_item.html").read_text(
        encoding="utf-8"
    )
    assert '<div class="agenda__item' in tpl, "the row must be a div"
    assert '<a class="agenda__item' not in tpl, "the row must not be a link as a whole"
    assert '{% include "_card_title.html" %}' in tpl, "no partial with a toggle"
    assert 'view="agenda"' in tpl


def test_agenda_view_is_registered_for_title_fragment():
    """The `agenda` kind is declared in the fragment route — otherwise an htmx update would give 404."""
    src = pathlib.Path("src/api/cabinet/routes_tender_ai.py").read_text(
        encoding="utf-8"
    )
    assert '"agenda": {' in src
