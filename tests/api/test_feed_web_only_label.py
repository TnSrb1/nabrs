"""Row ``web_only`` (cabinet as the third channel, #820): the label "found", not "delivered".

``web_only`` was not sent: the ``sent_at`` of such a row is ``created_at``
(``_EVENT_TS``), and the old "delivered DD.MM" label lied to the client. Expectations are
literals from the rendered fact, not via ``t()``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.cabinet import feed_service
from tests.api.test_also_on_cabinet import _item, _render

_FOUND_AT = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
@pytest.mark.parametrize(
    ("lang", "label"),
    [("ru", "найдено 16.10"), ("sr", "pronađeno 16.10"), ("en", "found 16.10")],
)
def test_web_only_item_is_labelled_found_not_delivered(template, lang, label):
    html = _render(template, _item(web_only=True, sent_at=_FOUND_AT), lang)
    assert f">{label}</span>" in html
    assert "доставлено 16.10" not in html
    assert "isporučeno 16.10" not in html
    assert "delivered 16.10" not in html


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
def test_sent_item_keeps_delivered_label(template):
    html = _render(template, _item(web_only=False, sent_at=_FOUND_AT), "ru")
    assert ">доставлено 16.10</span>" in html
    assert "найдено 16.10" not in html


def test_row_to_feed_item_carries_web_only_flag():
    row = SimpleNamespace(
        tender_id="t1",
        profile_id="p1",
        title="Tender",
        customer_name=None,
        deadline_at=None,
        tender_url=None,
        profile_name="P",
        ai_level=None,
        sent_at=_FOUND_AT,
        source_label=None,
        source_key=None,
        canonical_confidence=None,
        also_on_n=0,
        capped=False,
        web_only=True,
        delivered_ai_level=None,
        has_bid_verdict=False,
        has_checklist=False,
        has_draft=False,
    )
    assert feed_service._row_to_feed_item(row).web_only is True
