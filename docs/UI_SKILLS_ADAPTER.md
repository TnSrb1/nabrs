# UI_SKILLS_ADAPTER — how to apply `.claude/skills/better-*` in TenderSRB

v1.0 · 02.08.2026 · author: owner

The `better-*` skills are written for an abstract frontend: Tailwind with a build step, React, `framer-motion`, OKLCH tokens. **We have none of this.** This document translates their rules into ours, so that a review does not produce findings that physically cannot be applied and a fix does not break an invariant.

Top-level rule: **the skills give WHAT to fix, this file — HOW to fix it.** When they disagree, the internal project rules win.

---

## 1. Order of work (mandatory)

1. **An audit is read-only only.** `/better-interface full <screen>` does not edit code. The result is a table of findings with `file:line`.
2. **Screening by value** (the rule `check-value-before-work`): is the phenomenon present in the production render → does the customer see it → is it our layer. A finding “from reading code” not confirmed by the render is marked `Not verified` and does not go into a brief.
3. **Only then** — a task brief for an implementer as a table “file:line → what it was → what to replace with” and a PR through the usual cycle (gate → review → merge).
4. **Do not mix** the audit and the fix in one run: a mixed run gives “fixed what it found” without screening by value.

---

## 2. Translation table: a skill rule → our constraint

