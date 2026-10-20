# CODE_MAP — module map

**Derived document.** Generated from the code at `67aff991` (2026-10-18). Regenerate: `python3 scripts/code_map.py`.
Do not edit the map by hand when it diverges from the code: **the code is right**. The exception is the "Where to look" section (it lives in `scripts/code_map.py`).

Scope: **259 modules** in `src/`, **82 612 lines** of Python.

## Checking the map against fact

The map is a hypothesis, production is fact. Both checks are read-only, both are done from the development clone:

```
# routes: the number of paths in the live app’s schema (compare with the “Routes” section)
docker compose -f deploy/docker-compose.yml exec -T api python -c \
  "from src.api.main import app; print(len(app.openapi()['paths']))"

# tables: the list from the live DB (compare with the “DB tables” section)
docker compose -f deploy/docker-compose.yml exec -T postgres bash -lc \
  'psql -U $POSTGRES_USER -d $POSTGRES_DB -c \"\\dt\"'
```

As of 10.08.2026 checked: **72 of 72** paths matched in both directions; **41 of 42** tables (the discrepancy is the service table `alembic_version`).

## Where to look (predicates)

| Question | Predicate (not a retelling) |
| --- | --- |
| Where is an interface string? | `grep -rn "<string>" src/api/*/i18n.py src/i18n_common.py src/i18n_email.py` |
| Where is an entity rendered? | `grep -rln "<partial>.html" src/api/templates/` — fix ALL points, not one |
| Who accesses a table? | `grep -rn "<table>" src/ --include=*.py` + `grep -rn "<table>" alembic/versions/` |
| Does a table have RLS? | `grep -rn "FORCE ROW LEVEL SECURITY" alembic/versions/ \| grep <table>` |
| Where is the tier/limit gate? | `src/ai/usage.py` (`_TIER_LIMITS`), `src/plans/catalog.py`, `src/api/deps.py` |
| Where is access granted? | `src/domain/access.py`, `src/domain/access_grants.py` — the single point |
| Which route serves a screen? | `grep -rn "<template>.html" src/api/` |
| Where is a CSS class defined? | `grep -n "<class>" src/api/static/app.css` (there is NO Tailwind build) |
| What does migration N change? | `alembic/versions/` — the file by number; heads: `alembic heads` (in the development clone) |
| A cross-tenant operation? | `grep -rn "app_admin_\|app_resolve_" src/ alembic/versions/` — only SECURITY DEFINER |

## Entry points

| What | File | How it is launched |
| --- | --- | --- |
| HTTP API + cabinet + storefront | `src/api/main.py` | uvicorn, the `api` service |
| Telegram bot | `src/bots/telegram.py` | the `bot` service |
| Migrations | `alembic/` | `docker compose run --rm migrate` |
| Workers (21) | `src/workers/` | separate compose services |

## Packages of `src/`

### `src/` — 4 modules, 1334 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `i18n_email.py` | 720 | E-mail letter catalog (sr/ru/en) — SINGLE source for both surfaces |
| `i18n_common.py` | 547 | Shared vocabulary bot↔cabinet (sr/ru/en) — SINGLE source for both surfaces |
| `log_secrets.py` | 65 | Masking of secrets in the httpx log (a common module for all workers/the bot) |
| `__init__.py` | 2 | Tender Bot Serbia — application source root |

### `src/ai/` — 9 modules, 2809 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `client.py` | 598 | A wrapper over the Anthropic SDK (ARCHITECTURE §9 “AI Service Layer”, §14.6–14.7) |
| `usage.py` | 596 | Common helpers for AI usage accounting (§14.5) |
| `translation.py` | 531 | Translation of a tender card sr→ru over the original, with a Redis cache (VG-26) |
| `prompts.py` | 528 | Prompts of the AI pipeline v2 — reworked after the patterns of a production tool layer |
| `schemas.py` | 213 | Pydantic schemas of the JSON output of AI models (ARCHITECTURE §14.2 L1, §14.3 L2, §14.4 L3) |
| `profile_context.py` | 142 | A single builder of the supplier-profile context string for AI prompts |
| `track_record.py` | 122 | The company’s behavioral memory — a compact track record from ``tender_user_status`` |
| `translate_prompts.py` | 51 | Prompt for translating a tender card sr→ru (VG-26) |
| `__init__.py` | 28 | AI Service Layer (ARCHITECTURE §9, §14) |

### `src/analytics/` — 4 modules, 1961 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `awards_analytics.py` | 961 | VG-06 — analytics of the contracting authority and competitors over ``tender_awards`` |
| `report_builder.py` | 786 | Report builder «Analitika» (R4, owner decision 10.10.2026) — SQL layer |
| `reprocurement.py` | 207 | VG-07 phase-1 — reprocurement forecast |
| `__init__.py` | 7 | Analytics over tender_awards (VG-05 → VG-06) |

### `src/api/` — 17 modules, 3564 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `repo.py` | 606 | A thin DB access layer for the cabinet (Sprint-19) |
| `deps.py` | 495 | FastAPI dependencies of the cabinet (Sprint-19) |
| `templating.py` | 396 | The shared Jinja2 renderer of the cabinet (ADR-007: FastAPI + Jinja2 + HTMX + Tailwind) |
| `forms.py` | 312 | Parsing and validation of HTMX profile forms (Sprint-19) |
| `main.py` | 285 | FastAPI application (ADR-006) · 1 route(s) |
| `anti_abuse.py` | 252 | The anti-abuse layer of sign-up (ADR-018 revised, ARCHITECTURE §4) |
| `emailer.py` | 251 | Transactional e-mail sending (ARCHITECTURE §9) |
| `security.py` | 215 | Signing the session cookie and handling one-time tokens (Sprint-19) |
| `tiers.py` | 196 | The tier gate for profiles (ARCHITECTURE §12.1/§12.4, ADR-018 revised) |
| `config.py` | 118 | Configuration of the web cabinet (pydantic-settings, Sprint-19) |
| `pager.py` | 96 | The window of page numbers for a pager (shared by the cabinet and the storefront) |
| `nav_access.py` | 83 | Tenant access for the cabinet header: a status badge and plan marks on tools |
| `rate_limit.py` | 72 | In-memory rate limit for the public points of the cabinet (STAGE 2, security minimum) |
| `static_assets.py` | 63 | Static version: ``?v=`` in the URL and ``Cache-Control`` driven by it (TASK static-cache) |
| `admin_notify.py` | 62 | Best-effort notification of platform operators by e-mail |
| `admin_access.py` | 60 | A single source of truth about who is a platform operator |
| `__init__.py` | 2 | FastAPI application package (ADR-006) |

