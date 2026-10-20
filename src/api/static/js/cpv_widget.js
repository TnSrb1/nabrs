// CPV classifier widget — vendored self-script (CSP script-src 'self', no inline).
//
// Two axes (do not mix):
//  * CPV NAMES = the original of the reference — Serbian (l_sr), ALWAYS primary.
//    Next to it — the translation by the interface language: ru → l_ru (falling back to l), en → l.
//  * the CHROME of the widget (labels / hints / aria) = the interface language (users.locale),
//    comes from data-cpv-i18n on the container (the cabinet catalog, three languages).
//
// Data: cpv_codes.json, record {c, lv, p?, l_sr?, l, l_ru?}. The address comes from
// data-cpv-src on the container (there `static_url()` with ?v=<hash> — without the version the client
// after an update of the reference would keep the old cache); the fallback — the bare path.
// The widget also works with ONE side only (include): the Analitika reports.
let CPV_DATA = null;
let CPV_INDEX = null; // { byCode, children, roots }

async function loadCPV() {
  if (CPV_DATA) return CPV_DATA;
  const holder = document.querySelector('[data-cpv-src]');
  const src = (holder && holder.getAttribute('data-cpv-src')) || '/static/cpv_codes.json';
  const r = await fetch(src);
  CPV_DATA = await r.json();
  return CPV_DATA;
}

// The hierarchy index: children by the parent code `p`, roots — lv==1.
function buildIndex(data) {
  if (CPV_INDEX) return CPV_INDEX;
  const byCode = {};
  const children = {};
  const roots = [];
  data.forEach((e) => {
    byCode[e.c] = e;
    if (e.lv === 1 || !e.p) roots.push(e);
    if (e.p) (children[e.p] = children[e.p] || []).push(e);
  });
  CPV_INDEX = { byCode, children, roots };
  return CPV_INDEX;
}

// The interface language from <html lang> (the same axis as the chrome).
function uiLocale() {
  const l = (document.documentElement.lang || 'sr').slice(0, 2).toLowerCase();
  return l === 'ru' || l === 'en' ? l : 'sr';
}

// Serbian Cyrillic / Latin + Russian Cyrillic → Latin without diacritics,
// so that the search matches regardless of the input writing (a real complaint: a Serb types
// in Latin / Cyrillic and finds nothing) and of the case.
const _TRANSLIT = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', ђ: 'dj', е: 'e', ё: 'e', ж: 'z',
  з: 'z', и: 'i', й: 'j', ј: 'j', к: 'k', л: 'l', љ: 'lj', м: 'm', н: 'n',
  њ: 'nj', о: 'o', п: 'p', р: 'r', с: 's', т: 't', ћ: 'c', у: 'u', ф: 'f',
  х: 'h', ц: 'c', ч: 'c', џ: 'dz', ш: 's', щ: 'sc', ъ: '', ы: 'y', ь: '',
  э: 'e', ю: 'ju', я: 'ja', і: 'i',
  // Latin Serbian digraphs / diacritics → ascii
  đ: 'dj', ž: 'z', ć: 'c', č: 'c', š: 's',
};
// owner 10.10.2026: the Serbian interface — Latin ONLY. The names from the reference
// and the API responses are in Cyrillic; at the display boundary (textContent) — into Latin.
// The values (value / code) are not touched. A mirror of src/domain/text_norm.cyr_to_lat_strict.
const _SR_LAT = {
  а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', ђ: 'đ', е: 'e', ж: 'ž', з: 'z',
  и: 'i', ј: 'j', к: 'k', л: 'l', љ: 'lj', м: 'm', н: 'n', њ: 'nj', о: 'o',
  п: 'p', р: 'r', с: 's', т: 't', ћ: 'ć', у: 'u', ф: 'f', х: 'h', ц: 'c',
  ч: 'č', џ: 'dž', ш: 'š', ё: 'jo', й: 'j', щ: 'šč', ъ: '', ы: 'y', ь: '',
  э: 'e', ю: 'ju', я: 'ja',
};
function srLat(s) {
  if ((document.documentElement.lang || '').slice(0, 2).toLowerCase() !== 'sr') return s;
  const chars = Array.from(s || '');
  let out = '';
  chars.forEach(function (ch, i) {
    const low = ch.toLowerCase();
    const lat = _SR_LAT[low];
    if (lat === undefined) { out += ch; return; }
    if (ch === low) { out += lat; return; }
    if (lat.length <= 1) { out += lat.toUpperCase(); return; }
    const nxt = chars[i + 1] || '';
    const shout = nxt !== '' && nxt !== nxt.toLowerCase();
    out += shout ? lat.toUpperCase() : lat.charAt(0).toUpperCase() + lat.slice(1);
  });
  return out;
}
function norm(s) {
  s = (s || '').toLowerCase();
  let out = '';
  for (const ch of s) out += _TRANSLIT[ch] !== undefined ? _TRANSLIT[ch] : ch;
  return out;
}

