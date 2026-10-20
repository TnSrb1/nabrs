"""Guards of the source’s credibility (W2): robots · 403/429 · the silent zero · the canary.

All the tests — WITHOUT a network and WITHOUT a DB (fetch/Redis are injected), the snapshot is taken from
`docs/research/snapshots/vg13/` ⇒ they really run in the CI.

‼️ Each guard is checked by a MUTATION (see `docs/research/W2_SOURCE_TRUST_GUARDS.md`
§4): a broken selector, robots with `Disallow: /`, an empty list — the test must
turn red. A guard without a mutation is not proved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.parsers.guards import (
    CountStore,
    GuardKind,
    GuardReporter,
    HttpVerdict,
    LayoutCanary,
    RobotsDisallowed,
    RobotsUnavailable,
    check_http_status,
    check_layout,
    check_robots,
    check_volume,
    classify_status,
    retry_after_seconds,
    robots_allows,
    robots_url,
)

_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "research"
    / "snapshots"
    / "vg13"
    / "mod-nabavke-art27-list.html"
)

_UA = "TenderBotSerbia/1.0 (+https://nab.rs/about)"
_LIST_URL = "http://www.nabavke.src-mod.example.invalid/index.php"


class RecordingReporter(GuardReporter):
    """The reporter-spy: accumulates the verdicts instead of TG (and always logs, as the combat one does)."""

    def __init__(self) -> None:
        super().__init__(alert=None)
        self.events: list[tuple[str, GuardKind, str]] = []

    async def report(self, source_key: str, kind: GuardKind, message: str) -> None:
        await super().report(source_key, kind, message)
        self.events.append((source_key, kind, message))

    @property
    def kinds(self) -> list[GuardKind]:
        return [k for _, k, _ in self.events]


class FakeRedis:
    """A minimal in-memory Redis (get/set with ex) — the state without a server."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str):
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


def _fetch_returning(status: int, text: str = ""):
    async def _fetch(url: str) -> tuple[int, str]:
        return status, text

    return _fetch


# --------------------------------------------------------------------------- #
#  Guard №1 — the robots stop
# --------------------------------------------------------------------------- #
def test_robots_url_is_site_root():
    assert robots_url("http://www.nabavke.src-mod.example.invalid/index.php?a=1") == (
        "http://www.nabavke.src-mod.example.invalid/robots.txt"
    )


@pytest.mark.asyncio
async def test_robots_404_means_allowed():
    """No robots.txt (404) = allowed — a fact of the reconnaissance for nabavke.src-mod.example.invalid."""
    reporter = RecordingReporter()
    await check_robots(
        _fetch_returning(404),
        url=_LIST_URL,
        user_agent=_UA,
        source_key="mod_nabavke",
        reporter=reporter,
    )
    assert reporter.events == []


@pytest.mark.asyncio
async def test_robots_disallow_all_stops_the_pass():
    """‼️ MUTATION: `Disallow: /` is substituted → the pass MUST stop."""
    reporter = RecordingReporter()
    with pytest.raises(RobotsDisallowed):
        await check_robots(
            _fetch_returning(200, "User-agent: *\nDisallow: /\n"),
            url=_LIST_URL,
            user_agent=_UA,
            source_key="mod_nabavke",
            reporter=reporter,
        )
    assert reporter.kinds == [GuardKind.ROBOTS_DISALLOW]


@pytest.mark.asyncio
async def test_robots_disallow_targets_our_ua_by_name():
    """A ban for exactly OUR UA (not `*`) — also a stop. The UA is not masked."""
    robots = (
        "User-agent: TenderBotSerbia\nDisallow: /index.php\n\nUser-agent: *\nAllow: /\n"
    )
    with pytest.raises(RobotsDisallowed):
        await check_robots(
            _fetch_returning(200, robots),
            url=_LIST_URL,
            user_agent=_UA,
            source_key="mod_nabavke",
            reporter=RecordingReporter(),
        )


@pytest.mark.asyncio
async def test_robots_allow_lets_the_pass_through():
    """An allowing robots — the guard is silent and the pass goes on (otherwise the guard is useless)."""
    reporter = RecordingReporter()
    await check_robots(
        _fetch_returning(200, "User-agent: *\nDisallow: /admin/\n"),
        url=_LIST_URL,
        user_agent=_UA,
        source_key="mod_nabavke",
        reporter=reporter,
    )
    assert reporter.events == []


