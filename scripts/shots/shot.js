// Screenshot of a page + measurement of the invariants of the TenderSRB interface.
//
// The metric measures only what is formulated, therefore the tool does both
// things at once: it puts a PNG (to look at with the eyes) and prints a JSON with the measurements
// which are recorded for us as invariants — the overflow by width, tap zones,
// Russian letters in a non-Russian render, inline handlers under CSP, <summary>
// not as the first child, interactive elements without an accessible name.
//
// Usage (via scripts/shots/shot.sh):
//   --url <address>          required
//   --out <file.png>         where to put the screenshot (default /out/shot.png)
//   --json <file.json>       where to put the measurements (default only stdout)
//   --width <n> --height <n> the viewport (default 390x844 — mobile)
//   --lang <sr|ru|en>        adds ?lang= to the address
//   --cookie name=value      can be repeated (the signed session of the cabinet)
//   --full                   a screenshot of the whole page, not only the viewport
//   --wait <selector>        wait for the selector before the shot
//   --clip <selector>        capture only this element (cropping a block, not the page)
//   --click <selector>       click the element after loading (open the “More” menu
//                            — `.nav-more > summary`), the measurement and the PNG already with the open
//   --settle <ms>            pause after load before the shot (default 700)
//   --idle                   wait for networkidle instead of load; ‼️ on pages with
//                            Turnstile the network silence never comes — do not enable
//   --timeout <ms>           navigation timeout (default 30000)

const { chromium } = require("playwright");
const fs = require("fs");

function parseArgs(argv) {
  const args = { cookies: [], width: 390, height: 844, timeout: 30000, settle: 700 };
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    const next = () => argv[(i += 1)];
    if (key === "--url") args.url = next();
    else if (key === "--out") args.out = next();
    else if (key === "--json") args.json = next();
    else if (key === "--width") args.width = Number(next());
    else if (key === "--height") args.height = Number(next());
    else if (key === "--lang") args.lang = next();
    else if (key === "--cookie") args.cookies.push(next());
    else if (key === "--wait") args.wait = next();
    else if (key === "--click") args.click = next();
    else if (key === "--clip") args.clip = next();
    else if (key === "--timeout") args.timeout = Number(next());
    else if (key === "--settle") args.settle = Number(next());
    else if (key === "--idle") args.idle = true;
    else if (key === "--full") args.full = true;
    else throw new Error(`Неизвестный аргумент: ${key}`);
  }
  if (!args.url) throw new Error("--url обязателен");
  args.out = args.out || "/out/shot.png";
  return args;
}

