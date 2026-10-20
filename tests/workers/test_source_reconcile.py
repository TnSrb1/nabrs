"""The worker of the M2 reconciliation: a divergence → an alert; a match → none; without a method → a skip; a repeat on the same day → one alert. The DB — memory (substitution of session_factory)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.parsers.base import SourceAdapter
from src.workers import source_reconcile as sr

_NOW = datetime(2026, 10, 16, 2, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, row=None, scalar=None):
        self._row, self._scalar = row, scalar

    def first(self):
        return self._row

    def scalar_one(self):
        return self._scalar


class FakeDb:
    """Memory instead of PG: our_count by day + the table source_reconcile."""

    def __init__(self, our: dict[date, int]):
        self.our = our
        self.rows: dict[tuple[str, date], dict] = {}

    def factory(self):
        db = self

        class _Session:
            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *a):
                return False

            async def commit(self_):
                return None

            async def execute(self_, stmt, params=None):
                sql = str(stmt)
                if "FROM tenders_normalized" in sql:
                    return _Result(scalar=db.our[params["day"]])
                if "MAX(checked_at)" in sql:
                    mine = [
                        r["checked_at"]
                        for (k, _), r in db.rows.items()
                        if k == params["source_key"]
                    ]
                    return _Result(row=SimpleNamespace(last_at=max(mine, default=None)))
                if "SELECT source_count, our_count" in sql:
                    r = db.rows.get((params["source_key"], params["day"]))
                    return _Result(row=SimpleNamespace(**r) if r else None)
                if "INSERT INTO source_reconcile" in sql:
                    db.rows[(params["source_key"], params["day"])] = {
                        "source_count": params["source_count"],
                        "our_count": params["our_count"],
                        "checked_at": params["checked_at"],
                    }
                    return _Result()
                raise AssertionError(sql)

        return _Session()


class _Counting(SourceAdapter):
    source_key = "fake"
    reconcile_tz = "UTC"

    def __init__(self, counts):
        self.counts = counts
        self.calls: list[date] = []

    def fetch_batch(self, since):  # pragma: no cover
        raise NotImplementedError

    def build_source_uid(self, raw):  # pragma: no cover
        return ""

    def normalize(self, raw):  # pragma: no cover
        raise NotImplementedError

    async def count_published_on(self, day):
        self.calls.append(day)
        return self.counts[day]


class _NoMethod(_Counting):
    count_published_on = SourceAdapter.count_published_on


async def _nosleep(_):
    return None


def _make(db, alerts, emails=None):
    async def alert(text_):
        alerts.append(text_)

    async def email(subject, body):
        if emails is not None:
            emails.append(subject)

    return sr.SourceReconciler(
        session_factory=db.factory, alert=alert, email=email, sleep=_nosleep
    )


_DAYS = [date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)]


@pytest.mark.asyncio
async def test_mismatch_alerts_once_per_day_and_stores_history():
    db = FakeDb({_DAYS[0]: 217, _DAYS[1]: 155, _DAYS[2]: 176})
    ad = _Counting({_DAYS[0]: 217, _DAYS[1]: 155, _DAYS[2]: 140})  # 15.10: −36
    alerts: list[str] = []
    emails: list[str] = []
    rec = _make(db, alerts, emails)
    res = await rec.reconcile_enabled([("fake", None, ad)], now=_NOW)
    assert [r.mismatch for r in res] == [False, False, True]
    assert len(alerts) == 1 and "15.10.2026" in alerts[0] and "140" in alerts[0]
    assert len(emails) == 1
    assert len(db.rows) == 3  # history: a row for each day
    # a repeat on the same day (after a restart — a new instance): already reconciled → 0 queries
    ad.calls.clear()
    rec2 = _make(db, alerts)
    assert await rec2.reconcile_enabled([("fake", None, ad)], now=_NOW) == []
    assert ad.calls == [] and len(alerts) == 1


@pytest.mark.asyncio
async def test_same_mismatch_next_day_does_not_alert_again():
    db = FakeDb({_DAYS[0]: 100, _DAYS[1]: 100, _DAYS[2]: 100})
    ad = _Counting({d: 150 for d in _DAYS})
    alerts: list[str] = []
    rec = _make(db, alerts)
    await rec.reconcile_enabled([("fake", None, ad)], now=_NOW)
    assert len(alerts) == 3  # three different days — one alert for each (source/day)
    # on the next day the window shifted: 13.10 left, 16.10 came; 14 and 15 — a repeat
    nxt = datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc)
    d16 = date(2026, 10, 16)
    db.our[d16] = 100
    ad.counts[d16] = 150
    await rec.reconcile_enabled([("fake", None, ad)], now=nxt)
    assert len(alerts) == 4  # only the new one, 16.10


@pytest.mark.asyncio
async def test_match_does_not_alert():
    db = FakeDb({d: 100 for d in _DAYS})
    ad = _Counting({d: 101 for d in _DAYS})
    alerts: list[str] = []
    res = await _make(db, alerts).reconcile_enabled([("fake", None, ad)], now=_NOW)
    assert alerts == [] and not any(r.mismatch for r in res)


@pytest.mark.asyncio
async def test_source_without_method_is_skipped():
    db = FakeDb({})
    ad = _NoMethod({})
    alerts: list[str] = []
    res = await _make(db, alerts).reconcile_enabled([("fake", None, ad)], now=_NOW)
    assert res == [] and alerts == [] and db.rows == {}


@pytest.mark.asyncio
async def test_before_0130_utc_does_nothing():
    db = FakeDb({d: 1 for d in _DAYS})
    ad = _Counting({d: 99 for d in _DAYS})
    early = datetime(2026, 10, 16, 1, 29, tzinfo=timezone.utc)
    assert await _make(db, []).reconcile_enabled([("fake", None, ad)], now=early) == []
    assert ad.calls == []


@pytest.mark.asyncio
async def test_source_failure_is_swallowed_and_retried_not_before_an_hour():
    db = FakeDb({d: 1 for d in _DAYS})

    class _Boom(_Counting):
        async def count_published_on(self, day):
            self.calls.append(day)
            raise RuntimeError("429")

    ad = _Boom({})
    rec = _make(db, [])
    assert await rec.reconcile_enabled([("fake", None, ad)], now=_NOW) == []
    assert len(ad.calls) == 1
    await rec.reconcile_enabled([("fake", None, ad)], now=_NOW)
    assert len(ad.calls) == 1  # backoff: for an hour we do not hammer the source


def test_reconcile_days_are_last_three_full_days():
    assert sr.reconcile_days(_NOW) == _DAYS
