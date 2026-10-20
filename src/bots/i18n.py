"""The catalog of the Telegram bot chrome — sr/ru/en (i18n Phase 2a: the INCOMING handlers).

Historically the bot was ru only: the chrome was hardcoded in :mod:`src.bots.telegram`, and the handlers
did not read the language. Here — a separate dict catalog of the bot’s chrome (we do NOT introduce
Babel) plus :func:`bot_t`. The locale resolver lives in ``telegram.py``
(``resolve_bot_locale``): by the Telegram user → ``user_channels.locale``
(the language of THIS chat, set by the ``/language`` command, 0038) → ``users.locale``
(the language of the cabinet) → ru.

Do not mix the two axes of the language (CLAUDE.md): here ONLY the interface language of the bot’s chrome
(the ``users.locale`` axis, refined by the language of the channel). The body of the AI analysis and the
tender data (title/description) are another axis (``profiles.target_language``) and are NOT translated by this catalog.

The bot’s vocabulary is bot-specific (greeting/help/onboarding/command replies) — the common
vocabulary (funnel statuses, plan names) is NOT here: the common vocabulary is kept by ONE NEUTRAL catalog ``COMMON`` in
:mod:`src.i18n_common` (the cabinet imports it too); the bot handlers take the status/plan from there via ``common_t`` — do NOT duplicate it here.

The parity of the keys of all three languages is kept by ``tests/bots/test_telegram_i18n.py``.
The canon ru matches the former literals of ``telegram.py`` — so that the existing
bot tests stay green without edits of the expectations.

The normalization of the language code is reused from :mod:`src.i18n_email`
(``norm_locale``; the same SUPPORTED sr/ru/en and the default ru) — we do not multiply a second one.
"""

from __future__ import annotations

from src.i18n_common import common_t
from src.i18n_email import DEFAULT, SUPPORTED, norm_locale

__all__ = ["BOT", "DEFAULT", "SUPPORTED", "bot_t", "common_t", "norm_locale"]


