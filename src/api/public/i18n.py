"""The minimal language mechanism of the public pages of the showcase (TASK 1, SITE-04).

The showcase is multilingual: ``sr`` (the default — the market’s language), ``ru``, ``en``. The cabinet and auth are NOT touched — this module serves only the public marketing and legal pages.

The priority of the language choice (``resolve_lang``):
    ?lang=<code>  →  cookie ``lang``  →  ``sr``. ``Accept-Language`` is NOT taken into account
(owner’s decision 30.09.2026: Serbian by default, ru / en — only by an explicit choice).

The texts are stored as the dictionaries ``UI`` (the shared chrome: header / footer / switcher) and per page (``LANDING``). The legal texts are added by a separate task (legal-pages) and reuse ``resolve_lang`` / ``public_context``.
"""

from __future__ import annotations

from typing import TypedDict
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import Response

from src.api import security
from src.i18n_common import COMMON

SUPPORTED: tuple[str, ...] = ("sr", "ru", "en")
DEFAULT = "sr"
LANG_COOKIE = "lang"
LANG_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year

# The labels of the language switcher (the displayed codes).
LANG_NAMES: dict[str, str] = {"sr": "SR", "ru": "RU", "en": "EN"}


def parse_accept_language(header: str | None) -> str | None:
    """The first supported language from ``Accept-Language`` in descending ``q`` order.

    ``'sr-RS,sr;q=0.9,en;q=0.8'`` → ``'sr'``. The region (``-RS``) is discarded.
    Returns ``None`` if no language is supported.
    """
    if not header:
        return None
    items: list[tuple[float, str]] = []
    for part in header.split(","):
        token = part.strip()
        if not token:
            continue
        code, _, q = token.partition(";")
        code = code.strip().lower().split("-")[0]
        qval = 1.0
        q = q.strip()
        if q.startswith("q="):
            try:
                qval = float(q[2:])
            except ValueError:
                qval = 0.0
        items.append((qval, code))
    items.sort(key=lambda x: x[0], reverse=True)
    for _, code in items:
        if code in SUPPORTED:
            return code
    return None


def resolve_lang(request: Request) -> str:
    """Determines the page language: ?lang= → cookie → the default ``sr`` (Accept-Language is ignored).

    This same resolver serves the ANONYMOUS cabinet login screens (signup / login / resend): the client comes to them straight from the showcase, and the default must match — otherwise the showcase would give sr for the same request, while the signup form gives ru (audit 2026-08-02, HIGH-2). The string catalog there stays the cabinet one; only the language detection changes (see :func:`src.api.templating._auth_context`).
    """
    chosen = request.query_params.get("lang")
    if chosen in SUPPORTED:
        return chosen
    cookie = request.cookies.get(LANG_COOKIE)
    if cookie in SUPPORTED:
        return cookie
    return DEFAULT


def lang_switch_href(request: Request, code: str) -> str:
    """The language switcher link: the current query with ``lang`` replaced.

    Previously the pill led to a bare ``?lang=…`` and dropped the other parameters — ``/tenderi?q=skola`` → ``/tenderi?lang=ru`` (audit 07.10). Now the filters and the search survive a language change; the path stays the current one (a relative link).
    """
    params = []
    for k, v in request.query_params.multi_items():
        if k == "lang":
            continue
        # ``next`` (where to return after login) comes from the URL, so it is hostile. It goes
        # further ONLY if it passes the same validator as the login form and the
        # magic link: a relative path with a single ``/``. Otherwise — we discard it.
        if k == "next" and security.safe_next_path(v) is None:
            continue
        params.append((k, v))
    params.append(("lang", code))
    return "?" + urlencode(params)


def persist_lang(request: Request, response: Response) -> None:
    """If the language is chosen explicitly (``?lang=``) — we remember it in the ``lang`` cookie.

    It lives next to the resolver, not in one router: the same mechanism is needed by the anonymous login screens (the SR / RU / EN pills there are GET links ``?lang=`` too), and the product must not have a second way to remember the choice.
    """
    chosen = request.query_params.get("lang")
    if chosen in SUPPORTED:
        response.set_cookie(
            LANG_COOKIE,
            chosen,
            max_age=LANG_COOKIE_MAX_AGE,
            samesite="lax",
            path="/",
        )


# --------------------------------------------------------------------------- #
# The texts. UI — the shared chrome of the public layout; LANDING — the offer (a draft,
# approved by the owner, SITE-01).
# --------------------------------------------------------------------------- #

UI: dict[str, dict[str, str]] = {
    "sr": {
        "brand": "TenderSRB",
        "nav_login": "Prijava",
        "nav_signup": "Registracija",
        "nav_cabinet": "Kabinet",
        "nav_plans": "Cenovnik",
        "nav_help": "Pomoć",
        "nav_tenderi": "Tenderi",
        "nav_sources": "Izvori",
        "nav_cpv": "CPV šifre",
        "nav_stats": "Statistika",
        "nav_news": "Novosti",
        "nav_contact": "Kontakt",
        "skip_link": "Preskoči na sadržaj",
        "aria_lang": "Jezik",
        "to_top": "Na vrh",
        "footer_privacy": "Politika privatnosti",
        "footer_terms": "Uslovi korišćenja",
        "footer_impressum": "Impresum",
        "footer_rights": "Sva prava zadržana.",
    },
    "ru": {
        "brand": "TenderSRB",
        "nav_login": "Вход",
        "nav_signup": "Регистрация",
        "nav_cabinet": "Кабинет",
        "nav_plans": "Тарифы",
        "nav_help": "Помощь",
        "nav_tenderi": "Тендеры",
        "nav_sources": "Источники",
        "nav_cpv": "Коды CPV",
        "nav_stats": "Статистика",
        "nav_news": "Новости",
        "nav_contact": "Контакты",
        "skip_link": "Перейти к содержимому",
        "aria_lang": "Язык",
        "to_top": "Наверх",
        "footer_privacy": "Политика конфиденциальности",
        "footer_terms": "Условия использования",
        "footer_impressum": "Импрессум",
        "footer_rights": "Все права защищены.",
    },
    "en": {
        "brand": "TenderSRB",
        "nav_login": "Sign in",
        "nav_signup": "Sign up",
        "nav_cabinet": "Cabinet",
        "nav_plans": "Pricing",
        "nav_help": "Help",
        "nav_tenderi": "Tenders",
        "nav_sources": "Sources",
        "nav_cpv": "CPV codes",
        "nav_stats": "Statistics",
        "nav_news": "News",
        "nav_contact": "Contact",
        "skip_link": "Skip to content",
        "aria_lang": "Language",
        "to_top": "Back to top",
        "footer_privacy": "Privacy Policy",
        "footer_terms": "Terms of Use",
        "footer_impressum": "Imprint",
        "footer_rights": "All rights reserved.",
    },
}

LANDING: dict[str, dict[str, object]] = {
    "sr": {
        "hero_title": "Javne nabavke u Srbiji — samo one koje su za vas",
        "hero_subtitle": (
            "Proveravamo izvore javnih nabavki ({sources}) {interval} i šaljemo vam tendere po "
            "vašim ključnim rečima i CPV-kodovima — u Telegram ili na email. AI uz tendere "
            "kaže da li vredi učestvovati i šta treba pripremiti; obim AI analize zavisi od tarifa."
        ),
        "ai_title": "AI koji vam kaže šta da radite, ne samo šta se desilo",
        "ai_subtitle": (
            "Za poklapanja možete zatražiti gotov AI-odgovor, ne sirove podatke za "
            "dalju analizu. Obim zavisi od vašeg tarifa."
        ),
        "ai_included": (
            "Uključeno u Pro (400 mesečno) i Business (1500). U probnom periodu — "
            "demo: 10 automatskih AI-analiza tendera za ceo probni period, zatim samo na plaćenom tarifu."
        ),
        "ai_features": [
            {
                "icon": "🎯",
                "title": "Vredi li se prijaviti?",
                "text": (
                    "AI ocenjuje tender (da/ne) na osnovu tehničke specifikacije "
                    "i vašeg šablona — pre nego što utrošite vreme na dokumentaciju. "
                    "Kod TED kartica specifikacija često nije u objavi, pa ocena može biti ograničena."
                ),
            },
            {
                "icon": "📋",
                "title": "Šta treba da pripremim?",
                "text": (
                    "Ček-lista dokumenata i uslova za konkretan tender — bez "
                    "ručnog čitanja konkursne dokumentacije."
                ),
            },
            {
                "icon": "📝",
                "title": "Nacrt ponude",
                "text": (
                    "AI priprema radni nacrt teksta ponude; vi ga pregledate i "
                    "dorađujete."
                ),
            },
            {
                "icon": "🏛",
                "title": "Ko obično pobeđuje?",
                "text": (
                    "Analitika naručioca: istorija dodela ugovora i konkurencija "
                    "na sličnim tenderima."
                ),
            },
        ],
        "how_title": "Kako funkcioniše",
        "how_steps": [
            "Podesite šablon: ključne reči i CPV-kategorije vaše delatnosti.",
            "Sistem proverava izvore javnih nabavki ({sources}) {interval}. {interval_note}",
            "AI rangira tendere po relevantnosti i objašnjava zašto vam odgovaraju; "
            "dubinska analiza na zahtev.",
            "Dobijate poklapanja u Telegramu ili na email-u i u kabinetu.",
        ],
        "icp_title": "Za koga",
        "icp_text": (
            "Za firme i preduzetnike koji učestvuju u javnim nabavkama u Srbiji — "
            "građevina, IT, usluge, isporuke."
        ),
        "trial_note": "{trial} Pro besplatno · samo email, bez kartice",
        "trial_after": (
            "Posle probnog perioda slanje staje, a kabinet ostaje otvoren; "
            "produženje do 30 dana možete zatražiti zahtevom iz kabineta — odluku donosi operater."
        ),
        "cta": "Registracija",
        "cta_cabinet": "Moj kabinet",
        "hero_cta_plans": "Pogledaj cenovnik",
        "facts_open": "Otvorenih tendera sada: {n}",
        "facts_updated_today": "ažurirano danas",
        "facts_updated_on": "ažurirano {date}",
        "example_title": "Primer: najnoviji otvoreni tender",
        "facts_sources_one": "{n}\u00a0izvor",
        "facts_sources_few": "{n}\u00a0izvora",
        "facts_sources_many": "{n}\u00a0izvora",
        "facts_countries_one": "{n}\u00a0zemlja",
        "facts_countries_few": "{n}\u00a0zemlje",
        "facts_countries_many": "{n}\u00a0zemalja",
        "q_title": "Šta vas zanima?",
        "q_open_title": "Šta je otvoreno sada?",
        "q_soon_title": "Tenderi po oblasti i regionu",
        "q_soon_sub": "Pregled po oblastima i regionima",
        "q_win_title": "Ko pobeđuje i po kojoj ceni?",
        "q_win_sub": "Statistika: pobednici i cene",
        "ex_title": "Primer: šta dobijate uz tender",
        "ex_caption": "Primer AI analize od {date}",
        "ex_customer": "Naručilac",
        "ex_amount": "Vrednost",
        "ex_verdict_label": "Procena",
        "ex_verdict_caution": "Učešće uz ograde",
        "ex_score": "Ocena {score}/100",
        "ex_pros": "Prednosti",
        "ex_cons": "Rizici",
        "ex_lang_note": "Primer, skraćeno — prevod sa ruskog originala analize.",
        "ex_link": "Otvori javnu karticu tendera",
        "ex_button_note": "Procenu „da li vredi učestvovati“ pokrećete dugmetom u ličnom kabinetu, na tarifi Pro i višoj (uključujući probni period); ne radi se samo od sebe.",
        "ex_real": "Realan primer iz baze tendera.",
        "ai_disclaimer": "AI može da greši — proverite sa dokumentacijom nabavke.",
    },
    "ru": {
        "hero_title": "Госзакупки Сербии — только те, что подходят вам",
        "hero_subtitle": (
            "Проверяем источники госзакупок ({sources}) {interval} и присылаем тендеры по вашим "
            "ключевым словам и CPV-кодам — в Telegram или на email. AI подсказывает, стоит ли "
            "участвовать и что подготовить; объём AI-разбора зависит от тарифа."
        ),
        "ai_title": "AI, который подсказывает решение, а не просто данные",
        "ai_subtitle": (
            "По совпадениям можно запросить готовый AI-разбор, а не сырые данные для "
            "самостоятельного анализа. Объём зависит от тарифа."
        ),
        "ai_included": (
            "Входит в Pro (400 в месяц) и Business (1500). В пробном периоде — "
            "демо: 10 авто-разборов тендеров на весь пробный период, дальше — только на платном тарифе."
        ),
        "ai_features": [
            {
                "icon": "🎯",
                "title": "Стоит ли участвовать?",
                "text": (
                    "AI-вердикт (bid/no-bid) на основе техзадания и вашего "
                    "шаблона — до того, как вы потратите время на документы. "
                    "У карточек TED техзадание часто не публикуется, поэтому вердикт может быть ограничен."
                ),
            },
            {
                "icon": "📋",
                "title": "Что подготовить?",
                "text": (
                    "Чек-лист документов и требований для конкретного тендера — "
                    "без ручного чтения конкурсной документации."
                ),
            },
            {
                "icon": "📝",
                "title": "Черновик заявки",
                "text": (
                    "AI готовит рабочий черновик текста заявки — вы проверяете "
                    "и дорабатываете."
                ),
            },
            {
                "icon": "🏛",
                "title": "Кто обычно выигрывает?",
                "text": (
                    "Аналитика по заказчику: история присуждений и конкуренты "
                    "на похожих тендерах."
                ),
            },
        ],
        "how_title": "Как это работает",
        "how_steps": [
            "Настройте шаблон: ключевые слова и CPV-категории вашей сферы.",
            "Система проверяет источники госзакупок ({sources}) {interval}. {interval_note}",
            "AI ранжирует тендеры по релевантности и объясняет, чем они подходят; "
            "глубокий разбор — по запросу.",
            "Получаете совпадения в Telegram или на email и в кабинете.",
        ],
        "icp_title": "Для кого",
        "icp_text": (
            "Для компаний и предпринимателей, участвующих в госзакупках Сербии: "
            "строительство, ИТ, услуги, поставки."
        ),
        "trial_note": "{trial} Pro бесплатно · только email, без карты",
        "trial_after": (
            "После пробного периода рассылка останавливается, а кабинет "
            "остаётся открытым; продление до 30 дней — заявкой из кабинета, решение принимает оператор."
        ),
        "cta": "Регистрация",
        "cta_cabinet": "Мой кабинет",
        "hero_cta_plans": "Смотреть тарифы",
        "facts_open": "Открытых тендеров сейчас: {n}",
        "facts_updated_today": "обновлено сегодня",
        "facts_updated_on": "обновлено {date}",
        "example_title": "Пример: самый свежий открытый тендер",
        "facts_sources_one": "{n}\u00a0источник",
        "facts_sources_few": "{n}\u00a0источника",
        "facts_sources_many": "{n}\u00a0источников",
        "facts_countries_one": "{n}\u00a0страна",
        "facts_countries_few": "{n}\u00a0страны",
        "facts_countries_many": "{n}\u00a0стран",
        "q_title": "Что вас интересует?",
        "q_open_title": "Что открыто сейчас?",
        "q_soon_title": "Тендеры по отраслям и регионам",
        "q_soon_sub": "Разделы CPV и регионы",
        "q_win_title": "Кто побеждает и по какой цене?",
        "q_win_sub": "Статистика: победители и цены",
        "ex_title": "Пример: что вы получаете по тендеру",
        "ex_caption": "Пример AI-разбора от {date}",
        "ex_customer": "Заказчик",
        "ex_amount": "Сумма",
        "ex_verdict_label": "Вердикт",
        "ex_verdict_caution": "Участвовать с оговорками",
        "ex_score": "Оценка {score}/100",
        "ex_pros": "Плюсы",
        "ex_cons": "Риски",
        "ex_lang_note": "Текст разбора — как сгенерирован (на русском).",
        "ex_link": "Открыть публичную карточку тендера",
        "ex_button_note": "Вердикт «стоит ли участвовать» запускается кнопкой в личном кабинете — на тарифе Pro и выше (включая пробный период); сам по себе он не выполняется.",
        "ex_real": "Реальный пример из базы тендеров.",
        "ai_disclaimer": "AI может ошибаться — сверяйте с документацией закупки.",
    },
    "en": {
        "hero_title": "Serbian public tenders — only the ones that fit you",
        "hero_subtitle": (
            "We check public procurement sources ({sources}) {interval} and send you tenders "
            "matching your keywords and CPV codes — to Telegram or email. AI tells you whether "
            "a tender is worth bidding and what to prepare; the volume of AI analysis depends on your plan."
        ),
        "ai_title": "AI that tells you what to do, not just what happened",
        "ai_subtitle": (
            "For matches you can request a finished AI review, not raw data to "
            "sift through yourself. The volume depends on your plan."
        ),
        "ai_included": (
            "Included in Pro (400 a month) and Business (1500). During the trial — "
            "a demo: 10 automatic tender analyses for the whole trial period, then only on a paid plan."
        ),
        "ai_features": [
            {
                "icon": "🎯",
                "title": "Is it worth bidding?",
                "text": (
                    "AI bid/no-bid verdict based on the technical specification "
                    "and your template — before you spend time on paperwork. "
                    "TED cards often don't publish the specification, so the verdict may be limited."
                ),
            },
            {
                "icon": "📋",
                "title": "What do I need to prepare?",
                "text": (
                    "A checklist of documents and requirements for this "
                    "specific tender — no manual reading of the tender "
                    "documentation."
                ),
            },
            {
                "icon": "📝",
                "title": "Draft bid",
                "text": (
                    "AI drafts a working version of your bid text — you "
                    "review and refine it."
                ),
            },
            {
                "icon": "🏛",
                "title": "Who usually wins?",
                "text": (
                    "Contracting authority analytics: award history and "
                    "competitors on similar tenders."
                ),
            },
        ],
        "how_title": "How it works",
        "how_steps": [
            "Set up a template: keywords and CPV categories for your field.",
            "The system checks public procurement sources ({sources}) {interval}. {interval_note}",
            "AI ranks tenders by relevance and explains why they fit; "
            "deep analysis on demand.",
            "Receive matches in Telegram or by email, and on your dashboard.",
        ],
        "icp_title": "Who it's for",
        "icp_text": (
            "For companies and entrepreneurs bidding on Serbian public tenders: "
            "construction, IT, services, supply."
        ),
        "trial_note": "{trial} of Pro free · email only, no card",
        "trial_after": "After the trial delivery stops and the cabinet stays open; you can request an extension of up to 30 days from the cabinet — the operator decides.",
        "cta": "Sign up",
        "cta_cabinet": "My cabinet",
        "hero_cta_plans": "See pricing",
        "facts_open": "Open tenders now: {n}",
        "facts_updated_today": "updated today",
        "facts_updated_on": "updated {date}",
        "example_title": "Example: the newest open tender",
        "facts_sources_one": "{n}\u00a0source",
        "facts_sources_few": "{n}\u00a0sources",
        "facts_sources_many": "{n}\u00a0sources",
        "facts_countries_one": "{n}\u00a0country",
        "facts_countries_few": "{n}\u00a0countries",
        "facts_countries_many": "{n}\u00a0countries",
        "q_title": "What are you looking for?",
        "q_open_title": "What is open now?",
        "q_soon_title": "Tenders by sector and region",
        "q_soon_sub": "Sectors (CPV) and regions",
        "q_win_title": "Who wins and at what price?",
        "q_win_sub": "Statistics: winners and prices",
        "ex_title": "Example: what you get for a tender",
        "ex_caption": "Example AI analysis from {date}",
        "ex_customer": "Contracting authority",
        "ex_amount": "Value",
        "ex_verdict_label": "Verdict",
        "ex_verdict_caution": "Participate with caveats",
        "ex_score": "Score {score}/100",
        "ex_pros": "Pros",
        "ex_cons": "Risks",
        "ex_lang_note": "Example, abridged — translated from the original Russian analysis.",
        "ex_link": "Open the public tender card",
        "ex_button_note": "The “is it worth bidding” verdict is run with a button in your account, on the Pro plan and above (including the trial period); it does not run on its own.",
        "ex_real": "A real example from the tender database.",
        "ai_disclaimer": "AI can make mistakes — check against the procurement documentation.",
    },
}


