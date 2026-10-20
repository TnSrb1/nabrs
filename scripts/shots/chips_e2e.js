// A browser check of the keyword chips (keyword_chips.js) on the static
// stand scripts/shots/stand_profile_form.py. It prints JSON with the facts and
// puts a 390px screenshot into /out. Return code 1 — if at least one check is false.
//
//   docker run --rm --network host -v ~/shots:/out \
//     -v "$PWD/scripts/shots/chips_e2e.js:/work/chips_e2e.js:ro" \
//     --entrypoint node shots:local chips_e2e.js http://127.0.0.1:8765/form_package.html
const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(url);
  await page.evaluate(() => {
    window.__inputs = 0;
    document.getElementById('pf-kw-inc')
      .addEventListener('input', () => { window.__inputs += 1; });
  });
  const area = () => page.$eval('#pf-kw-inc', (el) => el.value);
  const chips = () => page.$$eval('.kw-chip span', (els) => els.map((e) => e.textContent));
  const facts = {};
  const initialLines = (await area()).split('\n').filter(Boolean).length;
  facts.textareaHidden = await page.$eval('#pf-kw-inc', (el) => getComputedStyle(el).display === 'none');
  facts.chipsVisible = await page.$eval('[data-kw-chips]', (el) => getComputedStyle(el).display !== 'none');
  facts.initialChipsEqualLines = (await chips()).length === initialLines && initialLines > 0;
  facts.labelPointsToChipInput = await page.$eval('label[for="pf-kw-chip-input"]', () => true).catch(() => false);
  facts.nojsHintHidden = await page.$eval('[data-kw-nojs]', (el) => getComputedStyle(el).display === 'none');

  await page.fill('#pf-kw-chip-input', 'škola');
  await page.press('#pf-kw-chip-input', 'Enter');
  facts.enterAddsChip = (await chips()).at(-1) === 'škola' && (await area()).split('\n').at(-1) === 'škola';
  facts.enterDidNotSubmit = page.url() === url;

  await page.type('#pf-kw-chip-input', 'video-nadzor + montaža,');
  facts.plusChipLabel = (await chips()).at(-1) === 'video-nadzor + montaža';
  facts.plusChipSerialized = (await area()).split('\n').at(-1) === 'video-nadzor, montaža';

  const before = (await chips()).length;
  const firstLine = (await area()).split('\n')[0];
  await page.click('.kw-chip__remove');
  facts.removeDropsChip = (await chips()).length === before - 1 && !(await area()).split('\n').includes(firstLine);

  await page.fill('#pf-kw-chip-input', 'hran, skol, gradjevin');
  facts.pasteSplits = (await chips()).slice(-2).join('|') === 'hran|skol' &&
    (await page.$eval('#pf-kw-chip-input', (el) => el.value)) === ' gradjevin';
  await page.click('h1');  // blur — what was typed is not lost
  facts.blurCommits = (await chips()).at(-1) === 'gradjevin';

  facts.inputEventsToForm = await page.evaluate(() => window.__inputs);
  const box = await page.$eval('.kw-chip__remove', (el) => {
    const r = el.getBoundingClientRect();
    return { w: r.width, h: r.height };
  });
  facts.removeTapZone = box;
  facts.removeTapZoneOk = box.w >= 32 && box.h >= 32;
  facts.noHorizontalScroll = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  facts.pageErrors = errors;
  await page.locator('[data-kw-chips]').scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/out/tw2_chips_390.png' });
  console.log(JSON.stringify(facts, null, 2));
  await browser.close();
  const bad = Object.entries(facts).filter(([, v]) => v === false);
  process.exit(bad.length || errors.length ? 1 : 0);
})();