### `src/api/admin/` — 4 modules, 2224 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `admin_service.py` | 1307 | The service of the administrative cabinet (EPIC 6) |
| `routes_admin.py` | 638 | The administrative cabinet (EPIC 6) · prefix `/admin` · 23 route(s) |
| `sources_admin.py` | 278 | The “Izvori” admin — source management (SRC-0d, owner decision 30.09 item 5) |
| `__init__.py` | 1 | — |

### `src/api/billing/` — 2 modules, 103 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `routes_billing.py` | 101 | Billing routes (VG-11.1): the provider webhook and checkout from the cabinet · 2 route(s) |
| `__init__.py` | 2 | The HTTP layer of billing (VG-11.1): provider webhook + checkout from the cabinet |

### `src/api/cabinet/` — 44 modules, 19687 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `i18n.py` | 4584 | Cabinet multilingual support — sr/ru/en. Phase 1.1: skeleton + language switcher |
| `routes_profiles.py` | 1619 | Cabinet profile routes (ARCHITECTURE §12) · 19 route(s) |
| `feed_service.py` | 1131 | Delivery feed + burning deadlines + visible AI actions (VG-28.3b-BE) |
| `routes_tender_ai.py` | 911 | Routes of tender AI artifacts in the cabinet (web-ai-1 viewing + web-ai-2 generation) · 6 route(s) |
| `customer_analytics_service.py` | 897 | Contracting-authority (naručilac) analytics of a tender in the web cabinet (web-ai-3/4, steps 3–4) |
| `workflow_service.py` | 863 | Cabinet workflow/retention service (VG-17): folders, notes, watching |
| `preview_service.py` | 706 | Preview "how many tenders would have matched per month" (EPIC 3) |
| `channel_service.py` | 641 | The cabinet channel-binding service (Chat 26, T3 — stitching cabinet↔bot, C-04) |
| `auth_service.py` | 619 | Cabinet authentication service (ARCHITECTURE §4, ADR-018 revised) |
| `analitika_service.py` | 613 | The “Analitika” page in the cabinet — a report builder with Excel export (R4) |
| `vertical_packages.py` | 497 | The SSOT catalog of vertical profile packages (VG-14) |
| `routes_auth.py` | 495 | Cabinet authentication routes (ARCHITECTURE §4, ADR-018 revised) · 11 route(s) |
| `tender_ai_generate.py` | 439 | Web generation of AI artifacts from the cabinet (web-ai-2, step 2) |
| `profile_service.py` | 417 | The cabinet profile service (ARCHITECTURE §12, DB_SCHEMA §2.6/§2.7) |
| `market_service.py` | 389 | “Tržište” — a tab of the Vesti block on the dashboard (owner 13.10, step 3) |
| `routes_my_tenders.py` | 372 | Cabinet workflow/retention routes (VG-17): folders, notes, watching · 10 route(s) |
| `routes_account.py` | 350 | The “My account” section in the cabinet (owner 20.09.2026, addendum owner 22.09.2026) · 3 route(s) |
| `template_library_service.py` | 299 | The service of the search template library (EPIC 4 + EPIC 5) |
| `routes_channels.py` | 281 | Cabinet channel-binding routes (Chat 26, T3 — stitching cabinet↔bot, C-04) · 7 route(s) |
| `source_banner.py` | 275 | The banner “enable the source in all templates” and the honest figure “you have K of N” |
| `narucilac_page_service.py` | 273 | The “Contracting-authority analytics” page in the cabinet (A3, VG-06 lite) |
| `routes_library.py` | 273 | Routes of the search template library (EPIC 4) · 2 route(s) |
| `trial_service.py` | 266 | The trial access service for the cabinet (STAGE T) |
| `tender_ai_service.py` | 211 | Read-only viewing of tender AI artifacts in the web cabinet (web-ai-1, step 1) |
| `library_sectors.py` | 208 | One template storefront by sectors (owner complaint 01.10.2026: “all over the place, in a heap”) |
| `account_export_service.py` | 206 | The “Download my data” export from the cabinet (LEG-06, second half, owner 14.10.2026) |
| `competition_service.py` | 202 | The “Konkurencija” block on the tender card: who won before (owner 13.10, step 4) |
| `stats_service.py` | 186 | Cabinet dashboard statistics (Track A) |
| `account_delete_service.py` | 181 | Self-deletion of an account from the cabinet (LEG-06, owner 10.10.2026) |
| `template_prefill.py` | 133 | Profile prefill from a library template / vertical package — ONE point |
| `routes_narucilac.py` | 120 | The route of the “Contracting-authority analytics” page in the cabinet (A3, VG-06 lite) · 2 route(s) |
| `subscription_service.py` | 115 | Reading a tenant’s subscription for the “My subscription” section of the cabinet (VG-11.2) |
| `ai_limits_service.py` | 113 | The remainder of a tenant’s AI limits for display in the cabinet (read-only) |
| `my_tenders_export.py` | 112 | F2 — building the CSV export “My tenders” |
| `routes_subscription.py` | 112 | The “My subscription” section in the cabinet (VG-11.2) — now a section of the “Moj kabinet” hub · 1 route(s) |
| `routes_locale.py` | 87 | The cabinet interface language switcher (Phase 1.1) · 1 route(s) |
| `customer_suggest.py` | 85 | Buyer suggestions for the profile filters “follow / exclude” (0046) |
| `routes_analitika.py` | 83 | “Analitika” routes — a report builder with Excel export (R4) · 2 route(s) |
| `analitika_xlsx.py` | 82 | Excel export of the “Analitika” report builder (R4) |
| `routes_trial.py` | 82 | The route of the trial access request from the cabinet (STAGE T) · 1 route(s) |
| `library_counts.py` | 78 | The live figure “≈ N tenders in 30 days” for the /library storefront cards |
| `tender_path.py` | 40 | Normalisation of ``{tender_id}`` in cabinet paths ``/app/tenders/{id}…`` (#632) |
| `source_choices.py` | 39 | Source checkbox rows — one set for the template form, “Composition” and “as is” |
| `__init__.py` | 2 | The subscriber web cabinet (FastAPI + Jinja2 + HTMX + Tailwind, ADR-007) |

### `src/api/public/` — 18 modules, 7611 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `i18n.py` | 3041 | Minimal language mechanism for public showcase pages (TASK 1, SITE-04) |
| `routes_geo.py` | 736 | GEO-2: ``/tenderi`` landing pages — “by CPV section” and “by region” · 4 route(s) |
| `legal_texts.py` | 670 | Drafts of the storefront legal texts (TASK 2, SITE-05) |
| `routes_public.py` | 524 | Public marketing and legal showcase routes (TASK 1/2, SITE-01/04/05) · 7 route(s) |
| `routes_seo.py` | 375 | GEO/SEO foundation: robots.txt, sitemap.xml (+ chunks), llms.txt, favicon · 8 route(s) |
| `stats_page.py` | 338 | Data of the public “Statistika” storefront (``GET /statistika``, R3 ANALYTICS_PRODUCT) |
| `portal_filters.py` | 295 | Filters and sorting of the public ``/tenderi`` portal (TASK portal_filters) |
| `news.py` | 294 | Product news (owner 10.10.2026, ANALYTICS_PRODUCT §7): once — three languages |
| `routes_tender.py` | 283 | Public tender card ``/tender/{id}`` (ARCHITECTURE §5.2) · 2 route(s) |
| `routes_contact.py` | 233 | Feedback form: GET/POST /kontakt — the ONLY receiver for all forms · 2 route(s) |
| `sources_view.py` | 203 | The list of enabled sources for the storefront and help (SRC-0d, owner decision R7) |
| `sources_page.py` | 193 | Data of the “Sources” storefront (``GET /izvori``) |
| `geo_cache.py` | 119 | In-process TTL cache of ``/tenderi`` aggregates (CPV sections, regions) |
| `routes_news.py` | 114 | Product news: GET /novosti, GET /novosti/{slug}, POST /novosti/procitano · 3 route(s) |
| `home_ai_example.py` | 72 | A static snapshot of a real AI analysis for the home page (UX audit P1, owner 10.10) |
| `cpv_directory.py` | 63 | The CPV directory for ``/cpv-kodovi``: a tree of sections → groups and labels by language |
| `trial_text.py` | 56 | The trial period length in storefront texts: one source — ``settings.trial_days`` |
| `__init__.py` | 2 | Public routes (no authorization): tender card, webhooks (later) |

### `src/billing/` — 8 modules, 1056 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `service.py` | 337 | The core for applying billing events (VG-11.1) |
| `stripe.py` | 230 | The Stripe connector (VG-11.1) — all Stripe specifics are isolated here |
| `paddle.py` | 189 | The Paddle Billing connector (VG-11.1) — all Paddle specifics are isolated here |
| `registry.py` | 127 | The registry of payment connectors (VG-11.1) |
| `gateway.py` | 60 | The payment gateway abstraction (VG-11.1) |
| `models.py` | 57 | Provider-neutral billing models (VG-11.1) |
| `util.py` | 36 | Small provider-neutral helpers for connectors (VG-11.1) |
| `__init__.py` | 20 | The universal payment connector (VG-11.1) |

### `src/bots/` — 21 modules, 8963 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `telegram.py` | 3411 | Telegram bot (aiogram 3) — ARCHITECTURE §9, §12, ADR-006 |
| `i18n.py` | 934 | The chrome catalog of the Telegram bot — sr/ru/en (i18n Phase 2a: INBOUND handlers) |
| `awards_analytics_service.py` | 650 | On-demand contracting-authority analytics (VG-06) — the service for a Telegram button |
| `manage.py` | 548 | Management from the bot: “📋 My tenders” (``/list``) and pausing templates (``/pause``) |
| `compliance_checklist_service.py` | 365 | The on-demand checklist “what to prepare” (VG-09) — the service for the “📋 What to prepare?” button |
| `deep_analysis_service.py` | 361 | On-demand L3 deep analysis (§14.4) — the service for the Telegram button “🧠 Deep analysis” |
| `draft_service.py` | 331 | An on-demand AI bid draft (VG-10) — the service for the “📝 Bid draft” button |
| `bid_verdict_service.py` | 300 | An on-demand bid/no-bid verdict (VG-08) — the service for the Telegram button “🎯 Worth it?” |
| `source_choice.py` | 266 | ``/izvori`` — a subscriber chooses the profile’s sources in the bot (SRC-0b, R6) |
| `i18n_ai_render.py` | 248 | The chrome of the bot’s AI wrapper (deep analysis, verdict, checklist, draft, analytics, |
| `trial_service.py` | 246 | The trial access decision service for the Telegram bot (STAGE T) |
| `award_watch_service.py` | 234 | The winner subscription service (VG-06 E3b) — business logic for Telegram handlers |
| `status_service.py` | 227 | The tender status funnel service for the Telegram bot (a port of v6 tracker_bot) |
| `trial_handlers.py` | 211 | Trial access handlers for the Telegram bot (STAGE T) |
| `analytics_export.py` | 170 | VG-06 STAGE 3a — export of contracting-authority/competitor analytics to CSV |
| `tg_html.py` | 111 | Splitting a bot HTML reply into Telegram messages (the 4096-character limit) |
| `ai_gate.py` | 110 | A read-only gate reader of AI levels for Telegram handlers (#513) |
| `card_stash.py` | 81 | The stash of a collapsed tender card (“✖ Hide” → “↩ Restore”) |
| `sr_latin.py` | 78 | Serbian in the bot — Latin script only (owner 10.10.2026) |
| `config.py` | 75 | Bot configuration (pydantic-settings) |
| `__init__.py` | 6 | Messenger bots (ARCHITECTURE §9) |

### `src/db/` — 3 modules, 141 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `session.py` | 112 | A minimal async session layer of SQLAlchemy 2.0 |
| `base.py` | 17 | SQLAlchemy 2.0 declarative base for Tender Bot Serbia |
| `__init__.py` | 12 | Database layer for Tender Bot Serbia |

### `src/db/models/` — 18 modules, 1623 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `tenant.py` | 192 | Tenant and account models · tables: `tenants`, `users`, `user_channels`, `subscriptions`, `anti_abuse_identifiers` |
| `tender.py` | 161 | Two-tier tender models + canonical clustering · tables: `tenders_raw`, `canonical_tenders`, `tenders_normalized` |
| `profile.py` | 136 | Subscriber search-profile models · tables: `profiles`, `profile_revisions` |
| `workflow.py` | 126 | Workflow/retention: folders, membership, notes (VG-17, migration 0029) · tables: `tender_folders`, `folder_items`, `tender_notes` |
| `geo.py` | 104 | Foundational reference tables (multi-country foundations, ADR-033) · tables: `countries`, `classification_systems`, `classifications`, `regions` |
| `tender_status.py` | 97 | The user’s tender status funnel within a tenant (migration 0008) · tables: `tender_user_status` |
| `__init__.py` | 92 | ORM models for Tender Bot Serbia (DB_SCHEMA.md v1.1 + auth_tokens) |
| `ai.py` | 83 | AI pipeline models (ADR-024) · tables: `tender_ai_analyses`, `ai_usage` |
| `award_watch.py` | 81 | ORM models for winner alerts (VG-06 E3b, migration 0019) · tables: `award_watch_subscriptions`, `award_watch_sent` |
| `tender_award.py` | 77 | The TenderAward ORM model — tender award decisions (F03 eNabavke) · tables: `tender_awards` |
| `billing.py` | 76 | Billing models (VG-11.1, migration 0028) · tables: `billing_subscriptions`, `billing_webhook_events` |
| `delivery.py` | 69 | Delivery outbox models (outbox pattern, ADR-025) · tables: `delivery_outbox`, `delivery_attempts` |
| `template_library.py` | 66 | Template library model (EPIC 4 — a library of ready-made search templates) · tables: `template_library` |
| `reminder_log.py` | 60 | The log of sent deadline reminders (migration 0009) · tables: `reminder_log` |
| `channel_dead_notice_log.py` | 56 | The log of “delivery channel dropped” incidents (migration 0031) · tables: `channel_dead_notice_log` |
| `summary_log.py` | 55 | The log of sent daily summaries (migration 0010) · tables: `summary_log` |
| `auth.py` | 53 | One-time auth tokens of the cabinet (Sprint-19, +channel_bind since Chat 26) · tables: `auth_tokens` |
| `source.py` | 39 | Tender source runtime config · tables: `sources` |

### `src/dedup/` — 5 modules, 732 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `cross_source.py` | 312 | Scoring of the pair “the same procurement on two sources” (TED ↔ a source adapter (not published)) |
| `linker.py` | 255 | Linking a tender to a cross-source cluster at ingest (owner decision 01.10.2026) |
| `cluster_key.py` | 107 | compute_cluster_key — a deterministic cross-source dedup key |
| `also_on.py` | 56 | The mark “the same tender is also on other sources” — ONE predicate for all renders |
| `__init__.py` | 2 | Cross-source tender deduplication (ADR-022 + ADR-033, DB_SCHEMA §7) |

### `src/domain/` — 22 modules, 2157 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `text_norm.py` | 371 | A single contract for normalizing Serbian text for matching (research §6) |
| `sources.py` | 295 | The registry of source platforms — the ``sources`` table as the single list (SRC-0a) |
| `classifications.py` | 175 | A single dictionary of classification systems: tender side ↔ profile side |
| `ics.py` | 155 | iCalendar generator (RFC 5545) for "Add to calendar" — tender submission deadline |
| `access.py` | 142 | The SSOT predicate of a tenant’s access activity/freeze (FIX-SG-01) |
| `tz.py` | 127 | The project’s single time contract: in the DB and in logic — UTC ONLY |
| `repoll.py` | 104 | Re-polling of open tenders: periodicity and the per-pass ceiling |
| `indexing.py` | 79 | Which tender cards we give to search engines (sitemap + ``robots``) |
| `access_grants.py` | 78 | The single point of EXTENDING a tenant’s access by N days (migration 0041) |
| `regions.py` | 74 | The SSOT directory of Serbian districts (ISO 3166-2:RS, RS-00..RS-29) for the UI |
| `actuality.py` | 71 | A tender without a submission deadline is current for :data:`NO_DEADLINE_ACTUAL_DAYS` days |
| `description_render.py` | 70 | Parsing a tender description into “document heading + text” blocks |
| `profile_channels.py` | 61 | A channel bound AFTER a template was created — into templates without channels |
| `tiers.py` | 61 | The SSOT resolver of the effective tier for AI gates (a neutral domain layer) |
| `audit.py` | 57 | A neutral layer for writing to the immutable admin journal (``admin_audit_log``) |
| `delivery_cap.py` | 55 | The daily delivery limit: ONE place for the figure and the warning threshold |
| `live_tender.py` | 52 | The “live tender” predicate and the start of regular collection — ONE for the cabinet dashboard and the ``/statistika`` storefront |
| `source_reconcile.py` | 49 | Reconciliation with the source (M2 of monitoring): one “discrepancy” predicate and the alert text |
| `source_silence.py` | 35 | Source silence: hours of quiet and the “time to alert” decision (SRC-0d) |
| `seo_text.py` | 24 | Cleaning tender text for SEO surfaces (title/meta/JSON-LD) |
| `ids.py` | 20 | The canonical form of identifiers from URLs |
| `__init__.py` | 2 | The domain layer: pure business logic without DB/IO (FIX-SG-01) |

### `src/parsers/` — 6 modules, 1062 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `guards.py` | 531 | Source reliability guards: robots · HTTP class · silent zero · canary |
| `base.py` | 249 | SourceAdapter — the single interface of tender platforms (ADR-021) |
| `capabilities.py` | 177 | What the customer gets from a source — the adapter’s capability declaration |
| `registry.py` | 42 | Loading source adapters into the registry — an EXPLICIT list (SRC-0a) |
| `art27.py` | 36 | A shared dictionary of the VG-13 epic — “article 27 of the PPL” procurements (below the threshold) |
| `__init__.py` | 27 | Source adapters package (ADR-021) |

### `src/plans/` — 2 modules, 452 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `catalog.py` | 451 | SSOT catalog of public showcase plans (VG-12) |
| `__init__.py` | 1 | — |

### `src/workers/` — 22 modules, 11126 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `ingest.py` | 2014 | Ingest Worker — the pipeline entry: ``fetch_batch → tenders_raw → tenders_normalized`` |
| `matching.py` | 1313 | Two-stage tender matching against subscriber profiles |
| `delivery.py` | 1005 | Delivery Worker — sends tenders from ``delivery_outbox`` to channels |
| `award_watch_notifier.py` | 624 | Award Watch Notifier — push alerts about new wins of subscribed PIBs (VG-06 E3b) |
| `flow_observability.py` | 600 | Observability of the tender flow and card enrichment (REV-07, owner 20.09.2026) |
| `awards_ingest.py` | 540 | Awards Ingest Worker — filling tender_awards from F03 eNabavke (VG-05 STAGE 3) |
| `ai.py` | 513 | AI Worker — analysis of the terms of reference from ``delivery_outbox`` (the ``ai_pending → ai_done`` pipeline) |
| `subscription_reminders.py` | 504 | Subscription Reminder Worker — reminders about subscription expiry |
| `ai_spend_report.py` | 484 | AI Spend Report — a daily summary of Anthropic spend + an operator alert |
| `channel_dead_notify.py` | 479 | Channel Dead Notify Worker — an email to the subscriber about a dropped delivery channel |
| `summary.py` | 398 | Summary Worker — a daily summary of the subscriber’s funnel (a port of v6 daily_summary) |
| `billing_notifications.py` | 377 | Billing Notifications Worker — payment lifecycle emails (blocker #3) |
| `reminders.py` | 370 | Reminder Worker — reminders about an approaching deadline (a port of v6 reminders_loop) |
| `flow_report.py` | 321 | Flow Report — a daily “flow and enrichment” summary (REV-07, owner 20.09.2026) |
| `routing.py` | 318 | Routing Worker — a LISTEN/NOTIFY loop over the ``tender_changed`` channel |
| `health.py` | 267 | Worker observability: heartbeat (Healthchecks.io) + a Telegram alert (REV-05/06) |
| `source_reconcile.py` | 247 | A daily reconciliation of the number of notices with the source (M2, ``docs/development/SOURCE_MONITORING_2026-10-15.md``) |
| `news_autopost.py` | 246 | Auto-publishing ``/novosti`` news to a Telegram channel (owner decision 16.10.2026) |
| `alsu_collector.py` | 182 | Collecting ALSU notices into ``asset_sales`` (the “assets” direction, stage 1) |
| `plans_collector.py` | 158 | Collecting procurement plan items of a source adapter (not published) into ``procurement_plan_items`` (stage 1) |
| `keyword_expand.py` | 145 | Keyword expansion ru/en → Serbian word forms in Cyrillic (+ en forms for English-language sources) |
| `__init__.py` | 21 | Background workers of Tender Bot Serbia |

### `src/workers/adapters/` — 5 modules, 1627 lines

| Module | Lines | Purpose (docstring) |
| --- | ---: | --- |
| `base.py` | 663 | Channel adapter contract for delivery (ARCHITECTURE §15.1, ADR-025) |
| `email.py` | 453 | E-mail adapter for tender delivery (ARCHITECTURE §15.1, ADR-025) |
| `telegram.py` | 391 | Telegram adapter for delivery (aiogram 3, ARCHITECTURE §15, ADR-025) |
| `__init__.py` | 88 | Channel delivery adapters (ARCHITECTURE §15.1, ADR-025) |
| `viber.py` | 32 | The Viber delivery adapter — a STUB (ARCHITECTURE §15.1) |

## DB tables

There are THREE sources: models (`__tablename__`), migrations and one-off scripts. A table without a model exists only in SQL — look for its accesses with a predicate over `text(`, not through the ORM.

Checking the map against the LIVE database (the map is a hypothesis, the database is fact):
```
docker compose -f deploy/docker-compose.yml exec -T postgres bash -lc \
  'psql -U $POSTGRES_USER -d $POSTGRES_DB -tAc "select table_name from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1\"'
```

| Table | Model | Created in |
| --- | --- | --- |
| `admin_audit_log` | — (SQL only) | `alembic/versions/0030_admin_audit_log.py` |
| `ai_spend_alert_log` | — (SQL only) | `alembic/versions/0040_ai_spend_daily.py` |
| `ai_spend_daily` | — (SQL only) | `alembic/versions/0040_ai_spend_daily.py` |
| `ai_usage` | `src/db/models/ai.py` | `alembic/versions/0001_initial_schema.py` |
| `anti_abuse_identifiers` | `src/db/models/tenant.py` | `alembic/versions/0001_initial_schema.py` |
| `asset_sales` | — (SQL only) | `alembic/versions/0054_asset_sales.py` |
| `auth_tokens` | `src/db/models/auth.py` | `alembic/versions/0006_auth_tokens.py` |
| `award_watch_sent` | `src/db/models/award_watch.py` | `alembic/versions/0019_award_watch.py` |
| `award_watch_subscriptions` | `src/db/models/award_watch.py` | `alembic/versions/0019_award_watch.py` |
| `billing_notification_outbox` | — (SQL only) | `alembic/versions/0033_billing_notification_outbox.py` |
| `billing_subscriptions` | `src/db/models/billing.py` | `alembic/versions/0028_billing_subscriptions.py` |
| `billing_webhook_events` | `src/db/models/billing.py` | `alembic/versions/0028_billing_subscriptions.py` |
| `canonical_tenders` | `src/db/models/tender.py` | `alembic/versions/0001_initial_schema.py` |
| `channel_dead_notice_log` | `src/db/models/channel_dead_notice_log.py` | `alembic/versions/0031_channel_dead_notice_log.py` |
| `channel_posts` | — (SQL only) | `alembic/versions/0056_channel_posts.py` |
| `classification_systems` | `src/db/models/geo.py` | `alembic/versions/0001_initial_schema.py` |
| `classifications` | `src/db/models/geo.py` | `alembic/versions/0001_initial_schema.py` |
| `countries` | `src/db/models/geo.py` | `alembic/versions/0001_initial_schema.py` |
| `deadline_tz_backfill` | — (SQL only) | `scripts/backfill_deadline_tz.py` |
| `delivery_attempts` | `src/db/models/delivery.py` | `alembic/versions/0001_initial_schema.py` |
| `delivery_outbox` | `src/db/models/delivery.py` | `alembic/versions/0001_initial_schema.py` |
| `folder_items` | `src/db/models/workflow.py` | `alembic/versions/0029_workflow_folders_notes.py` |
| `procurement_plan_items` | — (SQL only) | `alembic/versions/0053_procurement_plan_items.py` |
| `profile_revisions` | `src/db/models/profile.py` | `alembic/versions/0001_initial_schema.py` |
| `profiles` | `src/db/models/profile.py` | `alembic/versions/0001_initial_schema.py` |
| `referrals` | — (SQL only) | `alembic/versions/0020_referrals.py` |
| `regions` | `src/db/models/geo.py` | `alembic/versions/0001_initial_schema.py` |
| `reminder_log` | `src/db/models/reminder_log.py` | `alembic/versions/0009_reminder_log.py` |
| `source_reconcile` | — (SQL only) | `alembic/versions/0055_source_reconcile.py` |
| `sources` | `src/db/models/source.py` | `alembic/versions/0001_initial_schema.py` |
| `subscription_reminder_log` | — (SQL only) | `alembic/versions/0017_subscription_reminder_log.py` |
| `subscriptions` | `src/db/models/tenant.py` | `alembic/versions/0001_initial_schema.py` |
| `summary_log` | `src/db/models/summary_log.py` | `alembic/versions/0010_summary_log.py` |
| `template_library` | `src/db/models/template_library.py` | `alembic/versions/0016_template_library.py` |
| `tenants` | `src/db/models/tenant.py` | `alembic/versions/0001_initial_schema.py` |
| `tender_ai_analyses` | `src/db/models/ai.py` | `alembic/versions/0001_initial_schema.py` |
| `tender_awards` | `src/db/models/tender_award.py` | `alembic/versions/0018_tender_awards.py` |
| `tender_folders` | `src/db/models/workflow.py` | `alembic/versions/0029_workflow_folders_notes.py` |
| `tender_notes` | `src/db/models/workflow.py` | `alembic/versions/0029_workflow_folders_notes.py` |
| `tender_user_status` | `src/db/models/tender_status.py` | `alembic/versions/0008_tender_user_status.py` |
| `tenders_normalized` | `src/db/models/tender.py` | `alembic/versions/0001_initial_schema.py` |
| `tenders_raw` | `src/db/models/tender.py` | `alembic/versions/0001_initial_schema.py` |
| `trial_requests` | — (SQL only) | `alembic/versions/0012_trial_access.py` |
| `user_channels` | `src/db/models/tenant.py` | `alembic/versions/0001_initial_schema.py` |
| `users` | `src/db/models/tenant.py` | `alembic/versions/0001_initial_schema.py` |

## Routes

**117 routes** in 21 modules. Path = the router’s `prefix` + the decorator path (`include_router` in `src/api/main.py` goes WITHOUT `prefix=`), i.e. the full URL.

| Method | Path | Address |
| --- | --- | --- |
| GET | `/` | `src/api/public/routes_public.py:190` |
| GET | `/.well-known/security.txt` | `src/api/public/routes_seo.py:360` |
| GET | `/admin` | `src/api/admin/routes_admin.py:95` |
| GET | `/admin/` | `src/api/admin/routes_admin.py:95` |
| GET | `/admin/audit` | `src/api/admin/routes_admin.py:441` |
| GET | `/admin/health` | `src/api/admin/routes_admin.py:357` |
| GET | `/admin/login` | `src/api/admin/routes_admin.py:70` |
| GET | `/admin/moderation` | `src/api/admin/routes_admin.py:555` |
| POST | `/admin/moderation/{template_id}/approve` | `src/api/admin/routes_admin.py:570` |
| POST | `/admin/moderation/{template_id}/edit` | `src/api/admin/routes_admin.py:602` |
| POST | `/admin/moderation/{template_id}/reject` | `src/api/admin/routes_admin.py:586` |
| GET | `/admin/requests` | `src/api/admin/routes_admin.py:469` |
| POST | `/admin/requests/{request_id}/approve` | `src/api/admin/routes_admin.py:498` |
| POST | `/admin/requests/{request_id}/reject` | `src/api/admin/routes_admin.py:529` |
| GET | `/admin/sources` | `src/api/admin/routes_admin.py:379` |
| POST | `/admin/sources/{source_key}/disable` | `src/api/admin/routes_admin.py:409` |
| POST | `/admin/sources/{source_key}/enable` | `src/api/admin/routes_admin.py:399` |
| GET | `/admin/templates` | `src/api/admin/routes_admin.py:342` |
| GET | `/admin/users` | `src/api/admin/routes_admin.py:109` |
| GET | `/admin/users/{uid}` | `src/api/admin/routes_admin.py:152` |
| POST | `/admin/users/{uid}/disable` | `src/api/admin/routes_admin.py:241` |
| POST | `/admin/users/{uid}/enable` | `src/api/admin/routes_admin.py:271` |
| POST | `/admin/users/{uid}/grant` | `src/api/admin/routes_admin.py:184` |
| POST | `/admin/users/{uid}/revoke` | `src/api/admin/routes_admin.py:217` |
| POST | `/admin/users/{uid}/set-tier` | `src/api/admin/routes_admin.py:313` |
| GET | `/app` | `src/api/cabinet/routes_profiles.py:338` |
| GET | `/app/analitika` | `src/api/cabinet/routes_analitika.py:38` |
| GET | `/app/analitika.xlsx` | `src/api/cabinet/routes_analitika.py:56` |
| GET | `/app/feed` | `src/api/cabinet/routes_profiles.py:623` |
| POST | `/app/folders` | `src/api/cabinet/routes_my_tenders.py:197` |
| POST | `/app/folders/{folder_id}/delete` | `src/api/cabinet/routes_my_tenders.py:211` |
| POST | `/app/izvori/skip` | `src/api/cabinet/routes_profiles.py:583` |
| POST | `/app/izvori/vklyuchit` | `src/api/cabinet/routes_profiles.py:541` |
| GET | `/app/my` | `src/api/cabinet/routes_my_tenders.py:84` |
| GET | `/app/my/export.csv` | `src/api/cabinet/routes_my_tenders.py:122` |
| GET | `/app/tenders/calendar.ics` | `src/api/cabinet/routes_my_tenders.py:153` |
| GET | `/app/tenders/{tender_id}` | `src/api/cabinet/routes_tender_ai.py:281` |
| GET | `/app/tenders/{tender_id}/analytics.csv` | `src/api/cabinet/routes_tender_ai.py:669` |
| GET | `/app/tenders/{tender_id}/description` | `src/api/cabinet/routes_tender_ai.py:396` |
| POST | `/app/tenders/{tender_id}/folders/{folder_id}/add` | `src/api/cabinet/routes_my_tenders.py:314` |
| POST | `/app/tenders/{tender_id}/folders/{folder_id}/remove` | `src/api/cabinet/routes_my_tenders.py:346` |
| POST | `/app/tenders/{tender_id}/generate/{level}` | `src/api/cabinet/routes_tender_ai.py:738` |
| POST | `/app/tenders/{tender_id}/note` | `src/api/cabinet/routes_my_tenders.py:289` |
| POST | `/app/tenders/{tender_id}/status` | `src/api/cabinet/routes_my_tenders.py:255` |
| GET | `/app/tenders/{tender_id}/status/{level}` | `src/api/cabinet/routes_tender_ai.py:837` |
| GET | `/app/tenders/{tender_id}/title` | `src/api/cabinet/routes_tender_ai.py:519` |
| POST | `/app/tenders/{tender_id}/watch` | `src/api/cabinet/routes_my_tenders.py:230` |
| GET | `/app/vesti` | `src/api/cabinet/routes_profiles.py:299` |
| GET | `/auth/magic` | `src/api/cabinet/routes_auth.py:436` |
| GET | `/auth/resend` | `src/api/cabinet/routes_auth.py:297` |
| POST | `/auth/resend` | `src/api/cabinet/routes_auth.py:304` |
| GET | `/auth/verify` | `src/api/cabinet/routes_auth.py:340` |
| POST | `/billing/checkout/{plan_key}` | `src/api/billing/routes_billing.py:63` |
| POST | `/billing/webhook/{provider}` | `src/api/billing/routes_billing.py:29` |
| GET | `/cabinet/account` | `src/api/cabinet/routes_account.py:212` |
| POST | `/cabinet/account/delete` | `src/api/cabinet/routes_account.py:292` |
| POST | `/cabinet/account/export` | `src/api/cabinet/routes_account.py:240` |
| GET | `/cabinet/email` | `src/api/cabinet/routes_channels.py:136` |
| GET | `/cabinet/email/confirm` | `src/api/cabinet/routes_channels.py:219` |
| POST | `/cabinet/email/link` | `src/api/cabinet/routes_channels.py:144` |
| POST | `/cabinet/email/resend` | `src/api/cabinet/routes_channels.py:185` |
| POST | `/cabinet/email/unlink` | `src/api/cabinet/routes_channels.py:268` |
| POST | `/cabinet/locale` | `src/api/cabinet/routes_locale.py:41` |
| GET | `/cabinet/narucilac` | `src/api/cabinet/routes_narucilac.py:47` |
| GET | `/cabinet/narucilac.csv` | `src/api/cabinet/routes_narucilac.py:80` |
| GET | `/cabinet/subscription` | `src/api/cabinet/routes_subscription.py:101` |
| GET | `/cabinet/telegram` | `src/api/cabinet/routes_channels.py:82` |
| POST | `/cabinet/telegram/link` | `src/api/cabinet/routes_channels.py:94` |
| POST | `/cabinet/trial/request` | `src/api/cabinet/routes_trial.py:27` |
| GET | `/cpv-kodovi` | `src/api/public/routes_geo.py:565` |
| GET | `/favicon.ico` | `src/api/public/routes_seo.py:372` |
| GET | `/healthz` | `src/api/main.py:200` |
| GET | `/help` | `src/api/public/routes_public.py:255` |
| GET | `/izvori` | `src/api/public/routes_public.py:301` |
| GET | `/kontakt` | `src/api/public/routes_contact.py:110` |
| POST | `/kontakt` | `src/api/public/routes_contact.py:130` |
| GET | `/legal/{name}` | `src/api/public/routes_public.py:390` |
| GET | `/library` | `src/api/cabinet/routes_library.py:126` |
| GET | `/library/{kind}/{key}` | `src/api/cabinet/routes_library.py:176` |
| GET | `/llms.txt` | `src/api/public/routes_seo.py:322` |
| GET | `/login` | `src/api/cabinet/routes_auth.py:385` |
| POST | `/login` | `src/api/cabinet/routes_auth.py:396` |
| GET | `/logout` | `src/api/cabinet/routes_auth.py:469` |
| POST | `/logout` | `src/api/cabinet/routes_auth.py:480` |
| GET | `/novosti` | `src/api/public/routes_news.py:36` |
| POST | `/novosti/procitano` | `src/api/public/routes_news.py:109` |
| GET | `/novosti/{slug}` | `src/api/public/routes_news.py:59` |
| GET | `/plans` | `src/api/public/routes_public.py:416` |
| POST | `/plans/upgrade` | `src/api/public/routes_public.py:472` |
| POST | `/profiles` | `src/api/cabinet/routes_profiles.py:1109` |
| GET | `/profiles/customer-suggest` | `src/api/cabinet/routes_profiles.py:821` |
| GET | `/profiles/new` | `src/api/cabinet/routes_profiles.py:874` |
| GET | `/profiles/new/confirm` | `src/api/cabinet/routes_profiles.py:1015` |
| GET | `/profiles/preview-count` | `src/api/cabinet/routes_profiles.py:745` |
| GET | `/profiles/{profile_id}` | `src/api/cabinet/routes_profiles.py:1226` |
| POST | `/profiles/{profile_id}` | `src/api/cabinet/routes_profiles.py:1356` |
| POST | `/profiles/{profile_id}/activate` | `src/api/cabinet/routes_profiles.py:1491` |
| GET | `/profiles/{profile_id}/created` | `src/api/cabinet/routes_profiles.py:1174` |
| POST | `/profiles/{profile_id}/deactivate` | `src/api/cabinet/routes_profiles.py:1466` |
| POST | `/profiles/{profile_id}/delete` | `src/api/cabinet/routes_profiles.py:1450` |
| GET | `/profiles/{profile_id}/edit` | `src/api/cabinet/routes_profiles.py:1320` |
| POST | `/profiles/{profile_id}/propose` | `src/api/cabinet/routes_profiles.py:1408` |
| POST | `/profiles/{profile_id}/sources/add` | `src/api/cabinet/routes_profiles.py:1275` |
| GET | `/robots.txt` | `src/api/public/routes_seo.py:122` |
| GET | `/security.txt` | `src/api/public/routes_seo.py:360` |
| GET | `/signup` | `src/api/cabinet/routes_auth.py:105` |
| POST | `/signup` | `src/api/cabinet/routes_auth.py:121` |
| POST | `/signup/reregister` | `src/api/cabinet/routes_auth.py:206` |
| GET | `/sitemap-pages.xml` | `src/api/public/routes_seo.py:193` |
| GET | `/sitemap-tenders-{n}.xml` | `src/api/public/routes_seo.py:218` |
| GET | `/sitemap.xml` | `src/api/public/routes_seo.py:146` |
| GET | `/statistika` | `src/api/public/routes_public.py:348` |
| GET | `/tender/{tender_id}` | `src/api/public/routes_tender.py:193` |
| GET | `/tender/{tender_id}.ics` | `src/api/public/routes_tender.py:153` |
| GET | `/tenderi` | `src/api/public/routes_geo.py:299` |
| GET | `/tenderi/cpv/{division}` | `src/api/public/routes_geo.py:461` |
| GET | `/tenderi/region/{code}` | `src/api/public/routes_geo.py:649` |

## Large files (≥25 KB) — edit surgically

| File | KB |
| --- | ---: |
| `src/api/cabinet/i18n.py` | 294 |
| `src/bots/telegram.py` | 155 |
| `src/api/public/i18n.py` | 155 |
| `src/parsers/jnportal/adapter.py` | 118 |
| `src/workers/ingest.py` | 107 |
| `src/api/cabinet/routes_profiles.py` | 81 |
| `src/workers/matching.py` | 71 |
| `src/bots/i18n.py` | 57 |
| `src/api/cabinet/feed_service.py` | 57 |
| `src/parsers/opstine_art27/adapter.py` | 55 |
| `src/api/admin/admin_service.py` | 55 |
| `src/workers/delivery.py` | 54 |
| `src/analytics/awards_analytics.py` | 50 |
| `src/api/cabinet/routes_tender_ai.py` | 48 |
| `src/parsers/ukck_nabavke/adapter.py` | 42 |
| `src/i18n_email.py` | 39 |
| `src/api/cabinet/workflow_service.py` | 39 |
| `src/api/public/legal_texts.py` | 36 |
| `src/api/cabinet/customer_analytics_service.py` | 36 |
| `src/api/cabinet/preview_service.py` | 35 |
| `src/workers/adapters/base.py` | 34 |
| `src/parsers/mod_nabavke/adapter.py` | 34 |
| `src/parsers/ted/adapter.py` | 33 |
| `src/parsers/nis/adapter.py` | 33 |
| `src/parsers/jnportal/codebooks.py` | 33 |
| `src/parsers/bih_enabavke/adapter.py` | 33 |
| `src/analytics/report_builder.py` | 33 |
| `src/workers/flow_observability.py` | 32 |
| `src/api/cabinet/channel_service.py` | 32 |
| `src/parsers/osce/adapter.py` | 31 |
| `src/i18n_common.py` | 31 |
| `src/api/templates/profile_form.html` | 30 |
| `src/ai/prompts.py` | 30 |
| `src/parsers/guards.py` | 29 |
| `src/api/public/routes_geo.py` | 28 |
| `src/api/cabinet/auth_service.py` | 28 |
| `src/workers/award_watch_notifier.py` | 27 |
| `src/workers/ai.py` | 27 |
| `src/parsers/plantaze/adapter.py` | 27 |
| `src/api/repo.py` | 27 |
| `src/ai/usage.py` | 27 |
| `src/workers/channel_dead_notify.py` | 26 |
| `src/api/deps.py` | 26 |
| `src/ai/translation.py` | 26 |
| `src/parsers/eufor/adapter.py` | 25 |
| `src/ai/client.py` | 25 |

## Templates

`src/api/templates/` — **99 files**, of which **52 partials** (`_*.html`).

‼️ A feature visible in several blocks is fixed with a predicate over ALL render points: `grep -rln "_<partial>.html" src/api/templates/`.

“—” = no inclusion and no render from code: a candidate for a dead file, check before editing.

| Partial | Included in | Rendered from code |
| --- | --- | --- |
| `_access_badge.html` | `_cabinet_nav.html`, `account.html`, `analitika.html`, `base.html`, `narucilac.html` | — |
| `_agenda_item.html` | `_agenda_list.html` | — |
| `_agenda_list.html` | `_rokovi.html` | — |
| `_ai_artifact.html` | `_ai_slot.html`, `tender_detail.html` | — |
| `_ai_slot.html` | `tender_detail.html` | — |
| `_also_on.html` | `_cockpit_feed_teaser.html`, `_feed_card.html`, `tender_detail.html` | — |
| `_cabinet_nav.html` | `base.html`, `public_base.html` | — |
| `_calendar.html` | `dashboard.html` | — |
| `_card_title.html` | `_agenda_item.html`, `_cockpit_feed_teaser.html`, `_feed_card.html`, `_my_tender_row.html`, `tender_detail.html` | — |
| `_cockpit_feed_teaser.html` | `dashboard.html` | — |
| `_cockpit_hero.html` | `dashboard.html` | — |
| `_cockpit_plural.html` | — | — |
| `_competition.html` | `tender_detail.html` | — |
| `_competitor_analytics.html` | `tender_detail.html` | — |
| `_contact_form.html` | `account.html`, `contact.html`, `help.html` | — |
| `_customer_analytics.html` | `tender_detail.html` | — |
| `_customer_incumbent.html` | `tender_detail.html` | — |
| `_customer_profile_body.html` | `_customer_analytics.html`, `narucilac.html` | — |
| `_dash_alert.html` | `dashboard.html` | — |
| `_dash_sub_strip.html` | `dashboard.html` | — |
| `_delivery_warning.html` | `account.html`, `feed_archive.html`, `profile_created.html`, `profile_detail.html` | — |
| `_empty_state.html` | `_cockpit_feed_teaser.html`, `_competitor_analytics.html`, `_customer_analytics.html`, `_reprocurement_forecast.html`, `admin_audit.html`, `admin_health.html`, `admin_moderation.html`, `admin_requests.html`, `admin_sources.html`, `admin_templates.html`, `admin_users.html`, `analitika.html`, `dashboard.html`, `feed_archive.html`, `izvori.html`, `library.html`, `my_tenders.html`, `narucilac.html`, `statistika.html`, `tender_detail.html` | — |
| `_feed_card.html` | `feed_archive.html` | — |
| `_feed_filters.html` | `feed_archive.html` | — |
| `_flash_limit.html` | `_ai_slot.html` | — |
| `_funnel_board.html` | `my_tenders.html` | — |
| `_funnel_column.html` | `_funnel_board.html` | — |
| `_geo_tender_row.html` | `landing.html`, `tenderi_hub.html`, `tenderi_section.html` | — |
| `_hint_tip.html` | `_calendar.html`, `_cockpit_feed_teaser.html`, `_cockpit_hero.html`, `_rokovi.html`, `dashboard.html`, `library.html`, `profile_form.html`, `tender_detail.html` | — |
| `_kw_forms.html` | `_preview_count.html` | `src/api/cabinet/routes_profiles.py:801` |
| `_my_tender_row.html` | `_funnel_column.html`, `my_tenders.html` | — |
| `_onboarding_steps.html` | `dashboard.html` | — |
| `_open_now.html` | `profile_created.html`, `profile_detail.html` | — |
| `_pager.html` | `feed_archive.html`, `narucilac.html`, `tenderi_hub.html`, `tenderi_section.html` | — |
| `_portal_filters.html` | `tenderi_hub.html`, `tenderi_section.html` | — |
| `_preview_count.html` | — | — |
| `_preview_examples.html` | `_preview_count.html`, `library_detail.html`, `profile_created.html` | — |
| `_profile_sources.html` | `profile_detail.html` | — |
| `_region_hidden_banner.html` | `dashboard.html`, `feed_archive.html` | — |
| `_reprocurement_forecast.html` | `tender_detail.html` | — |
| `_rokovi.html` | `dashboard.html` | — |
| `_source_banner.html` | `dashboard.html` | — |
| `_source_banner_done.html` | — | — |
| `_source_checks.html` | `library_detail.html`, `profile_confirm.html` | — |
| `_sub_ai_limits.html` | `account.html` | — |
| `_sub_details.html` | `account.html` | — |
| `_sub_plan.html` | `account.html` | — |
| `_tender_actions.html` | `tender_detail.html` | — |
| `_tender_description.html` | `tender_detail.html` | — |
| `_tg_channel.html` | `account.html`, `help.html` | — |
| `_tpl_cards.html` | `library.html`, `library_detail.html`, `profile_start.html` | — |
| `_vesti.html` | `dashboard.html` | — |

## Migrations

`alembic/versions/` — **56 files**. The latest: `0054_asset_sales.py`, `0055_source_reconcile.py`, `0056_channel_posts.py`

‼️ Check the number against `origin/main` AND open `claude/*` branches before pushing: two branches with one number give TWO heads and a failed rollout in production. `alembic heads` — only in the development clone.

## Tests

| Directory | Files | What it covers |
| --- | ---: | --- |
| `tests/(root)` | 14 |  |
| `tests/ai` | 13 |  |
| `tests/analytics` | 5 |  |
| `tests/api` | 188 | routes, templates, render i18n |
| `tests/billing` | 8 |  |
| `tests/bots` | 40 |  |
| `tests/db` | 70 | the RLS perimeter, the FORCE RLS invariant |
| `tests/dedup` | 4 |  |
| `tests/domain` | 9 |  |
| `tests/infra` | 1 |  |
| `tests/parsers` | 61 |  |
| `tests/plans` | 3 |  |
| `tests/quality` | 8 | the mypy ratchet, coverage of CSS classes by templates |
| `tests/scripts` | 16 |  |
| `tests/workers` | 55 | delivery, reminders, ingest |

‼️ In the second clone a run is INCOMPLETE (21 routes instead of hundreds, `alembic` is not importable) — the deciding `gate.sh` only in the development clone.

---

Code invariants are enforced by the tests.