# The feature keys (features) ↔ src.plans.catalog.FEATURE_KEYS. The value matrix on
# the showcase is a derivative of the gating (test link: test_feature_matrix_matches_gating).
# The i18n parity of the sr / ru / en keys is mandatory (test test_plans_i18n_key_parity).
PLANS: dict[str, dict] = {
    "sr": {
        "title": "Cenovnik",
        "subtitle": "Bez godišnjeg ugovora. Otkazivanje u svakom trenutku.",
        "sources_link": "Odakle dolaze tenderi i šta dobijate iz svakog izvora",
        "per_month": "/ mes.",
        "on_request": "Po dogovoru",
        "no_contract": "Bez godišnjeg ugovora",
        "current_plan": "Vaš plan",
        "unlimited": "bez limita",
        "all_plans_note": "U svim planovima — skeniranje izvora javnih nabavki {interval}.",
        "delivery_cap_note": "Isporuka u Telegram i na e-poštu: do {cap} tendera dnevno po šablonu; na sajtu su vidljivi svi tenderi.",
        "features_heading": "Šta je uključeno",
        "comparison_heading": "Poređenje planova",
        "comparison_col_feature": "Mogućnost",
        "comparison_note": "Limiti dubinske analize i nacrta se obnavljaju mesečno.",
        "trial_ai_note": "AI-odabir: 400 mesečno je na plaćenom Pro. U probnom periodu — demo: 10 automatskih analiza tendera za ceo probni period.",
        "trial_reports_note": "Analitika i izveštaji (Excel) na Pro su označeni crticom, ali su u probnom periodu dostupni.",
        "faq_heading": "Česta pitanja o tarifama",
        "faq": (
            {
                "q": "Mogu li da promenim ili otkažem plan?",
                "a": (
                    "Da. Plan menjate u svakom trenutku, bez godišnjeg ugovora; "
                    "posle otkazivanja pristup ostaje do kraja plaćenog perioda."
                ),
            },
            {
                "q": "Šta je šablon pretrage i koliko ih dobijam?",
                "a": (
                    "Šablon je skup ključnih reči i CPV-kategorija po kojima pratite "
                    "nabavke. Pro uključuje 3, a Business 10 šablona."
                ),
            },
            {
                "q": "Šta se dešava posle probnog perioda?",
                "a": (
                    "Posle {trial} slanje obaveštenja se zaustavlja, a kabinet "
                    "ostaje otvoren. Slanje možete produžiti do 30 dana jednim "
                    "zahtevom iz kabineta (odluku donosi operater), a za stalno slanje "
                    "pošaljite zahtev za pretplatu (Pro ili Business) — "
                    "javićemo vam se sa uslovima plaćanja."
                ),
            },
            {
                "q": "U kojoj valuti se plaća?",
                "a": (
                    "Cene su iskazane u evrima; pored je približan iznos u dinarima "
                    "po kursu koji važi na dan naplate."
                ),
            },
        ),
        "cta_free": "Počnite {trial} besplatno",
        "cta_upgrade": "Zatraži pretplatu",
        "cta_upgrade_hint": "Pošaljite zahtev — javićemo vam se sa uslovima plaćanja.",
        "trial_banner": "Svaki novi nalog dobija {trial} Pro besplatno.",
        "cta_checkout": "Pretplatite se",
        "cta_soon": "Uskoro",
        "cta_soon_hint": "Plaćanje još nije aktivirano.",
        "cta_signup": "Registrujte se",
        "cta_contact": "Kontaktirajte nas",
        "notice_requested": (
            "Plaćanje je u pripremi. Zahtev za pretplatu je poslat administratoru "
            "— odluka u roku od jednog radnog dana."
        ),
        "notice_pending": "Zahtev je već u obradi.",
        "notice_active": "Pristup je već aktivan.",
        "features": {
            "profiles": "Šabloni pretrage",
            "email": "Obaveštenja na email",
            "telegram": "Obaveštenja u Telegramu",
            "ai_match": "AI-odabir i objašnjenje relevantnosti",
            "ai_deep": "Dubinska AI-analiza specifikacije",
            "bid": "Procena: učestvovati ili ne",
            "checklist": "Ček-lista dokumenata za prijavu",
            "draft": "Nacrt ponude",
            "analytics": "Analitika naručioca, konkurenata i ponovnih nabavki",
            "reports": "Analitika i izveštaji (Excel)",
        },
        "plans": {
            "free": {
                "name": "Free",
                "desc": "Posle probnog perioda — pregled tendera na sajtu.",
            },
            "pro": {
                "name": "Pro",
                "desc": "Za aktivne učesnike javnih nabavki.",
            },
            "business": {
                "name": "Business",
                "desc": "Za firme sa više delatnosti.",
            },
            "enterprise": {
                "name": "Enterprise",
                "desc": "Za organizacije sa specifičnim potrebama.",
            },
        },
    },
    "ru": {
        "title": "Тарифы",
        "subtitle": "Без годового контракта. Отмена в любое время.",
        "sources_link": "Откуда приходят тендеры и что вы получаете по каждому источнику",
        "per_month": "/ мес.",
        "on_request": "По запросу",
        "no_contract": "Без годового контракта",
        "current_plan": "Ваш план",
        "unlimited": "без лимита",
        "all_plans_note": "Во всех планах — сканирование источников госзакупок {interval}.",
        "delivery_cap_note": "Доставка в Telegram и на почту: до {cap} тендеров в день на шаблон; на сайте видны все тендеры.",
        "features_heading": "Что входит",
        "comparison_heading": "Сравнение планов",
        "comparison_col_feature": "Возможность",
        "comparison_note": "Лимиты глубокого разбора и черновиков обновляются ежемесячно.",
        "trial_ai_note": "Подбор AI: 400 в месяц — на платном Pro. В пробном периоде — демо: 10 авто-разборов тендеров на весь пробный период.",
        "trial_reports_note": "Аналитика и отчёты (Excel) на Pro отмечены прочерком, но в пробном периоде доступны.",
        "faq_heading": "Частые вопросы о тарифах",
        "faq": (
            {
                "q": "Можно ли сменить или отменить план?",
                "a": (
                    "Да. План меняется в любой момент, без годового контракта; "
                    "после отмены доступ сохраняется до конца оплаченного периода."
                ),
            },
            {
                "q": "Что такое шаблон поиска и сколько их даётся?",
                "a": (
                    "Шаблон — это набор ключевых слов и CPV-категорий, по которым "
                    "вы отслеживаете закупки. Pro включает 3, Business — 10."
                ),
            },
            {
                "q": "Что происходит после пробного периода?",
                "a": (
                    "Через {trial} рассылка уведомлений останавливается, а "
                    "кабинет остаётся открытым. Рассылку можно продлить до 30 дней "
                    "одной заявкой из кабинета — решение принимает оператор, а для "
                    "постоянной рассылки отправьте заявку на подписку (Pro "
                    "или Business) — мы свяжемся с условиями оплаты."
                ),
            },
            {
                "q": "В какой валюте оплата?",
                "a": (
                    "Цены указаны в евро; рядом приведён примерный эквивалент в "
                    "динарах по курсу на день списания."
                ),
            },
        ),
        "cta_free": "Начните {trial} бесплатно",
        "cta_upgrade": "Запросить подписку",
        "cta_upgrade_hint": "Отправьте заявку — мы свяжемся с условиями оплаты.",
        "trial_banner": "Каждый новый аккаунт получает {trial} Pro бесплатно.",
        "cta_checkout": "Оформить",
        "cta_soon": "Скоро",
        "cta_soon_hint": "Оплата пока не подключена.",
        "cta_signup": "Зарегистрироваться",
        "cta_contact": "Связаться с нами",
        "notice_requested": (
            "Оплата в доработке. Запрос на подписку отправлен администратору "
            "— решение в течение 1 рабочего дня."
        ),
        "notice_pending": "Заявка уже на рассмотрении.",
        "notice_active": "Доступ уже активен.",
        "features": {
            "profiles": "Шаблоны поиска",
            "email": "Уведомления на email",
            "telegram": "Уведомления в Telegram",
            "ai_match": "AI-подбор и объяснение релевантности",
            "ai_deep": "Глубокий AI-разбор спецификации",
            "bid": "Вердикт «участвовать или нет»",
            "checklist": "Чек-лист документов для заявки",
            "draft": "Черновик заявки",
            "analytics": "Аналитика заказчика, конкурентов и перезакупок",
            "reports": "Аналитика и отчёты (Excel)",
        },
        "plans": {
            "free": {
                "name": "Free",
                "desc": "После пробного периода — просмотр тендеров на сайте.",
            },
            "pro": {
                "name": "Pro",
                "desc": "Для активных участников госзакупок.",
            },
            "business": {
                "name": "Business",
                "desc": "Для компаний.",
            },
            "enterprise": {
                "name": "Enterprise",
                "desc": "Для организаций с особыми требованиями.",
            },
        },
    },
    "en": {
        "title": "Pricing",
        "subtitle": "No annual contract. Cancel any time.",
        "sources_link": "Where tenders come from and what you get from each source",
        "per_month": "/ mo",
        "on_request": "On request",
        "no_contract": "No annual contract",
        "current_plan": "Your plan",
        "unlimited": "unlimited",
        "all_plans_note": "All plans include scanning of public procurement sources {interval}.",
        "delivery_cap_note": "Delivery to Telegram and email: up to {cap} tenders a day per template; the website shows all of them.",
        "features_heading": "What's included",
        "comparison_heading": "Compare plans",
        "comparison_col_feature": "Feature",
        "comparison_note": "Deep-analysis and draft limits reset every month.",
        "trial_ai_note": "AI matching: 400 a month is on the paid Pro. During the trial — a demo: 10 automatic tender analyses for the whole trial period.",
        "trial_reports_note": "Analytics & reports (Excel) show a dash on Pro, but are available during the trial.",
        "faq_heading": "Pricing FAQ",
        "faq": (
            {
                "q": "Can I change or cancel my plan?",
                "a": (
                    "Yes. You can change plans at any time, with no annual contract; "
                    "after cancelling, access stays until the end of the paid period."
                ),
            },
            {
                "q": "What is a search template and how many do I get?",
                "a": (
                    "A template is a set of keywords and CPV categories you track "
                    "tenders by. Pro includes 3 and Business 10 templates."
                ),
            },
            {
                "q": "What happens after the trial period?",
                "a": (
                    "After {trial}, notification delivery stops and the cabinet "
                    "stays open. You can request an extension of delivery by up to "
                    "30 days with one request from the cabinet — the operator decides; for ongoing "
                    "delivery send a subscription request (Pro or Business) — "
                    "we will contact you with payment terms."
                ),
            },
            {
                "q": "What currency am I billed in?",
                "a": (
                    "Prices are shown in euros; an approximate amount in dinars is "
                    "shown next to it, at the rate on the billing date."
                ),
            },
        ),
        "cta_free": "Start {trial} free",
        "cta_upgrade": "Request subscription",
        "cta_upgrade_hint": "Send a request — we will contact you with payment terms.",
        "trial_banner": "Every new account gets {trial} of Pro for free.",
        "cta_checkout": "Subscribe",
        "cta_soon": "Coming soon",
        "cta_soon_hint": "Payments are not enabled yet.",
        "cta_signup": "Sign up",
        "cta_contact": "Contact us",
        "notice_requested": (
            "Payments are being finalized. Your subscription request has been sent "
            "to the administrator — a decision within one business day."
        ),
        "notice_pending": "A request is already pending review.",
        "notice_active": "Your access is already active.",
        "features": {
            "profiles": "Search templates",
            "email": "Email notifications",
            "telegram": "Telegram notifications",
            "ai_match": "AI matching with relevance explanation",
            "ai_deep": "Deep AI specification analysis",
            "bid": "Bid / no-bid verdict",
            "checklist": "Application document checklist",
            "draft": "Bid draft",
            "analytics": "Buyer, competitor & re-procurement analytics",
            "reports": "Analytics & reports (Excel)",
        },
        "plans": {
            "free": {
                "name": "Free",
                "desc": "After the trial — browse tenders on the site.",
            },
            "pro": {
                "name": "Pro",
                "desc": "For active procurement participants.",
            },
            "business": {
                "name": "Business",
                "desc": "For companies with multiple business lines.",
            },
            "enterprise": {
                "name": "Enterprise",
                "desc": "For organisations with specific requirements.",
            },
        },
    },
}


