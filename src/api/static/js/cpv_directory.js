/* /cpv-kodovi: search over all CPV codes (cpv_codes.json) on the client.
   The label by the page language — the same rule as cpv_directory.py::label_of:
   sr — l_sr in Latin, ru — l_ru, otherwise the English l with a mark.
   The counters and the set of links — from the JSON block #cpv-dir-data. Without inline handlers (CSP). */
(function () {
  'use strict';
  var root = document.getElementById('cpv-dir');
  var input = document.getElementById('cpv-dir-q');
  if (!root || !input) return;
  var box = document.getElementById('cpv-dir-results');
  var list = document.getElementById('cpv-dir-list');
  var status = document.getElementById('cpv-dir-status');
  var ds = root.dataset;
  var lang = ds.lang;
  var LIMIT = 100;
  var meta = JSON.parse(document.getElementById('cpv-dir-data').textContent);
  var counts = meta.n || {};
  var groups = new Set(meta.g || []);
  var divisions = new Set(meta.d || []);

  var LAT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'ђ': 'đ', 'е': 'e', 'ж': 'ž',
    'з': 'z', 'и': 'i', 'ј': 'j', 'к': 'k', 'л': 'l', 'љ': 'lj', 'м': 'm', 'н': 'n',
    'њ': 'nj', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'ћ': 'ć', 'у': 'u',
    'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'č', 'џ': 'dž', 'ш': 'š'
  };
  function cyrToLat(text) {
    var out = '';
    for (var i = 0; i < text.length; i++) {
      var ch = text.charAt(i);
      var low = ch.toLowerCase();
      var lat = LAT[low];
      if (lat === undefined) { out += ch; continue; }
      if (ch === low) { out += lat; continue; }
      if (lat.length === 1) { out += lat.toUpperCase(); continue; }
      var nxt = text.charAt(i + 1);
      var prv = i ? text.charAt(i - 1) : '';
      var shout = (nxt && nxt !== nxt.toLowerCase()) || (prv && prv !== prv.toLowerCase());
      out += shout ? lat.toUpperCase() : lat.charAt(0).toUpperCase() + lat.slice(1);
    }
    return out;
  }
  function fold(text) {
    return text.toLowerCase().replace(/đ/g, 'd').normalize('NFD').replace(/[̀-ͯ]/g, '');
  }
  function labelOf(row) {
    if (lang === 'sr' && row.l_sr) return { text: cyrToLat(row.l_sr), en: false };
    if (lang === 'ru' && row.l_ru) return { text: row.l_ru, en: false };
    return { text: row.l || '', en: lang !== 'en' };
  }

  var rows = null;
  var byCode = {};
  var children = {};
  function prepare(data) {
    rows = data;
    rows.forEach(function (r) {
      byCode[r.c] = r;
      // The search runs over any language: sr-Latin, ru and en in one string.
      r.h = fold(cyrToLat(r.l_sr || '') + ' ' + (r.l_ru || '') + ' ' + (r.l || ''));
      if (r.p) (children[r.p] = children[r.p] || []).push(r.c);
    });
  }

  function listHref(code) {
    var g = code.slice(0, 3);
    if (groups.has(g)) return '/tenderi/cpv/' + g;
    var d = code.slice(0, 2);
    if (divisions.has(d)) return '/tenderi/cpv/' + d;
    return null;
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function badge(parent, lab) {
    if (!lab.en) return;
    var a = el('abbr', 'text-xs text-slate-500', ' ' + ds.enMark);
    a.title = ds.enTitle;
    parent.appendChild(a);
  }
  function jump(code) {
    return function () {
      input.value = code;
      run();
      input.focus();
    };
  }
  function codeButton(code) {
    var b = el('button', 'btn-ghost text-sm', code);
    b.type = 'button';
    b.addEventListener('click', jump(code));
    return b;
  }

  function renderRow(r) {
    var li = el('li', 'py-3');
    var head = el('div', 'cd-head');
    head.appendChild(el('span', 'font-mono text-slate-500', r.c));
    var lab = labelOf(r);
    head.appendChild(el('span', 'text-slate-900', lab.text));
    badge(head, lab);
    var n = counts[r.c];
    if (n) head.appendChild(el('span', 'text-slate-500', '(' + ds.tendersN.replace('{n}', n) + ')'));
    li.appendChild(head);

    var meta2 = el('div', 'cd-meta');
    var href = listHref(r.c);
    if (href) {
      var a = el('a', 'cd-link', ds.listLink);
      a.href = href;
      meta2.appendChild(a);
    }
    if (r.p && byCode[r.p]) {
      var pl = labelOf(byCode[r.p]);
      meta2.appendChild(el('span', 'text-slate-500', ds.parent + ':'));
      meta2.appendChild(codeButton(r.p));
      meta2.appendChild(el('span', 'text-slate-500', pl.text));
    }
    li.appendChild(meta2);

    var kids = children[r.c];
    if (kids && kids.length) {
      var det = el('details', 'mt-1 text-sm');
      det.appendChild(el('summary', 'cd-sum cursor-pointer text-slate-600', String(kids.length)));
      var ul = el('ul', 'mt-1 space-y-1');
      kids.slice(0, 50).forEach(function (c) {
        var k = el('li');
        k.appendChild(codeButton(c));
        k.appendChild(el('span', 'text-slate-600', ' ' + labelOf(byCode[c]).text));
        ul.appendChild(k);
      });
      det.appendChild(ul);
      li.appendChild(det);
    }
    return li;
  }

  function search(raw) {
    var q = raw.trim();
    if (!q) return null;
    if (/^[0-9][0-9\s\-]*$/.test(q)) {
      var digits = q.replace(/\D/g, '');
      return rows.filter(function (r) { return r.c.indexOf(digits) === 0; });
    }
    var variants = [fold(q)];
    var asLat = fold(cyrToLat(q));
    if (asLat !== variants[0]) variants.push(asLat);
    var tokenSets = variants.map(function (v) { return v.split(/\s+/).filter(Boolean); });
    var hits = rows.filter(function (r) {
      return tokenSets.some(function (toks) {
        return toks.every(function (t) { return r.h.indexOf(t) !== -1; });
      });
    });
    var first = tokenSets[0][0] || '';
    hits.sort(function (a, b) {
      var sa = a.h.indexOf(first) === 0 ? 0 : 1;
      var sb = b.h.indexOf(first) === 0 ? 0 : 1;
      return sa - sb || (counts[b.c] || 0) - (counts[a.c] || 0) || (a.c < b.c ? -1 : 1);
    });
    return hits;
  }

  function run() {
    var hits = search(input.value);
    list.textContent = '';
    if (hits === null) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    if (!hits.length) {
      status.textContent = ds.noResults;
      return;
    }
    hits.slice(0, LIMIT).forEach(function (r) { list.appendChild(renderRow(r)); });
    status.textContent = hits.length > LIMIT
      ? ds.more.replace('{shown}', LIMIT).replace('{total}', hits.length)
      : String(hits.length);
  }

  // ?q=… — a ready query by a link (and for a measurement); the search starts after the directory has loaded.
  var preset = new URLSearchParams(window.location.search).get('q');
  if (preset) input.value = preset;

  var timer = null;
  input.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () { if (rows) run(); }, 120);
  });

  fetch(ds.jsonUrl, { credentials: 'omit' })
    .then(function (resp) { if (!resp.ok) throw new Error(resp.status); return resp.json(); })
    .then(function (data) { prepare(data); if (input.value) run(); })
    .catch(function () {
      box.hidden = false;
      status.textContent = ds.error;
    });
})();
