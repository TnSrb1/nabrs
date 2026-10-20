"""The SSOT catalog of the profile vertical packages (VG-14).

“Package” — a ready-made search profile for a specific vertical (construction “građevina”, IT, medical equipment “medicinska oprema” / RFZO, security “obezbeđenje”, cleaning “čišćenje”, electrical energy “el. energija”). A new subscriber picks his vertical at /library and gets the new-profile form ALREADY filled in (keywords + CPV) — instead of setting up from scratch.

The mechanism is data-as-code, like :mod:`src.plans.catalog` (the owner edits it here, WITHOUT migrations and without a new table). The /library showcase reuses the existing profile-creation flow: the package card → ``/profiles/new?from_package=<slug>`` → the form prefilled through the same ``_profile_form_ctx`` as the templates (``from_template``).

The keywords are Serbian STEMS in Latin (owner’s decision 30.09.2026): a substring match on the single fold ``text_norm.fold`` catches all the cases, and the ru/en→sr dictionary (:mod:`src.workers.keyword_expand`) is not needed for them. CPV — the standard EU CPV codes (matching the seed templates / existing codebooks), the system code ``eu_cpv``. We keep the sr/ru/en i18n parity in the data; the cabinet renders ru — when a public surface appears it will take sr/en from here too.
"""

from __future__ import annotations

from dataclasses import dataclass

# The CPV classification system in the profile / templates (see 0016_template_library).
CPV_SYSTEM = "eu_cpv"


@dataclass(frozen=True)
class VerticalPackage:
    slug: str  # url-safe id for ?from_package=<slug>
    name: dict[str, str]  # {"sr","ru","en"} — key parity
    description: dict[str, str]  # the value of the package, {"sr","ru","en"}
    # keywords_include — as in profiles / template_library: a list of OR groups,
    # each group {"all_of": [<term>, ...]} = all of its terms (AND).
    keywords_include: tuple[dict, ...]
    cpv: tuple[str, ...]  # the EU CPV codes (system_code=eu_cpv)
    # The profile’s stop words (profiles.keywords_exclude — a flat list; any
    # match excludes the tender). The default is empty — the existing 6 packages do not set it.
    # ‼️ Do not mix ru (expanded by the keyword_expand dictionary) and direct
    # sr literals: the dictionary does not cover all the needed roots (see VG-17-remote).
    keywords_exclude: tuple[str, ...] = ()
    # Extra context for the AI analysis (profiles.ai_extra_context), {"sr","ru","en"}
    # — key parity, as for name / description. None — the package does not set it.
    ai_extra_context: dict[str, str] | None = None
    # Words for DISPLAY (card / composition tags), {"sr","ru","en"} → words. Separate
    # from keywords_include: there they are the stems for the substring search (“građevin”),
    # to the client they read as fragments. The search / matching is NOT affected by this field.
    # sr — in Latin (the whole sr UI is in Latin).
    display_words: dict[str, tuple[str, ...]] | None = None