@pytest.mark.asyncio
async def test_robots_5xx_is_fail_closed():
    """5xx on robots = a stop: an unproven permission ≠ a permission (RFC 9309).

    The type — :exc:`RobotsUnavailable` (“we could not ask”), and NOT the bare
    `RobotsDisallowed` (“we were banned”): the events differ, the reaction is common.
    """
    with pytest.raises(RobotsUnavailable):
        await check_robots(
            _fetch_returning(503),
            url=_LIST_URL,
            user_agent=_UA,
            source_key="mod_nabavke",
            reporter=RecordingReporter(),
        )


@pytest.mark.asyncio
async def test_robots_network_error_is_fail_closed():
    """The network dropped on robots — also a stop, not “well, then we’ll go”."""

    async def _boom(url: str) -> tuple[int, str]:
        raise ConnectionError("dns")

    with pytest.raises(RobotsUnavailable):
        await check_robots(
            _boom,
            url=_LIST_URL,
            user_agent=_UA,
            source_key="mod_nabavke",
            reporter=RecordingReporter(),
        )


# --- #600: a 2xx that is NOT robots.txt (a challenge/HTML) — is not a permission -------- #
# A live response of 06.10.2026 from the VPS: `GET http://www.nabavke.src-mod.example.invalid/robots.txt` →
# `200 text/html`, the body — a JS interstitial (verbatim below). Before #600 it went to
# robotparser → there are no rules → “the path is allowed”.
_MOD_ROBOTS_CHALLENGE = (
    "<!DOCTYPE html><html><title></title><script>var y=window</script><script>"
    "var x={o:'/777UeVTc/robots.txt',c:1},z=3;function rsu(h){return(x.o)."
    "substr(0,x.c)+(x.o).substr(x.c+h)}y.location.assign(rsu(z))</script></html>"
)
# The live robots.txt of the enabled/preparing platforms (06.10.2026, `text/plain`).
_UKCK_ROBOTS = (
    "User-agent: *\nDisallow: /wp-admin/\nAllow: /wp-admin/admin-ajax.php\n\n"
    "Sitemap: https://src-ukck.example.invalid/wp-sitemap.xml\n"
)
_JNPORTAL_ROBOTS = "user-agent: *\nAllow: /$\nDisallow: /\n"


def _fetch_with_ctype(status: int, text: str, ctype: str | None):
    async def _fetch(url: str) -> tuple[int, str, str | None]:
        return status, text, ctype

    return _fetch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fetch",
    [
        pytest.param(_fetch_returning(200, _MOD_ROBOTS_CHALLENGE), id="body-no-ctype"),
        pytest.param(
            _fetch_with_ctype(200, _MOD_ROBOTS_CHALLENGE, "text/html"),
            id="mod-live-06.10",
        ),
        pytest.param(
            _fetch_with_ctype(200, "", "text/html; charset=UTF-8"), id="html-ctype"
        ),
    ],
)
async def test_robots_html_or_challenge_is_unavailable_not_allowed(fetch):
    """‼️ A 2xx with HTML/a challenge instead of robots.txt → “we could not ask” (#600).

    A permission “proved” by a page that is not robots.txt is the same unproven permission as a 5xx: fail-closed, :exc:`RobotsUnavailable`.
    """
    reporter = RecordingReporter()
    with pytest.raises(RobotsUnavailable):
        await check_robots(
            fetch,
            url=_LIST_URL,
            user_agent=_UA,
            source_key="mod_nabavke",
            reporter=reporter,
        )
    assert reporter.kinds == [GuardKind.ROBOTS_DISALLOW]


@pytest.mark.asyncio
async def test_live_plain_robots_still_decided_by_content():
    """The control to #600: real robots (live, `text/plain`) are decided by the rules.

    ukck allows the path WP REST; jnportal (`Disallow: /`) forbids — exactly what is written in its file (jnportal check_robots is not called, see REPORT).
    """
    await check_robots(
        _fetch_with_ctype(200, _UKCK_ROBOTS, "text/plain; charset=utf-8"),
        url="https://src-ukck.example.invalid/wp-json/wp/v2/pages/12978",
        user_agent=_UA,
        source_key="ukck_nabavke",
        reporter=RecordingReporter(),
    )
    with pytest.raises(RobotsDisallowed) as info:
        await check_robots(
            _fetch_with_ctype(200, _JNPORTAL_ROBOTS, "text/plain"),
            url="https://jnportal.src-ujn.example.invalid/api/Tender/GetList",
            user_agent=_UA,
            source_key="jnportal",
            reporter=RecordingReporter(),
        )
    assert not isinstance(info.value, RobotsUnavailable)


