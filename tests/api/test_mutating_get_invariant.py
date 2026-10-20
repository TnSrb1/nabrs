"""Invariant: a mutating GET route must not appear in the app unnoticed.

WHY. The project has no CSRF tokens — protection rests on the session cookie
``SameSite=lax`` + ``HttpOnly`` + ``Secure`` (``src/api/deps.py``) and the CSP
``form-action self``. For POST this works: the browser will not send a lax cookie on a cross-site POST. But ``SameSite=lax`` DOES send the cookie on top-level GET navigation — so a GET route that changes state instantly becomes a CSRF hole: it is enough to lure a logged-in user by a link (``<a>``, redirect, ``<img src>``). Therefore “mutation = POST” here is not a style, but the only thing the protection rests on.

WHY A LIST. Whether a route mutates cannot be determined automatically, so the guard pins the SET of GET routes itself. A new GET appeared — the test goes red, and the author must deliberately enter it here, justifying why it does not change state (or rework it into a POST). Cheap, and it catches exactly what is needed: an unnoticed appearance of a new GET. The other side is useful too: a route deleted or renamed — the list desyncs, and the test will show it.

LEGITIMATE EXCEPTIONS. Three GETs do change state, and this is design, not a defect: ``/auth/magic``, ``/auth/verify`` and ``/cabinet/email/confirm`` arrive as a link from an e-mail and are protected by the token SECRET — the attacker cannot forge a link he does not have. A CSRF hole is when the attacker knows the full URL; here he does not know it. The justification of each GET is in ``_ALLOWED_GET_ROUTES`` below.
"""

from __future__ import annotations

from fastapi import FastAPI

from src.api.main import app
from tests.api._routes import iter_effective_routes

