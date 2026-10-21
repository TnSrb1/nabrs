# DECISIONS.md — The log of technical decisions

Format: each decision is recorded as an ADR (Architectural Decision Record).

---

## ADR-001: Multi-tenant architecture — one shared bot + profiles

**Date:** 2026-05-01
**Status:** accepted

**Context:** up to 50+ subscribers with individual filtering rules.

**Decision:** one Telegram + one Viber bot, a DB with a `profiles(subscriber_id, ...)` table, the routing engine matches each new tender against the profiles.

**Alternatives rejected:**
- A bot per subscriber — impossible to maintain 50 tokens and webhooks
- A bot per source — the filters are individual anyway, poor UX

---

## ADR-002: PostgreSQL instead of SQLite

**Date:** 2026-05-01
**Status:** accepted

**Context:** v6 used SQLite + ATTACH DATABASE.

**Decision:** PostgreSQL 16 as the only database.

**Rationale:**
- Row-Level Security for isolating subscriber data
- JSONB for flexible profiles and AI results
- Full-text search with Serbian support (unaccent + simple)
- pg_trgm for deduplication
- LISTEN/NOTIFY for real-time updates in the cabinet
- Parallel writers (on SQLite in multi-tenant — a writer lock)

---

## ADR-003: Paddle (MoR) at the start, CorvusPay in phase 2

**Date:** 2026-05-01
**Status:** accepted

**Decision:** Paddle as the starting payment provider, CorvusPay through a DOO added once there are 15+ paying subscribers.

**Rationale for Paddle:**
- Merchant of Record — takes on EU/UK/RS VAT
- Launch in 1 day
- 5% + $0.50 per transaction (acceptable at low MRR)

**Rationale for adding CorvusPay later:**
- A local fee of 1.5–2.5% versus Paddle’s 5%
- A saving of €25–125/month as MRR grows
- Onboarding takes 2–4 weeks + PCI DSS SAQ-A

**Architectural consequence:** a `PaymentProvider` interface with two implementations.

---

## ADR-004: Delivery channels — Viber + Telegram + Email

**Date:** 2026-05-01
**Status:** accepted

**Decision:** the MVP includes Viber, Telegram, an Email digest and the web cabinet. WhatsApp is postponed to phase 4.

**Rationale:** Viber is the most popular messenger in Serbia. Telegram covers Russian residents and the IT audience. Email is a universal B2B channel. WhatsApp is more expensive and requires a verified Business Account, we will connect it on explicit demand from ≥10 subscribers.

---

## ADR-005: Docker Compose instead of native systemd

**Date:** 2026-05-01
**Status:** accepted

**Decision:** all components in Docker containers, orchestration via Docker Compose v2.

**Rationale:**
- Reproducible infrastructure (the Compose file = the whole environment in Git)
- A single shared log stream
- Healthchecks out of the box
- A simple rollback via `docker compose up <old-tag>`

---

## ADR-006: Stack — Python 3.12 + aiogram 3 + FastAPI

**Date:** 2026-05-01
**Status:** accepted

**Decision:** Python 3.12, aiogram 3 for Telegram, FastAPI for everything else (webhooks, the cabinet REST API, the Paddle webhook).

**Rationale:** reuse of 60% of the v6 code, a single async stack, one entry point for all webhooks.

---

## ADR-007: Web cabinet — FastAPI + HTMX + Jinja2 + Tailwind

**Date:** 2026-05-01
**Status:** accepted

**Decision:** at MVP we do not build an SPA on React/Vue, we use HTMX + Jinja2 + Tailwind.

**Rationale:** two developers without a dedicated frontend engineer; HTMX covers 90% of the cabinet’s needs (forms, lists, filters). In phase 4, if necessary, we migrate to Next.js.

---

## ADR-008: AI stack — Claude API (3 models)

**Date:** 2026-05-01
**Status:** accepted; extended by ADR-029 (Chat 10)

**Decision:** Anthropic Claude as the only AI provider.
- **Haiku 4.5** — fast classification, deduplication
- **Sonnet 4.6** — the standard “terms-of-reference analysis” (all paid plans)
- **Opus 4.7** — the “Deep analysis” (Pro and Business)

**Reserve:** Gemini 2.5 / GPT-5 as a fallback in case of price increases or blocks.

**Rationale:** the v6 experience, a stable API, prompt caching, embeddings from OpenAI separately (cheaper).

**Extension in Chat 10:** ADR-029 introduced tier naming (`cheap`/`balanced`/`premium`) in the architecture. The concrete slugs listed above (Haiku 4.5 / Sonnet 4.6 / Opus 4.7) are a historical record as of Chat 1. The current `tier→slug` mapping lives in `infra/config.yaml`. The `tender_ai_analyses.model` field in the DB stores the slug at the time of the call for audit.

---

## ADR-009: removed from the public snapshot

## ADR-010: SSH-only authentication for git operations on the VPS

**Date:** 2026-05-06
**Status:** accepted in Chat 4

**Context:** at the first `git clone` on the VPS a PAT was accidentally entered in the Username field instead of an empty string — the token ended up in the terminal, a screenshot and the chat context. The token was revoked immediately.

**Decision:** the git remote was switched to SSH (`git@github.com:<owner>/<repo>.git`). All PATs (classic and fine-grained) were revoked. Only the SSH key (`~/.ssh/<ssh-key>`) authorizes git operations from the VPS.

**Alternatives rejected:**
- HTTPS + a PAT in an environment variable — the PAT is still stored in plaintext and ends up in `env`, ps aux, logs
- HTTPS + a git credential helper — complicates Claude Code’s autonomous work

**Consequences:** on the production VPS the same scheme from scratch. We do not create a PAT at all.

---

## ADR-011: removed from the public snapshot

## ADR-012: Visibility and archive of tenders — a hybrid “by status + 30 days + a plan gate”

**Date:** 2026-05-08
**Status:** accepted in Chat 5

**Context:** owner raised two related questions:
1. Do not clutter visibility with closed tenders, yet keep the historical base.
2. The possibility of monetising the archive (paid access to history, analytics).

The alternative “delete tenders after N months” was rejected: the historical dataset is the project’s competitive asset (in a year — 40k tenders that competitors cannot reproduce in a day).

**Decision — two levels:**

### Level 1. Default visibility (for everyone, including Free):
- **All active tenders** (`deadline >= NOW()`) — shown.
- **Closed within the last 30 days** (`deadline >= NOW() - 30d`) — shown with a “closed” note.
- **Closed older than 30 days** — NOT shown by default.

### Level 2. The archive gate by plan:
| Plan | Archive access |
|---|---|
| Free | only active + 30 days of closed |
| Starter (€29) | the same |
| Pro (€69) | + the full archive of the **current calendar year** |
| Business (€149) | + the full history (5+ years) + basic analytics by buyer/CPV |
| Enterprise (from €399) | + extended analytics + an API to historical data + export |

### Level 3. History is never deleted.
Each day of parsing is +110 tenders in the permanent archive. This is the core of the future SKUs “Tender Analytics” and “Tender Report on-demand” (see Q-15 → the marketing document).

**Technical consequences:**
- The `is_archived` field (computed: `deadline < NOW() - 30d`) — indexed for fast filters in the routing engine.
- Partitioning `tenders` by `date_published` (RANGE) — postponed to phase 4 (~3 years of data; partitions are not needed before that).
- Cold partitions in a read-only tablespace — also phase 4+.

**Alternatives rejected:**
- A hard TTL “delete after 6 months” — we lose the asset, it has no value from the subscription point of view.
- One threshold for all plans — we miss the monetisation of archive analytics.

---

## ADR-013: Testing strategy — a pytest stack without AI QA agents

**Date:** 2026-05-08
**Status:** accepted in Chat 5

