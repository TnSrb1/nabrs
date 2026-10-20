/* The customers “follow / exclude” in the profile form (0046).
 *
 * ‼️ Progressive enhancement. The source of truth — the ticked checkboxes
 * name="customers_include" / "customers_exclude" (src/api/forms.py,
 * _customers). Without this file the selected ones are visible as checkboxes: unticking =
 * removing the customer on saving. The script adds a search with suggestions
 * (GET /profiles/customer-suggest) and a × button on each chip.
 *
 * The names from the response are inserted only via textContent / value — HTML from the
 * response is not built. After each change on a checkbox — a change event:
 * on the form there is the htmx trigger of the live counter.
 *
 * ‼️ CSP script-src 'self': there are no inline handlers; the file is attached with SRI
 * (the guard — tests/api/test_design_system.py).
 */
(function () {
  'use strict';

  var DELAY_MS = 250;
  var MIN_LEN = 2;

  // The label for display: the server gives it.label (sr — in Latin, owner 10.10.2026);
  // the VALUE of the checkbox remains the exact name — the customer is compared letter by letter.
  function labelOf(it) {
    return it.label || it.name;
  }

  function keyOf(name) {
    return name.split(/\s+/).join(' ').trim().toLowerCase();
  }

  function setup(box) {
    var field = box.dataset.field;
    var list = box.querySelector('[data-cust-list]');
    var search = box.querySelector('[data-cust-search]');
    var input = search && search.querySelector('input');
    var results = box.querySelector('[data-cust-results]');
    if (!field || !list || !search || !input || !results) return;
    var removeLabel = box.dataset.removeLabel || '×';
    var timer = null;
    var seq = 0;
    var active = -1;

    function chosen() {
      var keys = {};
      Array.prototype.forEach.call(
        list.querySelectorAll('input[name="' + field + '"]'),
        function (cb) { keys[keyOf(cb.value)] = true; }
      );
      return keys;
    }

    function addRemove(li, cb, name) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cust-chip__remove';
      btn.setAttribute('aria-label', removeLabel + ': ' + name);
      btn.textContent = '×';
      btn.addEventListener('click', function () {
        cb.checked = false;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
        li.remove();
        input.focus();
      });
      li.appendChild(btn);
    }

    function addChip(name, label) {
      if (chosen()[keyOf(name)]) return;
      var li = document.createElement('li');
      li.className = 'cust-chip';
      var label = document.createElement('label');
      label.className = 'cust-chip__label';
      var cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.name = field;
      cb.value = name;
      cb.checked = true;
      var span = document.createElement('span');
      span.textContent = label || name;
      label.appendChild(cb);
      label.appendChild(document.createTextNode(' '));
      label.appendChild(span);
      li.appendChild(label);
      addRemove(li, cb, label || name);
      list.appendChild(li);
      cb.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function close() {
      results.textContent = '';
      results.classList.add('hidden');
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
    }

    function pick(name, label) {
      addChip(name, label);
      input.value = '';
      close();
      input.focus();
    }

    function highlight(i) {
      var items = results.querySelectorAll('[role="option"]');
      if (!items.length) return;
      active = (i + items.length) % items.length;
      Array.prototype.forEach.call(items, function (el, j) {
        el.setAttribute('aria-selected', j === active ? 'true' : 'false');
      });
      input.setAttribute('aria-activedescendant', items[active].id);
      items[active].scrollIntoView({ block: 'nearest' });
    }

    function render(items) {
      results.textContent = '';
      active = -1;
      var taken = chosen();
      var fresh = items.filter(function (it) { return !taken[keyOf(it.name)]; });
      if (!fresh.length) {
        var empty = document.createElement('li');
        empty.className = 'cust-results__empty';
        empty.textContent = box.dataset.nothingFound || '';
        results.appendChild(empty);
      }
      fresh.forEach(function (it, i) {
        var li = document.createElement('li');
        li.id = results.id + '-' + i;
        li.className = 'cust-results__item';
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', 'false');
        var name = document.createElement('span');
        name.textContent = labelOf(it);
        li.dataset.name = it.name;
        li.dataset.label = labelOf(it);
        var count = document.createElement('span');
        count.className = 'cust-results__count';
        count.textContent = String(it.tenders);
        li.appendChild(name);
        li.appendChild(count);
        // mousedown, not click: the blur of the field would close the list before the click.
        li.addEventListener('mousedown', function (e) {
          e.preventDefault();
          pick(it.name, labelOf(it));
        });
        results.appendChild(li);
      });
      results.classList.remove('hidden');
      input.setAttribute('aria-expanded', 'true');
    }

    function query() {
      var q = input.value.trim();
      var my = ++seq;
      if (q.length < MIN_LEN) { close(); return; }
      var url = box.dataset.suggestUrl;
      url += (url.indexOf('?') >= 0 ? '&' : '?') + 'q=' + encodeURIComponent(q);
      fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (r) { return r.ok ? r.json() : { items: [] }; })
        .then(function (data) {
          if (my === seq) render((data && data.items) || []);
        })
        .catch(function () { if (my === seq) close(); });
    }

    input.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(query, DELAY_MS);
    });
    input.addEventListener('keydown', function (e) {
      var open = !results.classList.contains('hidden');
      if (e.key === 'ArrowDown' && open) {
        e.preventDefault();
        highlight(active + 1);
      } else if (e.key === 'ArrowUp' && open) {
        e.preventDefault();
        highlight(active - 1);
      } else if (e.key === 'Enter') {
        // Enter in the form field would submit the whole template — here it selects.
        e.preventDefault();
        var items = results.querySelectorAll('[role="option"]');
        if (open && items.length) {
          var el = items[active >= 0 ? active : 0];
          pick(el.dataset.name, el.dataset.label);
        }
      } else if (e.key === 'Escape' && open) {
        e.preventDefault();
        close();
      }
    });
    input.addEventListener('blur', close);

    // The script is alive: for the already selected ones there is a × button; the hint “untick” is not needed.
    Array.prototype.forEach.call(list.querySelectorAll('li'), function (li) {
      var cb = li.querySelector('input[type="checkbox"]');
      if (cb) {
        var sp = li.querySelector('span');
        addRemove(li, cb, sp ? sp.textContent : cb.value);
      }
    });
    Array.prototype.forEach.call(
      box.querySelectorAll('[data-cust-nojs]'),
      function (el) { el.classList.add('hidden'); }
    );
    search.classList.remove('hidden');
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-cust-picker]'), setup
    );
  });
})();
