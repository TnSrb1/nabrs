"""Template library model (EPIC 4 — the library of ready-made search templates)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, Boolean, ForeignKey, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base

# dict[str, Any] — see the explanation in tenant.py: without an annotation the type dict[str, object]
# does not fit any overload of mapped_column. The fix is typical, of the types, not of the values.
_UUID_PK: dict[str, Any] = dict(primary_key=True, server_default=func.gen_random_uuid())


class TemplateLibrary(Base):
    """A public/user search template of tenders.

    Public templates (is_public=True, status='approved') are created via the seed
    and are used as presets for the form /profiles/new?from_template=<id>.

    The fields status / is_public / created_by_user_id are laid down for EPIC 5
    (user suggestions + the moderation queue); the flow of EPIC 5 is not implemented.
    """

    __tablename__ = "template_library"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), **_UUID_PK)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    activity_kind: Mapped[str | None] = mapped_column(Text)

    keywords_include: Mapped[dict | None] = mapped_column(JSONB)
    classifications: Mapped[dict | None] = mapped_column(JSONB)

    # The translations of the storefront (0036): {"sr": {"name","description","activity_kind"},
    # "en": {...}}. The Russian lives in the columns above themselves — i18n only supplements.
    # NULL for the user templates (EPIC 5): their text was written by a person, it cannot be
    # machine-translated → the rendering shows the original (fail-open).
    i18n: Mapped[dict | None] = mapped_column(JSONB)

    # EPIC 5: user suggestions + moderation (laid down, not implemented)
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'approved'")
    )
    created_by_user_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))

    country_code: Mapped[str] = mapped_column(
        CHAR(2),
        ForeignKey("countries.code"),
        nullable=False,
        server_default=text("'RS'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
