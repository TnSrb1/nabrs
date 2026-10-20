/* The template creation wizard: three steps of one form.
 *
 * ‼️ Progressive enhancement, not a replacement of the form. Without this file (JS is off,
 * the CSP stripped the script, the file did not arrive) the page remains the same questionnaire: all
 * the sections are expanded, the “Next / Back” buttons are hidden by the .hidden class right in the
 * markup, and submit works. The steps are sections of ONE form and one POST /profiles;
 * there is no intermediate state on the server.
 *
 * ‼️ CSP script-src 'self': inline handlers cannot be here; the file
 * is attached with SRI (the guard — tests/api/test_design_system.py).
 */
(function () {
  'use strict';

  var LAST_STEP = 3;

  /* The search fields of the CPV widget do not affect the selection (they have no name), but their `input`
     bubbles up to the form, and the form has the htmx trigger of the live counter. Without
     this every letter in the search over the reference would start a full recount of
     the 30-day window. We stop the bubbling, not the request: a filter of the htmx trigger
     would require eval, which the CSP does not allow. */
  function muteNonFilterInputs() {
    var muted = document.querySelectorAll('[data-no-preview]');
    Array.prototype.forEach.call(muted, function (el) {
      el.addEventListener('input', function (e) { e.stopPropagation(); });
    });
  }

  function setupWizard() {
    var form = document.getElementById('profileForm');
    if (!form || form.dataset.wizard !== '1') return;
    var steps = form.querySelectorAll('[data-step]');
    if (!steps.length) return;
    var nav = document.getElementById('wizardNav');
    var navItems = nav ? nav.querySelectorAll('[data-nav-step]') : [];
    var submitRow = form.querySelector('[data-form-submit]');
    var reduced = window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function show(n) {
      Array.prototype.forEach.call(steps, function (section) {
        section.classList.toggle('hidden', section.dataset.step !== String(n));
      });
      Array.prototype.forEach.call(navItems, function (item) {
        var current = item.dataset.navStep === String(n);
        item.classList.toggle('wizard-nav__item--active', current);
        item.classList.toggle(
          'wizard-nav__item--done', Number(item.dataset.navStep) < n
        );
        if (current) item.setAttribute('aria-current', 'step');
        else item.removeAttribute('aria-current');
      });
      // The template can be saved only from the last step — otherwise the “Save” button
      // is visible next to “Next” and reads as two different ways to continue.
      if (submitRow) submitRow.classList.toggle('hidden', n !== LAST_STEP);
      form.dataset.wizardStep = String(n);
    }

    // The step buttons and the progress strip appear ONLY when the script is alive.
    var actions = form.querySelectorAll('[data-wizard-actions]');
    Array.prototype.forEach.call(actions, function (row) {
      row.classList.remove('hidden');
    });
    if (nav) nav.classList.remove('hidden');

    var buttons = form.querySelectorAll('[data-wizard-go]');
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener('click', function () {
        show(Number(btn.dataset.wizardGo));
        window.scrollTo({ top: 0, behavior: reduced ? 'auto' : 'smooth' });
      });
    });

    show(1);
  }

  document.addEventListener('DOMContentLoaded', function () {
    muteNonFilterInputs();
    setupWizard();
  });
})();