**Context:** owner asked whether external AI agents / a separate “second Claude as QA” / commercial AI testing services are needed.

**Decision:** for testing we use the standard deterministic pytest stack. No external AI agents / AI QA bots / AI test generators. Tests are written once as ordinary deterministic code and run a million times for free — this is not an area where AI adds confidence.

### The stack (all free):

| Layer | Tool | Purpose |
|---|---|---|
| Unit | `pytest`, `pytest-asyncio`, `pytest-mock` | Isolated logic (functions, classes) |
| Integration | `testcontainers-python` | Real Postgres + Redis in Docker for tests (no mocks) |
| API | `httpx` + FastAPI `TestClient` | REST endpoints, webhooks |
| External APIs | `vcrpy` or `pytest-recording` | Record real Anthropic/Paddle/Postmark responses **once**, then replay without a network |
| Property-based | `hypothesis` | CPV parsing, price calculation, deduplication — critical logic |
| Coverage | `pytest-cov` + the Codecov free tier | Coverage visibility in every PR |
| Security (code) | `bandit` | Static analysis of Python for vulnerabilities |
| Security (deps) | `pip-audit` | CVEs in dependencies |
| Smoke (after deploy) | `curl` + a manual checklist | Staging → continuing is possible only if everything is green |

### Target coverage (the CI gate):

- **Billing (Paddle webhook, plan upgrade), the routing engine, parsers:** ≥85% — money and data reliability.
- **API endpoints, DB models:** ≥70%.
- **Telegram/Viber handlers:** ≥50% (the UX part is tested by hand).
- **The web cabinet (HTMX views):** ≥40% — verified by eye.
- **The general gate for merging into main:** **<70% total coverage = merge forbidden.**

### What Claude does NOT test automatically (only owner/partner by hand):

- The UX of the web cabinet and the mobile Telegram UI.
- Production texts in Serbian — only a native Serbian speaker (see Sprint-23).
- Legal texts (Iubenda).
- Production deploy and confirmation that it works.

### Rejected:

- ❌ A second Claude / GPT for code review — expensive, no better than human review.
- ❌ Mutahunter, Codium, AI test generators — an extra layer, hides bugs in tests.
- ❌ SonarCloud / Snyk commercial tiers — overkill for the MVP, free is enough.
- ❌ Mutation testing (`mutmut`, `cosmic-ray`) — postpone to phase 2, expensive in time.

**Consequences:**
- In Sprint-11 (CI/CD) → ci.yml: ruff + black + mypy + pytest + pytest-cov with a 70% coverage gate.
- In every PR from a `claude/*` branch — green CI is mandatory as a condition of merge.
- In Sprint-13+ (phase 1, real code) — every PR from Claude contains at least one new test for the new logic.

---

## ADR-014: Anti-loop guardrails for Claude Code

**Date:** 2026-05-08
**Status:** accepted in Chat 5

**Context:** owner raised the risk of “loops” in which Claude Code burns tokens without progress. Typical scenarios: read-edit-read-edit, test-fix-test-fix, search-rephrase-search, a doc rabbit hole, a lint-fix cascade.

**Decision:** introduce 7 hard behavior rules for Claude Code on the staging VPS. The rules are recorded in three places:
1. **the project rules** in the repo root (≤200 lines) — Claude Code sees it at every session.
2. **`.claude/settings.json`** — machine-readable limits and deny rules (Sprint-9.1).
3. **`docs/archive/AUTONOMY_PLAN.md`** — a human-readable methodology for owner/partner.

### The 7 rules:

1. **Plan-first mandatory.** Any task longer than 50 lines of code or with >2 files — first a plan via Claude Code’s `/plan` mode, then execute. The plan is committed in the PR description.

2. **Hard stop conditions:**
   - A test does not pass after **3 fix attempts** → STOP, push a WIP branch, write to the TG dev channel “owner needed”.
   - **>10 read calls** in one task without a write → STOP, rethink.
   - A file is edited **>5 times in a row** → STOP, most likely the architecture is wrong.
   - **>5 search calls** (`Grep`, `codebase_search`) with rephrasing for a single task → STOP.

3. **Token budget per session.** In `.claude/settings.json` — a soft warning at 80% of the Pro plan limit used. When <20% is left — **a ban on starting new tasks**, only finalise the current one.

4. **TODO-list discipline.** Claude Code keeps an explicit TODO in `.claude/TODO.md`. If the TODO grows faster than `[x]` marks appear in 30 minutes — a looping signal, we stop and write to the TG dev channel.

5. **Atomic commits + branch isolation.** Each task → its own branch `claude/<task-id>`. If it went into a loop — `git checkout main && git branch -D claude/<task-id>`. Only this session’s tokens are lost, not code.

6. **Local pre-commit hooks** (introduced in Sprint-7 or Sprint-11):
   - `ruff check --fix` + `black` + `mypy` + `gitleaks`.
   - If it is red locally — Claude does not push, it fixes. **Never** “let CI sort it out”.

7. **Fresh context per atomic task.** Long sessions = bad. 5 short chats of 30 minutes with a clear single goal are better than one multi-hour marathon. Between tasks — `/compact` or a new chat.

### Control on the owner’s side:

- The **Telegram dev channel** already sends commit notifications — owner sees Claude Code’s activity in real time.
- **Branch protection on `main`** (Sprint-9.1) — Claude cannot merge into main by itself.
- **Daily diff review** — once a day owner looks at `git log --since=yesterday claude/*` — 5 minutes.
- **Cost monitoring** — Anthropic Console → a spending limit of $50/month. Overspend = a hard stop.

### Where to wire it in technically (Sprint-9.1):

Limits and hooks are added to `.claude/settings.json`. An example:

```json
{
  "limits": {
    "maxToolCallsPerTask": 30,
    "maxReadCallsBeforeWrite": 10,
    "maxFileEditsConsecutive": 5,
    "maxSearchRetries": 5,
    "maxTestFixAttempts": 3
  },
  "stopConditions": {
    "onTodoStagnation": "stop_and_notify",
    "onLimitExceeded": "stop_and_notify"
  },
  "notify": {
    "channel": "telegram",
    "events": ["stop_triggered", "session_end", "test_failure_3x"]
  }
}
```

(The exact syntax depends on the Claude Code version — to be clarified in Sprint-9.1.)

**Alternatives rejected:**
- “Trust the model to self-regulate” — practice shows that looping is a real and regular problem, without hard limits the model goes in circles.
- “A hard token limit per chat” from Anthropic — it already exists on the Pro plan, but it does not help catch a loop at an early stage (it fires only after the spend).

---

## ADR-015: Merge strategy = Squash and merge

**Date:** 2026-05-08
**Status:** accepted in Chat 6

**Context:** when merging a PR into `main` GitHub offers three strategies (merge commit / squash / rebase). A linear and readable main history, a simple `git log`, a simple revert are needed.

**Decision:** all PRs into `main` are merged via **Squash and merge**. One PR = one commit in main. The branch’s intermediate commits are not preserved in the main history — they are available only on the PR page on GitHub.

**Alternatives rejected:**
- *Create a merge commit* — main gets cluttered with “Merge pull request #N” merge commits, complicating bisect.
- *Rebase and merge* — every edit in the branch (including “fix typo”) lands in main as a separate commit, the main history bloats.

**Consequences:**
- In GitHub Settings → General → Pull Requests: leave only `Allow squash merging` enabled, disable `Allow merge commits` and `Allow rebase merging` (done in Sprint-9.1 together with branch protection of main).
- The squash commit in main takes its title from the PR title — phrase the PR title as a normal commit message (`feat: ...`, `fix: ...`, `test: ...`).
- Before merging, there is no need to delete draft commits like `wip` from the branch, squash will merge them.

---

## ADR-016: The GitHub repository — the single source of truth for deployment

