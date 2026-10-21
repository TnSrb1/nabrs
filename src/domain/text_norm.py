"""A single contract for normalizing Serbian text for matching (research §6).

Invariant: any text comparison “word ↔ tender text” goes through ONE
folding. ``title_normalized``/``description_normalized`` are written by ingest via
:func:`normalize_text`; the same function must also fold the SEARCH side
(profile keywords, word forms from ``keyword_dict``) before comparison —
otherwise the sides live in different alphabets and never match (a real defect:
the dictionary in Serbian Cyrillic, the haystack in Latin → 0 matches).

The folding (the order of steps is FIXED — the normalized keys of the
codebooks in :mod:`src.parsers.jnportal.codebooks` (a source adapter, not published) and the adapter’s enum dictionaries depend on it):

1. cyrl→latn by :data:`CYR2LAT` (Serbian Cyrillic; the Russian-specific letters
   U+044E/U+044F/U+0439/U+0449/… are absent from the table and pass through as is — so both Latin and Russian
   Cyrillic fold to a common form symmetrically on both sides);
2. lowercase;
3. ``đ`` → ``dj`` (in lower case after step 2);
4. stripping diacritics via NFKD (č→c, š→s, ž→z, …);
5. ijekavian → ekavian (:func:`fold_ijekavica`, BA/ME, 15.10.2026): “cijevi”→“cevi”,
   “mlijeko”→“mleko”, “djeca”→“deca”. Conservative, per a measurement of the RS corpus.

The module is a leaf: stdlib only, without importing parsers/matching, so that anyone can
import it without cycles.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Cyrillic → ascii Latin (the Serbian set of letters, research §6). The Russian-
# specific letters (U+0451, U+0439, U+0449, U+044A, U+044B, U+044C, U+044D, U+044E, U+044F) are deliberately absent: they are not in
# Serbian, and on comparison they pass as is on both sides.
CYR2LAT: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "ђ": "dj",
    "е": "e",
    "ж": "z",
    "з": "z",
    "и": "i",
    "ј": "j",
    "к": "k",
    "л": "l",
    "љ": "lj",
    "м": "m",
    "н": "n",
    "њ": "nj",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "ћ": "c",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "c",
    "џ": "dz",
    # Macedonian letters (owner decision 01.10): gje→dj, kje→c, dze→z — like the Serbian
    # dje/tshe/ze, so that MK strings (TED, NPS) are searched with the same keys.
    "ѓ": "dj",
    "ќ": "c",
    "ѕ": "z",
    "ш": "s",
}
CYR2LAT.update({k.upper(): v for k, v in CYR2LAT.items()})


# Serbian Cyrillic → Latin FOR DISPLAY: diacritics are preserved (unlike
# :data:`CYR2LAT`, which is a folding table for search). The case follows the input;
# digraphs (lj, nj, dž letters) in capitalised form — “Lj”/“Nj”/“Dž”, and inside a word of
# capitals (“LJUBAV” in Cyrillic) — “LJ”/“NJ”/“DŽ”.
_CYR2LAT_DISPLAY: dict[str, str] = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "ђ": "đ",
    "е": "e",
    "ж": "ž",
    "з": "z",
    "и": "i",
    "ј": "j",
    "к": "k",
    "л": "l",
    "љ": "lj",
    "м": "m",
    "н": "n",
    "њ": "nj",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "ћ": "ć",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "č",
    "џ": "dž",
    "ш": "š",
}
#: Russian letters that do not exist in Serbian Cyrillic: a text with them is not Serbian.
RU_ONLY_LETTERS: frozenset[str] = frozenset("ёйщъыьэюяЁЙЩЪЫЬЭЮЯ")


def cyr_to_lat(value: Any) -> str:
    """Serbian Cyrillic → Latin for DISPLAY (č/ć/š/ž/đ/dž/lj/nj are preserved).

    Not a folding: for comparison/search use :func:`normalize_text`. The case
    is preserved; Latin and other characters pass as is. Russian letters
    absent from the table (yery, e, yu, ya, …) also pass as is — the caller
    decides whether to transliterate a text of unknown language.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    out: list[str] = []
    for i, ch in enumerate(text):
        low = ch.lower()
        lat = _CYR2LAT_DISPLAY.get(low)
        if lat is None:
            out.append(ch)
        elif ch == low:
            out.append(lat)
        elif len(lat) == 1:
            out.append(lat.upper())
        else:
            # A capital digraph: “Lj” at the start of a word, “LJ” among capitals.
            nxt = text[i + 1] if i + 1 < len(text) else ""
            prv = text[i - 1] if i else ""
            shout = (nxt.isalpha() and nxt.isupper()) or (
                prv.isalpha() and prv.isupper()
            )
            out.append(lat.upper() if shout else lat.capitalize())
    return "".join(out)