// The reference record by the filter code. The PREFIX code (“48*” — the whole section 48)
// is not in the reference: we take the record of its base code (48 → 48000000),
// otherwise the selected filter would be drawn as “48* 48*” — the code instead of the name.
function entryFor(idx, code) {
  if (idx.byCode[code]) return idx.byCode[code];
  const star = code.indexOf('*');
  if (star > -1) {
    const base = code.slice(0, star);
    const padded = (base + '00000000').slice(0, 8);
    const parent = idx.byCode[padded];
    if (parent) return Object.assign({}, parent, { c: code });
  }
  return { c: code };
}

// The primary (sr) and the secondary (the translation by locale) labels of the record.
function labelParts(e, locale) {
  const primary = e.l_sr || e.l || e.c;
  let secondary = '';
  if (locale === 'ru') secondary = e.l_ru || e.l || '';
  else if (locale === 'en') secondary = e.l || '';
  if (secondary && norm(secondary) === norm(primary)) secondary = '';
  return { primary: srLat(primary), secondary: srLat(secondary) };
}

// --- the selected codes (right) -------------------------------------------------
const selected = { include: new Map(), exclude: new Map() };

function i18n(side) {
  const el = document.getElementById('cpv' + cap(side) + 'Widget');
  try {
    return JSON.parse((el && el.getAttribute('data-cpv-i18n')) || '{}');
  } catch (_e) {
    return {};
  }
}

