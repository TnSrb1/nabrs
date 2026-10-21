"""ai_spend_daily + ai_spend_alert_log — a daily snapshot of AI spend for the whole account

Per-tenant limits live in ``src/ai/usage.py::_TIER_LIMITS`` (#468), and there is no ceiling for
the WHOLE Anthropic account in the code and there will not be (the hard ceiling is in the Console — the owner's zone). Before public access an
observer is needed: how much was spent per day and per month. These two tables are its memory.

‼️ Why exactly a SNAPSHOT. ``ai_usage`` is a monthly aggregate
(PK ``(tenant_id, period, level)``, ``period`` = ``YYYY-MM`` UTC, accumulated
via ``INSERT ... ON CONFLICT DO UPDATE``). The row has NO timestamp at all,
so “spend per day” cannot be computed from it by any query. The daily
delta is computed as “today's cumulative total of the period MINUS yesterday's snapshot of THE SAME period” —
hence the ``period`` column in the snapshot: without it the first day of the month would give a negative
delta (yesterday's total of the previous month is larger than today's) or an inflated one.

‼️ Both tables are PLATFORM tables, there is NO ``tenant_id`` column in them — and this is not
economy but a mandatory condition of working. The invariant from the project rules requires a tenant-scoped table to have the
``tenant_isolation`` policy + ``ENABLE`` + ``FORCE ROW LEVEL SECURITY``; ``FORCE`` applies also to the OWNER, and the spend
worker runs privileged and cross-tenant (without
``SET app.current_tenant_id``) — it would read 0 rows from its own table.
The breakdown by tenants is needed only in the text of the message to the operator and is computed
on the fly from ``ai_usage``; storing it is not needed.

Access: ``REVOKE ALL ... FROM tendersrb_app`` — 0021 granted DEFAULT PRIVILEGES
for future tables, but these are read/written ONLY by the privileged worker (owner).
The cabinet and the bot do not access them; a direct SELECT under the app role → permission denied
(fail-closed, the pattern of ``admin_audit_log`` 0030 and
``billing_notification_outbox`` 0033).

``ai_spend_alert_log`` — a claim-then-send journal (pattern of ``subscription_reminder_log`` 0017):
PK ``(day, kind)`` = “one alert of one kind per one day”. The day's snapshot is overwritten on each run
(``ON CONFLICT (day) DO UPDATE``), so it cannot serve as the claim itself — the journal is separate.
A Telegram failure does NOT roll back the journal (anti-spam).

Additive on top of 0039 (single head): only CREATE of two new tables, not a single
DROP/ALTER of existing objects — rolls out on the live production.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Daily spend snapshot. day — PK: one snapshot per one day (UTC),
    # a repeated worker run OVERWRITES the row rather than producing new ones.
    # period is stored together with the snapshot: yesterday can be compared with today
    # only WITHIN one accounting period (see the docstring).
    op.execute("""
        CREATE TABLE ai_spend_daily (
            day date PRIMARY KEY,
            period text NOT NULL,
            cost_usd_total numeric NOT NULL,
            tokens_total bigint NOT NULL,
            calls integer NOT NULL,
            tenants_count integer NOT NULL,
            captured_at timestamptz NOT NULL DEFAULT now()
        )
        """)

    # 2. Alert journal: claim-then-send, “one alert kind once per day”.
    # kind — 'daily' | 'month' (thresholds are independent, each with its own claim).
    op.execute("""
        CREATE TABLE ai_spend_alert_log (
            day date NOT NULL,
            kind text NOT NULL,
            sent_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (day, kind)
        )
        """)

    # Platform tables outside the app-role contour: both are touched only by
    # the privileged worker. RLS is deliberately not set up here — there is no
    # tenant_id column, nothing to isolate (see the docstring).
    op.execute("REVOKE ALL ON ai_spend_daily FROM tendersrb_app")
    op.execute("REVOKE ALL ON ai_spend_alert_log FROM tendersrb_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ai_spend_alert_log CASCADE")
    op.execute("DROP TABLE IF EXISTS ai_spend_daily CASCADE")
