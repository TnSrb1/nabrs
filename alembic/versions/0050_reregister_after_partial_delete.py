"""Re-registration also after self-deletion of NOT the last user (review #667, owner 11.10).

0049 opened a request only if the tenant was ``deleted`` (``app_reregister_request``).
Self-deletion of one of several users does not close the tenant — the request was silently not created, and a person who deleted himself from a team tenant could not return.

What the migration changes (additive, RLS is not weakened, everything cross-tenant — SECURITY DEFINER):

1. ``users.deleted_email_hash text NULL`` — the hash of the address (the same ``hash_value(canonical_email_for_abuse)``
   as in ``anti_abuse_identifiers``), written on self-deletion. The address is already anonymized (email → placeholder), without the hash a deleted address cannot be found in a live tenant.
   Deletions BEFORE 0050 — NULL: for them the previous path works (tenant ``deleted``).
2. ``app_reregister_request`` — the request is created if the hash in ``anti_abuse_identifiers``
   belongs to a tenant that is ``deleted`` (as before) OR contains a deleted user with this hash (new). The tenant status is no longer a condition.
3. ``app_reregister_claim`` — lifts the block by APPROVAL on the hash row in any tenant
   (the condition ``tenants.status = 'deleted'`` is removed: an approval on a live tenant is the same block).
   Once-only and monotonicity are as before: the hash row moves to a “spent” hash.

The outward answer does not change: ``request_reregistration`` returns the id or None, the caller shows the same answer (anti-enumeration).

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-11
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REQUEST_0050 = """
        CREATE OR REPLACE FUNCTION app_reregister_request(
            p_hash text, p_lang text, p_email text
        )
        RETURNS TABLE(request_id uuid)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_tenant uuid;
            v_id uuid;
        BEGIN
            -- Хеш принадлежит удалённому аккаунту (тенант deleted ИЛИ удалён
            -- пользователь этого тенанта с тем же хешем) и одобрения ещё нет.
            SELECT a.tenant_id INTO v_tenant
              FROM anti_abuse_identifiers a
             WHERE a.value_hash = p_hash
               AND a.reregister_approved_at IS NULL
               AND (
                    EXISTS (SELECT 1 FROM tenants t
                             WHERE t.id = a.tenant_id AND t.status = 'deleted')
                 OR EXISTS (SELECT 1 FROM users u
                             WHERE u.tenant_id = a.tenant_id
                               AND u.deleted_email_hash = p_hash
                               AND u.deleted_at IS NOT NULL)
               )
             ORDER BY a.created_at DESC, a.id DESC
             LIMIT 1;
            IF v_tenant IS NULL THEN
                RETURN;
            END IF;
            -- Одна ожидающая заявка на тенант: повтор блокируется.
            PERFORM 1 FROM trial_requests
             WHERE tenant_id = v_tenant AND status = 'pending'
               AND comment LIKE 'reregister_request:%';
            IF FOUND THEN
                RETURN;
            END IF;
            INSERT INTO trial_requests (tenant_id, comment)
            VALUES (v_tenant, 'reregister_request:' || p_lang || ':' || p_email)
            RETURNING id INTO v_id;
            request_id := v_id;
            RETURN NEXT;
        END
        $$
        """

_CLAIM_0050 = """
        CREATE OR REPLACE FUNCTION app_reregister_claim(p_hash text)
        RETURNS TABLE(tenant_id uuid, trial_used boolean)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_id uuid;
            v_tenant uuid;
            v_expires timestamptz;
        BEGIN
            SELECT a.id, a.tenant_id, a.expires_at INTO v_id, v_tenant, v_expires
              FROM anti_abuse_identifiers a
             WHERE a.value_hash = p_hash
               AND a.reregister_approved_at IS NOT NULL
             ORDER BY a.created_at DESC, a.id DESC
             LIMIT 1
             FOR UPDATE OF a;
            IF v_id IS NULL THEN
                RETURN;
            END IF;
            -- Блок снят однократно: старая запись уходит на «погашенный» хеш.
            UPDATE anti_abuse_identifiers
               SET value_hash = encode(
                       sha256(convert_to(p_hash || ':' || v_id::text, 'UTF8')), 'hex')
             WHERE id = v_id;
            tenant_id := v_tenant;
            trial_used := v_expires > now();
            RETURN NEXT;
        END
        $$
        """

# The versions of 0049 — for downgrade (they require tenants.status = 'deleted').
_REQUEST_0049 = """
        CREATE OR REPLACE FUNCTION app_reregister_request(
            p_hash text, p_lang text, p_email text
        )
        RETURNS TABLE(request_id uuid)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_tenant uuid;
            v_id uuid;
        BEGIN
            SELECT a.tenant_id INTO v_tenant
              FROM anti_abuse_identifiers a
              JOIN tenants t ON t.id = a.tenant_id
             WHERE a.value_hash = p_hash
               AND t.status = 'deleted'
               AND a.reregister_approved_at IS NULL
             ORDER BY a.created_at DESC, a.id DESC
             LIMIT 1;
            IF v_tenant IS NULL THEN
                RETURN;
            END IF;
            PERFORM 1 FROM trial_requests
             WHERE tenant_id = v_tenant AND status = 'pending'
               AND comment LIKE 'reregister_request:%';
            IF FOUND THEN
                RETURN;
            END IF;
            INSERT INTO trial_requests (tenant_id, comment)
            VALUES (v_tenant, 'reregister_request:' || p_lang || ':' || p_email)
            RETURNING id INTO v_id;
            request_id := v_id;
            RETURN NEXT;
        END
        $$
        """

_CLAIM_0049 = """
        CREATE OR REPLACE FUNCTION app_reregister_claim(p_hash text)
        RETURNS TABLE(tenant_id uuid, trial_used boolean)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_id uuid;
            v_tenant uuid;
            v_expires timestamptz;
        BEGIN
            SELECT a.id, a.tenant_id, a.expires_at INTO v_id, v_tenant, v_expires
              FROM anti_abuse_identifiers a
              JOIN tenants t ON t.id = a.tenant_id
             WHERE a.value_hash = p_hash
               AND a.reregister_approved_at IS NOT NULL
               AND t.status = 'deleted'
             ORDER BY a.created_at DESC, a.id DESC
             LIMIT 1
             FOR UPDATE OF a;
            IF v_id IS NULL THEN
                RETURN;
            END IF;
            UPDATE anti_abuse_identifiers
               SET value_hash = encode(
                       sha256(convert_to(p_hash || ':' || v_id::text, 'UTF8')), 'hex')
             WHERE id = v_id;
            tenant_id := v_tenant;
            trial_used := v_expires > now();
            RETURN NEXT;
        END
        $$
        """


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_email_hash text"
    )
    op.execute(_REQUEST_0050)
    op.execute(_CLAIM_0050)


def downgrade() -> None:
    op.execute(_REQUEST_0049)
    op.execute(_CLAIM_0049)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_email_hash")
