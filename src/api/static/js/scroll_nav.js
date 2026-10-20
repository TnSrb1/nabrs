/* The navigation of long pages (portal, Stage 9): the “To the top” button.
 *
 * Progressive enhancement: the page is self-sufficient; the button is an accelerator.
 * While this file has not executed (the CSP stripped it, the network did not deliver it, an old browser),
 * the button remains visibility:hidden — it is neither on the screen nor in the tab order,
 * and the markup does not change because of it.
 *
 * ‼️ prefers-reduced-motion: reduce — we scroll instantly (behavior:'auto');
 * the appearance animation in this mode is removed by the CSS.
 *
 * The scroll handler — passive + throttle via requestAnimationFrame: per frame the
 * state is recounted exactly once; the scroll does not jitter.
 */
(function () {
  "use strict";

  var btn = document.querySelector("[data-scroll-top]");
  if (!btn) return;

  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* The appearance threshold: the screen height, but not less than 600px. On short pages
     * the button does not flicker; on long ones it appears exactly when the header has gone and
     * returning by the wheel has become long. */
  function threshold() {
    return Math.max(600, window.innerHeight || 0);
  }

  var ticking = false;

  function sync() {
    ticking = false;
    var y = window.pageYOffset || document.documentElement.scrollTop || 0;
    btn.classList.toggle("is-visible", y > threshold());
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    window.requestAnimationFrame(sync);
  }

  btn.addEventListener("click", function () {
    window.scrollTo({ top: 0, left: 0, behavior: reduceMotion ? "auto" : "smooth" });
    /* We move the focus to the start of the document: otherwise a keyboard user, after the
       * scroll, would continue tabbing from the very bottom of the page. preventScroll —
       * so that the focus does not interrupt the smooth scroll already running. */
    var first = document.querySelector(".site-header .brand");
    if (first && first.focus) first.focus({ preventScroll: true });
  });

  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });

  /* The initial synchronization: reloading the page in the middle of a list (or a jump by an anchor)
     * must not leave the button hidden. */
  sync();
})();
