"""add tenders_normalized.contract_type (subject type: goods/services/works)

Passing the procurement subject type from the eNabavke card header
(`CODE_CORE_ContractType.Name` → `_enrich.contract_type` → `normalize()` →
`NormalizedTender.contract_type`) through to the v6 Telegram card. A free TEXT enum
(goods/services/works), no CHECK — resolution/validation is on the adapter side
(`_CONTRACT_TYPES`). The card renders the label “Supply/Services/Works”
(`contract_type_label`) instead of the procedure fallback.

Additive on top of 0010 (single head). No DROP/ALTER of existing objects —
only ADD COLUMN nullable (rolled out to the LIVE prod, without locks and backfill).

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-06
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenders_normalized",
        sa.Column("contract_type", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenders_normalized", "contract_type")
