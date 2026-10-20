"""tender_folders + folder_items + tender_notes (VG-17 workflow/retention)

Three tenant-scoped tables under FORCE ROW LEVEL SECURITY (a tenant sees only its own
rows; direct policy ``tenant_isolation`` on ``tenant_id``, modeled on
billing_subscriptions 0028 / tender_user_status 0008):

* ``tender_folders`` — the tenant’s “my tenders” folders (id, tenant_id, name).
  UNIQUE(tenant_id, lower(name)) — no duplicate names within a tenant.
* ``folder_items`` — membership of a tender in a folder (folder_id, tender_id), with a direct
  ``tenant_id`` column (direct RLS, no join policy). UNIQUE(folder_id,
  tender_id) — one tender at most once in a folder. FK to tender_folders
  (ON DELETE CASCADE) and tenders_normalized (ON DELETE CASCADE).
* ``tender_notes`` — a free-form subscriber note on a tender, one per pair
  (tenant_id, tender_id) — UNIQUE. FK to tenders_normalized (ON DELETE CASCADE).

Cross-tenant access is needed nowhere → SECURITY DEFINER is NOT introduced. Grants to the
tendersrb_app role — via ALTER DEFAULT PRIVILEGES from 0021 (new tables are
auto-granted). “Watching”/deadline reminders reuse the existing
``tender_user_status`` (0008) + the worker ``src/workers/reminders.py`` — no new
tables for watch.

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-15
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _enable_force_rls(table: str) -> None:
    """ENABLE + direct policy tenant_isolation + FORCE (model 0028)."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
    )
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    # 1. tender_folders — tenant folders.
    op.execute("""
        CREATE TABLE tender_folders (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT tender_folders_name_not_blank CHECK (length(btrim(name)) > 0)
        )
        """)
    # Name uniqueness within a tenant (case-insensitive).
    op.execute(
        "CREATE UNIQUE INDEX tender_folders_tenant_name_uniq "
        "ON tender_folders (tenant_id, lower(name))"
    )
    _enable_force_rls("tender_folders")

    # 2. folder_items — membership of a tender in a folder (direct tenant_id column).
    op.execute("""
        CREATE TABLE folder_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            folder_id uuid NOT NULL REFERENCES tender_folders(id) ON DELETE CASCADE,
            tender_id uuid NOT NULL
                REFERENCES tenders_normalized(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT folder_items_folder_tender_uniq UNIQUE (folder_id, tender_id)
        )
        """)
    op.execute("CREATE INDEX folder_items_tenant_idx ON folder_items (tenant_id)")
    op.execute("CREATE INDEX folder_items_tender_idx ON folder_items (tender_id)")
    _enable_force_rls("folder_items")

    # 3. tender_notes — one note per (tenant, tender).
    op.execute("""
        CREATE TABLE tender_notes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            tender_id uuid NOT NULL
                REFERENCES tenders_normalized(id) ON DELETE CASCADE,
            body text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT tender_notes_tenant_tender_uniq UNIQUE (tenant_id, tender_id)
        )
        """)
    _enable_force_rls("tender_notes")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tender_notes")
    op.execute("DROP TABLE IF EXISTS folder_items")
    op.execute("DROP TABLE IF EXISTS tender_folders")
