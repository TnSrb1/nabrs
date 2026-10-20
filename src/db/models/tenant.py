"""Tenant and account models.

DB_SCHEMA.md v1.1 §2.3 tenants, §2.4 users, §2.5 user_channels,
§2.18 subscriptions, §2.19 anti_abuse_identifiers.

All tables here are tenant-scoped and carry RLS policies (see §4); RLS is
applied in the migration, not at the ORM layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    ForeignKey,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# dict[str, Any], and not the output by values: without an annotation the type is dict[str, object],
# and each `**_UUID_PK` fits no overload of mapped_column, giving
# ~13 errors at the use-site. The annotation is typical; the set of keys and values is the same.
_UUID_PK: dict[str, Any] = dict(primary_key=True, server_default=func.gen_random_uuid())


class Tenant(Base):
    """Subscriber legal entity (DB_SCHEMA §2.3). ``pib`` -> ``tax_id`` +
    ``country_code`` after ADR-033. Composite uniqueness is enforced by the
    partial unique index ``tenants_country_tax_id_uniq`` (see §3 / migration)."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    country_code: Mapped[str] = mapped_column(
        CHAR(2), ForeignKey("countries.code"), nullable=False
    )
    tax_id: Mapped[str | None] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(Text)
    trial_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # The freeze axis (0012): NULL = unlimited active, otherwise access until this date.
    # On trial it is set = trial_until (signup), so that after the expiry
    # the freeze (domain/access.is_frozen) fires and stops the mailing/AI.
    access_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str | None] = mapped_column(Text)
    tz: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    """User within a tenant (DB_SCHEMA §2.4).

    role: 'user' (default) or 'admin'. First admin is set via SQL by owner:
      UPDATE users SET role='admin' WHERE email='...';
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    email: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    email_status: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    phone_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    role: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'user'")
    )
    locale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="users_role_check"),
    )


class UserChannel(Base):
    """Messenger channel binding of a user (DB_SCHEMA §2.5)."""

    __tablename__ = "user_channels"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    user_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    channel: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    linked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # The language of THIS delivery channel (0038). NULL = inherit users.locale — not
    # “unknown”: a copy of the language at binding would make the channel disagree with the cabinet
    # for those who did not choose a language in the bot. It is changed by the /language command in the bot.
    locale: Mapped[str | None] = mapped_column(Text)


class Subscription(Base):
    """Tenant subscription (DB_SCHEMA §2.18, ADR-033 §5.6)."""

    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    tier: Mapped[str] = mapped_column(Text, nullable=False)
    payment_processor: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'paddle'")
    )
    paddle_subscription_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    next_billed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "tier IN ('free','lite','starter','pro','business','enterprise')",
            name="subscriptions_tier_check",
        ),
        CheckConstraint(
            "payment_processor IN " "('paddle','corvuspay','uzcard','kaspi','stripe')",
            name="subscriptions_payment_processor_check",
        ),
        CheckConstraint(
            "status IN ('trial','active','suspended','cancelled')",
            name="subscriptions_status_check",
        ),
    )


class AntiAbuseIdentifier(Base):
    """Universal anti-abuse identifier (DB_SCHEMA §2.19, ADR-018 revised).

    One active ``value_hash`` per 12 months -> enforced by partial unique index
    ``anti_abuse_value_hash_active_uniq`` (see §3 / migration)."""

    __tablename__ = "anti_abuse_identifiers"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    tenant_id: Mapped[str | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    identifier_type: Mapped[str] = mapped_column(Text, nullable=False)
    value_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    value_last4: Mapped[str | None] = mapped_column(Text)
    risk_score_at_signup: Mapped[int | None] = mapped_column(Integer)
    verification_method: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    region: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("countries.code"))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "identifier_type IN ("
            "'serbian_pib_doo','serbian_jmbg','card_fingerprint',"
            "'email_domain','device_fingerprint','manual_b2b_attestation')",
            name="anti_abuse_identifiers_identifier_type_check",
        ),
    )
