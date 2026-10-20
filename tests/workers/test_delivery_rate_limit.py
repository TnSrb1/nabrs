"""Unit tests of the anti-spam of the delivery: the daily limit per profile + the pacing.

In the style of ``test_delivery.py`` (a fake session + a fake adapter), but the session routes the answers by a SQL substring — the counters (DISTINCT profile/tender) and the boolean result of EXISTS (``.scalar()``) must be returned, to check “the first suppress of the profile in the day”. ``_claim_batch``/``_load_jobs`` are substituted, to run the real logic of :meth:`DeliveryWorker.poll_once` on a ready batch of jobs without a DB.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.workers.adapters.base import DeliveryJob, DeliveryResult, FormattedMessage
from src.workers.delivery import DeliveryWorker

# --------------------------------------------------------------------------- #
#  The routing fake session (by the SQL substring)
# --------------------------------------------------------------------------- #


class _Result:
    """The result of execute: iterable rows + ``scalar()`` for EXISTS."""

    def __init__(self, rows=(), scalar_value=None):
        self._rows = list(rows)
        self.rowcount = len(self._rows)
        self._scalar = scalar_value

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        return self._scalar


class _RouterSession:
    """A fake session: the answer is chosen by the SQL substring.

    ``counts`` — the rows for the GROUP BY (the counters for the day); ``exists_seq`` — the queue of booleans for EXISTS (one per each suppress, in the order of the batch; the default is empty → False). Other queries (the INSERT of the attempt, the UPDATE of the statuses) → an empty result. All the calls are written to ``calls`` as ``(sql, params)``.
    """

    def __init__(self, *, counts=(), exists_seq=None):
        self._counts = list(counts)
        self._exists_seq = list(exists_seq or [])
        self.calls: list[tuple] = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params))
        if "SELECT DISTINCT profile_id" in sql:
            return _Result(self._counts)
        if "SELECT EXISTS" in sql:
            value = self._exists_seq.pop(0) if self._exists_seq else False
            return _Result(scalar_value=value)
        return _Result()

    async def commit(self):
        self.commits += 1


def _sent_rows(n: int, profile_id: str = "P"):
    """n tenders of the profile already sent today (the rows of the limit selection)."""
    return [
        SimpleNamespace(profile_id=profile_id, tender_id=f"old-{i}") for i in range(n)
    ]


def _session_factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


class _CapAdapter:
    """An adapter stub with ``send_text`` (records the cards and the limit summaries)."""

    channel = "telegram"

    def __init__(self, *, send_text_exc: Exception | None = None):
        self._send_text_exc = send_text_exc
        self.formatted: list = []
        self.sent: list = []
        self.text_sent: list = []
        self.text_locales: list = []

    def supports_buttons(self) -> bool:
        return True

    def format(self, job: DeliveryJob) -> FormattedMessage:
        self.formatted.append(job)
        return FormattedMessage(text="msg")

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        self.sent.append(job)
        return DeliveryResult(ok=True, response_code=200)

    async def send_text(self, target: str, text: str, **kwargs) -> DeliveryResult:
        if self._send_text_exc is not None:
            raise self._send_text_exc
        self.text_locales.append(kwargs.get("locale"))
        self.text_sent.append((target, text))
        return DeliveryResult(ok=True, response_code=200)


def _job(outbox_id: int, profile_id: str | None = "P", **over) -> DeliveryJob:
    base = dict(
        outbox_id=outbox_id,
        tender_id=f"t-{outbox_id}",
        channel="telegram",
        requires_ai=False,
        user_channel_id=f"uc-{outbox_id}",
        target="555",
        channel_status="active",
        title="Тендер",
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        profile_id=profile_id,
    )
    base.update(over)
    return DeliveryJob(**base)


def _worker(session, adapter, **kw) -> DeliveryWorker:
    return DeliveryWorker(
        adapters={"telegram": adapter},
        session_factory=_session_factory(session),
        **kw,
    )


def _patch_batch(worker: DeliveryWorker, jobs: list[DeliveryJob]) -> None:
    """It substitutes the pickup/loading: poll_once gets a ready batch without a DB."""
    ids = [job.outbox_id for job in jobs]

    async def fake_claim(session, hold=None):
        return list(ids)

    async def fake_load(session, claimed):
        return list(jobs)

    worker._claim_batch = fake_claim
    worker._load_jobs = fake_load


def _sent_count(session: _RouterSession) -> int:
    return sum(1 for sql, _ in session.calls if "status='sent'" in sql)


def _suppressed_count(session: _RouterSession) -> int:
    return sum(1 for sql, _ in session.calls if "status='suppressed'" in sql)


def _daily_cap_attempts(session: _RouterSession) -> int:
    return sum(
        1
        for sql, params in session.calls
        if "delivery_attempts" in sql and params and params.get("err") == "daily_cap"
    )


# --------------------------------------------------------------------------- #
#  1. cap=2, a batch of 3 of one profile, 0 today → 2 sent, 1 suppressed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cap_suppresses_overflow_and_sends_one_notice():
    jobs = [_job(1), _job(2), _job(3)]  # one profile P
    session = _RouterSession(counts=[], exists_seq=[False])  # 0 sent today
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    processed = await worker.poll_once()

    assert processed == 3
    assert len(adapter.sent) == 2  # the first two went out
    assert _sent_count(session) == 2
    assert _suppressed_count(session) == 1  # the third is suppressed
    assert _daily_cap_attempts(session) == 1  # attempt with err='daily_cap'
    assert len(adapter.text_sent) == 1  # exactly one warning summary
    # the text of the summary contains the limit and the hint /profile
    _, notice = adapter.text_sent[0]
    assert "30" not in notice  # the limit from cap=2, not the default
    # without a locale the tests go in ru (tests/conftest.py); the expectation — a literal
    assert notice.startswith("Дневной лимит 2 тендеров по шаблону «(без имени)»")
    assert "https://nab.rs/app/feed?profile_id=P" in notice
    assert "https://nab.rs/profiles/P/edit" in notice


@pytest.mark.asyncio
async def test_cap_notice_gets_channel_locale():
    """The limit summary goes out in the language of the channel — a Serbian recipient gets Latin."""
    jobs = [_job(1, locale="sr"), _job(2, locale="sr"), _job(3, locale="sr")]
    session = _RouterSession(counts=[], exists_seq=[False])
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert adapter.text_locales == ["sr"]


# --------------------------------------------------------------------------- #
#  2. the next batch of the same profile → suppressed WITHOUT a second summary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_next_batch_same_profile_suppressed_without_second_notice():
    jobs = [_job(4), _job(5)]
    # already sent today = cap (2) AND the suppressed row of the profile is already there
    # (EXISTS → True for both) → the summary is NOT sent again.
    session = _RouterSession(
        counts=_sent_rows(2),
        exists_seq=[True, True],
    )
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert _suppressed_count(session) == 2
    assert len(adapter.sent) == 0
    assert len(adapter.text_sent) == 0  # there is no second summary


# --------------------------------------------------------------------------- #
#  3. cap=0 → the limit is off, everything is sent
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cap_zero_disables_limit_all_sent():
    jobs = [_job(6), _job(7), _job(8)]
    session = _RouterSession()
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=0)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert len(adapter.sent) == 3
    assert _suppressed_count(session) == 0
    # the limit is off → the counters for the day are not read (there is no DISTINCT selection)
    assert all("SELECT DISTINCT profile_id" not in sql for sql, _ in session.calls)
    assert adapter.text_sent == []


# --------------------------------------------------------------------------- #
#  4. profile_id=None → the limit is not applied to the row
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_profile_id_none_not_capped():
    jobs = [_job(9, profile_id=None), _job(10, profile_id=None)]
    session = _RouterSession()
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=1)  # the limit is on
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert len(adapter.sent) == 2  # limit=1, but profile_id=None → we do not suppress
    assert _suppressed_count(session) == 0
    # there are no profiles in the batch → the counters are not read
    assert all("SELECT DISTINCT profile_id" not in sql for sql, _ in session.calls)


# --------------------------------------------------------------------------- #
#  5. the counter from the SQL is already = cap → the whole batch of the profile is suppressed (one summary)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_sql_counter_at_cap_suppresses_whole_batch():
    jobs = [_job(11), _job(12), _job(13)]
    # already sent today = cap; there are no suppressed rows yet → the first suppress
    # sends the summary (EXISTS False), the subsequent ones do not (EXISTS True).
    session = _RouterSession(
        counts=_sent_rows(2),
        exists_seq=[False, True, True],
    )
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert _suppressed_count(session) == 3  # the whole batch is suppressed
    assert len(adapter.sent) == 0
    assert _daily_cap_attempts(session) == 3
    assert len(adapter.text_sent) == 1  # exactly one summary per day


# --------------------------------------------------------------------------- #
#  6. pacing: with send_delay>0 a pause between the jobs (N-1 pauses)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pacing_sleeps_between_jobs():
    jobs = [
        _job(14, profile_id=None),
        _job(15, profile_id=None),
        _job(16, profile_id=None),
    ]
    session = _RouterSession()
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0.05, daily_cap=0)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    worker._sleep = fake_sleep
    _patch_batch(worker, jobs)

    await worker.poll_once()

    # for N jobs — N-1 pauses, all = send_delay (the pause is BETWEEN, not before the first)
    assert sleeps == [0.05, 0.05]
    assert len(adapter.sent) == 3


@pytest.mark.asyncio
async def test_no_pacing_when_send_delay_zero():
    jobs = [_job(17, profile_id=None), _job(18, profile_id=None)]
    session = _RouterSession()
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=0)

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    worker._sleep = fake_sleep
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert sleeps == []  # the pause is off
    assert len(adapter.sent) == 2


# --------------------------------------------------------------------------- #
#  7. an error of send_text does not roll back the suppress (a best-effort summary)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_text_error_does_not_block_suppress():
    jobs = [_job(19)]
    session = _RouterSession(
        counts=_sent_rows(5),
        exists_seq=[False],
    )
    adapter = _CapAdapter(send_text_exc=NotImplementedError("viber"))
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    await worker.poll_once()  # must not throw outside

    assert _suppressed_count(session) == 1  # the suppress took place
    assert _daily_cap_attempts(session) == 1
    assert adapter.text_sent == []  # send_text threw → the record did not appear


# --------------------------------------------------------------------------- #
#  8. owner 16.10: the limit is counted in TENDERS, not in the rows of the outbox (TG + email)
# --------------------------------------------------------------------------- #


class _EmailCapAdapter(_CapAdapter):
    channel = "email"


def _two_channel_worker(session, jobs, cap):
    tg, em = _CapAdapter(), _EmailCapAdapter()
    worker = DeliveryWorker(
        adapters={"telegram": tg, "email": em},
        session_factory=_session_factory(session),
        enabled_channels=("telegram", "email"),
        send_delay=0,
        daily_cap=cap,
    )
    _patch_batch(worker, jobs)
    return worker, tg, em


def _channel_jobs(tender_nums, *, start_id=1000, **over):
    """One job per EACH channel for each tender (as the rows of the outbox)."""
    jobs = []
    for i, num in enumerate(tender_nums):
        for k, channel in enumerate(("telegram", "email")):
            jobs.append(
                _job(
                    start_id + 2 * i + k,
                    tender_id=f"T{num}",
                    channel=channel,
                    **over,
                )
            )
    return jobs


@pytest.mark.asyncio
async def test_cap_counts_tenders_not_outbox_rows():
    """16 tenders × 2 channels = 32 rows at cap=30 — not a single one is cut."""
    jobs = _channel_jobs(range(16))
    session = _RouterSession(exists_seq=[])
    worker, tg, em = _two_channel_worker(session, jobs, cap=30)

    await worker.poll_once()

    assert len(tg.sent) == 16
    assert len(em.sent) == 16
    assert _suppressed_count(session) == 0


@pytest.mark.asyncio
async def test_31st_tender_suppressed_in_both_channels():
    """30 tenders already went out today: a new one is cut in both channels, and the second
    channel of the already sent tender (the email to the one gone to TG) — passes."""
    jobs = _channel_jobs([30]) + [
        _job(2000, tender_id="old-0", channel="email"),
    ]
    session = _RouterSession(counts=_sent_rows(30), exists_seq=[False, False])
    worker, tg, em = _two_channel_worker(session, jobs, cap=30)

    await worker.poll_once()

    assert _suppressed_count(session) == 2  # T30 in telegram and in email
    assert [j.tender_id for j in tg.sent] == []
    assert [j.tender_id for j in em.sent] == ["old-0"]  # the same tender, not a new one
    # The summary goes into EACH channel once (the mail has its own count of the “first suppress”)
    assert len(tg.text_sent) == 1 and len(em.text_sent) == 1


@pytest.mark.asyncio
async def test_suppress_exists_probe_is_per_channel():
    jobs = [_job(3000, tender_id="N", channel="email")]
    session = _RouterSession(counts=_sent_rows(30), exists_seq=[False])
    worker, _tg, em = _two_channel_worker(session, jobs, cap=30)

    await worker.poll_once()

    probes = [p for sql, p in session.calls if "SELECT EXISTS" in sql]
    assert probes and probes[0]["channel"] == "email"
    assert len(em.text_sent) == 1


@pytest.mark.asyncio
async def test_counter_fields_set_from_23_and_24_th_tender():
    """22 have already gone out: the 23rd tender → daily_sent=23, the 24th → 24 (counting itself)."""
    jobs = [_job(4000, tender_id="a"), _job(4001, tender_id="b")]
    session = _RouterSession(counts=_sent_rows(22))
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=30)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert [(j.daily_sent, j.daily_cap) for j in adapter.formatted] == [
        (23, 30),
        (24, 30),
    ]


def test_cap_notice_has_no_hardcoded_russian():
    """A Russian literal in the text of the limit summary = a violation of the i18n (owner 16.10).

    The log lines (``logger.*``) are for the operator and are outside the check.
    """
    import ast
    import inspect
    import re
    import textwrap

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(DeliveryWorker._send_cap_notice))
    )
    fn = tree.body[0]
    logged = {
        id(node)
        for call in ast.walk(fn)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "logger"
        for node in ast.walk(call)
    }
    doc = fn.body[0].value
    literals = [
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and id(n) not in logged
        and n is not doc
    ]
    assert literals  # the check is not empty
    assert [x for x in literals if re.search(r"[А-Яа-яЁё]", x)] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        (
            "sr",
            "Dnevni limit od 2 tendera po šablonu „Moj šablon“ je dostignut. "
            "Ostali današnji tenderi su na sajtu: https://nab.rs/app/feed?profile_id=P. "
            "Suzite šablon: https://nab.rs/profiles/P/edit",
        ),
        (
            "en",
            "The daily limit of 2 tenders for template “Moj šablon” has been reached. "
            "The rest of today’s are on the website: https://nab.rs/app/feed?profile_id=P. "
            "Narrow the template: https://nab.rs/profiles/P/edit",
        ),
        (
            "ru",
            "Дневной лимит 2 тендеров по шаблону «Moj šablon» достигнут. "
            "Остальные сегодняшние — на сайте: https://nab.rs/app/feed?profile_id=P. "
            "Сузить шаблон: https://nab.rs/profiles/P/edit",
        ),
    ],
)
async def test_cap_notice_text_per_locale(locale, expected, monkeypatch):
    monkeypatch.delenv("CABINET_BASE_URL", raising=False)
    jobs = [
        _job(5000 + i, tender_id=f"x{i}", locale=locale, profile_name="Moj šablon")
        for i in range(3)
    ]
    session = _RouterSession(exists_seq=[False])
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert adapter.text_sent == [("555", expected)]


# --------------------------------------------------------------------------- #
#  5. the name of the template with «<» / «&» → telegram (parse_mode=HTML) gets the escaped text
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cap_notice_escapes_profile_name_for_telegram(monkeypatch):
    """The HTML markup of telegram: «<» / «&» in the name are escaped, otherwise the summary would not go out."""
    monkeypatch.delenv("CABINET_BASE_URL", raising=False)
    jobs = [
        _job(6000 + i, tender_id=f"y{i}", locale="ru", profile_name="IT <Beograd> & Co")
        for i in range(3)
    ]
    session = _RouterSession(exists_seq=[False])
    adapter = _CapAdapter()
    worker = _worker(session, adapter, send_delay=0, daily_cap=2)
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert len(adapter.text_sent) == 1
    _, text = adapter.text_sent[0]
    assert "«IT &lt;Beograd&gt; &amp; Co»" in text
    assert "IT <Beograd>" not in text


@pytest.mark.asyncio
async def test_cap_notice_keeps_raw_profile_name_for_email(monkeypatch):
    """The e-mail escapes by itself (html_body) — the text is plain, the name is as it is, without a double escape."""
    monkeypatch.delenv("CABINET_BASE_URL", raising=False)
    jobs = [
        _job(
            7000 + i,
            tender_id=f"z{i}",
            channel="email",
            target="a@b.rs",
            locale="ru",
            profile_name="IT <Beograd> & Co",
        )
        for i in range(3)
    ]
    session = _RouterSession(exists_seq=[False])
    adapter = _CapAdapter()
    adapter.channel = "email"
    worker = DeliveryWorker(
        adapters={"email": adapter},
        session_factory=_session_factory(session),
        send_delay=0,
        daily_cap=2,
    )
    _patch_batch(worker, jobs)

    await worker.poll_once()

    assert len(adapter.text_sent) == 1
    _, text = adapter.text_sent[0]
    assert "«IT <Beograd> & Co»" in text
