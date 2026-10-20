// A browser check of the channel step (profile_form.html, the block .channel-connect) on
// the static stand scripts/shots/stand_profile_form.py: the binding buttons send
// POST to the existing routes IN A NEW TAB and do not touch the template form.
//
//   docker run --rm --network host -v ~/shots:/out \
//     -v "$PWD/scripts/shots/channel_e2e.js:/work/channel_e2e.js:ro" \
//     --entrypoint node shots:local channel_e2e.js http://127.0.0.1:8765/form_manual.html
const { chromium } = require('playwright');

(async () => {
  const url = process.argv[2];
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const posts = [];
  context.on('request', (r) => { if (r.method() === 'POST') posts.push({ url: new URL(r.url()).pathname, body: r.postData() || '' }); });
  await page.goto(url);
  const facts = {};
  // Without the wizard (the JS steps) all the sections are visible; on the stand the wizard is alive — we go to step 3.
  await page.fill('#pf-name', 'moj sablon');
  await page.click('[data-wizard-go="2"]');
  await page.click('[data-step="2"] [data-wizard-go="3"]');
  const tg = page.waitForEvent('popup');
  await page.click('button[form="pfTgLinkForm"]');
  const tgPopup = await tg;
  await tgPopup.waitForLoadState().catch(() => {});
  facts.telegramPostInNewTab = posts.some((p) => p.url === '/cabinet/telegram/link' && p.body === '');
  facts.formUrlUnchanged = page.url() === url;
  facts.templateFieldKept = (await page.$eval('#pf-name', (el) => el.value)) === 'moj sablon';
  await page.fill('#pf-ch-email', 'kupac@example.com');
  const em = page.waitForEvent('popup');
  await page.click('button[form="pfEmailLinkForm"]');
  await (await em).waitForLoadState().catch(() => {});
  const emailPost = posts.find((p) => p.url === '/cabinet/email/link');
  facts.emailPostOnlyAddress = !!emailPost && emailPost.body === 'email=kupac%40example.com';
  const sizes = await page.$$eval('.channel-connect button, .channel-connect input', (els) =>
    els.map((e) => { const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }));
  facts.controlSizes = sizes;
  facts.tapZonesOk = sizes.every(([w, h]) => w >= 32 && h >= 32);
  facts.noHorizontalScroll = await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
  await page.locator('.channel-connect').scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/out/tw2_channel_390.png' });
  console.log(JSON.stringify(facts, null, 2));
  await browser.close();
  process.exit(Object.values(facts).includes(false) ? 1 : 0);
})();
