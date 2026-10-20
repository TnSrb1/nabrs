// A measurement of the link “actual header height ↔ --header-h” (a: the “More” menu, b: the anchor, c: the limit flash).
// Run: docker run --rm --network host -v ~/shots:/out -v $PWD/scripts/shots/header_probe.js:/work/header_probe.js \
//   --entrypoint node shots:local /work/header_probe.js http://127.0.0.1:8791 "$COOKIE" <tag> [css_override_path]
const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie, tag] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = [];
  for (const [w, h] of [[320,568],[360,740],[390,844]]) for (const lang of ['sr','ru','en']) {
    const ctx = await browser.newContext({ viewport: { width: w, height: h } });
    await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
    const page = await ctx.newPage();
    await page.goto(`${base}/help?lang=${lang}`, { waitUntil: 'load' });
    await page.waitForTimeout(500);
    const name = `${tag}_${w}x${h}_${lang}`;
    const hdr = await page.evaluate(() => {
      const hd = document.querySelector('.site-header');
      return { headerH: Math.round(hd.getBoundingClientRect().height),
               identity: !!document.querySelector('.site-header__identity'),
               varH: getComputedStyle(document.documentElement).getPropertyValue('--header-h').trim(),
               varInline: document.documentElement.style.getPropertyValue('--header-h') };
    });
    // (b) anchor: take the first [id] below the header, jump by the hash, look at the top relative to the bottom of the header
    const anchor = await page.evaluate(() => {
      const cand = [...document.querySelectorAll('main [id], h2[id], h3[id]')].filter(e => e.getBoundingClientRect().height > 0 && e.offsetTop > 600);
      const el = cand[0]; if (!el) return null;
      el.scrollIntoView({ behavior: 'instant', block: 'start' }); // respects scroll-margin-top
      const hb = document.querySelector('.site-header').getBoundingClientRect().bottom;
      return { id: el.id, top: Math.round(el.getBoundingClientRect().top), headerBottom: Math.round(hb) };
    });
    if (anchor) anchor.ok = anchor.top >= anchor.headerBottom;
    await page.evaluate(() => window.scrollTo(0, 0));
    // (c) limit flash: insert the same markup as _flash_limit.html
    const flash = await page.evaluate(() => {
      const d = document.createElement('div'); d.className = 'limit-flash';
      d.innerHTML = '<p class="limit-flash__title">x</p><p class="limit-flash__body">y</p>';
      d.style.animation = 'none'; document.body.appendChild(d);
      const fb = d.getBoundingClientRect(); const hb = document.querySelector('.site-header').getBoundingClientRect().bottom;
      const r = { flashTop: Math.round(fb.top), headerBottom: Math.round(hb), ok: fb.top >= hb };
      d.remove(); return r;
    });
    // (a) menu
    await page.click('.nav-more > summary');
    await page.waitForTimeout(200);
    const reach = await page.evaluate(() => {
      const m = document.querySelector('.nav-more__menu');
      m.scrollTop = m.scrollHeight;
      const last = [...m.children].filter(e => getComputedStyle(e).display !== 'none').pop();
      const b = last.getBoundingClientRect(); const mb = m.getBoundingClientRect();
      return { last: last.textContent.trim(), menuTop: Math.round(mb.top), menuBottom: Math.round(mb.bottom), lastBottom: Math.round(b.bottom), inView: b.bottom <= innerHeight && b.top >= 0 };
    });
    await page.screenshot({ path: `/out/${name}_menu.png` });
    out.push({ name, ...hdr, anchor, flash, menu: reach });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(out));
})();
