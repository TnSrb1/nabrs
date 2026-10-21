# Architecture

This document describes the high-level architecture of TenderSRB. If you want to
find your way around the code, start here; for diagrams see
[`docs/diagrams/`](diagrams/) and the [animated overview](assets/architecture-animated.svg),
for the data model see [`DB_SCHEMA.md`](DB_SCHEMA.md), and for the reasoning behind
individual choices see the [decision records](DECISIONS.md).

Every path below is a link into this snapshot. An invariant that points at no test
is not listed.

## Bird's Eye View

TenderSRB turns public procurement notices into personal alerts. On the highest level
it is a pipeline: notices are collected from portals, normalised into one schema,
matched against each subscriber's templates, optionally explained by an AI model, and
delivered to Telegram or email.

```text
portal --> ingest --> tenders_raw --> tenders_normalized --> routing --> delivery_outbox --> ai --> delivery --> Telegram / email
                                              |
                                              +--> web cabinet and public catalogue (read side)
```

Three decisions shape everything else:

- **Workers share nothing but PostgreSQL.** Each stage (`ingest`, `routing`, `ai`,
  `delivery`, `reminders`, ...) is its own process; the hand-off between stages is a
  row in a table (the outbox pattern, [ADR-025](DECISIONS.md)). A worker can crash
  and restart, and the row is still there.
- **Tenant isolation lives in the database.** The API and the bot connect as a role that
  is not the table owner, and every tenant-scoped table has a row-level security policy
  that applies to that role. Application code cannot forget a `WHERE tenant_id = ...`
  and leak data, because the database refuses to return the rows.
- **Paid and irreversible things are gated in one place each.** A model call costs money
  and a notification cannot be unsent, so both go through a single checkpoint
  (the AI usage ledger and the delivery outbox) instead of being decided at each call site.

The product has two data axes that are easy to confuse: the **interface language** of a
user (`users.locale`) and the **content language** of AI output. They are separate
columns and separate catalogues. A third axis, the *plan* a customer pays for, is split
three ways on purpose: what is sold (`src/plans/catalog.py`), what is metered
(`_TIER_LIMITS`) and which model answers (`LEVEL_TIER`).

## Code Map

This section talks about the most important directories and data structures. Note that
it is a map, not a listing: use [`CODE_MAP.md`](CODE_MAP.md) to find an exact route,
table or module.

### [`src/parsers/`](../src/parsers/)

The boundary between the outside world and the system. A source adapter turns one
portal's notices into the shared `NormalizedTender` shape. [`base.py`](../src/parsers/base.py)
is the contract, [`registry.py`](../src/parsers/registry.py) the list of adapters,
[`guards.py`](../src/parsers/guards.py) the shared safety rules (hosts, secrets) and
[`example/`](../src/parsers/example/) a runnable adapter over a local file. The adapters
for real portals are **not** part of this snapshot.

An adapter returns data; persistence, deduplication and change detection belong to the
ingest worker.

### [`src/workers/`](../src/workers/)

One module per process. [`ingest.py`](../src/workers/ingest.py) stores raw and normalised
tenders and detects changes; [`routing.py`](../src/workers/routing.py) and
[`matching.py`](../src/workers/matching.py) decide who gets which tender;
[`ai.py`](../src/workers/ai.py) enriches an outbox row with a verdict;
[`delivery.py`](../src/workers/delivery.py) sends it. Reminders, award notifications and
reports are the same shape: read a table, do one thing, write a table.

As deployed, the ingest, routing and delivery workers connect as a database superuser,
which bypasses row-level security; such a worker scopes the tenant with an explicit
`WHERE`, as [`LIVE_CHANNELS_SQL`](../src/workers/matching.py) does.

### [`src/ai/`](../src/ai/)

The Claude client, prompts, the tier table and the usage ledger.
[`usage.py`](../src/ai/usage.py) is the only place that knows how many AI actions a
plan includes and how many were spent. [`stepwise.py`](../src/ai/stepwise.py) is the
step-by-step analysis of long documentation, described in [`AI_PIPELINE.md`](AI_PIPELINE.md).
[`prompts.py`](../src/ai/prompts.py) is behavioural code: a prompt change changes product
behaviour and is reviewed like any other logic.

