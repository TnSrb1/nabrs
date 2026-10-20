"""The one-time auth tokens of the cabinet (Sprint-19, + channel_bind since Chat 26).

``auth_tokens`` — passwordless authentication (ARCHITECTURE §4): email-verify
(TTL 24h) and magic-link (TTL ~15 min). Since Chat 26 — the third purpose
``channel_bind`` (TTL ~15 min, single-use): the bind token for the cabinet↔bot stitching
(deep-link ``/start <token>`` → ``user_channels(telegram)``, C-04). The DB stores
only the SHA-256 of the raw token (``token_hash``), the token itself lives only in the email/URL.

The **pre-auth** table (the lookup by ``token_hash`` BEFORE the session is established), therefore
NOT tenant-scoped and WITHOUT RLS — like ``sources``/the tenders (DB_SCHEMA §4). Created by
migration 0006 (a single head over 0005), additive; the CHECK set is extended by
migration 0007 (``channel_bind``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class AuthToken(Base):
    """A one-time token of sign-in/verification/binding (migrations 0006, 0007)."""

    __tablename__ = "auth_tokens"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('email_verify','magic_link','channel_bind')",
            name="auth_tokens_purpose_check",
        ),
    )
