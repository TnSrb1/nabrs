# GLOSSARY.md — A glossary of abbreviations and terms of the Tender Bot Serbia project

**Version:** 1.2 (Chat 14 — §3 PDV: 6 million → 8 million general VAT threshold; §5 Proton Mail Essentials → Plus per ADR-027; §5 Anthropic / Claude API → a link to ADR-029 tier naming)
**Purpose:** a single reference of abbreviations, acronyms and project terms. All other documents refer to this file; there is no need to duplicate the glossary in each file.

When a new abbreviation appears — add it here in the corresponding section, not in local documents.

---

## 1. Team and roles

| Abbr. | Meaning |
|---|---|
| **owner** | The founder and architect of the project. Only owner makes decisions on architecture, security-sensitive code, billing, DB migrations with DROP/ALTER, production deploy and merge into `main`. |
| **partner** | A second project partner: the legal and tax setup in Serbia. |
| **Claude** | AI assistant (Anthropic Claude) used for design, documentation and coding in branches reviewed by the owner. |

---

## 2. Technical abbreviations

| Abbr. | Meaning |
|---|---|
| **ADR** | Architecture Decision Record. Every accepted architectural decision is recorded in `docs/DECISIONS.md` as a separate ADR. |
| **API** | Application Programming Interface. |
| **CC** | Credit Card. Used in the context of “trial without CC” (without a mandatory card binding). |
| **CDN** | Content Delivery Network. For us — Cloudflare. |
| **CI/CD** | Continuous Integration / Continuous Deployment. For us — GitHub Actions. |
| **CLI** | Command-Line Interface. |
| **CPV** | Common Procurement Vocabulary — the European procurement classifier (categories of works/services/goods). After ADR-033 it is stored in the DB in the `classifications` table with the discriminator `system_code` (eu_cpv / unspsc / others). |
| **CRUD** | Create / Read / Update / Delete. |
| **DKIM** | DomainKeys Identified Mail — signing outgoing mail with a private key, verified by a DNS record. |
| **DMARC** | Domain-based Message Authentication, Reporting and Conformance — a policy on what to do with mail that fails SPF/DKIM. |
| **DNS** | Domain Name System. |
| **DSN** | Data Source Name (in our context — the Sentry DSN, the connection string for sending errors). |
| **DXA** | Twentieths of a Point — Word’s internal unit of measurement (1440 DXA = 1 inch). |
| **GUI** | Graphical User Interface. |
| **KYC** | Know Your Customer — a customer verification procedure (of us as a seller) with payment systems and messengers. |
| **LTS** | Long-Term Support (for Ubuntu — 5 years of support). |
| **MoR** | Merchant of Record — a model in which the payment company takes on taxes and compliance (our Paddle). |
| **MRR** | Monthly Recurring Revenue — a key SaaS metric. |
| **MX** | Mail Exchanger — a DNS record pointing to the domain’s mail-receiving server. |
| **NS** | Name Server — a DNS record with the zone’s authoritative servers. |
| **PAT** | Personal Access Token (GitHub). Revoked in the project — an SSH key is used. |
| **PR** | Pull Request. |
| **QA** | Quality Assurance. For us — a deterministic pytest stack without AI QA agents (see ADR-013). |
| **RAG** | Retrieval-Augmented Generation — a mode in which AI receives document fragments through search, not the whole file. Used by the GitHub connector in Claude.ai (see `docs/archive/CONNECTORS.md`). |
| **RLS** | Row-Level Security — a PostgreSQL mechanism for multi-tenant data isolation. |
| **SaaS** | Software as a Service. |
| **SKU** | Stock Keeping Unit — in our context: a separate product item (a plan, an analytics add-on, an on-demand report). |
| **SLA** | Service Level Agreement — obligations on the service level (response time, availability). |
| **SPF** | Sender Policy Framework — a DNS record with a list of servers allowed to send mail on behalf of the domain. |
| **SSH** | Secure Shell. |
| **TLD** | Top-Level Domain — `.com`, `.app`, `.rs`, etc. |
| **TLS** | Transport Layer Security (the successor of SSL). |
| **ToS** | Terms of Service. |
| **TZ** | Time Zone. The target value for the VPS is UTC. |
| **UFW** | Uncomplicated Firewall — an iptables frontend in Ubuntu. |
| **UNSPSC** | United Nations Standard Products and Services Code — an alternative procurement classifier (used by UNDP, the UN). Stored in the DB in `classifications` with `system_code='unspsc'`. |
| **VPN** | Virtual Private Network. For us — Tailscale. |
| **VPS** | Virtual Private Server. For us — the VPS provider. |
| **WAF** | Web Application Firewall. For us — Cloudflare. |

---

## 3. Serbian legal, tax and government terms