HELP: dict[str, dict[str, object]] = {
    "sr": {
        "title": "Pomoć",
        "subtitle": (
            "Vodič kroz TenderSRB: brzi start, AI-funkcije, Telegram-bot, "
            "tarife i najčešća pitanja. Sve o tome kako da izvučete maksimum."
        ),
        "sections": [
            {
                "id": "start",
                "title": "Brzi start",
                "icon": "🚀",
                "body": [
                    "Tri koraka do prvog tendera u vašem Telegramu — posle "
                    "podešavanja sistem radi automatski.",
                ],
                "steps": [
                    {
                        "title": "Napravite šablon pretrage",
                        "text": (
                            "Unesite ključne reči i/ili CPV-kategorije vaše "
                            "delatnosti, opciono budžet i rok do isteka."
                        ),
                    },
                    {
                        "title": "Povežite Telegram ili email",
                        "text": (
                            "U kabinetu kliknite „Povežite Telegram“ (bot "
                            "@nabrs_bot) ili izaberite email. Telegram je "
                            "dostupan na Pro i u probnom periodu."
                        ),
                    },
                    {
                        "title": "Primite prvi alert",
                        "text": (
                            "Sistem proverava izvore javnih nabavki {interval}; "
                            "odgovarajući tenderi stižu u bot ili na email i u kabinet po "
                            "vašem rasporedu."
                        ),
                    },
                ],
                "cta": {"href": "/signup", "label": "Počnite besplatno"},
            },
            {
                "id": "ai",
                "title": "AI-funkcije",
                "icon": "🤖",
                "body": [
                    "AI-funkcije su dostupne na tarifama Pro i višim. Svaku "
                    "pozivate direktno sa kartice tendera — u botu ili kabinetu.",
                ],
                "features": [
                    {
                        "name": "Odabir i objašnjenje relevantnosti",
                        "what": (
                            "Rangira poklapanja po relevantnosti i ukratko "
                            "objašnjava zašto vam tender odgovara."
                        ),
                        "how": "Stiže automatski uz alert.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Dubinska analiza specifikacije",
                        "what": (
                            "Razlaže tehničku specifikaciju: predmet, ključne "
                            "zahteve, rizike i zamke."
                        ),
                        "how": "Dugme „Dubinska analiza“ na kartici tendera.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Procena: učestvovati ili ne",
                        "what": (
                            "Analizira isplativost učešća i predlaže odluku "
                            "(ide / razmisliti / preskočiti) sa argumentima za "
                            "i protiv."
                        ),
                        "how": "Dugme „Da li se isplati“ na kartici tendera.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Ček-lista dokumentacije",
                        "what": (
                            "Sastavlja spisak dokumenata i uslova potrebnih za "
                            "podnošenje prijave."
                        ),
                        "how": "Dugme „Ček-lista“ na kartici tendera.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Nacrt ponude",
                        "what": (
                            "Priprema nacrt ponude na osnovu zahteva tendera "
                            "— ostaje da ga doradite po sebi."
                        ),
                        "how": "Dugme „Nacrt“ na kartici tendera.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Analitika naručioca i konkurencije",
                        "what": (
                            "Prikazuje istoriju naručioca, aktivne konkurente "
                            "i prognozu ponovnih nabavki."
                        ),
                        "how": "Odeljak analitike na kartici tendera.",
                        "tier": "Pro+",
                    },
                ],
            },
            {
                "id": "telegram",
                "title": "Telegram-bot",
                "icon": "💬",
                "body": [
                    "Dnevno stiže do {cap} tendera po šablonu u Telegram i na "
                    "e-poštu; na sajtu su vidljivi svi tenderi po vašem šablonu. "
                    "Kada se limit dostigne, bot vas o tome obaveštava i šalje "
                    "vezu ka listi na sajtu.",
                    "Bot @nabrs_bot šalje poklapanja po vašem šablonu i nudi "
                    "AI-akcije direktno na kartici tendera. Upravljanje — "
                    "jednostavnim komandama:",
                ],
                "commands": [
                    ("/start", "Registracija i podešavanje šablona pretrage"),
                    ("/help", "Spisak komandi i mogućnosti bota"),
                    ("/profile", "Prikaz trenutnih podešavanja šablona"),
                    ("/reset", "Ponovno podešavanje šablona"),
                    ("/watching", "Praćenje pobednika tendera"),
                    ("/list", "Poslednji odgovarajući tenderi"),
                    ("/status", "Status pretplate i tarifa"),
                ],
                "cta": {
                    "href": "https://t.me/nabrs_bot",
                    "label": "Otvorite bota",
                },
            },
            {
                "id": "sources",
                "title": "Izvori",
                "icon": "🌐",
                "body": [
                    "Pratimo izvore javnih nabavki: {sources}. Šaljemo samo aktuelne tendere koji su stigli posle uključenja izvora — arhiv se ne šalje.",
                    "Izvore biramo po šablonu: u kabinetu otvorite šablon i označite izvore u polju „Izvori“. Novi šablon podrazumevano prati srpske izvore; inostrane izvore uključujete sami.",
                ],
                "sources_heading": "Uključeni izvori:",
                "sources_more": "Šta dobijate iz svakog izvora i koliko često proveravamo nove objave",
                "sources_list": True,
            },
            {
                "id": "plans",
                "title": "Tarife i plaćanje",
                "icon": "💳",
                "body": [
                    "Novi nalog dobija {trial} Pro besplatno. Pro i Business su za "
                    "aktivno učešće: više šablona, isporuka u Telegram i mesečni "
                    "obim AI-analize.",
                    "Bez godišnjeg ugovora, otkazivanje u svakom trenutku. "
                    "Detalji i trenutni cenovnik su na stranici tarifa.",
                ],
                "cta": {"href": "/plans", "label": "Pogledajte cenovnik"},
            },
            {
                "id": "card",
                "title": "Kako čitati karticu tendera",
                "icon": "🗂️",
                "body": [
                    "Svaka kartica sažima ono što je bitno za odluku. Evo šta "
                    "znači koje polje.",
                ],
                "blocks": [
                    {
                        "h": "Naslov i naručilac",
                        "text": (
                            "Predmet nabavke i institucija koja je raspisuje "
                            "(naručilac). Po naručiocu procenjujete profil posla "
                            "i istoriju nabavki."
                        ),
                    },
                    {
                        "h": "Rok za podnošenje ponuda",
                        "text": (
                            "Datum i vreme do kog se prima ponuda. Kada rok "
                            "istekne, prijava više nije moguća — pratite ga rano."
                        ),
                    },
                    {
                        "h": "Procenjena vrednost",
                        "text": (
                            "Okvirni budžet koji je naručilac procenio. Pomaže "
                            "da brzo odlučite da li je posao vaše veličine."
                        ),
                    },
                    {
                        "h": "CPV i predmet",
                        "text": (
                            "CPV-kod je zvanična kategorija nabavke; predmet je "
                            "kratak opis. Po njima se kartica poklapa sa vašim "
                            "šablonom pretrage."
                        ),
                    },
                    {
                        "h": "Relevantnost (AI)",
                        "text": (
                            "Na Pro tarifama kartica nosi ocenu relevantnosti i "
                            "kratko objašnjenje zašto vam tender odgovara."
                        ),
                    },
                    {
                        "h": "Dokumentacija i akcije",
                        "text": (
                            "Linkovi ka konkursnoj dokumentaciji i dugmad za "
                            "AI-akcije (dubinska analiza, procena, ček-lista, "
                            "nacrt) nalaze se na samoj kartici."
                        ),
                    },
                ],
            },
            {
                "id": "bidnobid",
                "title": "Bid / no-bid: da li se isplati",
                "icon": "⚖️",
                "body": [
                    "„Bid ili no-bid“ je odluka da li da uložite trud i "
                    "podnesete ponudu — ili da preskočite tender i sačuvate "
                    "resurse za bolji.",
                ],
                "blocks": [
                    {
                        "h": "Šta znači",
                        "text": (
                            "Bid = idete na tender i pripremate ponudu. No-bid = "
                            "svesno preskačete jer šanse ili isplativost nisu "
                            "dovoljne."
                        ),
                    },
                    {
                        "h": "Kada ide (za)",
                        "text": (
                            "Predmet je u vašoj delatnosti, ispunjavate uslove, "
                            "rok je realan, a procenjena vrednost opravdava trud."
                        ),
                    },
                    {
                        "h": "Kada preskočiti (protiv)",
                        "text": (
                            "Prekratak rok, uslovi koje ne ispunjavate, nejasna "
                            "specifikacija ili marža premala da pokrije trošak "
                            "pripreme ponude."
                        ),
                    },
                    {
                        "h": "Kako AI pomaže",
                        "text": (
                            "Na Pro tarifama dugme „Da li se isplati“ daje "
                            "procenu (ide / razmisliti / preskočiti) sa "
                            "argumentima za i protiv — konačnu odluku donosite vi."
                        ),
                    },
                ],
            },
            {
                "id": "trial",
                "title": "Probni period od {trial}",
                "icon": "✨",
                "body": [
                    "Svaki novi nalog dobija {trial} Pro besplatno — sa slanjem obaveštenja i demo AI-analizom (10 tendera za ceo probni period). Posle toga slanje staje, a kabinet ostaje otvoren; slanje možete produžiti do 30 dana zahtevom iz kabineta (odluku donosi operater) ili poslati zahtev za pretplatu (Pro ili Business). Pun cenovnik je na stranici tarifa.",
                ],
                "cta": {"href": "/plans", "label": "Uporedite tarife"},
            },
            {
                "id": "glossary",
                "title": "Rečnik pojmova",
                "icon": "📖",
                "body": [
                    "Kratak rečnik termina javnih nabavki u Srbiji na koje "
                    "nailazite u karticama i dokumentaciji.",
                ],
                "glossary": [
                    {
                        "term": "Naručilac",
                        "desc": (
                            "Institucija ili preduzeće koje raspisuje nabavku i "
                            "ocenjuje ponude."
                        ),
                    },
                    {
                        "term": "Ponuđač",
                        "desc": (
                            "Firma koja podnosi ponudu na tender — u većini "
                            "slučajeva to ste vi."
                        ),
                    },
                    {
                        "term": "Portal javnih nabavki",
                        "desc": (
                            "Zvanični državni portal (jnportal.src-ujn.example.invalid) na "
                            "kom se objavljuju nabavke; jedan od izvora koje sistem "
                            "prati."
                        ),
                    },
                    {
                        "term": "Konkursna dokumentacija",
                        "desc": (
                            "Set dokumenata naručioca sa uslovima, "
                            "specifikacijom i obrascima za podnošenje ponude."
                        ),
                    },
                    {
                        "term": "CPV",
                        "desc": (
                            "Zajednički rečnik nabavki — zvanična klasifikacija "
                            "predmeta po kodovima, koristi se za filtriranje."
                        ),
                    },
                    {
                        "term": "Okvirni sporazum",
                        "desc": (
                            "Sporazum sa jednim ili više ponuđača koji utvrđuje "
                            "uslove za buduće nabavke tokom određenog perioda."
                        ),
                    },
                    {
                        "term": "Otvoreni postupak",
                        "desc": (
                            "Postupak u kom ponudu može podneti svako "
                            "zainteresovano lice u zadatom roku."
                        ),
                    },
                    {
                        "term": "Obaveštenja o nabavci i dodeli ugovora",
                        "desc": (
                            "Standardizovani obrasci obaveštenja: poziv/"
                            "obaveštenje o nabavci i obaveštenje o dodeli "
                            "(ishodu) ugovora."
                        ),
                    },
                    {
                        "term": "Procenjena vrednost nabavke",
                        "desc": (
                            "Budžet koji je naručilac procenio za nabavku, bez "
                            "obavezujuće konačne cene."
                        ),
                    },
                    {
                        "term": "Rok za podnošenje ponuda",
                        "desc": (
                            "Krajnji datum i vreme do kog naručilac prima "
                            "ponude; posle njega prijava nije moguća."
                        ),
                    },
                ],
            },
            {
                "id": "faq",
                "title": "Česta pitanja",
                "icon": "❓",
                "faq_items": [
                    {
                        "q": "Koliko često se pretražuju tenderi?",
                        "a": "Sistem proverava izvore javnih nabavki ({sources}) {interval}. {interval_note} Portal objavljuje nove nabavke jednom noćnom serijom, pa se najviše novih tendera pojavi noću.",
                    },
                    {
                        "q": "Da li mogu da imam više šablona pretrage?",
                        "a": (
                            "Da, broj šablona zavisi od tarife — pogledajte "
                            "stranicu tarifa."
                        ),
                    },
                    {
                        "q": "Kako rade ključne reči i stop-reči?",
                        "a": (
                            "Svaki red je AND-grupa (svi termini odvojeni "
                            "zarezom moraju da se poklope), a redovi međusobno "
                            "rade kao OR. Stop-reči isključuju tendere koji ih "
                            "sadrže."
                        ),
                    },
                    {
                        "q": "Šta su CPV-kodovi?",
                        "a": (
                            "Zvanična klasifikacija nabavki. Filtriranje po "
                            "kodovima radi nezavisno od ključnih reči — možete "
                            "kombinovati oba načina."
                        ),
                    },
                    {
                        "q": "Kako da povežem Telegram?",
                        "a": (
                            "U kabinetu, na kontrolnoj tabli, dugme „Poveži "
                            "Telegram“ vodi vas kroz povezivanje naloga."
                        ),
                    },
                    {
                        "q": "Da li mi treba plaćena tarifa da počnem?",
                        "a": (
                            "Ne. Svaki novi nalog dobija {trial} Pro besplatno — "
                            "sa slanjem i AI-funkcijama. Posle probnog perioda "
                            "slanje i AI-funkcije rade na Pro i višim tarifama."
                        ),
                    },
                    {
                        "q": "Šta ako ne pronađem odgovor ovde?",
                        "a": "Pošaljite /help komandu botu ili nam pišite.",
                        "link_phrase": "nam pišite",
                    },
                ],
            },
        ],
    },
    "ru": {
        "title": "Помощь",
        "subtitle": (
            "Гид по TenderSRB: быстрый старт, AI-функции, Telegram-бот, "
            "тарифы и частые вопросы. Всё о том, как получить максимум."
        ),
        "sections": [
            {
                "id": "start",
                "title": "Быстрый старт",
                "icon": "🚀",
                "body": [
                    "Три шага до первого тендера в вашем Telegram — после "
                    "настройки система работает автоматически.",
                ],
                "steps": [
                    {
                        "title": "Создайте шаблон поиска",
                        "text": (
                            "Укажите ключевые слова и/или CPV-категории вашей "
                            "сферы, при желании — бюджет и запас дней до "
                            "дедлайна."
                        ),
                    },
                    {
                        "title": "Подключите Telegram или email",
                        "text": (
                            "В кабинете нажмите «Подключить Telegram» (бот "
                            "@nabrs_bot) или выберите email. Telegram доступен "
                            "на Pro и в пробном периоде."
                        ),
                    },
                    {
                        "title": "Получите первый алерт",
                        "text": (
                            "Система проверяет источники госзакупок {interval}; "
                            "подходящие тендеры приходят в бот или на email и в кабинет по "
                            "вашему расписанию."
                        ),
                    },
                ],
                "cta": {"href": "/signup", "label": "Начать бесплатно"},
            },
            {
                "id": "ai",
                "title": "AI-функции",
                "icon": "🤖",
                "body": [
                    "AI-функции доступны на тарифах Pro и выше. Каждую можно "
                    "вызвать прямо с карточки тендера — в боте или в кабинете.",
                ],
                "features": [
                    {
                        "name": "Подбор и объяснение релевантности",
                        "what": (
                            "Ранжирует совпадения по релевантности и коротко "
                            "объясняет, чем тендер вам подходит."
                        ),
                        "how": "Приходит автоматически вместе с алертом.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Глубокий разбор ТЗ",
                        "what": (
                            "Разбирает техническую спецификацию: предмет, "
                            "ключевые требования, риски и подводные камни."
                        ),
                        "how": "Кнопка «Глубокий анализ» на карточке тендера.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Вердикт «участвовать или нет»",
                        "what": (
                            "Оценивает целесообразность участия и даёт вердикт "
                            "(идти / подумать / пропустить) с аргументами за и "
                            "против."
                        ),
                        "how": "Кнопка «Стоит ли участвовать» на карточке.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Чек-лист документов",
                        "what": (
                            "Формирует список документов и условий, которые "
                            "нужны для подачи заявки."
                        ),
                        "how": "Кнопка «Чек-лист» на карточке тендера.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Черновик заявки",
                        "what": (
                            "Готовит черновик заявки на основе требований "
                            "тендера — остаётся отредактировать под себя."
                        ),
                        "how": "Кнопка «Черновик» на карточке тендера.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Аналитика заказчика и конкурентов",
                        "what": (
                            "Показывает историю заказчика, активных "
                            "конкурентов и прогноз перезакупок."
                        ),
                        "how": "Раздел аналитики на карточке тендера.",
                        "tier": "Pro+",
                    },
                ],
            },
            {
                "id": "telegram",
                "title": "Telegram-бот",
                "icon": "💬",
                "body": [
                    "В день приходит до {cap} тендеров на шаблон — в Telegram и "
                    "на почту; на сайте видны все тендеры по вашему шаблону. "
                    "Когда лимит достигнут, бот сообщает об этом и даёт ссылку "
                    "на ленту на сайте.",
                    "Бот @nabrs_bot присылает совпадения по вашему шаблону и "
                    "предлагает AI-действия прямо на карточке тендера. "
                    "Управление — простыми командами:",
                ],
                "commands": [
                    ("/start", "Регистрация и настройка шаблона поиска"),
                    ("/help", "Список команд и возможностей бота"),
                    ("/profile", "Показать текущие настройки шаблона"),
                    ("/reset", "Перенастроить шаблон заново"),
                    ("/watching", "Подписки на победителей тендеров"),
                    ("/list", "Последние подходящие тендеры"),
                    ("/status", "Статус подписки и тарифа"),
                ],
                "cta": {
                    "href": "https://t.me/nabrs_bot",
                    "label": "Открыть бота",
                },
            },
            {
                "id": "sources",
                "title": "Источники",
                "icon": "🌐",
                "body": [
                    "Мы отслеживаем источники госзакупок: {sources}. Присылаем только актуальные тендеры, пришедшие после включения источника, — архив не рассылается.",
                    "Источники выбираются в шаблоне: откройте шаблон в кабинете и отметьте площадки в поле «Источники». Новый шаблон по умолчанию следит за сербскими источниками; иностранные вы включаете сами.",
                ],
                "sources_heading": "Подключённые источники:",
                "sources_more": "Что даёт каждый источник и как часто мы проверяем новые объявления",
                "sources_list": True,
            },
            {
                "id": "plans",
                "title": "Тарифы и оплата",
                "icon": "💳",
                "body": [
                    "Новый аккаунт получает {trial} Pro бесплатно. Pro и Business — "
                    "для активного участия: больше шаблонов, доставка в Telegram "
                    "и месячный объём AI-анализа.",
                    "Без годового контракта, отмена в любой момент. "
                    "Подробности и актуальные цены — на странице тарифов.",
                ],
                "cta": {"href": "/plans", "label": "Смотреть тарифы"},
            },
            {
                "id": "card",
                "title": "Как читать карточку тендера",
                "icon": "🗂️",
                "body": [
                    "Каждая карточка сжимает то, что важно для решения. Вот что "
                    "означает каждое поле.",
                ],
                "blocks": [
                    {
                        "h": "Заголовок и заказчик",
                        "text": (
                            "Предмет закупки и организация-заказчик (naručilac). "
                            "По заказчику можно оценить профиль работы и историю "
                            "закупок."
                        ),
                    },
                    {
                        "h": "Дедлайн подачи",
                        "text": (
                            "Дата и время, до которых принимается заявка. После "
                            "истечения срока подать уже нельзя — следите заранее."
                        ),
                    },
                    {
                        "h": "Оценочная стоимость",
                        "text": (
                            "Ориентировочный бюджет, который оценил заказчик. "
                            "Помогает быстро понять, ваш ли это масштаб."
                        ),
                    },
                    {
                        "h": "CPV и предмет",
                        "text": (
                            "CPV-код — официальная категория закупки; предмет — "
                            "краткое описание. По ним карточка совпадает с вашим "
                            "шаблоном поиска."
                        ),
                    },
                    {
                        "h": "Релевантность (AI)",
                        "text": (
                            "На тарифах Pro карточка несёт оценку релевантности "
                            "и краткое объяснение, чем тендер вам подходит."
                        ),
                    },
                    {
                        "h": "Документация и действия",
                        "text": (
                            "Ссылки на конкурсную документацию и кнопки "
                            "AI-действий (глубокий анализ, вердикт, чек-лист, "
                            "черновик) находятся на самой карточке."
                        ),
                    },
                ],
            },
            {
                "id": "bidnobid",
                "title": "Bid / no-bid: стоит ли участвовать",
                "icon": "⚖️",
                "body": [
                    "«Bid или no-bid» — решение, вкладывать ли усилия и подавать "
                    "заявку, или пропустить тендер и сохранить ресурсы для "
                    "лучшего.",
                ],
                "blocks": [
                    {
                        "h": "Что это значит",
                        "text": (
                            "Bid = идёте на тендер и готовите заявку. No-bid = "
                            "осознанно пропускаете, потому что шансы или выгода "
                            "недостаточны."
                        ),
                    },
                    {
                        "h": "Когда идти (за)",
                        "text": (
                            "Предмет в вашей сфере, вы отвечаете условиям, срок "
                            "реалистичен, а оценочная стоимость оправдывает труд."
                        ),
                    },
                    {
                        "h": "Когда пропустить (против)",
                        "text": (
                            "Слишком короткий срок, невыполнимые условия, "
                            "неясная спецификация или маржа, не покрывающая "
                            "затраты на подготовку заявки."
                        ),
                    },
                    {
                        "h": "Как помогает AI",
                        "text": (
                            "На тарифах Pro кнопка «Стоит ли участвовать» даёт "
                            "вердикт (идти / подумать / пропустить) с аргументами "
                            "за и против — финальное решение за вами."
                        ),
                    },
                ],
            },
            {
                "id": "trial",
                "title": "Пробный период {trial}",
                "icon": "✨",
                "body": [
                    "Каждый новый аккаунт получает {trial} Pro бесплатно — с рассылкой уведомлений и демо AI-разбором (10 тендеров на весь пробный период). После этого рассылка останавливается, а кабинет остаётся открытым; рассылку можно продлить до 30 дней заявкой из кабинета (решение принимает оператор) или отправить заявку на подписку (Pro или Business). Полный прайс — на странице тарифов.",
                ],
                "cta": {"href": "/plans", "label": "Сравнить тарифы"},
            },
            {
                "id": "glossary",
                "title": "Словарь терминов",
                "icon": "📖",
                "body": [
                    "Краткий словарь терминов госзакупок Сербии, которые вы "
                    "встречаете в карточках и документации.",
                ],
                "glossary": [
                    {
                        "term": "Naručilac (заказчик)",
                        "desc": (
                            "Организация или предприятие, которое объявляет "
                            "закупку и оценивает заявки."
                        ),
                    },
                    {
                        "term": "Ponuđač (участник)",
                        "desc": (
                            "Компания, подающая заявку на тендер — в большинстве "
                            "случаев это вы."
                        ),
                    },
                    {
                        "term": "Portal javnih nabavki",
                        "desc": (
                            "Официальный госпортал (jnportal.src-ujn.example.invalid), где "
                            "публикуются закупки; один из источников, которые отслеживает система."
                        ),
                    },
                    {
                        "term": "Konkursna dokumentacija",
                        "desc": (
                            "Комплект документов заказчика с условиями, "
                            "спецификацией и формами для подачи заявки."
                        ),
                    },
                    {
                        "term": "CPV",
                        "desc": (
                            "Общий словарь закупок — официальная классификация "
                            "предмета по кодам, используется для фильтрации."
                        ),
                    },
                    {
                        "term": "Okvirni sporazum (рамочное соглашение)",
                        "desc": (
                            "Соглашение с одним или несколькими участниками, "
                            "фиксирующее условия будущих закупок на определённый "
                            "период."
                        ),
                    },
                    {
                        "term": "Otvoreni postupak (открытая процедура)",
                        "desc": (
                            "Процедура, в которой заявку может подать любое "
                            "заинтересованное лицо в заданный срок."
                        ),
                    },
                    {
                        "term": "Извещения о закупке и о заключении договора",
                        "desc": (
                            "Стандартные формы извещений: извещение/"
                            "приглашение к закупке и извещение о заключении "
                            "(результате) договора."
                        ),
                    },
                    {
                        "term": "Procenjena vrednost (оценочная стоимость)",
                        "desc": (
                            "Бюджет, который заказчик оценил для закупки, без "
                            "обязывающей итоговой цены."
                        ),
                    },
                    {
                        "term": "Rok za podnošenje (срок подачи)",
                        "desc": (
                            "Крайние дата и время приёма заявок заказчиком; "
                            "после них подача невозможна."
                        ),
                    },
                ],
            },
            {
                "id": "faq",
                "title": "Частые вопросы",
                "icon": "❓",
                "faq_items": [
                    {
                        "q": "Как часто ищутся тендеры?",
                        "a": "Система проверяет источники госзакупок ({sources}) {interval}. {interval_note} Портал публикует новые закупки одной ночной пачкой, поэтому основная часть новых тендеров появляется ночью.",
                    },
                    {
                        "q": "Можно ли завести несколько шаблонов поиска?",
                        "a": (
                            "Да, число шаблонов зависит от тарифа — "
                            "подробности на странице тарифов."
                        ),
                    },
                    {
                        "q": "Как работают ключевые слова и стоп-слова?",
                        "a": (
                            "Каждая строка — группа AND (все термины через "
                            "запятую должны совпасть), а строки между собой "
                            "работают как OR. Стоп-слова исключают тендеры с "
                            "этими словами."
                        ),
                    },
                    {
                        "q": "Что такое CPV-коды?",
                        "a": (
                            "Официальный классификатор закупок. Фильтрация по "
                            "кодам работает независимо от ключевых слов — можно "
                            "комбинировать оба способа."
                        ),
                    },
                    {
                        "q": "Как подключить Telegram?",
                        "a": (
                            "В кабинете на дашборде кнопка «Подключить "
                            "Telegram» проведёт вас через привязку аккаунта."
                        ),
                    },
                    {
                        "q": "Нужен ли платный тариф, чтобы начать?",
                        "a": (
                            "Нет. Каждый новый аккаунт получает {trial} Pro "
                            "бесплатно — с рассылкой и AI-функциями. После "
                            "пробного периода рассылка и AI работают на Pro и выше."
                        ),
                    },
                    {
                        "q": "Не нашли ответ здесь?",
                        "a": "Отправьте боту команду /help или напишите нам.",
                        "link_phrase": "напишите нам",
                    },
                ],
            },
        ],
    },
    "en": {
        "title": "Help",
        "subtitle": (
            "A guide to TenderSRB: quick start, AI features, the Telegram "
            "bot, pricing and FAQ. Everything to get the most out of it."
        ),
        "sections": [
            {
                "id": "start",
                "title": "Quick start",
                "icon": "🚀",
                "body": [
                    "Three steps to your first tender in Telegram — after "
                    "setup the system runs automatically.",
                ],
                "steps": [
                    {
                        "title": "Create a search template",
                        "text": (
                            "Enter keywords and/or CPV categories for your "
                            "field, optionally a budget range and minimum days "
                            "until deadline."
                        ),
                    },
                    {
                        "title": "Connect Telegram or email",
                        "text": (
                            "On your dashboard, click “Connect Telegram” (bot "
                            "@nabrs_bot) or choose email. Telegram is available "
                            "on Pro and during the trial."
                        ),
                    },
                    {
                        "title": "Get your first alert",
                        "text": (
                            "The system checks public procurement sources {interval}; "
                            "matching tenders arrive in the bot or by email, and on your "
                            "dashboard on your schedule."
                        ),
                    },
                ],
                "cta": {"href": "/signup", "label": "Start free"},
            },
            {
                "id": "ai",
                "title": "AI features",
                "icon": "🤖",
                "body": [
                    "AI features are available on Pro and higher plans. Each "
                    "one is triggered right from the tender card — in the bot "
                    "or on your dashboard.",
                ],
                "features": [
                    {
                        "name": "Relevance ranking and explanation",
                        "what": (
                            "Ranks matches by relevance and briefly explains "
                            "why a tender fits you."
                        ),
                        "how": "Arrives automatically with the alert.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Deep specification analysis",
                        "what": (
                            "Breaks down the technical specification: subject, "
                            "key requirements, risks and pitfalls."
                        ),
                        "how": "“Deep analysis” button on the tender card.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Bid worth-it verdict",
                        "what": (
                            "Assesses whether it's worth bidding and gives a "
                            "verdict (go / consider / skip) with pros and cons."
                        ),
                        "how": "“Worth bidding?” button on the tender card.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Document checklist",
                        "what": (
                            "Builds the list of documents and conditions "
                            "needed to submit a bid."
                        ),
                        "how": "“Checklist” button on the tender card.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Bid draft",
                        "what": (
                            "Drafts a bid based on the tender's "
                            "requirements — you just edit it to fit."
                        ),
                        "how": "“Draft” button on the tender card.",
                        "tier": "Pro+",
                    },
                    {
                        "name": "Buyer and competitor analytics",
                        "what": (
                            "Shows the buyer's history, active competitors and "
                            "a re-procurement forecast."
                        ),
                        "how": "Analytics section on the tender card.",
                        "tier": "Pro+",
                    },
                ],
            },
            {
                "id": "telegram",
                "title": "Telegram bot",
                "icon": "💬",
                "body": [
                    "Up to {cap} tenders a day per template are delivered to "
                    "Telegram and email; the website shows all tenders matching "
                    "your template. When the limit is reached, the bot tells you "
                    "and links to the feed on the website.",
                    "The @nabrs_bot sends matches for your template and offers "
                    "AI actions right on the tender card. Control it with "
                    "simple commands:",
                ],
                "commands": [
                    ("/start", "Sign up and set up your search template"),
                    ("/help", "List of bot commands and features"),
                    ("/profile", "Show current template settings"),
                    ("/reset", "Reconfigure your template from scratch"),
                    ("/watching", "Winner-tracking subscriptions"),
                    ("/list", "Latest matching tenders"),
                    ("/status", "Subscription and plan status"),
                ],
                "cta": {
                    "href": "https://t.me/nabrs_bot",
                    "label": "Open the bot",
                },
            },
            {
                "id": "sources",
                "title": "Sources",
                "icon": "🌐",
                "body": [
                    "We track public procurement sources: {sources}. We send only current tenders that arrived after a source was switched on — the archive is not sent.",
                    "Sources are chosen per template: open a template in your dashboard and tick the sources in the “Sources” field. A new template follows Serbian sources by default; you switch on foreign ones yourself.",
                ],
                "sources_heading": "Connected sources:",
                "sources_more": "What each source provides and how often we check for new notices",
                "sources_list": True,
            },
            {
                "id": "plans",
                "title": "Pricing and payment",
                "icon": "💳",
                "body": [
                    "A new account gets {trial} of Pro for free. Pro and Business are "
                    "for active bidding: more templates, Telegram delivery and a "
                    "monthly AI analysis volume.",
                    "No annual contract, cancel anytime. Details and current "
                    "prices are on the pricing page.",
                ],
                "cta": {"href": "/plans", "label": "View pricing"},
            },
            {
                "id": "card",
                "title": "How to read a tender card",
                "icon": "🗂️",
                "body": [
                    "Every card condenses what matters for a decision. Here is "
                    "what each field means.",
                ],
                "blocks": [
                    {
                        "h": "Title and contracting authority",
                        "text": (
                            "The procurement subject and the institution issuing "
                            "it (naručilac). The authority hints at the type of "
                            "work and its procurement history."
                        ),
                    },
                    {
                        "h": "Submission deadline",
                        "text": (
                            "The date and time by which a bid is accepted. Once "
                            "it passes, applying is no longer possible — track it "
                            "early."
                        ),
                    },
                    {
                        "h": "Estimated value",
                        "text": (
                            "The indicative budget the authority estimated. It "
                            "helps you quickly judge whether the job fits your "
                            "size."
                        ),
                    },
                    {
                        "h": "CPV and subject",
                        "text": (
                            "The CPV code is the official procurement category; "
                            "the subject is a short description. They are how a "
                            "card matches your search template."
                        ),
                    },
                    {
                        "h": "Relevance (AI)",
                        "text": (
                            "On Pro plans the card carries a relevance score and "
                            "a short explanation of why the tender fits you."
                        ),
                    },
                    {
                        "h": "Documentation and actions",
                        "text": (
                            "Links to the tender documentation and buttons for "
                            "AI actions (deep analysis, verdict, checklist, "
                            "draft) sit on the card itself."
                        ),
                    },
                ],
            },
            {
                "id": "bidnobid",
                "title": "Bid / no-bid: is it worth it",
                "icon": "⚖️",
                "body": [
                    "“Bid or no-bid” is the decision whether to invest effort "
                    "and submit a bid — or skip the tender and save resources "
                    "for a better one.",
                ],
                "blocks": [
                    {
                        "h": "What it means",
                        "text": (
                            "Bid = you go for the tender and prepare an offer. "
                            "No-bid = you deliberately skip it because the odds "
                            "or payoff are not enough."
                        ),
                    },
                    {
                        "h": "When to go (for)",
                        "text": (
                            "The subject is in your field, you meet the "
                            "conditions, the deadline is realistic, and the "
                            "estimated value justifies the effort."
                        ),
                    },
                    {
                        "h": "When to skip (against)",
                        "text": (
                            "Too short a deadline, conditions you cannot meet, "
                            "an unclear specification, or a margin too thin to "
                            "cover the cost of preparing a bid."
                        ),
                    },
                    {
                        "h": "How AI helps",
                        "text": (
                            "On Pro plans the “Is it worth it” button gives a "
                            "verdict (go / consider / skip) with arguments for "
                            "and against — the final call is yours."
                        ),
                    },
                ],
            },
            {
                "id": "trial",
                "title": "{trial_n}-day trial",
                "icon": "✨",
                "body": [
                    "Every new account gets {trial} of Pro for free — with notifications and a demo AI analysis (10 tenders for the whole trial). After that, delivery stops and the cabinet stays open; you can request an extension of up to 30 days from the cabinet (the operator decides), or send a subscription request (Pro or Business). The full price list is on the pricing page.",
                ],
                "cta": {"href": "/plans", "label": "Compare plans"},
            },
            {
                "id": "glossary",
                "title": "Glossary",
                "icon": "📖",
                "body": [
                    "A short glossary of Serbian public procurement terms you "
                    "meet in cards and documentation.",
                ],
                "glossary": [
                    {
                        "term": "Naručilac (contracting authority)",
                        "desc": (
                            "The institution or company that issues the "
                            "procurement and evaluates bids."
                        ),
                    },
                    {
                        "term": "Ponuđač (bidder)",
                        "desc": (
                            "The company that submits a bid for a tender — in "
                            "most cases that is you."
                        ),
                    },
                    {
                        "term": "Portal javnih nabavki",
                        "desc": (
                            "The official state portal (jnportal.src-ujn.example.invalid) "
                            "where procurements are published; one of the "
                            "sources the system tracks."
                        ),
                    },
                    {
                        "term": "Konkursna dokumentacija (tender documentation)",
                        "desc": (
                            "The authority's set of documents with conditions, "
                            "the specification and forms for submitting a bid."
                        ),
                    },
                    {
                        "term": "CPV",
                        "desc": (
                            "Common Procurement Vocabulary — the official "
                            "classification of the subject by codes, used for "
                            "filtering."
                        ),
                    },
                    {
                        "term": "Okvirni sporazum (framework agreement)",
                        "desc": (
                            "An agreement with one or more bidders that sets the "
                            "terms for future procurements over a given period."
                        ),
                    },
                    {
                        "term": "Otvoreni postupak (open procedure)",
                        "desc": (
                            "A procedure in which any interested party may "
                            "submit a bid within the set deadline."
                        ),
                    },
                    {
                        "term": "Procurement and contract award notices",
                        "desc": (
                            "Standard notice forms: the contract/procurement "
                            "notice and the contract award (outcome) notice."
                        ),
                    },
                    {
                        "term": "Procenjena vrednost (estimated value)",
                        "desc": (
                            "The budget the authority estimated for the "
                            "procurement, without a binding final price."
                        ),
                    },
                    {
                        "term": "Rok za podnošenje (submission deadline)",
                        "desc": (
                            "The final date and time the authority accepts bids; "
                            "after it, submitting is impossible."
                        ),
                    },
                ],
            },
            {
                "id": "faq",
                "title": "FAQ",
                "icon": "❓",
                "faq_items": [
                    {
                        "q": "How often are tenders searched?",
                        "a": "The system checks public procurement sources ({sources}) {interval}. {interval_note} The portal publishes new tenders in one nightly batch, so most new ones appear overnight.",
                    },
                    {
                        "q": "Can I have more than one search template?",
                        "a": (
                            "Yes, the number of templates depends on your "
                            "plan — see the pricing page for details."
                        ),
                    },
                    {
                        "q": "How do keywords and stop words work?",
                        "a": (
                            "Each line is an AND-group (all comma-separated "
                            "terms must match), while lines act as OR between "
                            "each other. Stop words exclude tenders containing "
                            "them."
                        ),
                    },
                    {
                        "q": "What are CPV codes?",
                        "a": (
                            "The official procurement classification. "
                            "Filtering by codes works independently of "
                            "keywords — you can combine both."
                        ),
                    },
                    {
                        "q": "How do I connect Telegram?",
                        "a": (
                            "On your dashboard, the “Connect "
                            "Telegram” button walks you through linking "
                            "your account."
                        ),
                    },
                    {
                        "q": "Do I need a paid plan to start?",
                        "a": (
                            "No. Every new account gets {trial} of Pro for free — "
                            "with delivery and AI features. After the trial, "
                            "delivery and AI work on Pro and higher."
                        ),
                    },
                    {
                        "q": "Didn't find your answer here?",
                        "a": "Send the bot the /help command or contact us.",
                        "link_phrase": "contact us",
                    },
                ],
            },
        ],
    },
}