| The skill suggests | With us | What to do |
| --- | --- | --- |
| Add a Tailwind utility class (`active:scale-[0.96]`, `transition-transform`, `after:size-11`) | **`app.css` is a static prebuild, there is NO build step.** Checked 02.08: `active:scale-*` and `transition-transform` are **absent** from the subset, `tabular-nums` (11), `focus-visible` (29), `line-clamp` (1), `text-wrap` (2) are present | First `grep` the class in `src/api/static/css/app.css`. If absent — write your own rule in the custom block at the top of `app.css`, do not invent a utility. The guard is `tests/quality/test_css_template_coverage.py` |
| Editing `app.css` | The SRI hash is written in `base.html` **and** `public_base.html` | If you changed CSS → recompute sha384 in both layouts; after the rollout compare the hash of the served CSS with the `integrity` in the served HTML |
| `framer-motion` / `motion`, `AnimatePresence`, `initial={false}` | There is no JS framework. There is htmx, GSAP (vendored, with SRI), own files in `src/api/static/js/` | Animations are CSS transitions or GSAP from the vendor already included. Do not introduce new dependencies |
| An inline handler, `onclick`, `onsubmit="return confirm(...)"` | **CSP `script-src 'self'` — inline silently does NOT run** | Confirmations — `data-confirm` (`confirm_dialogs.js`); behavior — htmx attributes or a vendored file with SRI |
| Convert colors to OKLCH, generate a palette | The palette **#1f3760 / #c07f1a is fixed**, tokens in hex | We use `better-colors` **only** as a measurer (WCAG/APCA render pairs) and as a map of color roles. Do not migrate the notation — the skill itself forbids it (principle 1: “Respect the existing system”) |
| Color roles “one role — one meaning” | Already canonised in the project rules: gray = expired · orange `--status-urgent` = urgency only · blue `--status-new` = time in reserve and the match score · amber = action/upsell · `.badge-muted` = metadata | A finding is valid if a new meaning was hung on a taken color. Changing the token itself is forbidden |
| Hit area 24×24 (WCAG AA) / 44×44 touch | Our invariant: **from 2rem**, mobile navigation **2.75rem**; the card title links (`fd-card__title`, `fn-row__title`) are deliberately NOT touched — a forced height breaks the list rhythm | Buttons and badges — yes; text rows — no. ‼️ If you change sizes — **measure `scrollWidth` vs `clientWidth`**: the tap-target fix of 25.07 widened the header from 490 to 510 at viewport 390 |
| Replace copy/labels (“Verb-first buttons”, “Errors say how to fix”) | Three languages, catalogs: the public site `src/api/public/i18n.py` (default sr) · the cabinet `src/api/cabinet/i18n.py` (default ru) · shared vocabulary `src/i18n_common.py` · emails `src/i18n_email.py` · storefront data — the column `template_library.i18n` | Text is edited **in the catalog, in all three languages at once**, key parity is under test. Do not put raw Cyrillic into a template (a lint guard). State the fallback explicitly, not via a module `DEFAULT` |
| A uniform capitalisation policy, “one word — one meaning” | The shared bot ↔ cabinet ↔ CSV vocabulary lives in `src/i18n_common.py` | Check a term edit with a predicate over ALL consumers (`COMMON`, the bot, exports): the export had already diverged from the cabinet (#455) |
| Rewrite tender/description text | **Content is “as is” in the source language**, translation only on a pressed button | Copywriting touches only the chrome, never tender data |
| `<label for>`, `autocomplete`, `aria-invalid`, focus on the first erroneous field | Forms: `signup.html`, `login.html`, `resend_verification.html`, `profile_form.html`, admin ones | A valid zone, apply as written. Error texts — through `AUTH_MSG` in the catalog, not in the template |
| 16px inputs on mobile (iOS zoom) | Fonts are **system** (`@font-face` = 0), sizes from the subset | Check by fact in the render; the fix is a rule in the custom block of `app.css` |
| `<summary>`/`<details>`, progressive disclosure | ‼️ `<summary>` must be the **first child** of `<details>` — otherwise the UA hides the content entirely (a real defect of the public card) | Check on any edit of disclosure widgets |
| Dark theme, checking pairs in both schemes | The product has **no** dark theme | Findings about dark mode are out of scope, mark them `Not applicable` |
| RTL, logical properties | Languages ru/sr/en, RTL is not supported | `margin-inline-*` is welcome as hygiene, but RTL findings are not prioritized |

---

## 3. The stop list (never do, even if a skill advises)

- Change the brand colors `#1f3760` / `#c07f1a` and introduce OKLCH notation.
- Add frontend dependencies, CDN links, inline scripts.
- Edit `app.css` without recomputing the SRI in **both** base layouts.
- Introduce text strings bypassing the i18n catalogs (and the other two languages).
- Touch `src/ai/prompts.py` — it is behavioral code (PR only as a draft + A/B).
- Force a height on card title links.
- Put an interactive element inside `<a>` — a link row and a button are incompatible (#464).
- Extend the i18n scope to the admin — it is operator-facing and stays ru.

---

## 4. How to verify a finding by FACT (our method instead of the skill’s “Verification”)

The skill requires looking at the render. With us this is done as follows (recipes — `docs/NEXT_SESSION_BRIEF.md` §4, the memory `visual-check-headless`):

- **The render, not the template.** A headless screenshot in docker (`shots:local`), for the cabinet — under an issued signed session; for languages — `locale=sr|en`.
- **Overflow.** `scrollWidth` vs `clientWidth` at viewport 390 — a mandatory measurement after any size edit.
- **Contrast.** A calculation from tokens (WCAG): small text ≥ 4.5, non-text indicators ≥ 3.0; readable text — from `--color-neutral-500`, neutrals 400/300 only lines and fills.
- **A metric measures only what is formulated.** “0 Russian letters” coexisted with a triple duplicate of a tag — after the rollout LOOK at the screenshot, not only at the number.
- **The place, not the path to it.** A feature visible in several blocks is fixed with a predicate over ALL render points (`grep -rn` over the partial/macro).

---

## 5. What counts as HIGH with us

The skill’s scale (`HIGH` = blocks a task / hides content / a system failure) is supplemented by our defect classes:

1. **“Looks fine but does not work”** — an inline handler under CSP, `<summary>` not the first child, an htmx action without an indicator, a button inside `<a>`.
2. **A promise without an implementation** — the UI/plan promises what the worker cannot do (the most valuable class, memory `promise-without-implementation`).
3. **A link/action leading to a 404 or 303** — check by hitting the route, not by reading code (#463).
4. **PII in logs and on screen** — the subscriber’s address/phone.
5. The rest — by the skill’s scale.

---

## 6. A screen map for a staged audit

Run one stage per run.

| Stage | Screen / templates | Why it matters |
| --- | --- | --- |
| 1. First contact | `landing.html`, `plans.html`, `signup.html`, `signup_result.html`, `login.html`, `resend_verification.html`, `public_base.html` | The public entry, sign-up is decided here; the default language is sr |
| 2. The public card | `tender.html`, `legal.html`, `help.html` | Indexing and the first display of value; the `<details>` defect lived here |
| 3. The cabinet cockpit | `dashboard.html`, `_cockpit_hero.html`, `_cockpit_todo.html`, `_agenda_item.html`, `_calendar.html`, `_cockpit_feed_teaser.html` | The subscriber’s daily screen |
| 4. Working with tenders | `feed_archive.html`, `_feed_card.html`, `tender_detail.html`, `my_tenders.html`, `_my_tender_row.html`, `_funnel_board.html` | The main working cycle |
| 5. Setup and money | `profile_form.html`, `profile_detail.html`, `subscription.html`, `telegram_link.html`, `email_panel.html` | Onboarding, payment, delivery |
| 6. The admin | `admin_*.html` | Operator-facing; owner’s criterion “convenient and clear” applies here too, but **we do not touch i18n** |

---

## 7. What the skills give us beyond the current canon

An honest list — it is why they were vendored at all (the rest is already written in the project rules):

- **Concentric radii** (`outer = inner + padding`) — we had no rule.
- **Shadows for elevation, borders for structure** — a criterion for when a `border` is superfluous.
- **`transition: all` is forbidden**, list properties explicitly.
- **Optical alignment** of icons instead of geometric.
- **A line measure of 60–75 characters** and `text-wrap: balance/pretty` — we had nothing about line length.
- **`tabular-nums` on changing numbers** (amounts, deadlines, counters) — the class is in the subset, applied haphazardly.
- **`prefers-reduced-motion`** — a mandatory wrapper for the portal’s GSAP animations.
- **Empty states “orient and lead onward”** — we have a single `_empty_state.html` for all cases.
- **Links describe the destination** (“Learn more” ×N on a page is a defect).
- **A toggle is named by its on state.**
