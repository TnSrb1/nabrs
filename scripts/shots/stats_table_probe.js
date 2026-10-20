// Measurement of the /statistika tables: whether each table `[data-stats-*]` fits into its wrapper
// without horizontal scroll. The invariant — `tableW <= wrapW` (the width of the table is not more than the
// area the client sees) and `pageScrollW <= pageClientW` (the page does not slide sideways).
// The screenshot template `shot.sh` does not catch such a break: the `overflow-x-auto` wrapper hides it.
// Run (the URL and the cookie — positionally; an empty cookie string for a public page):
//   docker run --rm --network host -v $PWD/scripts/shots/stats_table_probe.js:/work/probe.js \
//     --entrypoint node shots:local /work/probe.js https://app.nab.rs/statistika "" 390
// The width — as the third argument (default 390). The output — JSON to stdout.
const { chromium } = require('/work/node_modules/playwright');

(async () => {
  const [url, cookie = '', width = '390'] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: Number(width), height: 900 } });
  if (cookie) {
    const [name, value] = cookie.split('=');
    await page.context().addCookies([{ name, value, url }]);
  }
  await page.goto(url, { waitUntil: 'load' });
  const res = await page.evaluate(() => {
    const tables = [...document.querySelectorAll('table[data-stats-months], table[data-stats-cpv], table[data-stats-proc], table[data-stats-awards], table[data-stats-customers]')];
    const out = tables.map(t => {
      const wrap = t.parentElement;
      return {
        name: [...t.attributes].find(a => a.name.startsWith('data-stats-')).name,
        tableW: Math.round(t.getBoundingClientRect().width),
        wrapW: wrap.clientWidth,
        fits: t.getBoundingClientRect().width <= wrap.clientWidth + 0.5,
      };
    });
    // The sum «€…» must not break into lines: all the rectangles of the text piece with «€»
    // (the symbol + digits and non-breaking spaces U+00A0) lie on one line (top).
    const sumLines = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const re = /€[\d\u00a0 ]+\d/g;
      let m;
      while ((m = re.exec(n.nodeValue)) !== null) {
        const r = document.createRange();
        r.setStart(n, m.index);
        r.setEnd(n, m.index + m[0].length);
        const tops = new Set([...r.getClientRects()].map(x => Math.round(x.top)));
        sumLines.push({ text: m[0], lines: tops.size });
      }
    }
    return {
      tables: out,
      sums: sumLines,
      sumsBroken: sumLines.filter(s => s.lines > 1).length,
      pageScrollW: document.documentElement.scrollWidth,
      pageClientW: document.documentElement.clientWidth,
      allFit: out.length > 0 && out.every(t => t.fits) && document.documentElement.scrollWidth <= document.documentElement.clientWidth && sumLines.every(s => s.lines === 1),
    };
  });
  await browser.close();
  console.log(JSON.stringify(res, null, 2));
})();