# --------------------------------------------------------------------------- #
#  /izvori — the showcase of the sources (owner’s decision 02.10.2026). The labels of the platforms' capabilities
#  (cap_*) are shared with the profile form, in :mod:`src.i18n_common`; here
#  only the page chrome. There are no figures in the texts — they come from the ``sources`` table.
# --------------------------------------------------------------------------- #
SOURCES: dict[str, dict[str, str]] = {
    "sr": {
        "title": "Izvori tendera",
        "subtitle": "Odakle dolaze tenderi i šta dobijate iz svakog izvora.",
        "countries": "Zemlje",
        "site": "Sajt izvora",
        "since": "Tenderi iz ovog izvora od",
        "interval": "Nove objave proveravamo",
        "every_min": "svakih {n} min",
        "every_h": "svakih {n} h",
        "caps_heading": "Šta sadrži kartica tendera",
        "caps_unknown": "Za ovaj izvor još nismo opisali sadržaj kartice.",
        "note": "Šaljemo samo tendere koji su stigli posle uključenja izvora — arhiv se ne šalje. Izvore za svaki šablon birate u kabinetu.",
        "empty": "Trenutno nijedan izvor nije uključen.",
        "tenders_of_source": "Tenderi ovog izvora →",
        "yes": "ima",
        "no": "nema",
    },
    "ru": {
        "title": "Источники тендеров",
        "subtitle": "Откуда приходят тендеры и что вы получаете по каждому источнику.",
        "countries": "Страны",
        "site": "Сайт площадки",
        "since": "Тендеры с этой площадки — с",
        "interval": "Новые объявления проверяем",
        "every_min": "каждые {n} мин",
        "every_h": "каждые {n} ч",
        "caps_heading": "Что есть в карточке тендера",
        "caps_unknown": "Состав карточки для этого источника ещё не описан.",
        "note": "Присылаем только тендеры, пришедшие после подключения источника, — архив не рассылается. Источники для каждого шаблона выбираются в кабинете.",
        "empty": "Сейчас ни один источник не подключён.",
        "tenders_of_source": "Тендеры этой площадки →",
        "yes": "есть",
        "no": "нет",
    },
    "en": {
        "title": "Tender sources",
        "subtitle": "Where tenders come from and what you get from each source.",
        "countries": "Countries",
        "site": "Source website",
        "since": "Tenders from this source since",
        "interval": "New notices checked",
        "every_min": "every {n} min",
        "every_h": "every {n} h",
        "caps_heading": "What the tender card contains",
        "caps_unknown": "The card contents for this source are not described yet.",
        "note": "We only send tenders that arrived after the source was connected — the archive is not sent. You choose sources for each template in your account.",
        "empty": "No source is connected right now.",
        "tenders_of_source": "Tenders from this source →",
        "yes": "yes",
        "no": "no",
    },
}


