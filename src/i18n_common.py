"""Shared bot↔cabinet vocabulary — a SINGLE source for both surfaces.

The funnel statuses (``tender_user_status``), plan names and the tender card
chrome in Telegram are shown by TWO different layers of the project:

- the web cabinet (:mod:`src.api.cabinet.i18n` — the stage chips on the dashboard/"My
  tenders", plan labels on the subscription showcase);
- the Telegram bot (:mod:`src.bots.telegram` — the toast and the “Status: …” line on the tender
  card, the plan in ``/status``) and Telegram DELIVERY
  (:mod:`src.workers.adapters.telegram` — card row labels, AI buttons,
  subject-type and procedure labels).

The bot does NOT import ``src.api.cabinet`` (the cabinet pulls in starlette/FastAPI —
workers and the bot do not need that and it creates extra coupling). Therefore the shared vocabulary
lives HERE — in a NEUTRAL module without external dependencies, which BOTH the cabinet
and the bot import. Duplicating the catalog is not allowed: otherwise the same
``tender_user_status`` string would be named differently in the bot and in the cabinet (the project rules — "shared
vocabulary in ONE catalog").

Two axes of language must not be mixed (the project rules): here ONLY the language of the interface chrome
(``users.locale``). Tender data and AI content (``profiles.target_language``) —
a different axis, NOT translated by this catalog.

Parity of the keys across all three languages and the match of the ru statuses with the funnel source
(:data:`src.db.models.tender_status.STATUS_LABELS`) are held by the tests
``tests/test_i18n_common.py`` and ``tests/api/test_cabinet_i18n.py``. The status keys
have the prefix ``status_`` by the values of ``STATUSES``; the ru strings MUST match
``STATUS_LABELS`` (the source of truth of the funnel) — COMMON is an add-on over the source,
not a copy.

The same rule for the card row labels (``lbl_*``, ``source``, ``no_title``,
``no_profile_name``, ``found_by_template``): the SAME tender card is drawn by TWO
channels — Telegram (from here) and e-mail (:mod:`src.i18n_email`), and the field cannot be
named differently in them. The keys are named identically, and the match of the strings in all three
languages is held by the guard ``test_card_labels_match_email_catalog`` — this is the same
technique as the funnel statuses: “an add-on over the source + a test for drift”. The strings are duplicated on purpose: moving the e-mail chrome to a
neutral catalog is a separate task, and a divergence is caught by the test.

Also here is the vocabulary of BACKGROUND Telegram notifications (``sum_*`` the daily summary, ``rem_*`` the deadline reminder, ``sub_*`` subscription expiry, ``aw_*`` the new-win alert). This is the same
“chrome the subscriber sees” layer as the delivery card, and its language is taken the same way — ``COALESCE(user_channels.locale,
users.locale)``.

The normalization of the language code and the “N days” declension are reused from
:mod:`src.i18n_email` (``norm_locale``/``days_phrase``; the same SUPPORTED sr/ru/en
and the default ru) — we do not make a second one.
"""

from __future__ import annotations

from src.i18n_email import DEFAULT, SUPPORTED, days_phrase, norm_locale

__all__ = [
    "CARD_LABEL_KEYS",
    "COMMON",
    "DEFAULT",
    "NOTIFY_LABEL_KEYS",
    "SUPPORTED",
    "common_t",
    "days_phrase",
    "norm_locale",
]