def test_robots_allow_override_errs_towards_not_going():
    """‼️ The documented divergence of the stdlib from RFC 9309 — pinned by the test.

    `Disallow: /` + `Allow: /index.php`: the RFC requires the longest-match (⇒ “allowed”), while `urllib.robotparser` applies the rules in the order of the file (⇒ “not allowed”).
    The test fixes the REAL behavior, not the desired one: the divergence errs toward “we do not go”, and that is acceptable. It will turn red if the stdlib is fixed or we write our own parser — then the decision will have to be reviewed consciously.
    """
    robots = "User-agent: *\nDisallow: /\nAllow: /index.php\n"
    assert robots_allows(robots, _UA, _LIST_URL) is False


def test_robots_empty_disallow_means_allowed():
    """An empty `Disallow:` = everything is allowed (the corner of the standard, for which robotparser is used)."""
    assert robots_allows("User-agent: *\nDisallow:\n", _UA, _LIST_URL) is True


# --------------------------------------------------------------------------- #
#  Guard №2 — the HTTP class
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, HttpVerdict.OK),
        (204, HttpVerdict.OK),
        (403, HttpVerdict.BAN),
        (429, HttpVerdict.RATE_LIMIT),
        (404, HttpVerdict.NOT_FOUND),
        (500, HttpVerdict.SERVER_ERROR),
        (503, HttpVerdict.SERVER_ERROR),
        (302, HttpVerdict.OTHER),
    ],
)
def test_classify_status(status: int, expected: HttpVerdict):
    """403/429 — SEPARATE classes: they cannot be treated with a retry, as 5xx."""
    assert classify_status(status) is expected


@pytest.mark.asyncio
async def test_403_alerts_as_ban():
    reporter = RecordingReporter()
    verdict = await check_http_status(403, source_key="mod_nabavke", reporter=reporter)
    assert verdict is HttpVerdict.BAN
    assert reporter.kinds == [GuardKind.HTTP_BAN]


@pytest.mark.asyncio
async def test_429_alerts_as_rate_limit_and_reports_backoff():
    reporter = RecordingReporter()
    verdict = await check_http_status(
        429,
        source_key="mod_nabavke",
        reporter=reporter,
        headers={"Retry-After": "120"},
    )
    assert verdict is HttpVerdict.RATE_LIMIT
    assert reporter.kinds == [GuardKind.HTTP_RATE_LIMIT]
    assert "120" in reporter.events[0][2]


@pytest.mark.asyncio
async def test_404_and_5xx_do_not_alert_as_ban():
    """404/5xx — not a ban: otherwise “the site is unwell” would be read as “we were banned”."""
    reporter = RecordingReporter()
    assert (
        await check_http_status(404, source_key="s", reporter=reporter)
        is HttpVerdict.NOT_FOUND
    )
    assert (
        await check_http_status(500, source_key="s", reporter=reporter)
        is HttpVerdict.SERVER_ERROR
    )
    assert reporter.events == []


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "60"}, 60.0),
        ({"Retry-After": "0"}, 300.0),  # 0 → the default: “zero seconds” = hammering
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 300.0),  # HTTP-date
        ({}, 300.0),
        (None, 300.0),
    ],
)
def test_retry_after_seconds(headers, expected):
    assert retry_after_seconds(headers) == expected


# --------------------------------------------------------------------------- #
#  Guard №3 — the silent zero and the volume drop
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_zero_records_is_an_alert_not_a_success():
    """‼️ MUTATION: an empty list is substituted → the “silent zero” MUST alert."""
    reporter = RecordingReporter()
    kind = await check_volume(0, source_key="mod_nabavke", reporter=reporter)
    assert kind is GuardKind.SILENT_ZERO
    assert reporter.kinds == [GuardKind.SILENT_ZERO]