# --------------------------------------------------------------------------- #
#  /statistika — public statistics (R3 ANALYTICS_PRODUCT, owner 10.10.2026).
#  Chrome only: the figures and dates are given by :mod:`src.api.public.stats_page`.
#  ``{start}`` / ``{as_of}`` — the dates of the period, ``{n}`` / ``{m}`` — counters,
#  ``{trial}`` — the length of the trial period (fill_trial).
# --------------------------------------------------------------------------- #
NOVOSTI: dict[str, dict[str, str]] = {
    "sr": {
        "title": "Novosti proizvoda",
        "subtitle": "Šta je novo u TenderSRB-u.",
        "back": "Sve novosti",
        "published": "Objavljeno",
    },
    "ru": {
        "title": "Новости продукта",
        "subtitle": "Что нового в TenderSRB.",
        "back": "Все новости",
        "published": "Опубликовано",
    },
    "en": {
        "title": "Product news",
        "subtitle": "What is new in TenderSRB.",
        "back": "All news",
        "published": "Published",
    },
}

STATS: dict[str, dict[str, str]] = {
    "sr": {
        "title": "Statistika javnih nabavki u Srbiji",
        "subtitle": "Koliko tendera se objavljuje, u kojim oblastima i ko najviše ugovara — zbirni podaci bez podataka o ličnostima.",
        "period": "Podaci od {start} · stanje na {as_of}",
        "total": "Tendera u periodu: {n}",
        "months_heading": "Tenderi po mesecima",
        "months_note": "Po datumu objave. Tekući mesec još nije završen.",
        "col_month": "Mesec",
        "col_tenders": "Tendera",
        "cpv_heading": "Top 10 oblasti (CPV odeljak)",
        "col_cpv": "Oblast",
        "proc_heading": "Vrste postupka",
        "col_proc": "Postupak",
        "proc_none": "Nije navedeno",
        "awards_heading": "Odluke o dodeli ugovora po mesecima",
        "awards_note": "Po datumu odluke, bez storniranih ugovora. Odluka je jedan ponuđač u jednom tenderu (sve njegove partije zajedno); samo tenderi objavljeni od početka perioda.",
        "awards_lag_note": "Odluke se objavljuju sa zakašnjenjem: podaci za poslednja dva meseca se još dopunjuju.",
        "partial": "tekući mesec, nepotpuno",
        "col_decisions": "Odluka",
        "col_sum": "Iznos (EUR)",
        "col_known": "Iznos poznat",
        "known_of": "iznos poznat za {n} od {m}",
        "awards_total": "Ukupno: {n} odluka, {sum}",
        "customers_heading": "Top 10 naručilaca po vrednosti ugovora",
        "customers_note": "Zbir poznatih iznosa odluka od početka perioda. Naručioci su javni organi; ponuđače ovde ne prikazujemo.",
        "col_customer": "Naručilac",
        "cta_title": "Sastavite sopstveni izveštaj i preuzmite Excel — {trial} besplatno",
        "cta_text": "Filtrirajte po oblasti, naručiocu i periodu u svom kabinetu.",
        "cta_button": "Registrujte se",
        "cta_cabinet": "Moj kabinet",
        "empty": "Statistika trenutno nije dostupna. Pokušajte kasnije.",
        "no_awards": "U ovom periodu još nema odluka o dodeli.",
    },
    "ru": {
        "title": "Статистика госзакупок Сербии",
        "subtitle": "Сколько тендеров публикуется, в каких отраслях и кто больше всех заключает контракты — сводные данные без персональных данных.",
        "period": "Данные с {start} · по состоянию на {as_of}",
        "total": "Тендеров за период: {n}",
        "months_heading": "Тендеры по месяцам",
        "months_note": "По дате публикации. Текущий месяц ещё не закончился.",
        "col_month": "Месяц",
        "col_tenders": "Тендеров",
        "cpv_heading": "Топ-10 отраслей (раздел CPV)",
        "col_cpv": "Отрасль",
        "proc_heading": "Виды процедур",
        "col_proc": "Процедура",
        "proc_none": "Не указано",
        "awards_heading": "Решения о присуждении контрактов по месяцам",
        "awards_note": "По дате решения, без сторнированных контрактов. Решение — это один победитель в одном тендере (все его лоты вместе); только тендеры, опубликованные с начала периода.",
        "awards_lag_note": "Решения публикуются с задержкой: данные за последние два месяца ещё дополняются.",
        "partial": "текущий месяц, неполный",
        "col_decisions": "Решений",
        "col_sum": "Сумма (EUR)",
        "col_known": "Сумма известна",
        "known_of": "сумма известна для {n} из {m}",
        "awards_total": "Всего: {n} решений, {sum}",
        "customers_heading": "Топ-10 заказчиков по объёму контрактов",
        "customers_note": "Сумма известных сумм решений с начала периода. Заказчики — государственные органы; победителей здесь не показываем.",
        "col_customer": "Заказчик",
        "cta_title": "Соберите свой отчёт и скачайте Excel — {trial} бесплатно",
        "cta_text": "Фильтры по отрасли, заказчику и периоду — в вашем кабинете.",
        "cta_button": "Зарегистрироваться",
        "cta_cabinet": "Мой кабинет",
        "empty": "Статистика сейчас недоступна. Попробуйте позже.",
        "no_awards": "За этот период решений о присуждении пока нет.",
    },
    "en": {
        "title": "Public procurement statistics for Serbia",
        "subtitle": "How many tenders are published, in which sectors and who contracts the most — aggregate figures, no personal data.",
        "period": "Data since {start} · as of {as_of}",
        "total": "Tenders in the period: {n}",
        "months_heading": "Tenders by month",
        "months_note": "By publication date. The current month is not over yet.",
        "col_month": "Month",
        "col_tenders": "Tenders",
        "cpv_heading": "Top 10 sectors (CPV division)",
        "col_cpv": "Sector",
        "proc_heading": "Procedure types",
        "col_proc": "Procedure",
        "proc_none": "Not specified",
        "awards_heading": "Contract award decisions by month",
        "awards_note": "By decision date, reversed contracts excluded. A decision is one winner in one tender (all their lots together); only tenders published since the start of the period.",
        "awards_lag_note": "Decisions are published with a delay: figures for the last two months are still being completed.",
        "partial": "current month, incomplete",
        "col_decisions": "Decisions",
        "col_sum": "Amount (EUR)",
        "col_known": "Amount known",
        "known_of": "amount known for {n} of {m}",
        "awards_total": "Total: {n} decisions, {sum}",
        "customers_heading": "Top 10 contracting authorities by contract value",
        "customers_note": "Sum of known decision amounts since the start of the period. Contracting authorities are public bodies; bidders are not shown here.",
        "col_customer": "Contracting authority",
        "cta_title": "Build your own report and download Excel — {trial} free",
        "cta_text": "Filter by sector, contracting authority and period in your account.",
        "cta_button": "Sign up",
        "cta_cabinet": "My cabinet",
        "empty": "Statistics are not available right now. Please try again later.",
        "no_awards": "No award decisions in this period yet.",
    },
}


