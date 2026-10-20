// A measurement of the cabinet header after the split of the login row and the main row (owner 11.10.2026,
// header-identity-hub): header height, intersections with the logo, overflow,
// the number of rows of the main row, the link and the badge in the login row, tap zones.
// Prints a JSON line for each pair of width × language. Run (the stand — stand_nav.py):
//   docker run --rm --network host -v $PWD/scripts/shots/header_identity_probe.js:/work/header_identity_probe.js \
//     --entrypoint node shots:local /work/header_identity_probe.js http://127.0.0.1:8801 "$COOKIE" <tag> [/help]
const { chromium } = require('/work/node_modules/playwright');
(async () => {
  const [base, cookie, tag, path = '/help'] = process.argv.slice(2);
  const browser = await chromium.launch();
  for (const w of [390, 640, 768, 1024, 1280, 1366, 1440, 1920]) for (const lang of ['sr', 'ru']) {
    const ctx = await browser.newContext({ viewport: { width: w, height: 900 } });
    await ctx.addCookies([{ name: 'tb_session', value: cookie, url: base }]);
    const page = await ctx.newPage();
    await page.goto(`${base}${path}?lang=${lang}`, { waitUntil: 'load' });
    await page.waitForTimeout(500);
    const m = await page.evaluate(() => {
      const hd = document.querySelector('.site-header');
      const vis = (el) => el.checkVisibility() && el.getBoundingClientRect().width > 0;
      const name = (el) => (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
      const brand = hd.querySelector('.brand').getBoundingClientRect();
      const overlaps = [];
      for (const el of hd.querySelectorAll('a, button, summary, form, .badge')) {
        if (!vis(el) || el.closest('.brand')) continue;
        const r = el.getBoundingClientRect();
        if (r.left < brand.right - 0.5 && r.right > brand.left + 0.5 && r.top < brand.bottom - 0.5 && r.bottom > brand.top + 0.5)
          overlaps.push(name(el));
      }
      // Main row: how many different vertical positions the visible items have (≥640px)
      const bar = hd.querySelector('nav.site-header__actions');
      const rows = [];
      for (const el of bar.querySelectorAll('a, summary, button')) {
        if (!vis(el)) continue;
        const t = Math.round(el.getBoundingClientRect().top);
        if (!rows.some((x) => Math.abs(x - t) < 4)) rows.push(t);
      }
      const barItems = [...bar.querySelectorAll('a, summary')].filter(vis).map(name);
      const barAccountLinks = [...bar.querySelectorAll('a[href="/cabinet/account"]')].filter(vis).length;
      const idRow = hd.querySelector('.site-header__identity');
      const idVisible = !!idRow && vis(idRow);
      const idLink = idRow && idRow.querySelector('a.whoami');
      const idBadge = idRow && idRow.querySelector('a[href="/cabinet/account#status"] [data-access-state], a[href="/cabinet/account#status"].badge');
      const badgeRect = idBadge && idBadge.getBoundingClientRect();
      const small = [];
      // The badge — inside the #status link: the click target is the link (the container), that is what we measure.
      for (const el0 of hd.querySelectorAll('a, button, summary, .lang-pill, .badge')) {
        const el = el0.matches('.badge') && el0.closest('a') ? el0.closest('a') : el0;
        if (!vis(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 32 || r.height < 32) small.push(`${name(el)} ${Math.round(r.width)}x${Math.round(r.height)}`);
      }
      const mobileAccount = !!hd.querySelector('.nav-mobile a[href="/cabinet/account"]');
      return {
        headerH: Math.round(hd.getBoundingClientRect().height),
        docOverflow: document.documentElement.scrollWidth - window.innerWidth,
        headerOverflow: hd.scrollWidth - hd.clientWidth,
        overlapsBrand: overlaps,
        barRows: rows.length,
        barItems,
        barAccountLinks,
        identityVisible: idVisible,
        identityLinkText: idLink ? name(idLink) : null,
        identityLinkHref: idLink ? idLink.getAttribute('href') : null,
        identityLinkTextOverflow: idLink ? idLink.scrollWidth - idLink.clientWidth : null,
        badgeText: idBadge ? name(idBadge) : null,
        badgeBox: badgeRect ? [Math.round(badgeRect.width), Math.round(badgeRect.height)] : null,
        mobileMenuAccount: mobileAccount,
        smallTargets: small,
      };
    });
    console.log(JSON.stringify({ tag, width: w, lang, path, ...m }));
    await ctx.close();
  }
  await browser.close();
})();
