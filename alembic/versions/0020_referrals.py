"""referrals — PLG referral loop (VG-27)

Table ``referrals``: registry of invitations for native virality via
Telegram deep-link (``?start=ref_<inviter_tg_id>``).

Design decisions (Chat 85, owner — option B, durable table):
  * ``inviter_user_id`` / ``invitee_user_id`` — bigint: Telegram chat_id,
    NOT ``users.id`` (that one is UUID). FK to ``users.id`` is impossible due to
    the type mismatch; identification goes via ``user_channels.external_id``.
  * ``UNIQUE(invitee_user_id)`` — 1 invitee → 1 inviter, rewarded exactly once
    (the core of the protection against repeated bonuses).
  * ``status``: ``pending`` → ``rewarded`` (or ``rejected``).
  * Bonus days — a date extension of ``tenants.access_until``; the tier is raised
    to ``'pro'`` for lite/starter, NOT lowered for business/enterprise.

Do NOT add an RLS/tenant policy for now: the referrals table works at the level of
Telegram users (``user_channels``), outside tenant isolation. Project-wide RLS is
enabled in VG-01; until then bot queries filter explicitly.

Additive on top of 0019. No DROP/ALTER of existing objects — only CREATE.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-01
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "referrals",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            primary_key=True,
            nullable=False,
        ),
        # Telegram chat_id of the inviter (bigint, NOT users.id UUID).
        sa.Column("inviter_user_id", sa.BigInteger(), nullable=False),
        # Telegram chat_id of the invitee; NULL until onboarding.
        sa.Column("invitee_user_id", sa.BigInteger(), nullable=True),
        # Payload from the deep-link, for audit.
        sa.Column("ref_code", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "bonus_days_inviter",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "bonus_days_invitee",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            TS,
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("rewarded_at", TS, nullable=True),
        # One invitee → one record → one bonus (anti-abuse).
        sa.UniqueConstraint("invitee_user_id", name="referrals_invitee_uniq"),
        sa.CheckConstraint(
            "status IN ('pending','rewarded','rejected')",
            name="referrals_status_check",
        ),
    )
    # For the query “how many did I invite”.
    op.create_index(
        "referrals_inviter_user_id_idx",
        "referrals",
        ["inviter_user_id"],
    )


def downgrade() -> None:
    op.drop_table("referrals")
