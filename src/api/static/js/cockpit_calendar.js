/* The two-way link “date ↔ tenders” in the cockpit calendar (portal, Stage 7).
 *
 * The widget is assembled by the server and without this file it remains fully functional: today’s
 * day is highlighted by the markup (`is-today` + `aria-current="date"`), the agenda is visible
 * in full. Here — only the interactivity on top.
 *
 * ‼️ Resolving the click conflict. An agenda item is a LINK to a tender, and a click on
 * it must still open the tender. Therefore the directions of the link are separated
 * by different events:
 *   date → tenders : click / Enter / Space on a grid cell (the cell has no
 *                    transition of its own — the click is free);
 *   tender → date  : mouseenter / focusin on an item (the transition is not touched at all).
 *
 * ‼️ The keyboard — roving tabindex. If tabindex="0" were given to all 35–42 cells
 * of the month, a keyboard user would get fifty extra stops before
 * the agenda. Therefore in the tab order there is exactly ONE cell (today, then the selected one),
 * and inside the grid one moves with the arrows / Home / End.
 *
 * ‼️ The button role, tabindex and aria-label are set here, not in the template: without JS
 * the cell does nothing, and declaring it a button would be a lie for
 * the screen reader and the keyboard.
 */
(function () {
  "use strict";

  var root = document.querySelector('[data-cockpit="calendar"]');
  if (!root) return;

  /* Dashboard v2: the mini-calendar lives in the right column and is collapsed into <details>
   * below 1024px (on the desktop the block is open, the summary is hidden by CSS). We close it here, and
   * not by an attribute in the markup: without JS the block stays open and hides nothing. */
  if (
    root.tagName === "DETAILS" &&
    window.matchMedia &&
    window.matchMedia("(max-width: 1023px)").matches
  ) {
    root.open = false;
  }

  var grid = root.querySelector(".cal__grid");
  var cells = root.querySelectorAll("[data-cal-day][data-date]");
  if (!grid || !cells.length) return;

  var note = root.querySelector("[data-cal-note]");
  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* The date comes from our own markup, but it reaches the selector as a string —
   * we let in only a strict ISO day, without escaping and surprises. */
  var ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

  function each(nodes, fn) {
    Array.prototype.forEach.call(nodes, fn);
  }

  /* The agenda (ROKOVI) — in ANOTHER column, not inside the root: we look for the items across the whole
   * document. The items of the other widgets of the page do not carry the data-agenda-item attribute. */
  function itemsFor(date) {
    return document.querySelectorAll(
      '[data-agenda-item][data-date="' + date + '"]'
    );
  }

  function cellFor(date) {
    return root.querySelector('[data-cal-day][data-date="' + date + '"]');
  }

  function showNote(text) {
    if (!note) return;
    note.textContent = text;
    note.hidden = false;
  }

  function hideNote() {
    if (!note) return;
    note.hidden = true;
    note.textContent = "";
  }

  /* -- date → tenders -------------------------------------------------------- */

  function select(cell) {
    var date = cell.getAttribute("data-date");
    if (!ISO_DAY.test(date)) return;

    each(cells, function (c) {
      c.classList.remove("is-selected");
      c.setAttribute("aria-pressed", c === cell ? "true" : "false");
      /* Roving tabindex: only the selected cell stays in the tab order. */
      c.tabIndex = c === cell ? 0 : -1;
    });
    each(document.querySelectorAll("[data-agenda-item]"), function (item) {
      item.classList.remove("is-highlighted");
    });
    cell.classList.add("is-selected");

    var matched = itemsFor(date);
    if (!matched.length) {
      /* The date has events, but the agenda window (7 days) does not show them —
       * we say so, rather than “there are no events”: the client sees the dots on the grid. */
      if (cell.classList.contains("has-events") && note && note.getAttribute("data-cal-far")) {
        showNote(note.getAttribute("data-cal-far"));
        return;
      }
      var label = (cell.getAttribute("data-cal-label") || "").split(",")[0];
      showNote((label || "Эта дата") + " — событий нет.");
      return;
    }
    hideNote();
    var revealed = false;
    each(matched, function (item) {
      /* ‼️ The item may lie in the collapsed tail of the agenda (“Show N more”,
       * _agenda_list.html). We expand EXPLICITLY: otherwise a click on a day highlights
       * something invisible, and for the client the widget simply does not work. */
      var more = item.closest && item.closest("[data-agenda-more]");
      if (more && !more.open) {
        more.open = true;
        revealed = true;
      }
      item.classList.add("is-highlighted");
    });
    /* The item just expanded we bring to the CENTRE, not to “the nearest edge”: when
     * the tail is expanded the page grows by thousands of px, and `nearest` left the
     * row flush against the bottom edge (measurement: 841…907 at a viewport of 900 —
     * highlighted, but cut off). An ordinary click on a visible item does not change the behavior:
     * the extra jumps of the page are annoying there. */
    matched[0].scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: revealed ? "center" : "nearest"
    });
  }

  /* -- tender → date (peek; the link transition is not touched) -------------------- */

  function peek(item, on) {
    var date = item.getAttribute("data-date");
    if (!ISO_DAY.test(date)) return;
    var cell = cellFor(date);
    if (!cell) return; // the event is outside the current month — nothing to highlight
    cell.classList.toggle("is-peek", on);
  }

  /* -- initialization --------------------------------------------------------- */

  function upgradeCells() {
    grid.setAttribute("role", "group");
    grid.setAttribute("aria-label", "Сетка месяца — выберите дату");
    each(cells, function (cell, i) {
      cell.setAttribute("role", "button");
      cell.setAttribute("aria-pressed", "false");
      cell.setAttribute("aria-label", cell.getAttribute("data-cal-label") || "");
      cell.tabIndex = -1;
      cell.setAttribute("data-cal-index", String(i));
    });
    /* The single cell in the tab order — today, otherwise the first one in the grid. */
    var entry = root.querySelector('[data-cal-day][aria-current="date"]') || cells[0];
    entry.tabIndex = 0;
  }

  function moveFocus(from, delta) {
    var i = parseInt(from.getAttribute("data-cal-index"), 10);
    if (!isFinite(i)) return;
    var next = cells[Math.min(cells.length - 1, Math.max(0, i + delta))];
    if (!next || next === from) return;
    from.tabIndex = -1;
    next.tabIndex = 0;
    next.focus();
  }

  var STEPS = {
    ArrowLeft: -1,
    ArrowRight: 1,
    ArrowUp: -7,
    ArrowDown: 7
  };

  function onKeydown(event) {
    var cell = event.target.closest && event.target.closest("[data-cal-day]");
    if (!cell) return;
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
      event.preventDefault(); // otherwise space would scroll the page
      select(cell);
      return;
    }
    if (Object.prototype.hasOwnProperty.call(STEPS, event.key)) {
      event.preventDefault();
      moveFocus(cell, STEPS[event.key]);
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      moveFocus(cell, event.key === "Home" ? -cells.length : cells.length);
    }
  }

  function init() {
    upgradeCells();

    grid.addEventListener("click", function (event) {
      var cell = event.target.closest && event.target.closest("[data-cal-day]");
      if (cell) select(cell);
    });
    grid.addEventListener("keydown", onKeydown);

    /* mouseenter / mouseleave do not bubble — we attach them to the items themselves; focusin /
     * focusout do bubble, but then the handler is the same one for the mouse and the keyboard. */
    each(document.querySelectorAll("[data-agenda-item][data-date]"), function (item) {
      item.addEventListener("mouseenter", function () {
        peek(item, true);
      });
      item.addEventListener("mouseleave", function () {
        peek(item, false);
      });
      item.addEventListener("focus", function () {
        peek(item, true);
      });
      item.addEventListener("blur", function () {
        peek(item, false);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