# --------------------------------------------------------------------------- #
# The public tender card GET /tender/{id} (web-ux-6). An entry point / growth:
# someone shares a link → a clear page → registration. sr / ru / en parity.
# Only the chrome / copyright; the tender data (title / customer / deadline / CPV / description)
# come from tenders_normalized and are not translated.
# --------------------------------------------------------------------------- #

TENDER: dict[str, dict[str, str]] = {
    "sr": {
        "back": "Nazad na početnu",
        "meta_tail": "javna nabavka u Srbiji — rok, vrednost, CPV i AI-analiza.",
        "badge_new": "Novo",
        "badge_urgent": "Hitno",
        "badge_expired": "Zatvoreno",
        "customer": "Naručilac",
        "deadline": "Rok za podnošenje",
        "amount": "Procenjena vrednost",
        "status": "Status",
        "regions": "Region",
        "procedure": "Vrsta postupka",
        "cpv": "CPV klasifikacija",
        "plan_period": "Planski period",
        "amount_none": "Nije navedena",
        "amount_estimated": "procena",
        "deadline_none": "Nije naveden",
        "add_to_calendar": "📅 U kalendar",
        "awards_title": "Odluka o dodeli",
        "awards_winner": "Izabrani ponuđač",
        "awards_amount": "Vrednost ugovora",
        "awards_date": "Datum odluke",
        "awards_lot": "Lot",
        "awards_item": "Stavka",
        "status_unconfirmed": "Status nije potvrđen",
        "deadline_expired": "Rok je istekao",
        "deadline_today": "Ističe danas",
        "deadline_tomorrow": "Ističe sutra",
        "also_on": "Isti tender je i na još {n} platformi",
        "also_on_probable": "Verovatno isti tender je i na još {n} platformi",
        "source": "Otvori na izvoru",
        "source_fact": "Izvor",
        "source_open": "Otvori na: {name}",
        "source_hint": "Zvanična stranica nabavke ↗",
        "desc_title": "Opis i tehnička specifikacija",
        "desc_none": (
            "Detaljan opis nije preuzet. Otvorite izvor za pun tekst nabavke."
        ),
        "desc_more": "Prikaži ceo tekst",
        "desc_from_docs": "Iz dokumentacije",
        "desc_less": "Sakrij",
        "trust_title": "Odakle podaci i kako čitamo rokove",
        "trust_source": (
            "Podaci se preuzimaju sa zvaničnog izvora ({sources}) — "
            "bez ručnog unosa."
        ),
        "trust_urgency": "„Hitno“ znači da je do isteka roka ostalo 7 dana ili manje.",
        "trust_updated": (
            "Podaci se preuzimaju sa portala prilikom objave; izmene roka i "
            "dokumentacije pratite na izvornom portalu."
        ),
        "trust_how_link": "Kako radi",
        "ai_title": "AI-analiza za vaš šablon",
        "ai_relevance": "Relevantnost",
        "ai_pending": "Analiza za ovaj tender još nije spremna.",
        "ai_disclaimer": "AI može da greši — proverite sa dokumentacijom nabavke.",
        "cta_title": "Primajte ovakve tendere automatski — sa AI-analizom",
        "cta_subtitle": (
            "Podesite šablon pretrage jednom — odgovarajuće nabavke stižu u Telegram, "
            "a one važne možete analizirati uz AI."
        ),
        "cta_b1_t": "Vredi li se prijaviti?",
        "cta_b1_d": "AI-procena na osnovu tehničke specifikacije.",
        "cta_b2_t": "Šta pripremiti",
        "cta_b2_d": "Ček-lista dokumenata i kvalifikacija.",
        "cta_b3_t": "Nacrt ponude",
        "cta_b3_d": "Gotov nacrt koji samo doterate.",
        "cta_primary": "Počnite besplatno",
        "cta_cabinet": "Moj kabinet",
        "cta_secondary": "Cenovnik",
        "cta_trust": "{trial} Pro besplatno. Bez ugovora, otkažite bilo kada.",
        "not_found_title": "Tender nije pronađen",
        "not_found_text": "Tražena nabavka ne postoji ili je uklonjena.",
        "more_cpv": "Još tendera: {name}",
        "more_region": "Region: {name}",
    },
    "ru": {
        "back": "На главную",
        "meta_tail": "госзакупка в Сербии — дедлайн, сумма, CPV и AI-разбор.",
        "badge_new": "Новый",
        "badge_urgent": "Срочно",
        "badge_expired": "Закрыт",
        "customer": "Заказчик",
        "deadline": "Срок подачи",
        "amount": "Оценочная стоимость",
        "status": "Статус",
        "regions": "Регион",
        "procedure": "Тип процедуры",
        "cpv": "Классификация CPV",
        "plan_period": "Плановый период",
        "amount_none": "не указана",
        "amount_estimated": "оценка",
        "deadline_none": "не указан",
        "add_to_calendar": "📅 В календарь",
        "awards_title": "Решение о присуждении",
        "awards_winner": "Выбранный поставщик",
        "awards_amount": "Сумма договора",
        "awards_date": "Дата решения",
        "awards_lot": "Лот",
        "awards_item": "Позиция",
        "status_unconfirmed": "Статус не подтверждён",
        "deadline_expired": "Срок истёк",
        "deadline_today": "Истекает сегодня",
        "deadline_tomorrow": "Истекает завтра",
        "also_on": "также на {n} площадках",
        "also_on_probable": "вероятно, тот же тендер ещё на {n} площадках",
        "source": "Открыть на площадке",
        "source_fact": "Источник",
        "source_open": "Открыть на: {name}",
        "source_hint": "Официальная страница закупки ↗",
        "desc_title": "Описание и техзадание",
        "desc_none": (
            "Подробное описание не загружено. Откройте источник, "
            "чтобы прочитать полный текст закупки."
        ),
        "desc_more": "Показать полностью",
        "desc_from_docs": "Из документации",
        "desc_less": "Свернуть",
        "trust_title": "Откуда данные и как мы читаем сроки",
        "trust_source": (
            "Данные загружаются с официального источника ({sources}) — "
            "без ручного ввода."
        ),
        "trust_urgency": "«Срочно» означает, что до конца срока осталось 7 дней или меньше.",
        "trust_updated": (
            "Данные берутся с портала при публикации; изменения срока и "
            "документации смотрите на исходном портале."
        ),
        "trust_how_link": "Как это работает",
        "ai_title": "AI-анализ под ваш шаблон",
        "ai_relevance": "Релевантность",
        "ai_pending": "Разбор для этого тендера ещё не готов.",
        "ai_disclaimer": "AI может ошибаться — сверяйте с документацией закупки.",
        "cta_title": "Получайте такие тендеры автоматически — с AI-анализом",
        "cta_subtitle": (
            "Настройте шаблон поиска один раз — подходящие закупки приходят в Telegram, "
            "а нужные можно разобрать с AI."
        ),
        "cta_b1_t": "Стоит ли участвовать?",
        "cta_b1_d": "AI-вердикт по техзаданию.",
        "cta_b2_t": "Что подготовить",
        "cta_b2_d": "Чек-лист документов и квалификации.",
        "cta_b3_t": "Черновик заявки",
        "cta_b3_d": "Готовый черновик — останется доработать.",
        "cta_primary": "Начать бесплатно",
        "cta_cabinet": "Мой кабинет",
        "cta_secondary": "Тарифы",
        "cta_trust": "{trial} Pro бесплатно. Без договора, отмена в любой момент.",
        "not_found_title": "Тендер не найден",
        "not_found_text": "Запрошенный тендер не существует или был удалён.",
        "more_cpv": "Ещё тендеры: {name}",
        "more_region": "Регион: {name}",
    },
    "en": {
        "back": "Back to home",
        "meta_tail": "public tender in Serbia — deadline, value, CPV and AI review.",
        "badge_new": "New",
        "badge_urgent": "Urgent",
        "badge_expired": "Closed",
        "customer": "Contracting authority",
        "deadline": "Submission deadline",
        "amount": "Estimated value",
        "status": "Status",
        "regions": "Region",
        "procedure": "Procedure type",
        "cpv": "CPV classification",
        "plan_period": "Planned period",
        "amount_none": "not specified",
        "amount_estimated": "estimate",
        "deadline_none": "not specified",
        "add_to_calendar": "📅 Add to calendar",
        "awards_title": "Award decision",
        "awards_winner": "Selected supplier",
        "awards_amount": "Contract value",
        "awards_date": "Decision date",
        "awards_lot": "Lot",
        "awards_item": "Item",
        "status_unconfirmed": "Status not confirmed",
        "deadline_expired": "Deadline passed",
        "deadline_today": "Closes today",
        "deadline_tomorrow": "Closes tomorrow",
        "also_on": "also on {n} more platforms",
        "also_on_probable": "probably the same tender on {n} more platforms",
        "source": "Open on source",
        "source_fact": "Source",
        "source_open": "Open on: {name}",
        "source_hint": "Official procurement page ↗",
        "desc_title": "Description and specification",
        "desc_none": (
            "A detailed description was not fetched. Open the source "
            "for the full procurement text."
        ),
        "desc_more": "Show full text",
        "desc_from_docs": "From the documentation",
        "desc_less": "Collapse",
        "trust_title": "Where the data comes from and how we read deadlines",
        "trust_source": (
            "Data is pulled from the official source ({sources}) — " "no manual entry."
        ),
        "trust_urgency": "“Urgent” means 7 days or fewer remain until the deadline.",
        "trust_updated": (
            "Data is taken from the portal at publication; check the source "
            "portal for changes to the deadline and documents."
        ),
        "trust_how_link": "How it works",
        "ai_title": "AI review for your template",
        "ai_relevance": "Relevance",
        "ai_pending": "The review for this tender is not ready yet.",
        "ai_disclaimer": "AI can make mistakes — check against the procurement documentation.",
        "cta_title": "Get tenders like this automatically — with AI review",
        "cta_subtitle": (
            "Set up your search template once: matching tenders arrive in Telegram, "
            "with an AI review of the ones that matter."
        ),
        "cta_b1_t": "Worth bidding?",
        "cta_b1_d": "AI verdict based on the specification.",
        "cta_b2_t": "What to prepare",
        "cta_b2_d": "Checklist of documents and qualifications.",
        "cta_b3_t": "Bid draft",
        "cta_b3_d": "A ready draft you just refine.",
        "cta_primary": "Start for free",
        "cta_cabinet": "My cabinet",
        "cta_secondary": "Pricing",
        "cta_trust": "{trial} of Pro free. No contract, cancel anytime.",
        "not_found_title": "Tender not found",
        "not_found_text": "The requested tender does not exist or was removed.",
        "more_cpv": "More tenders: {name}",
        "more_region": "Region: {name}",
    },
}