# --------------------------------------------------------------------------- #
# Allowlist: path -> why this GET is not a CSRF hole.
#
# The acceptance rule for a new GET: it either READS (the render of a page/fragment, an export,
# a redirect), or changes the state ONLY by a secret that the attacker cannot forge.
# Everything else is a POST. An empty justification is not accepted (see
# test_every_allowed_get_has_rationale).
# --------------------------------------------------------------------------- #
_ALLOWED_GET_ROUTES: dict[str, str] = {
    # --- Public showcase: a clean render. ?lang= writes a cosmetic language cookie —
    # it is the browser’s own preference, not state on the server;
    # to replace it cross-site = change the victim’s language, there is nothing to steal.
    "/": "лендинг: рендер; ?lang= пишет только косметическую куку языка",
    "/help": "справка: рендер; ?lang= — косметическая кука языка",
    "/izvori": "витрина источников: SELECT из sources; ?lang= — косметическая кука языка",
    "/cpv-kodovi": "справочник CPV: SELECT-агрегаты из кеша geo_cache (1ч); ?lang= — кука языка",
    "/statistika": "публичная статистика: SELECT-агрегаты (кеш 1ч); ?lang= — кука языка",
    "/novosti": "лента новостей: рендер из кода; ставит косметическую куку «прочитано» (news_seen) и куку языка",
    "/novosti/{slug}": "страница новости: рендер из кода; та же косметическая кука news_seen и язык",
    "/legal/{name}": "юр. страницы: рендер; ?lang= — косметическая кука языка",
    "/kontakt": "форма обратной связи: рендер (+ чтение email сессии); запись только POST",
    "/plans": "витрина тарифов: рендер + чтение tier; ?lang= — кука языка",
    "/tender/{tender_id}": "публичная карточка тендера: только SELECT",
    "/tender/{tender_id}.ics": "«В календарь»: SELECT срока тендера, отдача файла; без записи",
    "/tenderi": "хаб посадочных страниц GEO: SELECT-агрегаты (кеш geo_cache)",
    "/tenderi/cpv/{division}": "раздел CPV: SELECT-и (агрегаты кешированы, листинг — прямой)",
    "/tenderi/region/{code}": "раздел региона: SELECT-и (агрегаты кешированы, листинг — прямой)",
    "/healthz": "liveness probe: возвращает константу",
    # --- GEO/SEO (geo_base): static/derived responses, no writes.
    "/robots.txt": "статический текст: список Disallow из реестра роутеров",
    "/sitemap.xml": "sitemap-индекс: SELECT count(*) тендеров, без записи",
    "/sitemap-pages.xml": "sitemap публичных страниц: статический список путей",
    "/sitemap-tenders-{n}.xml": "sitemap-чанк тендеров: только SELECT",
    "/llms.txt": "статический markdown из каталогов i18n/catalog",
    "/favicon.ico": "301 на существующий /static/favicon.svg, без записи",
    "/security.txt": "статический текст RFC 9116 (Contact/Expires/Canonical), без записи",
    "/.well-known/security.txt": "тот же RFC 9116 текст по каноническому пути, без записи",
    # --- Forms: GET draws the form, submission — a paired POST.
    "/signup": "форма регистрации; запись — POST /signup",
    "/login": "форма входа; выпуск magic-link — POST /login",
    "/auth/resend": "форма повторной отправки; отправка письма — POST /auth/resend",
    "/admin/login": "форма входа оператора; сам вход — по magic-link",
    # --- LEGITIMATE mutating GETs: the link from the e-mail, the protection = the token secret.
    "/auth/magic": (
        "ИСКЛЮЧЕНИЕ ПО ДИЗАЙНУ: гасит one-time токен и ставит сессионную куку. "
        "Не CSRF: ссылку нельзя подделать, не зная секрет токена из письма"
    ),
    "/auth/verify": (
        "ИСКЛЮЧЕНИЕ ПО ДИЗАЙНУ: подтверждает email и ставит сессионную куку. "
        "Тот же класс, что /auth/magic — защищён секретом токена из письма"
    ),
    "/cabinet/email/confirm": (
        "ИСКЛЮЧЕНИЕ ПО ДИЗАЙНУ: pending -> active для адреса доставки. "
        "Защита сильнее прочих: секрет токена (привязан к адресу хэшем) И "
        "активная сессия кабинета"
    ),
    # --- Logout: not server state.
    "/logout": (
        "GET только показывает страницу с кнопкой (выход — POST + CSRF-токен "
        "сессии); куку не трогает и ничего не меняет"
    ),
    # --- Cabinet: pages and fragments, all under a session, all on SELECT.
    "/app": "дашборд ЛК: SELECT-и (профили, лента, папки, статус триала)",
    "/app/vesti": "партиал блока Vesti (переключение вкладки без перезагрузки): SELECT + суточный кеш рынка в Redis (как у /app), состояние пользователя не пишет",
    "/app/feed": "веб-архив совпадений: SELECT (пагинация count + LIMIT/OFFSET)",
    "/app/my": "«мои тендеры»: SELECT",
    "/app/tenders/calendar.ics": "«Svi rokovi (.ics)»: SELECT тендеров со статусом в ЛК; без записи, вход — сессия",
    "/app/my/export.csv": "экспорт CSV: SELECT + сборка файла",
    "/app/tenders/{tender_id}": (
        "карточка тендера в ЛК: SELECT. Перевод на лету пишет кеш в Redis — "
        "это кеш отображения, не состояние тенанта"
    ),
    "/app/tenders/{tender_id}/analytics.csv": "экспорт аналитики: SELECT",
    "/app/tenders/{tender_id}/title": (
        "HTMX-фрагмент заголовка карточки (тумблер перевода): SELECT + перевод "
        "на лету. Пишет только кеш отображения в Redis (общий на всех тенантов, "
        "ключ по отпечатку текста) — состояния тенанта не трогает"
    ),
    "/app/tenders/{tender_id}/description": (
        "HTMX-фрагмент описания карточки (перевод фоном, VG-26): SELECT + перевод "
        "чанками на лету. Пишет только кеш отображения в Redis (общий на всех "
        "тенантов, ключ по отпечатку текста) — состояния тенанта не трогает"
    ),
    "/app/tenders/{tender_id}/status/{level}": (
        "HTMX-поллинг статуса генерации: перечитывает артефакты. Генерацию "
        "ставит POST /app/tenders/{tender_id}/generate/{level}, не этот роут"
    ),
    "/profiles/new": "форма нового профиля; создание — POST /profiles",
    "/profiles/new/confirm": (
        "экран подтверждения «что получится» перед созданием: только читает "
        "префилл и считает превью; создание — POST /profiles"
    ),
    "/profiles/{profile_id}/created": (
        "экран результата после создания (POST → 303 сюда): только читает "
        "сохранённый шаблон и считает превью"
    ),
    "/profiles/{profile_id}": "просмотр профиля: SELECT",
    "/profiles/{profile_id}/edit": "форма правки; сохранение — POST /profiles/{id}",
    "/profiles/preview-count": "оценка охвата профиля: SELECT, ничего не пишет",
    "/profiles/customer-suggest": "подсказка заказчиков формы: SELECT, ничего не пишет",
    "/library": "библиотека шаблонов: SELECT публичных шаблонов",
    "/library/{kind}/{key}": "состав шаблона/пакета: SELECT шаблона + превью-счётчик",
    "/cabinet/telegram": "страница привязки: SELECT; выпуск токена — POST",
    "/cabinet/email": "панель email-канала: SELECT",
    "/cabinet/subscription": "страница подписки: SELECT",
    "/cabinet/account": "«Мой аккаунт»: SELECT-и (подписка, профили, каналы); управление шаблоном — POST /profiles/{id}/deactivate|activate|delete",
    "/cabinet/narucilac": "аналитика заказчика: SELECT",
    "/cabinet/narucilac.csv": "экспорт аналитики заказчика: SELECT",
    "/app/analitika": "конструктор отчётов: SELECT (+ локальный statement_timeout)",
    "/app/analitika.xlsx": "выгрузка отчёта в Excel: SELECT + сборка файла",
    # --- Admin: view only, all actions — POST.
    "/admin": "редирект на /admin/users",
    "/admin/": "редирект на /admin/users",
    "/admin/health": "здоровье ingest/delivery: SELECT",
    "/admin/sources": "источники (Izvori): SELECT-и, вкл/выкл — только POST",
    "/admin/users": "список юзеров: SELECT",
    "/admin/users/{uid}": "карточка юзера: SELECT; grant/revoke/set-tier — POST",
    "/admin/templates": "список шаблонов: SELECT",
    "/admin/audit": "журнал аудита: SELECT через SECURITY DEFINER",
    "/admin/moderation": "очередь модерации: SELECT; approve/reject/edit — POST",
    "/admin/requests": "список pending-заявок: SELECT; approve/reject — POST",
}