# --------------------------------------------------------------------------- #
# The catalog of the bot’s chrome. The keys are the same in all languages (parity under test).
# ru — the canon of the existing literals of telegram.py (do not change without a synchronous
# edit of the bot test expectations).
# --------------------------------------------------------------------------- #
BOT: dict[str, dict[str, str]] = {
    "ru": {
        # --- /start: the entry point (not the onboarding — that is in 2b) ---
        "start_exists": "Аккаунт уже подключён. /profile — посмотреть шаблоны; настраиваются они в кабинете.",
        "help_hint": "\n\n💡 /help — список всех команд.",
        "signup_closed": "👋 Подписка на <b>TenderSRB</b> оформляется на сайте — https://app.nab.rs/signup\n\nУже есть аккаунт? Откройте кабинет → «Каналы» → «Подключить Telegram» (кнопка ниже) — этот чат привяжется к нему.",
        # --- /help: the structured help (VG-31 item 2) ---
        "help": (
            "<b>TenderSRB — что умеет бот</b>\n\n"
            "<b>Шаблон и настройка</b>\n"
            "/start — регистрация и настройка шаблона поиска\n"
            "/profile — показать текущие настройки\n"
            "/reset — перенастроить шаблон заново\n"
            "/izvori — источники тендеров (порталы)\n"
            "/pause — пауза и включение шаблонов\n\n"
            "<b>Тендеры</b>\n"
            "Подходящие тендеры бот присылает автоматически по вашему шаблону.\n"
            "{cap_line}\n"
            "На карточке тендера есть кнопки:\n"
            "🧠 Глубокий анализ — разбор техзадания\n"
            "🎯 Стоит ли? — вердикт, участвовать ли в закупке\n"
            "📋 Что подготовить? — чек-лист документов для заявки\n"
            "📝 Черновик заявки — черновой текст заявки\n"
            "🏛 Кто выигрывает? — аналитика по заказчику и конкурентам "
            "(включая 🔔 Следить за победителем)\n\n"
            "/list — мои тендеры: в работе и последние присланные\n"
            "/watching — ваши подписки «Следить за победителем»\n\n"
            "<b>Аккаунт</b>\n"
            "/status — статус подписки и тарифа\n"
            "/language — язык сообщений бота в этом чате\n\n"
            "/help — эта справка"
        ),
        # --- Onboarding (FSM: language → words → region → summary) ---
        "onb_welcome": (
            "👋 Добро пожаловать в <b>TenderSRB</b> — мониторинг "
            "государственных тендеров Сербии.\n\n"
            "Настроим шаблон поиска. Выберите язык интерфейса:"
        ),
        "onb_lang_chosen": (
            "Язык: {lang}\n\n"
            "Введите ключевые слова через запятую (минимум одно). Например:\n"
            "<i>строительство, ремонт дорог, IT-услуги</i>"
        ),
        "onb_keywords_empty": (
            "Нужно хотя бы одно ключевое слово. Введите их через запятую:"
        ),
        "onb_keywords_saved": "Ключевые слова сохранены. Выберите регион:",
        "onb_summary": (
            "Проверьте настройки шаблона:\n\n"
            "• Язык: {lang}\n"
            "• Ключевые слова: {keywords}\n"
            "• Регион: {region}\n\n"
            "Всё верно?"
        ),
        "onb_confirm_btn": "✅ Подтвердить",
        "onb_restart_btn": "🔄 Начать заново",
        "onb_saved": (
            "✅ Шаблон сохранён! Тендеры будут приходить по вашим критериям.\n\n"
            "/profile — посмотреть настройки, /reset — изменить.\n"
            "Источники (порталы) меняются командой /izvori."
        ),
        "onb_restart": "Начинаем заново. Выберите язык интерфейса:",
        "all_regions": "Все регионы",
        # --- /start: linking a channel from the cabinet (the deep-link bind token) ---
        "bind_ok": "✅ Telegram подключён к вашему аккаунту.",
        "bind_invalid": (
            "Ссылка недействительна или устарела, сгенерируйте новую в кабинете."
        ),
        # --- /profile: the list of the tenant’s profiles ---
        "profile_title": "⚙️ <b>Шаблоны поиска</b>",
        "lbl_account": "Аккаунт",
        "lbl_language": "Язык",
        "lbl_keywords": "Ключевые слова",
        "lbl_region": "Регион",
        "lbl_tier": "Тариф",
        "lbl_sources": "Источники",
        # --- /izvori: the sources of the template (SRC-0b) ---
        "src_menu_title": "🗂 <b>Источники шаблона «{name}»</b>",
        "src_menu_hint": (
            "Нажмите на портал, чтобы включить или выключить его. "
            "Будут приходить только тендеры, опубликованные с момента "
            "включения."
        ),
        "src_pick_profile": "🗂 <b>Источники</b>\n\nВыберите шаблон:",
        "src_back_btn": "⬅ Шаблоны",
        "src_banner_title": "🗂 <b>Новая площадка: {name}</b>",
        "src_banner_body": "Не включена в {missing} из {total} шаблонов. Включим, только когда вы нажмёте кнопку.",
        "src_banner_plan": "План, ещё не тендер: объявление анонсирует закупку, заявки по нему ещё не принимаются.",
        "src_banner_match_all": "Шаблон «все тендеры» — до 30 в день, лишнее не придёт.",
        "src_banner_enable": "Включить во всех шаблонах",
        "src_banner_done": "✅ Включено в шаблонах: {n}",
        "src_banner_failed": "Эта площадка сейчас недоступна для включения.",
        "src_toast_added": "✅ Включено: {name}",
        "src_toast_removed": "Выключено: {name}",
        "src_toast_last": (
            "Нельзя выключить последний источник: шаблон без источника не "
            "получает ни одного тендера. Сначала включите другой портал."
        ),
        "src_toast_stale": "Список источников изменился — отправьте /izvori ещё раз.",
        "profile_not_found": (
            "Шаблон не найден. Отправьте /start, чтобы пройти онбординг."
        ),
        "no_profile_name": "Без названия",
        "profile_state_active": "✅ Активен",
        "profile_state_disabled": "⛔ Отключён",
        "profile_all_tenders": "Все тендеры (без фильтра по теме)",
        "profile_active_section": "Активные — по ним приходят тендеры:",
        "profile_disabled_section": "Отключены — тендеры по ним НЕ приходят:",
        "profile_hidden_count": ("…и ещё {n} скрыто. Все шаблоны — в кабинете {url}"),
        # --- /reset ---
        "reset_intro": "Шаблоны поиска настраиваются в кабинете — там их можно изменить, выключить или добавить новый. Бот их не перезаписывает.",
        # --- /status ---
        "status_no_account": "📊 <b>Статус</b>\n\nЧат не привязан к аккаунту. Если аккаунт уже есть — подключите чат в кабинете (кнопка ниже); нет аккаунта — регистрация на https://app.nab.rs/signup",
        "status_title": "📊 <b>Статус подписки</b>",
        "status_access_unlimited": "• Доступ: бессрочный",
        "status_access_expired": (
            "• Доступ: <b>истёк {date}</b> — кабинет открыт, запросите продление рассылки до 30 дней (решение принимает оператор): app.nab.rs/app"
        ),
        "status_access_days": ("• Доступ: до <b>{date}</b> (осталось {days} дн.)"),
        "status_access_soon": "• Доступ: до <b>{date}</b> (меньше суток)",
        # --- /language: the language of THIS chat (user_channels.locale, 0038) ---
        # The command does not change the language of the cabinet — the wording must say so,
        # otherwise the subscriber expects a language change in the cabinet and in the emails.
        "lang_prompt": (
            "🌐 <b>Язык бота</b>\n\n"
            "Выберите язык сообщений и карточек тендеров в этом чате. "
            "Язык кабинета и писем останется прежним — его меняют на "
            "app.nab.rs."
        ),
        "lang_changed": "✅ Язык бота в этом чате: <b>{lang}</b>",
        "lang_no_account": "Чат не привязан к аккаунту. Подключить: кабинет → «Каналы» → «Подключить Telegram» (кнопка ниже).",
        "lang_unknown": "Не удалось распознать язык. Попробуйте /language ещё раз.",
        # --- “My tenders” /list and /pause (src/bots/manage.py) ---
        "list_title": "📋 <b>Мои тендеры</b>",
        "list_subtitle": "В работе и присланные в Telegram по вашим шаблонам за {days} дн.",
        "list_empty": (
            "📋 <b>Мои тендеры</b>\n\n"
            "Пока пусто: тендеров в работе нет, и за {days} дн. бот ничего не присылал.\n\n"
            "Тендеры приходят автоматически по вашим шаблонам. Нажмите «{follow}» или «{participate}» "
            "на карточке — и тендер появится в этом списке."
        ),
        "list_no_more": "Больше тендеров нет.",
        "list_more_btn": "Ещё ▸",
        "list_prev_btn": "◂ Назад",
        "list_cabinet_btn": "📋 Все в кабинете",
        "list_btn": "📋 Мои тендеры",
        "sources_btn": "🗂 Площадки",
        "pause_btn": "⏸ Пауза",
        "pause_title": "⏸ <b>Пауза рассылки</b>",
        "pause_hint": "Нажмите на шаблон, чтобы приостановить или снова включить отправку тендеров по нему. Пауза действует на все каналы — Telegram и email.",
        "pause_state_on": "работает",
        "pause_state_off": "на паузе",
        "pause_all_btn": "⏸ Пауза всем",
        "resume_all_btn": "▶ Включить все",
        "pause_toast_paused": "⏸ Шаблон на паузе — тендеры по нему не придут ни в Telegram, ни на email.",
        "pause_toast_resumed": "✅ Шаблон снова работает.",
        "pause_toast_limit": "Включить нельзя: на вашем тарифе уже работает максимум шаблонов. Поставьте другой на паузу или повысьте тариф в кабинете.",
        "pause_toast_gone": "Шаблон не найден — откройте /pause заново.",
        "pause_toast_all_off": "⏸ На паузе шаблонов: {n}.",
        "pause_toast_all_on": "✅ Включено шаблонов: {n}.",
        "pause_toast_all_limit": "✅ Включено шаблонов: {n}. Ещё {blocked} не включены — лимит тарифа.",
        # --- /watching ---
        "watching_no_account": "Чат не привязан к аккаунту. Подключить: кабинет → «Каналы» → «Подключить Telegram» (кнопка ниже).",
        "watching_empty": (
            "У вас нет активных подписок.\n"
            "Откройте профиль победителя (🥊) и нажмите 🔔 Следить."
        ),
        "watching_title": "🔔 <b>Ваши подписки на победителей:</b>\n",
        "watching_unsub_btn": "❌ Отписаться от {name}",
        # --- Service (the gates of the card buttons) ---
        "frozen": "Подписка истекла — продлите доступ в кабинете app.nab.rs",
        "toast_start_first": "Чат не привязан: кабинет → Каналы → Подключить Telegram",
        # --- The tender card: the funnel buttons + the “Status: …” line ---
        "act_participate": "✅ Участвую",
        "act_review": "👀 Изучить",
        "act_skip": "❌ Пропустить",
        "act_submitted": "📨 Подал заявку",
        "act_decline": "❌ Отказаться",
        "act_won": "🏆 Выиграли",
        "act_lost": "😔 Проиграли",
        "status_line": "Статус",
        # --- The AI buttons of the card + analytics (Phase 2c): progress, gates, errors ---
        # CHROME (users.locale). The body of the AI analysis/analytics — the content axis, it is NOT here.
        "ai_deep_progress": "⏳ Запускаю глубокий анализ…",
        "ai_bid_progress": "⏳ Оцениваю соответствие шаблону…",
        "ai_full_cabinet": "полный разбор в кабинете",
        "ai_chk_progress": "⏳ Составляю чек-лист документов…",
        "ai_drf_progress": "⏳ Генерирую черновик заявки…",
        "an_cust_progress": "⏳ Считаю аналитику…",
        "an_comp_progress": "⏳ Считаю профиль победителя…",
        "an_repr_progress": "⏳ Считаю ожидаемые перезакупки…",
        "an_wch_progress": "⏳ Оформляю подписку…",
        "an_export_progress": "⏳ Формирую CSV…",
        "ai_bad_tender": "Некорректный идентификатор тендера.",
        "ai_bad_callback": "Некорректный формат callback.",
        "bid_no_profile": "Шаблон не найден — пройдите /start для настройки.",
        "gate_deep": (
            "🧠 <b>Глубокий анализ</b> доступен на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_bid": (
            "🎯 <b>Вердикт «Стоит ли?»</b> доступен на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_chk": (
            "📋 <b>Чек-лист «Что подготовить?»</b> доступен на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_drf": (
            "📝 <b>Черновик заявки</b> доступен на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_cust": (
            "🏛 <b>Аналитика наручиоца</b> доступна на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_comp": (
            "🥊 <b>Профиль победителя</b> доступен на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_repr": (
            "🔮 <b>Ожидаемые перезакупки</b> доступны на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_watch": (
            "🔔 <b>Слежение за победителем</b> доступно на тарифах <b>Pro+</b>.\n"
            "Повысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_export_cust": (
            "📊 <b>Аналитика наручиоца</b> (экспорт CSV) доступна на тарифах "
            "<b>Pro+</b>.\nПовысьте тариф в кабинете на app.nab.rs"
        ),
        "gate_export_comp": (
            "📊 <b>Профиль победителя</b> (экспорт CSV) доступна на тарифах "
            "<b>Pro+</b>.\nПовысьте тариф в кабинете на app.nab.rs"
        ),
        "limit_deep": (
            "Месячный лимит глубоких анализов исчерпан.\n"
            "Лимит сбросится в начале следующего месяца."
        ),
        "limit_ai": (
            "Месячный лимит AI-анализов исчерпан.\n"
            "Лимит сбросится в начале следующего месяца."
        ),
        "limit_draft": (
            "Месячный лимит черновиков исчерпан.\n"
            "Лимит сбросится в начале следующего месяца."
        ),
        "limit_upsell_body": "{plan} — {limit} в месяц, {price} € / мес.",
        "limit_upsell_cta": "Выбрать тариф",
        "model_busy": "Модель сейчас занята. Попробуйте через несколько минут.",
        "err_deep": "Не удалось выполнить глубокий анализ. Попробуйте позже.",
        "err_bid": "Не удалось получить вердикт. Попробуйте позже.",
        "err_chk": "Не удалось составить чек-лист. Попробуйте позже.",
        "err_drf": "Не удалось сгенерировать черновик. Попробуйте позже.",
        "err_cust": "Не удалось получить аналитику. Попробуйте позже.",
        "err_comp": "Не удалось получить профиль победителя. Попробуйте позже.",
        "err_repr": "Не удалось получить прогноз перезакупок. Попробуйте позже.",
        "err_export": "Не удалось сформировать экспорт. Попробуйте позже.",
        "err_watch": "Не удалось оформить подписку. Попробуйте позже.",
        "no_customer": "У этого тендера не указан заказчик — аналитика недоступна.",
        "no_customer_export": (
            "У этого тендера не указан заказчик — экспорт недоступен."
        ),
        "no_data_cust": "По этому заказчику пока нет данных о решениях в нашей базе.",
        "no_data_comp": "По этому тендеру не удалось найти победителя с PIB.",
        "no_data_repr": (
            "Нет данных о сроках контрактов по этому наручиоцу пока.\n"
            "Данные пополняются по мере обновления реестра."
        ),
        "no_data_export": "По этому заказчику пока нет данных для экспорта.",
        "no_data_watch": "Не удалось определить победителя с PIB для этого тендера.",
        "watch_subscribed": (
            "🔔 Подписка оформлена: <b>{name}</b> (PIB {pib}).\n"
            "Уведомлю о новых победах.\n"
            "Управление — /watching"
        ),
        "export_caption": "📊 Экспорт аналитики · CSV",
        "unwatch_bad_id": "Некорректный id подписки.",
        "unwatch_ok": "Отписка выполнена.",
        "unwatch_not_found": "Подписка не найдена.",
        "link_cta": "🔗 Подключить Telegram",
        "reset_cta": "⚙️ Шаблоны в кабинете",
        "card_restore_gone": "Карточку уже не вернуть — откройте тендер в кабинете.",
        "an_btn_12m": "📅 12 мес",
        "an_btn_36m": "📅 36 мес",
        "an_btn_all": "📅 Всё время",
        "an_btn_comp": "🥊 Сильнейший победитель",
        "an_btn_watch": "🔔 Следить за победителем",
        "an_btn_repr": "🔮 Ожидаемые перезакупки",
        "an_btn_export": "📊 Экспорт CSV",
        "trial_notify_plan": "✅ Подписка {plan} активна до {date}.",
        "trial_notify_trial": "✅ Тестовый доступ открыт до {date}.",
        "trial_notify_denied": "❌ Заявка отклонена.",
    },
    "sr": {
        "start_exists": "Nalog je već povezan. /profile — da vidite šablone; podešavaju se u kabinetu.",
        "help_hint": "\n\n💡 /help — spisak svih komandi.",
        "signup_closed": "👋 Pretplata na <b>TenderSRB</b> obavlja se na sajtu — https://app.nab.rs/signup\n\nVeć imate nalog? Otvorite kabinet → „Kanali“ → „Poveži Telegram“ (dugme ispod) — ovaj čet će se povezati sa njim.",
        "help": (
            "<b>TenderSRB — šta bot ume</b>\n\n"
            "<b>Šablon i podešavanje</b>\n"
            "/start — registracija i podešavanje šablona pretrage\n"
            "/profile — prikaži trenutna podešavanja\n"
            "/reset — ponovo podesi šablon\n"
            "/izvori — izvori tendera (portali)\n"
            "/pause — pauza i uključivanje šablona\n\n"
            "<b>Tenderi</b>\n"
            "Odgovarajuće tendere bot šalje automatski prema vašem šablonu.\n"
            "{cap_line}\n"
            "Na kartici tendera postoje dugmad:\n"
            "🧠 Duboka analiza — razrada tehničke specifikacije\n"
            "🎯 Da li vredi? — procena, učestvovati li u nabavci\n"
            "📋 Šta pripremiti? — ček-lista dokumenata za ponudu\n"
            "📝 Nacrt ponude — radni tekst ponude\n"
            "🏛 Ko pobeđuje? — analitika naručioca i konkurenata "
            "(uključujući 🔔 Prati pobednika)\n\n"
            "/list — moji tenderi: u radu i poslednji poslati\n"
            "/watching — vaše pretplate „Prati pobednika“\n\n"
            "<b>Nalog</b>\n"
            "/status — status pretplate i plana\n"
            "/language — jezik poruka bota u ovom četu\n\n"
            "/help — ova pomoć"
        ),
        # --- Onboarding (FSM: jezik → reči → region → sažetak) ---
        "onb_welcome": (
            "👋 Dobrodošli u <b>TenderSRB</b> — praćenje javnih "
            "nabavki u Srbiji.\n\n"
            "Hajde da podesimo šablon pretrage. Izaberite jezik interfejsa:"
        ),
        "onb_lang_chosen": (
            "Jezik: {lang}\n\n"
            "Unesite ključne reči odvojene zarezom (najmanje jednu). Na primer:\n"
            "<i>građevina, popravka puteva, IT-usluge</i>"
        ),
        "onb_keywords_empty": (
            "Potrebna je bar jedna ključna reč. Unesite ih odvojene zarezom:"
        ),
        "onb_keywords_saved": "Ključne reči su sačuvane. Izaberite region:",
        "onb_summary": (
            "Proverite podešavanja šablona:\n\n"
            "• Jezik: {lang}\n"
            "• Ključne reči: {keywords}\n"
            "• Region: {region}\n\n"
            "Je li sve tačno?"
        ),
        "onb_confirm_btn": "✅ Potvrdi",
        "onb_restart_btn": "🔄 Počni iznova",
        "onb_saved": (
            "✅ Šablon je sačuvan! Tenderi će stizati prema vašim kriterijumima.\n\n"
            "/profile — pogledajte podešavanja, /reset — izmenite.\n"
            "Izvore menjate komandom /izvori."
        ),
        "onb_restart": "Počinjemo iznova. Izaberite jezik interfejsa:",
        "all_regions": "Svi regioni",
        # --- /start: povezivanje kanala iz kabineta (deep-link bind-token) ---
        "bind_ok": "✅ Telegram je povezan sa vašim nalogom.",
        "bind_invalid": (
            "Link nije važeći ili je zastareo, generišite novi u kabinetu."
        ),
        # --- /profile: lista profila zakupca ---
        "profile_title": "⚙️ <b>Šabloni pretrage</b>",
        "lbl_account": "Nalog",
        "lbl_language": "Jezik",
        "lbl_keywords": "Ključne reči",
        "lbl_region": "Region",
        "lbl_tier": "Plan",
        "lbl_sources": "Izvori",
        # --- /izvori: izvori šablona (SRC-0b) ---
        "src_menu_title": "🗂 <b>Izvori šablona „{name}“</b>",
        "src_menu_hint": (
            "Dodirnite portal da ga uključite ili isključite. Dobijaćete samo "
            "tendere objavljene od trenutka uključivanja."
        ),
        "src_pick_profile": "🗂 <b>Izvori</b>\n\nIzaberite šablon:",
        "src_back_btn": "⬅ Šabloni",
        "src_banner_title": "🗂 <b>Novi izvor: {name}</b>",
        "src_banner_body": "Nije uključen u {missing} od {total} šablona. Uključujemo ga tek kad kliknete.",
        "src_banner_plan": "Plan, još nije tender: objava najavljuje nabavku, rok za ponude još nije raspisan.",
        "src_banner_match_all": "Šablon „svi tenderi“ — do 30 dnevno, ostalo ne stiže.",
        "src_banner_enable": "Uključi u svim šablonima",
        "src_banner_done": "✅ Uključeno u šablonima: {n}",
        "src_banner_failed": "Ovaj izvor trenutno nije dostupan za uključivanje.",
        "src_toast_added": "✅ Uključeno: {name}",
        "src_toast_removed": "Isključeno: {name}",
        "src_toast_last": (
            "Poslednji izvor ne može da se isključi: šablon bez izvora ne dobija "
            "nijedan tender. Prvo uključite drugi portal."
        ),
        "src_toast_stale": "Spisak izvora se promenio — pošaljite /izvori ponovo.",
        "profile_not_found": (
            "Šablon nije pronađen. Pošaljite /start da prođete kroz podešavanje."
        ),
        "no_profile_name": "Bez naziva",
        "profile_state_active": "✅ Aktivan",
        "profile_state_disabled": "⛔ Isključen",
        "profile_all_tenders": "Svi tenderi (bez filtera po temi)",
        "profile_active_section": "Aktivni — po njima stižu tenderi:",
        "profile_disabled_section": "Isključeni — tenderi po njima NE stižu:",
        "profile_hidden_count": ("…i još {n} skriveno. Svi šabloni — u kabinetu {url}"),
        # --- /reset ---
        "reset_intro": "Šabloni pretrage podešavaju se u kabinetu — tamo ih možete izmeniti, isključiti ili dodati novi. Bot ih ne prepisuje.",
        # --- /status ---
        "status_no_account": "📊 <b>Status</b>\n\nČet nije povezan sa nalogom. Ako nalog već imate — povežite čet u kabinetu (dugme ispod); ako nemate — registracija na https://app.nab.rs/signup",
        "status_title": "📊 <b>Status pretplate</b>",
        "status_access_unlimited": "• Pristup: bez roka",
        "status_access_expired": (
            "• Pristup: <b>istekao {date}</b> — kabinet je otvoren, zatražite produženje slanja do 30 dana (odluku donosi operater): app.nab.rs/app"
        ),
        "status_access_days": ("• Pristup: do <b>{date}</b> (ostalo {days} dana)"),
        "status_access_soon": "• Pristup: do <b>{date}</b> (manje od jednog dana)",
        # --- /language: jezik OVOG četa (user_channels.locale, 0038) ---
        "lang_prompt": (
            "🌐 <b>Jezik bota</b>\n\n"
            "Izaberite jezik poruka i kartica tendera u ovom četu. Jezik "
            "kabineta i imejlova ostaje isti — njega menjate na app.nab.rs."
        ),
        "lang_changed": "✅ Jezik bota u ovom četu: <b>{lang}</b>",
        "lang_no_account": "Čet nije povezan sa nalogom. Povezivanje: kabinet → „Kanali“ → „Poveži Telegram“ (dugme ispod).",
        "lang_unknown": "Jezik nije prepoznat. Pokušajte /language ponovo.",
        # --- /list „Moji tenderi“ i /pause (src/bots/manage.py) ---
        "list_title": "📋 <b>Moji tenderi</b>",
        "list_subtitle": "U radu i poslati na Telegram po vašim šablonima u poslednjih {days} dana",
        "list_empty": (
            "📋 <b>Moji tenderi</b>\n\n"
            "Za sada prazno: nema tendera u radu, a za za {days} dana bot nije ništa poslao.\n\n"
            "Tenderi stižu automatski prema vašim šablonima. Pritisnite „{follow}“ ili „{participate}“ "
            "na kartici — i tender će se pojaviti na ovoj listi."
        ),
        "list_no_more": "Nema više tendera.",
        "list_more_btn": "Još ▸",
        "list_prev_btn": "◂ Nazad",
        "list_cabinet_btn": "📋 Svi u kabinetu",
        "list_btn": "📋 Moji tenderi",
        "sources_btn": "🗂 Izvori",
        "pause_btn": "⏸ Pauza",
        "pause_title": "⏸ <b>Pauza slanja</b>",
        "pause_hint": "Pritisnite šablon da pauzirate ili ponovo uključite slanje tendera po njemu. Pauza važi za sve kanale — Telegram i imejl.",
        "pause_state_on": "radi",
        "pause_state_off": "pauziran",
        "pause_all_btn": "⏸ Pauziraj sve",
        "resume_all_btn": "▶ Uključi sve",
        "pause_toast_paused": "⏸ Šablon je pauziran — tenderi po njemu neće stizati ni na Telegram, ni na imejl.",
        "pause_toast_resumed": "✅ Šablon ponovo radi.",
        "pause_toast_limit": "Uključivanje nije moguće: na vašem planu već radi maksimalan broj šablona. Pauzirajte drugi ili povećajte plan u kabinetu.",
        "pause_toast_gone": "Šablon nije pronađen — otvorite /pause ponovo.",
        "pause_toast_all_off": "⏸ Pauzirano šablona: {n}.",
        "pause_toast_all_on": "✅ Uključeno šablona: {n}.",
        "pause_toast_all_limit": "✅ Uključeno šablona: {n}. Još {blocked} nije uključeno — limit plana.",
        # --- /watching ---
        "watching_no_account": "Čet nije povezan sa nalogom. Povezivanje: kabinet → „Kanali“ → „Poveži Telegram“ (dugme ispod).",
        "watching_empty": (
            "Nemate aktivnih pretplata.\n"
            "Otvorite profil pobednika (🥊) i pritisnite 🔔 Prati."
        ),
        "watching_title": "🔔 <b>Vaše pretplate na pobednike:</b>\n",
        "watching_unsub_btn": "❌ Otkaži praćenje: {name}",
        # --- Servisne (gejtovi dugmadi na kartici) ---
        "frozen": "Pretplata je istekla — produžite pristup u kabinetu app.nab.rs",
        "toast_start_first": "Čet nije povezan: kabinet → Kanali → Poveži Telegram",
        # --- Kartica tendera: dugmad levka + red „Status: …“ ---
        "act_participate": "✅ Učestvujem",
        "act_review": "👀 Istraži",
        "act_skip": "❌ Preskoči",
        "act_submitted": "📨 Podneo ponudu",
        "act_decline": "❌ Odustani",
        "act_won": "🏆 Pobeda",
        "act_lost": "😔 Poraz",
        "status_line": "Status",
        # --- AI-dugmad kartice + analitika (Faza 2c): progres, gejtovi, greške ---
        "ai_deep_progress": "⏳ Pokrećem duboku analizu…",
        "ai_bid_progress": "⏳ Procenjujem usklađenost sa šablonom…",
        "ai_full_cabinet": "kompletna analiza u kabinetu",
        "ai_chk_progress": "⏳ Pravim ček-listu dokumenata…",
        "ai_drf_progress": "⏳ Generišem nacrt ponude…",
        "an_cust_progress": "⏳ Računam analitiku…",
        "an_comp_progress": "⏳ Računam profil pobednika…",
        "an_repr_progress": "⏳ Računam očekivane ponovne nabavke…",
        "an_wch_progress": "⏳ Formiram pretplatu…",
        "an_export_progress": "⏳ Pravim CSV…",
        "ai_bad_tender": "Neispravan identifikator tendera.",
        "ai_bad_callback": "Neispravan format callback-a.",
        "bid_no_profile": "Šablon nije pronađen — pošaljite /start za podešavanje.",
        "gate_deep": (
            "🧠 <b>Duboka analiza</b> dostupna je na planovima <b>Pro+</b>.\n"
            "Povećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_bid": (
            "🎯 <b>Procena „Da li vredi?“</b> dostupna je na planovima <b>Pro+</b>.\n"
            "Povećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_chk": (
            "📋 <b>Ček-lista „Šta pripremiti?“</b> dostupna je na planovima "
            "<b>Pro+</b>.\nPovećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_drf": (
            "📝 <b>Nacrt ponude</b> dostupan je na planovima <b>Pro+</b>.\n"
            "Povećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_cust": (
            "🏛 <b>Analitika naručioca</b> dostupna je na planovima <b>Pro+</b>.\n"
            "Povećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_comp": (
            "🥊 <b>Profil pobednika</b> dostupan je na planovima <b>Pro+</b>.\n"
            "Povećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_repr": (
            "🔮 <b>Očekivane ponovne nabavke</b> dostupne su na planovima "
            "<b>Pro+</b>.\nPovećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_watch": (
            "🔔 <b>Praćenje pobednika</b> dostupno je na planovima <b>Pro+</b>.\n"
            "Povećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_export_cust": (
            "📊 <b>Analitika naručioca</b> (CSV izvoz) dostupna je na planovima "
            "<b>Pro+</b>.\nPovećajte plan u kabinetu na app.nab.rs"
        ),
        "gate_export_comp": (
            "📊 <b>Profil pobednika</b> (CSV izvoz) dostupan je na planovima "
            "<b>Pro+</b>.\nPovećajte plan u kabinetu na app.nab.rs"
        ),
        "limit_deep": (
            "Mesečni limit dubokih analiza je iscrpljen.\n"
            "Limit se obnavlja početkom sledećeg meseca."
        ),
        "limit_ai": (
            "Mesečni limit AI-analiza je iscrpljen.\n"
            "Limit se obnavlja početkom sledećeg meseca."
        ),
        "limit_draft": (
            "Mesečni limit nacrta je iscrpljen.\n"
            "Limit se obnavlja početkom sledećeg meseca."
        ),
        "limit_upsell_body": "{plan} — {limit} mesečno, {price} € / mes.",
        "limit_upsell_cta": "Izaberite plan",
        "model_busy": "Model je trenutno zauzet. Pokušajte za nekoliko minuta.",
        "err_deep": "Duboka analiza nije uspela. Pokušajte kasnije.",
        "err_bid": "Nije uspelo dobijanje procene. Pokušajte kasnije.",
        "err_chk": "Nije uspelo pravljenje ček-liste. Pokušajte kasnije.",
        "err_drf": "Nije uspelo generisanje nacrta. Pokušajte kasnije.",
        "err_cust": "Nije uspelo dobijanje analitike. Pokušajte kasnije.",
        "err_comp": "Nije uspelo dobijanje profila pobednika. Pokušajte kasnije.",
        "err_repr": (
            "Nije uspelo dobijanje prognoze ponovnih nabavki. Pokušajte kasnije."
        ),
        "err_export": "Nije uspelo pravljenje izvoza. Pokušajte kasnije.",
        "err_watch": "Nije uspelo formiranje pretplate. Pokušajte kasnije.",
        "no_customer": (
            "Za ovaj tender nije naveden naručilac — analitika nije dostupna."
        ),
        "no_customer_export": (
            "Za ovaj tender nije naveden naručilac — izvoz nije dostupan."
        ),
        "no_data_cust": (
            "Za ovog naručioca još nema podataka o odlukama u našoj bazi."
        ),
        "no_data_comp": "Za ovaj tender nije pronađen pobednik sa PIB-om.",
        "no_data_repr": (
            "Još nema podataka o rokovima ugovora za ovog naručioca.\n"
            "Podaci se dopunjuju kako se registar ažurira."
        ),
        "no_data_export": "Za ovog naručioca još nema podataka za izvoz.",
        "no_data_watch": "Nije moguće odrediti pobednika sa PIB-om za ovaj tender.",
        "watch_subscribed": (
            "🔔 Pretplata je formirana: <b>{name}</b> (PIB {pib}).\n"
            "Obavestiću vas o novim pobedama.\n"
            "Upravljanje — /watching"
        ),
        "export_caption": "📊 Izvoz analitike · CSV",
        "unwatch_bad_id": "Neispravan id pretplate.",
        "unwatch_ok": "Otkazivanje praćenja je izvršeno.",
        "unwatch_not_found": "Pretplata nije pronađena.",
        "link_cta": "🔗 Poveži Telegram",
        "reset_cta": "⚙️ Šabloni u kabinetu",
        "card_restore_gone": "Karticu više nije moguće vratiti — otvorite tender u kabinetu.",
        "an_btn_12m": "📅 12 mes",
        "an_btn_36m": "📅 36 mes",
        "an_btn_all": "📅 Sve vreme",
        "an_btn_comp": "🥊 Najjači pobednik",
        "an_btn_watch": "🔔 Prati pobednika",
        "an_btn_repr": "🔮 Očekivane ponovne nabavke",
        "an_btn_export": "📊 Izvoz CSV",
        "trial_notify_plan": "✅ Pretplata {plan} je aktivna do {date}.",
        "trial_notify_trial": "✅ Probni pristup je otvoren do {date}.",
        "trial_notify_denied": "❌ Zahtev je odbijen.",
    },
    "en": {
        "start_exists": "Account already linked. /profile shows your templates; they are configured in the cabinet.",
        "help_hint": "\n\n💡 /help — list of all commands.",
        "signup_closed": "👋 A subscription to <b>TenderSRB</b> is set up on the website — https://app.nab.rs/signup\n\nAlready have an account? Open the cabinet → “Channels” → “Connect Telegram” (button below) — this chat will be linked to it.",
        "help": (
            "<b>TenderSRB — what the bot can do</b>\n\n"
            "<b>Template and setup</b>\n"
            "/start — registration and search template setup\n"
            "/profile — show current settings\n"
            "/reset — reconfigure the template\n"
            "/izvori — tender sources (portals)\n"
            "/pause — pause and resume templates\n\n"
            "<b>Tenders</b>\n"
            "The bot sends matching tenders automatically based on your "
            "template.\n"
            "{cap_line}\n"
            "The tender card has buttons:\n"
            "🧠 Deep analysis — breakdown of the specification\n"
            "🎯 Worth it? — verdict on whether to bid\n"
            "📋 What to prepare? — checklist of documents for the bid\n"
            "📝 Bid draft — working draft of the bid\n"
            "🏛 Who wins? — analytics on the buyer and competitors "
            "(including 🔔 Watch the winner)\n\n"
            "/list — my tenders: in progress and recently sent\n"
            "/watching — your „Watch the winner“ subscriptions\n\n"
            "<b>Account</b>\n"
            "/status — subscription and plan status\n"
            "/language — language of the bot messages in this chat\n\n"
            "/help — this help"
        ),
        # --- Onboarding (FSM: language → keywords → region → summary) ---
        "onb_welcome": (
            "👋 Welcome to <b>TenderSRB</b> — monitoring of public "
            "tenders in Serbia.\n\n"
            "Let's set up your search template. Choose the interface language:"
        ),
        "onb_lang_chosen": (
            "Language: {lang}\n\n"
            "Enter keywords separated by commas (at least one). For example:\n"
            "<i>construction, road repair, IT services</i>"
        ),
        "onb_keywords_empty": (
            "At least one keyword is required. Enter them separated by commas:"
        ),
        "onb_keywords_saved": "Keywords saved. Choose a region:",
        "onb_summary": (
            "Check your template settings:\n\n"
            "• Language: {lang}\n"
            "• Keywords: {keywords}\n"
            "• Region: {region}\n\n"
            "Is everything correct?"
        ),
        "onb_confirm_btn": "✅ Confirm",
        "onb_restart_btn": "🔄 Start over",
        "onb_saved": (
            "✅ Template saved! Tenders will arrive according to your criteria.\n\n"
            "/profile — view settings, /reset — change them.\n"
            "Change sources (portals) with /izvori."
        ),
        "onb_restart": "Starting over. Choose the interface language:",
        "all_regions": "All regions",
        # --- /start: channel binding from the web account (deep-link token) ---
        "bind_ok": "✅ Telegram connected to your account.",
        "bind_invalid": (
            "The link is invalid or expired, generate a new one in your account."
        ),
        # --- /profile: tenant's profile list ---
        "profile_title": "⚙️ <b>Search templates</b>",
        "lbl_account": "Account",
        "lbl_language": "Language",
        "lbl_keywords": "Keywords",
        "lbl_region": "Region",
        "lbl_tier": "Plan",
        "lbl_sources": "Sources",
        # --- /izvori: template sources (SRC-0b) ---
        "src_menu_title": "🗂 <b>Sources of template “{name}”</b>",
        "src_menu_hint": (
            "Tap a portal to turn it on or off. You will receive only tenders "
            "published from the moment you turn it on."
        ),
        "src_pick_profile": "🗂 <b>Sources</b>\n\nChoose a template:",
        "src_back_btn": "⬅ Templates",
        "src_banner_title": "🗂 <b>New source: {name}</b>",
        "src_banner_body": "It is off in {missing} of {total} templates. We turn it on only when you click.",
        "src_banner_plan": "A plan, not a tender yet: the notice announces a purchase; bids are not open yet.",
        "src_banner_match_all": "The “all tenders” template gets up to 30 a day; the rest won't arrive.",
        "src_banner_enable": "Turn on in all templates",
        "src_banner_done": "✅ Turned on in templates: {n}",
        "src_banner_failed": "This source can't be turned on right now.",
        "src_toast_added": "✅ Turned on: {name}",
        "src_toast_removed": "Turned off: {name}",
        "src_toast_last": (
            "The last source cannot be turned off: a template without a source "
            "receives no tenders. Turn on another portal first."
        ),
        "src_toast_stale": "The source list has changed — send /izvori again.",
        "profile_not_found": (
            "Template not found. Send /start to go through onboarding."
        ),
        "no_profile_name": "Unnamed",
        "profile_state_active": "✅ Active",
        "profile_state_disabled": "⛔ Disabled",
        "profile_all_tenders": "All tenders (no topic filter)",
        "profile_active_section": "Active — tenders are coming in for these:",
        "profile_disabled_section": "Disabled — tenders are NOT coming in for these:",
        "profile_hidden_count": ("…and {n} more hidden. See all templates at {url}"),
        # --- /reset ---
        "reset_intro": "Search templates are configured in the cabinet — change, disable or add one there. The bot does not overwrite them.",
        # --- /status ---
        "status_no_account": "📊 <b>Status</b>\n\nThis chat is not linked to an account. If you already have one, link this chat in the cabinet (button below); otherwise sign up at https://app.nab.rs/signup",
        "status_title": "📊 <b>Subscription status</b>",
        "status_access_unlimited": "• Access: unlimited",
        "status_access_expired": (
            "• Access: <b>expired on {date}</b> — the cabinet is open, request an extension of delivery by up to 30 days (the operator decides): app.nab.rs/app"
        ),
        "status_access_days": "• Access: until <b>{date}</b> ({days} days left)",
        "status_access_soon": "• Access: until <b>{date}</b> (less than a day)",
        # --- /language: language of THIS chat (user_channels.locale, 0038) ---
        "lang_prompt": (
            "🌐 <b>Bot language</b>\n\n"
            "Choose the language of messages and tender cards in this chat. "
            "The language of the web cabinet and emails stays the same — "
            "change it at app.nab.rs."
        ),
        "lang_changed": "✅ Bot language in this chat: <b>{lang}</b>",
        "lang_no_account": "This chat is not linked to an account. To link it: cabinet → “Channels” → “Connect Telegram” (button below).",
        "lang_unknown": "Could not recognise the language. Try /language again.",
        # --- /list "My tenders" and /pause (src/bots/manage.py) ---
        "list_title": "📋 <b>My tenders</b>",
        "list_subtitle": "In progress and sent to Telegram for your templates in the last {days} days",
        "list_empty": (
            "📋 <b>My tenders</b>\n\n"
            "Nothing here yet: no tenders in progress, and the bot has sent nothing in {days} days.\n\n"
            "Tenders arrive automatically based on your templates. Tap “{follow}” or “{participate}” "
            "on a card and the tender will appear in this list."
        ),
        "list_no_more": "No more tenders.",
        "list_more_btn": "More ▸",
        "list_prev_btn": "◂ Back",
        "list_cabinet_btn": "📋 All in the cabinet",
        "list_btn": "📋 My tenders",
        "sources_btn": "🗂 Sources",
        "pause_btn": "⏸ Pause",
        "pause_title": "⏸ <b>Pause delivery</b>",
        "pause_hint": "Tap a template to pause or resume sending tenders for it. A pause applies to all channels — Telegram and email.",
        "pause_state_on": "active",
        "pause_state_off": "paused",
        "pause_all_btn": "⏸ Pause all",
        "resume_all_btn": "▶ Resume all",
        "pause_toast_paused": "⏸ Template paused — no tenders will arrive for it, in Telegram or by email.",
        "pause_toast_resumed": "✅ Template is active again.",
        "pause_toast_limit": "Cannot resume: your plan already has the maximum number of active templates. Pause another one or upgrade the plan in the cabinet.",
        "pause_toast_gone": "Template not found — open /pause again.",
        "pause_toast_all_off": "⏸ Templates paused: {n}.",
        "pause_toast_all_on": "✅ Templates resumed: {n}.",
        "pause_toast_all_limit": "✅ Templates resumed: {n}. {blocked} more not resumed — plan limit.",
        # --- /watching ---
        "watching_no_account": "This chat is not linked to an account. To link it: cabinet → “Channels” → “Connect Telegram” (button below).",
        "watching_empty": (
            "You have no active subscriptions.\n"
            "Open a winner's profile (🥊) and press 🔔 Watch."
        ),
        "watching_title": "🔔 <b>Your winner subscriptions:</b>\n",
        "watching_unsub_btn": "❌ Unsubscribe from {name}",
        # --- Service (card button gates) ---
        "frozen": "Subscription expired — renew access at app.nab.rs",
        "toast_start_first": "Chat not linked: cabinet → Channels → Connect Telegram",
        # --- Tender card: funnel buttons + “Status: …” line ---
        "act_participate": "✅ Participate",
        "act_review": "👀 Review",
        "act_skip": "❌ Skip",
        "act_submitted": "📨 Bid submitted",
        "act_decline": "❌ Decline",
        "act_won": "🏆 Won",
        "act_lost": "😔 Lost",
        "status_line": "Status",
        # --- Card AI buttons + analytics (Phase 2c): progress, gates, errors ---
        "ai_deep_progress": "⏳ Starting deep analysis…",
        "ai_bid_progress": "⏳ Evaluating fit with your template…",
        "ai_full_cabinet": "full analysis in the cabinet",
        "ai_chk_progress": "⏳ Compiling the document checklist…",
        "ai_drf_progress": "⏳ Generating the bid draft…",
        "an_cust_progress": "⏳ Calculating analytics…",
        "an_comp_progress": "⏳ Calculating the winner profile…",
        "an_repr_progress": "⏳ Calculating expected re-procurements…",
        "an_wch_progress": "⏳ Setting up the subscription…",
        "an_export_progress": "⏳ Preparing CSV…",
        "ai_bad_tender": "Invalid tender identifier.",
        "ai_bad_callback": "Invalid callback format.",
        "bid_no_profile": "Template not found — send /start to set it up.",
        "gate_deep": (
            "🧠 <b>Deep analysis</b> is available on <b>Pro+</b> plans.\n"
            "Upgrade your plan at app.nab.rs"
        ),
        "gate_bid": (
            "🎯 <b>The „Worth it?“ verdict</b> is available on <b>Pro+</b> plans.\n"
            "Upgrade your plan at app.nab.rs"
        ),
        "gate_chk": (
            "📋 <b>The „What to prepare?“ checklist</b> is available on "
            "<b>Pro+</b> plans.\nUpgrade your plan at app.nab.rs"
        ),
        "gate_drf": (
            "📝 <b>The bid draft</b> is available on <b>Pro+</b> plans.\n"
            "Upgrade your plan at app.nab.rs"
        ),
        "gate_cust": (
            "🏛 <b>Buyer analytics</b> is available on <b>Pro+</b> plans.\n"
            "Upgrade your plan at app.nab.rs"
        ),
        "gate_comp": (
            "🥊 <b>The winner profile</b> is available on <b>Pro+</b> plans.\n"
            "Upgrade your plan at app.nab.rs"
        ),
        "gate_repr": (
            "🔮 <b>Expected re-procurements</b> are available on <b>Pro+</b> "
            "plans.\nUpgrade your plan at app.nab.rs"
        ),
        "gate_watch": (
            "🔔 <b>Watching the winner</b> is available on <b>Pro+</b> plans.\n"
            "Upgrade your plan at app.nab.rs"
        ),
        "gate_export_cust": (
            "📊 <b>Buyer analytics</b> (CSV export) is available on <b>Pro+</b> "
            "plans.\nUpgrade your plan at app.nab.rs"
        ),
        "gate_export_comp": (
            "📊 <b>The winner profile</b> (CSV export) is available on <b>Pro+</b> "
            "plans.\nUpgrade your plan at app.nab.rs"
        ),
        "limit_deep": (
            "Monthly deep-analysis limit reached.\n"
            "The limit resets at the start of next month."
        ),
        "limit_ai": (
            "Monthly AI-analysis limit reached.\n"
            "The limit resets at the start of next month."
        ),
        "limit_draft": (
            "Monthly draft limit reached.\n"
            "The limit resets at the start of next month."
        ),
        "limit_upsell_body": "{plan} — {limit} per month, €{price} / mo.",
        "limit_upsell_cta": "Choose a plan",
        "model_busy": "The model is busy right now. Try again in a few minutes.",
        "err_deep": "Deep analysis failed. Try again later.",
        "err_bid": "Could not get the verdict. Try again later.",
        "err_chk": "Could not compile the checklist. Try again later.",
        "err_drf": "Could not generate the draft. Try again later.",
        "err_cust": "Could not get the analytics. Try again later.",
        "err_comp": "Could not get the winner profile. Try again later.",
        "err_repr": "Could not get the re-procurement forecast. Try again later.",
        "err_export": "Could not build the export. Try again later.",
        "err_watch": "Could not set up the subscription. Try again later.",
        "no_customer": "This tender has no buyer specified — analytics unavailable.",
        "no_customer_export": (
            "This tender has no buyer specified — export unavailable."
        ),
        "no_data_cust": "No decision data for this buyer in our database yet.",
        "no_data_comp": "No winner with a PIB found for this tender.",
        "no_data_repr": (
            "No contract-term data for this buyer yet.\n"
            "Data is added as the registry updates."
        ),
        "no_data_export": "No data to export for this buyer yet.",
        "no_data_watch": "Could not determine a winner with a PIB for this tender.",
        "watch_subscribed": (
            "🔔 Subscription created: <b>{name}</b> (PIB {pib}).\n"
            "I'll notify you about new wins.\n"
            "Manage — /watching"
        ),
        "export_caption": "📊 Analytics export · CSV",
        "unwatch_bad_id": "Invalid subscription id.",
        "unwatch_ok": "Unsubscribed.",
        "unwatch_not_found": "Subscription not found.",
        "link_cta": "🔗 Connect Telegram",
        "reset_cta": "⚙️ Templates in the cabinet",
        "card_restore_gone": "The card can no longer be restored — open the tender in the cabinet.",
        "an_btn_12m": "📅 12 mo",
        "an_btn_36m": "📅 36 mo",
        "an_btn_all": "📅 All time",
        "an_btn_comp": "🥊 Top winner",
        "an_btn_watch": "🔔 Follow the winner",
        "an_btn_repr": "🔮 Expected re-procurements",
        "an_btn_export": "📊 Export CSV",
        "trial_notify_plan": "✅ {plan} subscription is active until {date}.",
        "trial_notify_trial": "✅ Test access is open until {date}.",
        "trial_notify_denied": "❌ Request declined.",
    },
}

# The chrome of the AI wrapper (analysis/verdict/checklist/draft/analytics/CSV) lives in a
# separate module for the sake of the file size, but in ONE catalog ``BOT``.
from src.bots.i18n_ai_render import AI_RENDER  # noqa: E402

for _lang, _extra in AI_RENDER.items():
    _dup = BOT[_lang].keys() & _extra.keys()
    assert not _dup, f"duplicate keys of the bot catalog: {sorted(_dup)}"
    BOT[_lang].update(_extra)


def bot_t(locale: str | None, key: str) -> str:
    """The bot’s chrome line for the interface language; unknown/None → sr (the owner’s decision)."""
    return BOT[norm_locale(locale)][key]
