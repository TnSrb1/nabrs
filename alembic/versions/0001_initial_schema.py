"""initial schema (19 tables, indexes, RLS, notify trigger)

Implements docs/DB_SCHEMA.md v1.1 §2 (tables), §3 (indexes), §4 (RLS),
§5.1 (notify_tender_change trigger). ADR-002/012/018rev/022/033.

Revision ID: 0001
Revises:
Create Date: 2026-05-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TS = postgresql.TIMESTAMP(timezone=True)
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB
ARRAY = postgresql.ARRAY
NOW = sa.text("now()")
GEN_UUID = sa.text("gen_random_uuid()")


# RLS: tables that carry a tenant_id column -> tenant_isolation policy.
_RLS_TENANT_ID_TABLES = (
    "users",
    "profiles",
    "subscriptions",
    "anti_abuse_identifiers",
    "ai_usage",
)
# RLS: listed in §4 as tenant-scoped but WITHOUT a tenant_id column.
# RLS is enabled (default-deny for non-owner roles) but the tenant_isolation
# policy is NOT created — there is no tenant_id column to reference.
# See PR body: needs a join-based policy / decision from owner.
_RLS_NO_TENANT_ID_TABLES = (
    "user_channels",  # tenant_id is absent — isolation via JOIN users
    "profile_revisions",
    "delivery_outbox",
    "delivery_attempts",
    "tender_ai_analyses",
)


def upgrade() -> None:
    # ---- extensions -------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # ---- §2.1 countries ---------------------------------------------------
    op.create_table(
        "countries",
        sa.Column("code", sa.CHAR(2), primary_key=True),
        sa.Column("default_locale", sa.Text(), nullable=False),
        sa.Column("default_currency", sa.CHAR(3), nullable=False),
        sa.Column("default_tz", sa.Text(), nullable=False),
        sa.Column("tax_id_required", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("tax_id_format", sa.Text()),
        sa.Column("kyc_provider_key", sa.Text()),
        sa.Column("regulator_url", sa.Text()),
        sa.Column("support_email", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )

    # ---- §2.2 classification_systems -------------------------------------
    op.create_table(
        "classification_systems",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scope_country_codes", ARRAY(sa.CHAR(2))),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
    )

    # ---- §2.12 classifications (composite PK) ----------------------------
    op.create_table(
        "classifications",
        sa.Column("system_code", sa.Text(),
                  sa.ForeignKey("classification_systems.code"), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("parent_code", sa.Text()),
        sa.Column("label_en", sa.Text()),
        sa.Column("label_sr_cyrl", sa.Text()),
        sa.Column("label_sr_latn", sa.Text()),
        sa.Column("label_ru", sa.Text()),
        sa.PrimaryKeyConstraint("system_code", "code"),
    )

    # ---- §2.13 regions ----------------------------------------------------
    op.create_table(
        "regions",
        sa.Column("code", sa.Text(), primary_key=True),
        sa.Column("country_code", sa.CHAR(2),
                  sa.ForeignKey("countries.code"), nullable=False,
                  server_default=sa.text("'RS'")),
        sa.Column("parent_code", sa.Text()),
        sa.Column("name_sr_cyrl", sa.Text()),
        sa.Column("name_sr_latn", sa.Text()),
        sa.Column("name_en", sa.Text()),
        sa.Column("name_ru", sa.Text()),
        sa.Column("level", sa.Text()),  # free TEXT, no CHECK
    )

    # ---- §2.3 tenants -----------------------------------------------------
    op.create_table(
        "tenants",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("country_code", sa.CHAR(2),
                  sa.ForeignKey("countries.code"), nullable=False),
        sa.Column("tax_id", sa.Text()),
        sa.Column("display_name", sa.Text()),
        sa.Column("tier", sa.Text()),
        sa.Column("trial_until", TS),
        sa.Column("status", sa.Text()),
        sa.Column("locale", sa.Text()),
        sa.Column("tz", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
    )

    # ---- §2.4 users -------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id")),
        sa.Column("email", sa.Text()),
        sa.Column("email_verified_at", TS),
        sa.Column("email_status", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("phone_verified_at", TS),
        sa.Column("role", sa.Text()),
        sa.Column("locale", sa.Text()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.Column("deleted_at", TS),
    )

    # ---- §2.5 user_channels ----------------------------------------------
    op.create_table(
        "user_channels",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id")),
        sa.Column("channel", sa.Text()),
        sa.Column("external_id", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("linked_at", TS),
    )

    # ---- §2.18 subscriptions ---------------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"),
                  nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("payment_processor", sa.Text(), nullable=False,
                  server_default=sa.text("'paddle'")),
        sa.Column("paddle_subscription_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("next_billed_at", TS),
        sa.Column("cancelled_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "tier IN ('free','lite','starter','pro','business','enterprise')",
            name="subscriptions_tier_check"),
        sa.CheckConstraint(
            "payment_processor IN "
            "('paddle','corvuspay','uzcard','kaspi','stripe')",
            name="subscriptions_payment_processor_check"),
        sa.CheckConstraint(
            "status IN ('trial','active','suspended','cancelled')",
            name="subscriptions_status_check"),
    )

    # ---- §2.19 anti_abuse_identifiers ------------------------------------
    op.create_table(
        "anti_abuse_identifiers",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("tenant_id", UUID,
                  sa.ForeignKey("tenants.id", ondelete="CASCADE")),
        sa.Column("identifier_type", sa.Text(), nullable=False),
        sa.Column("value_hash", sa.CHAR(64), nullable=False),
        sa.Column("value_last4", sa.Text()),
        sa.Column("risk_score_at_signup", sa.Integer()),
        sa.Column("verification_method", sa.Text()),
        sa.Column("verified_at", TS),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("region", sa.CHAR(2), sa.ForeignKey("countries.code")),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.CheckConstraint(
            "identifier_type IN ("
            "'serbian_pib_doo','serbian_jmbg','card_fingerprint',"
            "'email_domain','device_fingerprint','manual_b2b_attestation')",
            name="anti_abuse_identifiers_identifier_type_check"),
    )

    # ---- §2.6 profiles ----------------------------------------------------
    op.create_table(
        "profiles",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id")),
        sa.Column("country_code", sa.CHAR(2),
                  sa.ForeignKey("countries.code"), nullable=False,
                  server_default=sa.text("'RS'")),
        sa.Column("target_language", sa.Text(), nullable=False,
                  server_default=sa.text("'ru'")),
        sa.Column("name", sa.Text()),
        sa.Column("is_active", sa.Boolean()),
        sa.Column("enabled_source_keys", ARRAY(sa.Text())),
        sa.Column("keywords_include", JSONB),
        sa.Column("keywords_exclude", ARRAY(sa.Text())),
        sa.Column("classifications", JSONB),
        sa.Column("regions", ARRAY(sa.Text())),
        sa.Column("min_amount_eur", sa.Numeric()),
        sa.Column("max_amount_eur", sa.Numeric()),
        sa.Column("include_no_amount", sa.Boolean()),
        sa.Column("procedure_types", ARRAY(sa.Text())),
        sa.Column("languages", ARRAY(sa.Text())),
        sa.Column("min_days_until_deadline", sa.Integer()),
        sa.Column("ai_analysis_level", sa.Text()),
        sa.Column("ai_focus_questions", JSONB),
        sa.Column("ai_extra_context", sa.Text()),
        sa.Column("ai_extra_context_embedding", Vector(1536)),
        sa.Column("delivery_channels", ARRAY(sa.Text())),
        sa.Column("delivery_schedule", sa.Text()),
        sa.Column("digest_cron", sa.Text()),
        sa.Column("quiet_hours_start", sa.Time()),
        sa.Column("quiet_hours_end", sa.Time()),
        sa.Column("version", sa.Integer()),
        sa.Column("created_by_user_id", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.Column("deleted_at", TS),
    )

    # ---- §2.7 profile_revisions ------------------------------------------
    op.create_table(
        "profile_revisions",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("profile_id", UUID, sa.ForeignKey("profiles.id")),
        sa.Column("version", sa.Integer()),
        sa.Column("snapshot", JSONB),
        sa.Column("changed_by_user_id", UUID),
        sa.Column("changed_at", TS, nullable=False, server_default=NOW),
    )

    # ---- §2.8 sources -----------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("source_key", sa.Text(), primary_key=True),
        sa.Column("country_codes", ARRAY(sa.CHAR(2)),
                  server_default=sa.text("ARRAY['RS']::CHAR(2)[]")),
        sa.Column("display_name", sa.Text()),
        sa.Column("base_url", sa.Text()),
        sa.Column("enabled", sa.Boolean()),
        sa.Column("polling_interval_sec", sa.Integer()),
        sa.Column("last_run_at", TS),
        sa.Column("last_status", sa.Text()),
        sa.Column("error_count", sa.Integer()),
        sa.Column("available_for_tiers", ARRAY(sa.Text())),
    )

    # ---- §2.9 tenders_raw -------------------------------------------------
    op.create_table(
        "tenders_raw",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("country_code", sa.CHAR(2)),  # no FK per §2.9 (INT tenders)
        sa.Column("source_key", sa.Text()),
        sa.Column("source_uid", sa.Text()),
        sa.Column("raw_payload", JSONB),
        sa.Column("raw_hash", sa.Text()),
        sa.Column("fetched_at", TS),
    )

    # ---- §2.11 canonical_tenders (circular FK added after normalized) ----
    op.create_table(
        "canonical_tenders",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("cluster_key", sa.Text()),
        sa.Column("primary_tender_id", UUID),  # FK added below
    )

    # ---- §2.10 tenders_normalized ----------------------------------------
    op.create_table(
        "tenders_normalized",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("country_code", sa.CHAR(2)),  # no FK per §2.10 (INT tenders)
        sa.Column("tenders_raw_id", UUID, sa.ForeignKey("tenders_raw.id")),
        sa.Column("canonical_tender_id", UUID,
                  sa.ForeignKey("canonical_tenders.id")),
        sa.Column("cluster_key", sa.CHAR(40)),
        sa.Column("source_key", sa.Text()),
        sa.Column("source_uid", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("title_normalized", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("description_normalized", sa.Text()),
        sa.Column("description_embedding", Vector(1536)),
        sa.Column("customer_name", sa.Text()),
        sa.Column("customer_tax_id", sa.Text()),
        sa.Column("customer_region_code", sa.Text()),
        sa.Column("amount_original", sa.Numeric()),
        sa.Column("currency_original", sa.CHAR(3)),
        sa.Column("amount_eur", sa.Numeric()),
        sa.Column("is_amount_estimated", sa.Boolean()),
        sa.Column("published_at", TS),
        sa.Column("deadline_at", TS),
        sa.Column("opening_at", TS),
        sa.Column("classifications", JSONB),
        sa.Column("procedure_type", sa.Text()),
        sa.Column("language", sa.Text()),
        sa.Column("regions", ARRAY(sa.Text())),
        sa.Column("status", sa.Text()),
        sa.Column("documents", JSONB),
        sa.Column("first_seen_at", TS),
        sa.Column("last_seen_at", TS),
        sa.Column("last_changed_at", TS),
    )

    # circular FK: canonical_tenders.primary_tender_id -> tenders_normalized.id
    op.create_foreign_key(
        "canonical_tenders_primary_tender_id_fkey",
        "canonical_tenders", "tenders_normalized",
        ["primary_tender_id"], ["id"],
    )

    # ---- §2.16 tender_ai_analyses ----------------------------------------
    op.create_table(
        "tender_ai_analyses",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("tender_id", UUID,
                  sa.ForeignKey("tenders_normalized.id")),
        sa.Column("profile_id", UUID, sa.ForeignKey("profiles.id")),
        sa.Column("level", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("output", JSONB),
        sa.Column("tokens_input", sa.Integer()),
        sa.Column("tokens_output", sa.Integer()),
        sa.Column("tokens_cached", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.UniqueConstraint("tender_id", "profile_id", "level",
                            name="tender_ai_analyses_tender_profile_level_uniq"),
    )

    # ---- §2.14 delivery_outbox (BIGSERIAL) -------------------------------
    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("profile_id", UUID, sa.ForeignKey("profiles.id")),
        sa.Column("tender_id", UUID,
                  sa.ForeignKey("tenders_normalized.id")),
        sa.Column("channel", sa.Text()),
        sa.Column("status", sa.Text()),
        sa.Column("requires_ai", sa.Boolean()),
        sa.Column("ai_analysis_id", UUID,
                  sa.ForeignKey("tender_ai_analyses.id")),
        sa.Column("match_kind", sa.Text()),
        sa.Column("match_score", sa.Numeric()),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("sent_at", TS),
        sa.Column("failed_at", TS),
    )

    # ---- §2.15 delivery_attempts -----------------------------------------
    op.create_table(
        "delivery_attempts",
        sa.Column("id", UUID, primary_key=True, server_default=GEN_UUID),
        sa.Column("outbox_id", sa.BigInteger(),
                  sa.ForeignKey("delivery_outbox.id")),
        sa.Column("attempted_at", TS),
        sa.Column("response_code", sa.Integer()),
        sa.Column("error_text", sa.Text()),
    )

    # ---- §2.17 ai_usage (composite PK; see PR note) ----------------------
    op.create_table(
        "ai_usage",
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id")),
        sa.Column("period", sa.CHAR(7)),  # YYYY-MM
        sa.Column("level", sa.Text()),
        sa.Column("count", sa.Integer()),
        sa.Column("tokens_total", sa.BigInteger()),
        sa.Column("cost_usd_total", sa.Numeric()),
        sa.PrimaryKeyConstraint("tenant_id", "period", "level"),
    )

    # ---- §3 indexes (verbatim from DB_SCHEMA §3) -------------------------
    # asyncpg does not support several commands in one execute — each index
    # is created by a separate call.
    # classifications
    op.execute("CREATE INDEX ON classifications (parent_code)")
    # tenants
    op.execute(
        "CREATE UNIQUE INDEX tenants_country_tax_id_uniq"
        " ON tenants (country_code, tax_id) WHERE tax_id IS NOT NULL"
    )
    op.execute("CREATE INDEX ON tenants (country_code)")
    # profiles
    op.execute("CREATE INDEX ON profiles (tenant_id, is_active)")
    op.execute("CREATE INDEX ON profiles (country_code, is_active)")
    op.execute("CREATE INDEX ON profiles USING GIN (enabled_source_keys)")
    op.execute("CREATE INDEX ON profiles USING GIN (classifications jsonb_path_ops)")
    op.execute("CREATE INDEX ON profiles USING GIN (regions)")
    # tenders_raw
    op.execute("CREATE UNIQUE INDEX ON tenders_raw (source_key, source_uid)")
    op.execute("CREATE INDEX ON tenders_raw (country_code)")
    # tenders_normalized
    op.execute("CREATE UNIQUE INDEX ON tenders_normalized (source_key, source_uid)")
    op.execute("CREATE INDEX ON tenders_normalized (country_code)")
    op.execute("CREATE INDEX ON tenders_normalized (canonical_tender_id)")
    op.execute(
        "CREATE INDEX ON tenders_normalized (cluster_key)"
        " WHERE cluster_key IS NOT NULL"
    )
    op.execute("CREATE INDEX ON tenders_normalized (published_at DESC)")
    op.execute("CREATE INDEX ON tenders_normalized (deadline_at)")
    op.execute("CREATE INDEX ON tenders_normalized (customer_tax_id)")
    op.execute(
        "CREATE INDEX ON tenders_normalized"
        " USING GIN (classifications jsonb_path_ops)"
    )
    op.execute("CREATE INDEX ON tenders_normalized USING GIN (regions)")
    op.execute(
        "CREATE INDEX ON tenders_normalized USING GIN ("
        "to_tsvector('simple', title_normalized || ' ' || description_normalized))"
    )
    op.execute(
        "CREATE INDEX ON tenders_normalized"
        " USING ivfflat (description_embedding vector_cosine_ops)"
    )
    # sources
    op.execute("CREATE INDEX ON sources USING GIN (country_codes)")
    # regions
    op.execute("CREATE INDEX ON regions (country_code)")
    # delivery_outbox
    op.execute(
        "CREATE UNIQUE INDEX ON delivery_outbox (profile_id, tender_id, channel)"
    )
    op.execute(
        "CREATE INDEX ON delivery_outbox (status, created_at)"
        " WHERE status IN ('pending','ai_pending','ai_done')"
    )
    # subscriptions
    op.execute("CREATE INDEX ON subscriptions (tenant_id, status)")
    op.execute(
        "CREATE INDEX ON subscriptions (paddle_subscription_id)"
        " WHERE paddle_subscription_id IS NOT NULL"
    )
    # anti_abuse_identifiers
    op.execute(
        "CREATE UNIQUE INDEX anti_abuse_value_hash_active_uniq"
        " ON anti_abuse_identifiers (value_hash)"
    )
    op.execute("CREATE INDEX ON anti_abuse_identifiers (tenant_id)")
    op.execute(
        "CREATE INDEX ON anti_abuse_identifiers (identifier_type, expires_at)"
    )

    # ---- §4 Row-Level Security -------------------------------------------
    # tenants: policy keyed on id (not tenant_id)
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON tenants "
        "USING (id = current_setting('app.current_tenant_id')::uuid)"
    )
    # tables with a tenant_id column
    for tbl in _RLS_TENANT_ID_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {tbl} "
            "USING (tenant_id = current_setting('app.current_tenant_id')::uuid)"
        )
    # tables listed in §4 without a tenant_id column: enable RLS (default-deny
    # for non-owner roles), no policy created. See PR body.
    for tbl in _RLS_NO_TENANT_ID_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")

    # ---- §5.1 notify_tender_change trigger -------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION notify_tender_change() RETURNS trigger AS $$
        BEGIN
            PERFORM pg_notify('tender_changed',
                json_build_object(
                    'tender_id', NEW.id,
                    'country_code', NEW.country_code,
                    'change_kind', TG_OP,
                    'canonical_id', NEW.canonical_tender_id
                )::text
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER tenders_normalized_notify
        AFTER INSERT OR UPDATE ON tenders_normalized
        FOR EACH ROW EXECUTE FUNCTION notify_tender_change();
    """)


def downgrade() -> None:
    # trigger + function
    op.execute(
        "DROP TRIGGER IF EXISTS tenders_normalized_notify "
        "ON tenders_normalized"
    )
    op.execute("DROP FUNCTION IF EXISTS notify_tender_change()")

    # tables in reverse dependency order (indexes + RLS policies drop with them)
    for tbl in (
        "ai_usage",
        "delivery_attempts",
        "delivery_outbox",
        "tender_ai_analyses",
        "tenders_normalized",
        "canonical_tenders",
        "tenders_raw",
        "sources",
        "profile_revisions",
        "profiles",
        "anti_abuse_identifiers",
        "subscriptions",
        "user_channels",
        "users",
        "tenants",
        "regions",
        "classifications",
        "classification_systems",
        "countries",
    ):
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

    # extensions are intentionally left in place (may be shared).
