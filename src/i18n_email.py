"""Catalog of the e-mail chrome (sr/ru/en) — a SINGLE source for two surfaces.

The e-mails are sent by THREE different layers of the project:

- the tender delivery worker (:mod:`src.workers.adapters.email` +
  :mod:`src.workers.adapters.base`) — subject prefixes, the card body, the digest;
- the cabinet (:mod:`src.api.cabinet.auth_service`) — transactional e-mails
  (registration confirmation, the sign-in magic link);
- background notification workers (:mod:`src.workers.subscription_reminders` —
  subscription expiry, :mod:`src.workers.award_watch_notifier` — the new-win alert
  for a watched PIB).

The workers do NOT import ``src.api.cabinet`` (layering: the cabinet pulls in starlette/
FastAPI, workers do not need that and it creates an api↔workers coupling). Therefore the e-mail strings
live here — in a NEUTRAL module without external dependencies, which BOTH the workers
and the cabinet import. Duplicating the catalog is not allowed: otherwise the two
e-mail surfaces would diverge (CLAUDE.md — “shared vocabulary in one catalog”).

The two language AXES must not be mixed (CLAUDE.md): here ONLY the language of the e-mail chrome
(the recipient’s ``users.locale``). Tender data (title, description,
customer) and links are NOT translated — they are passed “as is”.

Parity of the keys across all three languages is held by ``tests/test_i18n_email.py``. The canon of the
ru strings matches the earlier literals (the e-mails were historically ru only),
so the existing delivery/auth tests stay green without changes to the expectations.
"""

from __future__ import annotations

SUPPORTED: tuple[str, ...] = ("sr", "ru", "en")
DEFAULT = "sr"  # default language (users.locale = NULL → sr, owner decision 30.09.2026)


def norm_locale(locale: str | None) -> str:
    """Normalises a language code to a supported one; unknown/None → ``sr``."""
    return locale if locale in SUPPORTED else DEFAULT