**Date:** 2026-05-09
**Status:** accepted in a mini-chat after Chat 6

**Context:** owner recorded two gaps between the actual state of the project and the repo:

1. The `README.md` in the repo root is outdated and does not reflect the current state of the documentation (15 ADRs, the new autonomy principles, disk hygiene, Linear).
2. The current VPS runs operational artifacts (`tg_notify.sh`, `check_updates.sh`, `ssh_notify.sh`, `rc.local`, cron jobs) that are **not in the repo**. They are documented only in `docs/VPS_NOTIFICATIONS.md`, but the files themselves exist only on the VPS. This breaks reproducibility — if the VPS is lost they would have to be restored by hand from the description.

**Decision:** **the GitHub repository is the single source of truth for all deployment and operations artifacts.** The goal: the project deploys on a new server relying only on `git clone`.

### What this means in practice

1. **Everything that runs on the VPS is in the repo.** Bash scripts, cron tasks, systemd units, daemon configs (Docker, journald, sshd), pre-commit hooks, env templates, install scripts. They go into `infra/`.

2. **No manual edits on the VPS that are not reflected in the repo.** If an edit appeared on the VPS, it must be moved to `infra/` immediately and committed. If an edit is not committed, it formally does not exist; at the next `git pull` + `install.sh` it will be overwritten or lost.

3. **The `README.md` in the repo root is updated at every chat finalisation.** It is the public face of the project: documentation version, phase status, repo structure, a short how-to “deploy from scratch”. Updating the README is part of the finalisation checklist in `docs/archive/CHAT_PROTOCOL.md`.

4. **`infra/vps/install.sh` is idempotent and applies the whole content of `infra/vps/` to the system.** Once for bootstrap, after every `git pull` — to apply changes.

5. **Secrets are NOT stored in the repo.** Only the template `infra/secrets/secrets.env.template` without values. On the VPS the filled-in file lives in `/etc/<app>/secrets.env` (chmod 600). Scripts include it via `source`.

### Structure of `infra/`

```
infra/
├── vps/
│   ├── install.sh         # bootstrap, idempotent
│   ├── scripts/           → /usr/local/bin/
│   ├── profile.d/         → /etc/profile.d/
│   ├── systemd/           → /etc/systemd/system/
│   ├── cron/              → /etc/cron.d/
│   ├── docker/            → /etc/docker/
│   ├── journald/          → /etc/systemd/journald.conf.d/
│   └── etc/               → /etc/
└── secrets/
    └── secrets.env.template
```

Details are in `docs/INFRA.md`.

### Migration (Sprint-4.0 — done at the start of Chat 8)

Sprint-4.0: move the existing artifacts from the VPS into the repo:

| From the VPS | Into the repo |
|---|---|
| `/usr/local/bin/tg_notify.sh` | `infra/vps/scripts/tg_notify.sh` (without a hardcoded token) |
| `/usr/local/bin/check_updates.sh` | `infra/vps/scripts/check_updates.sh` |
| `/etc/profile.d/ssh_notify.sh` | `infra/vps/profile.d/ssh_notify.sh` |
| `/etc/rc.local` | `infra/vps/etc/rc.local` |
| the systemd unit for rc-local | `infra/vps/systemd/rc-local.service` |
| crontab `0 10 * * * check_updates` | `infra/vps/cron/check_updates` |

Old tokens that ended up in the chat are not migrated — they are replaced by a variable from `secrets.env`. On the VPS the token lives only in `/etc/<app>/secrets.env`.

### Alternatives rejected

- **“Store everything in Linear / notion / Google Drive”** — we already have a private git repo with history and a review process. Duplication increases entropy.
- **"Keep only a description in `VPS_NOTIFICATIONS.md`, restore the scripts by hand"** — the current practice. Not reproducible, the human factor, a proven risk (`/etc/rc.local` is already “lost” — Q-11).
- **“Use Ansible / Salt / Chef”** — overkill for one VPS, adds dependencies. For production we will revisit if there become several VPSs (see the Watchlist in HANDOFF.md).

### Antipatterns

- ❌ Editing a file directly on the VPS through `nano /etc/...` without a subsequent commit to `infra/`.
- ❌ Hardcoding tokens in bash scripts in the repo.
- ❌ Putting `secrets.env` (with filled-in values) in the repo (even a private one).
- ❌ Ignoring `README.md` when finalising a chat.

---

## ADR-017: Domain name `tndr.rs` (primary) + `jns.rs` (reserve) — SUPERSEDED ADR-026

**Date:** 2026-05-12
**Status:** SUPERSEDED ADR-026 (see below). A historical record.

**Context:** Sprint-2.1 planned the choice of 1 name out of 4 options (`tendersrb.app`, `nabavke.app`, `serbtenders.com`, `tenderly.rs`) with registration through Cloudflare Registrar. The partner got ahead of the process — registered `tndr.rs` (primary) and `jns.rs` (reserve, from _javne nabavke_) on their own with a `.rs`-accredited registrar.

**Decision (as of Chat 7):** `tndr.rs` is accepted as primary, `jns.rs` as a reserve.

**What happened next (Chat 9):** The partner reconsidered the choice and registered `nab.rs` instead of `tndr.rs` / `jns.rs`. See **ADR-026**, which supersedes this ADR.

**Consequences of the old decision that remained relevant:**
1. **Cloudflare Registrar is not applicable to the `.rs` zone** — the zone is managed by RNIDS and requires a `.rs`-accredited registrar.
2. **DNS — via NS delegation to Cloudflare** — the Sprint-3 plan stayed relevant. Applied in Chat 9 to `nab.rs`.

---

## ADR-018: Trial strategy — a universal anti_abuse_identifier + an opt-in verification charge + the Lite tier (revised)

**Date:** 2026-05-12 (revised 2026-05-26 in Chat 14)
**Status:** accepted (revised); supersedes the original ADR-018 of 2026-05-12 (Chat 7, PIB-as-hard-required)

**Context:** the original ADR-018 required binding the trial to a Serbian DOO’s PIB as the only anti-abuse mechanism. After ADR-033 (multi-country foundations) this approach breaks for non-Serbian customers and for Serbian individuals/preduzetnik without a DOO. The research note (B4) showed: a universal `anti_abuse_identifier` + a low-friction stack (Turnstile + Kickbox + an opt-in €1 verification charge) wins on CAC and removes the geographic block. Benchmarks of 6 SaaS cases (Plausible, Ghost, Linear, Notion, Calendly, Cal.com) show: in the B2B segment with ACV €50–500/month nobody requires KYC/PIB/phone OTP on a trial.

**Decision:** a 14-day trial, activated via a universal `anti_abuse_identifier`. The PIB of a DOO is one of the identifier subtypes for Serbian tenants, not the only possible one.

### The anti-abuse stack by layers

1. **Cloudflare Turnstile** (Managed mode, free unlimited) — an invisible check on the sign-up form. Cuts off 95%+ of bots before an account row is created.
2. **Kickbox real-time email validation** (~$0.02/check) — a synchronous API call on signup. Blocks disposable domains (mailinator, temp-mail, 10minutemail, etc.) based on Kickbox’s maintained database of 50K+ domains. We keep no hardcoded blocklist in code.
3. **Email confirmation link** — 24h TTL, without a click → the account is auto-deleted after 72h.
4. **Risk scoring** based on fingerprint + IP/ASN + Kickbox flags:
   - Low risk → a cardless 14-day trial.
   - Medium/high risk → step-up: a €1 verification charge via Paddle SetupIntent, an instant refund after the card is confirmed.
5. **One `anti_abuse_identifier.value_hash` = one trial in 12 months** — a UNIQUE constraint in the DB, anti-multi-accounting at the schema level.

### Per-country specifics via KYCProvider