# The order = the order of the cards on the showcase. Names / descriptions are edited by the owner.
PACKAGES: tuple[VerticalPackage, ...] = (
    VerticalPackage(
        slug="gradjevina",
        name={"sr": "Građevina", "ru": "Строительство", "en": "Construction"},
        description={
            "sr": "Radovi na izgradnji, rekonstrukciji i održavanju objekata i puteva.",
            "ru": "Общестроительные работы, реконструкция, ремонт зданий и дорог.",
            "en": "Construction, reconstruction and maintenance of buildings and roads.",
        },
        keywords_include=(
            {"all_of": ["građevin"]},
            {"all_of": ["izgradnj"]},
            {"all_of": ["građenj"]},
            {"all_of": ["remont", "zgrad"]},
            {"all_of": ["rekonstrukcij", "zgrad"]},
            {"all_of": ["održavanj", "zgrad"]},
            {"all_of": ["popravk", "zgrad"]},
            {"all_of": ["sanacij", "zgrad"]},
            {"all_of": ["rekonstrukcij"]},
            {"all_of": ["put"]},
            {"all_of": ["putev"]},
        ),
        cpv=("45000000", "45210000", "45233000"),
        display_words={
            "sr": (
                "građevina",
                "izgradnja",
                "građenje",
                "remont zgrada",
                "rekonstrukcija",
                "održavanje zgrada",
                "popravka",
                "sanacija",
                "putevi",
            ),
            "ru": (
                "строительство",
                "строительные работы",
                "ремонт зданий",
                "реконструкция",
                "обслуживание зданий",
                "санация",
                "дороги",
            ),
            "en": (
                "construction",
                "building works",
                "building repair",
                "reconstruction",
                "building maintenance",
                "rehabilitation",
                "roads",
            ),
        },
    ),
    VerticalPackage(
        slug="it",
        name={
            "sr": "IT i tehnologije",
            "ru": "ИТ и технологии",
            "en": "IT & technology",
        },
        description={
            "sr": "Softver, licence, IT usluge i računarska oprema.",
            "ru": "ПО, лицензии, IT-услуги и разработка, компьютерная техника.",
            "en": "Software, licences, IT services and computer equipment.",
        },
        keywords_include=(
            {"all_of": ["softver"]},
            {"all_of": ["program"]},
            {"all_of": ["licenc"]},
            {"all_of": ["razvoj"]},
            {"all_of": ["izrad"]},
            {"all_of": ["razvija"]},
            {"all_of": ["računar"]},
            {"all_of": ["kompjuter"]},
        ),
        cpv=("48000000", "72000000", "30200000"),
        display_words={
            "sr": (
                "softver",
                "programi",
                "licence",
                "razvoj",
                "izrada",
                "računari",
                "kompjuteri",
            ),
            "ru": (
                "программное обеспечение",
                "программы",
                "лицензии",
                "разработка",
                "компьютеры",
            ),
            "en": ("software", "programs", "licences", "development", "computers"),
        },
    ),
    VerticalPackage(
        slug="medoprema",
        name={
            "sr": "Medicinska oprema",
            "ru": "Медицинское оборудование (РФЗО)",
            "en": "Medical equipment (RFZO)",
        },
        description={
            "sr": "Medicinska oprema i potrošni materijal za zdravstvene ustanove.",
            "ru": "Медтехника, лабораторное оборудование и расходные медматериалы.",
            "en": "Medical equipment and consumables for healthcare institutions.",
        },
        keywords_include=(
            {"all_of": ["medicinsk", "oprem"]},
            {"all_of": ["medicinsk", "uređaj"]},
            {"all_of": ["laboratorij"]},
            {"all_of": ["medicinsk", "tehnik"]},
        ),
        cpv=("33100000", "33140000"),
        display_words={
            "sr": (
                "medicinska oprema",
                "medicinski uređaji",
                "laboratorija",
                "medicinska tehnika",
            ),
            "ru": (
                "медицинское оборудование",
                "медицинские устройства",
                "лаборатория",
                "медтехника",
            ),
            "en": (
                "medical equipment",
                "medical devices",
                "laboratory",
                "medical technology",
            ),
        },
    ),
    VerticalPackage(
        slug="obezbedjenje",
        name={
            "sr": "Obezbeđenje",
            "ru": "Охрана и безопасность",
            "en": "Security services",
        },
        description={
            "sr": "Usluge fizičkog obezbeđenja, video-nadzor i sistemi alarmiranja.",
            "ru": "Охранные услуги, видеонаблюдение и системы сигнализации.",
            "en": "Physical security services, video surveillance and alarm systems.",
        },
        keywords_include=(
            {"all_of": ["obezbeđ"]},
            {"all_of": ["čuvanj"]},
            {"all_of": ["bezbednost"]},
            {"all_of": ["sigurnost"]},
            {"all_of": ["videonadzor"]},
            {"all_of": ["video nadzor"]},
        ),
        cpv=("79710000", "35125000"),
        display_words={
            "sr": ("obezbeđenje", "čuvanje", "bezbednost", "sigurnost", "videonadzor"),
            "ru": (
                "охрана",
                "обеспечение безопасности",
                "безопасность",
                "видеонаблюдение",
            ),
            "en": ("security", "guarding", "safety", "video surveillance"),
        },
    ),
    VerticalPackage(
        slug="ciscenje",
        name={"sr": "Čišćenje", "ru": "Клининг и уборка", "en": "Cleaning services"},
        description={
            "sr": "Usluge čišćenja i održavanja higijene objekata.",
            "ru": "Услуги уборки и клининга помещений, поддержание чистоты объектов.",
            "en": "Cleaning services and building hygiene maintenance.",
        },
        keywords_include=(
            {"all_of": ["čišćenj"]},
            {"all_of": ["čistoć"]},
            {"all_of": ["higijen"]},
            {"all_of": ["pranj"]},
        ),
        cpv=("90910000", "90911000"),
        display_words={
            "sr": ("čišćenje", "čistoća", "higijena", "pranje"),
            "ru": ("уборка", "клининг", "чистота", "гигиена", "стирка"),
            "en": ("cleaning", "cleanliness", "hygiene", "laundry"),
        },
    ),
    VerticalPackage(
        slug="elenergija",
        name={"sr": "El. energija", "ru": "Электроэнергия", "en": "Electricity supply"},
        description={
            "sr": "Nabavka električne energije i snabdevanje objekata.",
            "ru": "Поставка электроэнергии и электроснабжение объектов.",
            "en": "Supply of electricity and power for facilities.",
        },
        keywords_include=(
            {"all_of": ["električn", "energij"]},
            {"all_of": ["snabdevanje", "električn"]},
            {"all_of": ["električn", "energetsk"]},
        ),
        cpv=("09310000", "09300000"),
        display_words={
            "sr": (
                "električna energija",
                "snabdevanje električnom energijom",
                "energetski",
            ),
            "ru": ("электроэнергия", "электроснабжение", "энергетика"),
            "en": ("electricity", "power supply", "energy"),
        },
    ),
    VerticalPackage(
        slug="remote",
        name={
            "sr": "Usluge na daljinu",
            "ru": "Удалённые услуги",
            "en": "Remote services",
        },
        description={
            "sr": "Izrada sajtova i softvera, dokumentacija, prevodi, konsalting,"
            " obuka, sadržaj — rad isključivo sa računara, bez izlaska na teren"
            " i isporuke robe. CPV 71 (projektovanje s nadzorom na terenu)"
            " namerno nije uključen — dodajte ga ručno ako vam odgovara.",
            "ru": "Разработка сайтов и ПО, документация, переводы, консалтинг,"
            " обучение, контент — работа полностью с компьютера, без выезда"
            " и поставки товаров. CPV 71 (проектирование с надзором на объекте)"
            " сознательно не включён — добавьте вручную, если подходит.",
            "en": "Website and software development, documentation, translation,"
            " consulting, training, content — fully remote work, no site visits"
            " or goods delivery. CPV 71 (design with on-site supervision) is"
            " intentionally excluded — add it manually if it fits.",
        },
        # The dictionary-covered Russian terms (ru→sr via keyword_expand) — as for
        # the other packages. “Translations / consulting / content” are not covered by the dictionary
        # (checked by fact), so they have no separate positive label —
        # the niche for them is carried by CPV + stop words (see TESTING_vertical_package_remote.md).
        keywords_include=(
            {"all_of": ["razvoj"]},
            {"all_of": ["izrad"]},
            {"all_of": ["razvija"]},
            {"all_of": ["softver"]},
            {"all_of": ["program"]},
            {"all_of": ["informacion", "sistem"]},
            {"all_of": ["obuk"]},
            {"all_of": ["obrazovanj"]},
            {"all_of": ["trening"]},
            {"all_of": ["hosting"]},
            {"all_of": ["oblak"]},
            {"all_of": ["oblačn"]},
            {"all_of": ["projekat"]},
            {"all_of": ["projekt"]},
            {"all_of": ["licenc"]},
        ),
        # 72 IT, 79 business services, 73 R&D, 80 training, 92 media / content.
        # 71 (design + on-site supervision) is deliberately not included — see the description.
        cpv=("72000000", "79000000", "73000000", "80000000", "92000000"),
        display_words={
            "sr": (
                "razvoj",
                "izrada",
                "softver",
                "programi",
                "informacioni sistemi",
                "obuka",
                "obrazovanje",
                "trening",
                "hosting",
                "oblak",
                "projekat",
                "licence",
            ),
            "ru": (
                "разработка",
                "программное обеспечение",
                "информационные системы",
                "обучение",
                "образование",
                "тренинг",
                "хостинг",
                "облако",
                "проект",
                "лицензии",
            ),
            "en": (
                "development",
                "software",
                "information systems",
                "training",
                "education",
                "hosting",
                "cloud",
                "project",
                "licences",
            ),
        },
        # Measurement of 20.09.2026 (production, 30 days, CPV 72/79/73/80/92): 307 tenders,
        # the set below cuts all 97 with a sign of supply / installation / works (0 skipped)
        # and 12 additional physical ones (equipment rental / testing, printing + delivery,
        # inventory) out of 209 “clean” ones — see TESTING_vertical_package_remote.md.
        # The “supply” → “nabavka” mapping from the RU dictionary is NOT used: “nabavka” is a common word
        # (“javna nabavka” = public procurement), so a stop word would cut almost
        # every title; instead — the exact phrase “nabavka dobara” (as in
        # the owner’s predicate). The “maintenance” → “servis” mapping is excluded too: “servis” / “servise”
        # catch legitimate cloud / consulting services (Cloud
        # servis, phishing consulting) — the “održavanje” signal already gives “repair”.
        keywords_exclude=(
            "remont",  # repair
            "rekonstrukcij",
            "održavanj",
            "popravk",
            "sanacij",
            "montaž",  # installation
            "ugradnj",
            "oprem",  # equipment
            "uređaj",
            "server",  # server
            "nabavka dobara",  # nabavka dobara
            "nabavke dobara",  # nabavke dobara
            "radov",  # radov
            "materijal",  # materijal
        ),
        ai_extra_context={
            "sr": "Izvršilac radi isključivo daljinski: izrada sajtova i"
            " softvera, dokumentacija, projektovanje, prevodi, konsalting,"
            " obuka, sadržaj. NE odgovara: isporuka robe, montaža i ugradnja"
            " opreme, izlazak na teren, popravka i održavanje na licu mesta.",
            "ru": "Исполнитель работает полностью дистанционно: разработка"
            " сайтов и ПО, документация, проектирование, переводы, консалтинг,"
            " обучение, контент. НЕ подходит: поставка товаров, монтаж и"
            " установка оборудования, выезд на объект, ремонт и обслуживание"
            " на месте.",
            "en": "The contractor works fully remotely: website and software"
            " development, documentation, design, translation, consulting,"
            " training, content. Not a fit: delivery of goods, installation"
            " of equipment, on-site visits, on-site repair or maintenance.",
        },
    ),
)