# --------------------------------------------------------------------------- #
# Catalog of the e-mail chrome. Keys are identical in all languages (parity under the test).
# ru — the canon of the existing literals (do not change without a synchronous test update).
# --------------------------------------------------------------------------- #
EMAIL: dict[str, dict[str, str]] = {
    "ru": {
        # --- delivery: tender card by e-mail ---
        "greeting": "Здравствуйте!",
        "intro_single": "Найден тендер, подходящий под ваш шаблон поиска.",
        "subject_new": "Новый тендер",
        "no_title": "Без названия",
        "open_card": "Открыть карточку тендера",
        "source": "Источник",
        "footer_actions": (
            "Отметить участие, пропустить тендер или заказать AI-разбор "
            "можно на карточке."
        ),
        "footer_actions_digest": (
            "Отметить участие, пропустить тендер или заказать AI-разбор "
            "можно на карточке тендера."
        ),
        "found_by_template": "Найдено по шаблону",
        "why_matched": "Совпало",
        "cap_counter": "Сегодня отправлено {sent} из {cap} по шаблону «{name}»",
        "cap_notice": "Дневной лимит {cap} тендеров по шаблону «{name}» достигнут. Остальные сегодняшние — на сайте: {feed_url}. Сузить шаблон: {settings_url}",
        "cap_notice_subject": "Дневной лимит тендеров достигнут",
        "cap_help_line": "До {cap} тендеров в день на шаблон в Telegram и на почту; на сайте — все.",
        "no_profile_name": "(без имени)",
        # --- card row labels ---
        "lbl_number": "№ тендера",
        "lbl_region": "Регион",
        "lbl_type": "Тип",
        "lbl_procedure": "Процедура",
        "lbl_cpv": "CPV",
        "lbl_amount": "Сумма",
        "lbl_published": "Опубликован",
        "lbl_plan": "План",
        "lbl_deadline": "Приём до",
        "lbl_platform": "Площадка",
        "deadline_hour_unknown": "Час закрытия источник не указывает",
        "also_on": "Тот же тендер есть и на других площадках: {n}",
        "also_on_probable": "Вероятно, тот же тендер есть и на других площадках: {n}",
        # --- digest ---
        "digest_prefix": "Дайджест",
        "digest_heading_prefix": "📬 Дайджест",
        # --- transactional (cabinet) ---
        "verify_subject": "Подтвердите регистрацию — TenderSRB",
        "verify_body": (
            "Здравствуйте!\n\nПодтвердите email, чтобы активировать пробный "
            "период:\n{link}\n\nСсылка действует 24 часа."
        ),
        "rereg_approved_subject": "Повторная регистрация одобрена — TenderSRB",
        "rereg_approved_body": (
            "Здравствуйте!\n\nВаш запрос на повторную регистрацию одобрен. "
            "Зарегистрируйтесь по ссылке:\n{link}\n\nАккаунт будет новым, "
            "без прежних данных."
        ),
        "rereg_rejected_subject": "Повторная регистрация — решение — TenderSRB",
        "rereg_rejected_body": (
            "Здравствуйте!\n\nПо вашему запросу на повторную регистрацию принято "
            "решение: отказать."
        ),
        "channel_verify_subject": "Подтвердите адрес для писем — TenderSRB",
        "channel_verify_body": (
            "Здравствуйте!\n\nЭтот адрес указали в личном кабинете Tender Bot"
            " Serbia, чтобы получать на него тендеры. Подтвердите его — иначе"
            " письма приходить не будут:\n{link}\n\nСсылка действует {hours} ч."
            " и открывается в том браузере, где вы вошли в кабинет.\n\nЕсли вы"
            " этого не запрашивали — просто удалите письмо: без подтверждения мы"
            " на этот адрес ничего не шлём."
        ),
        "account_deleted_subject": "Аккаунт TenderSRB удалён",
        "account_deleted_body": (
            "Здравствуйте!\n\nВаш аккаунт TenderSRB удалён: доступ закрыт, "
            "Telegram и email отвязаны, персональные данные удалены. Мы храним "
            "только обезличенную запись об удалении и хеш адреса (один пробный "
            "период на адрес).\n\nЕсли это были не вы — напишите нам в ответ на "
            "это письмо."
        ),
        "magic_subject": "Вход в TenderSRB",
        "magic_body": (
            "Здравствуйте!\n\nВойдите по ссылке (действует 15 минут):\n{link}\n\n"
            "Если вы не запрашивали вход — проигнорируйте это письмо."
        ),
        # --- subscription expiry (subscription_reminders; mirror of COMMON) ---
        "sub_subject": "TenderSRB — доступ заканчивается через {days}",
        "sub_expiring": "Доступ к TenderSRB заканчивается через {days} ({date}).",
        "sub_renew": "Для продления обратитесь к {contact}.",
        # --- access has already expired (subscription_reminders, EXPIRED_THRESHOLD) ---
        "sub_expired_subject": "TenderSRB — доступ приостановлен",
        "sub_expired": (
            "Доступ к TenderSRB закончился {date}. "
            "Доставка новых тендеров остановлена."
        ),
        "sub_expired_renew": (
            "Кабинет остаётся открытым: запросите продление рассылки до 30 дней (решение принимает оператор) на app.nab.rs/app или обратитесь к {contact}."
        ),
        # --- new-win alert (award_watch_notifier) ---
        "aw_subject": "Новая победа: {winner} (PIB {pib})",
        "aw_new_win": "новая победа",
        "aw_intro": "Компания, за которой вы следите, выиграла тендер:",
        "aw_customer": "Заказчик",
        "tc_title": "Тендер изменён",
        "tc_deadline": "Срок подачи: {old} → {new}",
        "tc_amount": "Сумма: {old} → {new}",
        "tc_files_changed": "Изменены файлы: {names}",
        "tc_files_added": "Добавлены файлы: {names}",
        "tc_files_removed": "Удалены файлы: {names}",
        "tc_note": "Комментарий заказчика: {text}",
        "tc_ai_stale": "Прежний AI-разбор устарел — закажите новый на карточке.",
        "tc_subject": "Тендер изменён: {title}",
        "tc_intro": "В тендере, который вы получали, есть изменения:",
        "aw_tender": "Тендер",
        "aw_decision_date": "Дата решения",
        "aw_footer": "Кто ещё выигрывает у этого заказчика — на карточке.",
        # --- template exists, no channel (channel_dead_notify, nudge) ---
        "nch_subject": "TenderSRB — подключите канал, чтобы получать тендеры",
        "nch_body": (
            "Здравствуйте!\n\n"
            "Вы создали шаблон поиска, но канал доставки пока не подключён — "
            "поэтому подходящие тендеры вам не отправляются.\n\n"
            "Это занимает минуту: откройте «Мой кабинет» → «Каналы» и подключите "
            "Telegram (бот @nabrs_bot) или e-mail:\n"
            "{url}\n\n"
            "Если что-то не получится — просто ответьте на это письмо, поможем.\n"
        ),
        # --- delivery channel dropped (channel_dead_notify) ---
        "dch_subject": "TenderSRB — тендеры не приходят: {label} отключён",
        "dch_body": (
            "Здравствуйте!\n\n"
            "Мы заметили, что подходящие тендеры вам сейчас не приходят: "
            "канал доставки {label} отключился. Так бывает, если бот заблокирован "
            "или привязка была отменена — с вашим шаблоном поиска и подпиской всё "
            "в порядке, мы продолжаем искать тендеры для вас.\n\n"
            "Чтобы снова получать их, переподключите {label} за минуту:\n"
            "{url}\n\n"
            "Если что-то не получится — просто ответьте на это письмо, поможем.\n"
        ),
        # --- payment lifecycle e-mails (billing_notifications) ---
        "bn_subject_activated": "TenderSRB — оплата прошла, подписка активна",
        "bn_subject_renewed": "TenderSRB — подписка продлена",
        "bn_subject_past_due": "TenderSRB — платёж не прошёл",
        "bn_subject_canceled": "TenderSRB — подписка отменена",
        "bn_subject_manual_approved": "TenderSRB — заявка одобрена, доступ открыт",
        "bn_subject_manual_rejected": "TenderSRB — по заявке принято решение",
        "bn_subject_default": "TenderSRB — статус подписки",
        "bn_until": " до {date}",
        "bn_comment": "Комментарий: {text}",
        "bn_footer": (
            "Ваш личный кабинет: {url}\n"
            "Если что-то не так — просто ответьте на это письмо, поможем.\n"
        ),
        "bn_what_plan_until": "подписка {plan} активна — доступ открыт{until}",
        "bn_what_plan": "подписка {plan} активна",
        "bn_what_trial_until": "тестовый доступ открыт{until}",
        "bn_what_trial": "тестовый доступ открыт",
        "bn_body_manual_approved": (
            "Здравствуйте!\n\n"
            "Ваша заявка одобрена: {what}. Мы продолжаем искать подходящие "
            "тендеры и присылать их вам."
        ),
        "bn_body_manual_rejected": (
            "Здравствуйте!\n\n"
            "По вашей заявке принято решение: к сожалению, сейчас мы не можем её "
            "одобрить."
        ),
        "bn_body_activated": (
            "Здравствуйте!\n\n"
            "Оплата прошла — подписка на TenderSRB активна. Мы продолжаем искать "
            "подходящие тендеры и присылать их вам."
        ),
        "bn_body_renewed": (
            "Здравствуйте!\n\n"
            "Спасибо — подписка на TenderSRB продлена. Доступ и поиск тендеров "
            "продолжаются без перерыва."
        ),
        "bn_body_past_due": (
            "Здравствуйте!\n\n"
            "Мы не смогли списать оплату за подписку TenderSRB. Не переживайте: "
            "доступ сохраняется{until} — за это время обновите способ оплаты в "
            "личном кабинете, и подписка продолжится без перерыва."
        ),
        "bn_body_canceled": (
            "Здравствуйте!\n\n"
            "Подписка на TenderSRB отменена. Доступ к сервису сохраняется{until} "
            "— до этой даты вы продолжаете получать тендеры. Захотите вернуться — "
            "оформить подписку снова можно в личном кабинете."
        ),
        "bn_body_default": (
            "Здравствуйте!\n\n"
            "Статус вашей подписки на TenderSRB изменился. Подробности — в личном "
            "кабинете."
        ),
    },
    "sr": {
        "greeting": "Zdravo!",
        "intro_single": "Pronađen je tender koji odgovara vašem šablonu pretrage.",
        "subject_new": "Novi tender",
        "no_title": "Bez naziva",
        "open_card": "Otvori karticu tendera",
        "source": "Izvor",
        "footer_actions": (
            "Označite učešće, preskočite tender ili zatražite AI-analizu " "na kartici."
        ),
        "footer_actions_digest": (
            "Označite učešće, preskočite tender ili zatražite AI-analizu "
            "na kartici tendera."
        ),
        "found_by_template": "Pronađeno po šablonu",
        "why_matched": "Podudaranje",
        "cap_counter": "Danas poslato {sent} od {cap} po šablonu „{name}“",
        "cap_notice": "Dnevni limit od {cap} tendera po šablonu „{name}“ je dostignut. Ostali današnji tenderi su na sajtu: {feed_url}. Suzite šablon: {settings_url}",
        "cap_notice_subject": "Dnevni limit tendera je dostignut",
        "cap_help_line": "Do {cap} tendera dnevno po šablonu u Telegramu i na e-pošti; na sajtu — svi.",
        "no_profile_name": "(bez naziva)",
        "lbl_number": "Br. tendera",
        "lbl_region": "Region",
        "lbl_type": "Vrsta",
        "lbl_procedure": "Postupak",
        "lbl_cpv": "CPV",
        "lbl_amount": "Iznos",
        "lbl_published": "Objavljen",
        "lbl_plan": "Plan",
        "lbl_deadline": "Rok do",
        "lbl_platform": "Platforma",
        "deadline_hour_unknown": "Čas zatvaranja izvor ne navodi",
        "also_on": "Isti tender je i na drugim platformama: {n}",
        "also_on_probable": "Verovatno isti tender je i na drugim platformama: {n}",
        "digest_prefix": "Sažetak",
        "digest_heading_prefix": "📬 Sažetak",
        "verify_subject": "Potvrdite registraciju — TenderSRB",
        "verify_body": (
            "Zdravo!\n\nPotvrdite email da biste aktivirali probni period:\n"
            "{link}\n\nLink važi 24 sata."
        ),
        "rereg_approved_subject": "Ponovna registracija je odobrena — TenderSRB",
        "rereg_approved_body": (
            "Zdravo!\n\nVaš zahtev za ponovnu registraciju je odobren. "
            "Registrujte se putem linka:\n{link}\n\nNalog će biti nov, "
            "bez ranijih podataka."
        ),
        "rereg_rejected_subject": "Ponovna registracija — odluka — TenderSRB",
        "rereg_rejected_body": (
            "Zdravo!\n\nPo vašem zahtevu za ponovnu registraciju doneta je "
            "odluka: odbijeno."
        ),
        "channel_verify_subject": "Potvrdite adresu za e-poštu — TenderSRB",
        "channel_verify_body": (
            "Zdravo!\n\nOva adresa je uneta u nalogu TenderSRB radi"
            " primanja tendera. Potvrdite je — u suprotnom e-pošta neće"
            " stizati:\n{link}\n\nLink važi {hours} h i otvara se u pregledaču"
            " u kom ste prijavljeni.\n\nAko ovo niste tražili — obrišite poruku:"
            " bez potvrde na ovu adresu ništa ne šaljemo."
        ),
        "account_deleted_subject": "Nalog TenderSRB je obrisan",
        "account_deleted_body": (
            "Zdravo!\n\nVaš nalog TenderSRB je obrisan: pristup je zatvoren, "
            "Telegram i e-pošta su odvezani, lični podaci su uklonjeni. Čuvamo "
            "samo anonimni zapis o brisanju i heš adrese (jedan probni period "
            "po adresi).\n\nAko to niste bili vi — odgovorite na ovu poruku."
        ),
        "magic_subject": "Prijava na TenderSRB",
        "magic_body": (
            "Zdravo!\n\nPrijavite se putem linka (važi 15 minuta):\n{link}\n\n"
            "Ako niste tražili prijavu — zanemarite ovu poruku."
        ),
        "sub_subject": "TenderSRB — pristup ističe za {days}",
        "sub_expiring": "Pristup TenderSRB-u ističe za {days} ({date}).",
        "sub_renew": "Za produženje se obratite na {contact}.",
        "sub_expired_subject": "TenderSRB — pristup je obustavljen",
        "sub_expired": (
            "Pristup TenderSRB-u je istekao {date}. "
            "Isporuka novih tendera je zaustavljena."
        ),
        "sub_expired_renew": (
            "Kabinet ostaje otvoren: zatražite produženje slanja do 30 dana (odluku donosi operater) na app.nab.rs/app ili se obratite na {contact}."
        ),
        "aw_subject": "Nova pobeda: {winner} (PIB {pib})",
        "aw_new_win": "nova pobeda",
        "aw_intro": "Kompanija koju pratite pobedila je na tenderu:",
        "aw_customer": "Naručilac",
        "tc_title": "Tender je izmenjen",
        "tc_deadline": "Rok za podnošenje ponuda: {old} → {new}",
        "tc_amount": "Vrednost: {old} → {new}",
        "tc_files_changed": "Izmenjeni fajlovi: {names}",
        "tc_files_added": "Dodati fajlovi: {names}",
        "tc_files_removed": "Uklonjeni fajlovi: {names}",
        "tc_note": "Napomena naručioca: {text}",
        "tc_ai_stale": "Prethodna AI analiza je zastarela — zatražite novu na kartici.",
        "tc_subject": "Tender je izmenjen: {title}",
        "tc_intro": "U tenderu koji ste dobili došlo je do izmena:",
        "aw_tender": "Tender",
        "aw_decision_date": "Datum odluke",
        "aw_footer": "Ko još pobeđuje kod ovog naručioca — na kartici.",
        "nch_subject": "TenderSRB — povežite kanal da biste primali tendere",
        "nch_body": (
            "Zdravo!\n\n"
            "Napravili ste šablon pretrage, ali kanal isporuke još nije povezan — "
            "zato vam odgovarajući tenderi ne stižu.\n\n"
            "Treba vam samo minut: otvorite „Moj kabinet“ → „Kanali“ i povežite "
            "Telegram (bot @nabrs_bot) ili e-poštu:\n"
            "{url}\n\n"
            "Ako nešto ne uspe — samo odgovorite na ovo pismo, pomoći ćemo.\n"
        ),
        "dch_subject": "TenderSRB — tenderi ne stižu: {label} je isključen",
        "dch_body": (
            "Zdravo!\n\n"
            "Primetili smo da vam odgovarajući tenderi trenutno ne stižu: "
            "kanal isporuke {label} se isključio. To se dešava ako je bot blokiran "
            "ili je povezivanje poništeno — sa vašim šablonom pretrage i pretplatom "
            "je sve u redu, i dalje tražimo tendere za vas.\n\n"
            "Da biste ih ponovo primali, ponovo povežite {label} za minut:\n"
            "{url}\n\n"
            "Ako nešto ne uspe — samo odgovorite na ovo pismo, pomoći ćemo.\n"
        ),
        "bn_subject_activated": "TenderSRB — uplata je prošla, pretplata je aktivna",
        "bn_subject_renewed": "TenderSRB — pretplata je produžena",
        "bn_subject_past_due": "TenderSRB — plaćanje nije prošlo",
        "bn_subject_canceled": "TenderSRB — pretplata je otkazana",
        "bn_subject_manual_approved": "TenderSRB — zahtev je odobren, pristup je otvoren",
        "bn_subject_manual_rejected": "TenderSRB — doneta je odluka po zahtevu",
        "bn_subject_default": "TenderSRB — status pretplate",
        "bn_until": " do {date}",
        "bn_comment": "Komentar: {text}",
        "bn_footer": (
            "Vaš lični kabinet: {url}\n"
            "Ako nešto nije u redu — samo odgovorite na ovo pismo, pomoći ćemo.\n"
        ),
        "bn_what_plan_until": "pretplata {plan} je aktivna — pristup je otvoren{until}",
        "bn_what_plan": "pretplata {plan} je aktivna",
        "bn_what_trial_until": "probni pristup je otvoren{until}",
        "bn_what_trial": "probni pristup je otvoren",
        "bn_body_manual_approved": (
            "Zdravo!\n\n"
            "Vaš zahtev je odobren: {what}. I dalje tražimo odgovarajuće "
            "tendere i šaljemo vam ih."
        ),
        "bn_body_manual_rejected": (
            "Zdravo!\n\n"
            "Po vašem zahtevu je doneta odluka: nažalost, trenutno ne možemo da ga "
            "odobrimo."
        ),
        "bn_body_activated": (
            "Zdravo!\n\n"
            "Uplata je prošla — pretplata na TenderSRB je aktivna. I dalje tražimo "
            "odgovarajuće tendere i šaljemo vam ih."
        ),
        "bn_body_renewed": (
            "Zdravo!\n\n"
            "Hvala — pretplata na TenderSRB je produžena. Pristup i pretraga "
            "tendera se nastavljaju bez prekida."
        ),
        "bn_body_past_due": (
            "Zdravo!\n\n"
            "Nismo uspeli da naplatimo pretplatu na TenderSRB. Bez brige: "
            "pristup se čuva{until} — za to vreme ažurirajte način plaćanja u "
            "kabinetu i pretplata će se nastaviti bez prekida."
        ),
        "bn_body_canceled": (
            "Zdravo!\n\n"
            "Pretplata na TenderSRB je otkazana. Pristup servisu se čuva{until} "
            "— do tog datuma i dalje primate tendere. Ako poželite da se vratite — "
            "pretplatu možete ponovo da zaključite u kabinetu."
        ),
        "bn_body_default": (
            "Zdravo!\n\n"
            "Status vaše pretplate na TenderSRB se promenio. Detalji su u "
            "kabinetu."
        ),
    },
    "en": {
        "greeting": "Hello!",
        "intro_single": "We found a tender matching your search template.",
        "subject_new": "New tender",
        "no_title": "Untitled",
        "open_card": "Open tender card",
        "source": "Source",
        "footer_actions": (
            "You can mark participation, skip the tender or request an "
            "AI analysis on the card."
        ),
        "footer_actions_digest": (
            "You can mark participation, skip the tender or request an "
            "AI analysis on the tender card."
        ),
        "found_by_template": "Matched by template",
        "why_matched": "Matched",
        "cap_counter": "Sent today: {sent} of {cap} for template “{name}”",
        "cap_notice": "The daily limit of {cap} tenders for template “{name}” has been reached. The rest of today’s are on the website: {feed_url}. Narrow the template: {settings_url}",
        "cap_notice_subject": "Daily tender limit reached",
        "cap_help_line": "Up to {cap} tenders a day per template in Telegram and by email; on the website — all of them.",
        "no_profile_name": "(no name)",
        "lbl_number": "Tender no.",
        "lbl_region": "Region",
        "lbl_type": "Type",
        "lbl_procedure": "Procedure",
        "lbl_cpv": "CPV",
        "lbl_amount": "Amount",
        "lbl_published": "Published",
        "lbl_plan": "Plan",
        "lbl_deadline": "Deadline",
        "lbl_platform": "Platform",
        "deadline_hour_unknown": "Closing hour is not stated by the source",
        "also_on": "The same tender is also on other platforms: {n}",
        "also_on_probable": "Probably the same tender is also on other platforms: {n}",
        "digest_prefix": "Digest",
        "digest_heading_prefix": "📬 Digest",
        "verify_subject": "Confirm your registration — TenderSRB",
        "verify_body": (
            "Hello!\n\nConfirm your email to activate the trial period:\n{link}"
            "\n\nThe link is valid for 24 hours."
        ),
        "rereg_approved_subject": "Re-registration approved — TenderSRB",
        "rereg_approved_body": (
            "Hello!\n\nYour re-registration request has been approved. "
            "Sign up using this link:\n{link}\n\nThe account will be new, "
            "without any previous data."
        ),
        "rereg_rejected_subject": "Re-registration — decision — TenderSRB",
        "rereg_rejected_body": (
            "Hello!\n\nA decision has been made on your re-registration "
            "request: declined."
        ),
        "channel_verify_subject": "Confirm your e-mail address — TenderSRB",
        "channel_verify_body": (
            "Hello!\n\nThis address was entered in your TenderSRB"
            " account to receive tenders. Please confirm it — otherwise no"
            " e-mail will be delivered:\n{link}\n\nThe link is valid for"
            " {hours} h and opens in the browser where you are signed in.\n\n"
            "If you did not request this, just delete the message: without"
            " confirmation we send nothing to this address."
        ),
        "account_deleted_subject": "Your TenderSRB account was deleted",
        "account_deleted_body": (
            "Hello!\n\nYour TenderSRB account has been deleted: access is closed, "
            "Telegram and email are unlinked, personal data is removed. We keep "
            "only an anonymous deletion record and a hash of the address (one "
            "trial per address).\n\nIf this was not you, reply to this email."
        ),
        "magic_subject": "Sign in to TenderSRB",
        "magic_body": (
            "Hello!\n\nSign in using the link (valid for 15 minutes):\n{link}\n\n"
            "If you didn't request a sign-in, ignore this email."
        ),
        "sub_subject": "TenderSRB — access expires in {days}",
        "sub_expiring": "Your TenderSRB access expires in {days} ({date}).",
        "sub_renew": "To renew, contact {contact}.",
        "sub_expired_subject": "TenderSRB — access suspended",
        "sub_expired": (
            "Your TenderSRB access expired on {date}. "
            "Delivery of new tenders has been stopped."
        ),
        "sub_expired_renew": (
            "Your cabinet stays open: request an extension of delivery by up to 30 days (the operator decides) at app.nab.rs/app or contact {contact}."
        ),
        "aw_subject": "New win: {winner} (PIB {pib})",
        "aw_new_win": "new win",
        "aw_intro": "A company you follow has won a tender:",
        "aw_customer": "Customer",
        "tc_title": "Tender updated",
        "tc_deadline": "Bid deadline: {old} → {new}",
        "tc_amount": "Amount: {old} → {new}",
        "tc_files_changed": "Changed files: {names}",
        "tc_files_added": "Added files: {names}",
        "tc_files_removed": "Removed files: {names}",
        "tc_note": "Customer's note: {text}",
        "tc_ai_stale": "The previous AI analysis is out of date — request a new one on the card.",
        "tc_subject": "Tender updated: {title}",
        "tc_intro": "A tender you received has been changed:",
        "aw_tender": "Tender",
        "aw_decision_date": "Decision date",
        "aw_footer": "See who else wins with this customer on the card.",
        "nch_subject": "TenderSRB — connect a channel to receive tenders",
        "nch_body": (
            "Hello!\n\n"
            "You have created a search template, but no delivery channel is "
            "connected yet — so matching tenders are not being sent to you.\n\n"
            "It takes a minute: open “My cabinet” → “Channels” and connect "
            "Telegram (bot @nabrs_bot) or email:\n"
            "{url}\n\n"
            "If anything goes wrong, just reply to this email and we’ll help.\n"
        ),
        "dch_subject": "TenderSRB — tenders are not arriving: {label} disconnected",
        "dch_body": (
            "Hello!\n\n"
            "We noticed that matching tenders are not reaching you right now: "
            "the {label} delivery channel has disconnected. This happens when the "
            "bot is blocked or the link was cancelled — your search template and "
            "subscription are fine, and we keep searching for tenders for you.\n\n"
            "To receive them again, reconnect {label} in a minute:\n"
            "{url}\n\n"
            "If something goes wrong — just reply to this email and we will help.\n"
        ),
        "bn_subject_activated": "TenderSRB — payment received, subscription active",
        "bn_subject_renewed": "TenderSRB — subscription renewed",
        "bn_subject_past_due": "TenderSRB — payment failed",
        "bn_subject_canceled": "TenderSRB — subscription cancelled",
        "bn_subject_manual_approved": "TenderSRB — request approved, access opened",
        "bn_subject_manual_rejected": "TenderSRB — a decision was made on your request",
        "bn_subject_default": "TenderSRB — subscription status",
        "bn_until": " until {date}",
        "bn_comment": "Comment: {text}",
        "bn_footer": (
            "Your account: {url}\n"
            "If something is wrong — just reply to this email and we will help.\n"
        ),
        "bn_what_plan_until": "the {plan} subscription is active — access is open{until}",
        "bn_what_plan": "the {plan} subscription is active",
        "bn_what_trial_until": "trial access is open{until}",
        "bn_what_trial": "trial access is open",
        "bn_body_manual_approved": (
            "Hello!\n\n"
            "Your request was approved: {what}. We keep searching for matching "
            "tenders and sending them to you."
        ),
        "bn_body_manual_rejected": (
            "Hello!\n\n"
            "A decision was made on your request: unfortunately, we cannot "
            "approve it right now."
        ),
        "bn_body_activated": (
            "Hello!\n\n"
            "Payment received — your TenderSRB subscription is active. We keep "
            "searching for matching tenders and sending them to you."
        ),
        "bn_body_renewed": (
            "Hello!\n\n"
            "Thank you — your TenderSRB subscription was renewed. Access and "
            "tender search continue without interruption."
        ),
        "bn_body_past_due": (
            "Hello!\n\n"
            "We could not charge your TenderSRB subscription. Don't worry: "
            "access is kept{until} — update your payment method in your account "
            "during that time and the subscription will continue uninterrupted."
        ),
        "bn_body_canceled": (
            "Hello!\n\n"
            "Your TenderSRB subscription was cancelled. Access to the service is "
            "kept{until} — you keep receiving tenders until then. If you want to "
            "come back, you can subscribe again in your account."
        ),
        "bn_body_default": (
            "Hello!\n\n"
            "The status of your TenderSRB subscription has changed. Details are in "
            "your account."
        ),
    },
}


