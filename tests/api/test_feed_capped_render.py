"""The mark “not sent — daily cap” on a trimmed match (owner 16.10).

Small, in the neutral color role (the same ``fd-card__sent`` as “delivered”).
Expectations — literals, not via ``t()``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.api.cabinet import feed_service
from tests.api.test_also_on_cabinet import _item, _render
from src.workers.matching import CLUSTER_DELIVERED_STATUSES

_SENT = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
@pytest.mark.parametrize(
    ("lang", "note", "title"),
    [
        (
            "ru",
            "не отправлено: дневной лимит",
            "Не отправлено в Telegram и на почту — достигнут дневной лимит 30 тендеров на шаблон.",
        ),
        (
            "sr",
            "nije poslato: dnevni limit",
            "Nije poslato u Telegram i na e-poštu — dostignut je dnevni limit od 30 tendera po šablonu.",
        ),
        (
            "en",
            "not sent: daily limit",
            "Not sent to Telegram or email — the daily limit of 30 tenders per template was reached.",
        ),
    ],
)
def test_capped_item_marked(template, lang, note, title):
    html = _render(template, _item(capped=True, sent_at=_SENT), lang)
    assert f">{note}</span>" in html
    assert title in html
    assert "fd-card__sent fd-card__value" in html  # neutral role, not a new color
    assert "доставлено 16.10" not in html and "isporučeno 16.10" not in html


@pytest.mark.parametrize("template", ["_feed_card.html", "_cockpit_feed_teaser.html"])
def test_sent_item_has_no_capped_note(template):
    html = _render(template, _item(capped=False, sent_at=_SENT), "ru")
    assert "дневной лимит" not in html
    assert "доставлено 16.10" in html


def test_feed_visible_statuses_mirror_cluster_delivery_plus_suppressed():
    """The feed = “the client received or will receive” + what is trimmed by the cap (desync guard)."""
    assert set(feed_service.FEED_VISIBLE_STATUSES) == set(
        CLUSTER_DELIVERED_STATUSES
    ) | {"suppressed"}


def test_feed_sql_uses_visible_statuses_everywhere():
    """All three feed queries (body, COUNT, burning) — by one set of statuses."""
    expected = (
        "o.status IN ('sent', 'suppressed', 'delivering', 'pending', "
        "'ai_pending', 'analyzing', 'ai_done', 'web_only')"
    )
    for stmt in (
        feed_service._FEED_ITEMS_SQL,
        feed_service._FEED_COUNT_SQL,
        feed_service._HOT_DEADLINES_SQL,
    ):
        sql = str(stmt)
        assert expected in sql
        assert "o.status = 'sent'" not in sql
