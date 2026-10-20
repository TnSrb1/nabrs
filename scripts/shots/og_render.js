const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1200, height: 630 }, deviceScaleFactor: 1 });
  await p.goto('file:///out/og_default.html');
  await p.screenshot({ path: '/out/og.png' });
  await b.close();
})();
