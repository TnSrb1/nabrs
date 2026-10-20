# shots — visual check of the interface

A screenshot of the page **and** a measurement of the invariants in one run. The metric measures only what is formulated, therefore the tool always gives both things: a PNG (to look at with the eyes) and a JSON (to check the numbers).

## Run

```bash
# from the clone of the repository; docker is available to the user claude
bash scripts/shots/shot.sh --url https://app.nab.rs/plans --lang sr \
    --out /out/plans_sr.png --json /out/plans_sr.json
```

The files land in `~/shots` on the host (inside the container it is `/out`). The image `shots:local` is built automatically on the first run and after any docker cleanup.

| Flag | Meaning |
| --- | --- |
| `--url` | the address (required) |
| `--lang sr\|ru\|en` | adds `?lang=` |
| `--width` / `--height` | the viewport, by default `390×844` (mobile); desktop — `1280 900` |
| `--cookie name=value` | can be repeated; this is how the cabinet is captured under the signed session `tb_session` |
| `--full` | a screenshot of the whole page |
| `--wait <selector>` | wait for an element |
| `--clip <selector>` | capture only the element (cropping a block); the measurements are over the whole page |
| `--click <selector>` | click after loading (the “More” menu: `.nav-more > summary`) — the measurement and a PNG with the menu open |
| `--settle <ms>` | a pause after `load`, 700 by default |
| `--idle` | wait for networkidle — ‼️ **on pages with Turnstile it never happens**, the shot will fail on timeout |

## Stands

`stand_nav.py <port>` — uvicorn with a fake DB for the cabinet/showcase header, the “More” menu, `/tenderi`, `/izvori`; prints the cookie `tb_session` for `--cookie`. For a logged-in user it draws the whoami line (email + tenant) — without it the header is measured ~38px lower than on production. `header_probe.js` — the link between the header height and `--header-h` (the “More” menu / the anchor / the limit flash), run — in the header of the file. It was used for measuring the navigation polish (390/1280 × sr/ru/en).

## What the JSON counts

- `scrollWidth` / `clientWidth` / `overflowX` + up to 20 nodes sticking out of the viewport — the invariant “the overflow is fixed by the layout”.
- `scrollHeight` / `clientHeight` / `screens` — the length of the page in CSS px and how many viewports it is. An overview screen (a dashboard, a card) tens of screens long is a defect, even when the other numbers are clean: on 04.08.2026 the cockpit with a real flow gave ~30,800 px (≈36 screens) with a completely clean report.
- `smallTargets` — interactive elements smaller than 32px. The **effective** target is measured: a field inside `<label>` is clicked by the whole row, therefore the size is taken from the label (and goes into the report as the field `effective`). `exemptTargets` — what must not be stretched: the card title links (`fd-card__title`, `fn-row__title`, `agenda__title-link`) and the links inside a sentence — a forced height breaks the rhythm of the row.
- `unlabeledFields` — rendered fields (a non-zero size — the hidden carriers of the widget data, `display:none`/a zero rect, are not counted) without `label[for]`, a wrapper and `aria-label`.
- `smallFontFields` — fields with a font <16px: iOS Safari zooms the page on focus and does not zoom back. Only the INPUT fields are counted: a checkbox and a radio do not take text and do not cause a zoom.
- `unnamedInteractive` — links and buttons without an accessible name.
- `inlineHandlers` — attributes `on*`: with the CSP `script-src 'self'` they silently do not work.
- `brokenDetails` — `<summary>` not as the first child of `<details>` (the UA hides the content entirely).
- `ruLetters` — Russian-specific letters `U+044B U+044D U+0449 U+044A U+0451 U+0439` in the visible text: the acceptance of sr/en goes by the render.
- `headerHeight` — the height of `.site-header` (compare with `--header-h`). `overflowingNodes`/`smallTargets` do not count the invisible (`checkVisibility()`): the content of a closed `<details>` earlier gave phantom findings.
- `consoleErrors`, `failedRequests`, `status`, `title`.

## Verified by fact

On 02.08.2026 on the live production the tool confirmed the findings of the audit and added what is not visible by reading the code:

- `GET /signup` without headers → `<title>` in Russian (U+0420 U+0435 U+0433 U+0438 U+0441 U+0442 U+0440 U+0430 U+0446 U+0438 U+044F…), i.e. the form in Russian on the Serbian showcase (HIGH-2 of the audit).
- the email field — without a programmatic label (HIGH-3), a 14px font (M-8);
- **the consent checkbox 13×13 px** — this was not in the audit: the tap zone is half the minimum, and a miss on it blocks the registration.

## Limits

‼️ **`--full` on a long page until 10.08.2026 made up a defect.** A screenshot of the dashboard with a real flow showed the block “Do now” empty for several screens — the content below several viewports had not been rendered at the moment of the shot. A measurement on scrolling gave 50 elements, all visible. Now `shot.js` runs the page down to the bottom before the full-page shot; if you see an “empty” block again — first check it with a viewport shot at the needed scroll, and only then open a task.

‼️ **You substitute `app.css` on the fly (checking an edit before the rollout) — take the `integrity` into account.** The SRI in the markup is computed for the old file, the browser silently discards the substituted CSS, the page is left with no styles at all — and the measurement lies with an “improvement” (the row body “grew” simply because the flex layout disappeared). A sign of a substitution that did not work: the `display` of the element became `block` instead of the expected one.

The tool shows the render and the numbers, but does not replace the eye: looking at the PNG is mandatory. The contrast is not counted here — it is checked by calculation from the tokens (see `docs/UI_SKILLS_ADAPTER.md` §4).
