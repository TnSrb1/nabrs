# DB_SCHEMA.md — The database schema of Tender Bot Serbia

> **Note:** schema details cover migrations up to 0044; later migrations: see `alembic/versions/`.

**Version:** 1.9
**Date:** 2026-09-26 (docs-sync — **covered up to head 0044**. Added migrations 0034–0044: §2.34 `ai_spend_daily` (0040), §2.35 `ai_spend_alert_log` (0040), §2.36 `template_library.i18n` (0036, only the column — the table itself is not described, the 0016 gap is outside the scope of this edit); new columns in existing tables — `user_channels.locale`+`status='disabled'` (§2.5, 0037/0038), `profiles.match_all`+`ai_analysis_level` auto-analysis off (§2.6, 0043/0044), `trial_requests.decision_comment` (§2.26, 0035); a new §4.3 — SECURITY DEFINER functions 0034–0044 (subscription requests, disabling/enabling a subscriber, access extension, deterministic resolvers replacing `LIMIT 1` without `ORDER BY`). Earlier v1.8 2026-07-18 (docs-schema-hygiene — covered up to head 0033. Added tables/objects of migrations 0019–0033 (v1.7 stopped at 0018) + a retroactive addition of the missed `trial_requests` §2.26 (0012): §2.26 `trial_requests` (0012), §2.27 `award_watch_*` (0019), §2.28 `referrals` (0020), §2.29 billing schema `billing_subscriptions`/`billing_webhook_events` (0028), §2.30 `tender_folders`/`folder_items`/`tender_notes` (0029), §2.31 `admin_audit_log` (0030, global, REVOKE ALL from the app role), §2.32 `channel_dead_notice_log` (0031), §2.33 `billing_notification_outbox` (0033). A new §4.2 — **the perimeter of the app role `tendersrb_app` (VG-01, 0021)** + FORCE RLS + a catalog of SECURITY DEFINER functions `app_resolve_*`/`app_admin_*`/`app_billing_*`/`app_referral_*` (0021–0033). Changes to existing tables: `tenants.access_until` (0012), `tenants.tier` → CHECK `ck_tenants_tier`/DEFAULT `lite`/NOT NULL (0027). Earlier v1.7 Chat 68 VG-05 STAGE 2 — added `tender_awards` §2.25 (migration 0018): award decisions of F03 eNabavke, DocumentTypeId 11+204, 5 indexes, no RLS. DB head → 0018. Earlier v1.6 Chat 58 Track C — added `subscription_reminder_log` §2.24 (migration 0017): a log of subscription-expiry reminders, UNIQUE `(tenant_id, threshold_days, access_until_snapshot)` — one threshold per expiry date, on renewal a new snapshot gives a new UNIQUE key, a direct RLS policy `tenant_isolation`. Earlier v1.5 — added `summary_log` §2.23 (migration 0010): a log of sent daily summaries for the idempotency of the daily-summary worker (a port of v6 `daily_summary`), UNIQUE `(tenant_id, summary_date)` — “one summary per day per tenant”, a direct RLS policy `tenant_isolation`. Earlier v1.4 Chat 40 — `reminder_log` §2.22 (migration 0009): a log of sent deadline reminders for the idempotency of the reminder worker (a port of v6 `reminders_loop`), UNIQUE `(tenant_id, tender_id, threshold_days)` — “one threshold once”, a direct RLS policy `tenant_isolation`. v1.3 Chat 38 — `tender_user_status` §2.21 (migration 0008): the user's tender status funnel at tenant level, 7 values `new/watching/active/submitted/won/lost/skipped` (a port of v6), default `new`, a direct RLS policy `tenant_isolation`. v1.2 Chat 27 — T-fix: on-the-fly archiving, `auth_tokens` §2.20, a global UNIQUE anti-abuse, `user_channels` in the RLS list without `tenant_id`)
**Purpose:** a detailed operational reference for the PostgreSQL schema — DDL, indexes, RLS policies, triggers, template queries. It does not duplicate the conceptual data model from the architecture notes (not published) but unfolds it at the implementation level.

For the conceptual model and the rationale of decisions, see the architecture notes (not published) v3.0 §1.5 (multi-country foundations), §5 (data model), §6 (subscriber profile), §7 (routing engine), §19 (country pack roadmap), and also `docs/DECISIONS.md` ADR-002 (RLS), ADR-012 (archive), ADR-018 (universal anti_abuse_identifier, revised in Chat 14), ADR-022 (two-tier model + canonical), ADR-023 (profile + routing), ADR-024 (AI pipeline), ADR-025 (outbox), **ADR-033** (multi-country foundations).

Abbreviations — `docs/GLOSSARY.md`.

---

## 1. General implementation principles

- **PostgreSQL 16** as the only database (ADR-002), with the extensions `pgvector` (embeddings in `tenders_normalized.description_embedding` and `profiles.ai_extra_context_embedding`), `pg_trgm` (deduplication and fuzzy string comparison) and `unaccent` (normalizing Serbian Cyrillic/Latin for FTS).
- **Multi-tenant isolation through Row-Level Security** (ADR-002), not by splitting databases or schemas. The tenant is set via `SET LOCAL app.current_tenant_id = '<uuid>'` when the user enters the cabinet.
- **Multi-country foundations** (ADR-033). `country_code CHAR(2)` runs through the key tenant and tender tables; `countries` is the registry of supported countries; `classification_systems` is the registry of procurement classifiers. Phase 0 supports only RS.
- **Indexes for the routing-engine workload:** GIN on arrays (`enabled_source_keys`, `cpv_include`, `regions`, `classifications`), GIN on the FTS vector, `ivfflat` on pgvector for cosine similarity. Additional B-tree indexes on `country_code` for multi-country filtering.
- **Idempotency at the key level:** `UNIQUE (source_key, source_uid)` on raw/normalized; `UNIQUE (profile_id, tender_id, channel)` on `delivery_outbox`; `UNIQUE (tender_id, profile_id, level)` on `tender_ai_analyses` (the AI analysis cache); `UNIQUE (country_code, tax_id) WHERE tax_id IS NOT NULL` on `tenants`; `UNIQUE (tenant_id, tender_id)` on `tender_user_status` (one status per tenant↔tender pair); `UNIQUE (tenant_id, tender_id, threshold_days)` on `reminder_log` (one threshold reminder per tenant↔tender pair — idempotency of the reminder worker); `UNIQUE (tenant_id, summary_date)` on `summary_log` (one summary per day per tenant — idempotency of the daily-summary worker); `UNIQUE (tenant_id, threshold_days, access_until_snapshot)` on `subscription_reminder_log` (one threshold per tenant subscription expiry date — on renewal a new snapshot gives a new UNIQUE key).
- **JSONB for flexible fields** of the profile (`keywords_include`), AI output (`tender_ai_analyses.output`), raw source data (`tenders_raw.raw_payload`), profile snapshots (`profile_revisions.snapshot`).

---

## 2. Tables

### 2.1. countries (new, ADR-033)

The registry of supported countries. In phase 0 — only RS. Each record sets country-dependent defaults for tenants and for the behavior of the KYCProvider.

- `code CHAR(2) PRIMARY KEY` — ISO 3166-1 alpha-2 (for example, `RS`, `UZ`, `KZ`)
- `name TEXT NOT NULL` — a human-readable country name for `{{country_name}}` in the AI prompt (ADR-024/§14.3); one value in English, multilingual localisation later (migration 0005, Q-28)
- `default_locale TEXT NOT NULL` — BCP-47 (`sr-Latn`, `ru`, `uz-Latn`)
- `default_currency CHAR(3) NOT NULL` — ISO 4217 (`EUR`, `RSD`, `UZS`, `KZT`)
- `default_tz TEXT NOT NULL` — IANA tz database (`Europe/Belgrade`)
- `tax_id_required BOOLEAN NOT NULL DEFAULT TRUE`
- `tax_id_format TEXT` — a regex for validation, for example `^\d{9}$` for the RS PIB
- `kyc_provider_key TEXT` — `'apr_rs'`, NULL = fallback to email + Kickbox + Paddle SetupIntent
- `regulator_url TEXT` — a link to the national data protection regulator
- `support_email TEXT` — a per-country support inbox (at MVP — the shared `support@nab.rs`)
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**Seed for RS:**

```sql
INSERT INTO countries (code, name, default_locale, default_currency, default_tz,
                       tax_id_required, tax_id_format, kyc_provider_key,
                       regulator_url, support_email)
VALUES ('RS', 'Serbia', 'sr-Latn', 'EUR', 'Europe/Belgrade',
        TRUE, '^\d{9}$', 'apr_rs',
        'https://www.poverenik.rs/', 'support@nab.rs');
```

### 2.2. classification_systems (new, ADR-033)

The registry of procurement classifiers. `cpv_codes` used to hardcode the EU CPV; now the EU CPV is one system alongside UNSPSC and potential `uz_classifier`, `kz_oked`, etc.

- `code TEXT PRIMARY KEY` — `'eu_cpv'`, `'unspsc'`, `'kz_oked'`, etc.
- `name TEXT NOT NULL`
- `scope_country_codes CHAR(2)[]` — NULL = global (the EU CPV is harmonised across RS + EU); otherwise an array of countries
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**Seed:**

```sql
INSERT INTO classification_systems (code, name, scope_country_codes) VALUES
  ('eu_cpv',  'EU Common Procurement Vocabulary',         NULL),
  ('unspsc',  'UN Standard Products and Services Code',   NULL);
```

### 2.3. tenants

The subscriber’s legal entity (a company/DOO or an individual/preduzetnik for tax_id_required=false countries). After ADR-033: `pib` is renamed to `tax_id`, `country_code` is added. UNIQUE became composite.

- `id UUID PRIMARY KEY`
- `country_code CHAR(2) NOT NULL REFERENCES countries(code)` — always `'RS'` at MVP
- `tax_id TEXT` — the national tax identifier; for RS — the PIB (9 digits); nullable for countries with `tax_id_required=false` (phase 4+)
- `display_name TEXT`
- `tier TEXT` — `lite` / `starter` / `pro` / `business` / `enterprise` (Lite added in ADR-018 revised)
- `trial_until TIMESTAMPTZ`
- `status TEXT` — `trial` / `active` / `suspended` / `cancelled`
- `access_until TIMESTAMPTZ` (migration 0012) — the tenant’s access expiry; **NULL = unlimited** (demo/test). Expired (`access_until <= now()`) → matching/delivery stops (`STAGE_A_SQL` in `src/workers/matching`). Written by: the referral bonus (`app_referral_grant_bonus` 0023), the admin (`app_admin_grant_access`/`revoke_access` 0025), subscription reminders (§2.24)
- `locale TEXT` — `sr-Cyrl` / `sr-Latn` / `ru` / `en` (UI language)
- `tz TEXT` — Europe/Belgrade by default; taken from `countries.default_tz`, but may be overridden
- `created_at`, `updated_at`

> **`tier` canonicalised by migration 0027 (audit R8):** `tier` got `DEFAULT 'lite'`, `NOT NULL` and the CHECK `ck_tenants_tier CHECK (tier IN ('lite','starter','pro','business','enterprise'))` — the internal tier canon (SSOT `src/domain/tiers.py` `TIER_ORDER`). Backfill BEFORE the constraint: `'free'`/`NULL`/any non-canon → `'lite'`. ‼️ `'free'` is a storefront alias, NOT an internal tier (it cannot be in the DB after 0027). The axis divergence storefront/gating/model choice — see the project rules “Do NOT mix the plan axes”.

**UNIQUE (composite):**

```sql
CREATE UNIQUE INDEX tenants_country_tax_id_uniq
  ON tenants (country_code, tax_id)
  WHERE tax_id IS NOT NULL;
```

This allows two tenants with the same tax_id from different countries (a theoretical situation) and any number of tenants with tax_id IS NULL (for individuals/preduzetnik of phase 4+).

### 2.4. users

Users within a tenant. At MVP all have the admin role (RBAC — phase 2). Unchanged in ADR-033.

- `id UUID`
- `tenant_id UUID REFERENCES tenants(id)`
- `email`, `email_verified_at`, `email_status` (active/bounced)
- `phone`, `phone_verified_at`
- `role TEXT` — admin / member / viewer (RBAC, at MVP all are admin)
- `locale`
- `created_at`, `updated_at`, `deleted_at` (soft delete)

### 2.5. user_channels

A user's bindings to messenger channels. One user may have several channels. Unchanged in ADR-033. A Telegram binding is created by the bot via the deep link `/start <token>` (T3, see §2.20 `auth_tokens.purpose='channel_bind'`).

- `id`, `user_id`, `channel` (telegram/viber), `external_id` (Telegram chat_id / Viber user_id), `status` (active/blocked/disabled), `linked_at`
- `locale TEXT` (migration **0038**) — the language of THIS delivery channel; `NULL` = inherit `users.locale` (not “language unknown”). CHECK `ck_user_channels_locale`: `NULL` or one of `'sr','ru','en'` (the canon `src.i18n_email.SUPPORTED`). Changed by an explicit choice in the bot (`/language`) — after the choice it lives independently of `users.locale`.
- `status = 'disabled'` (migration **0037**) — the channel is disabled by the operator's `app_admin_disable_subscriber` (§4.3); `app_admin_enable_subscriber` returns to `active` only those channels it disabled itself.

> There is no `tenant_id` column — isolation goes through a JOIN to `users` (see §4.1).
> Self-deletion of an account (LEG-06, no migration): `status='disabled'` + `external_id=NULL`; for `users` — `deleted_at`, `email='deleted+<uid>@deleted.invalid'`, `phone=NULL`, `email_status='deleted'`; for the last user the tenant gets `status='deleted'`, `access_until=now()`.

### 2.6. profiles

The subscriber’s search profiles: filters, AI settings, delivery. Per-tier count limits — see below. After ADR-033 the `country_code` field was added (for routing stage A filtering).

- `id UUID`
- `tenant_id UUID`
- `country_code CHAR(2) NOT NULL DEFAULT 'RS' REFERENCES countries(code)` — which country the profile subscribes to; NULL is allowed only for subscriptions to international tenders (EBRD/UNDP) — in that case stage A skips the country filter
- `target_language TEXT NOT NULL DEFAULT 'ru'` — the AI analysis language (`ru` / `sr-Latn` / `en`); parametrises the AI system prompt (ADR-024 addendum)
- `name TEXT` — a human-readable profile name
- `is_active BOOLEAN`
- `debug_match BOOLEAN NOT NULL DEFAULT false` — debug mode (migration 0013): when `true` the tender card shows a “🐞 Match debug” block (matched keywords + CPV codes) and a “⚙️ Configure this template” button (a deep link into the cabinet). The match reason is computed on the fly and not stored in the DB. No plan gate — available to everyone.
- **Filtering:**
  - `enabled_source_keys TEXT[]` — the subset of allowed platforms
  - `keywords_include JSONB` — an array of groups: `[{"all_of":["video surveillance","installation"]}, {"all_of":["ACS"]}]`. A tender matches if at least one group matches entirely.
  - `keywords_exclude TEXT[]` — stop words: one match → exclude
  - `classifications JSONB` — include/exclude arrays by classification system + code, in the format `[{"system_code":"eu_cpv","code":"32*"}, ...]` (replaces the old `cpv_include`/`cpv_exclude`); prefix matching with `*` is preserved
  - `regions TEXT[]` — codes of districts / municipalities (see `tenders_normalized.regions`)
  - `min_amount_eur NUMERIC` / `max_amount_eur NUMERIC` / `include_no_amount BOOLEAN`
  - `procedure_types TEXT[]` — null = all
  - `languages TEXT[]` — null = all
  - `min_days_until_deadline INT` — 0 = everything, 3 = do not send “burning” ones
- `match_all BOOLEAN NOT NULL DEFAULT false` (migration **0043**) — an explicit “the whole flow without a topic” mode (it used to be a side effect of an empty `keywords_include`/`classifications`, indistinguishable from “not configured”). The backfill on rollout set `true` for all profiles without a topic (keywords and classifications) so as not to change the actual behavior in production.
- **AI settings:**
  - `ai_analysis_level TEXT` — none / quick / standard / deep. ‼️ Since migration **0044** (owner 15.08.2026) the profile form sends no value other than `none` and `parse_profile_form` hard-writes `'none'` — automatic AI analysis of the WHOLE flow is switched off by product decision (the median tender is ≈22k input tokens, ~$110/month per profile in the “All tenders” mode); AI on a button (verdict/checklist/draft/deep analysis) is not affected. The field stayed in the schema for a possible deliberate return with a limit.
  - `ai_focus_questions JSONB` — an array of strings, custom questions for the analysis
  - `ai_extra_context TEXT` — “we specialize in…”
  - `ai_extra_context_embedding VECTOR(1536)` — pgvector, for soft-match
- **Delivery:**
  - `delivery_channels TEXT[]` — a subset of ['telegram','viber','email','web']
  - `delivery_schedule TEXT` — immediate / digest_morning / digest_evening / digest_custom
  - `digest_cron TEXT` — for custom
  - `quiet_hours_start TIME`, `quiet_hours_end TIME` (NULL = none)
- **Audit:**
  - `version INT`, `created_by_user_id`, `created_at`, `updated_at`, `deleted_at`

**Per-plan limits (enforced on save):**

| Plan | Max profiles |
|---|---|
| Lite | 1 |
| Starter | 1 |
| Pro | 3 |
| Business | 10 |
| Enterprise | unlimited |

### 2.7. profile_revisions

A full snapshot of the profile on every change. Why — diagnosing complaints (“it used to be sent — it stopped”), A/B tests, audit.

- `id`, `profile_id`, `version`, `snapshot JSONB`, `changed_by_user_id`, `changed_at`

### 2.8. sources

The runtime config of tender platforms. Managed through the admin and ENV variables on first deploy; the parser logic itself is in code (`src/parsers/<source>/` — a source adapter, not published), see ADR-021. After ADR-033 the `country_codes` field was added (an array, because one source may cover several countries — EBRD/UNDP/tendersinfo).

- `source_key TEXT PRIMARY KEY` — corresponds to `SourceAdapter.source_key`
- `country_codes CHAR(2)[] DEFAULT ARRAY['RS']::CHAR(2)[]` — the countries whose tenders the source covers; for INT sources (EBRD, UNDP) — an array `['RS','HR','HU',...]` or NULL for “international”
- `display_name`, `base_url`
- `enabled BOOLEAN`
- `polling_interval_sec INT`
- `last_run_at`, `last_status`, `error_count`
- `available_for_tiers TEXT[]` — if the source is paid (for example, tendersinfo.com) — a tier gate

### 2.9. tenders_raw

An exact copy of the source’s response. Stored forever (ADR-012); never edited except by UPSERT on update. After ADR-033 a nullable `country_code` field was added (filled by the parser; may be NULL for INT tenders).

- `id UUID`
- `country_code CHAR(2)` — the tender’s country; NULL for INT tenders without an unambiguous country
- `source_key`, `source_uid` — UNIQUE together
- `raw_payload JSONB` — the raw body from the source
- `raw_hash TEXT` — for detecting changes
- `fetched_at TIMESTAMPTZ`

### 2.10. tenders_normalized

A per-source canonicalised record. The main working table of routing and AI. **Archived status (ADR-012) is not stored as a column** — it is computed on the fly in queries (`deadline_at < now() - interval '30 days'`); the index is an ordinary B-tree on `deadline_at`. After ADR-033: `country_code` (nullable, as in raw), `cluster_key` now includes country_code as the first component, the `cpv_codes` field is replaced with the JSONB `classifications`.

- `id UUID PRIMARY KEY`
- `country_code CHAR(2)` — the tender’s country; nullable for INT
- `tenders_raw_id` — FK to the raw record
- `canonical_tender_id UUID` — FK to `canonical_tenders` (see below)
- `cluster_key CHAR(40)` — SHA-1 hex, the formula is in §7 (includes country_code)
- `source_key`
- `source_uid`
- `source_url`
- **Content:**
  - `title TEXT`, `title_normalized TEXT` (lowercase + transliteration for FTS)
  - `description TEXT`, `description_normalized TEXT`
  - `description_embedding VECTOR(1536)` — pgvector
- **Buyer:**
  - `customer_name TEXT`, `customer_tax_id TEXT` (formerly `customer_pib`), `customer_region_code TEXT`
- **Finance:**
  - `amount_original NUMERIC`, `currency_original CHAR(3)`
  - `amount_eur NUMERIC` (converted at the rate on `published_at`)
  - `is_amount_estimated BOOLEAN`
- **Deadlines:**
  - `published_at TIMESTAMPTZ`, `deadline_at TIMESTAMPTZ`, `opening_at TIMESTAMPTZ`
- **Categorisation:**
  - `classifications JSONB` — an array `[{"system_code":"eu_cpv","code":"32200000"}, ...]`; replaces the old `cpv_codes TEXT[]`
  - `procedure_type TEXT` (a normalized enum)
  - `language TEXT` (sr-Cyrl / sr-Latn / en / ru / uz / kk — extended by country)
  - `regions TEXT[]` — an array of codes (ISO 3166-2:RS / 2:UZ / 2:KZ)
- **Status:**
  - `status TEXT` — active / awarded / cancelled / closed (the status of the tender itself from the source; for the user’s funnel at tenant level see §2.21 `tender_user_status`)
  - **archived status** (ADR-012) — NOT a column: computed on the fly `deadline_at < now() - interval '30 days'`. The `is_archived` column is not stored: `NOW()` is not IMMUTABLE → `GENERATED ALWAYS AS (...) STORED` is rejected by PostgreSQL (T-fix, Chat 27). The index is an ordinary B-tree on `deadline_at`.
- **Documents:**
  - `documents JSONB` — `[{name, url, type, size}]` (metadata, not content)
- **Audit:**
  - `first_seen_at`, `last_seen_at`, `last_changed_at`

### 2.11. canonical_tenders

A group of duplicates from different sources. Several `tenders_normalized` may point to one `canonical_tender_id` — this is cross-source deduplication (ADR-022, see §7). The subscriber is notified once, with the note “also available on N sources”.

- `id UUID PRIMARY KEY`
- `cluster_key TEXT` — a deterministic clustering key (see §7); includes `country_code` after ADR-033
- `primary_tender_id` — the main representation (from the primary source)

### 2.12. classifications (was cpv_codes)

A directory of procurement classifiers with localised labels. After ADR-033: `cpv_codes` renamed to `classifications`, `system_code` added (FK to `classification_systems`), composite PK `(system_code, code)`. In phase 0 — a seed only for the EU CPV.

- `system_code TEXT NOT NULL REFERENCES classification_systems(code)`
- `code TEXT NOT NULL` — 8 digits for the EU CPV, variable length for UNSPSC and others
- `parent_code TEXT`
- `label_en`, `label_sr_cyrl`, `label_sr_latn`, `label_ru`
- **PRIMARY KEY (system_code, code)**

### 2.13. regions

A directory of ISO 3166-2 subdivision codes. At MVP — RS (8 districts + Beograd + Vojvodina + KiM, ~150 opštine). After ADR-033 the `country_code` field was added for multi-country filtering.

- `code TEXT PRIMARY KEY` (for example `RS-00`, `RS-00-BG7`, in future `UZ-TK`)
- `country_code CHAR(2) NOT NULL REFERENCES countries(code) DEFAULT 'RS'`
- `parent_code TEXT`
- `name_sr_cyrl`, `name_sr_latn`, `name_en`, `name_ru` (for RS); per-country localisation is a separate seed when a country is added
- `level` — okrug / opstina (for RS); per-country administrative units

### 2.14. delivery_outbox

The delivery queue (outbox pattern, ADR-025). UNIQUE on (profile_id, tender_id, channel) — idempotency of routing. A pivot between routing and delivery, a status machine. Unchanged in ADR-033.

- `id BIGSERIAL`
- `profile_id`, `tender_id` (FK to normalized)
- `channel TEXT` (telegram/viber/email/web)
- `status TEXT` — pending / ai_pending / ai_done / sent / failed / cancelled
- `requires_ai BOOLEAN`
- `ai_analysis_id` — FK to `tender_ai_analyses` (NULL until the AI finishes)
- `match_kind TEXT` — `new` / `update` (if the tender was updated)
- `match_score NUMERIC` — for soft-match via embedding
- `created_at`, `sent_at`, `failed_at`
- `UNIQUE (profile_id, tender_id, channel)`

### 2.15. delivery_attempts

A log of every delivery attempt for diagnostics.

- `id`, `outbox_id`, `attempted_at`, `response_code`, `error_text`

### 2.16. tender_ai_analyses

AI analysis results with a cache (ADR-024). UNIQUE on (tender_id, profile_id, level) — reuses an existing analysis until the tender or profile changes.

- `id`
- `tender_id`
- `profile_id` (NULL for level 1 — it does not depend on the profile)
- `level TEXT` — quick / standard / deep
- `model TEXT` — the Claude model slug **at the time of the call** (for cost audit and history). Per **ADR-029** the current `tier → slug` mapping lives in `infra/config.yaml`; in the architecture we operate with the tier names `cheap` / `balanced` / `premium`. This field stores the concrete slug (`claude-haiku-4-5-20251001`, `claude-sonnet-4-6`, `claude-opus-4-7`, etc.) at the time of the call so that a year later one can say “which exact model wrote this analysis”.
- `output JSONB`
- `tokens_input INT`, `tokens_output INT`, `tokens_cached INT`
- `cost_usd NUMERIC`
- `created_at`
- `UNIQUE (tender_id, profile_id, level)` — the cache reuses an existing analysis

### 2.17. ai_usage

Bookkeeping of AI spend per tenant per month per level for enforcing plan limits (ADR-024).

- `tenant_id`, `period CHAR(7)` (YYYY-MM), `level`, `count`, `tokens_total`, `cost_usd_total`

### 2.18. subscriptions (new, ADR-033 §5.6)

A tenant's subscriptions — an entity separate from the tenant (the tier used to be stored in `tenants.tier`). It appears as a separate table because one tenant can change subscriptions over time (history), and because the choice of payment processor is per subscription, not per tenant.

- `id UUID PRIMARY KEY`
- `tenant_id UUID REFERENCES tenants(id) NOT NULL`
- `tier TEXT NOT NULL CHECK (tier IN ('free','lite','starter','pro','business','enterprise'))`
- `payment_processor TEXT DEFAULT 'paddle' CHECK (payment_processor IN ('paddle','corvuspay','uzcard','kaspi','stripe'))` — the processor choice per subscription (ADR-033 groundwork for a future ADR)
- `paddle_subscription_id TEXT` — the external ID in Paddle; NULL for other processors
- `status TEXT NOT NULL CHECK (status IN ('trial','active','suspended','cancelled'))`
- `started_at TIMESTAMPTZ NOT NULL`
- `next_billed_at TIMESTAMPTZ`
- `cancelled_at TIMESTAMPTZ`
- `created_at`, `updated_at`

In phase 0 all subscriptions are `payment_processor='paddle'`. The Paddle/CorvusPay hybrid is phase 2 after a future ADR. UZ/KZ processors — phase 4.

### 2.19. anti_abuse_identifiers (new, ADR-018 revised)

A universal anti-abuse identifier for trial activation (see ADR-018 revised). One identifier_value_hash can activate only one trial within 12 months — this is anti-multi-accounting at the schema level.

- `id UUID PRIMARY KEY`
- `tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE`
- `identifier_type TEXT NOT NULL CHECK (identifier_type IN (
    'serbian_pib_doo',
    'serbian_jmbg',
    'card_fingerprint',
    'email_domain',
    'device_fingerprint',
    'manual_b2b_attestation'
  ))`
- `value_hash CHAR(64) NOT NULL` — the SHA-256 of the identifier's normalized value
- `value_last4 TEXT` — the last 4 characters for display to the user (for example, “card •••• 1234”, “PIB •••• 5678”)
- `risk_score_at_signup INT` — 0–100, a heuristic at sign-up time
- `verification_method TEXT` — `'card_setup_intent'` / `'email_link'` / `'apr_lookup'` / `'manual_review'`
- `verified_at TIMESTAMPTZ`
- `expires_at TIMESTAMPTZ NOT NULL` — usually `verified_at + INTERVAL '12 months'`; a repeat trial with the same value_hash is blocked until it expires
- `region CHAR(2) REFERENCES countries(code)` — for geographic segmentation
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**UNIQUE index for anti-multi-account:**

```sql
CREATE UNIQUE INDEX anti_abuse_value_hash_active_uniq
  ON anti_abuse_identifiers (value_hash);
```

> **Semantics after the T-fix (Chat 27):** the index became a **full** unique one — the original `WHERE expires_at > NOW()` was removed because `NOW()` is not IMMUTABLE and a partial index with it is not allowed. The uniqueness of `value_hash` is now **global**, not only among active ones: a repeat trial with the same `value_hash` is blocked even after 12 months have passed (`expires_at <= NOW()` no longer “frees” the hash). The index name (`..._active_uniq`) is kept as in the code of 0001, but “active” no longer reflects the behavior. **Revision point when anti-abuse is switched on (billing track):** restore the TTL semantics through an IMMUTABLE-compatible mechanism (for example, a partial index on a boolean flag column `is_active` instead of an expression with `NOW()`).

### 2.20. auth_tokens (Sprint-19, migration 0006; purpose extended in 0007)

One-time pre-auth tokens of the cabinet: email confirmation, passwordless login (magic link) and Telegram channel binding (deep link, T3). Only the SHA-256 of the raw token is stored. **NO RLS** — a pre-auth table (accessed before the tenant is established, like `sources`/tenders, see §4).

- `id UUID PRIMARY KEY`
- `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `purpose TEXT NOT NULL` — the purpose, CHECK `auth_tokens_purpose_check`:
  - `email_verify` — email confirmation at signup (0006)
  - `magic_link` — passwordless login (0006)
  - `channel_bind` — a one-time bind token for the deep link `/start <token>` of Telegram binding (**0007, T3**): the cabinet generates it for its `user_id`, the bot burns it and creates `user_channels`
- `token_hash CHAR(64) NOT NULL` — the SHA-256 of the raw token (the token itself is not stored)
- `expires_at TIMESTAMPTZ NOT NULL` — TTL (channel_bind — 15 min)
- `consumed_at TIMESTAMPTZ` — the moment of burning (NULL = active)
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**Indexes:** `UNIQUE (token_hash)`; `(user_id, purpose)`; partial `(expires_at) WHERE consumed_at IS NULL` (active ones).

**Accompanying function (0006):** `delete_unconfirmed_accounts()` — deletes `users` with `email_verified_at IS NULL` older than 72h (ADR-018) + orphaned `tenants`; calling it by cron is out of scope (the function is ready, not wired in).

### 2.21. tender_user_status (migration 0008)

The user’s **status funnel** of a tender at tenant level: the subscriber’s mark on a specific tender in their procurement workflow pipeline. One status per (tenant_id, tender_id) pair. This is **not** `tenders_normalized.status` (§2.10 — the status of the tender itself from the source: active/awarded/cancelled/closed) but a per-tenant user state. The foundation for buttons/summary/reminders (handlers and workers are separate PRs; this PR has only the schema + the ORM model). tenant-scoped with a **direct** `tenant_id` column → a direct RLS policy `tenant_isolation` (see §4).

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `tender_id UUID NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE`
- `status TEXT NOT NULL DEFAULT 'new'` — CHECK `tender_user_status_status_check`: `status IN ('new','watching','active','submitted','won','lost','skipped')`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` — the ORM model sets `onupdate=NOW()`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`

**The funnel of 7 statuses (a port of v6 `STATUS_LABELS`):**

| Status | Label (v6) | Meaning |
|---|---|---|
| `new` | 🆕 New | default, before the user’s first action |
| `watching` | 👀 Researching | noted, being evaluated |
| `active` | ✅ Participating | decided to participate, preparation under way |
| `submitted` | 📨 Bid submitted | the bid has been sent to the buyer |
| `won` | 🏆 Won | the tender is won |
| `lost` | 😔 Lost | the tender is lost |
| `skipped` | ❌ Skipped | decided not to participate |

The default `new` is set at DB level (`server_default`), so an INSERT without an explicit status is correct. The source-of-truth constant for the UI and the CHECK is `STATUSES` in `src/db/models/tender_status.py`.

**Indexes:** `UNIQUE (tenant_id, tender_id)` (`tender_user_status_tenant_tender_uniq` — one status per tender within a tenant); `(tenant_id, status)` (`tender_user_status_tenant_status_idx` — “my participating/researching/won” selections for the summary).

**RLS:** enabled by migration 0008 — `ALTER TABLE tender_user_status ENABLE ROW LEVEL SECURITY` + a direct policy `tenant_isolation USING (tenant_id = current_setting('app.current_tenant_id')::uuid)`, as for `profiles` (§4).

### 2.22. reminder_log (migration 0009)

A log of sent **deadline reminders** — the idempotency table of the reminder worker (`src/workers/reminders.py`, a port of v6 `reminders_loop`). One row per sent **threshold** (5/3/1/0 days before `tenders_normalized.deadline_at`) per (tenant_id, tender_id) pair. UNIQUE `(tenant_id, tender_id, threshold_days)` guarantees “one threshold — once”: the worker does **claim-then-send** via `INSERT ... ON CONFLICT (tenant_id, tender_id, threshold_days) DO NOTHING RETURNING id` in a separate short transaction and sends the message ONLY on a non-empty `RETURNING` (protection against a race/double start). tenant-scoped with a **direct** `tenant_id` column → a direct RLS policy `tenant_isolation` (like `profiles`/`tender_user_status`, see §4). The worker writes privileged (the owner bypasses RLS, a cross-tenant server operation); the policy is for schema consistency.

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `tender_id UUID NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE`
- `threshold_days INT NOT NULL` — the threshold in days before the deadline (5 / 3 / 1 / 0)
- `sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` — the moment of the reminder's claim

**Indexes:** `UNIQUE (tenant_id, tender_id, threshold_days)` (`reminder_log_tenant_tender_threshold_uniq` — one threshold per tenant↔tender pair; the claim's `ON CONFLICT` also rests on it).

**RLS:** enabled by migration 0009 — `ALTER TABLE reminder_log ENABLE ROW LEVEL SECURITY` + a direct policy `tenant_isolation USING (tenant_id = current_setting('app.current_tenant_id')::uuid)`, as for `profiles`/`tender_user_status` (§4).

> **Reliability assumption (a v6 port):** if the channel fails after a successful claim the log is **not rolled back** — better to miss one reminder than to loop spam (rollback → repeated claim → repeated send every cycle). Recorded in a comment in `reminders.py`.

### 2.23. summary_log (migration 0010)

A log of sent **daily summaries** — the idempotency table of the daily-summary worker (`src/workers/summary.py`, a port of v6 `daily_summary`). One row per sent summary per tenant per **calendar date** (in the tenant’s tz). UNIQUE `(tenant_id, summary_date)` guarantees “one summary per day per tenant”: the worker does **claim-then-send** via `INSERT ... ON CONFLICT (tenant_id, summary_date) DO NOTHING RETURNING id` in a separate short transaction and sends the message ONLY on a non-empty `RETURNING` (protection against a race/double start). tenant-scoped with a **direct** `tenant_id` column → a direct RLS policy `tenant_isolation` (like `profiles`/`tender_user_status`/`reminder_log`, see §4). The worker writes privileged (the owner bypasses RLS, a cross-tenant server operation); the policy is for schema consistency.

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `summary_date DATE NOT NULL` — the calendar date of the summary in the tenant's zone (`COALESCE(tenants.tz, 'Europe/Belgrade')`), not the server UTC date
- `sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` — the moment of the summary's claim

**Indexes:** `UNIQUE (tenant_id, summary_date)` (`summary_log_tenant_date_uniq` — one summary per tenant↔calendar date pair; the claim's `ON CONFLICT` also rests on it).

**RLS:** enabled by migration 0010 — `ALTER TABLE summary_log ENABLE ROW LEVEL SECURITY` + a direct policy `tenant_isolation USING (tenant_id = current_setting('app.current_tenant_id')::uuid)`, as for `profiles`/`tender_user_status`/`reminder_log` (§4).

> **Send window + anti-spam (a v6 port):** the summary is sent once per tenant calendar day and not earlier than `SUMMARY_HOUR` (a module constant in `summary.py`, 8 by default — morning by tz). If all funnel counters are 0 AND there are no upcoming deadlines — no claim is made, an empty summary is not sent. If the channel fails after the claim the log is **not rolled back** (anti-spam, the same assumption as in `reminder_log`).

### 2.24. subscription_reminder_log (migration 0017)

A log of sent **subscription expiry reminders** — the idempotency table of the subscription reminder worker (`src/workers/subscription_reminders.py`). One row per sent **threshold** (7 / 1 day before `tenants.access_until`) per tenant, tied to a specific access end date. UNIQUE `(tenant_id, threshold_days, access_until_snapshot)` guarantees “one threshold per one end date, once”: the worker does **claim-then-send** via `INSERT ... ON CONFLICT (...) DO NOTHING RETURNING id` in a separate short transaction and sends ONLY on a non-empty `RETURNING`. Sending goes to Telegram AND email (both channels are tried under one claim). tenant-scoped with a **direct** `tenant_id` column → a direct RLS policy `tenant_isolation` (like `reminder_log`/`summary_log`, see §4). The worker writes privileged (the owner bypasses RLS, a cross-tenant server operation); the policy is for schema consistency.

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `threshold_days INT NOT NULL` — the threshold in days before access ends (7 / 1)
- `access_until_snapshot TIMESTAMPTZ NOT NULL` — a snapshot of `tenants.access_until` at the moment of the claim; the key UNIQUE field: when the subscription is **extended** `access_until` changes → a new snapshot → a new UNIQUE key → the thresholds fire again for the new end date
- `sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()` — the moment of the reminder's claim

**Indexes:** `UNIQUE (tenant_id, threshold_days, access_until_snapshot)` (`subscription_reminder_log_tenant_threshold_snapshot_uniq`).

**RLS:** enabled by migration 0017 — `ALTER TABLE subscription_reminder_log ENABLE ROW LEVEL SECURITY` + a direct policy `tenant_isolation USING (tenant_id = current_setting('app.current_tenant_id')::uuid)`, as for `reminder_log`/`summary_log` (§4).

> **Channels + anti-spam:** sent to Telegram (an active `user_channels` binding) AND email (`users.email`, if `email_status != 'bounced'`). If there is no channel at all — no claim is made (the reminder may still go out when a channel appears). A failure of one channel neither rolls back the claim nor blocks the other channel. `access_until IS NULL` (unlimited demo) → do not send. `access_until <= now()` (already expired) → do not send (SQL filter `access_until > now()`). Sending not earlier than `SEND_HOUR` (9:00 in the tenant's tz, analogous to `SUMMARY_HOUR`).

### 2.25. tender_awards (migration 0018)

A table of **tender award decisions** (F03 eNabavke). Source: `DocumentTypeId = 11` (F03 ordinary) and `204` (F03 for framework agreements/DSN). One record = one decision per tender or lot; multi-lot procurements give N rows with different `lot_number`. **No RLS** — awards are public data, not tenant-scoped. FK to `tenders_normalized` with `ON DELETE CASCADE`.

Lookup key: `(tender_id, lot_number, decision_date)` — an ordinary composite index (no UNIQUE: `HasChanged=True` produces a new record for the same lot).

- `id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`
- `tender_id UUID NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE`
- `source_uid TEXT NOT NULL` — the link to the procurement (analogous to source_uid in tenders)
- `document_type_id SMALLINT NOT NULL` — 11 or 204 (the type of the F03 form)
- `lot_number TEXT` — LotNo; NULL = a single procurement
- `lot_title TEXT` — LotTitle
- `winner_name TEXT` — VAwardDecisions.ContractorsNames
- `winner_pib TEXT` — VAwardDecisions.ContractorIDs (the winner’s PIB)
- `customer_pib TEXT` — VContractRegisterPublic.CAIdentificationNumber
- `customer_name TEXT` — the contracting authority (naručilac, ContractingBody from the list)
- `decision_date DATE` — VAwardDecisions.DecisionDate
- `award_amount_rsd NUMERIC(18,2)` — VContractRegisterPublic.TotalValue
- `award_amount_vat_rsd NUMERIC(18,2)` — TotalValueVat
- `award_amount_eur NUMERIC(18,2)` — computed at the NBS rate (filled by the parser of STAGE 3; at STAGE 2 = NULL)
- `currency_id SMALLINT` — TotalValueCurrencyId (1=RSD)
- `contract_date DATE` — VContractRegisterPublic.ContractDate
- `is_framework BOOLEAN NOT NULL DEFAULT false` — IsUponFA / framework
- `duration_months INT` — DurationMonth
- `has_changed BOOLEAN NOT NULL DEFAULT false` — HasChanged (a changed decision)
- `raw JSONB` — a raw snapshot of the award record (insurance against loss of fields)
- `captured_at TIMESTAMPTZ` — the moment of capture from the source
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`

**Indexes:** `tender_awards_tender_id_idx` (tender_id), `tender_awards_winner_pib_idx` (winner_pib), `tender_awards_customer_pib_idx` (customer_pib), `tender_awards_decision_date_idx` (decision_date), `tender_awards_tender_lot_date_idx` (tender_id, lot_number, decision_date).

**Stages:** STAGE 2 = migration 0018 (the table is empty); STAGE 3 = the F03 parser + filling `award_amount_eur`.

### 2.26. trial_requests (migration 0012 — retroactive addition)

> ‼️ **Gap fill:** the table was created by migration **0012** but was NOT described in v1.7 of the schema (which stopped at 0018). Documented here. Closely tied to `tenants.access_until` (also 0012) and to the FORCE invariant (§4.2, 0032, where `trial_requests` is one of the two holes).

Requests for **trial access** from the cabinet. The decision is made by owner in the bot (`@nabrs_bot`) with inline buttons: approve N days / reject. tenant-scoped, a **direct** `tenant_id` column → a direct RLS policy `tenant_isolation`. The cabinet reads its own status under the set `app.current_tenant_id`; writing decisions is a privileged server operation (owner role).

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `status TEXT NOT NULL DEFAULT 'pending'` — CHECK `trial_requests_status_check`: `pending` / `approved` / `rejected`
- `comment TEXT` — the request’s comment
- `days INT` — the approved access term in days
- `requested_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `decided_at TIMESTAMPTZ` — the moment of owner’s decision
- `admin_message_id BIGINT` — the id of the admin message in Telegram (for editing)
- `decision_comment TEXT` (migration **0035**) — an optional operator comment on the decision (the web form `/admin/requests`); the bot path writes no comment → `NULL`, which is normal. Part of a reliable (not only Telegram) reply to the subscriber about the decision — see `app_billing_pending_notifications` §4.3.

**Indexes:** `trial_requests_tenant_status_idx` (tenant_id, status).

**RLS:** `ENABLE` (0012) + a direct policy `tenant_isolation` (0012) + **`FORCE`** (migration **0032** — 0012 forgot FORCE, the hole lived for months; see §4.2). The catalog check `relforcerowsecurity` is the guard `tests/db/test_rls_force_invariant.py`.

### 2.27. award_watch_subscriptions + award_watch_sent (migration 0019)

Alerts about **tender winners** (VG-06 E3b). A tenant’s subscription to a winner’s PIB + dedup of sent alerts. Both tables were created **without RLS** in 0019 (the notifier is a system one, cross-tenant); RLS was attached in **0021** (ENABLE + policy + FORCE).

**`award_watch_subscriptions`** — a tenant’s subscription to a specific `winner_pib`:

- `id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `winner_pib TEXT NOT NULL` — the PIB of the tracked winner
- `winner_name TEXT` — the name (a snapshot for the UI)
- `is_active BOOLEAN NOT NULL DEFAULT true` — an UPSERT on re-subscription reactivates (`ON CONFLICT ... SET is_active=true`)
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(tenant_id, winner_pib)` — `award_watch_subscriptions_tenant_pib_uniq`

**Indexes:** `award_watch_subscriptions_winner_pib_idx` (winner_pib — the notifier JOINs `tender_awards.winner_pib`), `award_watch_subscriptions_tenant_id_idx` (tenant_id — the `/watching` command, the gate).

**RLS:** ENABLE (0021) + a **direct** policy `tenant_isolation` on `tenant_id` (0021) + FORCE (0021).

**`award_watch_sent`** — dedup of sent alerts (subscription × award). On subscribing, all already existing awards of the PIB are recorded at once (a “watermark”) — new alerts go only for future ones:

- `id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`
- `subscription_id BIGINT NOT NULL REFERENCES award_watch_subscriptions(id) ON DELETE CASCADE`
- `award_id BIGINT NOT NULL REFERENCES tender_awards(id) ON DELETE CASCADE`
- `sent_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(subscription_id, award_id)` — `award_watch_sent_sub_award_uniq`

**Indexes:** `award_watch_sent_subscription_id_idx` (subscription_id).

**RLS:** ENABLE (0021) + a **join policy** `tenant_isolation` (it has no `tenant_id` of its own — a join via `award_watch_subscriptions.tenant_id`, 0021) + FORCE (0021).

### 2.28. referrals (migration 0020)

A register of invitations for native virality via a Telegram deep link (`?start=ref_<inviter_tg_id>`) — the PLG referral loop (VG-27). **No RLS** (the table works at the level of Telegram users `user_channels`, outside tenant isolation; 0021/0023 do NOT attach ENABLE/FORCE to it — under `tendersrb_app` access goes through the GRANT from 0021, cross-tenant via the SECURITY DEFINER `app_resolve_tenant_by_tg`/`app_referral_grant_bonus`, see §4.2).

- `id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`
- `inviter_user_id BIGINT NOT NULL` — the inviter’s Telegram chat_id (bigint, **NOT** the `users.id` UUID — identification via `user_channels.external_id`)
- `invitee_user_id BIGINT` — the invitee’s Telegram chat_id; NULL until onboarding
- `ref_code TEXT NOT NULL` — the deep-link payload (audit)
- `status TEXT NOT NULL DEFAULT 'pending'` — CHECK `referrals_status_check`: `pending` / `rewarded` / `rejected`
- `bonus_days_inviter INT NOT NULL DEFAULT 0`, `bonus_days_invitee INT NOT NULL DEFAULT 0`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `rewarded_at TIMESTAMPTZ`
- UNIQUE `(invitee_user_id)` — `referrals_invitee_uniq`: 1 invitee → 1 inviter → rewarded exactly once (the core of anti-abuse)

**Indexes:** `referrals_inviter_user_id_idx` (inviter_user_id — the query “how many have I invited”).

> **Bonus days:** a date addition to `tenants.access_until`; the tier is raised to `pro` for lite/starter/free/NULL, NOT lowered for pro/business/enterprise (`app_referral_grant_bonus` 0023, §4.2).

### 2.29. billing_subscriptions + billing_webhook_events (migration 0028)

The universal payment connector (Paddle/Stripe) — VG-11.1. **The key billing schema.**

**`billing_subscriptions`** — a tenant-scoped subscription (a tenant sees only its own):

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id)`
- `provider TEXT NOT NULL` — `stripe` / `paddle`
- `provider_customer_id TEXT` — the customer-id at the provider
- `provider_subscription_id TEXT` — the subscription-id at the provider
- `status TEXT NOT NULL` — the subscription status at the provider
- `plan_key TEXT` — the plan key (storefront)
- `current_period_end TIMESTAMPTZ` — the end of the paid period
- `created_at`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(provider, provider_subscription_id)` — `billing_subscriptions_provider_sub_uniq` (the `ON CONFLICT` target of the webhook upsert)

**Indexes:** `billing_subscriptions_customer_idx` (provider, provider_customer_id), `billing_subscriptions_tenant_idx` (tenant_id).

**RLS:** ENABLE (0028) + a **direct** policy `tenant_isolation` on `tenant_id` (0028) + FORCE (0028). The cross-tenant upsert from the webhook goes via the SECURITY DEFINER `app_billing_upsert_subscription`; resolving the tenant by customer_id — `app_billing_resolve_tenant_by_customer` (both 0028, §4.2). Applying a payment to a tenant (tier/access) reuses `app_admin_set_tier`/`app_admin_grant_access` (0025).

**`billing_webhook_events`** — a **platform** webhook deduplication table, **without RLS**, without PII (`tendersrb_app` writes/reads directly via the GRANT from 0021):

- `id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`
- `provider TEXT NOT NULL`, `event_id TEXT NOT NULL`
- `processed_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(provider, event_id)` — `billing_webhook_events_provider_event_uniq` (idempotency of webhook processing)

### 2.30. tender_folders + folder_items + tender_notes (migration 0029)

Workflow/retention “my tenders” (VG-17). Three tenant-scoped tables under **direct** RLS (ENABLE + a direct policy `tenant_isolation` on `tenant_id` + FORCE — all in 0029, modelled on 0028). Cross-tenant is not needed anywhere → no SECURITY DEFINER is introduced; grants come via DEFAULT PRIVILEGES from 0021. “Watching”/deadline reminders reuse `tender_user_status` (0008) + the worker `src/workers/reminders.py` — there are no new watch tables.

**`tender_folders`** — the tenant’s folders:

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `name TEXT NOT NULL` — CHECK `tender_folders_name_not_blank` (`length(btrim(name)) > 0`)
- `created_at`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE INDEX `tender_folders_tenant_name_uniq` `(tenant_id, lower(name))` — the name is unique within a tenant, case-insensitively

**`folder_items`** — a tender’s membership in a folder (a direct `tenant_id` column → direct RLS, no join):

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `folder_id UUID NOT NULL REFERENCES tender_folders(id) ON DELETE CASCADE`
- `tender_id UUID NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(folder_id, tender_id)` — `folder_items_folder_tender_uniq` (a tender at most once in a folder)
- **Indexes:** `folder_items_tenant_idx` (tenant_id), `folder_items_tender_idx` (tender_id)

**`tender_notes`** — the subscriber’s free-form note, one per (tenant, tender) pair:

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `tender_id UUID NOT NULL REFERENCES tenders_normalized(id) ON DELETE CASCADE`
- `body TEXT NOT NULL`
- `created_at`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- UNIQUE `(tenant_id, tender_id)` — `tender_notes_tenant_tender_uniq`

### 2.31. admin_audit_log (migration 0030)

An immutable **global** log of privileged actions of the administrator-operator (who by email / what / over which tenant-user / details before→after / when) — VG-29.4. **NOT tenant-scoped → no RLS is attached.** ‼️ But 0021 granted `ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES TO tendersrb_app` — a new table would automatically get direct access for the app role. To keep “direct access of the app role fail-closed”, 0030 **explicitly** runs `REVOKE ALL ON admin_audit_log FROM tendersrb_app` + `REVOKE ALL ON SEQUENCE admin_audit_log_id_seq`. The app role works with the log ONLY via SECURITY DEFINER (`app_admin_write_audit`/`app_admin_list_audit`/`app_admin_audit_total`, 0030, §4.2); a direct SELECT/INSERT under the app role → “permission denied”.

- `id BIGSERIAL PRIMARY KEY`
- `at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `actor_email TEXT NOT NULL` — the operator
- `action TEXT NOT NULL`
- `target_tenant_id UUID`, `target_user_id UUID` — the target of the action
- `details JSONB NOT NULL DEFAULT '{}'::jsonb` — before→after
- `ip TEXT`

**Indexes:** `admin_audit_log_at_idx` (at DESC), `admin_audit_log_actor_idx` (actor_email), `admin_audit_log_target_tenant_idx` (target_tenant_id).

### 2.32. channel_dead_notice_log (migration 0031)

A log of “delivery channel dropped” incidents — the idempotency of the worker `src/workers/channel_dead_notify` (one email per ONE incident). An incident = a continuous period while a tenant’s channel is not alive (the predicate `src/workers/matching.channel_states`). The **“open incident”** model (not a snapshot, like 0017): dropped → claim (INSERT) + email; still dead → the claim does not pass (an open incident exists) → no second email; restored → `resolved_at = now()`; dropped again → no open ones → a new claim + a new email.

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`
- `channel TEXT NOT NULL` — the delivery channel
- `notified_at TIMESTAMPTZ NOT NULL DEFAULT now()` — the moment of the email
- `resolved_at TIMESTAMPTZ` — the moment the incident was closed (NULL = open)

**Indexes:** a **partial UNIQUE** `channel_dead_notice_log_open_incident_uniq` `(tenant_id, channel) WHERE resolved_at IS NULL` — at most one open incident per pair; closed ones as many as you like (history). The target of `INSERT ... ON CONFLICT (tenant_id, channel) WHERE resolved_at IS NULL DO NOTHING RETURNING id` (inferring a partial index requires repeating its predicate in ON CONFLICT).

**RLS:** ENABLE (0031) + a **direct** policy `tenant_isolation` on `tenant_id` (0031) + **`FORCE`** (migration **0032** — 0031 forgot FORCE; see §4.2). The cabinet does NOT read the log — the banner computes the predicate live.

### 2.33. billing_notification_outbox (migration 0033)

A queue of payment lifecycle emails (blocker #3, LAUNCH_AUDIT §3). The webhook changed the DB silently (payment passed/failed/canceled/renewed) — the subscriber did not know the status of their money. The core `apply_billing_event` now puts a row into this queue IN THE SAME TRANSACTION as the webhook’s dedup claim; the standalone worker `src/workers/billing_notifications` picks it up and sends the email.

A **platform** table (**NOT tenant-scoped**: the idempotency key is the webhook’s `(provider, event_id)`, like `billing_webhook_events`), **without RLS**. ‼️ The same fail-closed perimeter as `admin_audit_log` (0030): 0033 explicitly runs `REVOKE ALL ... FROM tendersrb_app` on the table and its identity sequence — the app role’s access ONLY via SECURITY DEFINER functions (§4.2).

- `id BIGINT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY`
- `provider TEXT NOT NULL`, `event_id TEXT NOT NULL`
- `tenant_id UUID NOT NULL REFERENCES tenants(id)` — the column exists but there is NO RLS (platform table)
- `kind TEXT NOT NULL` — the email type (`paid`/`past_due`/`canceled`/…)
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `sent_at TIMESTAMPTZ` (NULL = not sent)
- UNIQUE `(provider, event_id)` — `billing_notification_outbox_provider_event_uniq` (a double webhook = one email)

**Indexes:** the partial `billing_notification_outbox_pending_idx` `(created_at, id) WHERE sent_at IS NULL` (the worker’s poll).

**SECURITY DEFINER** (0033, §4.2): `app_billing_enqueue_notification` (an idempotent enqueue from the core in one transaction with the dedup claim), `app_billing_pending_notifications` (unsent + the resolved email/access_until/tz; the addressee = the oldest user with a valid email, ORDER BY mirrors `channel_dead_notify` 0031), `app_billing_mark_notification_sent` (an atomic claim `UPDATE ... WHERE sent_at IS NULL RETURNING id` — a double run = one email).

### 2.34. ai_spend_daily (migration 0040)

A daily **snapshot** of AI spend across the whole Anthropic account (an observer before public access; per-tenant limits are separate, `src/ai/usage.py::_TIER_LIMITS`). A snapshot, not a timestamped log: the monthly aggregate `ai_usage` (PK `(tenant_id, period, level)`) is cumulative and has no timestamp on the row, so “spend for the day” is computed as “today’s cumulative period total minus yesterday’s snapshot of THE SAME period” — hence the `period` column in the snapshot (without it the 1st of the month would give a negative or inflated delta).

- `day DATE PRIMARY KEY` — a repeated worker run OVERWRITES the row (does not breed new ones)
- `period TEXT NOT NULL` — `YYYY-MM` UTC, the same period as in `ai_usage`
- `cost_usd_total NUMERIC NOT NULL`, `tokens_total BIGINT NOT NULL`, `calls INTEGER NOT NULL`, `tenants_count INTEGER NOT NULL`
- `captured_at TIMESTAMPTZ NOT NULL DEFAULT now()`

**A platform table, WITHOUT `tenant_id` and WITHOUT RLS** — not an omission but a mandatory condition: `FORCE ROW LEVEL SECURITY` applies to the owner too, and the spend worker reads CROSS-tenant without `app.current_tenant_id` — with RLS it would read 0 rows from its own table. A per-tenant breakdown is not stored, it is computed on the fly from `ai_usage` for the alert text. `REVOKE ALL ... FROM tendersrb_app` (0021 handed out DEFAULT PRIVILEGES on future tables) — access only for the privileged owner worker, the same fail-closed pattern as `admin_audit_log` (0030) / `billing_notification_outbox` (0033).

### 2.35. ai_spend_alert_log (migration 0040)

A claim-then-send log for spend alerts (modelled on `subscription_reminder_log`, 0017): PK `(day, kind)` = “one alert of one kind per day”. The `ai_spend_daily` snapshot is overwritten on every run, so it cannot serve as a claim — the log is separate.

- `day DATE NOT NULL`, `kind TEXT NOT NULL` — `'daily'` / `'month'`, independent thresholds, each with its own claim
- `sent_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- PRIMARY KEY `(day, kind)`

Platform, without RLS (the same grounds as 2.34); `REVOKE ALL ... FROM tendersrb_app`. A Telegram send failure does NOT roll the log back (anti-spam matters more than a guarantee of alert delivery).

### 2.36. template_library.i18n (migration 0036 — only a new column)

> The `template_library` table itself (the template library seed, migration 0016) is not described in this schema as a separate section — a gap outside the scope of the 0034–0044 task (the task records only this column).

`i18n JSONB NULL` — storefront translations (`{"sr": {"name":…,"description":…,"activity_kind":…}, "en": {...}}`) for the 16 public templates of the 0016 seed. Russian stays in the original `name`/`description`/`activity_kind` (the contract of existing reads does not change), `i18n` only supplements. User templates (proposed by users) stay with `i18n IS NULL` — their text is written by a human, in this case the render shows the original (fail-open).

---

## 3. Indexes

```sql
-- countries / classification_systems (foundational)
-- the PK already gives the needed B-tree

-- classifications
CREATE INDEX ON classifications (parent_code);

-- tenants
CREATE UNIQUE INDEX tenants_country_tax_id_uniq
  ON tenants (country_code, tax_id) WHERE tax_id IS NOT NULL;
CREATE INDEX ON tenants (country_code);

-- profiles
CREATE INDEX ON profiles (tenant_id, is_active);
CREATE INDEX ON profiles (country_code, is_active);  -- routing stage A
CREATE INDEX ON profiles USING GIN (enabled_source_keys);
CREATE INDEX ON profiles USING GIN (classifications jsonb_path_ops);
CREATE INDEX ON profiles USING GIN (regions);

-- tenders_raw
CREATE UNIQUE INDEX ON tenders_raw (source_key, source_uid);
CREATE INDEX ON tenders_raw (country_code);

-- tenders_normalized
CREATE UNIQUE INDEX ON tenders_normalized (source_key, source_uid);
CREATE INDEX ON tenders_normalized (country_code);  -- routing stage A
CREATE INDEX ON tenders_normalized (canonical_tender_id);
CREATE INDEX ON tenders_normalized (cluster_key) WHERE cluster_key IS NOT NULL;
CREATE INDEX ON tenders_normalized (published_at DESC);
CREATE INDEX ON tenders_normalized (deadline_at);  -- archived status is computed on the fly from deadline_at (T-fix: there is no index on is_archived)
CREATE INDEX ON tenders_normalized (customer_tax_id);
CREATE INDEX ON tenders_normalized USING GIN (classifications jsonb_path_ops);
CREATE INDEX ON tenders_normalized USING GIN (regions);
CREATE INDEX ON tenders_normalized USING GIN (
  to_tsvector('simple', title_normalized || ' ' || description_normalized)
);
CREATE INDEX ON tenders_normalized USING ivfflat (description_embedding vector_cosine_ops);

-- sources
CREATE INDEX ON sources USING GIN (country_codes);

-- regions
CREATE INDEX ON regions (country_code);

-- delivery_outbox
CREATE UNIQUE INDEX ON delivery_outbox (profile_id, tender_id, channel);
CREATE INDEX ON delivery_outbox (status, created_at) WHERE status IN ('pending','ai_pending','ai_done');

-- subscriptions
CREATE INDEX ON subscriptions (tenant_id, status);
CREATE INDEX ON subscriptions (paddle_subscription_id) WHERE paddle_subscription_id IS NOT NULL;

-- anti_abuse_identifiers
CREATE UNIQUE INDEX anti_abuse_value_hash_active_uniq
  ON anti_abuse_identifiers (value_hash);  -- fully unique (T-fix: WHERE expires_at > NOW() removed, not IMMUTABLE)
CREATE INDEX ON anti_abuse_identifiers (tenant_id);
CREATE INDEX ON anti_abuse_identifiers (identifier_type, expires_at);

-- auth_tokens (0006/0007)
CREATE UNIQUE INDEX auth_tokens_token_hash_uniq ON auth_tokens (token_hash);
CREATE INDEX ON auth_tokens (user_id, purpose);
CREATE INDEX auth_tokens_active_idx ON auth_tokens (expires_at) WHERE consumed_at IS NULL;

-- tender_user_status (0008)
CREATE UNIQUE INDEX tender_user_status_tenant_tender_uniq
  ON tender_user_status (tenant_id, tender_id);
CREATE INDEX tender_user_status_tenant_status_idx
  ON tender_user_status (tenant_id, status);

-- reminder_log (0009)
CREATE UNIQUE INDEX reminder_log_tenant_tender_threshold_uniq
  ON reminder_log (tenant_id, tender_id, threshold_days);

-- summary_log (0010)
CREATE UNIQUE INDEX summary_log_tenant_date_uniq
  ON summary_log (tenant_id, summary_date);

-- subscription_reminder_log (0017)
CREATE UNIQUE INDEX subscription_reminder_log_tenant_threshold_snapshot_uniq
  ON subscription_reminder_log (tenant_id, threshold_days, access_until_snapshot);

-- tender_awards (0018)
CREATE INDEX tender_awards_tender_id_idx ON tender_awards (tender_id);
CREATE INDEX tender_awards_winner_pib_idx ON tender_awards (winner_pib);
CREATE INDEX tender_awards_customer_pib_idx ON tender_awards (customer_pib);
CREATE INDEX tender_awards_decision_date_idx ON tender_awards (decision_date);
CREATE INDEX tender_awards_tender_lot_date_idx ON tender_awards (tender_id, lot_number, decision_date);

-- trial_requests (0012)
CREATE INDEX trial_requests_tenant_status_idx ON trial_requests (tenant_id, status);

-- award_watch_subscriptions / award_watch_sent (0019)
CREATE UNIQUE INDEX award_watch_subscriptions_tenant_pib_uniq
  ON award_watch_subscriptions (tenant_id, winner_pib);
CREATE INDEX award_watch_subscriptions_winner_pib_idx ON award_watch_subscriptions (winner_pib);
CREATE INDEX award_watch_subscriptions_tenant_id_idx ON award_watch_subscriptions (tenant_id);
CREATE UNIQUE INDEX award_watch_sent_sub_award_uniq
  ON award_watch_sent (subscription_id, award_id);
CREATE INDEX award_watch_sent_subscription_id_idx ON award_watch_sent (subscription_id);

-- referrals (0020)
CREATE UNIQUE INDEX referrals_invitee_uniq ON referrals (invitee_user_id);
CREATE INDEX referrals_inviter_user_id_idx ON referrals (inviter_user_id);

-- billing_subscriptions / billing_webhook_events (0028)
CREATE UNIQUE INDEX billing_subscriptions_provider_sub_uniq
  ON billing_subscriptions (provider, provider_subscription_id);
CREATE INDEX billing_subscriptions_customer_idx ON billing_subscriptions (provider, provider_customer_id);
CREATE INDEX billing_subscriptions_tenant_idx ON billing_subscriptions (tenant_id);
CREATE UNIQUE INDEX billing_webhook_events_provider_event_uniq
  ON billing_webhook_events (provider, event_id);

-- tender_folders / folder_items / tender_notes (0029)
CREATE UNIQUE INDEX tender_folders_tenant_name_uniq ON tender_folders (tenant_id, lower(name));
CREATE UNIQUE INDEX folder_items_folder_tender_uniq ON folder_items (folder_id, tender_id);
CREATE INDEX folder_items_tenant_idx ON folder_items (tenant_id);
CREATE INDEX folder_items_tender_idx ON folder_items (tender_id);
CREATE UNIQUE INDEX tender_notes_tenant_tender_uniq ON tender_notes (tenant_id, tender_id);

-- admin_audit_log (0030)
CREATE INDEX admin_audit_log_at_idx ON admin_audit_log (at DESC);
CREATE INDEX admin_audit_log_actor_idx ON admin_audit_log (actor_email);
CREATE INDEX admin_audit_log_target_tenant_idx ON admin_audit_log (target_tenant_id);

-- channel_dead_notice_log (0031) — a partial UNIQUE: at most one open incident per pair
CREATE UNIQUE INDEX channel_dead_notice_log_open_incident_uniq
  ON channel_dead_notice_log (tenant_id, channel) WHERE resolved_at IS NULL;

-- billing_notification_outbox (0033)
CREATE UNIQUE INDEX billing_notification_outbox_provider_event_uniq
  ON billing_notification_outbox (provider, event_id);
CREATE INDEX billing_notification_outbox_pending_idx
  ON billing_notification_outbox (created_at, id) WHERE sent_at IS NULL;
```

---

## 4. Row-Level Security (RLS) policies

RLS is enabled on tables bound to a tenant: `tenants`, `users`, `user_channels`, `profiles`, `profile_revisions`, `delivery_outbox`, `delivery_attempts`, `tender_ai_analyses`, `ai_usage`, `subscriptions`, `anti_abuse_identifiers`, `tender_user_status`, `reminder_log`, `summary_log`, `subscription_reminder_log`, `trial_requests` (0012), `award_watch_subscriptions` + `award_watch_sent` (0021), `billing_subscriptions` (0028), `tender_folders` + `folder_items` + `tender_notes` (0029), `channel_dead_notice_log` (0031). When a user enters the cabinet, `SET LOCAL app.current_tenant_id = '<uuid>'` is set, and RLS guarantees that even with a bug in the API a query will not return someone else's data.

> Since migration **0021 (VG-01)** bot+api run under the non-superuser role `tendersrb_app` (NOBYPASSRLS) and **`FORCE ROW LEVEL SECURITY`** is enabled on all tenant tables — the perimeter, the catalog of SECURITY DEFINER functions and the FORCE invariant are described in **§4.2**. `ENABLE` does not apply to the table owner, `FORCE` does; under the app role the difference is not visible → check the CATALOG `relforcerowsecurity`, not behavior (the guard `tests/db/test_rls_force_invariant.py`).

A policy template (using `profiles` as an example):

```sql
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON profiles
    USING (tenant_id = current_setting('app.current_tenant_id')::uuid);
```

The `tenant_isolation` policy on a **direct column** is created on: `tenants` (by `id`), `users`, `profiles`, `subscriptions`, `anti_abuse_identifiers`, `ai_usage` (by `tenant_id`) — migration 0001; `trial_requests` (by `tenant_id`) — migration 0012; and also `tender_user_status` (by `tenant_id`) — migration 0008; `reminder_log` (by `tenant_id`) — migration 0009; `summary_log` (by `tenant_id`) — migration 0010; `subscription_reminder_log` (by `tenant_id`) — migration 0017; `award_watch_subscriptions` (by `tenant_id`) — migration 0021; `billing_subscriptions` (by `tenant_id`) — migration 0028; `tender_folders` / `folder_items` / `tender_notes` (by `tenant_id`) — migration 0029; `channel_dead_notice_log` (by `tenant_id`) — migration 0031. A **join-based** policy (no `tenant_id` of its own), besides the tables of §4.1, is also received by `award_watch_sent` — a join via `award_watch_subscriptions.tenant_id` (0021). The other tables without their own `tenant_id` column — see §4.1.

### 4.1. Tables without `tenant_id` (Q-27, Variant A + T-fix)

Five tenant-scoped tables **have no `tenant_id` column of their own**: `user_channels`, `profile_revisions`, `delivery_outbox`, `delivery_attempts`, `tender_ai_analyses`. RLS is enabled on all of them in migration 0001 (default-deny, no policy).

Join-based `tenant_isolation` policies (via `profiles.tenant_id`) are set in migration 0003 (Q-27, owner decision — Variant A) for **four** of them:

```sql
CREATE POLICY tenant_isolation ON profile_revisions
  USING (profile_id IN (
    SELECT id FROM profiles
    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid));

CREATE POLICY tenant_isolation ON delivery_outbox
  USING (profile_id IN (
    SELECT id FROM profiles
    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid));

CREATE POLICY tenant_isolation ON delivery_attempts
  USING (outbox_id IN (
    SELECT id FROM delivery_outbox
    WHERE profile_id IN (
      SELECT id FROM profiles
      WHERE tenant_id = current_setting('app.current_tenant_id')::uuid)));

CREATE POLICY tenant_isolation ON tender_ai_analyses
  USING (profile_id IS NULL OR profile_id IN (
    SELECT id FROM profiles
    WHERE tenant_id = current_setting('app.current_tenant_id')::uuid));
```

**`user_channels`** (T-fix, Chat 27 — moved to `_RLS_NO_TENANT_ID_TABLES`) did **NOT** get the join policy of 0003: RLS is enabled on it (default-deny), there is no separate policy. Isolation is provided at the application level via a JOIN to `users` (`user_channels.user_id → users.tenant_id`). Previously `user_channels` was mistakenly listed among tables with `tenant_id`, because of which `CREATE POLICY ... USING (tenant_id = ...)` would have failed on rollout (one of the 4 bugs of 0001, see `docs/archive/chat27_finalization.md`).

`tender_ai_analyses` is a special case: rows with `profile_id IS NULL` (level-1 quick analyses that do not depend on the profile — see §2.16) remain **shared** and visible to all tenants, like the tenders themselves; profile rows (`profile_id IS NOT NULL`) are isolated by a join on `profiles.tenant_id`.

> **`tender_user_status` (§2.21, migration 0008)**, **`reminder_log` (§2.22, migration 0009)**, **`summary_log` (§2.23, migration 0010)** and **`subscription_reminder_log` (§2.24, migration 0017)** are NOT in this list: all four tables have their **own** `tenant_id` column, so they get a **direct** `tenant_isolation` policy (like `profiles`), not a join-based one.

**What is NOT covered by RLS** (available to all, without tenant_id):
- `tenders_raw`, `tenders_normalized`, `canonical_tenders` — tenders are public data from public platforms, not bound to a tenant. Visibility is gated at the routing level (via profiles) and the application (plan access to the archive per ADR-012).
- `tender_awards` (§2.25) — awards are public data, not tenant-scoped.
- `auth_tokens` (§2.20) — a pre-auth table of one-time tokens (email_verify/magic_link/channel_bind): accessed before the tenant is established (migration 0006).
- `referrals` (§2.28) — works at the level of Telegram users (`user_channels`), outside tenant isolation; the app role goes through GRANT (0021), cross-tenant via SECURITY DEFINER (§4.2).
- Reference tables: `countries`, `classification_systems`, `classifications`, `regions`, `sources` — also shared.

**Platform tables WITHOUT RLS but with an EXPLICIT REVOKE ALL for the app role** (access only via SECURITY DEFINER, fail-closed — §4.2):
- `admin_audit_log` (§2.31, 0030) — the global admin log.
- `billing_notification_outbox` (§2.33, 0033) — the payment email queue.
- ‼️ Both are NOT tenant-scoped, but 0021 handed out `ALTER DEFAULT PRIVILEGES ... GRANT ... TO tendersrb_app` on future tables, so without an explicit `REVOKE ALL` the app role would get direct access. `billing_webhook_events` (§2.29) needs no REVOKE — it has no PII, the app role writes the dedup id directly.

---

## 4.2. The `tendersrb_app` role perimeter + SECURITY DEFINER functions (VG-01, migrations 0021–0033)

**The RLS perimeter (a code invariant, the project rules).** Since migration **0021** bot+api run under the restricted role `tendersrb_app` (`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`), not under the superuser owner `tendersrb`. The role’s password comes from the ENV `APP_DB_PASSWORD` (fail-hard if empty). The role is granted `CONNECT` on the DB, `USAGE` on `public`, `SELECT/INSERT/UPDATE/DELETE` on all tables + `USAGE,SELECT` on sequences, and also **`ALTER DEFAULT PRIVILEGES`** — the same grants automatically extend to the tables/sequences of future migrations (‼️ consequence: a new platform table automatically gets direct access for the app role — to close it an explicit `REVOKE ALL` is needed, see `admin_audit_log`/`billing_notification_outbox`).

**FORCE ROW LEVEL SECURITY.** 0021 set `FORCE` on 17 tenant tables (15 existing + `award_watch_subscriptions`/`award_watch_sent`). Later FORCE was attached in the migrations of the new tables themselves (0028/0029/0031). ‼️ Two omissions were fixed by **0032**: `trial_requests` (ENABLE + policy since 0012, FORCE forgotten) and `channel_dead_notice_log` (0031 forgot FORCE). `ENABLE` makes everyone comply with the policy EXCEPT the owner; `FORCE` — the owner too. Under the app role (NOBYPASSRLS) the policy works even without FORCE; the hole is on the owner (a script/worker/migration under the owner role would silently read all tenants). A superuser (`BYPASSRLS`) bypasses RLS regardless of FORCE — a PostgreSQL guarantee. The invariant is checked against the catalog `relforcerowsecurity` (`tests/db/test_rls_force_invariant.py`, works WITHOUT a DB — the table list is derived from the migration SQL).

**Bootstrap of `user_channels`.** 0021 attached a `USING(true)` policy to `user_channels` (bootstrap access to chat_id↔user_id of ALL tenants before the GUC is set). 0022 closed the crutch: it replaced it with a join policy `user_id IN (SELECT id FROM users WHERE tenant_id = current_setting('app.current_tenant_id')::uuid)` (the pattern of 0003), and moved the tenant resolution before the GUC into the SECURITY DEFINER `app_resolve_tenant_id`/`app_resolve_tenant_id_by_user_id`.

**SECURITY DEFINER functions.** Cross-tenant operations (the operator looks at ALL tenants; resolving a tenant BEFORE the GUC is set; writing into another tenant from a webhook) under the app role throw `UndefinedObjectError` (no GUC) or touch 0 rows (FORCE RLS). The solution — functions owned by the superuser `tendersrb`: `SECURITY DEFINER` (executed with the owner’s rights → they bypass RLS), `SET search_path = public` (protection against path hijacking), `GRANT EXECUTE ... TO tendersrb_app`. The catalog:

| Function | Migr. | Purpose |
|---|---|---|
| `app_resolve_tenant_id(external_id, channel)` | 0021 | tenant_id by Telegram external_id (bot, before the GUC) |
| `app_active_tenant_ids()` | 0021 | all `trial`/`active` tenant_ids (workers) |
| `app_resolve_tenant_id_by_user_id(user_id)` | 0022 | tenant_id by user_id (cabinet↔bot bind, before the GUC) |
| `app_resolve_tenant_by_tg(tg)` | 0023 | tenant_id by Telegram chat_id (referral: reading the inviter tenant) |
| `app_referral_grant_bonus(tenant, days)` | 0023 | UPDATE `tenants` (bonus days + tier→pro; bypasses FORCE RLS when GUC=invitee) |
| `app_resolve_user_for_auth(email)` | 0024 | id/tenant_id/email_verified_at by email (email login before the GUC) |
| `app_admin_list_users` / `app_admin_users_total` / `app_admin_get_user` | 0025 | a cross-tenant list/detail of users (admin) |
| `app_admin_list_profiles` / `app_admin_user_delivered_count` | 0025 | a cross-tenant list of profiles / a delivery counter |
| `app_admin_grant_access` / `app_admin_revoke_access` / `app_admin_set_tier` | 0025 | a cross-tenant UPDATE of `tenants` (admin; reused by billing 0028) |
| `app_admin_delivery_backlog()` / `app_admin_reminder_freshness()` | 0026 | aggregates of the delivery backlog / the freshness of the reminder workers (`/admin/health`; only COUNT/MAX, no PII) |
| `app_billing_upsert_subscription(...)` | 0028 | a cross-tenant upsert of a subscription from the webhook (`ON CONFLICT (provider, provider_subscription_id)`) |
| `app_billing_resolve_tenant_by_customer(provider, customer)` | 0028 | tenant_id by the provider’s customer_id |
| `app_admin_write_audit / app_admin_list_audit / app_admin_audit_total` | 0030 | writing/reading `admin_audit_log` (a table with REVOKE ALL for the app role) |
| `app_billing_enqueue_notification(...)` | 0033 | an idempotent enqueue of a payment email (the core, in the dedup claim's transaction) |
| `app_billing_pending_notifications(limit)` | 0033 | unsent emails + the resolved addressee (the oldest user with a valid email) |
| `app_billing_mark_notification_sent(id)` | 0033 | an atomic claim of a row before sending (`UPDATE ... WHERE sent_at IS NULL RETURNING id`) |

> The general rule of the perimeter (the project rules): any authed read/write per tenant → `await repo.set_tenant(db, tid)` as the FIRST action in the transaction. Cross-tenant — ONLY via the SECURITY DEFINER above, not by direct SQL. Tests under a superuser do NOT prove the perimeter — a regression under the app role (`TEST_RLS_DATABASE_URL`).

## 4.3. SECURITY DEFINER functions — migrations 0034–0044

A continuation of the §4.2 catalog with the same pattern (`SECURITY DEFINER`, `SET search_path = public`, `GRANT EXECUTE ... TO tendersrb_app`, the superuser owner `tendersrb`). These migrations contain no new tables/columns apart from those noted in §2 — this group is almost entirely about functions.

| Function | Migr. | Purpose |
|---|---|---|
| `app_admin_list_trial_requests(limit, offset)` / `app_admin_trial_requests_total()` | 0034 | a cross-tenant list/counter of pending subscription requests (the web admin `/admin/requests`) |
| `app_admin_decide_trial_request(id, status, days)` | 0034 | an idempotent decision on a request (guarded `UPDATE ... WHERE status='pending'`) |
| `app_admin_decide_trial_request(id, status, days, comment)` | 0035 | replaces the 3-arg version of 0034 (DROP+CREATE — the signature changes): the decision + the operator’s `decision_comment` |
| `app_billing_pending_notifications(limit)` | 0035 | DROP+CREATE over 0033: the return is extended with `plan`/`decision_comment` (LEFT JOIN `trial_requests` for rows with `provider='manual', event_id='decision:<id>'`; for payment lifecycle emails both columns are `NULL`, behavior does not change) |
| `app_admin_disable_subscriber(tenant)` | 0037 | disabling a subscriber by THREE axes at once: `tenants.access_until=now()` + `user_channels.status→'disabled'` (only `active`) + `users.deleted_at=now()` |
| `app_admin_enable_subscriber(tenant, days)` | 0037 | the reverse: clears `deleted_at`, raises ONLY the channels disabled by this same function (`status='disabled'→'active'`), extends `access_until` only when `p_days > 0` |
| `app_admin_list_users` / `app_admin_get_user` | 0039 | DROP+CREATE over 0025: the `deleted_at IS NULL` filter removed, the `deleted_at` column added to `RETURNS TABLE` (otherwise a subscriber disabled by 0037 vanished from the admin and the “Restore” button was unreachable) |
| `app_admin_users_total` | 0039 | `CREATE OR REPLACE` (the return type does not change): counts the same set as the list — otherwise pagination diverges |
| `app_admin_extend_access(tenant, days)` | 0041 | an atomic extension of `access_until` by N days from the GREATER of the dates (current term / `now()`) — closes a bug where three granting points counted the term from `now()` and silently SHORTENED an already larger access; the body mirrors `src.domain.access.extend_until_sql`, the guard `tests/db/test_0041_admin_extend_access.py` |
| `app_admin_tenant_notify_target(tenant)` | 0041 | the addressee of the notification about a request decision: the active Telegram chat + the email of one live user of the tenant (cross-tenant, the body 1:1 with the former raw bot SQL) |
| `app_resolve_user_for_auth(email)` | 0042 | a deterministic `ORDER BY created_at, id` instead of a bare `LIMIT 1` — in production one email had two live users in different tenants, a non-deterministic choice gave “login into someone else’s empty cabinet” (incident 04.08.2026); the order is chosen so that TODAY’s behavior does not change |
| `app_admin_tenant_notify_target(tenant)` | 0042 | DROP+CREATE over 0041: the same determinism — the channel `ORDER BY linked_at DESC NULLS LAST, id DESC` (the freshest active one, one chat_id may have several active channels), the user `ORDER BY (role='admin') DESC, created_at, id` |

‼️ **0034/0037/0041 — cross-tenant admin functions over FORCE RLS tables** (`trial_requests`, `user_channels` via JOIN `users`, `tenants`) — the pattern 1:1 with 0025/0030, RLS is not weakened anywhere in this group.

---

## 5. Triggers and NOTIFY channels

### 5.1. notify_tender_change

After INSERT/UPDATE in `tenders_normalized` an SQL trigger fires, executing `pg_notify` on the channel `tender_changed`:

```sql
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
```

`country_code` in the payload is for the routing worker, to avoid an extra lookup when determining candidate profiles.

**The Routing Worker** keeps a persistent connection through `asyncpg` and listens to the channel:

```python
async with conn.cursor() as cur:
    await cur.execute("LISTEN tender_changed")
    async for notification in conn.notifies():
        await route_tender(notification.payload)
```

On a worker restart — `SELECT id FROM tenders_normalized WHERE first_seen_at > $checkpoint AND NOT routed`. The checkpoint is stored in Redis.

---

## 6. Template routing-engine queries

For the concept of two- (three-) stage matching, see the architecture notes (not published) §13.

### 6.1. Stage A — SQL prefilter (with the country_code filter after ADR-033)

A single query returns candidate `profile_id`s by array conditions and numeric constraints. After ADR-033 a condition was added for the country_code to match between the tender and the profile (or the profile subscribes to international tenders via NULL).

```sql
SELECT p.id, p.tenant_id
FROM profiles p
JOIN tenants t ON t.id = p.tenant_id
WHERE p.is_active = true
  AND t.status IN ('trial', 'active')
  -- NEW: country_code filter (ADR-033)
  AND (
    p.country_code = :tender_country_code
    OR p.country_code IS NULL  -- subscriptions to INT tenders (EBRD, UNDP)
  )
  AND :source_key = ANY(p.enabled_source_keys)
  AND (cardinality(p.regions) = 0 OR p.regions && :tender_regions)
  -- classifications via JSONB (after ADR-033 replaces cpv_include/exclude)
  AND (
    jsonb_array_length(p.classifications->'include') = 0
    OR EXISTS (
      SELECT 1 FROM jsonb_array_elements(p.classifications->'include') inc
      WHERE :tender_system_code = inc->>'system_code'
        AND :tender_classification_code LIKE replace(inc->>'code', '*', '%')
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM jsonb_array_elements(p.classifications->'exclude') exc
    WHERE :tender_system_code = exc->>'system_code'
      AND :tender_classification_code LIKE replace(exc->>'code', '*', '%')
  )
  AND (cardinality(p.procedure_types) = 0 OR :proc_type = ANY(p.procedure_types))
  AND (cardinality(p.languages) = 0 OR :lang = ANY(p.languages))
  AND (
      :amount_eur IS NULL
      AND p.include_no_amount = true
      OR :amount_eur BETWEEN COALESCE(p.min_amount_eur, 0)
                          AND COALESCE(p.max_amount_eur, 1e18)
  )
  AND EXTRACT(EPOCH FROM (:deadline - NOW())) / 86400.0 >= p.min_days_until_deadline;
```

With 1000 active profiles this query runs in 5-30ms thanks to the B-tree index on `country_code` + GIN on classifications.

### 6.2. Stage B — text matching

For the remaining candidates (usually 5-50) we check the keywords. This is done on the Python side with precompiled regexes (fast) or via an FTS subquery:

```sql
WITH cand AS (...)  -- the result of stage A
SELECT cand.profile_id
FROM cand
JOIN tenders_normalized t ON t.id = :tender_id
WHERE
  -- include: at least one all_of group matches
  EXISTS (
    SELECT 1 FROM jsonb_array_elements(p.keywords_include) g
    WHERE NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements_text(g->'all_of') kw
      WHERE position(lower(kw) IN t.title_normalized || ' ' || t.description_normalized) = 0
    )
  )
  -- exclude: no stop word
  AND NOT EXISTS (
    SELECT 1 FROM unnest(p.keywords_exclude) kw
    WHERE position(lower(kw) IN t.title_normalized || ' ' || t.description_normalized) > 0
  );
```

Alternatively — move to FTS `tsvector @@ to_tsquery`. At MVP `position()` is enough (milliseconds on 5-50 profiles).

### 6.3. Stage C — soft-match via embedding (optional)

If a profile has set neither `keywords_include` nor `cpv_include` but has an `ai_extra_context_embedding` — we compute the cosine similarity between the tender's embedding and the embedding of the profile's context. If >= 0.75 — we add it as “possibly interesting” (`match_score < 1.0`).

Implemented with the pgvector operator `<=>` (cosine distance):

```sql
SELECT p.id
FROM profiles p
WHERE p.ai_extra_context_embedding IS NOT NULL
  AND jsonb_array_length(p.keywords_include) = 0
  AND jsonb_array_length(p.classifications->'include') = 0
  AND (1 - (p.ai_extra_context_embedding <=> :tender_embedding)) >= 0.75;
```

This is a soft signal, marked “by interests” in the UI and not counted as a strict match.

### 6.4. Writing to the outbox

For each match — one INSERT per channel from `profile.delivery_channels`:

```sql
INSERT INTO delivery_outbox (profile_id, tender_id, channel, status, requires_ai, match_kind, match_score)
SELECT
    :profile_id,
    :tender_id,
    channel,
    CASE WHEN :ai_level <> 'none' THEN 'ai_pending' ELSE 'pending' END,
    :ai_level <> 'none',
    :match_kind,
    :match_score
FROM unnest(:channels::text[]) AS channel
ON CONFLICT (profile_id, tender_id, channel) DO NOTHING;
```

A UNIQUE conflict gives idempotency: re-routing the same tender to the same profile is a no-op.

### 6.5. Re-routing when a tender is updated

For a conceptual description of the behavior on a tender update, see the architecture notes (not published) §13.4. In short: if a significant field of the tender changed (amount, deadline, classifications), routing runs again; for profiles that have already been delivered to, a separate outbox record is created with `match_kind='update'` (the UNIQUE index on `(profile_id, tender_id, channel, match_kind)` allows an “update” as a separate record alongside the original `match_kind='new'`).

---

## 7. Cluster_key for cross-source deduplication (ADR-022 + ADR-033 addendum)

For the concept, see the architecture notes (not published) §11.4 and ADR-022. After ADR-033 the formula was updated: `country_code` becomes the **first** component of the key, to rule out cross-country false clustering (identical tax_id-format strings in different countries).

```
cluster_key = sha1(
    country_code
    + customer_tax_id_normalized
    + primary_classification.system_code
    + primary_classification.code
    + round(amount_eur, -3)        -- rounding to 1000 EUR
    + date_trunc('day', deadline_at)
)
```

The implementation of the formula is a function in `src/dedup/cluster_key.py` (appears in Sprint-12).

If two tenders from different sources have the same `cluster_key`, they are candidates for merging into one canonical. **Confirmation** — cosine similarity of description embeddings ≥ 0.92. If the candidates are confirmed, both are assigned a common `canonical_tender_id`. One of them becomes `primary` (priority by source: jnportal > obnova > EBRD > UNDP > tendersinfo).

In the notification the subscriber sees the **primary** with the note “also available on: obnova”.

---

## 8. Partitioning (a phase 2-3 plan)

Partitioning is not introduced at MVP — PostgreSQL comfortably holds tables up to several million rows without it. The plan for phase 3 (at ~1000 subscribers, ~3M rows in `tenders_normalized`):

- **`tenders_normalized`** — RANGE partitioning by `published_at`, monthly partitions. After ADR-033 — possibly sub-partitioning by `country_code` when growing to 5+ countries.
- **`delivery_outbox`** — partitioning by day (hot 7 days + cold), for fast pruning of old records.
- **`delivery_attempts`** — partition aggressively with a 90-day retention (after that the partitions are dropped).

The details of the migration are a separate sprint in phase 3.

---

## 9. Mapping to ADRs

| Part of the schema | ADR | What it provides |
|---|---|---|
| `tenants` + multi-tenant with one cluster | ADR-001 | Multi-tenant architecture |
| RLS on tenant-scoped tables (§4) | ADR-002 | PostgreSQL + isolation via Row-Level Security |
| `tenders_normalized` archived status (computed on the fly `deadline_at < now() - interval '30 days'`) + an index on `deadline_at` | ADR-012 | Visibility and archive of tenders (a hybrid of “by status + 30 days + a plan gate”); `is_archived` as a STORED column was removed in the T-fix (NOW() is not IMMUTABLE) |
| `anti_abuse_identifiers` (§2.19) + a global UNIQUE on value_hash | ADR-018 (revised) | Universal anti_abuse_identifier; for RS — KYCProvider('apr_rs'); for other countries — a fallback. T-fix: UNIQUE became full (no partial WHERE) |
| `tenders_raw` + `tenders_normalized` + `canonical_tenders` + cluster_key (§7) with country_code | ADR-022 (+ addendum) | A two-tier tender model + cross-source deduplication + anti cross-country collision |
| `profiles.country_code` + two-stage routing with the country_code filter (§6.1) + per-tier limits | ADR-023 (+ addendum) | The profile structure + a routing engine with a multi-country filter |
| `tender_ai_analyses` + `ai_usage` + UNIQUE(tender, profile, level) | ADR-024 (+ addendum) | A three-level AI pipeline + cache + a plan gate + parametrisation of {{country_name}}/target_language |
| `delivery_outbox` (UNIQUE + status machine) + `delivery_attempts` + `user_channels` | ADR-025 | The outbox delivery pattern + a per-channel adapter |
| `tender_user_status` (§2.21) — the tender status funnel at tenant level (7 values, default `new`) + direct RLS | migration 0008 (a v6 port; no separate ADR assigned — a UX funnel, at owner’s discretion) | The user’s tender status funnel (new/watching/active/submitted/won/lost/skipped); the foundation for buttons/summary/reminders |
| `reminder_log` (§2.22) — a log of sent deadline reminders (threshold 5/3/1/0) + UNIQUE `(tenant_id, tender_id, threshold_days)` + direct RLS | migration 0009 (a port of v6 `reminders_loop`; no separate ADR assigned — a port of existing logic, at owner’s discretion) | Idempotency of the reminder worker: claim-then-send, “one threshold — once” |
| `summary_log` (§2.23) — a log of sent daily summaries + UNIQUE `(tenant_id, summary_date)` + direct RLS | migration 0010 (a port of v6 `daily_summary`; no separate ADR assigned — a port of existing logic, at owner’s discretion) | Idempotency of the daily-summary worker: claim-then-send, “one summary per day per tenant” |
| `subscription_reminder_log` (§2.24) — a log of subscription expiry reminders + UNIQUE `(tenant_id, threshold_days, access_until_snapshot)` + direct RLS | migration 0017 (a port of the reminder_log logic; no separate ADR assigned — a port of existing logic, at owner’s discretion) | Idempotency of the subscription reminder worker: claim-then-send, TG+email, repeat after an extension via a new snapshot |
| `tender_awards` (§2.25) — award decisions F03 eNabavke + 5 indexes, no RLS | migration 0018 (VG-05 STAGE 2) | Analytics of winners/competitors/contracting authorities; source DocumentTypeId 11+204 |
| `trial_requests` (§2.26) + `tenants.access_until` — trial access requests, direct RLS | migration 0012 (STAGE T); FORCE added by 0032 | Manual approval of trial access by owner in the bot |
| `award_watch_subscriptions` + `award_watch_sent` (§2.27) — winner alerts, direct/join RLS | migration 0019 (VG-06 E3b); RLS+FORCE 0021 | A tenant’s subscription to a winner’s PIB + dedup of sent ones |
| `referrals` (§2.28) — the PLG referral loop, no RLS (Telegram-user level) | migration 0020 (VG-27) | Native virality via a deep link; bonus days added to `access_until` |
| **The RLS perimeter of the app role `tendersrb_app`** (§4.2) + FORCE RLS on tenant tables + SECURITY DEFINER `app_resolve_*`/`app_admin_*` | migrations 0021–0027, 0030, 0032 (VG-01) | ADR-002 hardening: non-superuser + FORCE + fail-closed cross-tenant |
| **The billing schema** `billing_subscriptions` + `billing_webhook_events` (§2.29) + `billing_notification_outbox` (§2.33) + `app_billing_*` | migrations 0028, 0033 (VG-11.1, blocker #3) | The Stripe/Paddle payment connector: subscriptions, webhook dedup, a payment email queue |
| `tenants.tier` → CHECK `ck_tenants_tier` + DEFAULT `lite` + NOT NULL | migration 0027 (audit R8) | The tier canon in the schema (SSOT `src/domain/tiers.py`) |
| `tender_folders` + `folder_items` + `tender_notes` (§2.30) — “my tenders”, direct RLS | migration 0029 (VG-17) | Workflow/retention; watch reuses `tender_user_status` + reminders.py |
| `admin_audit_log` (§2.31) — a global log of admin actions, REVOKE ALL for the app role + SECURITY DEFINER | migration 0030 (VG-29.4) | An immutable audit of the operator’s privileged actions |
| `channel_dead_notice_log` (§2.32) — a log of “channel dropped” incidents, direct RLS, an open-incident model | migration 0031; FORCE added by 0032 | Idempotency of `channel_dead_notify`: one email per incident |
| `auth_tokens` (§2.20) + the deep link `channel_bind` | ADR-018/ADR-025 | Magic link/email-verify + stitching cabinet↔bot (T3) |
| `tender_ai_analyses.model` — the slug at the time of the call | ADR-029 | Tier naming in the architecture + the slug in the DB for audit |
| `countries` + `classification_systems` + `country_code` propagation + `tenants.tax_id` + `subscriptions.payment_processor` | ADR-033 | Multi-country foundations: the countries registry, classifications abstraction, country_code propagation, groundwork for payment_processor |

---

## End of DB_SCHEMA.md
