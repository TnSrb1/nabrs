"""Product news (owner 10.10.2026, ANALYTICS_PRODUCT §7): once — three languages.

Storage without a migration, as in ``legal_texts.py``: a list of records in the code. The source for the public ``/novosti`` (+ ``/novosti/<slug>``), the sitemap, the “Novo” marks in the cabinet menu and the banners on the dashboard. The record is written once for sr (LATIN) / en / ru.

The text rules: no promises of deadlines and no figures without a date. A new record goes
to the START of :data:`NEWS` (the order is from newest to oldest).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.plans.catalog import TG_CHANNEL_URL

#: Cookie “read”: the date of the freshest viewed news item (ISO). No DB.
SEEN_COOKIE = "news_seen"
SEEN_COOKIE_MAX_AGE = 365 * 24 * 3600


@dataclass(frozen=True)
class NewsItem:
    slug: str
    published: date
    title: dict[str, str]
    body: dict[str, tuple[str, ...]]  # paragraphs by language
    # A path in the product (“/statistika”) or an external https address (“https://t.me/…”).
    # An external one opens in a new tab (rel="noopener"), see is_external.
    link: str
    link_label: dict[str, str]

    @property
    def is_external(self) -> bool:
        return self.link.startswith("https://")


NEWS: tuple[NewsItem, ...] = (
    NewsItem(
        slug="bih-tenderi",
        published=date(2026, 10, 20),
        title={
            "sr": "Bosna i Hercegovina: novi izvor e-Nabavke",
            "en": "Bosnia and Herzegovina: new source e-Nabavke",
            "ru": "Босния и Герцеговина: новая площадка e-Nabavke",
        },
        body={
            "sr": (
                "Dodali smo nacionalni portal javnih nabavki Bosne i Hercegovine, e-Nabavke BiH (www.src-ejn.example.invalid). Od 19.10. u bazi je 1.987 otvorenih tendera iz BiH, a novi se dodaju pri svakom ciklu provere izvora.",
                "Da biste ih primali, dodajte izvor „e-Nabavke BiH“ u šablon. Izvor se ne dodaje u postojeće šablone sam: uključite ga u podešavanjima šablona ili jednim klikom na početnoj strani kabineta, gde nudimo izvore koji nedostaju u vašim šablonima.",
            ),
            "en": (
                "We added the national public procurement portal of Bosnia and Herzegovina, e-Nabavke BiH (www.src-ejn.example.invalid). As of 19.10., the database holds 1,987 open tenders from BiH, and new ones are added in every source check cycle.",
                "To receive them, add the “e-Nabavke BiH” source to your template. It is not added to existing templates by itself: turn it on in the template settings, or with one click on your cabinet home page, where we offer sources missing from your templates.",
            ),
            "ru": (
                "Мы подключили национальный портал госзакупок Боснии и Герцеговины: e-Nabavke BiH (www.src-ejn.example.invalid). На 19.10 в базе 1 987 открытых тендеров из Боснии и Герцеговины, новые добавляются в каждом цикле проверки площадки.",
                "Чтобы получать их, добавьте площадку «e-Nabavke BiH» в шаблон. В уже созданные шаблоны она сама не попадает: включите её в настройках шаблона или одним нажатием на главной странице кабинета, где мы предлагаем площадки, которых нет в ваших шаблонах.",
            ),
        },
        link="/app",
        link_label={
            "sr": "Otvori kabinet",
            "en": "Open your cabinet",
            "ru": "Открыть кабинет",
        },
    ),
    NewsItem(
        slug="ai-cita-sve-dokumente",
        published=date(2026, 10, 20),
        title={
            "sr": "AI čita svu dokumentaciju tendera",
            "en": "AI now reads all tender documents",
            "ru": "AI читает всю документацию тендера",
        },
        body={
            "sr": (
                "AI analiza sada čita i dokumente u formatima Word 97–2003 (.doc), OpenDocument (.odt), RTF i ZIP arhive, uz PDF, DOCX i XLSX, koje je čitala i ranije. Merenje od 19.10. pokazalo je da je na portalu jnportal kod 18% tendera tehnička specifikacija bila samo u takvim datotekama; sada je model vidi.",
                "Duplirani fajlovi u partijama (lotovima) više ne potiskuju jedinstvene dokumente. Skenirani dokumenti bez tekstualnog sloja su označeni; prepoznavanje teksta (OCR) se ne radi.",
            ),
            "en": (
                "AI analysis now reads documents in Word 97–2003 (.doc), OpenDocument (.odt), RTF and ZIP archives, in addition to the PDF, DOCX and XLSX files it already read. A check on 19.10. showed that on the jnportal portal, the technical specification for 18% of tenders was only in such files; the model now sees it.",
                "Duplicate files in lots no longer push out unique documents. Scanned documents without a text layer are marked; text recognition (OCR) is not performed.",
            ),
            "ru": (
                "Разбор AI теперь читает документы в форматах Word 97–2003 (.doc), OpenDocument (.odt), RTF и ZIP-архивы, помимо PDF, DOCX и XLSX, которые читались и раньше. Замер от 19.10 показал: на портале jnportal у 18% тендеров техническое задание было только в таких файлах, и теперь модель его видит.",
                "Дубли файлов в партиях (лотах) больше не вытесняют уникальные документы. Сканы без текстового слоя помечаются; распознавание текста (OCR) не выполняется.",
            ),
        },
        link="/app/feed",
        link_label={
            "sr": "Otvori poklapanja",
            "en": "Open my matches",
            "ru": "Открыть совпадения",
        },
    ),
    NewsItem(
        slug="otvoreni-kod",
        published=date(2026, 10, 20),
        title={
            "sr": "Otvoreni kod: snimak izvornog koda na GitHub-u",
            "en": "Open source: a snapshot of the source code on GitHub",
            "ru": "Открытый код: снимок исходного кода на GitHub",
        },
        body={
            "sr": (
                "Objavili smo snimak izvornog koda servisa TenderSRB na GitHub-u (github.com/TnSrb1/nabrs): veb sajt i kabinet, bot, radnici obrade i testovi.",
                "Konektori ka portalima javnih nabavki nisu deo objave. Ovo je snimak koda na dan objave.",
            ),
            "en": (
                "We published a snapshot of the TenderSRB source code on GitHub (github.com/TnSrb1/nabrs): the website and cabinet, the bot, the workers and the tests.",
                "The connectors to procurement portals are not part of the publication. This is a snapshot of the code as of the publication date.",
            ),
            "ru": (
                "Мы опубликовали снимок исходного кода TenderSRB на GitHub (github.com/TnSrb1/nabrs): сайт и кабинет, бота, воркеры и тесты.",
                "Коннекторы к порталам госзакупок в публикацию не входят. Это снимок кода на дату публикации.",
            ),
        },
        link="https://github.com/TnSrb1/nabrs",
        link_label={
            "sr": "Kod na GitHub-u",
            "en": "Code on GitHub",
            "ru": "Код на GitHub",
        },
    ),
    NewsItem(
        slug="kabinet-treci-kanal",
        published=date(2026, 10, 19),
        title={
            "sr": "Kabinet je treći kanal za tendere",
            "en": "The cabinet is a third channel for tenders",
            "ru": "Кабинет: третий канал получения тендеров",
        },
        body={
            "sr": (
                "Tenderi koji odgovaraju vašem šablonu, a nemaju povezan Telegram ni e-poštu, sada su vidljivi u kabinetu, na stranici „Moja poklapanja“. Kabinet je jedan od tri kanala za primanje tendera, uz Telegram i e-poštu.",
                "U kabinet stižu samo novi tenderi od trenutka ove promene, stariji se ne pojavljuju unazad. Ako kasnije povežete Telegram ili e-poštu, obaveštenja stižu o novim tenderima, a stari se ne šalju u paketu.",
            ),
            "en": (
                "Tenders that match your template but have no Telegram or email connected are now visible in your cabinet, on the “My matches” page. The cabinet is one of three ways to receive tenders, alongside Telegram and email.",
                "Only new tenders from the moment of this change appear in the cabinet, older ones do not show up retroactively. If you connect Telegram or email later, notifications come about new tenders, and older ones are not sent in a batch.",
            ),
            "ru": (
                "Тендеры, которые подходят вашему шаблону, но без подключённых Telegram и e-mail, теперь видны в кабинете, на странице «Мои совпадения». Кабинет входит в число трёх способов получать тендеры: наряду с Telegram и e-mail.",
                "В кабинет попадают только новые тендеры с момента этого изменения, старые задним числом не появляются. Если вы подключите Telegram или e-mail позже, уведомления придут о новых тендерах, а старые в пачке не отправляются.",
            ),
        },
        link="/app/feed",
        link_label={
            "sr": "Otvori poklapanja",
            "en": "Open my matches",
            "ru": "Открыть совпадения",
        },
    ),
    NewsItem(
        slug="rokovi-tacno",
        published=date(2026, 10, 19),
        title={
            "sr": "Tačni rokovi za narednih 7 dana",
            "en": "Exact deadlines for the next 7 days",
            "ru": "Точные сроки на ближайшие 7 дней",
        },
        body={
            "sr": (
                "Pločica „Ističe za 7 dana“ na početnoj strani kabineta i spisak rokova sada računaju sve tendere sa rokom u narednih 7 dana, bez ograničenja na 50 stavki. Ranije se spisak sekao, pa su neki rokovi mogli da izađu iz pregleda.",
                "Dani se računaju po kalendaru u Beogradu, a ne kao 24-časovni periodi od sada. Tender kojem rok ističe sutra ujutro tako uvek ulazi u prvih sedam dana, a rok koji je već prošao ne ulazi.",
            ),
            "en": (
                "The “Expiring in 7 days” tile on your cabinet home page and the deadlines list now count every tender due within the next 7 days, with no cap at 50 items. Before, the list was cut, so some deadlines could drop out of view.",
                "Days are counted on the Belgrade calendar, not as 24-hour periods from now. A tender due tomorrow morning always falls within the first seven days, and a deadline that has already passed does not.",
            ),
            "ru": (
                "Плитка «Истекает за 7 дней» на главной странице кабинета и список сроков теперь считают все тендеры со сроком в ближайшие 7 дней, без ограничения в 50 записей. Раньше список обрезался, и некоторые сроки могли выпасть из вида.",
                "Дни считаются по календарю Белграда, а не как 24-часовые отрезки от текущего момента. Тендер, у которого срок истекает завтра утром, всегда попадает в первые семь дней, а уже прошедший срок не попадает.",
            ),
        },
        link="/app",
        link_label={
            "sr": "Otvori kabinet",
            "en": "Open your cabinet",
            "ru": "Открыть кабинет",
        },
    ),
    NewsItem(
        slug="svetska-banka",
        published=date(2026, 10, 17),
        title={
            "sr": "Novi izvor: Svetska banka (Zapadni Balkan)",
            "en": "New source: World Bank (Western Balkans)",
            "ru": "Новая площадка: Всемирный банк (Западные Балканы)",
        },
        body={
            "sr": (
                "Dodali smo Svetsku banku (objave o nabavkama) kao izvor za Zapadni Balkan: Severna Makedonija, Albanija, Bosna i Hercegovina, Kosovo i Crna Gora. Na dan 17.10. otvoreno je 12 tendera: Severna Makedonija 5, Albanija 2, Bosna i Hercegovina 2, Kosovo 2, Crna Gora 1. Rokovi za podnošenje su od 22.10. do 06.11.2026.",
                "Tekstovi tendera su na engleskom i nemaju CPV šifre, zato ključne reči u šablonu pišite na engleskom. Izvor uključite u šablonu: štiklirajte ga na koraku „Sastav“, u kartici šablona dugmetom za jedan izvor ili u podešavanjima šablona. Izvor vidite i na stranici Izvori.",
            ),
            "en": (
                "We added the World Bank (procurement notices) as a source for the Western Balkans: North Macedonia, Albania, Bosnia and Herzegovina, Kosovo and Montenegro. As of 17.10., 12 tenders are open: North Macedonia 5, Albania 2, Bosnia and Herzegovina 2, Kosovo 2, Montenegro 1. Submission deadlines run from 22.10. to 06.11.2026.",
                "Tender texts are in English and carry no CPV codes, so write your template keywords in English. To turn the source on, tick it in your template: on the Composition step, with the single-source button on the template card, or in the template settings. You can also see the source on the Sources page.",
            ),
            "ru": (
                "Мы добавили Всемирный банк (объявления о закупках) как площадку для Западных Балкан: Северная Македония, Албания, Босния и Герцеговина, Косово и Черногория. На 17.10 открыто 12 тендеров: Северная Македония 5, Албания 2, Босния и Герцеговина 2, Косово 2, Черногория 1. Сроки подачи: с 22.10 по 06.11.2026.",
                "Тексты тендеров на английском и без кодов CPV, поэтому ключевые слова в шаблоне стоит писать по-английски. Чтобы включить площадку, поставьте галочку в шаблоне: на шаге «Состав», кнопкой добавления одной площадки в карточке шаблона или в настройках шаблона. Площадку также видно на странице «Площадки».",
            ),
        },
        link="/izvori",
        link_label={
            "sr": "Pogledaj izvore",
            "en": "View sources",
            "ru": "Смотреть площадки",
        },
    ),
    NewsItem(
        slug="kalendar-rokova",
        published=date(2026, 10, 17),
        title={
            "sr": "Rok podnošenja u vaš kalendar",
            "en": "Submission deadline in your calendar",
            "ru": "Срок подачи в ваш календарь",
        },
        body={
            "sr": (
                "Na kartici tendera pojavilo se dugme „📅 U kalendar“ dok rok za podnošenje nije prošao. Klik preuzima datoteku .ics sa rokom, linkom na tender i podsetnicima tri dana i jedan dan pre roka.",
                "Na stranici „Moji tenderi“ dugme „Svi rokovi (.ics)“ preuzima rokove svih praćenih tendera jednom datotekom. Datoteka se otvara u Google kalendaru (Podešavanja → Uvoz), Outlook-u i Apple kalendaru.",
            ),
            "en": (
                "A tender card now has an “📅 Add to calendar” button while the submission deadline has not passed. It downloads an .ics file with the deadline, a link to the tender, and reminders 3 days and 1 day before it.",
                "On the My tenders page, “All deadlines (.ics)” downloads the deadlines of all tenders you follow in one file. The file opens in Google Calendar (Settings → Import), Outlook and Apple Calendar.",
            ),
            "ru": (
                "На карточке тендера появилась кнопка «📅 В календарь», пока срок подачи не прошёл. Она скачивает файл .ics: со сроком, ссылкой на тендер и напоминаниями за 3 дня и за 1 день до срока.",
                "На странице «Мои тендеры» кнопка «Все сроки (.ics)» скачивает сроки всех отслеживаемых тендеров одним файлом. Файл открывается в Google Календаре (Настройки → Импорт), Outlook и Apple Календаре.",
            ),
        },
        link="/tenderi",
        link_label={
            "sr": "Pregledaj tendere",
            "en": "Browse tenders",
            "ru": "Смотреть тендеры",
        },
    ),
    NewsItem(
        slug="novi-izvori",
        published=date(2026, 10, 16),
        title={
            "sr": "Novi izvori: CEJN, CFCU, EEAS, NIS i TED Balkan",
            "en": "New sources: CEJN, CFCU, EEAS, NIS and TED Balkan",
            "ru": "Новые площадки: CEJN, CFCU, EEAS, NIS и TED Balkan",
        },
        body={
            "sr": (
                "Dodali smo pet novih izvora sa javnim objavama: CEJN Crna Gora (Crna Gora), CFCU Ministarstvo finansija (Srbija, IPA/CBC), EEAS — Delegacije EU (Srbija, Bosna i Hercegovina, Crna Gora), NIS a.d. (Srbija) i TED — Zapadni Balkan (Bosna i Hercegovina, Crna Gora, Severna Makedonija, Srbija).",
                "Novi izvor sam ne stiže u postojeće šablone: pretplata je oznaka u svakom šablonu. Na početnoj strani videćete poruku za izvore koji nedostaju u vašim šablonima. Uključite ih jednim klikom ili u podešavanju šablona.",
            ),
            "en": (
                "We added five new sources with public notices: CEJN Montenegro (Montenegro), CFCU Ministry of Finance (Serbia, IPA/CBC), EEAS — EU Delegations (Serbia, Bosnia and Herzegovina, Montenegro), NIS a.d. (Serbia) and TED — Western Balkans (Bosnia and Herzegovina, Montenegro, North Macedonia, Serbia).",
                "A new source does not reach existing templates by itself: a subscription is a tick in each template. The home page shows a notice for sources missing from your templates. Turn them on with one click, or in the template settings.",
            ),
            "ru": (
                "Мы добавили пять новых площадок с открытыми объявлениями: CEJN Черногория (Черногория), CFCU Министерство финансов (Сербия, IPA/CBC), EEAS — Делегации ЕС (Сербия, Босния и Герцеговина, Черногория), NIS a.d. (Сербия) и TED — Западные Балканы (Босния и Герцеговина, Черногория, Северная Македония, Сербия).",
                "Новая площадка сама не попадает в уже созданные шаблоны: подписка — галочка в каждом шаблоне. На главной странице кабинета вы увидите предложение для площадок, которых нет в ваших шаблонах. Включите их одним нажатием или в настройках шаблона.",
            ),
        },
        link="/izvori",
        link_label={
            "sr": "Pogledaj izvore",
            "en": "View sources",
            "ru": "Смотреть площадки",
        },
    ),
    NewsItem(
        slug="prethodna-obavestenja",
        published=date(2026, 10, 15),
        title={
            "sr": "Novi tipovi objava u TenderSRB-u",
            "en": "New notice types in TenderSRB",
            "ru": "Новые типы объявлений в TenderSRB",
        },
        body={
            "sr": (
                "Na TenderSRB sada se prikazuju i nove vrste objava sa portala javnih nabavki: prethodna informativna obaveštenja (PIN), obaveštenja o odbrani i bezbednosti (F17), pozivi za socijalne usluge (F21) i obaveštenja o sistemu kvalifikacije (F07).",
                "PIN ranije najavljuje plan nabavke, pa možete da se pripremite pre nego što tender bude raspisan. PIN i sistem kvalifikacije nemaju rok za podnošenje ponuda, zato kartica za njih navodi da rok nije naveden.",
            ),
            "en": (
                "TenderSRB now also shows new notice types from the public procurement portal: prior information notices (PIN), defence and security notices (F17), social services calls (F21) and qualification system notices (F07).",
                "A PIN lets you see a procurement plan in advance, so you can prepare before the tender is published. PIN and qualification systems have no submission deadline, so the card says the deadline is not specified for them.",
            ),
            "ru": (
                "В TenderSRB теперь показываются и новые типы объявлений с портала госзакупок: предварительные информационные уведомления (PIN), объявления в сфере обороны и безопасности (Ф17), объявления о социальных услугах (Ф21) и объявления о системе квалификации (Ф07).",
                "PIN заранее показывает план закупки: вы видите будущую закупку до того, как тендер опубликован, и успеваете подготовиться. У PIN и системы квалификации нет срока подачи, поэтому в карточке для них стоит «срок не указан».",
            ),
        },
        link="/tenderi",
        link_label={
            "sr": "Pregledaj tendere",
            "en": "Browse tenders",
            "ru": "Смотреть тендеры",
        },
    ),
    NewsItem(
        slug="telegram-kanal",
        published=date(2026, 10, 14),
        title={
            "sr": "Telegram kanal TenderSRB",
            "en": "TenderSRB Telegram channel",
            "ru": "Telegram-канал TenderSRB",
        },
        body={
            "sr": (
                "Otvorili smo Telegram kanal @nabrs_news: novosti o servisu, nove funkcije i beta test.",
                "Pitanja i predlozi — u komentarima ispod objava (grupa „nab.rs Questions and Feedback“). Obaveštenja o tenderima i dalje stižu preko bota @nabrs_bot i e-pošte.",
            ),
            "en": (
                "We have opened the Telegram channel @nabrs_news: service news, new features and the beta test.",
                "Questions and suggestions go in the comments under posts (group “nab.rs Questions and Feedback”). Tender notifications still arrive via the @nabrs_bot bot and by email.",
            ),
            "ru": (
                "Мы открыли Telegram-канал @nabrs_news: новости сервиса, новые функции и бета-тест.",
                "Вопросы и предложения — в комментариях под постами (группа «nab.rs Questions and Feedback»). Уведомления о тендерах по-прежнему приходят через бота @nabrs_bot и по почте.",
            ),
        },
        link=TG_CHANNEL_URL,
        link_label={
            "sr": "Otvori kanal",
            "en": "Open channel",
            "ru": "Открыть канал",
        },
    ),
    NewsItem(
        slug="statistika-trzista",
        published=date(2026, 10, 10),
        title={
            "sr": "Statistika tržišta javnih nabavki",
            "en": "Public procurement market statistics",
            "ru": "Статистика рынка госзакупок",
        },
        body={
            "sr": (
                "Na stranici Statistika objavljujemo zbirne podatke o javnim nabavkama u Srbiji: broj tendera po mesecima, najzastupljenije oblasti (CPV), vrste postupka i odluke o dodeli ugovora.",
                "Prikazani su samo zbirni podaci, bez podataka o ličnostima. Na stranici su navedeni datum početka podataka i datum stanja.",
            ),
            "en": (
                "The Statistics page publishes aggregate data on public procurement in Serbia: tenders per month, the most frequent areas (CPV), procedure types and contract award decisions.",
                "Only aggregates are shown, with no personal data. The page states when the data starts and the date it is current as of.",
            ),
            "ru": (
                "На странице «Статистика» мы публикуем сводные данные о госзакупках Сербии: число тендеров по месяцам, самые частые отрасли (CPV), виды процедур и решения о заключении договоров.",
                "Показаны только сводные данные, без персональных. На странице указаны дата начала данных и дата, на которую они актуальны.",
            ),
        },
        link="/statistika",
        link_label={
            "sr": "Otvori statistiku",
            "en": "Open statistics",
            "ru": "Открыть статистику",
        },
    ),
    NewsItem(
        slug="analitika-excel",
        published=date(2026, 10, 10),
        title={
            "sr": "Analitika i izveštaji u Excel-u",
            "en": "Analytics and reports in Excel",
            "ru": "Аналитика и отчёты в Excel",
        },
        body={
            "sr": (
                "U kabinetu je nova stranica Analitika: sastavite sopstveni izveštaj o tržištu i preuzmite ga kao Excel datoteku.",
                "Dostupno na tarifi Business i tokom probnog perioda.",
            ),
            "en": (
                "The cabinet has a new Analytics page: build your own market report and download it as an Excel file.",
                "Available on the Business plan and during the trial period.",
            ),
            "ru": (
                "В кабинете появилась страница «Аналитика»: соберите собственный отчёт о рынке и скачайте его файлом Excel.",
                "Доступно на тарифе Business и в пробный период.",
            ),
        },
        link="/app/analitika",
        link_label={
            "sr": "Otvori analitiku",
            "en": "Open analytics",
            "ru": "Открыть аналитику",
        },
    ),
    NewsItem(
        slug="sve-na-latinici",
        published=date(2026, 10, 10),
        title={
            "sr": "Sve na latinici",
            "en": "Everything in Latin script",
            "ru": "Всё на латинице",
        },
        body={
            "sr": (
                "Srpski u TenderSRB-u sada je svuda na latinici: stranice, tenderi, nazivi naručilaca i oblasti, kao i poruke koje šaljemo.",
                "Podaci u izvornom obliku ostaju neizmenjeni, a pretraga radi i kada ukucate ćirilicu.",
            ),
            "en": (
                "Serbian in TenderSRB is now shown in Latin script everywhere: pages, tenders, names of contracting authorities and areas, and the messages we send.",
                "Source data is stored unchanged, and search works when you type Cyrillic too.",
            ),
            "ru": (
                "Сербский в TenderSRB теперь везде показывается латиницей: страницы, тендеры, названия заказчиков и отраслей, а также наши сообщения.",
                "Исходные данные хранятся без изменений, а поиск работает и при вводе кириллицей.",
            ),
        },
        link="/tenderi",
        link_label={
            "sr": "Pregledaj tendere",
            "en": "Browse tenders",
            "ru": "Смотреть тендеры",
        },
    ),
)

NEWS_BY_SLUG: dict[str, NewsItem] = {n.slug: n for n in NEWS}


def latest_date() -> date:
    return max(n.published for n in NEWS)


def parse_seen(raw: str | None) -> date | None:
    """Cookie value → date; garbage → ``None`` (as “not read”)."""
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def latest_unread(raw_cookie: str | None) -> NewsItem | None:
    """The freshest news item (the first in :data:`NEWS`), if it is newer than what was read."""
    seen = parse_seen(raw_cookie)
    newest = NEWS[0]
    return newest if seen is None or newest.published > seen else None