# Endpoints that change state must be declared as POST. It catches the reverse
# sabotage — not “a GET was added”, but “the mutation was moved from POST to GET”.
_MUTATING_ENDPOINTS: tuple[str, ...] = (
    "/login",  # issuing the magic link + sending the e-mail
    "/signup",  # creating the account
    "/auth/resend",  # resending the e-mail
    "/cabinet/email/link",  # linking the delivery address
    "/cabinet/email/unlink",  # unlinking the delivery address
    "/cabinet/email/resend",  # resending the confirmation e-mail
    "/cabinet/telegram/link",  # issuing the Telegram linking token
    "/cabinet/trial/request",  # trial application
    "/profiles",  # creating a profile
    "/profiles/{profile_id}",  # editing a profile
    "/profiles/{profile_id}/delete",  # deleting a profile
    "/profiles/{profile_id}/propose",  # template publication
    "/plans/upgrade",  # plan change
    "/kontakt",  # feedback form (e-mail to the operators)
    "/app/folders",  # folder creation
    "/app/folders/{folder_id}/delete",  # folder deletion
    "/app/tenders/{tender_id}/note",  # tender note
    "/app/tenders/{tender_id}/watch",  # “follow” a tender
    "/app/tenders/{tender_id}/generate/{level}",  # launch of the AI generation
    "/admin/users/{uid}/grant",  # granting operator rights
    "/admin/users/{uid}/revoke",  # revoking operator rights
    "/admin/users/{uid}/set-tier",  # plan change by hand by the operator
    "/admin/moderation/{template_id}/approve",  # template moderation
    "/admin/moderation/{template_id}/reject",  # template moderation
    "/admin/moderation/{template_id}/edit",  # template moderation
    "/billing/checkout/{plan_key}",  # payment initiation
    "/billing/webhook/{provider}",  # receipt of the provider event
)