After ADR-033 anti-abuse is implemented as a pluggable `KYCProvider`, selected by `tenants.country_code`:

- **`KYCProvider('apr_rs')`** — for tenants with `country_code='RS'` and `identifier_type='serbian_pib_doo'` it requests the company status in the APR (`apr.gov.rs`). Active status → low risk. Inactive / in liquidation / not found → high risk → a forced step-up verification. Implemented in Sprint-22.
- **`KYCProvider('default')`** — for the other countries and identifiers without a country-specific lookup: only Kickbox + Paddle verification + risk scoring. Applied in phase 0 to all non-RS tenants and to RS tenants without a DOO PIB (individuals, preduzetnik).

Plugin discovery for a country-specific KYCProvider is postponed (see ADR-033 deferrable). In phase 0 there are only the two providers above.

### Identifier typing

`anti_abuse_identifier.identifier_type` (enum):

| Value | Purpose | When applied |
|---|---|---|
| `serbian_pib_doo` | A Serbian legal entity’s PIB (9 digits) | An RS tenant with a DOO; preferred for the B2B segment |
| `serbian_jmbg` | A Serbian personal ID | An RS preduzetnik or individual |
| `card_fingerprint` | A card fingerprint from Paddle SetupIntent | Universal for any country; applied on step-up |
| `email_domain` | A corporate email domain | B2B attestation without legal documents |
| `device_fingerprint` | A device fingerprint | For repeat attempts by the same individual |
| `manual_b2b_attestation` | A manual check by the partner/owner | Edge cases |

The `region` field (CHAR(2), FK to `countries.code`) is for future geographic segmentation (see ADR-033).

### Tier structure: adding Lite €9

Removes the price barrier for the segments that the old ADR-018 blocked: the self-employed, preduzetnik, individuals, foreign customers. The conversion entry point is paid from the start, not a trial gamble.

| Plan | Price/month | AI analysis | Channels | Multi-user |
|---|---|---|---|---|
| **Lite** (new) | €9 | No | Email only | 1 |
| Starter | €29 | L2 basic | Email + Viber + Telegram | 1 |
| Pro | €69 | L2 + L3 (10/month) | All | 3 |
| Business | €149 | L2 + L3 (50/month) | All + API | 10 |
| Enterprise | from €399 | Custom | All + an on-premise option | Custom |

Lite **has no AI analysis** (the most expensive operation) — this is both unit-economics protection and a natural motivation to upgrade to Starter.

### Cost projection

With 1 000 sign-ups/month:
- Turnstile: $0.
- Kickbox (100% × $0.02): $20.
- Paddle verification (30% suspicious × the Paddle fee $0.55 on €1): $165.
- TOTAL: ~$185/month ≈ $2 220/year.
- Anti-abuse CAC: 5.6% of MRR at 1 000 paying (€25 000+ MRR with a Lite/Starter/Pro mix).

Comparison with Twilio Verify SMS to Serbia (rejected): $460/month per 1 000 sign-ups — 2.5× more expensive with a smaller conversion uplift. Phone OTP is justified only as 2FA for paying customers, not for signup.

### Impact on the DB schema and architecture

- **DB_SCHEMA.md v1.1** (PR-3): the table `anti_abuse_identifiers` with the fields `identifier_type`, `value_hash` (SHA-256), `value_last4`, `risk_score_at_signup`, `verification_method`, `verified_at`, `expires_at`, `region` (CHAR(2) FK to `countries.code`). A UNIQUE constraint on `(value_hash)` globally (anti-multi-account across 12 months — via `expires_at`).
- **the architecture notes (not published) v3.0** (PR-3): the `KYCProvider` interface in §14 (after the AI pipeline) or in §1.5 multi-country foundations; the mapping `tenants.country_code → KYCProvider`.
- **Sprint-19** (web cabinet, signup flow): the implementation of Turnstile + Kickbox + email confirmation.
- **Sprint-21** (Paddle in full): the SetupIntent step-up flow, an instant refund after confirmation, recording `card_fingerprint` in the identifier.
- **Sprint-22** (KYC for RS): the implementation of `KYCProvider('apr_rs')` with a lookup in `apr.gov.rs`.

### Migration of the old ADR-018

The old ADR-018 (PIB hard-required) is **not implemented in code** — Sprint-19/21/22 are still ahead. This revision is applied at the first implementation. No data migration is needed. After ADR-033 the field `tenants.pib` was renamed to `tenants.tax_id` (composite UNIQUE `(country_code, tax_id) WHERE tax_id IS NOT NULL`); the link with `anti_abuse_identifiers.value_hash` goes through SHA-256(`tax_id_normalized`).

**Source:** research note B4, 23 sources of empirical evidence, 2026-05.

---

## ADR-019: Customer Support — a single mailbox + a native Serbian speaker + a single SLA

**Date:** 2026-05-12
**Status:** accepted in Chat 7

**Context:** it was necessary to define the customer support structure (Q-7) — one mailbox or separate ones per language, who answers in Serbian, how to vary the SLA by plan.

**Decisions (partner):**

1. **One shared mailbox `support@nab.rs`** (originally `support@tndr.rs` was planned, updated in Chat 9 after ADR-026) for all languages and plans. Implementation: Postmark inbound parsing with forwarding to a helpdesk (the exact tool is for phase 1; candidates: a helpdesk plugin in Linear, or Help Scout).
2. **Support languages:**
   - Russian, English — owner (an AI draft via Claude with a final human review is possible).
   - Serbian — a native speaker with conversational Serbian is needed. Hiring is planned in Sprint-23. Until then the partner answers in Serbian as far as possible.
3. **The response-time SLA is the same for all plans** (Free / Starter / Pro / Business). The bonus for Pro/Business is extra support hours + work on one of the weekend days.

**Alternatives rejected:**
- Separate mailboxes `sr@`, `ru@`, `en@` — complicate triage, lead to lost emails, overkill for a small team.
- A differentiated SLA “Starter 48h / Pro 24h / Business 4h” — rejected by the partner: “it is not clear, everyone needs the same response”. The value of Pro/Business is delivered through extra hours, not through response time.
- Outsourcing to a Serbian freelancer right away — rejected: there is no flow of requests, the freelancer would sit idle without tasks.

**Consequences:**
- In Sprint-3, when creating Proton Mail Plus (ADR-027) — the alias `support@nab.rs` is created in Chat 10 (or later as needed).
- In Sprint-17 (Postmark) — set up an inbound stream for `support@nab.rs`.
- In Sprint-23 — hire a native Serbian speaker for support + product localisation.
- The concrete SLA numbers (for example, “within 24 working hours”) are fixed before Sprint-19 in `BUSINESS_OPS.md`.
- Operational details (reply templates, scripts in 3 languages) — in `BUSINESS_OPS.md` section 4.

---

## ADR-020: GitHub MCP-based file workflow for the web chat

**Date:** 2026-05-17
**Status:** accepted in Chat 7

**Context:** `CONNECTORS.md` v1.0–1.2 recorded that Claude in the web chat **cannot write to GitHub** — all commits go through a manual upload by owner (download + GitHub web UI upload) or through Claude Code on the VPS. In Chat 7, when trying to use the GitHub MCP server (`api.githubcopilot.com/mcp`, available as a “GitHub CUSTOM” connector in Claude.ai), it turned out that the MCP has a full read+write API, but it did not work because the corresponding GitHub App “Claude Github MCP Connector” had been authorized by OAuth but not installed on the account `TnSrb1` as an Installation.

After a correct installation of the App with the permissions **All repositories + read+write to code, discussions, issues, pull requests, repository projects** the MCP started working. This changes the project’s file protocol.

**Decision:**

