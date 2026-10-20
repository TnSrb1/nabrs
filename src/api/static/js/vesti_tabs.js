/* The Vesti block tabs: Ctrl / ⌘ / Shift click — an ordinary link, not a block substitution.
 * htmx intercepts a click on `hx-get` in the bubbling phase at the link itself and replaces
 * the block even with a modifier; here the click is stopped earlier — in the capture phase
 * of the document. The browser opens the href itself (a new tab / window), as before the switch
 * without a reload. Without JS the href remains; the htmx tabs work as before. */
document.addEventListener('click', function (e) {
  if (!(e.ctrlKey || e.metaKey || e.shiftKey)) return;
  var tab = e.target && e.target.closest ? e.target.closest('[data-vesti-tab]') : null;
  if (tab) e.stopImmediatePropagation();
}, true);