_BY_SLUG: dict[str, VerticalPackage] = {p.slug: p for p in PACKAGES}


def all_packages() -> tuple[VerticalPackage, ...]:
    """All packages in the showcase order."""
    return PACKAGES


def get_package(slug: str | None) -> VerticalPackage | None:
    """A package by slug; None if unknown / empty."""
    if not slug:
        return None
    return _BY_SLUG.get(slug)


def _pick(text: dict[str, str], lang: str) -> str:
    """The value from sr / ru / en with a fallback to ru → sr."""
    return text.get(lang) or text.get("ru") or text.get("sr") or ""


def display_packages(lang: str = "ru") -> list[dict]:
    """The package data for the /library showcase (cabinet → ru).

    Returns per package: slug, name, description, a flat list of terms
    keywords (for tags) and a list of CPV — the same tag shape as for the templates.
    """
    out: list[dict] = []
    for p in PACKAGES:
        terms: list[str] = []
        for group in p.keywords_include:
            terms.extend(group.get("all_of") or [])
        out.append(
            {
                "slug": p.slug,
                "name": _pick(p.name, lang),
                "description": _pick(p.description, lang),
                "keywords": _pack_words(p, terms, lang),
                "cpv": list(p.cpv),
            }
        )
    return out


def _pack_words(pkg: VerticalPackage, terms: list[str], lang: str) -> list[str]:
    """Package tags: the readable words ``display_words``; none — the previous display of the terms."""
    words = pkg.display_words or {}
    chosen = words.get(lang) or words.get("ru")
    if chosen:
        return list(chosen)
    return _display_terms(terms, lang)