#: Russian letters that do not exist in Serbian Cyrillic → the display Latin. Needed
#: only by the Serbian interface: it has no right to show a single Cyrillic
#: letter (owner 10.10.2026), even when Russian text turned up in the data.
_RU_ONLY_TO_LAT: dict[str, str] = {
    "ё": "jo",
    "й": "j",
    "щ": "šč",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "ju",
    "я": "ja",
}
_RU_ONLY_TABLE = {ord(k): v for k, v in _RU_ONLY_TO_LAT.items()} | {
    ord(k.upper()): v.capitalize() for k, v in _RU_ONLY_TO_LAT.items()
}


def cyr_to_lat_strict(value: Any) -> str:
    """:func:`cyr_to_lat` + Russian letters: there is NO Cyrillic at all in the output.

    For the display boundary of the Serbian interface. The other characters of the Unicode block
    Cyrillic (Ukrainian/Macedonian/old) must not leak either, but they are not in the
    data — left as is until found by fact.
    """
    return cyr_to_lat(value).translate(_RU_ONLY_TABLE)


def sr_display(value: Any, lang: str | None) -> Any:
    """The display boundary: with ``lang == “sr”`` — a Latin string, otherwise as is.

    A single point for outputs outside Jinja (CSV, Excel, emails, the bot, XML): owner
    10.10.2026 “Serbian everywhere only in Latin”. Not a string — unchanged.
    """
    if lang == "sr" and isinstance(value, str):
        return cyr_to_lat_strict(value)
    return value


# --- Ijekavian → Ekavian (BA/ME; measurement S4/S5, 15.10.2026) -----------------------
# The reflex of yat in Ijekavian: “ije” (mlijeko, cijevi, snijeg, dijete) and “je” after
# a consonant (djeca, mjesto, cjevovod). Ekavian: mleko, cevi, sneg, dete, deca.
# The folding runs AFTER diacritics are stripped, so “dj” may also be đ (“izvodjenje”,
# “gradjevinski” — 303+200 RS tokens), “cj” — č/ć (“podrucje”, “decje”), “zj” — ž
# (“oruzje”). A naive “je→e” broke them (measurement: 70 of 7086 RS words in 30 days).
# Therefore each “je” is folded ONLY by its own context (a consonant +
# the next letter: djec/djel/djet/djevoj, cjev/cjen/cjel, sjev/sjed, …), and “ije” —
# only before a consonant/“o#”, except:
#   * “ije” at the END of a word — the genitive in “-ija” (ekskurzije, energije);
#   * roots of Greco-Latin origin :data:`_IJE_KEEP` (higijena, pacijent);
#   * “-ijeg/-ijem/-ijeh” at the end — adjectives (decijeg, najpovoljnijeg),
#     except snijeg/bijeg/smijeh/grijeh.
# Both sides of the comparison go through this same function, so a false MERGE of two words
# is possible (rarely, see docs/reports/TESTING_text-norm-ijekavica.md). A word with
# itself always matches, BUT a substring at the rule boundary does not: a key
# cut off at “ije” (“cije”) is not folded (there is no next consonant), while
# the haystack “cijevi” → “cevi” → the key “cije” will NOT find it (but “cij” will, “ce”
# too). Accepted: prefix input up to a syllable boundary is a rare case; a full word
# (“cijevi”→“cevi”) is found.
_IJE_KEEP_RE = re.compile(
    r"higijen|pacijen|prijem|prijep|dijeta|dijets|barijer|orijent|ambijent|"
    r"dobijen|pijezo|klijent|terijer|premijer|karnegij|aksentij|mijelo|recipijen|"
    r"dijetes|dijetetsk|dijetski"
)
_IJE_RE = re.compile(
    r"ije(?=[bcdfghjklmnprstvzx])(?![gmh]\b)|(?<=sn)ije(?=g\b)|(?<=\bb)ije(?=g\b)"
    r"|(?<=sm)ije(?=h\b)|(?<=gr)ije(?=h\b)|ije(?=o\b)"
)
_JE_RE = re.compile(
    r"(?<=c)je(?=[vnl])|(?<![nr]d)(?<=d)je(?=[cltj]|vo)|(?<=gd)je\b|(?<=s)je(?=[vdmk])"
    r"|(?<=t)je(?=[dhkr])|(?<=[mpvr])je|(?<=b)je(?=[lgs])|(?<=l)je(?=[kpts])"
)