# --------------------------------------------------------------------------- #
# Shared bot↔cabinet vocabulary: funnel statuses + plan names + card chrome
# of a tender in Telegram delivery. Keys are the same in all languages (parity under
# test). ru statuses = STATUS_LABELS (guard test_common_status_labels_match_
# funnel_source). Plan names are Latin brand names, the same in all languages
# (except ``plan_trial`` — “trial access”, translated); kept here for the sake of
# a single source.
#
# Delivery card blocks (Telegram):
# - ``lbl_*``/``source``/``no_title``/``no_profile_name``/``found_by_template``
#   — card row labels; keys with the SAME NAMES exist in :mod:`src.i18n_email`
#   (the same card as an e-mail); the match of the strings is held by a guard;
# - ``ct_*``/``pt_*`` — labels of the VALUES of the subject type and procedure (machine code
#   → human-readable label, see src.workers.adapters.base);
# - ``btn_*`` — labels of the inline buttons of the delivery card (emoji in the string, as in
#   ``status_*`` and ``act_*`` of the bot catalog); callback_data does NOT depend on the language;
# - ``debug_*`` — profile debug mode (``profiles.debug_match``).
# Emoji prefixes of card ROWS (📍/🗓/…) live in the adapter, not here: they
# are part of the row markup, not vocabulary.
# --------------------------------------------------------------------------- #
COMMON: dict[str, dict[str, str]] = {
    "sr": {
        "status_new": "🆕 Novo",
        "status_watching": "👀 Pratim",
        "status_active": "✅ Učestvujem",
        "status_submitted": "📨 Ponuda podneta",
        "status_won": "🏆 Pobeda",
        "status_lost": "😔 Poraz",
        "status_skipped": "❌ Preskočeno",
        "plan_free": "Free",
        "plan_trial": "Probni",
        "plan_pro": "Pro",
        # Sources (SRC-0b): the mark for a source disabled by the admin and the country labels —
        # shared by the cabinet form and the bot (/izvori, /profile).
        "src_paused": "privremeno isključen",
        # Source capabilities (the /izvori showcase, the row under a source in the
        # profile form): keys are the fields of src.parsers.capabilities.SourceCapabilities;
        # cap_s_* — the short form for the form, “{names}” — source names.
        "cap_deadline": "Rok za podnošenje ponuda",
        "cap_deadline_if_stated": "Rok za podnošenje ponuda — ako je čitljiv u pozivu (skenirani pozivi su bez roka)",
        "cap_amount": "Procenjena vrednost nabavke",
        "cap_amount_if_stated": "Procenjena vrednost — ako je naručilac navede",
        "cap_notice_description": "Opis predmeta iz same objave",
        "cap_notice_description_if_stated": "Opis predmeta iz objave — ako ga naručilac navede (često samo ponavlja naslov)",
        "cap_documents_text": "Tekst konkursne dokumentacije — AI analiza čita samu dokumentaciju",
        "cap_documents_text_off": "Tekst konkursne dokumentacije (AI analiza radi samo po tekstu objave)",
        "cap_cpv": "CPV kodovi — izbor tendera po klasifikatoru",
        "cap_original_link": "Link na objavu na sajtu izvora",
        "cap_awards": "Obaveštenje o izabranom ponuđaču",
        "cap_dedup": "Ista nabavka objavljena i na: {names} — označena kao duplikat",
        "cap_lang_label": "Jezik objava",
        "cap_lang_sr": "srpski, ćirilica",
        "cap_lang_en_original": "uglavnom engleski, ponekad jezik naručioca",
        "cap_lang_bhs": "bosanski, hrvatski ili srpski",
        "cap_s_deadline": "rok",
        "cap_s_deadline_if_stated": "rok ako je naveden",
        "cap_s_amount": "vrednost",
        "cap_s_amount_if_stated": "vrednost ako je navedena",
        "cap_s_notice_description": "opis",
        "cap_s_notice_description_if_stated": "opis ako je naveden",
        "cap_s_documents_text": "dokumentacija za AI",
        "cap_s_cpv": "CPV",
        "cap_s_awards": "izabrani ponuđač",
        "cap_more": "Detaljnije o izvoru",
        "country_RS": "Srbija",
        "country_ME": "Crna Gora",
        "country_BA": "Bosna i Hercegovina",
        "country_MK": "Severna Makedonija",
        "country_AL": "Albanija",
        "country_XK": "Kosovo",
        "country_HR": "Hrvatska",
        "country_SI": "Slovenija",
        "country_BG": "Bugarska",
        "country_RO": "Rumunija",
        "country_HU": "Mađarska",
        "country_GR": "Grčka",
        "plan_business": "Business",
        "plan_enterprise": "Enterprise",
        # --- delivery card row labels (mirror of src.i18n_email) ---
        "no_title": "Bez naziva",
        "no_profile_name": "(bez naziva)",
        "found_by_template": "Pronađeno po šablonu",
        "why_matched": "Podudaranje",
        "cap_counter": "Danas poslato {sent} od {cap} po šablonu „{name}“",
        "cap_notice": "Dnevni limit od {cap} tendera po šablonu „{name}“ je dostignut. Ostali današnji tenderi su na sajtu: {feed_url}. Suzite šablon: {settings_url}",
        "cap_notice_subject": "Dnevni limit tendera je dostignut",
        "cap_help_line": "Do {cap} tendera dnevno po šablonu u Telegramu i na e-pošti; na sajtu — svi.",
        "source": "Izvor",
        "lbl_region": "Region",
        "lbl_type": "Vrsta",
        "lbl_procedure": "Postupak",
        "lbl_cpv": "CPV",
        "lbl_published": "Objavljen",
        "lbl_plan": "Plan",
        "lbl_deadline": "Rok do",
        "lbl_platform": "Platforma",
        "deadline_hour_unknown": "Čas zatvaranja izvor ne navodi",
        "also_on": "Isti tender je i na drugim platformama: {n}",
        "also_on_probable": "Verovatno isti tender je i na drugim platformama: {n}",
        "amount_estimated": "(procenjeno)",
        # --- value labels: contract subject type ---
        "ct_goods": "Dobra",
        "ct_services": "Usluge",
        "ct_works": "Radovi",
        # --- value labels: procedure type ---
        "pt_open": "Otvoreni postupak",
        "pt_restricted": "Restriktivni postupak",
        "pt_negotiated_with_call": "Pregovarački postupak (sa objavom)",
        "pt_negotiated_without_call": "Pregovarački postupak (bez objave)",
        "pt_framework": "Okvirni sporazum",
        "pt_dps": "Dinamični sistem nabavke",
        "pt_competitive_dialogue": "Konkurentni dijalog",
        "pt_innovation_partnership": "Partnerstvo za inovacije",
        "pt_design_contest": "Konkurs za dizajn",
        "pt_qualification": "Kvalifikacioni postupak",
        "pt_small_value": "Nabavka male vrednosti",
        "pt_pin": "Prethodno informativno obaveštenje",
        # --- delivery card buttons ---
        "btn_deep": "🧠 Detaljna analiza",
        "btn_bid": "🎯 Da li se isplati?",
        "btn_checklist": "📋 Šta pripremiti?",
        "btn_draft": "📝 Nacrt ponude",
        "btn_customer_analytics": "🏛 Ko pobeđuje?",
        "btn_configure_profile": "⚙️ Podesi ovaj šablon",
        "btn_open_cabinet": "📂 Otvori u kabinetu",
        "btn_follow": "⭐ Prati",
        "btn_hide": "✖ Sakrij",
        "btn_ai_menu": "🧠 AI analiza ▾",
        "btn_back": "↩ Nazad",
        "btn_restore": "↩ Vrati",
        "card_hidden": "sakriveno",
        # --- profile debug mode ---
        "debug_match": "Dijagnostika podudaranja",
        "debug_keywords": "ključne reči",
        "debug_cpv": "CPV",
        # --- daily funnel summary (src.workers.summary) ---
        "sum_title": "Dnevni pregled",
        "sum_active": "Učestvujemo",
        "sum_watching": "Pratimo",
        "sum_submitted": "Podneto",
        "sum_won": "Pobede",
        "sum_deadlines": "Najbliži rokovi",
        "sum_no_deadlines": "nema najbližih rokova",
        # --- deadline reminder (src.workers.reminders) ---
        "rem_today": "Podsetnik: rok je DANAS",
        "rem_in_days": "Podsetnik: do roka {days}",
        # --- subscription expiry (src.workers.subscription_reminders) ---
        "sub_expiring": "Pristup TenderSRB-u ističe za {days} ({date}).",
        "sub_renew": "Za produženje se obratite na {contact}.",
        "sub_expired": (
            "Pristup TenderSRB-u je istekao {date}. "
            "Isporuka novih tendera je zaustavljena."
        ),
        "sub_expired_renew": (
            "Kabinet ostaje otvoren: zatražite produženje slanja do 30 dana (odluku donosi operater) na app.nab.rs/app ili se obratite na {contact}."
        ),
        # --- new-win alert (src.workers.award_watch_notifier) ---
        "aw_new_win": "nova pobeda",
        "aw_customer": "Naručilac",
        "tc_title": "Tender je izmenjen",
        "tc_deadline": "Rok za podnošenje ponuda: {old} → {new}",
        "tc_amount": "Vrednost: {old} → {new}",
        "tc_files_changed": "Izmenjeni fajlovi: {names}",
        "tc_files_added": "Dodati fajlovi: {names}",
        "tc_files_removed": "Uklonjeni fajlovi: {names}",
        "tc_note": "Napomena naručioca: {text}",
        "tc_ai_stale": "Prethodna AI analiza je zastarela — zatražite novu na kartici.",
    },
    "ru": {
        "status_new": "🆕 Новый",
        "status_watching": "👀 Изучаю",
        "status_active": "✅ Участвую",
        "status_submitted": "📨 Заявка подана",
        "status_won": "🏆 Выиграли",
        "status_lost": "😔 Проиграли",
        "status_skipped": "❌ Пропущен",
        "plan_free": "Free",
        "plan_trial": "Триал",
        "plan_pro": "Pro",
        # Sources (SRC-0b): the mark for a source disabled by the admin and the country labels —
        # shared by the cabinet form and the bot (/izvori, /profile).
        "src_paused": "временно отключён",
        "cap_deadline": "Срок подачи заявок",
        "cap_deadline_if_stated": "Срок подачи заявок — если читается в приглашении (у сканов срока нет)",
        "cap_amount": "Оценочная стоимость закупки",
        "cap_amount_if_stated": "Оценочная стоимость — если указана заказчиком",
        "cap_notice_description": "Описание предмета из самого объявления",
        "cap_notice_description_if_stated": "Описание предмета из объявления — если указано заказчиком (часто лишь повторяет заголовок)",
        "cap_documents_text": "Текст конкурсной документации — AI-разбор читает саму документацию",
        "cap_documents_text_off": "Текст конкурсной документации (AI-разбор — только по тексту объявления)",
        "cap_cpv": "Коды CPV — подбор тендеров по классификатору",
        "cap_original_link": "Ссылка на объявление на сайте площадки",
        "cap_awards": "Уведомление о выбранном победителе",
        "cap_dedup": "Та же закупка, опубликованная и на: {names}, — помечается как дубль",
        "cap_lang_label": "Язык объявлений",
        "cap_lang_sr": "сербский, кириллица",
        "cap_lang_en_original": "чаще английский, иногда язык заказчика",
        "cap_lang_bhs": "боснийский, хорватский или сербский",
        "cap_s_deadline": "срок",
        "cap_s_deadline_if_stated": "срок, если указан",
        "cap_s_amount": "сумма",
        "cap_s_amount_if_stated": "сумма, если указана",
        "cap_s_notice_description": "описание",
        "cap_s_notice_description_if_stated": "описание, если указано",
        "cap_s_documents_text": "документация для AI",
        "cap_s_cpv": "CPV",
        "cap_s_awards": "победитель",
        "cap_more": "Подробнее об источнике",
        "country_RS": "Сербия",
        "country_ME": "Черногория",
        "country_BA": "Босния и Герцеговина",
        "country_MK": "Северная Македония",
        "country_AL": "Албания",
        "country_XK": "Косово",
        "country_HR": "Хорватия",
        "country_SI": "Словения",
        "country_BG": "Болгария",
        "country_RO": "Румыния",
        "country_HU": "Венгрия",
        "country_GR": "Греция",
        "plan_business": "Business",
        "plan_enterprise": "Enterprise",
        "no_title": "Без названия",
        "no_profile_name": "(без имени)",
        "found_by_template": "Найдено по шаблону",
        "why_matched": "Совпало",
        "cap_counter": "Сегодня отправлено {sent} из {cap} по шаблону «{name}»",
        "cap_notice": "Дневной лимит {cap} тендеров по шаблону «{name}» достигнут. Остальные сегодняшние — на сайте: {feed_url}. Сузить шаблон: {settings_url}",
        "cap_notice_subject": "Дневной лимит тендеров достигнут",
        "cap_help_line": "До {cap} тендеров в день на шаблон в Telegram и на почту; на сайте — все.",
        "source": "Источник",
        "lbl_region": "Регион",
        "lbl_type": "Тип",
        "lbl_procedure": "Процедура",
        "lbl_cpv": "CPV",
        "lbl_published": "Опубликован",
        "lbl_plan": "План",
        "lbl_deadline": "Приём до",
        "lbl_platform": "Площадка",
        "deadline_hour_unknown": "Час закрытия источник не указывает",
        "also_on": "Тот же тендер есть и на других площадках: {n}",
        "also_on_probable": "Вероятно, тот же тендер есть и на других площадках: {n}",
        "amount_estimated": "(оценочно)",
        "ct_goods": "Поставка",
        "ct_services": "Услуги",
        "ct_works": "Работы",
        "pt_open": "Открытая процедура",
        "pt_restricted": "Ограниченная процедура",
        "pt_negotiated_with_call": "Переговорная (с публикацией)",
        "pt_negotiated_without_call": "Переговорная (без публикации)",
        "pt_framework": "Рамочное соглашение",
        "pt_dps": "Динамическая система закупок",
        "pt_competitive_dialogue": "Конкурентный диалог",
        "pt_innovation_partnership": "Партнёрство для инноваций",
        "pt_design_contest": "Конкурс на проект",
        "pt_qualification": "Квалификационная процедура",
        "pt_small_value": "Закупка малой стоимости",
        "pt_pin": "Предварительное информационное уведомление (PIN)",
        "btn_deep": "🧠 Глубокий анализ",
        "btn_bid": "🎯 Стоит ли?",
        "btn_checklist": "📋 Что подготовить?",
        "btn_draft": "📝 Черновик заявки",
        "btn_customer_analytics": "🏛 Кто выигрывает?",
        "btn_configure_profile": "⚙️ Настроить этот шаблон",
        "btn_open_cabinet": "📂 Открыть в ЛК",
        "btn_follow": "⭐ Следить",
        "btn_hide": "✖ Скрыть",
        "btn_ai_menu": "🧠 AI-разбор ▾",
        "btn_back": "↩ Назад",
        "btn_restore": "↩ Вернуть",
        "card_hidden": "скрыто",
        "debug_match": "Отладка совпадения",
        "debug_keywords": "ключевые слова",
        "debug_cpv": "CPV",
        "sum_title": "Ежедневная сводка",
        "sum_active": "Участвуем",
        "sum_watching": "Изучаем",
        "sum_submitted": "Подано",
        "sum_won": "Выиграно",
        "sum_deadlines": "Ближайшие дедлайны",
        "sum_no_deadlines": "нет ближайших дедлайнов",
        "rem_today": "Напоминание: дедлайн СЕГОДНЯ",
        "rem_in_days": "Напоминание: до дедлайна {days}",
        "sub_expiring": "Доступ к TenderSRB заканчивается через {days} ({date}).",
        "sub_renew": "Для продления обратитесь к {contact}.",
        "sub_expired": (
            "Доступ к TenderSRB закончился {date}. "
            "Доставка новых тендеров остановлена."
        ),
        "sub_expired_renew": (
            "Кабинет остаётся открытым: запросите продление рассылки до 30 дней (решение принимает оператор) на app.nab.rs/app или обратитесь к {contact}."
        ),
        "aw_new_win": "новая победа",
        "aw_customer": "Заказчик",
        "tc_title": "Тендер изменён",
        "tc_deadline": "Срок подачи: {old} → {new}",
        "tc_amount": "Сумма: {old} → {new}",
        "tc_files_changed": "Изменены файлы: {names}",
        "tc_files_added": "Добавлены файлы: {names}",
        "tc_files_removed": "Удалены файлы: {names}",
        "tc_note": "Комментарий заказчика: {text}",
        "tc_ai_stale": "Прежний AI-разбор устарел — закажите новый на карточке.",
    },
    "en": {
        "status_new": "🆕 New",
        "status_watching": "👀 Reviewing",
        "status_active": "✅ Participating",
        "status_submitted": "📨 Bid submitted",
        "status_won": "🏆 Won",
        "status_lost": "😔 Lost",
        "status_skipped": "❌ Skipped",
        "plan_free": "Free",
        "plan_trial": "Trial",
        "plan_pro": "Pro",
        # Sources (SRC-0b): the mark for a source disabled by the admin and the country labels —
        # shared by the cabinet form and the bot (/izvori, /profile).
        "src_paused": "temporarily disabled",
        "cap_deadline": "Bid submission deadline",
        "cap_deadline_if_stated": "Bid submission deadline — if readable in the invitation (scanned invitations have none)",
        "cap_amount": "Estimated contract value",
        "cap_amount_if_stated": "Estimated value — if the buyer states it",
        "cap_notice_description": "Description of the subject in the notice itself",
        "cap_notice_description_if_stated": "Description of the subject from the notice — if the buyer states it (often just repeats the title)",
        "cap_documents_text": "Tender documentation text — AI analysis reads the documents themselves",
        "cap_documents_text_off": "Tender documentation text (AI analysis uses the notice text only)",
        "cap_cpv": "CPV codes — tender matching by classifier",
        "cap_original_link": "Link to the notice on the source website",
        "cap_awards": "Notice of the selected bidder",
        "cap_dedup": "The same tender also published on: {names} — marked as a duplicate",
        "cap_lang_label": "Notice language",
        "cap_lang_sr": "Serbian, Cyrillic script",
        "cap_lang_en_original": "mostly English, sometimes the buyer's language",
        "cap_lang_bhs": "Bosnian, Croatian or Serbian",
        "cap_s_deadline": "deadline",
        "cap_s_deadline_if_stated": "deadline if stated",
        "cap_s_amount": "value",
        "cap_s_amount_if_stated": "value if stated",
        "cap_s_notice_description": "description",
        "cap_s_notice_description_if_stated": "description if stated",
        "cap_s_documents_text": "documents for AI",
        "cap_s_cpv": "CPV",
        "cap_s_awards": "selected bidder",
        "cap_more": "More about this source",
        "country_RS": "Serbia",
        "country_ME": "Montenegro",
        "country_BA": "Bosnia and Herzegovina",
        "country_MK": "North Macedonia",
        "country_AL": "Albania",
        "country_XK": "Kosovo",
        "country_HR": "Croatia",
        "country_SI": "Slovenia",
        "country_BG": "Bulgaria",
        "country_RO": "Romania",
        "country_HU": "Hungary",
        "country_GR": "Greece",
        "plan_business": "Business",
        "plan_enterprise": "Enterprise",
        "no_title": "Untitled",
        "no_profile_name": "(no name)",
        "found_by_template": "Matched by template",
        "why_matched": "Matched",
        "cap_counter": "Sent today: {sent} of {cap} for template “{name}”",
        "cap_notice": "The daily limit of {cap} tenders for template “{name}” has been reached. The rest of today’s are on the website: {feed_url}. Narrow the template: {settings_url}",
        "cap_notice_subject": "Daily tender limit reached",
        "cap_help_line": "Up to {cap} tenders a day per template in Telegram and by email; on the website — all of them.",
        "source": "Source",
        "lbl_region": "Region",
        "lbl_type": "Type",
        "lbl_procedure": "Procedure",
        "lbl_cpv": "CPV",
        "lbl_published": "Published",
        "lbl_plan": "Plan",
        "lbl_deadline": "Deadline",
        "lbl_platform": "Platform",
        "deadline_hour_unknown": "Closing hour is not stated by the source",
        "also_on": "The same tender is also on other platforms: {n}",
        "also_on_probable": "Probably the same tender is also on other platforms: {n}",
        "amount_estimated": "(estimated)",
        "ct_goods": "Goods",
        "ct_services": "Services",
        "ct_works": "Works",
        "pt_open": "Open procedure",
        "pt_restricted": "Restricted procedure",
        "pt_negotiated_with_call": "Negotiated (with call)",
        "pt_negotiated_without_call": "Negotiated (without call)",
        "pt_framework": "Framework agreement",
        "pt_dps": "Dynamic purchasing system",
        "pt_competitive_dialogue": "Competitive dialogue",
        "pt_innovation_partnership": "Innovation partnership",
        "pt_design_contest": "Design contest",
        "pt_qualification": "Qualification procedure",
        "pt_small_value": "Small-value procurement",
        "pt_pin": "Prior information notice",
        "btn_deep": "🧠 Deep analysis",
        "btn_bid": "🎯 Worth bidding?",
        "btn_checklist": "📋 What to prepare?",
        "btn_draft": "📝 Bid draft",
        "btn_customer_analytics": "🏛 Who wins?",
        "btn_configure_profile": "⚙️ Configure this template",
        "btn_open_cabinet": "📂 Open in cabinet",
        "btn_follow": "⭐ Follow",
        "btn_hide": "✖ Hide",
        "btn_ai_menu": "🧠 AI tools ▾",
        "btn_back": "↩ Back",
        "btn_restore": "↩ Restore",
        "card_hidden": "hidden",
        "debug_match": "Match debug",
        "debug_keywords": "keywords",
        "debug_cpv": "CPV",
        "sum_title": "Daily summary",
        "sum_active": "Participating",
        "sum_watching": "Reviewing",
        "sum_submitted": "Submitted",
        "sum_won": "Won",
        "sum_deadlines": "Upcoming deadlines",
        "sum_no_deadlines": "no upcoming deadlines",
        "rem_today": "Reminder: deadline is TODAY",
        "rem_in_days": "Reminder: {days} until the deadline",
        "sub_expiring": "Your TenderSRB access expires in {days} ({date}).",
        "sub_renew": "To renew, contact {contact}.",
        "sub_expired": (
            "Your TenderSRB access expired on {date}. "
            "Delivery of new tenders has been stopped."
        ),
        "sub_expired_renew": (
            "Your cabinet stays open: request an extension of delivery by up to 30 days (the operator decides) at app.nab.rs/app or contact {contact}."
        ),
        "aw_new_win": "new win",
        "aw_customer": "Customer",
        "tc_title": "Tender updated",
        "tc_deadline": "Bid deadline: {old} → {new}",
        "tc_amount": "Amount: {old} → {new}",
        "tc_files_changed": "Changed files: {names}",
        "tc_files_added": "Added files: {names}",
        "tc_files_removed": "Removed files: {names}",
        "tc_note": "Customer's note: {text}",
        "tc_ai_stale": "The previous AI analysis is out of date — request a new one on the card.",
    },
}