1. **GitHub MCP is the primary channel for writing to the repo** from the Claude web chat. Used for:
   - Creating feature branches `claude/<task-id>` via `GitHub:create_branch`.
   - Committing changes into these branches via `GitHub:create_or_update_file` or `GitHub:push_files`.
   - Opening PRs into `main` via `GitHub:create_pull_request`.
   - Reading fresh files via `GitHub:get_file_contents` (faster and more accurate than RAG).

2. **owner is the only link for merging into `main`.** Claude **does not call** `GitHub:merge_pull_request` for PRs into `main`. The merge is done by owner through the GitHub web UI with the “Squash and merge” button (ADR-015). In Sprint-9.1 this will additionally be protected by branch protection rules on `main`.

3. **The fallback write channels remain:**
   - `present_files` + a manual upload via the GitHub web UI — a fallback if the MCP drops (see `docs/archive/GITHUB_MCP_SETUP.md`).
   - Claude Code on the VPS — for large code tasks with testing (ADR-010, ADR-014).

4. **`GitHub Integration` (the RAG sync) is kept** as a parallel read channel through Project Knowledge. It is not replaced by the MCP — it has its own niche: semantic search over all documentation without knowing specific paths.

5. **Files that Claude does NOT edit via the MCP even technically having the rights:**
   - `infra/secrets/*` — secrets, even templates, require a conscious check by owner.
   - `.github/workflows/*` — CI changes can break the pipeline; edited by owner or by Claude Code on the VPS with a local check.
   - Files marked “owner only” in the project rules or in this ADR (as they appear).

**Technical consequences:**

- In every new chat Claude must call `tool_search("github")` to load the MCP tools. After that `GitHub:*` are available until the end of the chat.
- When writing an existing file its current SHA must be passed — otherwise the MCP returns 409 Conflict. The SHA is obtained via `GitHub:get_file_contents`.
- Branch name: `claude/<task-id>` or `claude/<short-description>`. Do not use `main` or `develop`.
- The PR title is phrased as a commit message (`feat:`, `fix:`, `docs:` — conventional commits), since with squash-merge it becomes the commit in `main`.
- If one PR has many files — prefer `GitHub:push_files` (one commit, several files) over a series of `GitHub:create_or_update_file`.

**Alternatives rejected:**

- **“Leave it as it was — no writing from the chat”** — Chat 7 demonstrated that a manual upload via the web UI leads to errors with misfiled files (see the history in `HANDOFF.md` Chat 7). The MCP path is more reliable.
- **"Allow Claude to merge into `main` directly"** — rejected: the merge is owner’s only control point over architectural decisions, losing it removes the review stage.
- **“Use only Claude Code on the VPS for all edits”** — rejected: for documentation (.md) that is excessive, the web chat with the MCP works faster and needs no context switch.

**Consequences for documentation:**

- `docs/CONNECTORS.md` — updated to v2.0, rewritten with the MCP in mind.
- `docs/archive/PROJECT_BRIEF.md` — updated to v1.6, the “Connectors” section rewritten.
- `docs/archive/GITHUB_MCP_SETUP.md` — a new file, a step-by-step guide to installing and restoring the MCP.
- `docs/archive/CHAT_PROTOCOL.md` — will be updated in following chats: the finalisation checklist now includes “open a PR via the MCP” as Method A.

**Lessons learned (for future diagnostics):**

- With connector problems **first check the vendor’s documentation** (`web_search`), rather than persisting with hypotheses from training data.
- If owner says “it works for other people” — that is weight against the internal hypothesis, not “user optimism”. In Chat 7 this hint from owner was dismissed by Claude five times, which tripled the diagnosis time.
- OAuth authorization and App installation are two **different** levels of access on GitHub. The entry “Authorized OAuth Apps: Never used” is a diagnostic signal that the Installation is missing.

---

## ADR-021: The Source Adapter pattern — a single interface for all tender platforms

**Date:** 2026-05-20
**Status:** accepted in Chat 8

**Context:** on a 24-month horizon the project must connect 5–10 heterogeneous sources: jnportal (Serbia, JSON/mobile app), obnova (HTML + a multilingual version), tendersinfo (international commercial, API), EBRD (RSS + API), UNDP (static HTML), potentially others. Each has its own format, schedule and quirks. If the parsing/normalization logic is “baked” into shared code, adding a new source requires revising the core and regressions in the existing ones. This violates the requirement “add a source in one sprint”.

**Decision:** each source is implemented as a class inheriting `SourceAdapter` with a fixed interface (see the architecture notes (not published) section 4.1). The adapter encapsulates **everything** source-specific: the HTTP client, pagination, parsing raw → a `RawTender` structure, forming a stable `source_uid`, normalization into the common `NormalizedTender` model, optional document download.

**A hybrid “registry in code + config in the DB”:**
- **In code** (`src/parsers/<source>/`): the adapter class itself. Registered at start-up through the decorator `@register_source`. Changes go through review via a PR.
- **In the DB** (the `sources` table): runtime config — `enabled`, `polling_interval_sec`, `last_run_at`, `last_status`, `available_for_tiers`. Managed through the admin and ENV variables on first deploy. Changes without a redeploy.

**Idempotency:**
- `UNIQUE (source_key, source_uid)` on `tenders_raw`.
- `INSERT ... ON CONFLICT DO UPDATE` with a `raw_hash` comparison — re-processing the same tender = a no-op if nothing changed.

**Rate limit and locking:**
- A per-source token bucket in Redis (`source:{key}:tokens`).
- A per-source distributed lock (`source:{key}:lock`, TTL 30 min) — one source = one worker at a time, even under horizontal scaling.

**Retry policy:**
- HTTP 429 / 5xx → exponential backoff (30s/2m/10m), max 3 attempts.
- HTTP 4xx (except 429) → straight to the DLQ + Sentry.
- TimeoutError / ConnectionError → retry 3 attempts.
- Parsing failed (the HTML changed) → DLQ + Sentry + an automatic alert to the TG dev channel.

**Observability:** the table `source_runs` records the history of every pass (when it started, what it found, how many new, how many updates, errors). From it — charts in the admin cabinet and an alert “the source is silent for more than 2× polling_interval”.

**Alternatives rejected:**
- **One shared parser with a “selectors in JSON” config** — works only while the sources are structurally similar; the first source with non-standard authorization/pagination breaks the abstraction.
- **Parsers as fully independent services** — overkill at MVP, adds deployment complexity and a network boundary where none is needed.
- **Third-party aggregators (tendersinfo as the primary source)** — we then become a middleman of a middleman, lose latency and depend on their ToS.

**Consequences:**
- In Sprint-14 (jnportal MVP) — the first adapter is implemented together with the base class and the adapter infrastructure.
- In Sprint-25 (obnova) — reuse of the infrastructure, done in 1 sprint.
- In phase 3 (tendersinfo, EBRD, UNDP) — each source = a separate PR that does not touch the core.
- Tender documents (PDF konkursna dokumentacija) — are not stored whole; the adapter returns only metadata (URL, name, size). The content is downloaded on demand for AI level 3 and deleted immediately (ADR-005, disk hygiene).

---

## ADR-022: A two-tier tender model + canonical for cross-source deduplication

**Date:** 2026-05-20
**Status:** accepted in Chat 8

**Addendum (2026-05-26, Chat 14):** after ADR-033 the `cluster_key` formula was updated — `country_code` becomes the first component:

```
cluster_key = sha1(
    country_code
    + customer_tax_id_normalized
    + primary_classification.system_code
    + primary_classification.code
    + round(amount_eur, -3)
    + date_trunc('day', deadline_at)
)
```

Applied in `DB_SCHEMA.md` v1.1.

**Context:** the same tender may be published on two or three sources (especially large ones with an international element — jnportal + obnova + EBRD). If stored as three independent records, the subscriber receives three notifications about the same thing. If merged “at parse time” — the raw data is lost for future reconciliation. Also: different sources use different formats (currency, CPV taxonomy, language, region encoding), while the cabinet needs to show a single card.

