/* Resetting the search in the filters of the feed /app/feed and the portal /tenderi.
 *
 * The search form is GET without JS: clearing the field (including the native × of type=search)
 * did not send a request, and the list stayed filtered by the old ?q=. Here:
 * the field became empty, while the URL had a non-empty q → we go to the current URL without q
 * and without page (the other filters are kept). There is NO live search on input.
 * Without JS the same is given by a ×-link next to the field (`.pf__clear`).
 * An external file: inline JS is forbidden by the CSP script-src 'self'.
 */
(function () {
  "use strict";
  var input = document.getElementById("pf-q");
  if (!input) return;

  function reset() {
    if (input.value.trim() !== "") return;
    var url = new URL(window.location.href);
    var q = url.searchParams.get("q");
    if (q === null || q.trim() === "") return;
    url.searchParams.delete("q");
    url.searchParams.delete("page");
    window.location.assign(url.toString());
  }

  // NOT on "input": deleting the query with Backspace to type a new one would take the page
  // away in the middle of typing (the owner’s review 02.10). "search" — the native × and Esc,
  // "change" — the field stayed empty after the focus left (Firefox without "search").
  input.addEventListener("search", reset);
  input.addEventListener("change", reset);
})();