@pytest.mark.asyncio
async def test_zero_alerts_even_without_redis():
    """The silent zero is caught WITHOUT a state — Redis is down, the guard is alive."""
    reporter = RecordingReporter()
    kind = await check_volume(0, source_key="s", reporter=reporter, store=None)
    assert kind is GuardKind.SILENT_ZERO


@pytest.mark.asyncio
async def test_healthy_volume_is_silent_and_remembered():
    reporter = RecordingReporter()
    store = CountStore(FakeRedis())
    kind = await check_volume(
        3350, source_key="mod_nabavke", reporter=reporter, store=store
    )
    assert kind is None
    assert reporter.events == []
    assert await store.get("mod_nabavke") == 3350


@pytest.mark.asyncio
async def test_sharp_volume_drop_alerts():
    """It was 3 350 → became 12: this is how a partly broken selector looks."""
    reporter = RecordingReporter()
    store = CountStore(FakeRedis())
    await check_volume(3350, source_key="mod_nabavke", reporter=reporter, store=store)
    kind = await check_volume(
        12, source_key="mod_nabavke", reporter=reporter, store=store
    )
    assert kind is GuardKind.VOLUME_DROP
    assert reporter.kinds == [GuardKind.VOLUME_DROP]


@pytest.mark.asyncio
async def test_mild_volume_change_is_not_an_anomaly():
    """A fluctuation within the threshold — we stay silent: a guard that always shouts is not read."""
    reporter = RecordingReporter()
    store = CountStore(FakeRedis())
    await check_volume(100, source_key="s", reporter=reporter, store=store)
    assert (
        await check_volume(60, source_key="s", reporter=reporter, store=store) is None
    )
    assert reporter.events == []


@pytest.mark.asyncio
async def test_small_previous_volume_is_not_compared():
    """3 → 1 on a piece feed — not an anomaly (the threshold `_DROP_MIN_PREV`)."""
    reporter = RecordingReporter()
    store = CountStore(FakeRedis())
    await check_volume(3, source_key="s", reporter=reporter, store=store)
    assert await check_volume(1, source_key="s", reporter=reporter, store=store) is None
    assert reporter.events == []


@pytest.mark.asyncio
async def test_incremental_pass_zero_is_legitimate():
    """‼️ A zero in the NARROWED pass (“nothing was published in an hour”) — not an alert.

    Otherwise the guard would shout every hour, people would stop reading it, and it would not fire on the one time it is needed. The increment is covered by the canary.
    """
    reporter = RecordingReporter()
    kind = await check_volume(0, source_key="s", reporter=reporter, full_pass=False)
    assert kind is None
    assert reporter.events == []


@pytest.mark.asyncio
async def test_broken_redis_does_not_break_the_guard():
    """A failure of the guard ≠ a failure of the pass: Redis is down — check_volume does not raise."""

    class DeadRedis:
        async def get(self, key: str):
            raise ConnectionError("redis down")

        async def set(self, key: str, value: str, ex: int | None = None):
            raise ConnectionError("redis down")

    reporter = RecordingReporter()
    store = CountStore(DeadRedis())
    assert (
        await check_volume(100, source_key="s", reporter=reporter, store=store) is None
    )


# --------------------------------------------------------------------------- #
#  Guard №4 — the canary of the markup (on the live snapshot)
# --------------------------------------------------------------------------- #
def _snapshot_html() -> str:
    return _SNAPSHOT.read_text(encoding="utf-8")


def test_snapshot_exists():
    """The snapshot is a live interception; without it the canary tests prove nothing."""
    assert _SNAPSHOT.is_file()


@pytest.mark.asyncio
async def test_canary_silent_on_live_snapshot():
    """On a real page the anchors are in place → the guard is silent."""
    from src.parsers.mod_nabavke.adapter import MOD_LIST_CANARY

    reporter = RecordingReporter()
    missing = await check_layout(
        _snapshot_html(),
        canary=MOD_LIST_CANARY,
        source_key="mod_nabavke",
        reporter=reporter,
    )
    assert missing == []
    assert reporter.events == []


