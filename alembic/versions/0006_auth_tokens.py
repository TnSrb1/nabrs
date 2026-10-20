"""auth_tokens (magic-link + email-verify) + purge func for unconfirmed accounts

Sprint-19. Additive on top of 0005 (single head). No DROP/ALTER of existing
tables.

- ``auth_tokens`` — one-time tokens (token_hash = SHA-256 of the raw token),
  pre-auth, WITHOUT RLS (like sources/tenders, DB_SCHEMA §4).
- ``delete_unconfirmed_accounts()`` — SQL cleanup function: deletes users (and
  cascade their tenants if no confirmed users are left in the tenant) with
  ``email_verified_at IS NULL`` older than 72 hours (ADR-018 §4: «no click →
  auto-delete account after 72h»). Invocation by cron is OUTSIDE the Sprint-19 scope
  (TODO: infra/vps/cron + Sprint-17 Postmark inbound); here only the function.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-30
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")


def upgrade() -> None:
    op.create_table(
        "auth_tokens",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column(
            "user_id",
            UUID,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("consumed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "purpose IN ('email_verify','magic_link')",
            name="auth_tokens_purpose_check",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX auth_tokens_token_hash_uniq "
        "ON auth_tokens (token_hash)"
    )
    op.execute("CREATE INDEX ON auth_tokens (user_id, purpose)")
    op.execute(
        "CREATE INDEX auth_tokens_active_idx ON auth_tokens (expires_at) "
        "WHERE consumed_at IS NULL"
    )

    # Cleanup of unconfirmed accounts older than 72h (cron — out of scope).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION delete_unconfirmed_accounts()
        RETURNS integer AS $$
        DECLARE
            removed integer;
        BEGIN
            WITH del AS (
                DELETE FROM users u
                WHERE u.email_verified_at IS NULL
                  AND u.created_at < NOW() - INTERVAL '72 hours'
                RETURNING u.tenant_id
            )
            SELECT count(*) INTO removed FROM del;

            -- осиротевшие tenants без подтверждённых пользователей
            DELETE FROM tenants t
            WHERE t.created_at < NOW() - INTERVAL '72 hours'
              AND NOT EXISTS (SELECT 1 FROM users u2 WHERE u2.tenant_id = t.id);

            RETURN removed;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS delete_unconfirmed_accounts()")
    op.execute("DROP TABLE IF EXISTS auth_tokens CASCADE")