def _methods(application: FastAPI, path: str) -> set[str]:
    """All HTTP methods declared for ``path`` (HEAD/OPTIONS are service methods)."""
    declared: set[str] = set()
    for route in iter_effective_routes(application):
        if route.path == path:
            declared |= {m for m in route.methods if m not in ("HEAD", "OPTIONS")}
    return declared


def _collect_get_paths(application: FastAPI) -> set[str]:
    """The paths of all GET routes of the application."""
    return {
        route.path
        for route in iter_effective_routes(application)
        if "GET" in route.methods
    }


def _diff_against_allowlist(
    application: FastAPI, allowed: dict[str, str]
) -> tuple[set[str], set[str]]:
    """(undeclared GETs, absent from the declared ones) — the core of the guard."""
    actual = _collect_get_paths(application)
    return actual - set(allowed), set(allowed) - actual


def test_no_undeclared_get_routes() -> None:
    """Every GET of the application is declared in the allowlist with a justification.

    A red test = a GET appeared that nobody declared safe. Before entering it here, answer: does it change state? If yes — it is a POST (SameSite=lax sends the cookie on a top-level GET, so a mutating GET = CSRF).
    """
    unexpected, missing = _diff_against_allowlist(app, _ALLOWED_GET_ROUTES)

    assert not unexpected, (
        "A new GET route is not declared in _ALLOWED_GET_ROUTES: "
        f"{sorted(unexpected)}. If it changes state — move it to POST "
        "(SameSite=lax sends the cookie on a top-level GET => a mutating GET = CSRF). "
        "If it only reads — enter it here with a justification."
    )
    assert not missing, (
        f"GET routes from _ALLOWED_GET_ROUTES have disappeared from the application: {sorted(missing)}. "
        "Remove them from the list so that it does not diverge from reality."
    )


def test_every_allowed_get_has_rationale() -> None:
    """A justification is mandatory: an empty string = not a deliberate decision."""
    empty = sorted(path for path, why in _ALLOWED_GET_ROUTES.items() if not why.strip())
    assert not empty, f"GET without a justification in _ALLOWED_GET_ROUTES: {empty}"


def test_mutating_endpoints_are_post() -> None:
    """Mutating endpoints are declared as POST — the lax cookie will not travel to them."""
    not_post = sorted(
        path for path in _MUTATING_ENDPOINTS if "POST" not in _methods(app, path)
    )
    assert not not_post, f"Mutating endpoints have lost their POST: {not_post}"


def test_mutating_endpoints_are_not_reachable_by_get() -> None:
    """A mutation must not have a GET twin — except “GET reads, POST writes”.

    Five paths share GET and POST deliberately: GET draws the page/form and writes nothing, POST on the same path submits it (see _ALLOWED_GET_ROUTES). The list is closed: a new GET twin of a mutation will stay red.
    """
    get_reads_post_writes = {
        "/login",  # GET: login form
        "/signup",  # GET: sign-up form
        "/auth/resend",  # GET: resend form
        "/kontakt",  # GET: feedback form, POST: sending the e-mail
        "/profiles/{profile_id}",  # GET: profile view, POST: saving the edit
    }
    with_get = sorted(
        path
        for path in _MUTATING_ENDPOINTS
        if path not in get_reads_post_writes and "GET" in _methods(app, path)
    )
    assert not with_get, (
        f"A mutating endpoint has acquired a GET twin: {with_get}. "
        "This is a CSRF hole: the browser will send the session cookie on a cross-site GET."
    )


def test_guard_catches_new_get_route() -> None:
    """Mutation check of the guard itself: a new GET must make it fall.

    Without it the guard can silently degrade (for example, by ceasing to see the routes) and stay green on any application.
    """
    probe = FastAPI()

    @probe.get("/danger/delete-everything")
    async def _mutating_get() -> dict[str, str]:  # pragma: no cover - not called
        return {"status": "state changed"}

    allowed = {"/some/known/page": "рендер"}
    unexpected, _ = _diff_against_allowlist(probe, allowed)

    assert unexpected == {"/danger/delete-everything"}