@pytest.mark.asyncio
async def test_canary_names_the_vanished_anchor():
    """‼️ МУТАЦИЯ: из шапки снапшота вырезана колонка `Оглашавач` → сторож краснеет.

    Проверяем не «покраснела», а «назвала ЧТО пропало»: без имени якоря человек
    получает то же «парсер вернул 0», от которого сторож и придуман.
    """
    from src.parsers.mod_nabavke.adapter import MOD_LIST_CANARY

    mutated = _snapshot_html().replace("<br>Оглашавач</td>", "<br>Publisher</td>")
    reporter = RecordingReporter()
    missing = await check_layout(
        mutated,
        canary=MOD_LIST_CANARY,
        source_key="mod_nabavke",
        reporter=reporter,
    )
    assert missing == ["Оглашавач (шапка)"]
    assert reporter.kinds == [GuardKind.LAYOUT_CHANGED]
    assert "Оглашавач" in reporter.events[0][2]


@pytest.mark.asyncio
async def test_canary_anchor_is_structural_not_a_bare_word():
    """‼️ Регресс на реальную ловушку: шапки таблицы НЕТ, но слова на странице есть.

    На живой странице `Оглашавач` встречается ещё и в форме фильтра
    (`<label>Оглашавач</label>`, `<option>Сви оглашавачи</option>`). Якорь по
    голому слову остался бы ЗЕЛЁНЫМ на странице без таблицы результатов — то
    есть соврал бы ровно там, ради чего заведён. Тест краснеет, если якорь
    ослабить до `r"Оглашавач"` (или вернуть `re.I`).
    """
    from src.parsers.mod_nabavke.adapter import MOD_LIST_CANARY

    filter_form_only = (
        "<html><body><form>"
        "<label>Оглашавач</label><br>"
        "<select><option value='0'>Сви оглашавачи</option></select>"
        "<label>Датум</label><label>Назив</label>"
        "</form></body></html>"
    )
    missing = MOD_LIST_CANARY.missing(filter_form_only)
    assert missing == [
        "Датум (шапка)",
        "Назив (шапка)",
        "Оглашавач (шапка)",
        "<span class='total' a='N'>",
    ]


@pytest.mark.asyncio
async def test_canary_catches_dropped_pagination_marker():
    """‼️ MUTATION: the `<span class='total' a='N'>` is broken → the canary names it."""
    from src.parsers.mod_nabavke.adapter import MOD_LIST_CANARY

    mutated = _snapshot_html().replace("class='total'", "class='summary'")
    missing = await check_layout(
        mutated,
        canary=MOD_LIST_CANARY,
        source_key="mod_nabavke",
        reporter=RecordingReporter(),
    )
    assert missing == ["<span class='total' a='N'>"]


def test_canary_on_empty_page_reports_every_anchor():
    """An empty response → all the anchors are gone (the page is absent — this is not “the feed became empty”)."""
    canary = LayoutCanary(anchors=(("a", "AAA"), ("b", "BBB")))
    assert canary.missing("") == ["a", "b"]
    assert canary.missing(None) == ["a", "b"]


# --------------------------------------------------------------------------- #
#  The report with a BLIND Telegram (today’s production reality)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reporter_logs_warning_even_without_telegram(caplog):
    """‼️ The TG keys do not reach the container (RED, owner) ⇒ the guard must live by the log.

    A guard whose only output is Telegram would today be a mute decoration.
    """
    reporter = GuardReporter(alert=None)
    with caplog.at_level("WARNING"):
        await reporter.report("mod_nabavke", GuardKind.SILENT_ZERO, "0 записей")
    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("mod_nabavke" in m and "silent_zero" in m for m in messages)


@pytest.mark.asyncio
async def test_reporter_sends_telegram_when_channel_configured():
    sent: list[str] = []

    async def _alert(text: str) -> None:
        sent.append(text)

    await GuardReporter(alert=_alert).report(
        "mod_nabavke", GuardKind.HTTP_BAN, "HTTP 403"
    )
    assert len(sent) == 1
    assert "mod_nabavke" in sent[0] and "http_ban" in sent[0]


@pytest.mark.asyncio
async def test_failing_telegram_does_not_break_the_guard(caplog):
    """Monitoring does not fail what it monitors: TG fell — the guard ran."""

    async def _boom(text: str) -> None:
        raise RuntimeError("tg down")

    with caplog.at_level("WARNING"):
        await GuardReporter(alert=_boom).report(
            "mod_nabavke", GuardKind.SILENT_ZERO, "0 записей"
        )
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
