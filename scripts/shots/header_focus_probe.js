// The focus outline of the language in the cabinet header: by Tab we reach .site-header__identity .lang-pill and
// check whether the overflow of the whoami row container clips it (owner 11.10.2026).
// overflow:hidden on the row clips the outline 2px + offset 2px below the bottom edge of the pill.
// Run (red — with the old overflow:hidden injected, green — as in app.css):
//   docker run --rm --network host -v ~/shots:/out -v $PWD/scripts/shots/header_focus_probe.js:/work/header_focus_probe.js \
//     --entrypoint node shots:local /work/header_focus_probe.js http://127.0.0.1:8794 "$COOKIE" <tag> [/help] [old]
const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie, tag, path = '/help', mode = 'new'] = process.argv.slice(2);
  const browser = await chromium.launch();
  const out = [];
  for (const w of [1024, 1920]) {
    const ctx = await browser.newContext({ viewport: { width: w, height: 900 } });
    await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
    const page = await ctx.newPage();
    await page.goto(`${base}${path}?lang=ru`, { waitUntil: 'load' });
    if (mode === 'old') await page.addStyleTag({ content: '.site-header__identity{overflow:hidden !important}' });
    // Tab through the document up to the first language pill in the identity row.
    let hit = false;
    for (let i = 0; i < 40 && !hit; i++) {
      await page.keyboard.press('Tab');
      hit = await page.evaluate(() => !!document.activeElement.closest('.site-header__identity .lang-pill'));
    }
    const m = await page.evaluate(() => {
      const el = document.activeElement;
      const ident = document.querySelector('.site-header__identity');
      const r = el.getBoundingClientRect();
      const ir = ident.getBoundingClientRect();
      const ov = getComputedStyle(ident).overflow;
      // outline 2px + offset 2px = 4px beyond the pill; clipping — if the bottom of the outline goes under the edge of the row
      const outlineBottom = r.bottom + 4;
      return {
        focused: el.textContent.trim(), focusVisible: el.matches(':focus-visible'),
        identOverflow: ov, pillBottom: Math.round(r.bottom), identBottom: Math.round(ir.bottom),
        outlineBottom: Math.round(outlineBottom), outlineClipped: ov !== 'visible' && outlineBottom > ir.bottom + 0.5,
        outlineStyle: getComputedStyle(el).outlineStyle + ' ' + getComputedStyle(el).outlineWidth,
      };
    });
    const name = `${tag}_focus_${w}`;
    await page.screenshot({ path: `/out/${name}.png`, clip: { x: 0, y: 0, width: w, height: 110 } });
    out.push({ name, w, hit, ...m });
    await ctx.close();
  }
  await browser.close();
  console.log(JSON.stringify(out, null, 1));
})();
