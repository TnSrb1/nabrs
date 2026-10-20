"""Drafts of the legal texts of the showcase (TASK 2, SITE-05).

⚠️ A DRAFT by Claude — the owner edits it finally and takes responsibility. The placeholders ``[[owner: ...]]`` mark what must be filled in / checked (the DOO details of the partner, the contact for PII requests, the legal basis of processing, the jurisdiction, the effective date). The texts are in the three languages of the showcase (sr / ru / en); the language is chosen by :func:`src.api.public.i18n.resolve_lang`.

The structure of one document per language::

    {"title": str, "updated": str, "intro": str,
     "sections": [{"h": str, "body": [str, ...]}, ...]}

Rendered by the common template ``legal.html``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# The service markup ``[[owner: ...]]`` in the legal text is an unfilled placeholder.
# It must not leak out: the render guard (:func:`get_doc`) cuts it out and substitutes
# a neutral localised stub, and records the fact of the non-completion in the WARNING log.
_PLACEHOLDER_RE = re.compile(r"\[\[owner:\s*(.*?)\s*\]\]", re.DOTALL)

# A localised neutral stub instead of an unfilled ``[[owner: ...]]``
# (sr / ru / en — the languages of the showcase; the key is the document language).
_UNFILLED_FALLBACK = {
    "sr": "biće precizirano",
    "ru": "уточняется",
    "en": "to be specified",
}

_PII_CONTACT = "[[owner: e-mail для запросов по персональным данным]]"
_PROCESSING_BASIS = (
    "[[owner: правовое основание обработки — договор / согласие / легитимный интерес]]"
)
_JURISDICTION = "[[owner: применимое право и юрисдикция — Сербия / по адресу DOO]]"
# The effective date — the date of the text check against the code (owner’s decision 10.10.2026).
_EFFECTIVE = "10.10.2026"
_DOO_REQUISITES = (
    "[[owner: полное наименование DOO, адрес регистрации, матичный broj (МБ), "
    "ПИБ (налоговый номер), законный представитель]]"
)

LEGAL: dict[str, dict[str, dict[str, object]]] = {
    # ----------------------------------------------------------------- privacy
    "privacy": {
        "sr": {
            "title": "Politika privatnosti",
            "updated": f"Stupa na snagu: {_EFFECTIVE}",
            "intro": (
                "Ova politika opisuje koje podatke Tender Bot Serbia prikuplja, "
                "u koje svrhe ih obrađuje i koja prava imate. Rukovalac podacima: "
                f"{_DOO_REQUISITES}."
            ),
            "sections": [
                {
                    "h": "Koje podatke prikupljamo",
                    "body": [
                        "E-mail adresu (registracija i prijava bez lozinke).",
                        "Telegram chat ID — radi isporuke obaveštenja u bot.",
                        "Profile pretrage koje sami kreirate (ključne reči, "
                        "CPV-kategorije, podešavanja).",
                        "Tehničke zapise (IP, vreme zahteva) radi bezbednosti i "
                        "sprečavanja zloupotrebe.",
                        "Heš e-adrese (ne sama adresa) — radi zaštite od ponovnog "
                        "korišćenja probnog perioda: jedan probni period po adresi.",
                    ],
                },
                {
                    "h": "Svrha i pravni osnov obrade",
                    "body": [
                        "Pružanje usluge praćenja nabavki i isporuke obaveštenja.",
                        f"Pravni osnov: {_PROCESSING_BASIS}.",
                    ],
                },
                {
                    "h": "Deljenje sa trećim licima",
                    "body": [
                        "Hosting infrastruktura (VPS provajder) i provajder e-pošte "
                        "— radi rada servisa i slanja pisama.",
                        "Telegram — radi isporuke poruka u bot.",
                        "AI-provajder (Anthropic) — tekst tehničke specifikacije "
                        "tendera šalje se radi automatske analize. Ne prodajemo "
                        "vaše podatke trećim licima.",
                    ],
                },
                {
                    "h": "Rok čuvanja",
                    "body": [
                        "Podatke čuvamo dok je nalog aktivan i koliko je potrebno "
                        "za navedene svrhe. [[owner: precizirati rokove]]",
                        "Nalog možete sami obrisati u kabinetu (Nalog), potvrdom "
                        "unosom svoje e-adrese. Brisanje je trajno: pristup se "
                        "odmah zatvara, Telegram i e-pošta se odvezuju, a e-adresa, "
                        "telefon, beleške, fascikle i praćenje tendera se brišu ili "
                        "anonimizuju; šabloni pretrage se gase, a njihovi nazivi i "
                        "dodatni AI-kontekst brišu. Ako nalog ima druge korisnike, "
                        "uklanja se samo vaš pristup, a nalog ostaje njima.",
                        "Posle brisanja zadržavamo: heš e-adrese (zaštita od "
                        "zloupotrebe probnog perioda), evidenciju o naplati, "
                        "anonimni zapis o brisanju, kao i tehničke i neidentifikujuće "
                        "zapise naloga (podešavanja šablona bez naziva, istorija "
                        "slanja, AI-analize tendera i brojači AI-potrošnje), bez veze "
                        "sa e-adresom.",
                        "Samostalna ponovna registracija sa istom e-adresom nije "
                        "moguća; novi pristup je moguć samo na zahtev upućen na "
                        f"{_PII_CONTACT}, uz odobrenje operatera.",
                    ],
                },
                {
                    "h": "Vaša prava",
                    "body": [
                        "Pristup, ispravka, brisanje podataka i povlačenje "
                        "saglasnosti. Nalog možete obrisati sami u kabinetu "
                        "(Nalog); za ostala prava obratite se na: "
                        f"{_PII_CONTACT}.",
                    ],
                },
                {
                    "h": "Kolačići",
                    "body": [
                        "Koristimo neophodne kolačiće: sesijski (prijava) i jezički "
                        "(izbor jezika). Ne koristimo reklamne kolačiće.",
                    ],
                },
            ],
        },
        "ru": {
            "title": "Политика конфиденциальности",
            "updated": f"Вступает в силу: {_EFFECTIVE}",
            "intro": (
                "Политика описывает, какие данные собирает Tender Bot Serbia, с "
                "какими целями обрабатывает и какие у вас права. Оператор данных: "
                f"{_DOO_REQUISITES}."
            ),
            "sections": [
                {
                    "h": "Какие данные мы собираем",
                    "body": [
                        "E-mail (беспарольная регистрация и вход).",
                        "Telegram chat ID — для доставки уведомлений в бот.",
                        "Профили поиска, которые вы создаёте (ключевые слова, "
                        "CPV-категории, настройки).",
                        "Технические записи (IP, время запроса) для безопасности и "
                        "защиты от злоупотреблений.",
                        "Хеш e-mail (не сам адрес) — для защиты от повторного "
                        "получения пробного периода: один пробный период на адрес.",
                    ],
                },
                {
                    "h": "Цели и основание обработки",
                    "body": [
                        "Оказание услуги мониторинга закупок и доставки уведомлений.",
                        f"Правовое основание: {_PROCESSING_BASIS}.",
                    ],
                },
                {
                    "h": "Передача третьим лицам",
                    "body": [
                        "Хостинг (VPS-провайдер) и провайдер e-mail — для работы "
                        "сервиса и отправки писем.",
                        "Telegram — для доставки сообщений в бот.",
                        "AI-провайдер (Anthropic) — текст техзадания тендера "
                        "передаётся для автоматического разбора. Мы не продаём ваши "
                        "данные третьим лицам.",
                    ],
                },
                {
                    "h": "Срок хранения",
                    "body": [
                        "Храним данные, пока аккаунт активен и сколько необходимо "
                        "для указанных целей. [[owner: уточнить сроки]]",
                        "Аккаунт можно удалить самостоятельно в кабинете (Аккаунт), "
                        "подтвердив вводом своего e-mail. Удаление необратимо: доступ "
                        "закрывается сразу, Telegram и e-mail отвязываются, а e-mail, "
                        "телефон, заметки, папки и отслеживание тендеров удаляются "
                        "или обезличиваются; шаблоны поиска отключаются, их названия "
                        "и дополнительный AI-контекст стираются. Если в аккаунте есть "
                        "другие пользователи, снимается только ваш доступ, аккаунт "
                        "остаётся у них.",
                        "После удаления мы сохраняем: хеш e-mail (защита от "
                        "злоупотребления пробным периодом), сведения о платежах, "
                        "анонимную запись об удалении, а также технические и "
                        "неидентифицирующие записи аккаунта (настройки шаблонов без "
                        "названия, история отправки, AI-разборы тендеров и счётчики "
                        "использования AI), не связанные с e-mail.",
                        "Самостоятельная повторная регистрация с тем же e-mail "
                        "невозможна; новый доступ возможен только по запросу на "
                        f"{_PII_CONTACT} с одобрения оператора.",
                    ],
                },
                {
                    "h": "Ваши права",
                    "body": [
                        "Доступ, исправление, удаление данных и отзыв согласия. "
                        "Аккаунт можно удалить самостоятельно в кабинете (Аккаунт); "
                        f"по остальным правам напишите: {_PII_CONTACT}.",
                    ],
                },
                {
                    "h": "Cookie",
                    "body": [
                        "Используем только необходимые cookie: сессионную (вход) и "
                        "языковую (выбор языка). Рекламные cookie не используем.",
                    ],
                },
            ],
        },
        "en": {
            "title": "Privacy Policy",
            "updated": f"Effective date: {_EFFECTIVE}",
            "intro": (
                "This policy describes what data Tender Bot Serbia collects, the "
                "purposes of processing, and your rights. Data controller: "
                f"{_DOO_REQUISITES}."
            ),
            "sections": [
                {
                    "h": "What data we collect",
                    "body": [
                        "Email address (passwordless sign-up and sign-in).",
                        "Telegram chat ID — to deliver notifications to the bot.",
                        "Search profiles you create (keywords, CPV categories, "
                        "settings).",
                        "Technical logs (IP, request time) for security and abuse "
                        "prevention.",
                        "A hash of the email address (not the address itself) — to "
                        "prevent repeat use of the trial: one trial per address.",
                    ],
                },
                {
                    "h": "Purpose and legal basis",
                    "body": [
                        "Providing the tender monitoring and notification service.",
                        f"Legal basis: {_PROCESSING_BASIS}.",
                    ],
                },
                {
                    "h": "Sharing with third parties",
                    "body": [
                        "Hosting infrastructure (VPS provider) and email provider — "
                        "to operate the service and send emails.",
                        "Telegram — to deliver messages to the bot.",
                        "AI provider (Anthropic) — the tender technical "
                        "specification text is sent for automated analysis. We do "
                        "not sell your data to third parties.",
                    ],
                },
                {
                    "h": "Retention",
                    "body": [
                        "We keep data while the account is active and as long as "
                        "needed for the stated purposes. [[owner: specify periods]]",
                        "You can delete the account yourself in the cabinet "
                        "(Account) by typing your email to confirm. Deletion is "
                        "permanent: access is closed at once, Telegram and email are "
                        "unlinked, and email, phone, notes, folders and tender "
                        "tracking are deleted or anonymised; search templates are "
                        "switched off and their names and extra AI context erased. "
                        "If the account has other users, only your own access is "
                        "removed and the account stays with them.",
                        "After deletion we keep: a hash of the email (trial-abuse "
                        "protection), billing records, an anonymous deletion record, "
                        "and technical, non-identifying account records (search "
                        "template settings without a name, delivery history, AI "
                        "tender analyses and AI usage counters), not linked to the "
                        "email.",
                        "Self-service re-registration with the same email is not "
                        "possible; new access is possible only on request sent to "
                        f"{_PII_CONTACT}, with the operator's approval.",
                    ],
                },
                {
                    "h": "Your rights",
                    "body": [
                        "Access, correction, deletion, and withdrawal of consent. "
                        "You can delete the account yourself in the cabinet "
                        f"(Account); for other rights, contact: {_PII_CONTACT}.",
                    ],
                },
                {
                    "h": "Cookies",
                    "body": [
                        "We use only necessary cookies: session (sign-in) and "
                        "language (language choice). We do not use advertising "
                        "cookies.",
                    ],
                },
            ],
        },
    },
    # ------------------------------------------------------------------- terms
    "terms": {
        "sr": {
            "title": "Uslovi korišćenja",
            "updated": f"Stupa na snagu: {_EFFECTIVE}",
            "intro": (
                "Korišćenjem servisa Tender Bot Serbia prihvatate ove uslove. "
                "Pažljivo ih pročitajte."
            ),
            "sections": [
                {
                    "h": "Opis servisa",
                    "body": [
                        "Servis prati javne nabavke u Srbiji i šalje relevantne "
                        "tendere uz AI-analizu tehničke specifikacije. Informacije "
                        "su informativnog karaktera i ne predstavljaju pravni savet.",
                    ],
                },
                {
                    "h": "Registracija i pristup",
                    "body": [
                        "Pristup zahteva registraciju putem e-pošte. Svaki novi "
                        "nalog dobija {trial} probnog perioda sa mogućnostima "
                        "tarife Pro; šta se dešava posle isteka pristupa, opisano "
                        "je u odeljku „Tarife i probni period“.",
                        "Nalog možete sami obrisati u kabinetu (Nalog), potvrdom "
                        "unosom e-adrese. Brisanje je trajno; dok traje aktivna "
                        "plaćena pretplata, prvo je otkažite.",
                    ],
                },
                {
                    "h": "Tarife i probni period",
                    "body": [
                        "Probni period traje {trial} i besplatan je.",
                        "Posle isteka pristupa slanje tendera se zaustavlja, ali "
                        "prijava u kabinet ostaje: pregled feeda, kartica tendera i "
                        "vaših podataka je moguć. AI-funkcije (AI-analize, procene, "
                        "ček-liste, nacrti prijave), analitika i praćenje dodele "
                        "ugovora nisu dostupni dok pristup ne bude produžen.",
                        "Produženje slanja možete besplatno zatražiti iz obaveštenja "
                        "u kabinetu. Svako produženje traje najviše 30 dana, "
                        "odobrava ga operater i može biti odbijeno.",
                        "Pro — 29 EUR mesečno: 3 šablona pretrage; mesečno do 10 "
                        "dubinskih AI-analiza, 30 procena „učestvovati ili ne“, "
                        "30 ček-lista dokumenata i 5 nacrta prijave.",
                        "Business — 79 EUR mesečno: 10 šablona pretrage; mesečno "
                        "do 50 dubinskih AI-analiza, 150 procena „učestvovati ili "
                        "ne“, 150 ček-lista dokumenata i 30 nacrta prijave.",
                        "Enterprise — po dogovoru.",
                        "Cene su iskazane u evrima. Bez godišnjeg ugovora, "
                        "otkazivanje u svakom trenutku.",
                    ],
                },
                {
                    "h": "Prihvatljivo korišćenje",
                    "body": [
                        "Ne smete zloupotrebljavati servis, automatski masovno "
                        "preuzimati sadržaj niti ometati rad sistema.",
                    ],
                },
                {
                    "h": "Odricanje od garancija",
                    "body": [
                        "Ne garantujemo potpunost, tačnost ni pravovremenost "
                        "podataka iz izvora javnih nabavki. Odluke o učešću u "
                        "tenderima donosite samostalno i na sopstvenu odgovornost.",
                    ],
                },
                {
                    "h": "Ograničenje odgovornosti",
                    "body": [
                        "U najvećoj meri dozvoljenoj zakonom, ne odgovaramo za "
                        "indirektnu štetu nastalu korišćenjem servisa. "
                        "[[owner: proveriti formulaciju sa pravnikom]]",
                    ],
                },
                {
                    "h": "Izmene uslova",
                    "body": [
                        "Uslove možemo menjati; o bitnim izmenama obavestićemo "
                        "korisnike.",
                    ],
                },
                {
                    "h": "Merodavno pravo",
                    "body": [f"{_JURISDICTION}."],
                },
            ],
        },
        "ru": {
            "title": "Условия использования",
            "updated": f"Вступает в силу: {_EFFECTIVE}",
            "intro": (
                "Используя сервис Tender Bot Serbia, вы принимаете эти условия. "
                "Внимательно прочитайте их."
            ),
            "sections": [
                {
                    "h": "Описание сервиса",
                    "body": [
                        "Сервис отслеживает госзакупки Сербии и присылает "
                        "релевантные тендеры с AI-разбором техзадания. Информация "
                        "носит справочный характер и не является юридической "
                        "консультацией.",
                    ],
                },
                {
                    "h": "Регистрация и доступ",
                    "body": [
                        "Доступ требует регистрации по e-mail. Каждый новый "
                        "аккаунт получает {trial} пробного периода с возможностями "
                        "тарифа Pro; что происходит после окончания доступа — в "
                        "разделе «Тарифы и пробный период».",
                        "Аккаунт можно удалить самостоятельно в кабинете (Аккаунт), "
                        "подтвердив вводом e-mail. Удаление необратимо; пока "
                        "действует платная подписка, сначала отмените её.",
                    ],
                },
                {
                    "h": "Тарифы и пробный период",
                    "body": [
                        "Пробный период длится {trial} и бесплатен.",
                        "После окончания доступа рассылка тендеров останавливается, "
                        "но вход в кабинет сохраняется: доступны просмотр ленты, "
                        "карточек тендеров и ваших данных. AI-функции (AI-разбор, "
                        "вердикты, чек-листы, черновики заявки), аналитика и "
                        "отслеживание результатов закупок недоступны, пока доступ "
                        "не продлён.",
                        "Продление рассылки можно бесплатно запросить из баннера в "
                        "кабинете. Каждое продление — не более 30 дней, его "
                        "одобряет оператор, в продлении может быть отказано.",
                        "Pro — 29 EUR в месяц: 3 шаблона поиска; в месяц до 10 "
                        "глубоких AI-разборов, 30 вердиктов «участвовать или нет», "
                        "30 чек-листов документов и 5 черновиков заявки.",
                        "Business — 79 EUR в месяц: 10 шаблонов поиска; в месяц до "
                        "50 глубоких AI-разборов, 150 вердиктов «участвовать или "
                        "нет», 150 чек-листов документов и 30 черновиков заявки.",
                        "Enterprise — по договорённости.",
                        "Цены указаны в евро. Без годового контракта, отмена в "
                        "любой момент.",
                    ],
                },
                {
                    "h": "Допустимое использование",
                    "body": [
                        "Запрещено злоупотреблять сервисом, автоматически массово "
                        "выгружать контент и мешать работе системы.",
                    ],
                },
                {
                    "h": "Отказ от гарантий",
                    "body": [
                        "Мы не гарантируем полноту, точность и своевременность "
                        "данных из источников госзакупок. Решения об участии в "
                        "тендерах вы принимаете самостоятельно и под свою "
                        "ответственность.",
                    ],
                },
                {
                    "h": "Ограничение ответственности",
                    "body": [
                        "В максимально допустимой законом мере мы не отвечаем за "
                        "косвенный ущерб от использования сервиса. "
                        "[[owner: проверить формулировку с юристом]]",
                    ],
                },
                {
                    "h": "Изменение условий",
                    "body": [
                        "Условия могут меняться; о существенных изменениях уведомим "
                        "пользователей.",
                    ],
                },
                {
                    "h": "Применимое право",
                    "body": [f"{_JURISDICTION}."],
                },
            ],
        },
        "en": {
            "title": "Terms of Use",
            "updated": f"Effective date: {_EFFECTIVE}",
            "intro": (
                "By using Tender Bot Serbia you accept these terms. Please read "
                "them carefully."
            ),
            "sections": [
                {
                    "h": "Service description",
                    "body": [
                        "The service monitors Serbian public procurement and sends "
                        "relevant tenders with AI analysis of the technical "
                        "specification. The information is for reference only and is "
                        "not legal advice.",
                    ],
                },
                {
                    "h": "Registration and access",
                    "body": [
                        "Access requires email registration. Every new account "
                        "gets a {trial_n}-day trial with the features of the Pro plan; "
                        "what happens after access ends is described in “Plans and "
                        "trial period”.",
                        "You can delete the account yourself in the cabinet "
                        "(Account) by typing your email to confirm. Deletion is "
                        "permanent; while a paid subscription is active, cancel it "
                        "first.",
                    ],
                },
                {
                    "h": "Plans and trial period",
                    "body": [
                        "The trial lasts {trial} and is free.",
                        "After access ends, tender delivery stops, but you can still "
                        "sign in to the cabinet: browsing the feed, tender cards and "
                        "your data remains possible. AI features (AI analysis, "
                        "verdicts, checklists, application drafts), analytics and "
                        "award tracking are unavailable until access is extended.",
                        "You can request a free extension of delivery from the "
                        "banner in the cabinet. Each extension is at most 30 days, "
                        "is approved by an operator, and may be declined.",
                        "Pro — 29 EUR per month: 3 search templates; per month up "
                        "to 10 deep AI analyses, 30 bid / no-bid verdicts, 30 "
                        "document checklists and 5 application drafts.",
                        "Business — 79 EUR per month: 10 search templates; per "
                        "month up to 50 deep AI analyses, 150 bid / no-bid "
                        "verdicts, 150 document checklists and 30 application "
                        "drafts.",
                        "Enterprise — by agreement.",
                        "Prices are in euros. No annual contract, cancel anytime.",
                    ],
                },
                {
                    "h": "Acceptable use",
                    "body": [
                        "You must not abuse the service, automatically bulk-download "
                        "content, or disrupt the system.",
                    ],
                },
                {
                    "h": "Disclaimer of warranties",
                    "body": [
                        "We do not guarantee the completeness, accuracy, or "
                        "timeliness of data from public procurement sources. You make "
                        "tender participation decisions yourself and at your own risk.",
                    ],
                },
                {
                    "h": "Limitation of liability",
                    "body": [
                        "To the maximum extent permitted by law, we are not liable "
                        "for indirect damages arising from use of the service. "
                        "[[owner: verify wording with a lawyer]]",
                    ],
                },
                {
                    "h": "Changes to the terms",
                    "body": [
                        "We may change these terms; we will notify users of material "
                        "changes.",
                    ],
                },
                {
                    "h": "Governing law",
                    "body": [f"{_JURISDICTION}."],
                },
            ],
        },
    },
    # --------------------------------------------------------------- impressum
    "impressum": {
        "sr": {
            "title": "Impresum",
            "updated": "",
            "intro": "Podaci o pružaocu usluge.",
            "sections": [
                {"h": "Pružalac usluge", "body": [f"{_DOO_REQUISITES}."]},
                {"h": "Kontakt", "body": ["E-pošta: info@nab.rs"]},
                {
                    "h": "Napomena",
                    "body": [
                        "[[owner: dopuniti u skladu sa srpskim propisima o e-trgovini "
                        "i obaveznim podacima pružaoca usluge]]",
                    ],
                },
            ],
        },
        "ru": {
            "title": "Импрессум (реквизиты)",
            "updated": "",
            "intro": "Сведения о поставщике услуги.",
            "sections": [
                {"h": "Поставщик услуги", "body": [f"{_DOO_REQUISITES}."]},
                {"h": "Контакт", "body": ["E-mail: info@nab.rs"]},
                {
                    "h": "Примечание",
                    "body": [
                        "[[owner: дополнить по требованиям сербского законодательства "
                        "об электронной торговле и обязательным сведениям о "
                        "поставщике услуги]]",
                    ],
                },
            ],
        },
        "en": {
            "title": "Imprint",
            "updated": "",
            "intro": "Service provider information.",
            "sections": [
                {"h": "Service provider", "body": [f"{_DOO_REQUISITES}."]},
                {"h": "Contact", "body": ["Email: info@nab.rs"]},
                {
                    "h": "Note",
                    "body": [
                        "[[owner: complete per Serbian e-commerce regulations and "
                        "mandatory service-provider disclosures]]",
                    ],
                },
            ],
        },
    },
}


def _guard_placeholders(value: object, fallback: str, found: list[str]) -> object:
    """Recursively strips ``[[owner: ...]]`` → ``fallback``; collects the marks into ``found``.

    Returns a NEW structure (it does not mutate the shared ``LEGAL``), so that the draft marks remain in the source for the later filling in by the owner.
    """
    if isinstance(value, str):

        def _sub(m: re.Match[str]) -> str:
            found.append(m.group(1).strip())
            return fallback

        return _PLACEHOLDER_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_guard_placeholders(v, fallback, found) for v in value]
    if isinstance(value, dict):
        return {k: _guard_placeholders(v, fallback, found) for k, v in value.items()}
    return value


def get_doc(name: str, lang: str) -> dict[str, object] | None:
    """The document ``name`` (privacy / terms / impressum) in the language ``lang``, or ``None``.

    The render guard: unfilled ``[[owner: ...]]`` markup NEVER leaks out — it is replaced by a neutral localised stub, and the list of the unfilled service marks (without PII) is written to the WARNING log as technical debt.
    """
    from src.api.public.i18n import DEFAULT
    from src.api.public.trial_text import fill_trial

    page = LEGAL.get(name)
    if page is None:
        return None
    raw = page.get(lang) or page.get(DEFAULT)
    if raw is None:
        return None
    fallback = _UNFILLED_FALLBACK.get(lang) or _UNFILLED_FALLBACK[DEFAULT]
    found: list[str] = []
    doc = _guard_placeholders(raw, fallback, found)
    if found:
        logger.warning(
            "legal doc %r (lang=%s): %d unfilled [[owner:]] placeholder(s): %s",
            name,
            lang,
            len(found),
            " | ".join(sorted(set(found))),
        )
    # The trial length — from settings.trial_days (placeholders {trial} / {trial_n}).
    return fill_trial(doc, lang if lang in page else DEFAULT)  # type: ignore[no-any-return]
