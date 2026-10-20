/* The animations of the cockpit dashboard (portal, Stage 1).
 *
 * Progressive enhancement: the cockpit markup is self-sufficient — all the cards,
 * the figures and the agenda items are rendered by the server in the FINAL form. This file only
 * plays the entrance; if it did not load, the CSP stripped it or GSAP did not start
 * — the page looks and works exactly the same, just without animation.
 *
 * ‼️ prefers-reduced-motion: reduce — we exit at once, touching nothing: no
 * flicker of “first hidden, then shown”.
 *
 * We animate ONLY opacity / transform (GPU compositing, without layout thrash) and
 * we clear them via clearProps, so that after the entrance the elements do not stay with a
 * transform (otherwise position:sticky / hover states behave strangely).
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

  /* The tiles of the hero strip — a staggered entry, businesslike and short. */
  function animateCards() {
    animate("[data-cockpit-card]", {
      opacity: 0,
      y: 12,
      duration: 0.45,
      stagger: 0.06,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  /* The cockpit sections (the calendar, “do now”, the teaser) — a light appearance. */
  function animateSections() {
    animate('[data-cockpit="calendar"], [data-cockpit="todo"], [data-cockpit="teaser"]', {
      opacity: 0,
      y: 16,
      duration: 0.5,
      stagger: 0.08,
      delay: 0.1,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  /* The agenda items — a soft cascade from top to bottom.
   *
   * ‼️ The cascade step is limited by the COMMON time (`amount`), not set per element.
   * `stagger: 0.04` is multiplied by the number of items, and on the dashboard with a real
   * stream there are 202 of them: the last ones appeared 8+ seconds after loading, and the block
   * “Do now” — it comes after the agenda in the DOM — stood empty for several
   * screens. A measurement of 15.08.2026 on production: the content is in the markup (202
   * `agenda__item`), but a full-page snapshot caught them transparent.
   * `amount` distributes the same cascade over a fixed window: on a short
   * list the step is the same as before; on a long one the entrance fits into 0.6 s and does not depend
   * on the volume of data.
   */
  function animateAgenda() {
    animate("[data-agenda-item]", {
      opacity: 0,
      y: 8,
      duration: 0.35,
      stagger: { each: 0.04, amount: 0.6 },
      delay: 0.2,
      ease: EASE,
      clearProps: "opacity,transform"
    });
  }

  /* The counters (the subscription days, “in work”, the deliveries). The target number is already in the
   * markup — we read it from data-count-to and count up to the same one, therefore
   * the final state matches the server one at any outcome of the animation. */
  function animateCounters() {
    var nodes = document.querySelectorAll("[data-count-to]");
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
    animateCards();
    animateSections();
    animateAgenda();
    animateCounters();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