// The measurements run in the context of the page: only the render, no assumptions
// from the sources. We return the raw numbers — the interpretation is on the side of the caller.
function collect() {
  const doc = document.documentElement;

  const overflowingNodes = [];
  document.querySelectorAll("body *").forEach((el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    // The content of a closed <details> (the “More” menu) is in the layout, but not visible.
    if (!el.checkVisibility()) return;
    if (rect.right > doc.clientWidth + 1 || rect.left < -1) {
      overflowingNodes.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.getAttribute("class") || "").slice(0, 80),
        left: Math.round(rect.left),
        right: Math.round(rect.right),
      });
    }
  });

  // Tap zones. Our invariant is from 2rem (32px), the navigation on mobile 2.75rem
  // (44px). The card title links are deliberately excluded: a forced height
  // breaks the vertical rhythm of the list (CLAUDE.md, the section “Frontend”).
  //
  // ‼️ One must measure the EFFECTIVE target, not the element. Two cases, on which
  // on 02.08 the tool nearly produced a phantom task:
  //   1) a checkbox inside <label> — the whole row is clicked (verified by a click on
  //      the text: checked became true), therefore the target = the rectangle of the label;
  //   2) a link INSIDE a sentence — it must not be stretched, this would break the rhythm
  //      of the row exactly as the card titles do. Such go into
  //      exempt, not into violations: otherwise every run demands “fix” the
  //      thing that must not be fixed, and the report stops being read.
  const TITLE_EXEMPT = ["fd-card__title", "fn-row__title", "agenda__title-link"];
  // Classes where cls.includes() would give false positives on OTHER elements —
  // here only the EXACT match of the class attribute (the audit of 03.08):
  //   - "font-medium hover:underline" — the teaser title of a card/profile
  //     (_CARD_TITLE_VIEWS["teaser"], dashboard.html the profile link): the same
  //     situation as fd-card__title/fn-row__title, but a substring match would catch
  //     OTHER CTA links with extra classes ("text-sm text-brand font-medium
  //     hover:underline shrink-0" — “All matches”/“My tenders”), which are
  //     not described by this rule;
  //   - “text-slate-600 underline” — a standalone secondary “back” link
  //     (the only child of its <p>: /cabinet/telegram «Detailed help
  //     →», /app/tenders/{id} “← To the feed” and the similar ones on message/signup_result/
  //     telegram_link/email_panel) — stretching them to 2rem is forbidden for the same
  //     reason as the card titles: it breaks the compact rhythm of these screens.
  const TITLE_EXEMPT_EXACT = ["font-medium hover:underline", "text-slate-600 underline"];
  const smallTargets = [];
  const exemptTargets = [];
  const selector =
    'a[href], button, input:not([type="hidden"]), select, textarea, summary, [role="button"], [role="tab"]';
  // A link is considered inline if its own text is next to it in the parent.
  const isInlineInProse = (el) => {
    if (el.tagName.toLowerCase() !== "a") return false;
    if (getComputedStyle(el).display !== "inline") return false;
    const parent = el.parentElement;
    if (!parent) return false;
    const own = Array.from(parent.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => n.textContent.trim())
      .join("");
    return own.length > 0;
  };
  document.querySelectorAll(selector).forEach((el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    if (!el.checkVisibility()) return;
    // A field inside <label> is clicked through the label — the target is by it.
    const wrap = el.closest("label");
    const target = wrap && wrap !== el ? wrap.getBoundingClientRect() : rect;
    if (target.height >= 32 && target.width >= 32) return;
    const cls = el.getAttribute("class") || "";
    const item = {
      tag: el.tagName.toLowerCase(),
      cls: cls.slice(0, 80),
      text: (el.textContent || "").trim().slice(0, 40),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
    };
    if (target !== rect) {
      item.effective = { w: Math.round(target.width), h: Math.round(target.height) };
    }
    if (
      TITLE_EXEMPT.some((c) => cls.includes(c)) ||
      TITLE_EXEMPT_EXACT.includes(cls.trim()) ||
      isInlineInProse(el)
    ) {
      exemptTargets.push(item);
    } else {
      smallTargets.push(item);
    }
  });

  // An interactive element without an accessible name: empty text and no aria-label/title.
  const unnamed = [];
  document.querySelectorAll('a[href], button, [role="button"]').forEach((el) => {
    const text = (el.textContent || "").trim();
    const label = el.getAttribute("aria-label") || el.getAttribute("title") || "";
    const labelledby = el.getAttribute("aria-labelledby");
    if (!text && !label && !labelledby) {
      unnamed.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.getAttribute("class") || "").slice(0, 80),
        href: el.getAttribute("href") || "",
      });
    }
  });

  // Input fields without a programmatic label (label[for], a wrapper, aria-label).
  // Unrendered fields (zero size — the same criterion as above for
  // overflowingNodes/smallTargets) do not go into the measurement: a hidden data carrier
  // of a widget (`<textarea class="hidden">`) is invisible and does not require a label —
  // otherwise the measurement raises a false alarm (04.08, cls_include/cls_exclude).
  const unlabeledFields = [];
  document
    .querySelectorAll('input:not([type="hidden"]):not([type="submit"]), select, textarea')
    .forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      const id = el.getAttribute("id");
      const hasFor = id && document.querySelector(`label[for="${CSS.escape(id)}"]`);
      const wrapped = el.closest("label");
      const aria = el.getAttribute("aria-label") || el.getAttribute("aria-labelledby");
      if (!hasFor && !wrapped && !aria) {
        unlabeledFields.push({
          name: el.getAttribute("name") || "",
          type: el.getAttribute("type") || el.tagName.toLowerCase(),
        });
      }
    });

  // iOS Safari zooms the page if the font of the field in focus is smaller than 16px. This is about
  // the INPUT fields: a checkbox and a radio do not take text and do not cause a zoom — their
  // size is measured by the tap zone above, not by the font (otherwise the consent row
  // would hang in the report forever).
  const smallFontFields = [];
  const TYPELESS = new Set(["checkbox", "radio", "range", "color", "file", "submit", "button"]);
  document.querySelectorAll('input:not([type="hidden"]), select, textarea').forEach((el) => {
    if (TYPELESS.has((el.getAttribute("type") || "").toLowerCase())) return;
    const size = parseFloat(getComputedStyle(el).fontSize);
    if (size && size < 16) {
      smallFontFields.push({
        name: el.getAttribute("name") || "",
        fontSize: size,
      });
    }
  });

  // CSP script-src 'self' — an inline handler silently does not execute.
  const inlineHandlers = [];
  document.querySelectorAll("*").forEach((el) => {
    for (const attr of el.attributes || []) {
      if (attr.name.startsWith("on")) {
        inlineHandlers.push({
          tag: el.tagName.toLowerCase(),
          attr: attr.name,
          value: attr.value.slice(0, 60),
        });
      }
    }
  });

  // <summary> must be the FIRST child of <details>, otherwise the UA hides the content.
  const brokenDetails = [];
  document.querySelectorAll("details").forEach((el) => {
    const first = el.firstElementChild;
    if (!first || first.tagName.toLowerCase() !== "summary") {
      brokenDetails.push({ cls: (el.getAttribute("class") || "").slice(0, 80) });
    }
  });

  // Russian-specific letters in the visible text: the acceptance of sr/en goes by the RENDER.
  const visibleText = document.body.innerText || "";
  const ruLetters = (visibleText.match(/[ыэщъёЫЭЩЪЁ]/g) || []).length;

  // ‼️ The length of the page. The metric measures only what is formulated: on 04.08.2026
  // the cockpit grew to ~30,800 CSS px (≈36 mobile screens in a row), and the report was
  // at the same time CLEAN — no overflow, tap zones are normal, no errors. The defect
  // “the page has stopped being an overview” is visible only by eye, therefore the number
  // we give raw: screens = how many viewports one has to scroll through.
  const screens = doc.clientHeight
    ? Number((doc.scrollHeight / doc.clientHeight).toFixed(1))
    : null;

  return {
    title: document.title,
    headerHeight: (() => {
      const h = document.querySelector(".site-header");
      return h ? Math.round(h.getBoundingClientRect().height) : null;
    })(),
    scrollWidth: doc.scrollWidth,
    clientWidth: doc.clientWidth,
    scrollHeight: doc.scrollHeight,
    clientHeight: doc.clientHeight,
    screens,
    overflowX: doc.scrollWidth > doc.clientWidth,
    overflowingNodes: overflowingNodes.slice(0, 20),
    smallTargets,
    exemptTargets,
    unnamedInteractive: unnamed,
    unlabeledFields,
    smallFontFields,
    inlineHandlers,
    brokenDetails,
    ruLetters,
    visibleTextLength: visibleText.length,
  };
}