function renderList(side) {
  const container = document.getElementById('cpv' + cap(side) + 'List');
  const t = i18n(side);
  const locale = uiLocale();
  container.innerHTML = '';
  if (selected[side].size === 0) {
    const empty = document.createElement('div');
    empty.className = 'px-3 py-2 text-slate-400 italic';
    empty.textContent = t.nothing_selected || '—';
    container.appendChild(empty);
    syncHidden(side);
    return;
  }
  for (const [code, e] of selected[side]) {
    const parts = labelParts(e, locale);
    const row = document.createElement('div');
    row.className = 'flex items-center justify-between px-3 py-1 hover:bg-slate-50 border-b last:border-b-0';
    const left = document.createElement('span');
    left.className = 'text-sm';
    const codeEl = document.createElement('span');
    codeEl.className = 'font-mono text-slate-500 mr-2';
    codeEl.textContent = code;
    left.appendChild(codeEl);
    if (parts.primary !== code) {
      const labelEl = document.createElement('span');
      labelEl.className = 'text-slate-800';
      labelEl.textContent = parts.primary;
      left.appendChild(labelEl);
    }
    if (parts.secondary) {
      const trEl = document.createElement('span');
      trEl.className = 'text-slate-400 ml-2';
      trEl.textContent = parts.secondary;
      left.appendChild(trEl);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ml-3 text-slate-400 hover:text-red-500 text-lg leading-none flex-shrink-0';
    btn.style.minHeight = '2rem';
    btn.style.minWidth = '2rem';
    btn.innerHTML = '&times;';
    btn.setAttribute('aria-label', (t.remove || 'Remove') + ' ' + code);
    btn.addEventListener('click', () => removeCode(side, code));
    row.appendChild(left);
    row.appendChild(btn);
    container.appendChild(row);
  }
  syncHidden(side);
}

function syncHidden(side) {
  const ta = document.getElementById('cpv' + cap(side) + 'Hidden');
  ta.value = [...selected[side].keys()].map((c) => 'eu_cpv:' + c).join('\n');
  // The live counter listens to `input` on the form (the htmx driver #previewDriver).
  // The assignment of value from the script does NOT generate such an event, therefore the selection
  // of a CPV code would otherwise not recount the figure — "looks fine, but does not
  // work". The event bubbles: the listener sits on the form, not on the field.
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

function removeCode(side, code) {
  selected[side].delete(code);
  renderList(side);
}

// In the selection the CODE is saved; for display we keep the record (sr / the translation).
function addEntry(side, e) {
  if (selected[side].has(e.c)) return;
  selected[side].set(e.c, e);
  renderList(side);
}

function cap(s) { return s[0].toUpperCase() + s.slice(1); }

let _debounce = {};
function debounce(fn, ms, key) {
  clearTimeout(_debounce[key]);
  _debounce[key] = setTimeout(fn, ms);
}

// --- the result / node row --------------------------------------------------
// Shows the code + sr (primary) + the translation (secondary); optionally a chevron to descend.
function makeRow(side, e, { drillable }) {
  const t = i18n(side);
  const locale = uiLocale();
  const parts = labelParts(e, locale);
  const row = document.createElement('div');
  row.className = 'flex items-center gap-2 px-3 border-b last:border-b-0 hover:bg-white';
  row.style.minHeight = '2rem';

  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'text-slate-400 hover:text-slate-900 text-lg leading-none flex-shrink-0';
  add.style.minHeight = '2rem';
  add.style.minWidth = '2rem';
  add.textContent = '+';
  add.setAttribute('aria-label', (t.add || 'Add') + ' ' + e.c);
  add.addEventListener('click', (ev) => { ev.stopPropagation(); addEntry(side, e); });
  row.appendChild(add);

  const body = document.createElement('div');
  body.className = 'flex-1 min-w-0 py-1 text-sm';
  body.style.minHeight = '2rem';
  if (drillable) { body.style.cursor = 'pointer'; }
  const codeEl = document.createElement('span');
  codeEl.className = 'font-mono text-slate-500 mr-2';
  codeEl.textContent = e.c;
  const primary = document.createElement('span');
  primary.className = 'text-slate-800';
  primary.textContent = parts.primary;
  body.appendChild(codeEl);
  body.appendChild(primary);
  if (parts.secondary) {
    const sec = document.createElement('span');
    sec.className = 'text-slate-400 ml-2';
    sec.textContent = parts.secondary;
    body.appendChild(sec);
  }
  row.appendChild(body);

  if (drillable) {
    const chev = document.createElement('span');
    chev.className = 'text-slate-400 flex-shrink-0 pr-1';
    chev.textContent = '›';
    chev.setAttribute('aria-hidden', 'true');
    row.appendChild(chev);
    body.setAttribute('role', 'button');
    body.setAttribute('tabindex', '0');
    body.addEventListener('click', () => drillTo(side, e.c));
    body.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); drillTo(side, e.c); }
    });
  }
  return row;
}

// --- browse navigation (the tree by p) ----------------------------------------
const browsePath = { include: [], exclude: [] };

function drillTo(side, code) {
  browsePath[side] = browsePath[side].concat([code]);
  renderBrowse(side);
}

function crumbTo(side, idx) {
  browsePath[side] = browsePath[side].slice(0, idx);
  renderBrowse(side);
}

