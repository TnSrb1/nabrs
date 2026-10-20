// A browser check of the customers “follow / exclude” (customer_picker.js, 0046)
// on the static stand scripts/shots/stand_profile_form.py (the edit page
// form_edit.html, the hint — the static file of the stand). It prints JSON with the facts,
// puts the screenshots into /out. Return code 1 — if at least one check is false.
//
//   docker run --rm --network host -v ~/shots:/out \
//     -v "$PWD/scripts/shots/customers_e2e.js:/work/customers_e2e.js:ro" \
//     --entrypoint node shots:local customers_e2e.js http://127.0.0.1:8765/form_edit.html
const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  // Network errors — with the address: the live counter (/profiles/preview-count) on the
  // static stand does not answer by definition, everything else is a defect.
  page.on('response', (r) => { if (r.status() >= 400) errors.push(`${r.status()} ${r.url()}`); });
  await page.goto(url);
  await page.evaluate(() => {
    window.__changes = 0;
    document.getElementById('profileForm')
      .addEventListener('change', () => { window.__changes += 1; });
  });
  const inc = '[data-cust-picker][data-field="customers_include"]';
  const values = (field) => page.$$eval(
    `#profileForm input[name="${field}"]`,
    (els) => els.filter((e) => e.type === 'hidden' || e.checked).map((e) => e.value),
  );
  const facts = {};
  facts.searchVisible = await page.$eval(`${inc} [data-cust-search]`, (el) => getComputedStyle(el).display !== 'none');
  facts.nojsHintHidden = await page.$eval(`${inc} [data-cust-nojs]`, (el) => getComputedStyle(el).display === 'none');
  facts.initialInclude = await values('customers_include');

  await page.locator(`${inc} [data-cust-search]`).scrollIntoViewIfNeeded();
  await page.fill('#pf-customers_include-search', 'elek');
  await page.waitForSelector('#pf-customers_include-results [role="option"]');
  facts.expanded = await page.$eval('#pf-customers_include-search', (el) => el.getAttribute('aria-expanded'));
  // An already chosen customer is not repeated in the hint.
  facts.options = await page.$$eval('#pf-customers_include-results [role="option"] span:first-child', (els) => els.map((e) => e.textContent));
  await page.screenshot({ path: '/out/pf_customers_suggest_390.png' });
  const optBox = await page.$eval('#pf-customers_include-results [role="option"]', (el) => {
    const r = el.getBoundingClientRect(); return { w: r.width, h: r.height };
  });
  facts.optionTapZoneOk = optBox.h >= 32;

  await page.press('#pf-customers_include-search', 'ArrowDown');
  await page.press('#pf-customers_include-search', 'Enter');
  facts.enterDidNotSubmit = page.url() === url;
  facts.afterPick = await values('customers_include');
  facts.listClosed = await page.$eval('#pf-customers_include-results', (el) => el.classList.contains('hidden'));

  const before = (await values('customers_include')).length;
  await page.click(`${inc} .cust-chip__remove`);
  facts.removeDropsOne = (await values('customers_include')).length === before - 1;
  const box = await page.$eval(`${inc} .cust-chip__remove`, (el) => {
    const r = el.getBoundingClientRect(); return { w: r.width, h: r.height };
  });
  facts.removeTapZoneOk = box.w >= 32 && box.h >= 32;
  facts.changeEventsToForm = await page.evaluate(() => window.__changes);
  facts.noHorizontalScroll = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  facts.pageErrors = errors.filter((e) => !/preview-count/.test(e));
  await page.screenshot({ path: '/out/pf_customers_after_390.png', fullPage: true });
  await browser.close();

  const ok = facts.searchVisible && facts.nojsHintHidden && facts.expanded === 'true'
    && facts.initialInclude.filter(Boolean).length === 1
    && !facts.options.includes(facts.initialInclude.filter(Boolean)[0])
    && facts.options.length === 2 && facts.optionTapZoneOk && facts.enterDidNotSubmit
    && facts.afterPick.filter(Boolean).length === 2 && facts.listClosed
    && facts.removeDropsOne && facts.removeTapZoneOk && facts.changeEventsToForm >= 2
    && facts.noHorizontalScroll && facts.pageErrors.length === 0;
  console.log(JSON.stringify({ ok, facts }, null, 2));
  process.exit(ok ? 0 : 1);
})();