**Decision — three storage tiers:**

1. **`tenders_raw`** — an exact copy of what the source returned. `(source_key, source_uid)` UNIQUE, `raw_payload JSONB`, `raw_hash`. Never edited (except UPSERT on update), stored forever (ADR-012).

2. **`tenders_normalized`** — a per-source canonicalised record. One to one with raw (1:1), but in the common schema: title, description, customer_pib, amount_eur (converted), cpv_codes (mapped to EU CPV), regions (mapped to ISO 3166-2:RS), language (`sr-Cyrl`/`sr-Latn`/`en`), procedure_type (a normalized enum), documents (metadata), `description_embedding` (pgvector 1536). All parsers, routing and AI work with exactly this table.

3. **`canonical_tenders`** — a group of duplicates from different platforms. Several `tenders_normalized` may point to one `canonical_tender_id`. One of them is `primary` (by priority: jnportal > obnova > EBRD > UNDP > tendersinfo).

For the full details of the deduplication algorithm, RLS and embeddings, see the architecture notes (not published) section 5.

**Alternatives rejected:**
- **One tenders table without a raw/normalized split** — we lose the ability to recompute normalization when the logic is updated without re-downloading.
- **Merging duplicates via AI** — expensive and unstable. A deterministic cluster_key + embedding cosine is faster and reproducible.
- **Deleting "duplicates"** — we lose the links to other sources.

**Consequences:**
- In Sprint-13 (multi-tenant DB) — the tables are created by this schema right away.
- Embeddings (OpenAI text-embedding-3-small) are computed on INSERT/UPDATE of `tenders_normalized`; the cost is insignificant (~$0.40 per 40k tenders/year).
- Partitioning `tenders_normalized` by `published_at` — phase 3 (~3M rows).

---

## ADR-023: The subscriber profile structure and a two-stage routing engine

**Date:** 2026-05-20
**Status:** accepted in Chat 8

**Addendum (2026-05-26, Chat 14):** after ADR-033 stage A of the routing engine adds the condition `(p.country_code = t.country_code OR p.country_code IS NULL)`. The second condition is for subscriptions to international tenders (EBRD, UNDP) with `country_code IS NULL`. Applied in `DB_SCHEMA.md` v1.1 §6.1 and in `src/routing/` in Sprint-16.

**Context:** the key requirement of the project is per-subscriber flexibility. Each must be able to set: sources, keywords, CPV, regions, amount range, languages, procedure types, minimum margin before the deadline, delivery mode, quiet hours, AI settings. All of this must work efficiently at N=200–1000 active profiles.

**Decision — the profile model as a set of fields in a single `profiles` table** + two-stage matching. Key features:

1. **Sources as an array `enabled_source_keys TEXT[]`** with a GIN index.
2. **Keywords as two-level AND/OR groups (JSONB):** `OR` between groups, `AND` inside. Covers 95% of real cases without a full Lucene DSL.
3. **CPV — prefix match** (`32*`), include/exclude arrays.
4. **Soft-match via embedding** (optional, for profiles without hard keys).
5. **Profile versioning** via `profile_revisions`.
6. **Per-tier limits:** Free/Starter — 1, Pro — 3, Business — 10, Enterprise — unlimited.

**The two-stage routing engine:**

**Trigger:** a PostgreSQL trigger on `tenders_normalized` → `pg_notify('tender_changed', ...)`. The **Routing Worker** listens to the channel through asyncpg, and on each event:

**Stage A — an SQL prefilter** (one query, ~5–30ms per 1000 profiles thanks to GIN indexes): selects candidate `profile_id`s by array conditions and numeric constraints.

**Stage B — text matching** in Python (precompiled regexes) or FTS, milliseconds on 5–50 candidates.

**Stage C (opt.)** — soft-match via cosine similarity.

**Writing to the outbox with UNIQUE (profile_id, tender_id, channel) DO NOTHING** — idempotency.

**Target SLA:** from the INSERT into `tenders_normalized` to the outbox record — p95 < 5 sec at 1000 active profiles.

For the full details of the schema, the SQL query and update handling, see the architecture notes (not published) sections 6 and 7.

**Alternatives rejected:**
- A materialized view refreshed by cron — we lose real time.
- Third-party search engines — overkill for the current volume.
- A Lucene DSL for the user — too complex for non-programmers.

**Consequences:**
- In Sprint-13 — the tables by this schema.
- In Sprint-16 — the implementation of the routing engine.
- In Sprint-19 — the UI with plan gates + a match preview.
- pgvector is mandatory in PostgreSQL.

---

## ADR-024: The AI pipeline — three analysis levels + caching + a plan gate

**Date:** 2026-05-20
**Status:** accepted in Chat 8

**Addendum (2026-05-26, Chat 14):** after ADR-033 the AI system prompt is parametrised by `{{country_name}}` and `target_language`. At MVP — `country_name="Serbia"`, `target_language="ru"`. The parameters are taken from `tenants.country_code` + `profiles.target_language`.

**Context:** AI is the product’s main feature and at the same time the main variable cost line. An unconditional Sonnet/Opus call on every tender for every profile scales to $6600/month on AI alone at 200 profiles. This kills the unit economics. On the other hand, AI analysis is the main value difference between Free and paid.

**Decision — three analysis levels:**

| Level | Name | Model | Trigger | Cost / analysis |
|---|---|---|---|---|
| 1 | classification | Haiku 4.5 | Automatically on every tender during normalization | ~$0.0001 |
| 2 | standard | Sonnet 4.6 | Per (tender × profile), if `ai_analysis_level >= standard` | ~$0.01 |
| 3 | deep | Opus 4.7 | By a button in the cabinet, Pro+ | ~$0.10 |

**Level 1 (always, independent of the subscriber):** language, predicted_cpv, procedure type, flags, topic_tags, quality_score.

**Level 2 (on the profile’s request):** the full text + the subscriber’s context. Anthropic prompt caching → a saving of 80-90% of input tokens. A JSON output with summary, match_score, red_flags, etc.

**Level 3 (Pro+ by a button):** + the PDF documentation + the buyer’s history. A detailed technical analysis.

**Per-plan limits:**

| Plan | L1 | L2 | L3 |
|---|---|---|---|
| Free | ✅ | ❌ | ❌ |
| Starter | ✅ | ✅ up to 50/month | ❌ |
| Pro | ✅ | ✅ unlimited | ✅ up to 10/month |
| Business | ✅ | ✅ unlimited | ✅ up to 50/month |
| Enterprise | ✅ | ✅ unlimited | ✅ unlimited + custom prompts |

**Cost control:**
- A spending limit in the Anthropic Console.
- A daily report to the TG dev channel.
- An alert at 80% of the monthly budget.

**Failover:** retry → at 30 min of downtime we postpone L2, L3 → to the user “try again later”. Long-term unavailability — a fallback to OpenAI/Gemini (phase 2).

Full details — the architecture notes (not published) section 8.

**Alternatives rejected:**
- A single level — always Sonnet — kills the unit economics of Starter.
- AI only by a button — we lose the value of paid.
- A self-hosted LLM — RAM on the VPS is scarce.

**Consequences:**
- In Sprint-6 — set up the spending limit, check prompt caching.
- In Sprint-18 — the implementation of all three levels + the cache.
- The AI worker — a separate RQ queue.

---

## ADR-025: The delivery outbox pattern + a per-channel adapter + a schedule

**Date:** 2026-05-20
**Status:** accepted in Chat 8

**Context:** notification delivery is a bottleneck and a source of many classes of bugs: AI failed but the push went out, a network failure ⇒ a duplicate, a blocked user ⇒ useless attempts, a digest user receives instantly, quiet hours are ignored.

**Decision — the outbox pattern:**

