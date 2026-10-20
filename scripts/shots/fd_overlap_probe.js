// An overlap of the deadline badge with the value in the rows of the feed/archive (`article.fd-card`, columns
// fd-card__side--cols): for each row — the intersection of the badge with `.fd-card__value` (px, 0 = none),
// the badge sticking out of its column (`.fd-card__deadline`), the width of the widest badge, scroll/client.
// Run (the URL and the cookie — positionally; the width — as the third argument, default 320):
//   docker run --rm --network host -v $PWD/scripts/shots/fd_overlap_probe.js:/work/probe.js:ro \
//     --entrypoint node shots:local /work/probe.js "http://127.0.0.1:8791/app/feed?actual=0" "tb_session=<cookie>" 320
// The output — JSON to stdout.
const { chromium } = require('/work/node_modules/playwright');

(async () => {
  const [url, cookie = '', width = '320'] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: Number(width), height: 900 } });
  if (cookie) {
    const [name, value] = cookie.split('=');
    await page.context().addCookies([{ name, value, url }]);
  }
  await page.goto(url, { waitUntil: 'load' });
  const res = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('article.fd-card')].filter(c => c.checkVisibility());
    const overlaps = [];
    const spill = [];
    const badgeW = [];
    const samples = [];
    let withValue = 0;
    for (const c of cards) {
      const cell = c.querySelector('.fd-card__deadline');
      const badge = cell && cell.querySelector('.badge');
      const value = c.querySelector('.fd-card__value');
      if (!badge) continue;
      const b = badge.getBoundingClientRect();
      badgeW.push(Math.round(b.width));
      const cr = cell.getBoundingClientRect();
      // Overflow: the badge is wider than its column (px beyond the right edge of the column).
      spill.push(Math.max(0, Math.round((b.right - cr.right) * 10) / 10));
      if (value) {
        withValue++;
        const v = value.getBoundingClientRect();
        const ov = Math.max(0, Math.min(b.right, v.right) - Math.max(b.left, v.left));
        overlaps.push(Math.round(ov * 10) / 10);
        if (ov > 0 && samples.length < 3) samples.push({ badge: badge.textContent.trim(), value: value.textContent.trim(), overlapPx: ov });
      }
    }
    return {
      rows: cards.length,
      withValue,
      overlapMax: overlaps.length ? Math.max(...overlaps) : null,
      overlapRows: overlaps.filter(o => o > 0).length,
      spillMax: spill.length ? Math.max(...spill) : null,
      badgeWidthMax: badgeW.length ? Math.max(...badgeW) : null,
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      samples,
    };
  });
  console.log(JSON.stringify({ url, width: Number(width), ...res }, null, 1));
  await browser.close();
})();