def email_t(locale: str | None, key: str) -> str:
    """A line of the e-mail chrome by the recipient’s language; unknown language → ru."""
    return EMAIL[norm_locale(locale)][key]


# --------------------------------------------------------------------------- #
# Welcome e-mail after the e-mail confirmation (owner 16.10.2026).
# sr/en texts — verbatim from the decision; by decision there is NO ru variant: ru and an empty
# language both receive both e-mails in one (sr on top). Therefore the keys live apart from
# EMAIL and are not part of the three-language parity (that one holds only the chrome).
# Placeholders: {name} — the salutation (name or e-mail), {contact_email},
# {tg_url} (without the scheme), {app_url}.
# --------------------------------------------------------------------------- #
WELCOME: dict[str, dict[str, str]] = {
    "sr": {
        "subject": "Dobrodošli na nab.rs — besplatan probni period",
        "body": (
            "Poštovani {name},\n\n"
            "hvala što ste se registrovali na nab.rs.\n\n"
            "Servis je trenutno u probnom periodu.\n"
            "Korišćenje je potpuno besplatno.\n\n"
            "Vaše mišljenje nam je najvažnije.\n"
            "Pišite nam šta ne radi, šta nedostaje i šta biste promenili.\n"
            "Servis dorađujemo upravo na osnovu vaših primedbi i predloga.\n\n"
            "Rado ćemo pomoći i odgovoriti na sva pitanja:\n\n"
            "e-mail: {contact_email}\n"
            "Telegram: {tg_url}\n\n"
            "Prijava u servis: {app_url}\n\n"
            "Srdačan pozdrav,\n"
            "Tim nab.rs"
        ),
    },
    "en": {
        "subject": "Welcome to nab.rs — free trial period",
        "body": (
            "Dear {name},\n\n"
            "thank you for registering on nab.rs.\n\n"
            "The service is currently in a free trial period.\n"
            "Using it is completely free of charge.\n\n"
            "Your opinion matters most to us.\n"
            "Please tell us what does not work, what is missing and what you would change.\n"
            "We are improving the service precisely on the basis of your comments and suggestions.\n\n"
            "We will be happy to help and answer all your questions:\n\n"
            "e-mail: {contact_email}\n"
            "Telegram: {tg_url}\n\n"
            "Log in to the service: {app_url}\n\n"
            "Kind regards,\n"
            "The nab.rs team"
        ),
    },
}

