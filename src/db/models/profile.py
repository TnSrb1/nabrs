"""Subscriber search-profile models.

DB_SCHEMA.md v1.1 §2.6 profiles, §2.7 profile_revisions.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# dict[str, Any] — see the explanation in tenant.py: without an annotation the type dict[str, object]
# does not fit any overload of mapped_column. The fix is typical, of the types, not of the values.
_UUID_PK: dict[str, Any] = dict(primary_key=True, server_default=func.gen_random_uuid())


class Profile(Base):
    """Subscriber search profile (DB_SCHEMA §2.6).

    NOTE on ``country_code``: DB_SCHEMA §2.6 declares the column literally as
    ``CHAR(2) NOT NULL DEFAULT 'RS'`` yet its prose and the routing query in
    §6.1 (``p.country_code IS NULL``) assume NULL is allowed for INT
    subscriptions. Implemented NOT NULL per the literal column definition; the
    contradiction is flagged in the PR body for owner.
    """

    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    tenant_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"))
    country_code: Mapped[str] = mapped_column(
        CHAR(2),
        ForeignKey("countries.code"),
        nullable=False,
        server_default=text("'RS'"),
    )
    target_language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ru'")
    )
    name: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool | None] = mapped_column()
    debug_match: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    keyword_dict: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )

    # Filtering
    # The “All tenders” mode: the whole flow of the portal without selection by topic (0043). Earlier
    # this state was reached by an EMPTY profile and was indistinguishable from "the client
    # did not set up the filters"; now it is explicit, and without it a profile without a topic
    # matches nothing (see STAGE_A_SQL in src/workers/matching.py).
    match_all: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    enabled_source_keys: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # {source_key: the ISO moment the source was switched on in the profile} (0045, SRC-0a R5):
    # a tender is delivered only if it arrived AFTER this moment (the fallback —
    # created_at). Written ONLY through src.domain.sources.apply_source_selection.
    source_opt_in: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    keywords_include: Mapped[dict | None] = mapped_column(JSONB)
    keywords_exclude: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    classifications: Mapped[dict | None] = mapped_column(JSONB)
    regions: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    min_amount_eur: Mapped[float | None] = mapped_column(Numeric)
    max_amount_eur: Mapped[float | None] = mapped_column(Numeric)
    include_no_amount: Mapped[bool | None] = mapped_column()
    procedure_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    languages: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    # The item type (goods|services|works) and the customers “watch”/“exclude”
    # (0046). The customer — the name as in the source, the comparison by the key
    # sql_customer_key (src/workers/matching.py). NULL/empty = no restriction.
    contract_types: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    customers_include: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    customers_exclude: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    min_days_until_deadline: Mapped[int | None] = mapped_column(Integer)

    # AI settings
    ai_analysis_level: Mapped[str | None] = mapped_column(Text)
    ai_focus_questions: Mapped[dict | None] = mapped_column(JSONB)
    ai_extra_context: Mapped[str | None] = mapped_column(Text)
    ai_extra_context_embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))

    # Delivery
    delivery_channels: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    delivery_schedule: Mapped[str | None] = mapped_column(Text)
    digest_cron: Mapped[str | None] = mapped_column(Text)
    quiet_hours_start: Mapped[time | None] = mapped_column(Time)
    quiet_hours_end: Mapped[time | None] = mapped_column(Time)

    # Audit
    version: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class ProfileRevision(Base):
    """Full profile snapshot on each change (DB_SCHEMA §2.7)."""

    __tablename__ = "profile_revisions"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    profile_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("profiles.id")
    )
    version: Mapped[int | None] = mapped_column(Integer)
    snapshot: Mapped[dict | None] = mapped_column(JSONB)
    changed_by_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    changed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