| Abbr. / term | Meaning |
|---|---|
| **DOO** | _Društvo s ograničenom odgovornošću_ — a limited liability company (the Serbian analogue of an LLC). The partner (legal entity in Serbia) already has one registered. |
| **PDV** | _Porez na dodatu vrednost_ — VAT in Serbia. **The general VAT threshold for mandatory registration is 8 million RSD** (≈ €68 000) of revenue over 12 months. **6 million RSD** is the **threshold for the flat-rate preduzetnik** regime (not the general VAT threshold). Details — `docs/BUSINESS_OPS.md` §1 (the PDV-fix coda of Chat 11 per PR #30 §7.1). |
| **PIB** | _Poreski identifikacioni broj_ — the tax identification number of a Serbian legal entity (9 digits). After ADR-033 it is stored in the DB in the generalised field `tenants.tax_id` with `country_code='RS'` and a validation regular expression in `countries.tax_id_format`. |
| **KEP** | _Kvalifikovani elektronski sertifikat_ — a qualified electronic certificate (a personal electronic signature). Needed for the authorized sections of `jnportal` and `eUprava`. Issued by Pošta Srbije and Privredna komora Srbije. |
| **APR** | _Agencija za privredne registre_ (`apr.gov.rs`) — the Serbian registrar of legal entities. Used to check a company's status. After the revision of ADR-018 (Chat 14) — one of the KYCProviders in the universal `anti_abuse_identifier` (`KYCProvider('apr_rs')`). |
| **RNIDS** | _Registar nacionalnog internet domena Srbije_ — the operator of the `.rs` zone and the Cyrillic IDN zone for Serbia. Accredits registrars. **Cloudflare Registrar is not among the accredited ones.** |
| **RSD** | The Serbian dinar (currency). |
| **EUR / €** | The euro. The project’s plans (€29 / €69 / €149) are denominated in euros, settlement with customers — through Paddle. |
| **jnportal** | `jnportal.src-ujn.example.invalid` — the state public procurement portal of Serbia. The main source of tenders for the project (phase 1). ~40k procedures a year. |
| **eUprava** | `euprava.gov.rs` — the portal of electronic government services of Serbia. Authorisation via KEP. |
| **javne nabavke** | Public procurement (Serbian). |
| **Pošta Srbije** | `posta.rs` / `pts.rs` — the Serbian Post; one of the KEP issuing centres. |
| **Privredna komora Srbije** | The Serbian Chamber of Commerce and Industry; the second KEP issuing center. |
| **CorvusPay** | A Serbian payment gateway. Enabled in phase 2 after Paddle (or as Path A2 through an Estonian OÜ at MRR ≥ €1500–3000 — see research PR #30). |
| **Zakon o zaštiti podataka o ličnosti** | The Serbian personal data protection law (~ an analogue of the GDPR). |

---

## 4. Project domain names

| Name | Purpose |
|---|---|
| **`nab.rs`** | **The primary domain of the project** (from *nabavka* — “procurement” in Serbian, see ADR-026, supersedes ADR-017). Registered by owner with the registrar Loopia DOO, active until 22.05.2027. It hosts: the landing page, the web cabinet, the API, the mailboxes `*@nab.rs` (`info@`, `support@`, `noreply@`, `billing@`, etc.), Telegram/Viber links. |

**A reserve is not registered** (see ADR-026). The historical decision from ADR-017 on the pair `tndr.rs` (primary) + `jns.rs` (reserve) is superseded; it is kept in the repo as a historical record only in `docs/DECISIONS.md` ADR-017 and `docs/archive/HANDOFF.md`.

Old variants that were considered but **not chosen:** `tendersrb.app`, `nabavke.app`, `serbtenders.com`, `tenderly.rs`.

---

## 5. Services and tools of the stack

| Service | Purpose |
|---|---|
| **Anthropic / Claude API** | AI models for analyzing terms of reference and general tasks. The architecture uses the tier naming `cheap` / `balanced` / `premium` (ADR-008 + **ADR-029**); the concrete model slugs live in `infra/config.yaml` (the runtime `tier → slug` mapping, changed without a redeploy) and in the DB (the field `tender_ai_analyses.model` stores the slug at the time of the call for audit). |
| **Paddle** | MoR billing (phase 1). Confirmed through research PR #30: accepts a sole trader without a formal business registration (Path A). |
| **CorvusPay** | A local Serbian payment gateway. Enabled in phase 2 (Path A2 through an Estonian OÜ or through the partner's DOO — Path B). |
| **Postmark** | Transactional email (sending + inbound parsing for support@). |
| **Proton Mail Plus** | Corporate mailboxes on the custom domain `nab.rs`. Phase 0 — Plus (1 mailbox + 10 aliases), phase 1 — an upgrade to Essentials (separate mailboxes for owner and partner). See **ADR-027**. |
| **Cloudflare** | DNS + CDN + WAF (Free tier). Also — Pages for the `nab.rs` landing page (ADR-031) and Web Analytics (privacy-friendly, cookieless). |
| **Tailscale** | A VPN network for access to the VPS (Free tier). |
| **Linear** | Issue-tracker (Free, workspace `TenderSRB`, team `TSR`). |
| **1Password Family** | A secrets manager (the vault “TenderSRB Production”). |
| **Sentry** | Error monitoring. |
| **Healthchecks.io** | Cron-monitoring. |
| **UptimeRobot** | External uptime monitoring. |
| **Better Stack Logs** | Centralised logs. |
| **Backblaze B2** | Cloud storage for DB backups (with age encryption). |
| **VPS provider** | VPS hosting (the Netherlands). |
| **Iubenda** | A generator of legal documents (ToS / Privacy / Cookie policy) in 3 languages. Under review after the AI drafts `docs/legal/*` (PR #18). |
| **Caddy 2** | A web server with automatic Let's Encrypt TLS. |
| **aiogram 3** | A Python library for Telegram bots. |
| **Viber Public Account** | A Viber business channel (for the priority notification delivery channel in Serbia). |
| **Pico.css** | A minimalist CSS framework for the `nab.rs` landing page (ADR-031). |

---

## 6. Chat types (per `docs/archive/CHAT_PROTOCOL.md` v2.6)

| Type | Purpose | Duration |
|---|---|---|
| **Type 1** | A long chat-sprint on one large task (Sprint-13, 14, 16, 17, 18, 21, 22) | 4-8 hours |
| **Type 2** | A day chat session, combines 2-5 small sprints | 3-5 hours |
| **Type 3** | A mini-chat for one-off questions without docs finalisation | 30-90 minutes |

---

## End of GLOSSARY.md
