// A measurement of the dashboard teaser (`[data-cockpit="teaser"]`): the left edge of the deadline badge and the gap between
// the badge and the value «Isporučeno dd.mm» in the right block of the row. The goal: the spread of the left edge ≤1 px
// (the badges in one column) and the overlap of the badge with the value = 0 px.
// Run (the URL and the cookie — positionally; an empty cookie string for a public page):
//   docker run --rm --network host -v $PWD/scripts/shots/dash_teaser_probe.js:/work/probe.js \
//     --entrypoint node shots:local /work/probe.js http://127.0.0.1:8781/app "tb_session=<cookie>" 1280
// The width — as the third argument (default 1280). The output — JSON to stdout.
const { chromium } = require('/work/node_modules/playwright');

(async () => {
  const [url, cookie = '', width = '1280'] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: Number(width), height: 900 } });
  if (cookie) {
    const [name, value] = cookie.split('=');
    await page.context().addCookies([{ name, value, url }]);
  }
  await page.goto(url, { waitUntil: 'load' });
  const res = await page.evaluate(() => {
    const rows = [...document.querySelectorAll('[data-cockpit="teaser"] ul > li')].filter(r => r.checkVisibility());
    const badgeLeft = [];
    const badgeW = [];
    const overlaps = [];
    let withoutBadge = 0;
    for (const r of rows) {
      // The right block of the row — the last direct child of li (the left one — .dash-feed__main).
      const side = r.querySelector(':scope > div:last-child');
      const badge = side && side.querySelector('.badge');
      // The value — the last span of the right block (the delivery date); before the class fix there is none — we take the text.
      const value = side && [...side.querySelectorAll('span')].find(el => !el.classList.contains('badge') && el.textContent.trim() && !el.classList.contains('fd-card__deadline'));
      if (!badge) { withoutBadge++; continue; }
      const b = badge.getBoundingClientRect();
      badgeLeft.push(Math.round(b.left * 10) / 10);
      badgeW.push(Math.round(b.width));
      if (value) {
        const v = value.getBoundingClientRect();
        // The horizontal overlap: how many px of the badge climb onto the value (0 = they do not).
        overlaps.push(Math.max(0, Math.round((Math.min(b.right, v.right) - Math.max(b.left, v.left)) * 10) / 10));
      }
    }
    const spread = a => (a.length ? Math.round((Math.max(...a) - Math.min(...a)) * 10) / 10 : null);
    return {
      rows: rows.length,
      // The width of the widest badge in the right block (px) — to compare with the deadline column.
      badgeWidthMax: Math.max(...badgeW),
      withoutBadge,
      badgeLeft: { min: Math.min(...badgeLeft), max: Math.max(...badgeLeft), spread: spread(badgeLeft) },
      valueOverlapMax: overlaps.length ? Math.max(...overlaps) : null,
      valueRows: overlaps.length,
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    };
  });
  console.log(JSON.stringify({ url, width: Number(width), ...res }, null, 1));
  await browser.close();
})();
