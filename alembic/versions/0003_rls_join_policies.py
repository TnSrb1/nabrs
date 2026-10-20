"""RLS join-based policies for tenant_id-less tables (Q-27)

Adds the tenant_isolation policies for the 4 tables listed in
docs/DB_SCHEMA.md §4 as tenant-scoped but WITHOUT a tenant_id column:
profile_revisions, delivery_outbox, delivery_attempts, tender_ai_analyses.

RLS is already ENABLED on these tables in migration 0001 (default-deny, no
policy) — this migration does NOT re-enable it, it only CREATEs the
join-based policies via profile_id -> profiles.tenant_id (Q-27, Variant A
decided by owner in Chat 17). tender_ai_analyses is a special case: rows with
profile_id IS NULL (level-1 quick analyses) stay shared across tenants, like
the tenders themselves; profile-scoped rows are isolated by the join.
current_setting style matches 0001 (no missing_ok flag). ADR-002.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # RLS already ENABLED on these 4 tables in 0001 (default-deny, no policy).
    # Here we only CREATE the join-based tenant_isolation policies.
    op.execute(
        "CREATE POLICY tenant_isolation ON profile_revisions\n"
        "  USING (profile_id IN (\n"
        "    SELECT id FROM profiles\n"
        "    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid))"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON delivery_outbox\n"
        "  USING (profile_id IN (\n"
        "    SELECT id FROM profiles\n"
        "    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid))"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON delivery_attempts\n"
        "  USING (outbox_id IN (\n"
        "    SELECT id FROM delivery_outbox\n"
        "    WHERE profile_id IN (\n"
        "      SELECT id FROM profiles\n"
        "      WHERE tenant_id = current_setting('app.current_tenant_id')::uuid)))"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON tender_ai_analyses\n"
        "  USING (profile_id IS NULL OR profile_id IN (\n"
        "    SELECT id FROM profiles\n"
        "    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid))"
    )


def downgrade() -> None:
    # Drop only the policies. RLS stays ENABLED on these tables (from 0001).
    for tbl in (
        "tender_ai_analyses",
        "delivery_attempts",
        "delivery_outbox",
        "profile_revisions",
    ):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {tbl}")