# Keys IDENTICAL in name to the e-mail catalog (:data:`src.i18n_email.EMAIL`): one and the same
# tender card, two channels. The list is a predicate for the drift guard
# ``tests/test_i18n_common.py::test_card_labels_match_email_catalog``.
CARD_LABEL_KEYS: tuple[str, ...] = (
    "no_title",
    "no_profile_name",
    "found_by_template",
    # The label “Why this tender is for you”; the values match in both catalogs.
    "why_matched",
    # Daily delivery limit (src.domain.delivery_cap): the counter line and the summary.
    "cap_counter",
    "cap_notice",
    "cap_notice_subject",
    "cap_help_line",
    "source",
    "lbl_region",
    "lbl_type",
    "lbl_procedure",
    "lbl_cpv",
    "lbl_published",
    "lbl_plan",
    "lbl_deadline",
    "lbl_platform",
    # Deadline without an hour (CFKU source): date + mark; the same in e-mail and Telegram.
    "deadline_hour_unknown",
    # Mark of a cross-source duplicate (src.dedup.also_on), “{n}” — the number of sources.
    "also_on",
    "also_on_probable",
)

# The same rule as for :data:`CARD_LABEL_KEYS`, but for BACKGROUND notifications: one
# and the same notification goes to the subscriber through TWO channels — the subscription expiry is sent
# by both Telegram and e-mail (``src.workers.subscription_reminders``), the win alert is
# also sent through both (``src.workers.award_watch_notifier``). The channels take the language from DIFFERENT
# sources (channel ↔ cabinet is a normal divergence), but the wording ITSELF
# must be one: otherwise one subscriber reads about “access ending” in the
# bot and something else in the e-mail. The match is held by a guard
# ``tests/test_i18n_common.py::test_notify_labels_match_email_catalog``.
#
# Emoji prefixes (⏰/🔔/🏛) are NOT part of the keys: they are row markup and live in
# the worker code — as well as the delivery card row labels (see the module docstring).
NOTIFY_LABEL_KEYS: tuple[str, ...] = (
    "sub_expiring",
    "sub_renew",
    "sub_expired",
    "sub_expired_renew",
    "aw_new_win",
    "aw_customer",
    "tc_title",
    "tc_deadline",
    "tc_amount",
    "tc_files_changed",
    "tc_files_added",
    "tc_files_removed",
    "tc_note",
    "tc_ai_stale",
)


def common_t(locale: str | None, key: str) -> str:
    """Shared bot↔cabinet string by interface language; unknown language → ru."""
    return COMMON[norm_locale(locale)][key]


def country_label(locale: str | None, code: str) -> str:
    """Label of a source’s country (``sources.country_codes``) by interface language.

    The fallback is EXPLICIT: a country not in the catalog → the ISO code itself (a source of a new country must not break the form/bot; a code is more honest than an empty value). The catalog — ``country_XX``.
    """
    code = str(code or "").strip().upper()
    return COMMON[norm_locale(locale)].get(f"country_{code}", code)
