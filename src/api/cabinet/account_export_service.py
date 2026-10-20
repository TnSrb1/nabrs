"""“Download my data” export from the cabinet (LEG-06, second half, owner 14.10.2026).

One read-only transaction under ``tendersrb_app`` (FORCE RLS), ``set_tenant``
FIRST. Not a single write to the DB, no migrations needed. The scope is double: RLS cuts by
tenant, and each query additionally carries an explicit ``WHERE`` on ``tenant_id`` /
``user_id`` — ``auth_tokens`` and ``user_channels`` have no direct
``tenant_id`` (the former has no RLS at all), so one policy cannot be relied on.

The contents — ONLY what is explicitly listed below appears (explicit columns, not
``SELECT *``): a new secret column does not get into the file by itself.
The decision “table → in the export / no → why” is in
``docs/reports/TESTING_account-data-export.md``. Not exported: token values and hashes (``auth_tokens.token_hash``, ``anti_abuse_identifiers.value_hash``
/ ``value_last4`` — only the fact of existence), internal operator notes
(``trial_requests.decision_comment``), the internal AI cost (``cost_usd_total``), payment-provider identifiers, other users
of the tenant.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from src.db.session import set_current_tenant

#: Row cap of the notification journals (the most recent): the file stays downloadable.
LOG_ROWS_LIMIT = 10000

# --- account and tenant -----------------------------------------------------
_USER_SQL = text(
    "SELECT id, email, email_verified_at, email_status, phone, phone_verified_at, "
    "role, locale, created_at, updated_at FROM users "
    "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
    "AND deleted_at IS NULL"
)
_TENANT_SQL = text(
    "SELECT id, country_code, tax_id, display_name, tier, status, trial_until, "
    "access_until, locale, tz, created_at FROM tenants "
    "WHERE id = CAST(:tid AS uuid)"
)
_CHANNELS_SQL = text(
    "SELECT uc.channel, uc.external_id, uc.status, uc.linked_at, uc.locale "
    "FROM user_channels uc JOIN users u ON u.id = uc.user_id "
    "WHERE uc.user_id = CAST(:uid AS uuid) AND u.tenant_id = CAST(:tid AS uuid) "
    "ORDER BY uc.linked_at NULLS LAST, uc.channel, uc.id"
)

# --- tenant content ---------------------------------------------------------
# Search templates in full (all settings), EXCEPT the vector ai_extra_context_embedding
# (an internal representation of the text, not user data).
_PROFILES_SQL = text(
    "SELECT to_jsonb(p) - 'ai_extra_context_embedding' AS data FROM profiles p "
    "WHERE p.tenant_id = CAST(:tid AS uuid) AND p.deleted_at IS NULL "
    "ORDER BY p.created_at, p.id"
)
_FOLDERS_SQL = text(
    "SELECT id, name, created_at, updated_at FROM tender_folders "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY created_at, id"
)
_FOLDER_ITEMS_SQL = text(
    "SELECT fi.folder_id, fi.tender_id, t.title AS tender_title, "
    "t.source_url AS tender_url, fi.created_at "
    "FROM folder_items fi LEFT JOIN tenders_normalized t ON t.id = fi.tender_id "
    "WHERE fi.tenant_id = CAST(:tid AS uuid) ORDER BY fi.created_at, fi.id"
)
_NOTES_SQL = text(
    "SELECT n.id, n.tender_id, t.title AS tender_title, t.source_url AS tender_url, "
    "n.body, n.created_at, n.updated_at "
    "FROM tender_notes n LEFT JOIN tenders_normalized t ON t.id = n.tender_id "
    "WHERE n.tenant_id = CAST(:tid AS uuid) ORDER BY n.created_at, n.id"
)
_STATUSES_SQL = text(
    "SELECT s.tender_id, t.title AS tender_title, t.source_url AS tender_url, "
    "s.status, s.created_at, s.updated_at "
    "FROM tender_user_status s LEFT JOIN tenders_normalized t ON t.id = s.tender_id "
    "WHERE s.tenant_id = CAST(:tid AS uuid) ORDER BY s.updated_at, s.id"
)
_AWARD_WATCH_SQL = text(
    "SELECT winner_pib, winner_name, is_active, created_at "
    "FROM award_watch_subscriptions WHERE tenant_id = CAST(:tid AS uuid) "
    "ORDER BY created_at, id"
)

# --- plan, requests, AI ----------------------------------------------------
_SUBSCRIPTIONS_SQL = text(
    "SELECT tier, status, payment_processor, started_at, next_billed_at, "
    "cancelled_at FROM subscriptions WHERE tenant_id = CAST(:tid AS uuid) "
    "ORDER BY started_at, id"
)
_BILLING_SQL = text(
    "SELECT provider, status, plan_key, current_period_end, created_at "
    "FROM billing_subscriptions WHERE tenant_id = CAST(:tid AS uuid) "
    "ORDER BY created_at, id"
)
_TRIALS_SQL = text(
    "SELECT status, comment, days, requested_at, decided_at FROM trial_requests "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY requested_at, id"
)
_AI_USAGE_SQL = text(
    "SELECT period, level, count, tokens_total FROM ai_usage "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY period, level"
)

# --- notification journals (dates and types) -------------------------------
_REMINDERS_SQL = text(
    "SELECT tender_id, threshold_days, sent_at FROM reminder_log "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY sent_at DESC, id LIMIT :lim"
)
_SUMMARIES_SQL = text(
    "SELECT summary_date, sent_at FROM summary_log "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY sent_at DESC, id LIMIT :lim"
)
_SUB_REMINDERS_SQL = text(
    "SELECT threshold_days, access_until_snapshot, sent_at "
    "FROM subscription_reminder_log WHERE tenant_id = CAST(:tid AS uuid) "
    "ORDER BY sent_at DESC, id LIMIT :lim"
)
_CHANNEL_DEAD_SQL = text(
    "SELECT channel, notified_at, resolved_at FROM channel_dead_notice_log "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY notified_at DESC, id LIMIT :lim"
)

# --- service: only the fact of existence ------------------------------------
_ANTI_ABUSE_SQL = text(
    "SELECT identifier_type, created_at, expires_at FROM anti_abuse_identifiers "
    "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY created_at, id"
)
# auth_tokens: no RLS → a filter on user_id is mandatory, values are not read at all.
_TOKENS_SQL = text(
    "SELECT purpose, count(*) AS count FROM auth_tokens "
    "WHERE user_id = CAST(:uid AS uuid) GROUP BY purpose ORDER BY purpose"
)


async def _rows(db, stmt, params: dict) -> list[dict]:
    return [dict(r._mapping) for r in (await db.execute(stmt, params)).all()]


async def build_account_export(db, *, tenant_id: str, user_id: str) -> dict | None:
    """Collects the export of one’s own data; ``None`` — the user does not exist/is deleted.

    SELECT only; the caller closes the transaction itself (``rollback`` is not needed —
    the session is released by exiting ``async with``).
    """
    await set_current_tenant(db, tenant_id)
    p = {"tid": str(tenant_id), "uid": str(user_id)}
    lim = {**p, "lim": LOG_ROWS_LIMIT}

    user = (await db.execute(_USER_SQL, p)).first()
    if user is None:
        return None
    tenant = (await db.execute(_TENANT_SQL, p)).first()

    profiles = [r["data"] for r in await _rows(db, _PROFILES_SQL, p)]
    return {
        "exported_at": datetime.now(timezone.utc),
        "account": dict(user._mapping),
        "tenant": dict(tenant._mapping) if tenant is not None else None,
        "channels": await _rows(db, _CHANNELS_SQL, p),
        "profiles": profiles,
        "folders": await _rows(db, _FOLDERS_SQL, p),
        "folder_items": await _rows(db, _FOLDER_ITEMS_SQL, p),
        "notes": await _rows(db, _NOTES_SQL, p),
        "tender_statuses": await _rows(db, _STATUSES_SQL, p),
        "award_watch": await _rows(db, _AWARD_WATCH_SQL, p),
        "subscriptions": await _rows(db, _SUBSCRIPTIONS_SQL, p),
        "billing_subscriptions": await _rows(db, _BILLING_SQL, p),
        "trial_requests": await _rows(db, _TRIALS_SQL, p),
        "ai_usage": await _rows(db, _AI_USAGE_SQL, p),
        "notification_history": {
            "tender_deadline_reminders": await _rows(db, _REMINDERS_SQL, lim),
            "daily_summaries": await _rows(db, _SUMMARIES_SQL, lim),
            "subscription_reminders": await _rows(db, _SUB_REMINDERS_SQL, lim),
            "channel_dead_notices": await _rows(db, _CHANNEL_DEAD_SQL, lim),
        },
        # Only the fact of existence: no hashes, no token values.
        "stored_but_not_exported": {
            "anti_abuse_identifiers": await _rows(db, _ANTI_ABUSE_SQL, p),
            "login_tokens_by_purpose": await _rows(db, _TOKENS_SQL, p),
        },
    }


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)  # UUID and other


def export_filename(now: datetime | None = None) -> str:
    return f"tendersrb-my-data-{(now or datetime.now(timezone.utc)):%Y-%m-%d}.json"


def export_bytes(data: dict) -> bytes:
    """UTF-8 JSON with indentation (human-readable), Cyrillic is not escaped."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default).encode(
        "utf-8"
    )
