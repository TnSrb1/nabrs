/* The entrance animation of the archive of matches “All matches” (portal, Stage 3).
 *
 * Progressive enhancement: the page is rendered by the server in the FINAL form —
 * the sections, the cards, the urgency badges and the pagination. The file only plays the
 * appearance; if it did not load, the CSP stripped it or GSAP did not start — the list
 * is fully functional and clickable exactly the same.
 *
 * ‼️ prefers-reduced-motion: reduce — we exit at once, touching nothing.
 *
 * A separate file, not shared with my_tenders.js: there the board columns are animated, and
 * the stage counters; here — a flat stream of cards with a different timing. A shared
 * init is worth extracting when two pages ask for ONE animation, not
 * merely when both are animated.
 *
 * We animate ONLY opacity / transform and clear them via clearProps, so that
 * after the entrance no transform remained on the cards (it breaks the hover states).
 */
(function () {
  "use strict";

  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduceMotion) return;

  var gsap = window.gsap;
  if (!gsap) return;

  var EASE = "power3.out";

  function animate(selector, vars) {
    var nodes = document.querySelectorAll(selector);
    if (!nodes.length) return;
    gsap.from(nodes, vars);
  }

  /* The headings of the urgency sections — earlier than the cards, so that the eye has time to
   * catch on them before the list moves. */
  function animateGroups() {
    animate(".fd-group__title", {
      opacity: 0,
      y: 8,
      duration: 0.35,
      stagger: 0.08,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  /* The cards — a soft cascade from top to bottom. The stagger is small: on the page there are up to
   * 20 (FEED_PAGE_SIZE); at 0.1s the last one would appear two seconds later. */
  function animateCards() {
    animate("[data-feed-card]", {
      opacity: 0,
      y: 10,
      duration: 0.4,
      stagger: 0.035,
      delay: 0.1,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  function init() {
    animateGroups();
    animateCards();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