_WELCOME_SEPARATOR = "\n\n— — —\n\n"


def welcome_parts(
    locale: str | None,
    *,
    name: str,
    contact_email: str = "info@nab.rs",
    tg_url: str = "t.me/nabrs_news",
    app_url: str = "https://app.nab.rs",
) -> tuple[str, str, str]:
    """Subject, plain text and simple HTML of the welcome e-mail.

    ``sr`` → Serbian, ``en`` → English, any other (ru, empty, NULL) →
    Serbian + English in one e-mail (sr on top). The values are substituted
    into the HTML escaped: ``name`` — user input.
    """
    values = {
        "name": name,
        "contact_email": contact_email,
        "tg_url": tg_url,
        "app_url": app_url,
    }
    if locale == "sr":
        langs = ["sr"]
    elif locale == "en":
        langs = ["en"]
    else:
        langs = ["sr", "en"]
    subject = " / ".join(WELCOME[lc]["subject"] for lc in langs)
    text = _WELCOME_SEPARATOR.join(WELCOME[lc]["body"].format(**values) for lc in langs)
    html = "<hr>".join(
        _html_paragraphs(WELCOME[lc]["body"].format(**values)) for lc in langs
    )
    return subject, text, html


def _html_paragraphs(body: str) -> str:
    """Paragraphs split by an empty line, line breaks inside a paragraph — ``<br>``; everything is escaped."""
    return "".join(
        "<p>" + "<br>".join(_html_escape(line) for line in para.split("\n")) + "</p>"
        for para in body.split("\n\n")
    )


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------- #
# Declension of “N new tenders” (digest header/subject). Slavic languages (ru/sr)
# have the forms 1 / 2–4 / 5+; en — singular/plural.
# --------------------------------------------------------------------------- #
_DIGEST_PLURAL_FORMS: dict[str, tuple[str, ...]] = {
    "ru": ("новый тендер", "новых тендера", "новых тендеров"),
    "sr": ("novi tender", "nova tendera", "novih tendera"),
    "en": ("new tender", "new tenders"),
}


