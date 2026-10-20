"""tender_awards — table of tender award decisions (F03 eNabavke)

Source: DocumentTypeId = 11 (F03 regular) AND 204 (F03 for framework/DSN).
Data intercepted at STAGE 1 (VG-05, PR #234); parser + filling — STAGE 3.

ONE public table without RLS (awards are open data, not tenant-scoped).
FK to ``tenders_normalized`` with ON DELETE CASCADE.

Search key: ``(tender_id, lot_number, decision_date)`` — a regular composite
index (without UNIQUE: HasChanged=True produces a new record for the same lot).

Additive on top of 0017. No DROP/ALTER of existing objects — only CREATE.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-21
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "tender_awards",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tender_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenders_normalized.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_uid", sa.Text(), nullable=False),
        sa.Column("document_type_id", sa.SmallInteger(), nullable=False),
        sa.Column("lot_number", sa.Text(), nullable=True),
        sa.Column("lot_title", sa.Text(), nullable=True),
        sa.Column("winner_name", sa.Text(), nullable=True),
        sa.Column("winner_pib", sa.Text(), nullable=True),
        sa.Column("customer_pib", sa.Text(), nullable=True),
        sa.Column("customer_name", sa.Text(), nullable=True),
        sa.Column("decision_date", sa.Date(), nullable=True),
        sa.Column("award_amount_rsd", sa.Numeric(18, 2), nullable=True),
        sa.Column("award_amount_vat_rsd", sa.Numeric(18, 2), nullable=True),
        sa.Column("award_amount_eur", sa.Numeric(18, 2), nullable=True),
        sa.Column("currency_id", sa.SmallInteger(), nullable=True),
        sa.Column("contract_date", sa.Date(), nullable=True),
        sa.Column(
            "is_framework",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("duration_months", sa.Integer(), nullable=True),
        sa.Column(
            "has_changed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("captured_at", TS, nullable=True),
        sa.Column(
            "created_at",
            TS,
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Indexes (without UNIQUE — see docstring)
    op.create_index(
        "tender_awards_tender_id_idx",
        "tender_awards",
        ["tender_id"],
    )
    op.create_index(
        "tender_awards_winner_pib_idx",
        "tender_awards",
        ["winner_pib"],
    )
    op.create_index(
        "tender_awards_customer_pib_idx",
        "tender_awards",
        ["customer_pib"],
    )
    op.create_index(
        "tender_awards_decision_date_idx",
        "tender_awards",
        ["decision_date"],
    )
    op.create_index(
        "tender_awards_tender_lot_date_idx",
        "tender_awards",
        ["tender_id", "lot_number", "decision_date"],
    )


def downgrade() -> None:
    op.drop_table("tender_awards")
