/* The keywords as chips (template_wizard, owner 30.09.2026).
 *
 * ‼️ Progressive enhancement. The source of truth — the textarea name="keywords_include"
 * in the old server format (src/api/forms.py: a line = an item, words through
 * a comma in a line = must occur together, between lines — “any”).
 * Without this file the form remains a textarea with a hint about the lines. */
 *
 * A chip = one line of the textarea. Enter or a comma adds a chip; × removes it.
 * Words that must be together are written with “+”: a chip
 * “video-nadzor + montaža” ↔ the line “video-nadzor, montaža”. The server format
 * does not change — a comma in a chip becomes a separator only here.
 *
 * After each edit the textarea gets an input event: on the form there is the
 * htmx trigger of the live counter, and the figure with the examples is recounted.
 *
 * ‼️ CSP script-src 'self': there are no inline handlers; the file is attached with SRI
 * (the guard — tests/api/test_design_system.py).
 */
(function () {
  'use strict';

  function termsOf(text) {
    return text.split('+').map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  function setup(box) {
    var area = document.getElementById(box.dataset.for);
    var input = box.querySelector('[data-kw-chips-input]');
    var list = box.querySelector('[data-kw-chips-list]');
    if (!area || !input || !list) return;
    var removeLabel = box.dataset.removeLabel || '×';
    var chips = [];

    function sync() {
      area.value = chips.map(function (terms) { return terms.join(', '); })
        .join('\n');
      area.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function render() {
      list.textContent = '';
      chips.forEach(function (terms, i) {
        var label = terms.join(' + ');
        var li = document.createElement('li');
        li.className = 'kw-chip';
        var span = document.createElement('span');
        span.textContent = label;
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'kw-chip__remove';
        btn.setAttribute('aria-label', removeLabel + ': ' + label);
        btn.textContent = '×';
        btn.addEventListener('click', function () {
          chips.splice(i, 1);
          render();
          sync();
          input.focus();
        });
        li.appendChild(span);
        li.appendChild(btn);
        list.appendChild(li);
      });
    }

    function commit(text) {
      var terms = termsOf(text);
      if (!terms.length) return false;
      chips.push(terms);
      return true;
    }

    function commitInput() {
      var added = commit(input.value);
      input.value = '';
      if (added) { render(); sync(); }
    }

    // The initial chips — from the textarea (editing a template, the package prefill).
    area.value.split('\n').forEach(function (line) {
      var terms = line.split(',').map(function (s) { return s.trim(); })
        .filter(function (s) { return s.length > 0; });
      if (terms.length) chips.push(terms);
    });
    render();

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ',') {
        // Enter in the form field would submit the whole template — here it adds a chip.
        e.preventDefault();
        commitInput();
      } else if (e.key === 'Backspace' && input.value === '' && chips.length) {
        chips.pop();
        render();
        sync();
      }
    });
    // Pasting a list “škol, hran, gradjevin” — straight into chips.
    input.addEventListener('input', function () {
      var parts = input.value.split(/[,\n]/);
      if (parts.length < 2) return;
      var rest = parts.pop();
      var added = false;
      parts.forEach(function (p) { added = commit(p) || added; });
      input.value = rest;
      if (added) { render(); sync(); }
    });
    // A typed but unconfirmed word is not lost when leaving the field
    // and when the form is saved.
    input.addEventListener('blur', commitInput);
    if (area.form) area.form.addEventListener('submit', commitInput);

    // The script is alive: the textarea is hidden, the chips and their hint are shown; the hint
    // about the lines (only the textarea needs it) is hidden. The field label leads to the chips.
    area.classList.add('hidden');
    box.classList.remove('hidden');
    var field = box.parentElement;
    Array.prototype.forEach.call(
      field.querySelectorAll('[data-kw-chips-only]'),
      function (el) { el.classList.remove('hidden'); }
    );
    Array.prototype.forEach.call(
      field.querySelectorAll('[data-kw-nojs]'),
      function (el) { el.classList.add('hidden'); }
    );
    var label = field.querySelector('label[for="' + area.id + '"]');
    if (label) label.htmlFor = input.id;
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(
      document.querySelectorAll('[data-kw-chips]'), setup
    );
  });
})();