def fold_ijekavica(text: str) -> str:
    """Ijekavian → Ekavian for ALREADY folded (Latin, without diacritics) text.

    Words from :data:`_IJE_KEEP_RE` stay as is (the “ije” rule does not
    apply to them), the rest is folded by :data:`_IJE_RE`/:data:`_JE_RE`.
    Idempotent by construction (up to a fixed point); pinned by brute force in tests.
    """
    if "je" not in text:
        return text
    out: list[str] = []
    pos = 0
    for m in _IJE_KEEP_RE.finditer(text):
        out.append(_fold_ije_chunk(text[pos : m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_fold_ije_chunk(text[pos:]))
    return "".join(out)


def _fold_ije_chunk(chunk: str) -> str:
    # Up to a fixed point: removing “ij” may compose a new “ije” (“ijijen”).
    while True:
        folded = _JE_RE.sub("e", _IJE_RE.sub("e", chunk))
        if folded == chunk:
            return folded
        chunk = folded


def normalize_text(value: Any, *, ijekavica: bool = True) -> str | None:
    """cyrl→latn (base) → lowercase → đ→dj → unaccent → ije→e (research §6).

    ``ijekavica=False`` skips step 5: only for comparison with SQL columns
    folded by :func:`fold_sql` (buyer/winner names) — there is no SQL mirror of
    ijekavica (the exception list + the fixed point in ``regexp_replace`` cannot
    be expressed), so both sides of such a comparison go WITHOUT step 5.

    ``None``/empty input → ``None`` (the ingest contract: an empty field stays
    empty). The function is idempotent: applying it to already normalized text
    (Latin without diacritics) returns the same — so it is safe to fold the
    haystack again on the comparison side.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    out = "".join(CYR2LAT.get(ch, ch) for ch in value)
    out = out.lower()
    out = out.replace("đ", "dj")
    out = "".join(
        c for c in unicodedata.normalize("NFKD", out) if not unicodedata.combining(c)
    )
    return fold_ijekavica(out) if ijekavica else out


def fold(value: Any, *, ijekavica: bool = True) -> str:
    """Normalisation for substring comparison: like :func:`normalize_text`, but
    ``None``/empty input → ``""`` (convenient for ``kw in hay`` without None checks).

    ``ijekavica=False`` — the pair of :func:`fold_sql` (the SQL mirror without step 5).
    """
    return normalize_text(value, ijekavica=ijekavica) or ""


#: Latin diacritics outside the Serbian set — for the SQL folding (without ``unaccent``).
_SQL_EXTRA_ACCENTS = "áàâäãåéèêëíìîïóòôöõőúùûüűýÿñç"


def fold_sql(expr: str, *, lowered: bool = False) -> str:
    """An SQL expression equivalent to ``fold(..., ijekavica=False)`` for ``expr``.

    ‼️ WITHOUT the ijekavica step (5): the rule is not mirrored in SQL. Fold user input
    for comparison with this expression by ``fold(x, ijekavica=False)``, otherwise
    “Bijelo Polje”→“belo polje” will not find “bijelo polje” in the column.

    Needed where the comparison happens in the DB on values stored in Cyrillic (for example
    ``tender_awards.customer_name``), while user input is folded in Python.
    Generated from :data:`CYR2LAT` — the table stays ONE. The order as in
    :func:`fold`: lower → multi-letter replacements (lj/nj/dž/đ/gje/đ) → per-letter
    ``translate`` (Cyrillic and č/ć/š/ž → ascii). ``lowered=True`` — ``expr`` is already in lower
    case (do not call ``lower`` a second time). ``expr`` is a module literal, NOT
    user input. There is no ``unaccent`` in the DB, so Latin diacritics
    outside the Serbian set are removed by the explicit list :data:`_SQL_EXTRA_ACCENTS`
    (in production on 02.10 — 2 of 1520 names with Hungarian á/í/ó; owner review).
    The reconciliation with the Python :func:`fold` is pinned by a DB test.
    """
    pairs = {**CYR2LAT, "đ": "dj", "č": "c", "ć": "c", "š": "s", "ž": "z"}
    pairs.update(
        {ch: unicodedata.normalize("NFKD", ch)[0] for ch in _SQL_EXTRA_ACCENTS}
    )
    multi = {k: v for k, v in pairs.items() if len(v) > 1 and k == k.lower()}
    single = {k: v for k, v in pairs.items() if len(v) == 1 and k == k.lower()}
    out = expr if lowered else f"lower({expr})"
    for src, dst in multi.items():
        out = f"replace({out}, '{src}', '{dst}')"
    frm = "".join(single)
    to = "".join(single.values())
    return f"translate({out}, '{frm}', '{to}')"


# Serbian case endings for CONSERVATIVE truncation (template_wizard,
# owner 30.09.2026). Long ones first: “skolama” → “skol”, not “skolam”.
SR_CASE_ENDINGS: tuple[str, ...] = (
    "ima",
    "ama",
    "om",
    "ih",
    "og",
    "oj",
    "em",
    "a",
    "e",
    "i",
    "u",
    "o",
)
#: A shorter word is not truncated (“voda” → “vod” would catch “vodovod”).
STEM_MIN_WORD = 5
#: A shorter stem — the ending is not removed (“radom” → “rad” is too broad).
STEM_MIN_BASE = 4
#: Stems that are prefixes of frequent FOREIGN words: the word is searched verbatim.
#: Measurement (production, 90 days, 10 300 RS tenders, 01.10.2026): a substring against
#: “stem + a case ending as a whole word” — post 280/12 (postupak,
#: postojeci), stan 238/1 (stanica, stanje), plat 78/4 (platforma), stol 82/0
#: (stolarija, stolice). Extend the list only with the same kind of measurement.
STEM_AMBIGUOUS: frozenset[str] = frozenset({"post", "stan", "plat", "stol"})


def stem_sr(word: Any) -> str:
    """The stem of ONE word for substring search: folding + stripping the ending.

    The search is substring-based, so the stem catches all cases: “gradjevina” → “gradjevin”
    finds “gradjevinskih”, “gradjevine”. The stem is always a prefix of the folded
    word, so what is found by the whole word is also found by the stem.

    Conservative: a word < :data:`STEM_MIN_WORD` characters (after folding) is not
    touched; we strip the FIRST suitable ending from :data:`SR_CASE_ENDINGS`
    (long ones first) and only if the stem is ≥ :data:`STEM_MIN_BASE` — otherwise the word
    stays as is (a shorter ending is not tried). A stem from
    :data:`STEM_AMBIGUOUS` (a prefix of frequent foreign words) is not stripped either.
    """
    w = fold(word).strip()
    if len(w) < STEM_MIN_WORD:
        return w
    for end in SR_CASE_ENDINGS:
        if w.endswith(end):
            base = w[: -len(end)]
            if len(base) < STEM_MIN_BASE or base in STEM_AMBIGUOUS:
                return w
            return base
    return w
