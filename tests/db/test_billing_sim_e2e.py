"""The live E2E of the payment path (billing-sim) under ``TEST_RLS_DATABASE_URL``.

It proves the LAST link of the chain, not covered by the per-layer tests:
``apply_billing_event`` → a row in ``billing_notification_outbox`` (0033, in the same
transaction as the dedup claim) → ``BillingNotificationsWorker.poll_once`` →
exactly one letter to the mock sender → ``sent_at`` is set → a repeated ``poll_once``
sends NO letters (the idempotent claim ``app_billing_mark_notification_sent``).

Requires TEST_RLS_DATABASE_URL (the connection under the role ``tendersrb_app``, NOBYPASSRLS,
the DB up to head). Without the variable — SKIP, like the whole ``tests/db``. The setup/cleanup/reading of the queue — under the superuser (``TEST_SUPERUSER_DATABASE_URL``, bypasses RLS and the REVOKE of the direct access to the queue). The worker and the core — under the app role via SECURITY DEFINER (``app_billing_*``): exactly so they go in production.

Self-sufficient: its own tenant + a subscriber with a valid email (otherwise
``app_billing_pending_notifications`` does not resolve the recipient, and there is nowhere to send).
The assertions filter by ITS OWN address/event — on a shared throwaway DB there may be
foreign pending rows, they do not touch this test.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.emailer import MockEmailSender
from src.billing.models import BillingEvent
from src.billing.service import apply_billing_event
from src.workers.billing_notifications import BillingNotificationsWorker

TEST_URL = os.getenv("TEST_RLS_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="TEST_RLS_DATABASE_URL не задан — E2E биллинга пропускается",
)


@pytest.mark.asyncio
async def test_billing_activated_end_to_end_outbox_worker_email():
    """checkout→webhook(core)→outbox→worker.poll_once→email, idempotently.

    Steps:
      1. the app role: ``apply_billing_event(activated)`` — via SECURITY DEFINER
         writes the subscription/plan and the enqueue of the letter (kind=activated).
      2. the superuser: SELECT from ``billing_notification_outbox`` → 1 row of the needed
         kind, ``sent_at IS NULL`` (not yet sent).
      3. the app role: ``BillingNotificationsWorker(email=Mock).poll_once()`` → exactly
         one letter to OUR subscriber; the superuser: ``sent_at`` is set.
      4. a repeated ``poll_once()`` with the same mock → no second letter (the claim is idempotent).
    """
    superuser_url = os.getenv("TEST_SUPERUSER_DATABASE_URL", TEST_URL)
    tid = uuid.uuid4()
    user_id = uuid.uuid4()
    user_email = f"payer-{user_id.hex}@sim.rs"
    event_id = f"evt-sim-{uuid.uuid4().hex}"
    subscription_id = f"sub-sim-{uuid.uuid4().hex}"
    customer_id = f"cus-sim-{uuid.uuid4().hex}"
    period_end = datetime(2030, 1, 1, tzinfo=timezone.utc)

    app_engine = create_async_engine(TEST_URL, poolclass=NullPool, future=True)
    app_sessions = async_sessionmaker(
        app_engine, class_=AsyncSession, expire_on_commit=False
    )

    # 0. Setup under the superuser: a tenant + a subscriber with a valid email.
    sup_setup = create_async_engine(superuser_url, poolclass=NullPool, future=True)
    try:
        async with sup_setup.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tenants (id, country_code, tier, status) "
                    "VALUES (:id, 'RS', 'lite', 'active')"
                ),
                {"id": tid},
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, tenant_id, locale, role, email) "
                    "VALUES (:id, :tid, 'ru', 'admin', :email)"
                ),
                {"id": user_id, "tid": tid, "email": user_email},
            )
    finally:
        await sup_setup.dispose()

    activated = BillingEvent(
        provider="stripe",
        provider_event_id=event_id,
        type="activated",
        provider_customer_id=customer_id,
        provider_subscription_id=subscription_id,
        plan_key="pro",
        current_period_end=period_end,
        tenant_id=str(tid),
    )

    try:
        # 1. The core under the app role (the webhook WITHOUT a session/GUC) — all via SECURITY DEFINER.
        async with app_sessions() as session:
            await apply_billing_event(session, activated)

        # 2. The superuser: the letter is queued, not yet sent.
        sup_check = create_async_engine(superuser_url, future=True)
        try:
            async with sup_check.begin() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT kind, sent_at FROM billing_notification_outbox "
                            "WHERE provider = :p AND event_id = :e"
                        ),
                        {"p": "stripe", "e": event_id},
                    )
                ).fetchall()
        finally:
            await sup_check.dispose()
        assert len(rows) == 1, "the enqueue must put exactly one row of the letter"
        assert rows[0].kind == "activated"
        assert rows[0].sent_at is None, "the letter is not sent before the worker"

        # 3. The worker under the app role (SECURITY DEFINER pending/mark) → one letter.
        mock = MockEmailSender()
        worker = BillingNotificationsWorker(
            email_sender=mock,
            session_factory=app_sessions,
            cabinet_base_url="https://nab.rs",
        )
        sent = await worker.poll_once()
        assert sent >= 1
        ours = [m for m in mock.outbox if m.to == user_email]
        assert len(ours) == 1, "to OUR subscriber — exactly one letter"
        assert "подписка" in ours[0].subject or "оплата" in ours[0].subject.lower()

        sup_sent = create_async_engine(superuser_url, future=True)
        try:
            async with sup_sent.begin() as conn:
                sent_at = (
                    await conn.execute(
                        text(
                            "SELECT sent_at FROM billing_notification_outbox "
                            "WHERE provider = :p AND event_id = :e"
                        ),
                        {"p": "stripe", "e": event_id},
                    )
                ).scalar_one()
        finally:
            await sup_sent.dispose()
        assert sent_at is not None, "the claim must set sent_at"

        # 4. A repeated run with the same mock — there is no second letter to OUR subscriber.
        await worker.poll_once()
        ours_again = [m for m in mock.outbox if m.to == user_email]
        assert len(ours_again) == 1, (
            "a repeated poll_once must not send a second letter (the claim is idempotent "
            "by sent_at IS NULL)"
        )
    finally:
        # Cleanup: the outbox BEFORE the tenants (the FK tenant_id), then the subscription/event/user.
        sup_clean = create_async_engine(superuser_url, future=True)
        try:
            async with sup_clean.begin() as conn:
                await conn.execute(
                    text(
                        "DELETE FROM billing_notification_outbox "
                        "WHERE provider = :p AND event_id = :e"
                    ),
                    {"p": "stripe", "e": event_id},
                )
                await conn.execute(
                    text("DELETE FROM billing_webhook_events WHERE event_id = :e"),
                    {"e": event_id},
                )
                await conn.execute(
                    text(
                        "DELETE FROM billing_subscriptions "
                        "WHERE provider_subscription_id = :s"
                    ),
                    {"s": subscription_id},
                )
                await conn.execute(
                    text("DELETE FROM users WHERE id = :id"), {"id": user_id}
                )
                await conn.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": tid}
                )
        finally:
            await sup_clean.dispose()
        await app_engine.dispose()
