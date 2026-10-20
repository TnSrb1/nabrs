"""The delivery language is taken from the CHANNEL, and the cabinet language is only the fallback (0038).

The pack A (#481) taught the Telegram card to listen to ``DeliveryJob.locale``, but the source was ``users.locale`` — the language of the CABINET. The pack B added the language of the channel itself (``user_channels.locale``, ``/language`` in the bot), and the delivery selection must prefer it: ``COALESCE(uc.locale, u.locale)``.

‼️ The honesty boundary is the same as in ``test_delivery_e2e``: the session here is a fake, it does NOT execute the SQL. Therefore the file proves TWO things that are always checkable in the CI: (1) the selection asks the DB exactly for the COALESCE expression, and not for the bare ``u.locale``; (2) the value from the column ``locale`` gets to ``DeliveryJob``, and from there — to the chrome of the adapter. The semantics of the COALESCE itself on three cases (channel sr + user ru → sr; channel NULL + user sr → sr; both NULL → NULL→ru in the adapter) is given only by a live PostgreSQL — the run is attached to the REPORT/PR.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.workers.adapters.telegram import TelegramAdapter
from src.workers.delivery import _LOAD_JOBS_SQL, DeliveryWorker


class _FakeBot:
    async def send_message(self, **kwargs):  # pragma: no cover — is not called
        return object()


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self.rowcount = len(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])

    async def commit(self):
        pass


def _worker(session) -> DeliveryWorker:
    @asynccontextmanager
    async def factory():
        yield session

    return DeliveryWorker(session_factory=factory, adapters=[])


def _row(**over):
    row = dict(
        outbox_id=1,
        tender_id="t-1",
        channel="telegram",
        requires_ai=False,
        user_channel_id="uc-1",
        target="42",
        channel_status="active",
        title="Nabavka kancelarijskog materijala",
        source_url="https://jnportal.rs/tender-eo/1",
        amount_eur=1000.0,
        deadline_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
    )
    row.update(over)
    return SimpleNamespace(**row)


# --------------------------------------------------------------------------- #
# (1) The contract of the SQL: we ask for the language of the channel, the cabinet is the fallback.
# --------------------------------------------------------------------------- #


def test_load_jobs_sql_prefers_channel_locale_over_user_locale():
    flat = " ".join(str(_LOAD_JOBS_SQL).split())
    assert "COALESCE(uc.locale, u.locale) AS locale" in flat


def test_load_jobs_sql_no_longer_reads_user_locale_alone():
    """The guard of the regression: the bare ``u.locale AS locale`` would return the language of the cabinet.

    Exactly this is how the selection looked before 0038 — and the choice of language in the bot would not reach the cards, staying a cosmetic of the bot’s answers.
    """
    flat = " ".join(str(_LOAD_JOBS_SQL).split())
    assert "u.locale AS locale" not in flat


# --------------------------------------------------------------------------- #
# (2) The value of the column reaches the task and the chrome of the adapter.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolved_locale_reaches_job():
    """The column ``locale`` of the selection (already collapsed by the COALESCE) → DeliveryJob."""
    session = _FakeSession([_FakeResult([_row(locale="sr")])])
    jobs = await _worker(session)._load_jobs(session, [1])
    assert jobs[0].locale == "sr"


@pytest.mark.asyncio
async def test_null_locale_stays_none_and_falls_back_in_adapter():
    """Both NULL → job.locale=None; the language of the card is decided by the adapter (the ru default)."""
    session = _FakeSession([_FakeResult([_row(locale=None)])])
    jobs = await _worker(session)._load_jobs(session, [1])
    assert jobs[0].locale is None
    text = TelegramAdapter(_FakeBot()).format(jobs[0]).text
    assert "Приём до" in text  # the ru canon of the adapter at locale=None


@pytest.mark.asyncio
async def test_channel_locale_changes_card_chrome():
    """The same tender under sr — the chrome of the card is Serbian, the data are not translated."""
    session = _FakeSession([_FakeResult([_row(locale="sr")])])
    jobs = await _worker(session)._load_jobs(session, [1])
    text = TelegramAdapter(_FakeBot()).format(jobs[0]).text
    assert "Rok do" in text
    assert "Приём до" not in text
    assert "Nabavka kancelarijskog materijala" in text
