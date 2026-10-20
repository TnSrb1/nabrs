"""auth_tokens.purpose += 'channel_bind' (deep-link binding of Telegram, T3)

Chat 26, T3 (C-04). Additive on top of 0006 (single head). Extends the allowed
values of CHECK ``auth_tokens_purpose_check`` with a third purpose,
``channel_bind`` — a one-time bind token that the cabinet issues for its
``user_id`` and the bot redeems in ``/start <token>``, creating ``user_channels``
(linking cabinet↔bot). No DROP/ALTER of columns: only the CHECK constraint is
recreated (the set of values is extended); downgrade returns the previous
set.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-02
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "auth_tokens_purpose_check"
_NEW = "purpose IN ('email_verify','magic_link','channel_bind')"
_OLD = "purpose IN ('email_verify','magic_link')"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "auth_tokens", type_="check")
    op.create_check_constraint(_CONSTRAINT, "auth_tokens", _NEW)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "auth_tokens", type_="check")
    op.create_check_constraint(_CONSTRAINT, "auth_tokens", _OLD)