# GEO-2: landing pages /tenderi (the hub + the CPV / region sections). The texts of the
# chrome / empty states / pagination — its own catalog; the shared labels of the tender row
# (amount_none / deadline_none / badge_*) are reused from TENDER, so as not to
# a second copy in two catalogs.
GEO: dict[str, dict[str, str]] = {
    "sr": {
        "hub_title": "Tenderi po kategoriji i regionu",
        "hub_meta": "Sve javne nabavke u Srbiji razvrstane po CPV kategoriji i regionu.",
        "hub_intro": "Izaberite CPV kategoriju ili region da vidite otvorene javne nabavke.",
        "hub_sources_link": "Tenderi iz izvora: {n} →",
        "hub_cpv_heading": "Po CPV kategoriji",
        "hub_region_heading": "Po regionu",
        "hub_empty": "Za sada nema kategorija sa dovoljno tendera.",
        "breadcrumb_home": "Početna",
        "breadcrumb_hub": "Tenderi",
        "section_cpv_intro": "Otvorene javne nabavke iz CPV kategorije {code} — ažurira se {interval}.",
        "section_region_intro": "Otvorene javne nabavke u regionu {name} — ažurira se {interval}.",
        "cpv_code_label": "CPV kod",
        "cpv_dir_title": "CPV šifarnik — javne nabavke po šifri",
        "cpv_dir_meta": "Pretraga svih CPV šifara javnih nabavki po broju i reči, sa brojem tendera i vezom ka listi tendera.",
        "cpv_dir_intro": "Zajednički rečnik za javne nabavke (CPV): odeljci i grupe, pretraga po broju ili reči i broj naših tendera po šifri.",
        "cpv_dir_search_label": "Pretraga CPV šifre",
        "cpv_dir_search_ph": "Broj ili reč, npr. 45233 ili asfalt",
        "cpv_dir_tree_heading": "Odeljci i grupe",
        "cpv_dir_results_heading": "Rezultati pretrage",
        "cpv_dir_no_results": "Nema šifre za ovaj upit.",
        "cpv_dir_loading": "Učitavanje šifarnika…",
        "cpv_dir_error": "Šifarnik nije učitan. Osvežite stranicu.",
        "cpv_dir_more": "Prikazano {shown} od {total}. Suzite upit.",
        "cpv_dir_tenders_n": "tendera: {n}",
        "cpv_dir_en_mark": "EN",
        "cpv_dir_en_title": "Naziv nije preveden — prikazan je na engleskom",
        "cpv_dir_parent": "Nadređena šifra",
        "cpv_dir_list_link": "Lista tendera",
        "cpv_dir_groups_in": "Grupe u odeljku",
        "section_group_intro": "Otvorene javne nabavke iz CPV grupe {code} — ažurira se {interval}.",
        "groups_heading": "CPV grupe u ovoj kategoriji",
        "crumb_cpv": "CPV šifre",
        "stat_total": "Ukupno (istorija)",
        "stat_open": "Trenutno otvoreno",
        "stat_sum": "Vrednost otvorenih (EUR)",
        "open_heading": "Otvoreni tenderi",
        "closed_heading": "Poslednji zatvoreni",
        "empty_open": "Trenutno nema otvorenih tendera u ovoj kategoriji.",
        "empty_closed": "Nema zatvorenih tendera za prikaz.",
        "pager_prev": "Prethodna",
        "pager_first": "Na početak",
        "pager_last": "Na kraj",
        "pager_goto": "Idi na stranu",
        "pager_go": "Idi",
        "pager_next": "Sledeća",
        "pager_page": "Strana",
        "prev_link": "◀ {name}",
        "next_link": "{name} ▶",
        "back_to_hub": "← Svi tenderi",
        "not_found_title": "Kategorija ne postoji",
        "not_found_text": "Ova stranica ne postoji ili nema dovoljno tendera za prikaz.",
        "search_label": "Pretraga tendera",
        "search_placeholder": "Pretraga: predmet, naručilac…",
        "search_submit": "Traži",
        "search_clear": "Očisti pretragu",
        "filters_summary": "Filteri",
        "sort_label": "Sortiranje",
        "sort_deadline": "Po roku (najbliži prvi)",
        "sort_published_desc": "Po datumu objave (najnoviji)",
        "sort_amount_desc": "Po iznosu (najveći prvi)",
        "sort_amount_asc": "Po iznosu (najmanji prvi)",
        "f_customer": "Naručilac ili PIB",
        "f_region": "Region",
        "f_region_any": "Svi regioni",
        "f_source": "Izvor",
        "f_source_any": "Svi izvori",
        "f_min_amount": "Iznos od (EUR)",
        "f_max_amount": "Iznos do (EUR)",
        "f_no_amount": "Uključujući tendere bez iznosa",
        "f_published_from": "Objavljeno od",
        "f_published_to": "Objavljeno do",
        "f_actual": "Rok",
        "f_actual_yes": "Samo otvoreni",
        "f_actual_all": "Svi, i zatvoreni",
        "apply": "Primeni",
        "reset_all": "Poništi sve",
        "found_n": "Pronađeno: {n}",
        "chips_label": "Aktivni filteri",
        "chip_remove": "Ukloni filter: {name}",
        "chip_q": "Pretraga: {v}",
        "chip_customer": "Naručilac: {v}",
        "chip_region": "Region: {v}",
        "chip_source": "Izvor: {v}",
        "chip_min_amount": "Od {v} EUR",
        "chip_max_amount": "Do {v} EUR",
        "chip_no_amount": "Uključeni tenderi bez iznosa",
        "chip_published_from": "Objavljeno od {v}",
        "chip_published_to": "Objavljeno do {v}",
        "chip_all_dates": "Uključujući zatvorene",
        "chip_sort": "Sortiranje: {v}",
        "results_heading": "Rezultati pretrage",
        "empty_results": "Nijedan tender ne odgovara zadatim filterima.",
        "query_timeout": "Pretraga je predugo trajala. Precizirajte upit: dodajte reč, region ili raspon iznosa.",
    },
    "ru": {
        "hub_title": "Тендеры по категории и региону",
        "hub_meta": "Все госзакупки в Сербии по CPV-категории и региону.",
        "hub_intro": "Выберите CPV-категорию или регион, чтобы увидеть открытые тендеры.",
        "hub_sources_link": "Тендеры из источников: {n} →",
        "hub_cpv_heading": "По CPV-категории",
        "hub_region_heading": "По региону",
        "hub_empty": "Пока нет категорий с достаточным числом тендеров.",
        "breadcrumb_home": "Главная",
        "breadcrumb_hub": "Тендеры",
        "section_cpv_intro": "Открытые тендеры из CPV-категории {code} — обновляется {interval}.",
        "section_region_intro": "Открытые тендеры в регионе {name} — обновляется {interval}.",
        "cpv_code_label": "Код CPV",
        "cpv_dir_title": "Справочник CPV — тендеры по коду",
        "cpv_dir_meta": "Поиск по всем кодам CPV госзакупок по номеру и словам: число тендеров и ссылка на список тендеров.",
        "cpv_dir_intro": "Единый словарь госзакупок (CPV): разделы и группы, поиск по номеру или слову и число наших тендеров по коду.",
        "cpv_dir_search_label": "Поиск кода CPV",
        "cpv_dir_search_ph": "Номер или слово, например 45233 или асфальт",
        "cpv_dir_tree_heading": "Разделы и группы",
        "cpv_dir_results_heading": "Результаты поиска",
        "cpv_dir_no_results": "По этому запросу кодов нет.",
        "cpv_dir_loading": "Загрузка справочника…",
        "cpv_dir_error": "Справочник не загрузился. Обновите страницу.",
        "cpv_dir_more": "Показано {shown} из {total}. Уточните запрос.",
        "cpv_dir_tenders_n": "тендеров: {n}",
        "cpv_dir_en_mark": "EN",
        "cpv_dir_en_title": "Названия на этом языке нет — показано английское",
        "cpv_dir_parent": "Родительский код",
        "cpv_dir_list_link": "Список тендеров",
        "cpv_dir_groups_in": "Группы в разделе",
        "section_group_intro": "Открытые тендеры из CPV-группы {code} — обновляется {interval}.",
        "groups_heading": "CPV-группы в этой категории",
        "crumb_cpv": "Коды CPV",
        "stat_total": "Всего (за всё время)",
        "stat_open": "Открыто сейчас",
        "stat_sum": "Сумма открытых (EUR)",
        "open_heading": "Открытые тендеры",
        "closed_heading": "Последние закрытые",
        "empty_open": "Сейчас нет открытых тендеров в этой категории.",
        "empty_closed": "Нет закрытых тендеров для показа.",
        "pager_prev": "Назад",
        "pager_first": "В начало",
        "pager_last": "В конец",
        "pager_goto": "Перейти на страницу",
        "pager_go": "Перейти",
        "pager_next": "Вперёд",
        "pager_page": "Страница",
        "prev_link": "◀ {name}",
        "next_link": "{name} ▶",
        "back_to_hub": "← Все тендеры",
        "not_found_title": "Категория не найдена",
        "not_found_text": "Такой страницы нет или в ней недостаточно тендеров.",
        "search_label": "Поиск тендеров",
        "search_placeholder": "Поиск: предмет, заказчик…",
        "search_submit": "Найти",
        "search_clear": "Очистить поиск",
        "filters_summary": "Фильтры",
        "sort_label": "Сортировка",
        "sort_deadline": "По сроку (ближайшие первыми)",
        "sort_published_desc": "По дате публикации (новые)",
        "sort_amount_desc": "По сумме (крупные первыми)",
        "sort_amount_asc": "По сумме (мелкие первыми)",
        "f_customer": "Заказчик или ПИБ",
        "f_region": "Регион",
        "f_region_any": "Все регионы",
        "f_source": "Источник",
        "f_source_any": "Все источники",
        "f_min_amount": "Сумма от (EUR)",
        "f_max_amount": "Сумма до (EUR)",
        "f_no_amount": "Включая тендеры без суммы",
        "f_published_from": "Опубликовано с",
        "f_published_to": "Опубликовано по",
        "f_actual": "Срок подачи",
        "f_actual_yes": "Только открытые",
        "f_actual_all": "Все, включая закрытые",
        "apply": "Применить",
        "reset_all": "Сбросить всё",
        "found_n": "Найдено: {n}",
        "chips_label": "Активные фильтры",
        "chip_remove": "Убрать фильтр: {name}",
        "chip_q": "Поиск: {v}",
        "chip_customer": "Заказчик: {v}",
        "chip_region": "Регион: {v}",
        "chip_source": "Источник: {v}",
        "chip_min_amount": "От {v} EUR",
        "chip_max_amount": "До {v} EUR",
        "chip_no_amount": "Включая без суммы",
        "chip_published_from": "Опубликовано с {v}",
        "chip_published_to": "Опубликовано по {v}",
        "chip_all_dates": "Включая закрытые",
        "chip_sort": "Сортировка: {v}",
        "results_heading": "Результаты поиска",
        "empty_results": "Ни один тендер не подходит под заданные фильтры.",
        "query_timeout": "Поиск занял слишком много времени. Уточните запрос: добавьте слово, регион или диапазон суммы.",
    },
    "en": {
        "hub_title": "Tenders by category and region",
        "hub_meta": "All public tenders in Serbia grouped by CPV category and region.",
        "hub_intro": "Pick a CPV category or region to see open tenders.",
        "hub_sources_link": "Tender sources: {n} →",
        "hub_cpv_heading": "By CPV category",
        "hub_region_heading": "By region",
        "hub_empty": "No categories with enough tenders yet.",
        "breadcrumb_home": "Home",
        "breadcrumb_hub": "Tenders",
        "section_cpv_intro": "Open tenders from CPV category {code} — updated {interval}.",
        "section_region_intro": "Open tenders in {name} — updated {interval}.",
        "cpv_code_label": "CPV code",
        "cpv_dir_title": "CPV code directory — tenders by code",
        "cpv_dir_meta": "Search every public-procurement CPV code by number or word, with tender counts and a link to the tender list.",
        "cpv_dir_intro": "The common procurement vocabulary (CPV): divisions and groups, search by number or word, and how many of our tenders use each code.",
        "cpv_dir_search_label": "Search CPV codes",
        "cpv_dir_search_ph": "Number or word, e.g. 45233 or asphalt",
        "cpv_dir_tree_heading": "Divisions and groups",
        "cpv_dir_results_heading": "Search results",
        "cpv_dir_no_results": "No codes match this query.",
        "cpv_dir_loading": "Loading the directory…",
        "cpv_dir_error": "The directory failed to load. Reload the page.",
        "cpv_dir_more": "Showing {shown} of {total}. Narrow the query.",
        "cpv_dir_tenders_n": "tenders: {n}",
        "cpv_dir_en_mark": "EN",
        "cpv_dir_en_title": "No translation for this language — shown in English",
        "cpv_dir_parent": "Parent code",
        "cpv_dir_list_link": "Tender list",
        "cpv_dir_groups_in": "Groups in division",
        "section_group_intro": "Open public procurement from CPV group {code} — updated {interval}.",
        "groups_heading": "CPV groups in this category",
        "crumb_cpv": "CPV codes",
        "stat_total": "Total (all time)",
        "stat_open": "Currently open",
        "stat_sum": "Value of open tenders (EUR)",
        "open_heading": "Open tenders",
        "closed_heading": "Recently closed",
        "empty_open": "No open tenders in this category right now.",
        "empty_closed": "No closed tenders to show.",
        "pager_prev": "Previous",
        "pager_first": "First page",
        "pager_last": "Last page",
        "pager_goto": "Go to page",
        "pager_go": "Go",
        "pager_next": "Next",
        "pager_page": "Page",
        "prev_link": "◀ {name}",
        "next_link": "{name} ▶",
        "back_to_hub": "← All tenders",
        "not_found_title": "Category not found",
        "not_found_text": "This page does not exist or does not have enough tenders yet.",
        "search_label": "Search tenders",
        "search_placeholder": "Search: subject, contracting authority…",
        "search_submit": "Search",
        "search_clear": "Clear search",
        "filters_summary": "Filters",
        "sort_label": "Sort by",
        "sort_deadline": "Deadline (soonest first)",
        "sort_published_desc": "Publication date (newest)",
        "sort_amount_desc": "Amount (highest first)",
        "sort_amount_asc": "Amount (lowest first)",
        "f_customer": "Contracting authority or tax ID",
        "f_region": "Region",
        "f_region_any": "All regions",
        "f_source": "Source",
        "f_source_any": "All sources",
        "f_min_amount": "Amount from (EUR)",
        "f_max_amount": "Amount to (EUR)",
        "f_no_amount": "Include tenders without an amount",
        "f_published_from": "Published from",
        "f_published_to": "Published to",
        "f_actual": "Deadline",
        "f_actual_yes": "Open only",
        "f_actual_all": "All, including closed",
        "apply": "Apply",
        "reset_all": "Reset all",
        "found_n": "Found: {n}",
        "chips_label": "Active filters",
        "chip_remove": "Remove filter: {name}",
        "chip_q": "Search: {v}",
        "chip_customer": "Authority: {v}",
        "chip_region": "Region: {v}",
        "chip_source": "Source: {v}",
        "chip_min_amount": "From {v} EUR",
        "chip_max_amount": "Up to {v} EUR",
        "chip_no_amount": "Including no amount",
        "chip_published_from": "Published from {v}",
        "chip_published_to": "Published to {v}",
        "chip_all_dates": "Including closed",
        "chip_sort": "Sort: {v}",
        "results_heading": "Search results",
        "empty_results": "No tenders match the selected filters.",
        "query_timeout": "The search took too long. Please refine your query: add a keyword, region or amount range.",
    },
}


