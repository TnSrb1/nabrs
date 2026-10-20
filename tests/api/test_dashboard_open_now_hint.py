"""A new template without deliveries shows “N matching open” (the phrase agreed across forms), not a bare zero.

Reason: routing takes only tenders that arrived AFTER the template was created, and the portal’s feed is nightly — the first day of “no deliveries” looks like a breakdown. The hint is computed in ONE batch ``preview_counts`` and only for active templates younger than
``NEW_TEMPLATE_DAYS`` with no single delivery and a preview > 0.
Expectations — literals of the ru catalog (cabinet default), not ``t()`` calls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.api.cabinet import routes_profiles, stats_service
from tests.api.conftest import FakeResult
from tests.api.test_routes_profiles import SESS, _async, _client, _profile

NOW = datetime(2026, 10, 17, 9, 31, tzinfo=timezone.utc)
HINT_TEXT = "Новый шаблон: открыто 5 подходящих тендеров →"
ZERO_TEXT = "пока нет доставок"


def _fresh(pid="p-1", **kw):
    # The topic is required: without it matches_anything gives False and the SQL is not executed at all.
    kw.setdefault("keywords_include", [{"all_of": ["licenc"]}])
    return _profile(id=pid, created_at=NOW - timedelta(days=1), **kw)


def _stub_counts(monkeypatch, value=5):
    """preview_counts → ``value`` per payload; returns the list of calls."""
    calls = []

    async def _counts(db, payloads):
        calls.append(list(payloads))
        return [value] * len(payloads)

    monkeypatch.setattr(routes_profiles.preview_service, "preview_counts", _counts)
    return calls


async def _hints(profiles, delivered=None):
    return await routes_profiles._open_now_hints(
        object(), profiles, delivered or {}, now=NOW
    )


async def test_fresh_active_template_without_deliveries_gets_hint(monkeypatch):
    _stub_counts(monkeypatch, value=5)
    assert await _hints([_fresh()]) == {"p-1": 5}


async def test_template_older_than_seven_days_gets_no_hint(monkeypatch):
    _stub_counts(monkeypatch, value=5)
    old = _profile(id="p-old", created_at=NOW - timedelta(days=8))
    assert await _hints([old]) == {}


async def test_template_with_deliveries_gets_no_hint(monkeypatch):
    _stub_counts(monkeypatch, value=5)
    assert await _hints([_fresh()], delivered={"p-1": 2}) == {}


async def test_inactive_template_gets_no_hint(monkeypatch):
    _stub_counts(monkeypatch, value=5)
    assert await _hints([_fresh(is_active=False)]) == {}


async def test_zero_preview_gets_no_hint(monkeypatch):
    _stub_counts(monkeypatch, value=0)
    assert await _hints([_fresh()]) == {}


async def test_all_candidates_go_to_one_batch_call(monkeypatch):
    calls = _stub_counts(monkeypatch, value=3)
    profiles = [_fresh("p-1"), _fresh("p-2"), _fresh("p-3")]
    assert await _hints(profiles) == {"p-1": 3, "p-2": 3, "p-3": 3}
    assert len(calls) == 1
    assert len(calls[0]) == 3


async def test_no_candidates_means_no_preview_call(monkeypatch):
    calls = _stub_counts(monkeypatch, value=5)
    assert await _hints([_profile(created_at=NOW - timedelta(days=30))]) == {}
    assert calls == []


class _CountingSession:
    """Counts SQL queries; an empty result — preview by fact 0, but the loop is visible."""

    def __init__(self):
        self.executed = 0

    async def execute(self, *_args, **_kwargs):
        self.executed += 1
        return FakeResult(items=[])


async def test_db_queries_do_not_grow_with_template_count():
    """Batch: one template and four templates with identical filters — exactly one query."""
    one, many = _CountingSession(), _CountingSession()
    await routes_profiles._open_now_hints(one, [_fresh("p-1")], {}, now=NOW)
    await routes_profiles._open_now_hints(
        many, [_fresh(f"p-{i}") for i in range(4)], {}, now=NOW
    )
    assert one.executed == 1
    assert many.executed == 1


def _dashboard(monkeypatch, *, count, locale="ru"):
    monkeypatch.setattr(routes_profiles.svc, "list_profiles", _async([_fresh()]))
    monkeypatch.setattr(routes_profiles.svc, "downgrade_notice", _async([]))
    monkeypatch.setattr(
        routes_profiles.stats_service,
        "get_tenant_delivery_stats",
        _async(stats_service.TenantStats(total=0, by_profile=[])),
    )
    monkeypatch.setattr(
        routes_profiles.channel_svc, "has_active_telegram", _async(False)
    )
    monkeypatch.setattr(routes_profiles.channel_svc, "dead_channels", _async([]))
    monkeypatch.setattr(
        routes_profiles.channel_svc,
        "delivery_channel_status",
        _async(
            {
                "has_live_channel": True,
                "live_channels": ["email"],
                "gaps": [],
                "channelless": [],
            }
        ),
    )
    monkeypatch.setattr(
        routes_profiles.preview_service,
        "preview_counts",
        _async([count]),
    )
    resp = _client(sess={**SESS, "locale": locale}).get("/app")
    assert resp.status_code == 200
    return resp.text


def test_dashboard_shows_open_now_hint_with_link_to_template(monkeypatch):
    text = _dashboard(monkeypatch, count=5)
    assert HINT_TEXT in text
    assert 'href="/profiles/p-1?updated=1"' in text
    assert ZERO_TEXT not in text


def test_dashboard_keeps_zero_text_when_preview_is_zero(monkeypatch):
    text = _dashboard(monkeypatch, count=0)
    assert ZERO_TEXT in text
    assert "уже открыто" not in text


async def test_profile_without_created_at_gets_no_hint(monkeypatch):
    """A stub without a creation date is not counted as “new”: a missing date is no reason for a hint."""
    _stub_counts(monkeypatch, value=5)
    assert await _hints([_profile(id="p-1", created_at=None)]) == {}


@pytest.mark.parametrize(
    "locale,n,expected",
    [
        ("sr", 1, "Novi šablon: otvoren je 1 odgovarajući tender →"),
        ("sr", 3, "Novi šablon: otvorena su 3 odgovarajuća tendera →"),
        ("sr", 5, "Novi šablon: otvoreno je 5 odgovarajućih tendera →"),
        ("ru", 1, "Новый шаблон: открыт 1 подходящий тендер →"),
        ("ru", 3, "Новый шаблон: открыто 3 подходящих тендера →"),
        ("ru", 5, "Новый шаблон: открыто 5 подходящих тендеров →"),
    ],
)
def test_dashboard_open_now_hint_agrees_in_every_plural_form(
    monkeypatch, locale, n, expected
):
    """The whole phrase is agreed: the verb stands in plural forms, not separately from the number.

    Literals — from the rendered dashboard line (sr and ru), not from the catalog and not ``t()``.
    """
    text = _dashboard(monkeypatch, count=n, locale=locale)
    assert expected in text


async def test_profile_without_sources_gets_no_hint_and_runs_no_query():
    """Template without sources: preview = 0 without SQL, the “already open” hint is not shown.

    Without sources `open_now_examples` returns [] — since “N open” would lead to an empty block.
    The real `preview_counts`, not a mock: `object()` will fail if it reaches the query.
    """
    empty = _fresh(enabled_source_keys=[])
    assert await _hints([empty]) == {}
