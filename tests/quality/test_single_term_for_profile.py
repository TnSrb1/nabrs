"""The guard of the term: the saved set of search conditions is called by ONE word.

One and the same entity lived in the catalogs under three words — «template», “profile” and (nearby, in the neighbouring sentences) “subscription”. The client reads the three words as three different things: creates a «template» in the wizard, looks for it in the section “Profiles”, and sees a “search profile” in the letter. The word «template» / «šablon» / «template» was chosen — the same as in the creation wizard and in the library of ready sets.

This invariant breaks silently: it is enough to add one line with the old word. Therefore the guard does not enumerate the keys, but SCANS all the catalogs entirely — and it fails by itself if a new catalog that it does not know is set up next to them (see “A guard that scans a PART of the sources creates a blind zone”, CLAUDE.md).

The tests work without a DB and without a network: they read the constants of the modules, therefore they go in the ordinary CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.api.cabinet.i18n import CABINET
from src.bots.telegram import _PUBLIC_COMMANDS
from src.i18n_common import COMMON
from src.i18n_email import EMAIL

# The catalogs that the guard must scan. The list is checked against the tree
# (see test_guard_knows_every_catalog) — a new catalog will not slip through silently.
CATALOG_MODULES: tuple[str, ...] = (
    "src.api.cabinet.i18n",
    "src.api.public.i18n",
    "src.bots.i18n",
    "src.bots.i18n_ai_render",
    "src.i18n_common",
    "src.i18n_email",
)

# The old word in all three languages. `/profile` and `/profiles` — the name of the command
# of the bot and a piece of URL, not a text for a human: they are cut out before the check.
_COMMAND_OR_URL = re.compile(r"/profiles?\b")
_OLD_TERM = re.compile(r"профил|Профил|profil|Profil|PROFIL", re.UNICODE)

# “Profile” STAYS where it is ANOTHER entity. The sign — a neighbouring word
# on the same line, by it the meaning was told apart when the catalogs were analyzed.
_OTHER_ENTITY_MARKERS: tuple[tuple[str, str], ...] = (
    ("конкурент", "профиль конкурента (VG-06) — аналитика поставщика"),
    ("konkurent", "профиль конкурента (VG-06) — аналитика поставщика"),
    ("competitor", "профиль конкурента (VG-06) — аналитика поставщика"),
    ("победител", "профиль победителя в боте (🥊)"),
    ("pobednik", "профиль победителя в боте (🥊)"),
    ("winner", "профиль победителя в боте (🥊)"),
    ("профиль работы", "род деятельности заказчика, а не набор условий"),
    ("profil posla", "род деятельности заказчика, а не набор условий"),
)


def _strings(node: object) -> list[str]:
    """All the human-readable strings of the structure: the values of the dictionaries and the elements of the lists.

    The keys of the catalog are NOT human text (the code and the templates resolve them), therefore only ``.values()`` are walked.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings(v)]
    if isinstance(node, (list, tuple, set)):
        return [s for v in node for s in _strings(v)]
    return []


def _catalog_strings() -> list[tuple[str, str]]:
    """(module, string) over all the catalogs: their UPPER constant structures."""
    import importlib

    out: list[tuple[str, str]] = []
    for name in CATALOG_MODULES:
        mod = importlib.import_module(name)
        for attr, value in vars(mod).items():
            if attr.startswith("__") or not isinstance(value, (dict, list, tuple)):
                continue
            # ``*_KEYS`` — the lists of the NAMES of the keys (``no_profile_name``): they are read by
            # the card’s code, the client does not see them.
            if attr.endswith("_KEYS"):
                continue
            out += [(name, s) for s in _strings(value)]
    # In ``_PUBLIC_COMMANDS`` the human sees only the DESCRIPTION: the first
    # element of the pair — the name of the Telegram command (``profile``), it must not be changed.
    out += [
        ("src.bots.telegram._PUBLIC_COMMANDS", desc) for _cmd, desc in _PUBLIC_COMMANDS
    ]
    return out


def _is_other_entity(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker, _why in _OTHER_ENTITY_MARKERS)