1. **Any delivery → `delivery_outbox`** with UNIQUE (profile_id, tender_id, channel). Status: `pending`/`ai_pending`/`ai_done`/`sent`/`failed`/`cancelled`.

2. **Workflow by status:** Routing creates `pending` or `ai_pending` → the AI worker takes `ai_pending` and sets `ai_done` → the Delivery worker takes `pending`/`ai_done` (taking the schedule into account) → renders through a `DeliveryAdapter` → sends → sets `sent`.

3. **A single interface `DeliveryAdapter`** — `format()`, `send()`, `supports_buttons()`. Implementations: Telegram (aiogram 3), Viber (SDK), Email (Postmark), WebPush (SSE).

4. **Per-profile schedule:** immediate / digest_morning (09:00) / digest_evening (18:00) / digest_custom (Pro+, cron).

5. **Quiet hours** — in immediate mode we postpone until the end, in digest mode they are ignored.

6. **Canonical tender at send time:** if the profile already has an outbox with another tender_id of the same canonical — enrich the existing record with the note “also on obnova”.

7. **Per-channel rate limit** via a token bucket in Redis: Telegram 30/sec, Viber 15/sec, Postmark 10/sec.

8. **Error handling:** Telegram 403/blocked → `user_channels.status='blocked'`; Email hard bounce → `email_status='bounced'`; soft bounce → retry 1h/6h/24h; 5xx → retry 30s/2m/10m → DLQ.

9. **Read confirmation:** `tender_views` is written on a click in Telegram/Email/Viber or on opening the card. The open rate metric per profile is feedback for tuning.

Full details — the architecture notes (not published) section 9.

**Alternatives rejected:**
- A direct channel call from routing — produces all the bugs listed.
- A single delivery channel — contradicts ADR-004.
- A third-party notification platform — overkill.
- Kafka for the outbox — PostgreSQL LISTEN/NOTIFY covers our needs up to 10× the volume.

**Consequences:**
- In Sprint-13 — the tables delivery_outbox, delivery_attempts, user_channels.
- In Sprint-8/17/25 — each channel implements DeliveryAdapter.
- The delivery worker — a separate RQ queue.
- In the cabinet — a “Notification history” page with filters.

---

## ADR-026: Domain name `nab.rs` (supersedes ADR-017)

**Date:** 2026-05-22
**Status:** accepted in Chat 9 (a fait accompli from the partner)

**Context:** ADR-017 recorded the choice of `tndr.rs` (primary) + `jns.rs` (reserve) on the basis of ultra-shortness. However, at the actual registration the partner chose a different option — `nab.rs`. By Chat 9 this registration was already a fact.

**The partner's rationale (recorded as of the fait accompli):**
- `nab` is short for *nabavka* (“procurement” in Serbian), a direct semantic link to the product.
- 3 letters — shorter than `tndr` (4 letters).
- In the Serbian market a semantic link is stronger than the international abbreviation “tender”.
- The `.rs` zone is a suitable local one.

**Decision:**
- **Primary brand name:** `nab.rs`.
- **Reserve:** not registered (the `jns.rs` idea is canceled).
- ADR-017 is declared **superseded**, remaining in the log as a historical record.

**Registrar:** Loopia DOO (the Serbian branch of the Swedish Loopia AB). Active in Serbia since ~2010. Registered until 22.05.2027.

**The registrar’s specifics** (the exact name of the control panel, access, registrant data, the date of the next renewal) — **not yet received from the partner** in full at the time of closing the ADR (a tail of Sprint-2.1, open question Q-2.1).

**Consequences:**
- Wherever possible, replace all mentions of `tndr.rs` / `jns.rs` in the documentation with `nab.rs`. ADR-017 is not edited — it is historical.
- The project’s email domain: `*@nab.rs` (instead of `*@tndr.rs`).
- Support: `support@nab.rs` (per ADR-019), the main address: `info@nab.rs` (created in Chat 9 as a Proton Mail Plus mailbox).
- In marketing — the brand name “Tender Bot Serbia” is kept, but the domain `nab.rs` is used as a short handle.
- In the 1Password vault `TenderSRB Production` (after it is created in Sprint-5) — record the Loopia registrar data.

---

## ADR-027: removed from the public snapshot

## ADR-028: removed from the public snapshot

## ADR-029: Tier naming of Claude models in the architecture, the slug in config and the DB

**Date:** 2026-05-24
**Status:** accepted in Chat 10

**Context:** ADR-008 (Chat 1) fixed the AI stack with concrete model slugs: `claude-haiku-4-5-20251001` (level 1), `claude-sonnet-4-6` (level 2), `claude-opus-4-7` (level 3). These slugs change every 3-6 months in Anthropic's catalog — in a year the architecture document is outdated in the AI stack part. In PR #21 (the architecture notes (not published) v2.0) the decision was made to use tier names instead of slugs, which revealed the need for an explicit ADR to separate the levels of abstraction.

**Decision:** two-level naming of Claude models in the project:

1. **The architectural level — tier names.** In the architecture notes (not published), in code at the business-logic level, in `BUSINESS_OPS.md`, in marketing materials — abstract names are used:
   - `cheap` — fast classification (level 1)
   - `balanced` — the standard analysis (level 2)
   - `premium` — the deep analysis (level 3)

2. **The operational level — concrete slugs.** Concrete model names live in **two places**:
   - `infra/config.yaml` — the runtime `tier→slug` mapping. Changed without editing code (deploy config). An example:
     ```yaml
     ai_models:
       cheap: "claude-haiku-4-5-20251001"
       balanced: "claude-sonnet-4-6"
       premium: "claude-opus-4-7"
     ```
   - the `tender_ai_analyses.model` field in the DB — stores the **slug at the time of the call** for audit (which exact model analyzed which tender). This is a historical record, not the current configuration. See `DB_SCHEMA.md` §2.14.

**What this gives:**
- Replacing a model — one edit in `config.yaml`, zero edits in the architecture document and in code.
- Cost audit and response history — preserved with the exact slug in the DB, not lost on an upgrade.
- Marketing and documentation speak in plain language (“quick check”, “detailed analysis”), not technical jargon.

**Relation to ADR-008:** ADR-008 is not canceled. It remains as a historical record “AI = Anthropic Claude only, 3 analysis levels”. ADR-029 extends ADR-008 with a naming rule. The concrete slugs from the text of ADR-008 are the historical reality as of Chat 1, the current mapping is in `infra/config.yaml`.

**Alternatives rejected:**
- **Keep slugs in the architecture** (as in the architecture notes (not published) v1.0) — obsolescence every 3-6 months.
- **Tier only, without storing the slug in the DB** — loss of the ability to audit (in 6 months we could not say which model wrote “this nonsense”).
- **Configuration in env variables instead of config.yaml** — env is poorly suited to a structured mapping and will fall apart when growing to 5+ models.

**Consequences:**
- In Sprint-6 (Anthropic Console setup) — `infra/config.yaml` with the tier→slug mapping is created.
- In Sprint-18 (AI pipeline implementation) — the code reads the tier from business logic, the mapping is in config, the actual slug in the DB.
- In DB_SCHEMA.md §2.14 — add the comment "`model` stores the slug at the time of the call for audit; the tier→slug mapping is in `infra/config.yaml`". Not a blocker for Chat 10 — a spot edit in one of the nearest chats.

**Closes:** Q-17 in `OPEN_QUESTIONS.md`.

---

## ADR-030: removed from the public snapshot

## ADR-031: The tech stack of the `nab.rs` landing page — Vanilla HTML + Pico.css + Cloudflare Pages

**Date:** 2026-05-24
**Status:** accepted in Chat 10

