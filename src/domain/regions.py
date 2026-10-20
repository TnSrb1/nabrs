"""The SSOT directory of Serbian districts (ISO 3166-2:RS, RS-00..RS-29) for the UI.

The “region” axis lives in three places, and all three must converge to THIS list:

* ingest — ``parsers.jnportal.codebooks.OPSTINA_NAME_TO_OKRUG`` (a source adapter, not published) puts the district code
  into ``tenders_normalized.regions`` (resolved by municipality name, #364);
* matching — ``workers.matching`` intersects ``profiles.regions`` with the tender’s
  region (an empty profile filter = “all regions”, see below);
* UI — the web profile form (``api.cabinet.routes_profiles.REGION_CHOICES``) and
  the labels in the bot (``bots.telegram``).

So the list here is ONE, not a copy in each layer: offer the UI a district that
ingest does not emit — and the filter silently gives 0 matches. The set-equality guard is
``tests/test_regions_catalog.py`` (it checks the codes against ``OPSTINA_NAME_TO_OKRUG``
by a predicate, not by a handwritten list).

The names are from the comments to the mapping in ``codebooks.py`` (verified by the mapping’s author;
in parentheses — the administrative center), Serbian LATIN script (owner decision 30.09.2026:
Latin is the product’s main Serbian script; Cyrillic is a transliteration
``scripts/sr_first_templates.py::latinize``). Deliberately NOT translated into English:
a translation “by eye” would produce names that exist in no source.
The label differs from ``regions.name_sr_latn`` (there “Severnobački okrug” without
the center) — this is deliberate: the center helps to choose the district in the form.

‼️ An empty ``profiles.regions`` = “all regions” (matching: ``cardinality(...) = 0 OR
... && ...``). This is NOT the same as "no region chosen and therefore nothing
will arrive" — do not confuse a setting with a fact.
"""

from __future__ import annotations

# Order: Beograd first (RS-00), then by code — that is also the order in the UI.
OKRUG_CHOICES: list[tuple[str, str]] = [
    ("RS-00", "Grad Beograd"),
    ("RS-01", "Severnobački (Subotica)"),
    ("RS-02", "Srednjebanatski (Zrenjanin)"),
    ("RS-03", "Severnobanatski (Kikinda)"),
    ("RS-04", "Južnobanatski (Pančevo)"),
    ("RS-05", "Zapadnobački (Sombor)"),
    ("RS-06", "Južnobački (Novi Sad)"),
    ("RS-07", "Sremski (Sremska Mitrovica)"),
    ("RS-08", "Mačvanski (Šabac)"),
    ("RS-09", "Kolubarski (Valjevo)"),
    ("RS-10", "Podunavski (Smederevo)"),
    ("RS-11", "Braničevski (Požarevac)"),
    ("RS-12", "Šumadijski (Kragujevac)"),
    ("RS-13", "Pomoravski (Jagodina)"),
    ("RS-14", "Borski (Bor)"),
    ("RS-15", "Zaječarski (Zaječar)"),
    ("RS-16", "Zlatiborski (Užice)"),
    ("RS-17", "Moravički (Čačak)"),
    ("RS-18", "Raški (Kraljevo)"),
    ("RS-19", "Rasinski (Kruševac)"),
    ("RS-20", "Nišavski (Niš)"),
    ("RS-21", "Toplički (Prokuplje)"),
    ("RS-22", "Pirotski (Pirot)"),
    ("RS-23", "Jablanički (Leskovac)"),
    ("RS-24", "Pčinjski (Vranje)"),
    ("RS-25", "Kosovski (Priština)"),
    ("RS-26", "Pećki (Peć)"),
    ("RS-27", "Prizrenski (Prizren)"),
    ("RS-28", "Kosovsko-mitrovački (Kosovska Mitrovica)"),
    ("RS-29", "Kosovsko-pomoravski (Gnjilane)"),
]

OKRUG_LABELS: dict[str, str] = dict(OKRUG_CHOICES)

OKRUG_CODES: frozenset[str] = frozenset(OKRUG_LABELS)


def okrug_label(code: str) -> str:
    """A district code → a readable name. An unknown code is returned as is (no invention)."""
    return OKRUG_LABELS.get(code, code)