# Machine values of tenders_normalized.status / procedure_type (eNabavke) →
# showcase labels. The dictionary is small — measured on production on 24.07 (RS-09 / active / open
# defect): status occurs only as "active", procedure_type — "open"
# (6400) and "restricted" (8). An unknown code — fail-open (as is); values
# are not made up at random (the same logic as okrug_label in src/domain/regions.py).
TENDER_STATUS: dict[str, dict[str, str]] = {
    "sr": {"active": "Aktivan"},
    "ru": {"active": "Активен"},
    "en": {"active": "Active"},
}

TENDER_PROCEDURE: dict[str, dict[str, str]] = {
    "sr": {"open": "Otvoreni postupak", "restricted": "Restriktivni postupak"},
    "ru": {"open": "Открытая процедура", "restricted": "Ограниченная процедура"},
    "en": {"open": "Open procedure", "restricted": "Restricted procedure"},
}


def status_label(value: str, lang: str) -> str:
    """Tender status code → label; an unknown code is given as is."""
    return TENDER_STATUS.get(lang, TENDER_STATUS[DEFAULT]).get(value, value)


def procedure_label(value: str, lang: str) -> str:
    """Procedure type code → label; an unknown code is given as is.

    Its own showcase labels (open / restricted); the other codes come from the common vocabulary ``pt_<code>`` (the same as in the email and Telegram), otherwise pin / qualification would come out as a raw machine value.
    """
    own = TENDER_PROCEDURE.get(lang, TENDER_PROCEDURE[DEFAULT]).get(value)
    if own is not None:
        return own
    return COMMON.get(lang, COMMON[DEFAULT]).get(f"pt_{value}", value)


# --------------------------------------------------------------------------- #
# Replies of the signup / login form. They live HERE (the public contour), not in the
# email catalog: this is text on a page, not an email. A Serbian client sees them
# FIRST — before 25.07 they were hard-coded in Russian right in auth_service.
# --------------------------------------------------------------------------- #
AUTH_MSG: dict[str, dict[str, str]] = {
    "sr": {
        "invalid_email": "Unesite ispravnu e-mail adresu.",
        "turnstile_failed": "Provera „nisam robot“ nije prošla. Pokušajte ponovo.",
        "email_rejected": (
            "Ova e-mail adresa se ne može koristiti "
            "(privremena ili nedostupna adresa)."
        ),
        "trial_used": (
            "Probni pristup za ovu e-mail adresu već je aktiviran "
            "u poslednjih 12 meseci."
        ),
        "card_verification": (
            "Zahtev je prosleđen operateru, javićemo vam se e-poštom u roku od 1 radnog dana."
        ),
        "check_mail": "Poslali smo vam e-mail sa linkom za potvrdu. Proverite poštu.",
    },
    "ru": {
        "invalid_email": "Укажите корректный email.",
        "turnstile_failed": "Проверка «я не робот» не пройдена. Попробуйте снова.",
        "email_rejected": (
            "Этот email нельзя использовать (одноразовый или недоставляемый адрес)."
        ),
        "trial_used": (
            "Пробный период для этого email уже был активирован "
            "за последние 12 месяцев."
        ),
        "card_verification": (
            "Заявка передана оператору, мы свяжемся с вами по e-mail в течение 1 рабочего дня."
        ),
        "check_mail": (
            "Мы отправили письмо со ссылкой подтверждения. Проверьте почту."
        ),
    },
    "en": {
        "invalid_email": "Enter a valid e-mail address.",
        "turnstile_failed": "The «I'm not a robot» check failed. Please try again.",
        "email_rejected": (
            "This e-mail address cannot be used (disposable or undeliverable)."
        ),
        "trial_used": (
            "A trial for this e-mail was already activated in the last 12 months."
        ),
        "card_verification": (
            "Your request has been passed to an operator; we will contact you by e-mail within 1 business day."
        ),
        "check_mail": "We have sent you a confirmation link. Please check your inbox.",
    },
}


def auth_message(lang: str | None, key: str) -> str:
    """The message of the signup / login form in the client’s language.

    ‼️ The fallback is stated EXPLICITLY — **sr** (owner’s decision 30.09.2026), not through the module ``DEFAULT``: the calling contour (``auth_service.signup``) normalizes the locale via ``i18n_email.norm_locale`` (the default there is sr as well). Both defaults must match — otherwise the same None would give different languages on one path.
    """
    return AUTH_MSG.get(lang or "sr", AUTH_MSG["sr"])[key]


class PublicContext(TypedDict):
    """The context of a public page: the keys are known — otherwise ``ctx["lang"]`` has type ``object`` and fits neither as an index into the text dictionary nor as an argument of ``get_doc``."""

    lang: str
    ui: dict[str, str]
    lang_names: dict[str, str]
    current_path: str


# --- The “Contact us” form (/kontakt, a block in “Help”, the cabinet hub) ----------- #
CONTACT_MESSAGE_MAX = 2000
CONTACT: dict[str, dict[str, str]] = {
    "sr": {
        "title": "Kontaktirajte nas",
        "intro": "Opišite šta vam treba — odgovorićemo na vaš email.",
        "email": "Email",
        "name": "Ime ili kompanija (nije obavezno)",
        "message": "Poruka",
        "submit": "Pošalji poruku",
        "fallback": "Ili pišite direktno na",
        "err_email": "Unesite ispravnu email adresu.",
        "err_message": "Napišite poruku.",
        "err_too_long": "Poruka je predugačka (najviše 2000 znakova).",
        "err_rate": "Previše zahteva. Pokušajte ponovo kasnije ili pišite na email ispod.",
        "err_send": "Poruku trenutno nije moguće poslati. Pišite direktno na adresu ispod.",
        "err_origin": "Zahtev odbijen. Otvorite stranicu ponovo i pošaljite formu.",
        "thanks_title": "Hvala!",
        "thanks_text": "Odgovorićemo na",
        "back": "Nazad na cenovnik",
        "privacy_text": "Podatke iz forme obrađujemo u skladu sa",
        "privacy_link": "Politikom privatnosti.",
        "block_title": "Pišite nam",
        "tg_channel": "Telegram kanal: novosti i beta test",
        "back_help": "Nazad na pomoć",
        "back_cabinet": "Nazad u kabinet",
    },
    "ru": {
        "title": "Связаться с нами",
        "intro": "Опишите задачу — ответим на ваш email.",
        "email": "Email",
        "name": "Имя или компания (необязательно)",
        "message": "Сообщение",
        "submit": "Отправить сообщение",
        "fallback": "Или напишите напрямую на",
        "err_email": "Укажите корректный email.",
        "err_message": "Напишите сообщение.",
        "err_too_long": "Сообщение слишком длинное (не более 2000 символов).",
        "err_rate": "Слишком много запросов. Повторите позже или напишите на email ниже.",
        "err_send": "Сейчас не удалось отправить сообщение. Напишите напрямую по адресу ниже.",
        "err_origin": "Запрос отклонён. Откройте страницу заново и отправьте форму.",
        "thanks_title": "Спасибо!",
        "thanks_text": "Ответим на",
        "back": "Назад к тарифам",
        "privacy_text": "Данные из формы обрабатываем в соответствии с",
        "privacy_link": "Политикой конфиденциальности.",
        "block_title": "Напишите нам",
        "tg_channel": "Telegram-канал: новости и бета-тест",
        "back_help": "Назад к помощи",
        "back_cabinet": "Назад в кабинет",
    },
    "en": {
        "title": "Contact us",
        "intro": "Tell us what you need — we will reply to your email.",
        "email": "Email",
        "name": "Name or company (optional)",
        "message": "Message",
        "submit": "Send message",
        "fallback": "Or write to us directly at",
        "err_email": "Enter a valid email address.",
        "err_message": "Please write a message.",
        "err_too_long": "The message is too long (2000 characters max).",
        "err_rate": "Too many requests. Try again later or email us below.",
        "err_send": "We could not send your message right now. Please write to the address below.",
        "err_origin": "Request rejected. Reload the page and submit the form again.",
        "thanks_title": "Thank you!",
        "thanks_text": "We will reply to",
        "back": "Back to pricing",
        "privacy_text": "We process form data in line with our",
        "privacy_link": "Privacy Policy.",
        "block_title": "Write to us",
        "tg_channel": "Telegram channel: news and beta test",
        "back_help": "Back to help",
        "back_cabinet": "Back to account",
    },
}


def session_locale(request: Request) -> str | None:
    """``users.locale`` from the signed session cookie (does not touch the DB).

    ``None`` — anonymous, a broken / expired cookie, or an old session without the ``locale`` field. The parsing contract is the same as for ``templating._auth_context``.
    """
    # Lazy imports — as in templating: the showcase i18n does not pull in config / security
    # at an early import.
    from src.api import security
    from src.api.config import get_cabinet_settings

    settings = get_cabinet_settings()
    session = security.verify_session(
        request.cookies.get("tb_session"),
        settings.cabinet_secret_key,
        max_age=settings.session_max_age,
    )
    locale = session.get("locale") if session else None
    return locale if locale in SUPPORTED else None


def resolve_page_lang(request: Request) -> str:
    """The language of a showcase page: ?lang= → the logged-in user’s ``users.locale`` → cookie → sr.

    For a logged-in user the saved interface language takes priority over a random cookie (as in the cabinet, ``resolve_authed_lang``): otherwise “Plans” from the cabinet menu would open in sr with ``users.locale=ru``. Anonymous — the previous :func:`resolve_lang`.
    """
    if request.query_params.get("lang") in SUPPORTED:
        return resolve_lang(request)
    return session_locale(request) or resolve_lang(request)


def public_context(request: Request) -> PublicContext:
    """The base context of the public pages: language, chrome, switcher, path."""
    lang = resolve_page_lang(request)
    return {
        "lang": lang,
        "ui": UI[lang],
        "lang_names": LANG_NAMES,
        "current_path": request.url.path,
    }


def seo_canonical_and_hreflang(
    request: Request, base_url: str, page: int = 1
) -> tuple[str, list[tuple[str, str]]]:
    """Canonical + hreflang (sr / ru / en + x-default=sr) for a public page.

    From ``base_url`` (``CABINET_BASE_URL``), not ``request.url_for`` — the same reason as for the canonical link of the tender card (behind the uvicorn proxy it sees http). ``sr`` is the DEFAULT (see the module), so its URL and x-default go WITHOUT ``?lang=`` — this is how the client without a language choice really sees them.

    ``page`` (listing pagination): with ``page>1`` ``?page=N`` enters the canonical and ALL the hreflang links — page 2 is a different list, not a copy of the first. ``page=1`` — without the parameter. The filters and other queries do not get into the canonical.
    """
    base = base_url.rstrip("/")
    path = request.url.path
    plain = f"{base}{path}"
    current_lang = _lang_from_query(request)

    def _url(lang: str) -> str:
        params = [] if lang == DEFAULT else [f"lang={lang}"]
        if page > 1:
            params.append(f"page={page}")
        return f"{plain}?{'&'.join(params)}" if params else plain

    # Each language variant canonicalises ITSELF (the content is really different,
    # not a duplicate): Serbian — a “clean” URL without a query (this is its default form,
    # which the client sees without a language choice), ru / en — with their own
    # ?lang=. Otherwise Google would collapse the ru / en variants into the sr canonical and would not
    # index them separately.
    canonical = _url(current_lang)
    links = [(code, _url(code)) for code in SUPPORTED]
    links.append(("x-default", _url(DEFAULT)))
    return canonical, links


def _lang_from_query(request: Request) -> str:
    """The page language only by an explicit ``?lang=`` (no cookie / Accept-Language).

    For the canonical what matters is the URL variant ACTUALLY REQUESTED: the cookie / Accept-Language decide what the client sees without a choice, but a URL without a query is always canonicalised as sr (see the docstring above).
    """
    chosen = request.query_params.get("lang")
    return chosen if chosen in SUPPORTED else DEFAULT
