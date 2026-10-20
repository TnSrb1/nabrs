"""ORM models for the alerts about winners (VG-06 E3b, migration 0019).

award_watch_subscriptions: the subscriptions tenant × winner_pib.
award_watch_sent: dedup of the delivered alerts (subscription_id × award_id).
WITHOUT RLS (the notifier is cross-tenant, the bot queries filter by tenant_id explicitly).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class AwardWatchSubscription(Base):
    """The tenant’s subscription to a winner by PIB."""

    __tablename__ = "award_watch_subscriptions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    winner_pib: Mapped[str] = mapped_column(Text, nullable=False)
    winner_name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=func.cast(True, Boolean),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "winner_pib",
            name="award_watch_subscriptions_tenant_pib_uniq",
        ),
    )


class AwardWatchSent(Base):
    """A record of a delivered alert — dedup (subscription_id × award_id)."""

    __tablename__ = "award_watch_sent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("award_watch_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    award_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tender_awards.id", ondelete="CASCADE"),
        nullable=False,
    )
    sent_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "award_id",
            name="award_watch_sent_sub_award_uniq",
        ),
    )
