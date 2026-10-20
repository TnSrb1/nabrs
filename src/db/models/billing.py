"""Billing models (VG-11.1, migration 0028).

``billing_subscriptions`` — tenant-scoped (FORCE RLS): the tenant’s subscription with the
payment gateway. The cross-tenant write from the webhook — through SECURITY DEFINER
``app_billing_upsert_subscription``; the authenticated read of one’s own subscription in the cabinet — under
``set_tenant``. ``billing_webhook_events`` — a platform table of webhook deduplication
(without RLS, without PII), needed only by the core :mod:`src.billing.service`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

_UUID_PK = dict(primary_key=True, server_default=func.gen_random_uuid())


class BillingSubscription(Base):
    """The tenant’s subscription with the payment gateway (DB_SCHEMA, VG-11.1)."""

    __tablename__ = "billing_subscriptions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(Text)
    provider_subscription_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    plan_key: Mapped[str | None] = mapped_column(Text)
    current_period_end: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subscription_id",
            name="billing_subscriptions_provider_sub_uniq",
        ),
    )


class BillingWebhookEvent(Base):
    """Webhook deduplication: (provider, event_id) UNIQUE (VG-11.1).

    A platform table without RLS — only the idempotency of processing, without PII.
    """

    __tablename__ = "billing_webhook_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "event_id", name="billing_webhook_events_provider_event_uniq"
        ),
    )