def _display_terms(terms: list[str], lang: str) -> list[str]:
    """Tag terms in the SEARCH language (outside ru — the Serbian forms of the dictionary).

    Mirrors :func:`src.api.cabinet.template_library_service._localized_keywords`: the tag shows what the matching REALLY searches by (stage B expands the term via :func:`src.workers.keyword_expand.expand_term`), not the Russian label — otherwise the showcase would lie about how the search works. No form → the term as is.
    """
    if lang == "ru":
        return terms

    from src.workers.keyword_expand import expand_term

    shown: list[str] = []
    for term in terms:
        forms = [f for f in expand_term(term) if f != term]
        candidate = forms[0] if forms else term
        # ‼️ Dedup: different Russian terms collapse into ONE Serbian form
        # (“cleaning”, “janitorial”, “tidying up” → all “čišćenje”) — without this the showcase
        # showed “čišćenje čišćenje čišćenje” and read as a markup bug.
        if candidate not in shown:
            shown.append(candidate)
    return shown


def to_profile_prefill(pkg: VerticalPackage, lang: str = "ru") -> dict:
    """A package → a payload-like dict for the profile form prefill.

    The shape matches the profile / template (keywords_include — OR groups, classifications — {include:[{system_code,code}], exclude:[]}), so ``routes_profiles._profile_form_ctx`` is reused without duplication.
    """
    return {
        "name": _pick(pkg.name, lang),
        # A deep copy of the groups: the catalog is immutable, the consumer may edit it.
        "keywords_include": [
            {"all_of": list(g.get("all_of") or [])} for g in pkg.keywords_include
        ],
        "keywords_exclude": list(pkg.keywords_exclude),
        "classifications": {
            "include": [{"system_code": CPV_SYSTEM, "code": c} for c in pkg.cpv],
            "exclude": [],
        },
        "ai_extra_context": (
            _pick(pkg.ai_extra_context, lang) if pkg.ai_extra_context else None
        ),
    }