def test_guard_knows_every_catalog():
    """The guard knows ALL the catalogs of the tree — otherwise it has a blind zone.

    A failure means “a new i18n catalog was set up” — add it to CATALOG_MODULES, rather than weaken the predicate.
    """
    root = Path(__file__).resolve().parents[2]
    found = {
        ".".join(p.relative_to(root).with_suffix("").parts)
        for p in sorted(root.glob("src/**/i18n*.py"))
    }
    assert found == set(CATALOG_MODULES)


def test_no_old_term_in_human_strings():
    """Nowhere in the catalogs is there a “profile” in the sense of a set of search conditions.

    The exceptions are only the OTHER entities (the profile of the competitor/winner, the line of business of the customer): on the same line there is a sign-word for them.
    """
    leftovers = [
        (mod, s)
        for mod, s in _catalog_strings()
        if _OLD_TERM.search(_COMMAND_OR_URL.sub("", s)) and not _is_other_entity(s)
    ]
    assert leftovers == [], "the old word in the texts for a human: " + "; ".join(
        f"{mod}: {s[:90]!r}" for mod, s in leftovers
    )


def test_labels_say_template_in_all_three_languages():
    """Key labels — literals from the run, not through the same ``t()``.

    An assertion through ``t()`` turns green for any content of the catalog; here the very text that the client will see is pinned.
    """
    assert CABINET["sr"]["profiles_title"] == "Moji šabloni"
    assert CABINET["ru"]["profiles_title"] == "Мои шаблоны"
    assert CABINET["en"]["profiles_title"] == "My templates"

    assert CABINET["sr"]["pf_h1_new"] == "Novi šablon pretrage"
    assert CABINET["ru"]["pf_h1_new"] == "Новый шаблон поиска"
    assert CABINET["en"]["pf_h1_new"] == "New search template"

    assert CABINET["sr"]["acct_section_profiles"] == "Šabloni pretrage"
    assert CABINET["ru"]["acct_section_profiles"] == "Шаблоны поиска"
    assert CABINET["en"]["acct_section_profiles"] == "Search templates"


def test_card_and_email_say_template_too():
    """The tender card and the letter — the same words as the cabinet.

    Both surfaces resolve different catalogs (COMMON and EMAIL), they diverge independently of the cabinet.
    """
    assert COMMON["sr"]["found_by_template"] == "Pronađeno po šablonu"
    assert COMMON["ru"]["found_by_template"] == "Найдено по шаблону"
    assert COMMON["en"]["found_by_template"] == "Matched by template"

    assert COMMON["ru"]["btn_configure_profile"] == "⚙️ Настроить этот шаблон"

    assert EMAIL["sr"]["intro_single"] == (
        "Pronađen je tender koji odgovara vašem šablonu pretrage."
    )
    assert (
        EMAIL["ru"]["intro_single"]
        == "Найден тендер, подходящий под ваш шаблон поиска."
    )
    assert (
        EMAIL["en"]["intro_single"]
        == "We found a tender matching your search template."
    )


def test_bot_command_descriptions_say_template():
    """The list of the bot commands lives in THREE places — it diverges silently.

    `_PUBLIC_COMMANDS` is registered in Telegram, `tgp_cmd_*` shows the cabinet, the third list is drawn by the showcase (/help). The word must be one.
    """
    from src.api.public.i18n import HELP

    assert dict(_PUBLIC_COMMANDS)["start"] == "Регистрация и настройка шаблона поиска"
    assert dict(_PUBLIC_COMMANDS)["reset"] == "Перенастроить шаблон заново"

    assert CABINET["ru"]["tgp_cmd_start"] == "регистрация и настройка шаблона поиска"
    assert CABINET["ru"]["tgp_cmd_reset"] == "перенастроить шаблон заново"

    site = {
        cmd: text
        for section in HELP["ru"]["sections"]
        for cmd, text in section.get("commands", ())
    }
    assert site["/start"] == "Регистрация и настройка шаблона поиска"
    assert site["/reset"] == "Перенастроить шаблон заново"
