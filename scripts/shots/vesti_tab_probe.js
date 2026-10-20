// A measurement of the tabs of the Vesti block: a click on «Tržište» — without a full reload and without “blinking”.
// It writes the TRAJECTORY of scrollY after the click (polled from Node, survives the navigation): the base version
// reloads /app, the page goes to the top (scrollY=0) and rolls back by a smooth anchor
// to the block — and that is the “blinking”. The mark on window — a sign of a reload.
// Start: block — the block is in the visibility zone (as for the client); top — the page from the top.
// Run: docker run --rm --network host -v $PWD/scripts/shots/vesti_tab_probe.js:/work/vesti_tab_probe.js \
//   --entrypoint node shots:local /work/vesti_tab_probe.js http://127.0.0.1:8771 "$COOKIE" <tag>
const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie, tag] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = [];
  for (const [w, h] of [[390, 844], [1280, 900]]) for (const start of ['block', 'top']) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h } });
    await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
    const page = await ctx.newPage();
    await page.goto(`${base}/app?lang=sr`, { waitUntil: 'load' });
    await page.waitForTimeout(500);
    if (start === 'block') {
      // html { scroll-behavior: smooth } (app.css:2277): we set the position instantly.
      await page.evaluate(() => {
        const top = document.getElementById('vesti').getBoundingClientRect().top + window.scrollY;
        window.scrollTo({ top: top - 120, behavior: 'instant' });
      });
      await page.waitForTimeout(400);
    }
    await page.evaluate(() => { window.__noReloadMarker = 'alive'; });
    const startY = await page.evaluate(() => Math.round(window.scrollY));
    // A synthetic click: without Playwright scrolling to the element (it would shift the page itself).
    await page.evaluate(() => document.querySelector('[data-vesti-tab="market"]').dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })));
    const ys = [];
    for (let i = 0; i < 50; i++) {
      const y = await page.evaluate(() => Math.round(window.scrollY)).catch(() => null);
      if (y !== null) ys.push(y);
      await new Promise((r) => setTimeout(r, 50));
    }
    const after = await page.evaluate(() => ({
      marker: window.__noReloadMarker || null,
      url: location.pathname + location.search + location.hash,
      marketBlock: !!document.querySelector('[data-vesti-market]'),
      ariaCurrent: document.querySelector('[data-vesti-tab][aria-current="true"]')?.dataset.vestiTab || null,
      focused: document.activeElement?.dataset?.vestiTab || document.activeElement?.tagName || null,
    })).catch(() => ({ marker: null, url: null, marketBlock: null, ariaCurrent: null, focused: null }));
    out.push({
      viewport: `${w}x${h}`, start, startY,
      reloaded: after.marker !== 'alive',
      minY: Math.min(...ys), maxY: Math.max(...ys), finalY: ys[ys.length - 1],
      // A compressed trajectory: only the changes of the value (the first 12).
      trajectory: ys.filter((v, i) => i === 0 || v !== ys[i - 1]).slice(0, 12),
      url: after.url, marketBlock: after.marketBlock, ariaCurrent: after.ariaCurrent, focused: after.focused,
    });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify({ tag, results: out }, null, 2));
})().catch((e) => { console.error(e.name, e.message); process.exit(1); });
