"""award_watch_subscriptions + award_watch_sent (VG-06 E3b)

Two new tables for winner alerts (subscriptions + dedup of sent items):

  award_watch_subscriptions:
    - tenant × winner_pib, is_active, UNIQUE(tenant_id, winner_pib)
    - UPSERT on re-subscription reactivates (ON CONFLICT ... SET is_active=true)

  award_watch_sent:
    - dedup (subscription_id × award_id), UNIQUE(subscription_id, award_id)
    - “watermark”: on subscribing, all already existing awards with the PIB are recorded at once

WITHOUT RLS (the notifier is a system component, cross-tenant; bot queries filter by tenant_id explicitly).
Additive on top of 0018. No DROP/ALTER of existing objects — only CREATE.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "award_watch_subscriptions",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("winner_pib", sa.Text(), nullable=False),
        sa.Column("winner_name", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            TS,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "winner_pib",
            name="award_watch_subscriptions_tenant_pib_uniq",
        ),
    )
    # The notifier looks up subscriptions by PIB (JOIN with tender_awards.winner_pib)
    op.create_index(
        "award_watch_subscriptions_winner_pib_idx",
        "award_watch_subscriptions",
        ["winner_pib"],
    )
    # The /watching command and gate checks look up by tenant_id
    op.create_index(
        "award_watch_subscriptions_tenant_id_idx",
        "award_watch_subscriptions",
        ["tenant_id"],
    )

    op.create_table(
        "award_watch_sent",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "subscription_id",
            sa.BigInteger(),
            sa.ForeignKey("award_watch_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "award_id",
            sa.BigInteger(),
            sa.ForeignKey("tender_awards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sent_at",
            TS,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "subscription_id",
            "award_id",
            name="award_watch_sent_sub_award_uniq",
        ),
    )
    op.create_index(
        "award_watch_sent_subscription_id_idx",
        "award_watch_sent",
        ["subscription_id"],
    )


def downgrade() -> None:
    op.drop_table("award_watch_sent")
    op.drop_table("award_watch_subscriptions")
