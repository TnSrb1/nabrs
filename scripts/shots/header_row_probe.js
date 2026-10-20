// A measurement of the cabinet/showcase header at desktop widths: overflow, the logo intersecting its neighbours, line wrapping,
// the position of the languages relative to the whoami row. Run:
//   docker run --rm --network host -v ~/shots:/out -v $PWD/scripts/shots/header_row_probe.js:/work/header_row_probe.js \
//     --entrypoint node shots:local /work/header_row_probe.js http://127.0.0.1:8793 "$COOKIE" <tag> [/app|/help]
const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie, tag, path = '/help'] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = [];
  for (const w of [390, 640, 768, 1024, 1280, 1366, 1440, 1920]) for (const lang of ['sr', 'ru']) {
    const ctx = await browser.newContext({ viewport: { width: w, height: 900 } });
    if (cookie !== '-') await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
    const page = await ctx.newPage();
    await page.goto(`${base}${path}?lang=${lang}`, { waitUntil: 'load' });
    await page.waitForTimeout(500);
    const m = await page.evaluate(() => {
      const hd = document.querySelector('.site-header');
      const brand = hd.querySelector('.brand');
      const br = brand.getBoundingClientRect();
      const hits = [];
      for (const el of hd.querySelectorAll('a, button, summary, span.lang-pill, form, .badge')) {
        if (el === brand || brand.contains(el) || el.contains(brand)) continue;
        if (!el.checkVisibility()) continue;
        const r = el.getBoundingClientRect();
        if (!r.width || !r.height) continue;
        if (r.left < br.right - 0.5 && r.right > br.left + 0.5 && r.top < br.bottom - 0.5 && r.bottom > br.top + 0.5)
          hits.push((el.className || el.tagName) + ' ' + (el.textContent || '').trim().slice(0, 20));
      }
      const ids = hd.querySelector('.site-header__identity');
      const lang = [...hd.querySelectorAll('.lang-switch')].find(e => e.checkVisibility());
      const lr = lang && lang.getBoundingClientRect();
      const mainRow = hd.querySelector('.site-header__bar').getBoundingClientRect();
      const nav = hd.querySelector('.site-header__actions');
      const rows = new Set([...nav.querySelectorAll(':scope > *')].filter(e => e.checkVisibility()).map(e => Math.round(e.getBoundingClientRect().top)));
      const rowsOfItems = [...nav.querySelectorAll(':scope > a, :scope > details, :scope > form')].filter(e => e.checkVisibility()).map(e => Math.round(e.getBoundingClientRect().top));
      return {
        scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
        headerSW: hd.scrollWidth, headerCW: hd.clientWidth,
        headerH: Math.round(hd.getBoundingClientRect().height),
        brandRect: [br.left, br.top, br.right, br.bottom].map(Math.round),
        brandHits: hits,
        langRect: lr ? [lr.left, lr.top, lr.right, lr.bottom].map(Math.round) : null,
        langInIdentity: !!(ids && lang && ids.contains(lang)),
        langAboveMainRow: lr ? lr.bottom <= mainRow.top + 1 : null,
        langRightGap: lr ? Math.round(innerWidth - lr.right) : null,
        navRowTops: [...new Set(rowsOfItems)].sort((a, b) => a - b),
        cssHeaderH: getComputedStyle(document.documentElement).getPropertyValue('--header-h').trim(),
      };
    });
    const name = `${tag}_${w}_${lang}`;
    await page.screenshot({ path: `/out/${name}.png`, clip: { x: 0, y: 0, width: w, height: Math.min(260, m.headerH + 20) } });
    out.push({ name, w, lang, ...m });
    await ctx.close();
  }
  await browser.close();
  require('fs').writeFileSync(`/out/${tag}.json`, JSON.stringify(out, null, 1));
  for (const r of out) console.log(r.name, 'sw', r.scrollWidth + '/' + r.clientWidth, 'hdr', r.headerSW + '/' + r.headerCW, 'h', r.headerH, 'hits', JSON.stringify(r.brandHits), 'lang', JSON.stringify(r.langRect), 'inIdent', r.langInIdentity, 'above', r.langAboveMainRow, 'rows', JSON.stringify(r.navRowTops));
})();
