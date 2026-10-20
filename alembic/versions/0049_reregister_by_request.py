"""Re-registration after self-deletion — only via a request (issue #658, owner 10.10).

The owner's decision: one cannot register oneself on the address of a deleted account; one can
REQUEST it, the operator approves, after which the address is admitted to registration.
We do not introduce a request table: we reuse ``trial_requests`` (a marker in ``comment``
``reregister_request:<lang>:<email>``, like ``extension_request`` / ``upgrade_request``).

What the migration adds (all additive, RLS is not weakened — ALL cross-tenant
operations are only SECURITY DEFINER, ``SET search_path = public``):

1. ``anti_abuse_identifiers.reregister_approved_at timestamptz NULL`` — the mark
   “the operator approved re-registration on this hash”. The hash stays in place
   (anti-abuse is not weakened) until the person uses the permission.
2. ``app_reregister_request(p_hash, p_lang, p_email)`` — creates a pending request
   ONLY if the hash belongs to ``tenants.status = 'deleted'`` and there is neither
   a pending request nor an unused approval. For the others (a live account,
   an unknown hash) silently 0 rows — the answer outwards is the same (anti-enumeration).
3. ``app_admin_decide_reregister(p_id, p_approve, p_comment)`` — guarded UPDATE
   ``status = 'pending'`` (repeat → 0 rows). Returns the address from the request ONCE
   and at once wipes it in ``comment`` (personal data is not kept after the decision). Approval sets ``reregister_approved_at`` (once: ``IS NULL``).
4. ``app_reregister_claim(p_hash)`` — called by the registration: if the hash has an approval, it SWITCHES the old record to a “spent” hash
   ``sha256(hash || ':' || id)`` (the block is lifted, it will not trigger a second time — monotonic and one-off) and returns whether the trial was used (``expires_at > now()``).
5. ``app_admin_decide_trial_request`` (4 args) — the same contract, but re-registration requests are NOT decided via the COMMON path
   (the bot ``/trials``, access extension): only ``app_admin_decide_reregister`` decides them (otherwise the bot button would extend access for a deleted tenant and “eat” the request, not admitting the address to registration).
6. ``app_admin_list_trial_requests`` — the same signature, but for re-registration requests
   ``plan = 'reregister'`` and the ``email`` is taken from the request (a deleted tenant has no live users), so that the operator sees the request next to the others.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-10
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREFIX = "reregister_request"

# The list from 0034 — for downgrade.
_LIST_0034 = """
    CREATE OR REPLACE FUNCTION app_admin_list_trial_requests(
        p_limit int, p_offset int
    )
    RETURNS TABLE(
        id uuid,
        tenant_id uuid,
        email text,
        plan text,
        requested_at timestamptz,
        days int
    )
    LANGUAGE SQL STABLE SECURITY DEFINER
    SET search_path = public
    AS $$
      SELECT
          tr.id,
          tr.tenant_id,
          (SELECT u.email FROM users u
            WHERE u.tenant_id = tr.tenant_id AND u.deleted_at IS NULL
            ORDER BY u.created_at ASC
            LIMIT 1)                                    AS email,
          CASE
              WHEN tr.comment LIKE 'upgrade_request:%'
              THEN split_part(tr.comment, ':', 2)
              ELSE NULL
          END                                           AS plan,
          tr.requested_at,
          tr.days
      FROM trial_requests tr
      WHERE tr.status = 'pending'
      ORDER BY tr.requested_at ASC
      LIMIT p_limit OFFSET p_offset
    $$
    """

_DECIDE_0035 = """
    CREATE OR REPLACE FUNCTION app_admin_decide_trial_request(
        p_id uuid, p_status text, p_days int, p_comment text
    )
    RETURNS TABLE(tenant_id uuid, comment text)
    LANGUAGE SQL VOLATILE SECURITY DEFINER
    SET search_path = public
    AS $$
      UPDATE trial_requests
         SET status = p_status,
             days = p_days,
             decided_at = now(),
             decision_comment = p_comment
       WHERE id = p_id AND status = 'pending'
      RETURNING tenant_id, comment
    $$
    """

_DECIDE_0049 = """
    CREATE OR REPLACE FUNCTION app_admin_decide_trial_request(
        p_id uuid, p_status text, p_days int, p_comment text
    )
    RETURNS TABLE(tenant_id uuid, comment text)
    LANGUAGE SQL VOLATILE SECURITY DEFINER
    SET search_path = public
    AS $$
      UPDATE trial_requests
         SET status = p_status,
             days = p_days,
             decided_at = now(),
             decision_comment = p_comment
       WHERE id = p_id AND status = 'pending'
         AND (comment IS NULL OR comment NOT LIKE 'reregister_request:%')
      RETURNING tenant_id, comment
    $$
    """

_LIST_0049 = """
    CREATE OR REPLACE FUNCTION app_admin_list_trial_requests(
        p_limit int, p_offset int
    )
    RETURNS TABLE(
        id uuid,
        tenant_id uuid,
        email text,
        plan text,
        requested_at timestamptz,
        days int
    )
    LANGUAGE SQL STABLE SECURITY DEFINER
    SET search_path = public
    AS $$
      SELECT
          tr.id,
          tr.tenant_id,
          COALESCE(
              (SELECT u.email FROM users u
                WHERE u.tenant_id = tr.tenant_id AND u.deleted_at IS NULL
                ORDER BY u.created_at ASC
                LIMIT 1),
              CASE
                  WHEN tr.comment LIKE 'reregister_request:%'
                  THEN substr(tr.comment,
                              length('reregister_request:')
                              + length(split_part(tr.comment, ':', 2)) + 2)
                  ELSE NULL
              END
          )                                             AS email,
          CASE
              WHEN tr.comment LIKE 'upgrade_request:%'
              THEN split_part(tr.comment, ':', 2)
              WHEN tr.comment LIKE 'reregister_request:%'
              THEN 'reregister'
              ELSE NULL
          END                                           AS plan,
          tr.requested_at,
          tr.days
      FROM trial_requests tr
      WHERE tr.status = 'pending'
      ORDER BY tr.requested_at ASC, tr.id ASC
      LIMIT p_limit OFFSET p_offset
    $$
    """


def upgrade() -> None:
    op.execute(
        "ALTER TABLE anti_abuse_identifiers "
        "ADD COLUMN IF NOT EXISTS reregister_approved_at timestamptz"
    )

    op.execute("""
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
            -- Хеш принадлежит УДАЛЁННОМУ аккаунту и одобрения на нём ещё нет.
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
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_reregister_request(text, text, text) "
        "TO tendersrb_app"
    )

    op.execute("""
        CREATE OR REPLACE FUNCTION app_admin_decide_reregister(
            p_id uuid, p_approve boolean, p_comment text
        )
        RETURNS TABLE(tenant_id uuid, lang text, email text)
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            v_tenant uuid;
            v_comment text;
        BEGIN
            -- Guarded: только pending-заявка повторной регистрации (повтор → 0 строк).
            SELECT r.tenant_id, r.comment INTO v_tenant, v_comment
              FROM trial_requests r
             WHERE r.id = p_id AND r.status = 'pending'
               AND r.comment LIKE 'reregister_request:%'
             FOR UPDATE;
            IF v_tenant IS NULL THEN
                RETURN;
            END IF;
            UPDATE trial_requests
               SET status = CASE WHEN p_approve THEN 'approved' ELSE 'rejected' END,
                   days = NULL,
                   decided_at = now(),
                   decision_comment = p_comment,
                   comment = 'reregister_request'  -- адрес больше не храним
             WHERE id = p_id;
            IF p_approve THEN
                UPDATE anti_abuse_identifiers a
                   SET reregister_approved_at = now()
                 WHERE a.tenant_id = v_tenant AND a.reregister_approved_at IS NULL;
            END IF;
            tenant_id := v_tenant;
            lang := split_part(v_comment, ':', 2);
            email := substr(v_comment,
                            length('reregister_request:')
                            + length(split_part(v_comment, ':', 2)) + 2);
            RETURN NEXT;
        END
        $$
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_admin_decide_reregister(uuid, boolean, text) "
        "TO tendersrb_app"
    )

    op.execute("""
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
        """)
    op.execute(
        "GRANT EXECUTE ON FUNCTION app_reregister_claim(text) TO tendersrb_app"
    )

    op.execute(_DECIDE_0049)
    op.execute(_LIST_0049)


def downgrade() -> None:
    op.execute(_LIST_0034)
    op.execute(_DECIDE_0035)
    op.execute("DROP FUNCTION IF EXISTS app_reregister_claim(text)")
    op.execute("DROP FUNCTION IF EXISTS app_admin_decide_reregister(uuid, boolean, text)")
    op.execute("DROP FUNCTION IF EXISTS app_reregister_request(text, text, text)")
    op.execute(
        "ALTER TABLE anti_abuse_identifiers DROP COLUMN IF EXISTS reregister_approved_at"
    )