**Context:** in Chat 10 the project needed an actually deployed landing page at the demand of Paddle KYC and Viber for Business (both require a working website, not “coming soon”). A research agent carried out research (PR #23) — compared 5 stack options on 8 criteria, gave a recommendation.

**Decision (owner after reviewing the research):**

1. **Stack:** **Vanilla HTML + CSS + JS + Pico.css 2.x** (via CDN).
   - No npm/yarn/build step.
   - 3 base files per language (main + 2 legal) — manageable without a templating engine.
   - When growing to 9+ pages — migration to Astro (the plan is in `site/README.md`, trivial for static content).

2. **Hosting:** **Cloudflare Pages** (free).
   - Connects to the GitHub repo.
   - Auto-deploy on push to `main`.
   - The free plan — 500 builds/month, unlimited bandwidth and requests.
   - Custom domain `nab.rs` (via the existing Cloudflare DNS zone, see ADR-026).

3. **Analytics:** **Cloudflare Web Analytics** (free, cookieless, privacy-friendly).
   - Needs no consent banner — simplifies GDPR compliance.
   - A snippet with the placeholder `{{CF_ANALYTICS_TOKEN}}` — owner fills it in after the first deploy.

4. **Structure of the `site/` folder:**
   ```
   site/
   ├── index.html          root redirect → /ru/
   ├── ru/                 main content (one-pager + 2 legal)
   ├── en/                 coming-soon stub + 2 legal
   ├── sr/                 uskoro stub + 2 legal (with ⚠️ ČERNOVIK)
   ├── assets/             style.css + favicon.svg
   ├── _headers            Cloudflare Pages security headers
   ├── _redirects          pretty URLs + root fallback
   └── README.md           maintenance guide + migration to Astro
   ```

5. **Localisation:** **a structure for 3 languages right away** (ru/en/sr), **content only in ru for now**. This is needed for future language versions without rewriting the structure. EN and SR are placeholder stubs at the start, the full content closer to launch in Sprint-19 (en) and Sprint-23 (sr with a native speaker).

6. **Contact form:** **`mailto:info@nab.rs`** (no backend). If needed in the future — migrate to Cloudflare Pages Functions (free up to 100k invocations/day).

**Rationale for choosing Vanilla (not Astro):**
- The highest weight in the research criteria was the item “owner edits via the GitHub web UI without a local build” — Vanilla gives this in pure form (open `.html` → Edit → commit → on the site in 1 min).
- Zero dependencies — in line with the principles of ADR-005 (Docker hygiene) and ADR-016 (minimum dependencies in `infra/`).
- When growing to 9 files in three languages the structure stays manageable; if it becomes heavy — Astro as the next step (the plan is already recorded in `site/README.md`).

**Cost:**
- Pico.css — $0 (MIT license, opensource, CDN).
- Cloudflare Pages — $0 (free tier).
- Cloudflare Web Analytics — $0.
- Cloudflare Pages Functions (if later needed for the form) — $0 up to 100k/day.
- **Total landing infrastructure: $0/month.**

**Alternatives rejected (from research PR #23):**
- **Astro 6** — a close score (4.17 vs 4.60), but needs node_modules locally or in CI. Postponed until growth.
- **Eleventy** — also needs Node.js. No clear advantages over Astro.
- **Hugo** — a high entry threshold (Go templates), dropped.
- **Pelican** — Python is excessive for 3 files, dropped.

**Implementation:** PR #24 (Chat 10) — 15 files in `site/`, ready to deploy to Cloudflare Pages. owner connects CF Pages following the 10-step instruction in `site/README.md`. Fills in 5 placeholders: `{{COMPANY_NAME}}`, `{{COMPANY_PIB}}`, `{{COMPANY_REG_NUMBER}}`, `{{COMPANY_ADDRESS}}` (after receiving them from the partner), `{{CF_ANALYTICS_TOKEN}}` (after registering the site in CF Web Analytics).

**Consequences:**
- The landing infrastructure cost — $0/month permanently (on a 24-month horizon).
- For the Paddle and Viber KYC — a real website at `https://nab.rs/` (after owner deploys).
- When content grows to 9+ pages — migration to Astro per the plan in `site/README.md` (1-2 days for the research assistant).
- Lighthouse score target 95+ on mobile — without a heavy JS bundle, checked after the deploy.

---

## ADR-033: Multi-country foundations — countries registry, country_code propagation, classifications abstraction

**Date:** 2026-05-26
**Status:** accepted in Chat 14

**Context:** the project must be extensible to any country (UZ, KZ as the first candidates). Architecture v2.0 is ~60% universal, but 7 country-dependent points are hardcoded to Serbia: tenants.pib, cpv_codes, regions ISO 3166-2:RS, anti-abuse via APR, the AI system prompt, the absence of `country_code` on key tables, the absence of an abstraction over procurement classifiers.

Research: PR #29 + PR #40 (B8, merged).

**Decision:** apply the foundational DDL before Sprint-13. A full country-pack mechanism (KYC providers, locale packs, plugin discovery) is postponed until an actual second country.

### Foundational (before Sprint-13)

1. A new table `countries` (a registry: code, default_locale, default_currency, default_tz, tax_id_required, tax_id_format regex, kyc_provider_key, regulator_url, support_email). Seed for RS.
2. A new table `classification_systems` (a registry of procurement classifiers). Seed: `eu_cpv`, `unspsc`.
3. Rename `cpv_codes` → `classifications` + add `system_code` (FK to `classification_systems`). Composite PK `(system_code, code)`.
4. `country_code CHAR(2)` on `tenants`, `tenders_raw`, `tenders_normalized`, `profiles`, `regions` (scalar); `sources.country_codes CHAR(2)[]` (an array). FK to `countries(code)` where applicable.
5. `tenants.pib` → `tenants.tax_id` + nullable + UNIQUE `(country_code, tax_id) WHERE tax_id IS NOT NULL`.
6. `cluster_key` (ADR-022) includes `country_code` as the first component of the SHA-1 formula.
7. Routing stage A filters: `p.country_code = t.country_code OR p.country_code IS NULL`.
8. The AI system prompt is parametrised by `{{country_name}}` and `target_language`.
9. `subscriptions.payment_processor` (enum: `paddle`, `corvuspay`, `uzcard`, `kaspi`, `stripe`) — groundwork for a future ADR.

The full DDL is in B8 §5. It is applied in `docs/DB_SCHEMA.md` v1.1 by a separate brief for the research assistant.

### Impact on existing ADRs

- ADR-018 — rewritten in Chat 14 on the basis of PR #39 (B4): a universal `anti_abuse_identifier` (Turnstile + Kickbox + an opt-in €1 Paddle verification + the Lite €9 tier); for RS — `KYCProvider('apr_rs')`, for other countries — a fallback.
- ADR-022 — addendum: cluster_key includes country_code.
- ADR-023 — addendum: stage A filters by country_code.
- ADR-024 — addendum: the AI system prompt is parametrised.

### Deferrable (until an actual second country)

- The implementation of a `KYCProvider` for the second country.
- A locale pack (i18n, transliteration, formatting).
- A legal pack (ToS + Privacy in local languages).
- Multi-domain DNS strategy.
- Customer support routing per-country.
- Plugin discovery through Python entry points.
- Classification mapping between systems.
- Concrete adapters: `EisUzAdapter`, `UzCardAdapter`, `SamrukKazynaAdapter`, `KaspiAdapter`.

### Consequences

1. PR #29 (`max/universality-research`) — owner merges with Squash and merge.
2. A separate brief for the research assistant in Chat 15 — apply the DDL in `docs/DB_SCHEMA.md` v1.1 and update the architecture notes (not published) to v3.0.
3. Sprint-13 relies on the universal schema. The first code is written with `country_code='RS'` by default.

**Closes:** Q-20.

---

## ADR-034: removed from the public snapshot

## ADR-035: removed from the public snapshot

## End of DECISIONS.md
