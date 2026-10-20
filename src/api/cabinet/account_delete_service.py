"""Self-deletion of an account from the cabinet (LEG-06, owner 10.10.2026).

One transaction under ``tendersrb_app`` (FORCE RLS, ``set_tenant`` FIRST): all the
affected tables are tenant-scoped, therefore cross-tenant SECURITY DEFINER is not
needed and neither is a new migration; the only global write is the audit, through the
existing ``app_admin_write_audit`` (0030, ``src.domain.audit.write_audit``).

Disabling the subscriber — THREE axes (``money-ai-tiers.md``): ``tenants.access_until``
(access), ``user_channels → disabled`` (the bot stops answering), ``users.deleted_at``
(login, including an already issued cookie, ``deps.require_active_access``). Here they
move by the same rules as ``app_admin_disable_subscriber`` (0037), but
ONLY for their own user, and the access/tenant axis — only if they are the last one:
that function shuts the whole tenant at once; for self-deletion of one of several
users it would be collateral damage.

Beyond disabling — anonymization: email/phone/chat_id are wiped, notes and
folders are deleted. The address hash in ``anti_abuse_identifiers`` is NOT touched (otherwise
delete-and-register-again = endless trials); only ``value_last4`` is wiped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import bindparam, text

from src.api import security
from src.domain.audit import write_audit

logger = logging.getLogger(__name__)

SELF_DELETE_ACTOR = "self-service"
SELF_DELETE_ACTION = "account_self_delete"

OK = "ok"
NOT_FOUND = "not_found"
EMAIL_MISMATCH = "email_mismatch"
ACTIVE_SUBSCRIPTION = "active_subscription"

#: Statuses of ``billing_subscriptions`` at which the provider still charges money.
_BILLING_LIVE = ("active", "past_due", "trialing")

_LOCK_USER_SQL = text(
    "SELECT email, locale FROM users "
    "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
    "AND deleted_at IS NULL FOR UPDATE"
)
_OTHER_USERS_SQL = text(
    "SELECT count(*) FROM users "
    "WHERE tenant_id = CAST(:tid AS uuid) AND deleted_at IS NULL "
    "AND id <> CAST(:uid AS uuid)"
)
_LIVE_BILLING_SQL = text(
    "SELECT count(*) FROM billing_subscriptions "
    "WHERE tenant_id = CAST(:tid AS uuid) AND status IN :live"
).bindparams(bindparam("live", expanding=True))

# Own channels: status 'disabled' (the bot does not answer, delivery stops), chat_id —
# set to NULL (a personal identifier, there is nothing to restore by it).
_CHANNELS_SQL = text(
    "UPDATE user_channels SET status = 'disabled', external_id = NULL "
    "WHERE user_id = CAST(:uid AS uuid)"
)
# One-time login/linking links of the user — revoked, so that they do not outlive the user.
_TOKENS_SQL = text("DELETE FROM auth_tokens WHERE user_id = CAST(:uid AS uuid)")
# email → a unique unreadable placeholder: it collides with nothing, does not
# resolve as a login address (plus deleted_at, which the resolver 0024 filters by).
# deleted_email_hash — the hash of the address (the same as in anti_abuse); by it the
# re-registration request finds the deleted user (0050) while the tenant is alive.
_USER_SQL = text(
    "UPDATE users SET deleted_at = now(), updated_at = now(), "
    "email = 'deleted+' || id::text || '@deleted.invalid', "
    "email_verified_at = NULL, email_status = 'deleted', "
    "phone = NULL, phone_verified_at = NULL, "
    "deleted_email_hash = :eh "
    "WHERE id = CAST(:uid AS uuid)"
)
# The operator’s notes (decision_comment) may contain an address. We wipe them for all requests
# of the tenant on ANY self-deletion: the tenant may remain alive (if not the last one).
_DECISION_NOTES_SQL = text(
    "UPDATE trial_requests SET decision_comment = NULL "
    "WHERE tenant_id = CAST(:tid AS uuid)"
)

# The whole tenant — only when the last user is deleted.
_TENANT_STATEMENTS = (
    text(
        "UPDATE tenants SET access_until = now(), status = 'deleted', "
        "display_name = NULL, tax_id = NULL, updated_at = now() "
        "WHERE id = CAST(:tid AS uuid)"
    ),
    text(
        "UPDATE profiles SET is_active = false, deleted_at = now(), name = NULL, "
        "ai_extra_context = NULL, ai_extra_context_embedding = NULL, "
        "ai_focus_questions = NULL "
        "WHERE tenant_id = CAST(:tid AS uuid)"
    ),
    text("DELETE FROM tender_notes WHERE tenant_id = CAST(:tid AS uuid)"),
    text("DELETE FROM tender_folders WHERE tenant_id = CAST(:tid AS uuid)"),
    text("DELETE FROM tender_user_status WHERE tenant_id = CAST(:tid AS uuid)"),
    text(
        "UPDATE award_watch_subscriptions SET is_active = false, winner_name = NULL "
        "WHERE tenant_id = CAST(:tid AS uuid)"
    ),
    text(
        "UPDATE trial_requests SET comment = NULL WHERE tenant_id = CAST(:tid AS uuid)"
    ),
    # The hash remains (anti-abuse), the last 4 characters of the address — personal.
    text(
        "UPDATE anti_abuse_identifiers SET value_last4 = NULL "
        "WHERE tenant_id = CAST(:tid AS uuid)"
    ),
)


@dataclass(frozen=True)
class DeleteResult:
    outcome: str
    #: The address BEFORE anonymization — only for the confirmation email, stored nowhere.
    email: str | None = None
    locale: str | None = None
    tenant_deleted: bool = False


async def delete_own_account(
    db, *, tenant_id: str, user_id: str, confirm_email: str
) -> DeleteResult:
    """Deletes the user ``user_id`` (and the tenant, if they are the last one). Commits.

    Refusals write NOTHING: ``email_mismatch`` (the confirmation did not match),
    ``active_subscription`` (the last user with a live paid subscription —
    otherwise the provider would keep charging a “deleted” account).
    """
    from src.db.session import set_current_tenant

    await set_current_tenant(db, tenant_id)
    params = {"tid": str(tenant_id), "uid": str(user_id)}
    row = (await db.execute(_LOCK_USER_SQL, params)).first()
    if row is None:
        await db.rollback()
        return DeleteResult(NOT_FOUND)
    email, locale = row.email, row.locale
    if security.normalize_email(confirm_email or "") != security.normalize_email(
        email or ""
    ):
        await db.rollback()
        return DeleteResult(EMAIL_MISMATCH)

    email_hash = security.hash_value(
        security.canonical_email_for_abuse(security.normalize_email(email or ""))
    )
    last = (await db.execute(_OTHER_USERS_SQL, params)).scalar_one() == 0
    if last:
        live = (
            await db.execute(_LIVE_BILLING_SQL, {**params, "live": list(_BILLING_LIVE)})
        ).scalar_one()
        if live:
            await db.rollback()
            return DeleteResult(ACTIVE_SUBSCRIPTION)

    await db.execute(_CHANNELS_SQL, params)
    await db.execute(_TOKENS_SQL, params)
    await db.execute(_USER_SQL, {**params, "eh": email_hash})
    await db.execute(_DECISION_NOTES_SQL, params)
    if last:
        for stmt in _TENANT_STATEMENTS:
            await db.execute(stmt, params)
    # The audit in the SAME transaction. Without email and IP: the journal is immutable, and deleting
    # personal data must not leave a copy of it in the journal.
    await write_audit(
        db,
        actor=SELF_DELETE_ACTOR,
        action=SELF_DELETE_ACTION,
        target_tenant=str(tenant_id),
        target_user=str(user_id),
        details={"tenant_deleted": last},
    )
    await db.commit()
    return DeleteResult(OK, email=email, locale=locale, tenant_deleted=last)