def digest_count_phrase(count: int, locale: str | None = None) -> str:
    """“3 new tenders” / “2 nova tendera” / “2 new tenders” — by language."""
    lc = norm_locale(locale)
    forms = _DIGEST_PLURAL_FORMS[lc]
    if lc == "en":
        form = forms[0] if count == 1 else forms[1]
        return f"{count} {form}"
    tail = count % 100
    last = count % 10
    if 11 <= tail <= 14:
        form = forms[2]
    elif last == 1:
        form = forms[0]
    elif 2 <= last <= 4:
        form = forms[1]
    else:
        form = forms[2]
    return f"{count} {form}"


# --------------------------------------------------------------------------- #
# “N days” in deadline notifications (deadline reminder, subscription expiry).
# It lives HERE, not in the consumer catalog: the phrase is assembled by BOTH neutral
# catalogs (e-mails from here, Telegram — :mod:`src.i18n_common`), and a second copy
# of the declension rule would diverge from the first — like ``norm_locale``/``SUPPORTED``,
# which ``i18n_common`` also reuses rather than creating its own.
#
# ru — the non-declinable abbreviation “d.” (the canon of the existing e-mails and reminders,
# the period in it doubles as the end of a sentence). sr/en decline by number: Serbian
# has the forms 1 / 2+ (“dan” / “dana”), English — singular and
# plural.
# --------------------------------------------------------------------------- #
_DAYS_FORMS: dict[str, tuple[str, str]] = {
    "ru": ("дн.", "дн."),
    "sr": ("dan", "dana"),
    "en": ("day", "days"),
}


def days_phrase(count: int, locale: str | None = None) -> str:
    """“7 d.” / “7 dana” / “7 days” — the number with the unit declined by language."""
    one, many = _DAYS_FORMS[norm_locale(locale)]
    return f"{count} {one if count == 1 else many}"
