/* The animations of the funnel board “My tenders” (portal, Stage 2).
 *
 * Progressive enhancement: the page markup is self-sufficient — the columns, the stage counters
 * and the tender rows are rendered by the server in the FINAL form. The file only
 * plays the entrance; if it did not load, the CSP stripped it or GSAP did not start — the page
 * looks and works the same. The HTMX actions on a tender do not depend on this
 * file at all: it attaches nothing to the buttons and does not touch the actions markup.
 *
 * ‼️ prefers-reduced-motion: reduce — we exit at once, touching nothing.
 *
 * A separate file, not shared with cockpit.js: the cockpit has its own selectors and its own
 * timing, and the dashboard is not touched in this stage. A shared init is worth extracting
 * when a third page asks for the same animations.
 *
 * We animate ONLY opacity / transform and clear them via clearProps, so that after the
 * entrance no transform remains on the elements (it breaks the sticky / hover states).
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

  /* The board columns — a staggered entry from left to right, along the funnel’s course. */
  function animateColumns() {
    animate("[data-funnel-col]", {
      opacity: 0,
      y: 14,
      duration: 0.45,
      stagger: 0.07,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  /* The tender rows — a soft cascade within the columns. */
  function animateRows() {
    animate("[data-funnel] .fn-row", {
      opacity: 0,
      y: 8,
      duration: 0.35,
      stagger: 0.03,
      delay: 0.15,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  /* The stage counters. The target is already in the markup — we read it from data-count-to and
   * count up to the same one, therefore the outcome matches the server one at any result of the
   * animation (including if it was interrupted by a transition to another page). */
  function animateCounters() {
    var nodes = document.querySelectorAll("[data-funnel] [data-count-to]");
    Array.prototype.forEach.call(nodes, function (el) {
      var target = parseInt(el.getAttribute("data-count-to"), 10);
      if (!isFinite(target) || target <= 0) return;
      var state = { value: 0 };
      gsap.to(state, {
        value: target,
        duration: Math.min(1.1, 0.4 + target * 0.02),
        ease: "power2.out",
        onUpdate: function () {
          el.textContent = String(Math.round(state.value));
        },
        onComplete: function () {
          el.textContent = String(target);
        }
      });
    });
  }

  function init() {
    animateColumns();
    animateRows();
    animateCounters();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
