// Ctrl-click on the Vesti tab opens a new tab and does NOT substitute the block (vesti_tabs.js).
// Run: docker run --rm --network host -v $PWD/scripts/shots/vesti_ctrl_probe.js:/work/vesti_ctrl_probe.js \
//   --entrypoint node shots:local /work/vesti_ctrl_probe.js http://127.0.0.1:8771 "$COOKIE"
const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie] = process.argv.slice(2);
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
  const page = await ctx.newPage();
  await page.goto(`${base}/app?lang=sr`, { waitUntil: 'load' });
  await page.waitForTimeout(500);
  const opened = [];
  ctx.on('page', async (p) => { await p.waitForURL((u) => u.toString() !== 'about:blank', { timeout: 5000 }).catch(() => {}); opened.push(p.url()); });
  await page.evaluate(() => { window.__m = 'alive'; });
  await page.click('[data-vesti-tab="market"]', { modifiers: ['Control'] });
  await page.waitForTimeout(1500);
  const marker = await page.evaluate(() => window.__m || null).catch(() => 'nav');
  console.log(JSON.stringify({ newTabs: opened, currentReloadedOrSwapped: marker !== 'alive' ? 'reloaded/navigated' : 'same-doc', marketNow: await page.evaluate(() => !!document.querySelector('[data-vesti-market]')).catch(() => 'n/a') }));
  await browser.close();
})().catch((e) => { console.error(e.name, e.message); process.exit(1); });
