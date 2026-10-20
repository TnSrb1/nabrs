/* The loading indicator of the “Ready report” (/app/analitika).
 *
 * The preset and “Show report” — a GET transition that waits for the SQL (up to statement_timeout).
 * Without the indicator the client sees that “nothing happened” and presses again: the server
 * gets identical requests in a row. Here: aria-busy on the pressed element,
 * the display of the label [data-an-busy], a repeated press is suppressed.
 * Without JS the ordinary transition remains — the page works as before.
 * An external file: inline JS is forbidden by the CSP script-src 'self'.
 */
(function () {
  "use strict";

  var busy = false;

  function setBusyLabels(show) {
    document.querySelectorAll("[data-an-busy]").forEach(function (el) {
      el.hidden = !show;
    });
  }

  function begin(el) {
    if (busy) return false;
    busy = true;
    el.setAttribute("aria-busy", "true");
    el.setAttribute("aria-disabled", "true");
    setBusyLabels(true);
    return true;
  }

  // Ctrl / ⌘ / Shift / middle button — opening in a new tab: the page does not leave, the indicator is not needed.
  document.addEventListener("click", function (e) {
    var link = e.target.closest ? e.target.closest("a[data-an-preset]") : null;
    if (!link || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    if (!begin(link)) e.preventDefault();
  });

  var form = document.querySelector("form[data-an-form]");
  if (form) {
    form.addEventListener("submit", function (e) {
      var btn = form.querySelector("[data-an-run]") || form;
      if (!begin(btn)) e.preventDefault();
    });
  }

  // Back from bfcache: the page came back with the indicator on — we remove it.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    busy = false;
    document.querySelectorAll("[aria-busy], [aria-disabled]").forEach(function (el) {
      el.removeAttribute("aria-busy");
      el.removeAttribute("aria-disabled");
    });
    setBusyLabels(false);
  });
})();
