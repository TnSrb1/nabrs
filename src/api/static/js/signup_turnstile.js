// Turnstile on /signup: the submit button is blocked until the widget’s callback.
// The callbacks are global functions (data-callback refers to the name); the file is external:
// the CSP `script-src 'self'` does not let an inline one in.
// Degradation: without JS the button is active (there is no disabled in the markup), and the server still
// checks the token; if the widget did not load within TS_WAIT_MS (a blocker, the network) —
// we unblock the button, so that the form does not become dead forever.
(function () {
  var TS_WAIT_MS = 8000;
  var btn = null;
  var wait = null;  // the "checking" label inside aria-live: visible while the button is disabled
  var done = false;

  function setBlocked(blocked) {
    if (!btn) return;
    btn.disabled = blocked;
    // app.css is the prebuilt one with SRI; there is no :disabled rule in it — the visible state is here.
    btn.style.opacity = blocked ? '0.55' : '';
    if (wait) wait.hidden = !blocked;
  }

  window.tsrbTurnstileOk = function () { done = true; setBlocked(false); };
  window.tsrbTurnstileReset = function () { done = false; setBlocked(true); };
  // A failure of the widget itself: we do not block; the server decides.
  window.tsrbTurnstileError = function () { done = true; setBlocked(false); };

  document.addEventListener('DOMContentLoaded', function () {
    var widget = document.querySelector('.cf-turnstile');
    btn = document.querySelector('form[data-turnstile-form] button[type="submit"]');
    wait = document.getElementById('turnstile-wait');
    if (!widget || !btn) return;
    setBlocked(!done);  // the callback may have arrived before DOMContentLoaded — do not overwrite success
    setTimeout(function () { if (!done) setBlocked(false); }, TS_WAIT_MS);
  });
})();
