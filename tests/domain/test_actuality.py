"""“No deadline → current for 30 days” (owner 06.10.2026): the Python mirror and the ingest.

The SQL form is checked on a real PG: ``tests/db/test_no_deadline_30d.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.domain.actuality import (
    NO_DEADLINE_ACTUAL_DAYS,
    no_deadline_is_fresh,
    sql_no_deadline_fresh_tn,
)
from src.parsers.base import NormalizedTender
from src.workers.ingest import _is_actual

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def test_window_is_30_days_by_d1_decision():
    assert NO_DEADLINE_ACTUAL_DAYS == 30


def test_mirror_boundaries():
    assert no_deadline_is_fresh(None, NOW - timedelta(days=29), NOW) is True
    assert no_deadline_is_fresh(None, NOW - timedelta(days=31), NOW) is False
    # the boundary is inclusive, like `>=` in the SQL
    assert no_deadline_is_fresh(None, NOW - timedelta(days=30), NOW) is True
    # published_at is primary over first_seen_at
    assert (
        no_deadline_is_fresh(NOW - timedelta(days=40), NOW - timedelta(days=1), NOW)
        is False
    )
    # both dates unknown — the freshness is not proved
    assert no_deadline_is_fresh(None, None, NOW) is False
    # naive = UTC
    assert no_deadline_is_fresh(datetime(2026, 9, 20), None, NOW) is True


def test_sql_fragment_text():
    assert sql_no_deadline_fresh_tn("tn") == (
        "(tn.deadline_at IS NULL AND COALESCE(tn.published_at, tn.first_seen_at) "
        ">= now() - interval '30 days')"
    )


def _n(**kw) -> NormalizedTender:
    base = dict(source_key="x", source_uid="1", country_code="RS")
    base.update(kw)
    return NormalizedTender(**base)


def test_ingest_insert_only_actual_uses_30_day_rule():
    # no deadline and no publication date (ukck) — a new tender is current
    assert _is_actual(_n(), NOW) is True
    assert _is_actual(_n(published_at=NOW - timedelta(days=29)), NOW) is True
    assert _is_actual(_n(published_at=NOW - timedelta(days=31)), NOW) is False
    # a deadline in the future — current at any age of the publication
    assert (
        _is_actual(
            _n(
                published_at=NOW - timedelta(days=400),
                deadline_at=NOW + timedelta(days=1),
            ),
            NOW,
        )
        is True
    )
    assert _is_actual(_n(deadline_at=NOW - timedelta(minutes=1)), NOW) is False
    assert _is_actual(_n(status="awarded"), NOW) is False
