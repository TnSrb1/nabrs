// A browser check of the search reset (pf_search_reset.js) on the stand
// scripts/shots/stand_feed_search.py. Return code 1 — if the check is false.
//
//   docker run --rm --network host -v ~/shots:/out \
//     -v "$PWD/scripts/shots/feed_search_e2e.js:/work/feed_search_e2e.js:ro" \
//     --entrypoint node shots:local feed_search_e2e.js http://127.0.0.1:8767 "<tb_session>"
const { chromium } = require('playwright');

(async () => {
  const base = process.argv[2];
  const cookie = process.argv[3];
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 390, height: 844 } });
  await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  const rows = () => page.$$eval('main li, main article', (els) => els.length);
  const facts = {};
  await page.goto(base + '/app/feed');
  const full = await page.$$eval('a[href^="/app/tenders/"]', (a) => a.length);
  facts.fullList = full;
  await page.fill('#pf-q', 'гориво');
  await Promise.all([page.waitForNavigation(), page.press('#pf-q', 'Enter')]);
  facts.urlAfterSubmit = page.url();
  facts.filteredLinks = await page.$$eval('a[href^="/app/tenders/"]', (a) => a.length);
  facts.clearLinkShown = !!(await page.$('.pf__clear'));
  await page.screenshot({ path: '/out/feed_q_390.png' });
  await Promise.all([page.waitForNavigation(), page.fill('#pf-q', '')]);
  facts.urlAfterClear = page.url();
  facts.linksAfterClear = await page.$$eval('a[href^="/app/tenders/"]', (a) => a.length);
  facts.inputAfterClear = await page.$eval('#pf-q', (el) => el.value);
  await page.reload();
  facts.inputAfterReload = await page.$eval('#pf-q', (el) => el.value);
  // the other filters are kept, page is reset
  await page.goto(base + '/app/feed?q=гориво&sort=deadline&page=2');
  await Promise.all([page.waitForNavigation(), page.fill('#pf-q', '')]);
  facts.urlKeepsSort = page.url();
  // without q in the URL an empty navigation field does not cause a call
  const before = page.url();
  await page.fill('#pf-q', 'x');
  await page.fill('#pf-q', '');
  await page.waitForTimeout(400);
  facts.noNavWithoutQ = page.url() === before;
  facts.errors = errors;
  console.log(JSON.stringify(facts, null, 1));
  const ok = facts.linksAfterClear === full && facts.filteredLinks < full &&
    !facts.urlAfterClear.includes('q=') && facts.inputAfterReload === '' &&
    facts.urlKeepsSort.includes('sort=deadline') && !facts.urlKeepsSort.includes('page=') &&
    !facts.urlKeepsSort.includes('q=') && facts.noNavWithoutQ && errors.length === 0;
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
