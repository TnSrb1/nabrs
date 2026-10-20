/* --header-h by the ACTUAL height of the sticky header.
 *
 * CSS holds --header-h as a constant (9.1rem — the worst case "2 rows + 3 rows without
 * whoami"), but the real header of a logged-in user is 139 px (2 rows) / 183 px
 * (3 rows): the whoami row (+38 px) lies inside <header>. The scroll-margin of the anchors,
 * the max-height of the “More” menu and the top of the limit flash depend on the token, therefore
 * an underestimate hid the end of the menu at 320×568, the anchor heading and the top of the flash under
 * the header.
 *
 * Progressive enhancement: without JS (CSP, network, an old browser) the
 * CSS fallback remains. ResizeObserver catches also the change of language / width / the wrap of rows.
 * Attached synchronously right AFTER the header (a body of ~10 lines): by this moment
 * .site-header is already in the DOM; the value is set before <main> is drawn — without a jump. */
 */
(function () {
  "use strict";

  var header = document.querySelector(".site-header");
  if (!header) return;

  var root = document.documentElement;
  var last = -1;

  function apply() {
    var h = Math.ceil(header.getBoundingClientRect().height);
    if (!h || h === last) return;
    last = h;
    root.style.setProperty("--header-h", h + "px");
  }

  apply();
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(apply).observe(header);
  } else {
    window.addEventListener("resize", apply);
  }
})();