### [`src/domain/`](../src/domain/)

Pure logic with no database and no network: text normalisation, time zones, access
periods, plan tiers, source rules. This is the cheapest place to test and the place where
a rule should live if two layers need it.

Today `src/domain` imports nothing from `src/api`, `src/bots` or `src/workers`; no test
enforces it, so it is a convention, not an invariant.

### [`src/db/`](../src/db/) and [`alembic/`](../alembic/)

SQLAlchemy models, the session factory with its tenant helper, and the migrations.
[`session.py`](../src/db/session.py) holds `set_current_tenant`, which sets the
transaction-local tenant used by the row-level security policies.

### [`src/api/`](../src/api/)

The FastAPI application: the public site (`public/`), the subscriber cabinet
(`cabinet/`), the operator admin (`admin/`), authentication and templates.
[`repo.py`](../src/api/repo.py) is the query layer shared by routes.

### [`src/bots/`](../src/bots/)

The Telegram bot. It connects with the same restricted role as the API.

### [`src/billing/`](../src/billing/) and [`src/plans/`](../src/plans/)

Payment gateways behind one interface (Stripe, Paddle) and the plan catalogue.
Billing is implemented and not yet live.

### [`tests/`](../tests/)

Unit tests, API tests and database tests. Database tests run against a throwaway
PostgreSQL instance. [`tests/quality/`](../tests/quality/) holds *guards*: tests that
scan the code for a rule (for example, every refusal is visible) instead of testing one
function. A guard for the bot's SQL lives in
[`tests/db/test_bots_no_raw_rls_sql.py`](../tests/db/test_bots_no_raw_rls_sql.py).

## Invariants

The rules below are the ones that cost real incidents or real money when they were
broken. Each names the code that holds it and the test that fails if it is broken. The
longer write-up of the first three groups is in [`docs/rules/`](rules/).

### Tenancy

**Architecture Invariant:** every tenant-scoped table has a policy, `ENABLE` **and**
`FORCE` row-level security. `ENABLE` alone does not apply to the table owner, so a
script running as owner would read every tenant silently. The check reads the catalogue
of migrations, not behaviour, because behaviour under the application role looks the
same with or without `FORCE`.
Held by [`tests/db/test_rls_force_invariant.py`](../tests/db/test_rls_force_invariant.py)
(`test_every_tenant_isolated_table_is_force_rls`) and
[`tests/db/test_rls_isolation.py`](../tests/db/test_rls_isolation.py).

**Architecture Invariant:** authenticated code calls `set_tenant` as the **first**
statement of a transaction. Without it a query returns zero rows (fail closed), never
another tenant's rows.
Held by [`src/api/repo.py`](../src/api/repo.py) (`set_tenant`) over
[`src/db/session.py`](../src/db/session.py) (`set_current_tenant`, transaction-local),
and by [`tests/db/test_rls_isolation.py`](../tests/db/test_rls_isolation.py).

