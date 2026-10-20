const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie, tag] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = [];
  for (const [w, h] of [[320,568],[360,740],[390,844],[844,320],[1280,900],[1280,300]]) for (const lang of ['sr','ru','en']) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h } });
    await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
    const page = await ctx.newPage();
    await page.goto(`${base}/help?lang=${lang}`, { waitUntil: 'load' });
    await page.waitForTimeout(500);
    await page.click('.nav-more > summary');
    await page.waitForTimeout(200);
    const name = `${tag}_${w}x${h}_${lang}`;
    await page.screenshot({ path: `/out/${name}.png` });
    const r = await page.evaluate(() => {
      const m = document.querySelector('.nav-more__menu');
      const cs = getComputedStyle(m);
      const vis = [...m.children].filter(e => getComputedStyle(e).display !== 'none');
      const rects = vis.map(e => { const b = e.getBoundingClientRect(); return { t: e.textContent.trim(), top: Math.round(b.top), h: Math.round(b.height), w: Math.round(b.width), disp: getComputedStyle(e).display }; });
      const mb = m.getBoundingClientRect();
      const gaps = rects.slice(1).map((x, i) => x.top - (rects[i].top + rects[i].h));
      return { vh: innerHeight, menuTop: Math.round(mb.top), menuH: Math.round(mb.height), menuBottom: Math.round(mb.bottom), overflowY: cs.overflowY, maxH: cs.maxHeight, scrollH: m.scrollHeight, clientH: m.clientHeight, pos: cs.position, rects, gaps, left: Math.round(mb.left), right: Math.round(mb.right) };
    });
    // reachability: scroll the menu (if possible) and the page to the end, look at the bottom of the last item
    const reach = await page.evaluate(() => {
      const m = document.querySelector('.nav-more__menu');
      m.scrollTop = m.scrollHeight; window.scrollTo(0, document.body.scrollHeight);
      const last = [...m.children].filter(e => getComputedStyle(e).display !== 'none').pop();
      const b = last.getBoundingClientRect();
      return { last: last.textContent.trim(), bottom: Math.round(b.bottom), top: Math.round(b.top), inView: b.bottom <= innerHeight && b.top >= 0 };
    });
    await page.screenshot({ path: `/out/${name}_scrolled.png` });
    out.push({ name, ...r, reach, rects: r.rects.map(x => `${x.t}:${x.top}+${x.h}x${x.w}${x.disp[0]}`).join(' | ') });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(out, null, 1));
})();
