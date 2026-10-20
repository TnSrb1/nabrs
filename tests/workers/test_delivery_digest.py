"""Unit tests of the digest mode of the delivery (``profiles.delivery_schedule``).

The cabinet let one choose “Digest in the morning/in the evening/one’s own schedule”, but the delivery did not read this field at all — the subscriber silently got ``immediate``. Here the contour is pinned entirely, without a real PostgreSQL:

1. **The window** (:func:`digest_window` / :func:`digest_due`) — pure functions: the hours of the fixed windows, the parsing of ``digest_cron``, the tenant’s tz, “we have already sent today”.
2. **Outside the window the row WAITS, and is not lost** — the profile gets into the ``:hold`` of the pickup, the status of the row is not touched.
3. **In the window — ONE message for N tenders** (``format_digest``), not N cards.
4. **``immediate`` works as before** (the regression: this is how all the profiles of production live).
5. **A repeated run does not send the digest twice** (``max(sent_at)`` for today).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.workers.adapters.base import DeliveryJob, DeliveryResult, FormattedMessage
from src.workers.delivery import (
    DIGEST_EVENING_HOUR,
    DIGEST_MORNING_HOUR,
    DeliveryWorker,
    digest_due,
    digest_window,
)

BELGRADE = ZoneInfo("Europe/Belgrade")
PID = "11111111-1111-1111-1111-111111111111"


# --------------------------------------------------------------------------- #
#  Fakes: a session with routing by SQL + an adapter that distinguishes format/digest
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, rows=(), scalar_value=None):
        self._rows = list(rows)
        self.rowcount = len(self._rows)
        self._scalar = scalar_value

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._scalar


class _Session:
    """The session-router: the digest schedules, the pickup of the batch, the rest — empty."""

    def __init__(self, *, profiles=(), claimed=(), counts=()):
        self._profiles = list(profiles)
        self._claimed = list(claimed)
        self._counts = list(counts)
        self.calls: list[tuple] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if "FROM profiles p" in sql:
            return _Result(self._profiles)
        if "UPDATE delivery_outbox" in sql and "RETURNING id" in sql:
            return _Result(self._claimed)
        if "GROUP BY profile_id" in sql:
            return _Result(self._counts)
        if "SELECT EXISTS" in sql:
            return _Result(scalar_value=False)
        return _Result()

    async def commit(self):
        self.commits += 1

    # -- the helpers of the assertions --------------------------------------------- #

    def claim_params(self) -> dict:
        for sql, params in self.calls:
            if "UPDATE delivery_outbox" in sql and "RETURNING id" in sql:
                return params or {}
        raise AssertionError("the pickup of the batch was not called")

    def count_sql(self, needle: str) -> int:
        return sum(1 for sql, _ in self.calls if needle in sql)


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _Adapter:
    """An adapter that counts the cards (``format``) and the digests separately."""

    channel = "telegram"

    def __init__(self, result: DeliveryResult | None = None):
        self._result = result or DeliveryResult(ok=True, response_code=200)
        self.cards: list[DeliveryJob] = []
        self.digests: list[list[DeliveryJob]] = []
        self.sent: list[FormattedMessage] = []

    def supports_buttons(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return True

    def format(self, job: DeliveryJob) -> FormattedMessage:
        self.cards.append(job)
        return FormattedMessage(text=f"карточка {job.outbox_id}")

    def format_digest(self, jobs: list[DeliveryJob]) -> FormattedMessage:
        self.digests.append(list(jobs))
        return FormattedMessage(text=f"дайджест {len(jobs)}")

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        self.sent.append(message)
        return self._result


def _job(outbox_id: int, *, profile_id: str | None = PID, **over) -> DeliveryJob:
    base = dict(
        outbox_id=outbox_id,
        tender_id=f"t-{outbox_id}",
        channel="telegram",
        requires_ai=False,
        user_channel_id=f"uc-{outbox_id}",
        target="555",
        channel_status="active",
        title=f"Тендер {outbox_id}",
        source_url=f"https://x/{outbox_id}",
        amount_eur=None,
        deadline_at=None,
        profile_id=profile_id,
    )
    base.update(over)
    return DeliveryJob(**base)


def _profile_row(*, schedule="digest_morning", cron=None, last_sent_at=None):
    return SimpleNamespace(
        profile_id=PID,
        delivery_schedule=schedule,
        digest_cron=cron,
        tz="Europe/Belgrade",
        last_sent_at=last_sent_at,
    )


def _worker(session, adapter, *, now: datetime, jobs=None, **kw) -> DeliveryWorker:
    worker = DeliveryWorker(
        adapters={"telegram": adapter},
        session_factory=_session_factory(session),
        send_delay=0,
        now_factory=lambda: now,
        **kw,
    )
    if jobs is not None:

        async def fake_load(session_, claimed):
            return list(jobs)

        worker._load_jobs = fake_load
    return worker


# --------------------------------------------------------------------------- #
#  1. The window: digest_window — the fixed hours and the parsing of digest_cron
# --------------------------------------------------------------------------- #


def test_window_immediate_is_not_digest():
    # immediate/NULL/unknown → None: the previous delivery path, without the digest.
    assert digest_window("immediate", None) is None
    assert digest_window(None, None) is None
    assert digest_window("weird", "0 9 * * *") is None


def test_window_fixed_hours():
    assert digest_window("digest_morning", None) == (DIGEST_MORNING_HOUR, None)
    assert digest_window("digest_evening", None) == (DIGEST_EVENING_HOUR, None)
    # The cron of the fixed windows is not read — the hour is only from the schedule.
    assert digest_window("digest_morning", "0 23 * * *") == (DIGEST_MORNING_HOUR, None)


@pytest.mark.parametrize(
    ("cron", "expected"),
    [
        ("0 7 * * *", (7, None)),
        ("30 8,20 * * *", (8, None)),  # a list of hours → the earliest
        ("0 6-9 * * *", (6, None)),  # a range → the earliest
        ("0 9 * * 1-5", (9, {1, 2, 3, 4, 5})),  # weekdays
        ("0 9 * * 0", (9, {7})),  # cron: 0 = Sunday → ISO 7
        ("0 9 * * *", (9, None)),  # * by days → no restriction
        ("0 9 * * 0-6", (9, None)),  # all the days are listed → no restriction
    ],
)
def test_window_custom_cron(cron, expected):
    assert digest_window("digest_custom", cron) == expected


@pytest.mark.parametrize(
    "cron", [None, "", "мусор", "0 9 * *", "0 25 * * *", "0 abc * * *", "0 9/0 * * *"]
)
def test_window_custom_broken_cron_falls_back_to_morning(cron):
    # An unparseable cron → the morning window: sending in the morning is better than not sending.
    assert digest_window("digest_custom", cron) == (DIGEST_MORNING_HOUR, None)


# --------------------------------------------------------------------------- #
#  2. The window: digest_due — the hour, the tenant’s tz, the days of the week, “we have already sent today”
# --------------------------------------------------------------------------- #


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_due_false_before_window_hour():
    # 06:00 UTC = 08:00 Belgrade < 09:00 — the window has not yet come.
    assert not digest_due(
        (DIGEST_MORNING_HOUR, None), tz=BELGRADE, now=_utc(16, 6), last_sent_at=None
    )


def test_due_true_inside_window_uses_tenant_tz():
    # 07:00 UTC = 09:00 Belgrade: by UTC the window has NOT come, by the tenant’s tz — it has.
    now = _utc(16, 7)
    assert now.hour < DIGEST_MORNING_HOUR  # the premise of the test: the UTC hour is smaller
    assert digest_due(
        (DIGEST_MORNING_HOUR, None), tz=BELGRADE, now=now, last_sent_at=None
    )


def test_due_true_after_missed_window():
    # The worker was down at 09:00 — the window is caught up on the same day, rather than lost.
    assert digest_due(
        (DIGEST_MORNING_HOUR, None), tz=BELGRADE, now=_utc(16, 20), last_sent_at=None
    )


def test_due_false_when_already_sent_today():
    now = _utc(16, 12)
    assert not digest_due(
        (DIGEST_MORNING_HOUR, None),
        tz=BELGRADE,
        now=now,
        last_sent_at=now - timedelta(hours=1),
    )


def test_due_true_when_last_send_was_yesterday():
    now = _utc(16, 12)
    assert digest_due(
        (DIGEST_MORNING_HOUR, None),
        tz=BELGRADE,
        now=now,
        last_sent_at=now - timedelta(days=1),
    )


def test_due_false_on_weekday_mismatch():
    # 2026-07-18 is a Saturday (ISO 6); the cron «0 9 * * 1-5» does not include it.
    window = digest_window("digest_custom", "0 9 * * 1-5")
    assert not digest_due(window, tz=BELGRADE, now=_utc(18, 12), last_sent_at=None)
    # ...but on Friday (17th, ISO 5) — we send.
    assert digest_due(window, tz=BELGRADE, now=_utc(17, 12), last_sent_at=None)


# --------------------------------------------------------------------------- #
#  3. Outside the window: the row is NOT picked up and is NOT lost
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_outside_window_holds_rows_without_touching_them():
    session = _Session(profiles=[_profile_row()], claimed=[])
    adapter = _Adapter()
    worker = _worker(session, adapter, now=_utc(16, 6))  # 08:00 Belgrade < 09:00

    processed = await worker.poll_once()

    assert processed == 0
    assert adapter.sent == []
    # The profile is handed to :hold — the pickup did not touch its row.
    assert session.claim_params()["hold"] == [PID]
    # The row waits: not a single status transition (a loss) happened.
    assert session.count_sql("status='sent'") == 0
    assert session.count_sql("status='failed'") == 0
    assert session.count_sql("status='suppressed'") == 0


@pytest.mark.asyncio
async def test_inside_window_does_not_hold_profile():
    session = _Session(profiles=[_profile_row()], claimed=[])
    worker = _worker(session, _Adapter(), now=_utc(16, 7))  # 09:00 Belgrade

    await worker.poll_once()

    assert session.claim_params()["hold"] == []


# --------------------------------------------------------------------------- #
#  4. In the window: ONE message for N tenders
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inside_window_sends_single_digest_for_batch():
    jobs = [_job(1), _job(2), _job(3)]
    session = _Session(
        profiles=[_profile_row()],
        claimed=[SimpleNamespace(id=j.outbox_id) for j in jobs],
    )
    adapter = _Adapter()
    worker = _worker(session, adapter, now=_utc(16, 7), jobs=jobs)

    processed = await worker.poll_once()

    assert processed == 3
    # One message for the whole batch, assembled by format_digest from 3 tenders.
    assert len(adapter.sent) == 1
    assert adapter.cards == []  # there were NO cards per single tender
    assert [j.outbox_id for j in adapter.digests[0]] == [1, 2, 3]
    # All three rows are finalised as sent.
    assert session.count_sql("status='sent'") == 3


@pytest.mark.asyncio
async def test_digest_groups_are_per_channel():
    # The profile has two bindings (telegram + email) → two batches, not one.
    jobs = [_job(1), _job(2, channel="email"), _job(3)]
    session = _Session(
        profiles=[_profile_row()],
        claimed=[SimpleNamespace(id=j.outbox_id) for j in jobs],
    )
    tg, email = _Adapter(), _Adapter()
    email.channel = "email"
    worker = DeliveryWorker(
        adapters={"telegram": tg, "email": email},
        session_factory=_session_factory(session),
        send_delay=0,
        now_factory=lambda: _utc(16, 7),
    )

    async def fake_load(session_, claimed):
        return list(jobs)

    worker._load_jobs = fake_load
    await worker.poll_once()

    assert [j.outbox_id for j in tg.digests[0]] == [1, 3]
    assert [j.outbox_id for j in email.digests[0]] == [2]


@pytest.mark.asyncio
async def test_digest_not_sent_to_unconfirmed_channel():
    """‼️ owner: an unconfirmed (``pending``) address does not receive the digest either.

    The digest is the second send path with ITS OWN predicate of the channel (the batch is judged by ``lead``), therefore “pending is not active” must be pinned here too: the single delivery and the digest can diverge, and their address is shared.
    """
    jobs = [_job(1, channel_status="pending"), _job(2, channel_status="pending")]
    session = _Session(
        profiles=[_profile_row()],
        claimed=[SimpleNamespace(id=j.outbox_id) for j in jobs],
    )
    adapter = _Adapter()
    worker = _worker(session, adapter, now=_utc(16, 7), jobs=jobs)

    await worker.poll_once()

    assert adapter.sent == []  # the letter did not go out
    assert adapter.digests == []  # and was not even assembled
    assert session.count_sql("status='failed'") == 2


@pytest.mark.asyncio
async def test_digest_retryable_failure_returns_whole_batch_to_queue():
    jobs = [_job(1), _job(2)]
    session = _Session(
        profiles=[_profile_row()],
        claimed=[SimpleNamespace(id=j.outbox_id) for j in jobs],
    )
    adapter = _Adapter(DeliveryResult(ok=False, error_text="429", retryable=True))
    worker = _worker(session, adapter, now=_utc(16, 7), jobs=jobs)

    await worker.poll_once()

    # No row is marked sent — the whole batch will return on the next cycle.
    assert session.count_sql("status='sent'") == 0
    assert session.count_sql("CASE WHEN requires_ai") == 2


@pytest.mark.asyncio
async def test_digest_respects_daily_cap():
    # cap=2, in the batch 3 tenders: two go out by the digest, the third is suppressed.
    jobs = [_job(1), _job(2), _job(3)]
    session = _Session(
        profiles=[_profile_row()],
        claimed=[SimpleNamespace(id=j.outbox_id) for j in jobs],
    )
    adapter = _Adapter()
    worker = _worker(session, adapter, now=_utc(16, 7), jobs=jobs, daily_cap=2)

    await worker.poll_once()

    assert [j.outbox_id for j in adapter.digests[0]] == [1, 2]
    assert session.count_sql("status='suppressed'") == 1


# --------------------------------------------------------------------------- #
#  5. The regression of immediate + a repeated run
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_immediate_profile_still_gets_one_card_per_tender():
    # A profile without a digest schedule does not get into the selection of schedules at all —
    # this is how ALL the profiles of production live: a card per tender, as before.
    jobs = [_job(1), _job(2)]
    session = _Session(
        profiles=[], claimed=[SimpleNamespace(id=j.outbox_id) for j in jobs]
    )
    adapter = _Adapter()
    worker = _worker(session, adapter, now=_utc(16, 7), jobs=jobs)

    processed = await worker.poll_once()

    assert processed == 2
    assert len(adapter.sent) == 2  # two messages, not one digest
    assert [j.outbox_id for j in adapter.cards] == [1, 2]
    assert adapter.digests == []
    assert session.claim_params()["hold"] == []
    assert session.count_sql("status='sent'") == 2


@pytest.mark.asyncio
async def test_second_run_after_digest_sends_nothing():
    # The digest already went out an hour ago (max(sent_at) today) → the profile is again “outside the window”: the repeated run does not send a second digest, the new ones accumulate until tomorrow.
    # window»:
    now = _utc(16, 8)  # 10:00 Belgrade, the window has come
    session = _Session(
        profiles=[_profile_row(last_sent_at=now - timedelta(hours=1))], claimed=[]
    )
    adapter = _Adapter()
    worker = _worker(session, adapter, now=now)

    processed = await worker.poll_once()

    assert processed == 0
    assert adapter.sent == []
    assert session.claim_params()["hold"] == [PID]