async function renderBrowse(side) {
  const resultsEl = document.getElementById('cpv' + cap(side) + 'Results');
  const t = i18n(side);
  const locale = uiLocale();
  const data = await loadCPV();
  const idx = buildIndex(data);
  resultsEl.innerHTML = '';

  // breadcrumbs
  const crumbs = document.createElement('div');
  crumbs.className = 'flex flex-wrap items-center gap-1 px-3 py-1 text-xs bg-slate-100 border-b';
  const root = document.createElement('button');
  root.type = 'button';
  root.className = 'text-slate-600 hover:underline';
  root.textContent = t.browse_root || 'CPV';
  root.addEventListener('click', () => crumbTo(side, 0));
  crumbs.appendChild(root);
  browsePath[side].forEach((code, i) => {
    const sep = document.createElement('span');
    sep.className = 'text-slate-400';
    sep.textContent = '›';
    crumbs.appendChild(sep);
    const e = idx.byCode[code];
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'text-slate-600 hover:underline truncate max-w-full';
    b.textContent = e ? labelParts(e, locale).primary : code;
    b.addEventListener('click', () => crumbTo(side, i + 1));
    crumbs.appendChild(b);
  });
  resultsEl.appendChild(crumbs);

  const current = browsePath[side][browsePath[side].length - 1];
  const nodes = current ? (idx.children[current] || []) : idx.roots;
  nodes.slice().sort((a, b) => a.c.localeCompare(b.c)).forEach((e) => {
    const hasKids = !!(idx.children[e.c] && idx.children[e.c].length);
    resultsEl.appendChild(makeRow(side, e, { drillable: hasKids }));
  });
  resultsEl.classList.remove('hidden');
}

// --- search -------------------------------------------------------------------
async function cpvSearch(side) {
  const raw = document.getElementById('cpv' + cap(side) + 'Search').value.trim();
  const resultsEl = document.getElementById('cpv' + cap(side) + 'Results');
  const t = i18n(side);
  if (raw.length < 2) { renderBrowse(side); return; } // empty/1 character → browse mode
  const q = norm(raw);
  const qDigits = raw.replace(/\D/g, '');
  const data = await loadCPV();
  const matches = data.filter((e) => {
    if (qDigits && e.c.startsWith(qDigits)) return true;
    if (norm(e.l_sr).includes(q)) return true;
    if (norm(e.l_ru).includes(q)) return true;
    if (norm(e.l).includes(q)) return true;
    return false;
  }).slice(0, 40);
  resultsEl.innerHTML = '';
  if (!matches.length) {
    const msg = document.createElement('div');
    msg.className = 'px-3 py-2 text-slate-400';
    msg.textContent = t.nothing_found || 'Nothing found';
    resultsEl.appendChild(msg);
    resultsEl.classList.remove('hidden');
    return;
  }
  matches.forEach((e) => resultsEl.appendChild(makeRow(side, e, { drillable: false })));
  resultsEl.classList.remove('hidden');
}

function cpvClearSearch(side) {
  document.getElementById('cpv' + cap(side) + 'Search').value = '';
  browsePath[side] = [];
  renderBrowse(side);
}

function initCpvWidget(side) {
  const ta = document.getElementById('cpv' + cap(side) + 'Hidden');
  if (!ta) return; // this side is not on the page
  const lines = (ta.value || '').split('\n').map((l) => l.trim()).filter(Boolean);
  loadCPV().then((data) => {
    const idx = buildIndex(data);
    lines.forEach((line) => {
      const code = line.startsWith('eu_cpv:') ? line.slice(7) : line;
      if (code) selected[side].set(code, entryFor(idx, code));
    });
    renderList(side);
    renderBrowse(side); // the directory is visible right away — "at a glance"
  });
}

document.addEventListener('DOMContentLoaded', () => {
  const incSearch = document.getElementById('cpvIncludeSearch');
  const excSearch = document.getElementById('cpvExcludeSearch');
  const incClear = document.querySelector('button[data-cpv-clear="include"]');
  const excClear = document.querySelector('button[data-cpv-clear="exclude"]');

  if (incSearch) incSearch.addEventListener('input', () => debounce(() => cpvSearch('include'), 200, 'inc'));
  if (excSearch) excSearch.addEventListener('input', () => debounce(() => cpvSearch('exclude'), 200, 'exc'));
  // data-cpv-no-submit on the container: Enter in the search row does not submit the form
  // of the page (the Analitika report would go out with an incomplete selection). The profile — as it was.
  [incSearch, excSearch].forEach((el) => {
    if (el && el.closest('[data-cpv-no-submit]')) {
      el.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') ev.preventDefault(); });
    }
  });
  if (incClear) incClear.addEventListener('click', () => cpvClearSearch('include'));
  if (excClear) excClear.addEventListener('click', () => cpvClearSearch('exclude'));

  initCpvWidget('include');
  initCpvWidget('exclude');
});