**Architecture Invariant:** an operation across tenants (an operator deciding on
someone else's account) goes through a `SECURITY DEFINER` function, never through raw SQL
in the bot or the web layer.
Held by [`tests/db/test_bots_no_raw_rls_sql.py`](../tests/db/test_bots_no_raw_rls_sql.py)
(`test_no_raw_cross_tenant_sql_in_bots`).

### Money

**Architecture Invariant:** a paid model call is three things: a level in `_TIER_LIMITS`,
a gate that runs **before** the call, and a record in `ai_usage` **after** it.
A new call site missing any of the three is a bug.
Held by [`src/ai/usage.py`](../src/ai/usage.py) (`_TIER_LIMITS`, `limit_decision`,
`record_usage`, `finalize_with_insert`) and
[`tests/ai/test_usage_limit_decision.py`](../tests/ai/test_usage_limit_decision.py).

**Architecture Invariant:** automatic analysis spends money without a human click, so it
is gated by **subscription status** as well as by plan: an active subscription uses the
monthly plan limit, a trial uses a fixed demo allowance, anything else is refused. The
gate sits in the worker, not in the form, because a form is not the last line of defence.
Held by [`auto_limit_decision`](../src/ai/usage.py), called from
[`src/workers/ai.py`](../src/workers/ai.py), and
[`tests/ai/test_usage_auto_limit_decision.py`](../tests/ai/test_usage_auto_limit_decision.py).

**Architecture Invariant:** a refusal caused by a limit is visible: the remaining amount
before, a flash at the moment of refusal, a state on the button after. "It silently
turned off" is a defect, not a saving.
Held by [`tests/quality/test_limit_refusal_is_visible.py`](../tests/quality/test_limit_refusal_is_visible.py),
which collects every refusal point by predicate and requires a visible path for each.

**Architecture Invariant:** granting access for N days is monotonic. The new expiry is
`max(current expiry, now) + N`; shortening access is a separate, explicit operation.
The Python form is `extend_until` in [`src/domain/access.py`](../src/domain/access.py),
called from the admin service ([`src/api/admin/admin_service.py`](../src/api/admin/admin_service.py)).
The SQL form is the function `app_admin_extend_access` (migration 0041), which the
application calls through [`src/domain/access_grants.py`](../src/domain/access_grants.py)
(`extend_access`); the same expression is copied into the referral bonus function
(migration 0023). The copy in 0041 mirrors `extend_until_sql` in `access.py` and is not
imported, because migrations are frozen snapshots.
Held by [`tests/test_access.py`](../tests/test_access.py) (`test_extend_until_base_is_the_later_point`)
and [`tests/db/test_0041_admin_extend_access.py`](../tests/db/test_0041_admin_extend_access.py),
which fails when the two sides diverge.

### Matching and delivery

**Architecture Invariant:** text is folded by **one** module for ingest and for search.
A keyword in Cyrillic and a title in Latin script must meet in the same form, or the
match is silently empty.
Held by [`src/domain/text_norm.py`](../src/domain/text_norm.py) (`fold`, `normalize_text`),
used by [`src/workers/matching.py`](../src/workers/matching.py) and
[`src/api/public/portal_filters.py`](../src/api/public/portal_filters.py), and pinned by
[`tests/workers/test_keywords_match_golden.py`](../tests/workers/test_keywords_match_golden.py).

**Architecture Invariant:** nothing is queued for a channel that cannot receive it. A
channel is live only if it is active and has a non-empty address; a blocked or revoked
channel gets no row in the outbox.
Held by the predicate `ChannelState.is_live` in [`src/workers/matching.py`](../src/workers/matching.py)
(`LIVE_CHANNELS_SQL` there is the SQL selection that mirrors it) and
[`tests/workers/test_routing.py`](../tests/workers/test_routing.py)
(`test_route_tender_matches_profile_but_skips_outbox_for_dead_channel`).

**Architecture Invariant:** every moment in time is stored and compared in UTC.
Conversion to local time happens at the edge of presentation, and the reverse conversion
only at ingest, where a portal gives wall-clock time without an offset.
Held by [`src/domain/tz.py`](../src/domain/tz.py) and
[`tests/domain/test_tz.py`](../tests/domain/test_tz.py).

**Architecture Invariant:** a delivery that cannot be attempted yet is retried, not
dropped: an email row without a configured transport stays in the outbox.
Held by [`src/workers/delivery.py`](../src/workers/delivery.py) and
[`tests/workers/test_delivery_e2e.py`](../tests/workers/test_delivery_e2e.py)
(`test_email_without_transport_is_retryable_not_lost`).

## Cross-Cutting Concerns

### Testing

Database tests need a PostgreSQL instance; they are skipped without
`TEST_RLS_DATABASE_URL`. A test that runs as a database superuser does not prove
isolation, so isolation tests run under the application role. A new test is
mutation-checked: remove the fix, the test must fail.

### Errors and PII

Secrets that sit inside a URL (a bot token in the path, an API token in the query string)
are masked in the log by a pattern, not by a list of known values
([`src/log_secrets.py`](../src/log_secrets.py),
[`tests/test_log_secrets.py`](../tests/test_log_secrets.py)). Personal data is not logged;
only the exception type is.

### Languages

Interface strings live in dictionary catalogues, one per area, with a test for key
parity across languages ([`src/i18n_common.py`](../src/i18n_common.py),
[`src/i18n_email.py`](../src/i18n_email.py)). A channel is localised as a whole:
half a message in one language is worse than none.