(async () => {
  const args = parseArgs(process.argv.slice(2));
  const url = args.lang
    ? `${args.url}${args.url.includes("?") ? "&" : "?"}lang=${args.lang}`
    : args.url;

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: args.width, height: args.height },
    deviceScaleFactor: 2,
  });

  if (args.cookies.length) {
    const { hostname } = new URL(url);
    await context.addCookies(
      args.cookies.map((raw) => {
        const eq = raw.indexOf("=");
        return {
          name: raw.slice(0, eq),
          value: raw.slice(eq + 1),
          domain: hostname,
          path: "/",
        };
      })
    );
  }

  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 200));
  });
  const failedRequests = [];
  page.on("requestfailed", (req) => {
    failedRequests.push(`${req.method()} ${req.url().slice(0, 120)}`);
  });

  // ‼️ networkidle on pages with Turnstile never comes: the widget
  // Cloudflare keeps background requests, and the shot fails on timeout. We wait for
  // `load`, and the network silence — only by the explicit --idle.
  const response = await page.goto(url, {
    waitUntil: args.idle ? "networkidle" : "load",
    timeout: args.timeout,
  });
  if (args.wait) await page.waitForSelector(args.wait, { timeout: args.timeout });
  if (args.click) await page.click(args.click, { timeout: args.timeout });
  await page.waitForTimeout(args.settle);

  // ‼️ fullPage on a LONG page gives pieces of white canvas: the content below
  // several viewports has not yet been rendered at the moment of the shot, and the picture looks
  // as if the block were empty. On 10.08.2026 this cost a false defect: on the screenshot
  // of the dashboard the block “Do now” was empty for several screens, while a measurement on
  // scrolling showed 50 elements, all visible. A tool that INVENTS a defect
  // is worse than the absence of a tool — we run the page to the bottom so that
  // everything gets rendered, and only then we shoot.
  if (args.full) {
    await page.evaluate(async () => {
      const step = Math.round(window.innerHeight * 0.9);
      for (let y = 0; y < document.documentElement.scrollHeight; y += step) {
        window.scrollTo(0, y);
        await new Promise((r) => requestAnimationFrame(() => setTimeout(r, 60)));
      }
      window.scrollTo(0, 0);
      await new Promise((r) => setTimeout(r, 150));
    });
  }

  if (args.clip) {
    await page.locator(args.clip).first().screenshot({ path: args.out });
  } else {
    await page.screenshot({ path: args.out, fullPage: Boolean(args.full) });
  }

  const measured = await page.evaluate(collect);
  const report = {
    url,
    status: response ? response.status() : null,
    viewport: { width: args.width, height: args.height },
    screenshot: args.out,
    consoleErrors,
    failedRequests,
    ...measured,
  };

  if (args.json) fs.writeFileSync(args.json, JSON.stringify(report, null, 2));
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);

  await browser.close();
})().catch((err) => {
  process.stderr.write(`${err.stack || err}\n`);
  process.exit(1);
});
