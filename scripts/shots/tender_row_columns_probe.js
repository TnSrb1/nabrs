// A measurement of the columns of the right block of the tender rows: the x of the left edge of the deadline badge and the x of the right edge of the amount
// across all the rows (`.fd-card`). The goal of the invariant is a spread of 0 px: the badges stand in one column,
// the amounts — in another, regardless of the length of the text and the presence of an amount.
// A row without an amount and without a placeholder does not enter the spread of the amount (the counter `withoutAmount`).
// Run (the URL and the cookie — positionally; an empty cookie string for a public page):
//   docker run --rm --network host -v $PWD/scripts/shots/tender_row_columns_probe.js:/work/probe.js \
//     --entrypoint node shots:local /work/probe.js https://app.nab.rs/tenderi/cpv/09 "" 1280
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
    const cards = [...document.querySelectorAll('article.fd-card')].filter(c => c.checkVisibility());
    const badgeLeft = [];
    const badgeW = [];
    const amountW = [];
    const amountRight = [];
    let withoutAmount = 0;
    let withoutBadge = 0;
    for (const c of cards) {
      const badge = c.querySelector('.fd-card__side .badge');
      // The amount — the <p> of the right part of the row (the feed has no amount: there it is the delivery date).
      const amount = c.querySelector('.fd-card__side p');
      if (badge) {
        badgeLeft.push(Math.round(badge.getBoundingClientRect().left * 10) / 10);
        badgeW.push(Math.round(badge.getBoundingClientRect().width));
      }
      else withoutBadge++;
      // An empty amount — also in the spread: its placeholder «—» must keep the same right edge.
      if (amount) {
        amountRight.push(Math.round(amount.getBoundingClientRect().right * 10) / 10);
        if (amount.textContent.trim() !== '—') amountW.push(Math.round(amount.getBoundingClientRect().width));
      } else {
        withoutAmount++;
      }
    }
    const spread = a => (a.length ? Math.max(...a) - Math.min(...a) : null);
    const mobile = innerWidth < 640;
    return {
      rows: cards.length,
      withoutBadge,
      withoutAmount,
      badgeLeft: { min: Math.min(...badgeLeft), max: Math.max(...badgeLeft), spread: spread(badgeLeft) },
      amountRight: { min: amountRight.length ? Math.min(...amountRight) : null, max: amountRight.length ? Math.max(...amountRight) : null, spread: spread(amountRight) },
      badgeWidthMax: badgeW.length ? Math.max(...badgeW) : null,
      amountWidthMax: amountW.length ? Math.max(...amountW) : null,
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      mobile,
    };
  });
  console.log(JSON.stringify({ url, width: Number(width), ...res }, null, 1));
  await browser.close();
})();
