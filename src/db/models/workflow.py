"""Workflow/retention: folders, membership, notes (VG-17, migration 0029).

Three tenant-scoped models under the direct RLS policy ``tenant_isolation`` (a direct
column ``tenant_id``, like ``tender_user_status`` 0008):

* :class:`TenderFolder` — the “my tenders” folder of the tenant. UNIQUE(tenant_id,
  lower(name)) is set by migration 0029 (there is no partial index — an ordinary
  functional UNIQUE index, not reflected in the ORM).
* :class:`FolderItem` — a tender in a folder. UNIQUE(folder_id, tender_id).
* :class:`TenderNote` — one free note per (tenant_id, tender_id).

“Watching”/reminders use the existing ``tender_user_status`` (0008) —
there is no separate model for the watch.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base


class TenderFolder(Base):
    """The “my tenders” folder within the tenant (migration 0029)."""

    __tablename__ = "tender_folders"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "length(btrim(name)) > 0", name="tender_folders_name_not_blank"
        ),
    )


class FolderItem(Base):
    """The membership of a tender in a folder (a direct column tenant_id; migration 0029)."""

    __tablename__ = "folder_items"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    folder_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tender_folders.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "folder_id", "tender_id", name="folder_items_folder_tender_uniq"
        ),
    )


class TenderNote(Base):
    """A free note on a tender, one per pair (tenant_id, tender_id)."""

    __tablename__ = "tender_notes"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "tender_id", name="tender_notes_tenant_tender_uniq"
        ),
    )
