"""Source reliability guards: robots · HTTP class · silent zero · canary.

Why the module exists
---------------------
A government site does not “go down” — it **silently changes its markup**, and the parser returns 0 records
AS A SUCCESS. You must monitor not uptime, but the PLAUSIBILITY of the result. The same class
of defect has bitten the project twice: `MockEmailSender` reported success without e-mails;
sections of a contracting authority looked alive while being dead since 2020 (VG-13 reconnaissance:
Srbijašume — last record 14.08.2020, MUP — deadlines up to 28.12.2020).

The guards live in a COMMON layer, not as a copy in each adapter: “the source is silent” and
“we have been banned” mean the same thing for all sources, and diverged copies of the
predicate would quietly diverge in verdicts (the project rules — “one predicate per meaning”).

Reliability contract (‼️ read before editing)
---------------------------------------------
* **A guard failure ≠ a pass failure.** The canary, the silent zero and the HTTP class only
  REPORT; no exception leaves them. Monitoring must not be the cause of the failure of what it monitors.
* **The exception — the robots stop: it MUST stop the pass** (:exc:`RobotsDisallowed`).
  That is its point: the owner’s decision — we respect robots, and on a ban we leave.
  ‼️ Explicit prohibition: do NOT mask the UA, do NOT rotate IPs, do NOT bypass the captcha.
* **The guard must work with a blind Telegram.** :class:`GuardReporter` ALWAYS writes
  ``logger.warning``, and sends the TG alert only if the channel is configured:
  a guard whose only output is Telegram would be a mute decoration on any
  channel failure. ‼️ The earlier note “the keys don’t reach the container, the contour is blind”
  is STALE: `ADMIN_BOT_TOKEN`/`TG_ALERTS_CHAT_ID` are present both in
  `/etc/<app>/secrets.env`, in `HOST_KEYS` (the rollout script), and in the
  `environment:` of the `ingest` service — verified in the container on 03.08.2026, the alert
  is alive. The unconditional log stays for the reason above, not “because TG is silent”.

The state between passes is Redis (`REDIS_URL`), already up in the contour.
We do NOT add a new migration (RED). If Redis is unavailable → the guard degrades HONESTLY:
an absolute zero is still caught, an anomaly drop is not (there is nothing to compare with).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# The volume comparison window: the counter of the previous pass lives 30 days. A source that
# was silent for longer than a month has nothing to compare with — there the question is not about an anomaly, but about
# the source itself (see the sections dead since 2020 in the VG-13 reconnaissance).
_COUNT_TTL_SEC = 30 * 24 * 3600

# Anomaly threshold: a volume drop MORE THAN 70% against the previous pass.
# Rationale for the number: a below-threshold feed is topped up piecemeal, and daily fluctuations of
# ±50% are normal there (VG-13 reconnaissance: 3 350 records over ~6 years ⇒ single units per day) —
# a 50% threshold would produce noise out of nothing. A drop by 3+ times in one pass
# is no longer explained by natural seasonality: that is exactly what a changed
# selector catching part of the table looks like. The threshold is deliberately coarse: the exact boundary of
# “the norm” will be suggested only by a live series, and until the source is connected (owner) there is none.
_DROP_RATIO = 0.3

# Small passes below the threshold are not compared: a drop 3→0 on a piecemeal feed is
# “nothing was published today”, not broken markup. An absolute zero is
# caught SEPARATELY and always (SILENT_ZERO), so there is no hole here.
_DROP_MIN_PREV = 10


# --------------------------------------------------------------------------- #
#  Verdicts
# --------------------------------------------------------------------------- #
class GuardKind(str, Enum):
    """The guard’s event class — goes into the log and into the alert text."""

    ROBOTS_DISALLOW = "robots_disallow"
    HTTP_BAN = "http_ban"  # 403
    HTTP_RATE_LIMIT = "http_rate_limit"  # 429
    SILENT_ZERO = "silent_zero"
    VOLUME_DROP = "volume_drop"
    LAYOUT_CHANGED = "layout_changed"
    # The source API changed its shape/protection (CSRF, response fields, card labels).
    API_CHANGED = "api_changed"


class HttpVerdict(str, Enum):
    """HTTP class of a response. 403/429 — a SEPARATE meaning, not to be confused with 404 and 5xx.

    403/429 = “we are not let in” (a problem of the relationship with the source: a ban, a limit) —
    it is fixed by us LEAVING. 404 = “the page is gone” (the path may have changed), 5xx =
    “the site is unwell” (fixed by a retry later). Lumping them into one “failure”, we would retry
    a ban — that is, hammer the source exactly when it asks us to stop.
    """

    OK = "ok"
    BAN = "ban"  # 403 Forbidden
    RATE_LIMIT = "rate_limit"  # 429 Too Many Requests
    NOT_FOUND = "not_found"  # 404
    SERVER_ERROR = "server_error"  # 5xx
    OTHER = "other"


class RobotsDisallowed(Exception):
    """robots.txt FORBIDS our path ⇒ the pass stops.

    ‼️ The only guard that is allowed to break a pass: passing against
    robots is not a “monitoring bug”, but a violation of the site owner’s prohibition.
    """


class RobotsUnavailable(RobotsDisallowed):
    """robots.txt could not be READ (network/5xx) ⇒ also a stop, but this is NOT a ban.

    A separate type not for beauty: “we were forbidden” and “we could not ask” are
    different events with different reactions. The first is a reason not to return (and to call
    a human), the second is an ordinary network failure, fixed by the next pass.
    Lump them into one type — and an ordinary network break would read in the log as “the site banned us”.
    A subclass of :exc:`RobotsDisallowed`, because the decision is common: WE DO NOT GO.
    """


# --------------------------------------------------------------------------- #
#  1. robots stop (the main guard)
# --------------------------------------------------------------------------- #
def robots_url(base_url: str) -> str:
    """`http://host/any/path` → `http://host/robots.txt` (robots always lives at the root)."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


# Signs that a 2xx response to `/robots.txt` is NOT robots.txt (#600): an HTML
# page or a JS challenge of bot protection. A real robots.txt is plain text of
# directives; there is no markup in it. A live fact 06.10.2026: src-mod.example.invalid at
# `/robots.txt` returns `200 text/html` with `<script>…location.assign(rsu(z))`.
_NOT_ROBOTS_MARKERS = ("<html", "<script", "<!doctype", "<body", "<head")


def looks_like_not_robots(text: str | None, content_type: str | None = None) -> bool:
    """Is a 2xx response to `/robots.txt` actually HTML/a challenge, not robots?

    ``content_type`` — the response header, if the caller passed it; without it
    the body decides. ``text/plain`` with markup markers is not robots either: the header
    of a protection may lie, the body does not.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in ("text/html", "application/xhtml+xml"):
        return True
    body = (text or "").lstrip().lower()
    if body.startswith("<"):
        return True
    return any(marker in body for marker in _NOT_ROBOTS_MARKERS)


def robots_allows(robots_txt: str, user_agent: str, url: str) -> bool:
    """Does robots.txt allow our UA to fetch ``url``?

    Parsing — with the standard :class:`urllib.robotparser.RobotFileParser`, not with our own
    regex: robots rules are full of corners (an empty `Disallow:` = everything allowed,
    User-agent groups, prefixes), and our own implementation would err silently — towards
    “we may”, which here is the worst kind of failure.

    UA matching — as in the standard: `TenderBotSerbia/1.0 (+…)` matches the group
    `User-agent: TenderBotSerbia` and the group `*`.

    ‼️ Known divergence of the stdlib from RFC 9309 (verified, not assumed):
    robotparser applies the rules IN FILE ORDER (the first match), while the
    RFC requires longest-match. On `Disallow: /` + `Allow: /index.php` the stdlib
    says “no”, and Google says “yes”. The divergence is left DELIBERATELY: it errs
    towards “we do not go”, which is exactly the side where it is safe to err. Our own
    implementation for the sake of an extra permission is a bad trade.
    """
    from urllib.robotparser import RobotFileParser

    parser = RobotFileParser()
    parser.parse(robots_txt.splitlines())
    return parser.can_fetch(user_agent, url)


async def check_robots(
    fetch: Callable[[str], Awaitable[tuple[Any, ...]]],
    *,
    url: str,
    user_agent: str,
    source_key: str,
    reporter: "GuardReporter",
) -> None:
    """Guard №1: robots forbids — we brake OURSELVES (:exc:`RobotsDisallowed`).

    ``fetch(robots_url) -> (status, text)`` or ``(status, text, content_type)``
    is injected — the module does not pull the network and is tested without it.

    The semantics of the statuses — per RFC 9309 §2.3.1.3 (Google/the standard robots):
      * **4xx (incl. 404) → allowed.** No robots — no prohibition; that is the case for
        `nabavke.src-mod.example.invalid` (a fact from VG-13 reconnaissance).
      * **2xx → the content decides** — if it is robots.txt. A 2xx with HTML/a
        challenge instead of it (``text/html`` or markup in the body, #600) →
        :exc:`RobotsUnavailable`: a permission “proven” by a non-robots page
        is not proven.
      * **5xx / network dropped → STOP** (:exc:`RobotsUnavailable`, fail-closed).
        We could not prove that we are welcome ⇒ we do not go. The same principle as in W1
        (“no row in ``sources`` = we do not load”): an unproven permission ≠
        a permission. The type is SEPARATE: this is not a ban, but “we could not ask”.
    """
    try:
        status, text, *rest = await fetch(robots_url(url))
    except Exception as exc:  # noqa: BLE001 — the network dropped
        reason = f"robots.txt не прочитан ({type(exc).__name__}) — проход остановлен"
        await reporter.report(source_key, GuardKind.ROBOTS_DISALLOW, reason)
        raise RobotsUnavailable(reason) from exc

    if 400 <= status < 500:
        logger.info(
            "guard[%s] robots.txt: HTTP %s ⇒ no prohibition, we go", source_key, status
        )
        return

    if status >= 500 or status < 200 or status >= 300:
        reason = f"robots.txt отдал HTTP {status} — проход остановлен (fail-closed)"
        await reporter.report(source_key, GuardKind.ROBOTS_DISALLOW, reason)
        raise RobotsUnavailable(reason)

    content_type = rest[0] if rest else None
    if looks_like_not_robots(text, content_type):
        reason = (
            f"robots.txt отдал HTTP {status} ({content_type or 'без Content-Type'}), "
            "но это HTML/челлендж, а не robots — проход остановлен (fail-closed). "
            "Челлендж не обходим."
        )
        await reporter.report(source_key, GuardKind.ROBOTS_DISALLOW, reason)
        raise RobotsUnavailable(reason)

    if not robots_allows(text, user_agent, url):
        reason = (
            f"robots.txt запрещает {url} для UA {user_agent!r} — проход остановлен. "
            f"Запрет уважаем: UA не маскируем, IP не ротируем, капчу не обходим."
        )
        await reporter.report(source_key, GuardKind.ROBOTS_DISALLOW, reason)
        raise RobotsDisallowed(reason)

    logger.info("guard[%s] robots.txt: path allowed", source_key)


# --------------------------------------------------------------------------- #
#  2. HTTP class: 403/429 separately from 404 and 5xx
# --------------------------------------------------------------------------- #
def classify_status(status: int) -> HttpVerdict:
    """HTTP status → :class:`HttpVerdict` (the meaning, not the number)."""
    if 200 <= status < 300:
        return HttpVerdict.OK
    if status == 403:
        return HttpVerdict.BAN
    if status == 429:
        return HttpVerdict.RATE_LIMIT
    if status == 404:
        return HttpVerdict.NOT_FOUND
    if status >= 500:
        return HttpVerdict.SERVER_ERROR
    return HttpVerdict.OTHER


def retry_after_seconds(headers: Any, *, default: float = 300.0) -> float:
    """`Retry-After` → seconds (on 429 — how long NOT to go).

    Only delta-seconds are supported: an HTTP-date in this header is rare,
    and parsing it without the source’s time zone gives negative pauses — when in doubt it is more honest to take
    ``default`` than to compute “0 seconds” and keep hammering. No header / garbage → ``default`` (5 min).
    """
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except Exception:  # noqa: BLE001 — headers can be anything
        return default
    if raw is None:
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def check_http_status(
    status: int,
    *,
    source_key: str,
    reporter: "GuardReporter",
    headers: Any = None,
) -> HttpVerdict:
    """Guard №2: 403/429 → a separate “ban/rate-limit” alert. Does not break the pass.

    Returns a verdict — the decision “whether to leave” is made by the calling adapter
    (on 429 — do not hammer: a pause of ``retry_after_seconds``).
    """
    verdict = classify_status(status)
    if verdict is HttpVerdict.BAN:
        await reporter.report(
            source_key,
            GuardKind.HTTP_BAN,
            "HTTP 403 — источник нас не пускает (бан UA/IP). Не обходим: уходим.",
        )
    elif verdict is HttpVerdict.RATE_LIMIT:
        delay = retry_after_seconds(headers)
        await reporter.report(
            source_key,
            GuardKind.HTTP_RATE_LIMIT,
            f"HTTP 429 — rate-limit. Пауза {delay:.0f}с (Retry-After), не долбим.",
        )
    return verdict


# --------------------------------------------------------------------------- #
#  3. Silent zero + volume anomaly
# --------------------------------------------------------------------------- #
class CountStore:
    """The counter of the previous pass in Redis. All errors → None/no-op (does not break the pass).

    Redis was chosen because it is ALREADY in the contour (`REDIS_URL`, `src/workers/
    routing.py`): a new table would require a migration, and that is RED.
    """

    def __init__(self, redis: Any, *, ttl: int = _COUNT_TTL_SEC) -> None:
        self._redis = redis
        self._ttl = ttl

    @staticmethod
    def key(source_key: str) -> str:
        return f"parsers:guard:last_count:{source_key}"

    async def get(self, source_key: str) -> int | None:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(self.key(source_key))
        except Exception:  # noqa: BLE001 — the guard does not break the pass
            logger.warning("guard[%s]: Redis unavailable for reading", source_key)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def set(self, source_key: str, count: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(self.key(source_key), str(count), ex=self._ttl)
        except Exception:  # noqa: BLE001
            logger.warning("guard[%s]: Redis unavailable for writing", source_key)


async def check_volume(
    count: int,
    *,
    source_key: str,
    reporter: "GuardReporter",
    store: CountStore | None = None,
    full_pass: bool = True,
) -> GuardKind | None:
    """Guard №3: 0 records = an ALERT (not a success) + a sharp-drop anomaly.

    Two different questions:
      * **Silent zero** — “the pass returned 0”. Suspicious: this is exactly what a
        changed selector looks like. It is caught WITHOUT state ⇒ works without Redis too.
      * **Anomaly** — “it was 3 350, now it is 12”. A drop of more than 70% against the
        previous pass (``_DROP_RATIO``) when the previous volume was ≥ ``_DROP_MIN_PREV``
        — the rationale for the thresholds is in the module header.

    ‼️ ``full_pass=False`` (the pass is NARROWED by the “only newer than X” filter) — the guard
    stays silent, and this is not a hole but the condition of its meaningfulness:
      * a zero in an incremental pass is LEGITIMATE (“nothing was published in the last hour”)
        — an alert on it would be noise every hour, and a guard that “always shouts”
        stops being read, and it does not fire in the one time it is needed;
      * comparing volumes of different windows is meaningless: 3 350 (full) → 12 (last hour)
        is not a drop, but a different question.
      The incremental pass is covered by the CANARY: it checks the STRUCTURE
      of the page and does not depend on the window, or on how many rows were in it.

    Returns the class of the triggered guard (or None if the volume is plausible).
    """
    if not full_pass:
        logger.info(
            "guard[%s]: the pass is narrowed by a filter (%s records) — the volume is not compared",
            source_key,
            count,
        )
        return None

    if count == 0:
        await reporter.report(
            source_key,
            GuardKind.SILENT_ZERO,
            "проход вернул 0 записей — это НЕ успех: так выглядит сменившаяся "
            "разметка (или мёртвый раздел). Проверить селекторы и живость фида.",
        )
        return GuardKind.SILENT_ZERO

    prev = await store.get(source_key) if store is not None else None
    if store is not None:
        await store.set(source_key, count)

    if prev is not None and prev >= _DROP_MIN_PREV and count < prev * _DROP_RATIO:
        await reporter.report(
            source_key,
            GuardKind.VOLUME_DROP,
            f"объём прохода упал {prev} → {count} (более чем на "
            f"{(1 - _DROP_RATIO) * 100:.0f}%) — похоже на частично сломанный "
            f"селектор, а не на затишье фида.",
        )
        return GuardKind.VOLUME_DROP
    return None


# --------------------------------------------------------------------------- #
#  4. Markup canary
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LayoutCanary:
    """Structural anchors of the page: gone → “the markup changed”, not “empty”.

    An anchor is a pair “name → regex”. The name goes into the alert so that a person immediately knows
    WHAT exactly disappeared (`Datum`? `<span class='total'>`?), instead of reading “the parser
    returned 0”. Anchors are taken NOT by CSS classes (they are changed at the first redesign),
    but by column captions and the form of the service markup — this is what lives for years.

    ‼️ An anchor MUST be STRUCTURAL (a caption + the cell markup), not a bare
    word, and the check is CASE-SENSITIVE. A lesson from a live MoD snapshot: a bare
    `Oglašavač` under `re.I` matched `<option>Svi oglašavači</option>` in the filter dropdown — that is, the canary
    would stay GREEN even if the site dropped the whole header of the results table. A guard that is green on a broken page is worse than
    no guard: it gives a false “all is well”. This is caught only by a mutation.
    """

    anchors: tuple[tuple[str, str], ...]

    def missing(self, html: str | None) -> list[str]:
        """Names of anchors that are NOT in the html. An empty html → all (the page is gone)."""
        if not html:
            return [name for name, _ in self.anchors]
        return [
            name for name, pattern in self.anchors if not re.search(pattern, html, re.S)
        ]


async def check_layout(
    html: str | None,
    *,
    canary: LayoutCanary,
    source_key: str,
    reporter: "GuardReporter",
) -> list[str]:
    """Guard №4: missing anchors → an alert WITH THE NAME of the anchor. Does not break the pass.

    Returns the list of missing anchors ([] = the markup is in place).
    """
    missing = canary.missing(html)
    if missing:
        await reporter.report(
            source_key,
            GuardKind.LAYOUT_CHANGED,
            f"разметка сменилась — пропали якоря: {', '.join(missing)}. "
            f"Парсер вернёт пустой результат, и это НЕ «фид опустел».",
        )
    return missing


# --------------------------------------------------------------------------- #
#  Report: WARNING always, TG — if the channel is configured
# --------------------------------------------------------------------------- #
class GuardReporter:
    """Where the guard shouts.

    ‼️ ``logger.warning`` — ALWAYS, unconditionally. The TG alert — only if the channel is
    configured, and its failure is swallowed. A guard whose only output is Telegram
    would be a mute decoration on the first channel failure: the log is always readable.
    ‼️ The caveat “the alert contour is blind today, the keys do not arrive” is STALE:
    `ADMIN_BOT_TOKEN`/`TG_ALERTS_CHAT_ID` arrive at `ingest` (secrets.env →
    HOST_KEYS → environment:, verified in the container on 03.08.2026) — the guard’s
    TG alert works.

    There is no PII here by design (source_key + the failure class), but the rule from the project rules
    is observed in exceptions too: the CLASS of the error is logged, without ``exc_info``.
    """

    def __init__(self, alert: Callable[[str], Awaitable[None]] | None = None) -> None:
        self._alert = alert

    async def report(self, source_key: str, kind: GuardKind, message: str) -> None:
        logger.warning("guard[%s] %s: %s", source_key, kind.value, message)
        if self._alert is None:
            return
        try:
            await self._alert(f"⚠️ [{source_key}] {kind.value}: {message}")
        except Exception as exc:  # noqa: BLE001 — the alert does not break the guard
            logger.warning(
                "guard[%s]: TG alert not sent (%s)", source_key, type(exc).__name__
            )


def reporter_from_env(label: str = "ingest") -> GuardReporter:
    """:class:`GuardReporter` on top of the standard alert contour (`WorkerHealth`).

    ``WorkerHealth.alert`` by itself is a no-op if the token/chat is not set — that is,
    exactly today’s blind TG. The logs are written in any case.
    The import is lazy: `src/parsers/` must not pull `src/workers/` when the
    package is imported (otherwise the adapters would stop being clean snapshot-testable modules).
    """
    from src.workers.health import WorkerHealth

    return GuardReporter(alert=WorkerHealth.from_env(label=label).alert)


__all__ = [
    "CountStore",
    "GuardKind",
    "GuardReporter",
    "HttpVerdict",
    "LayoutCanary",
    "RobotsDisallowed",
    "RobotsUnavailable",
    "check_http_status",
    "check_layout",
    "check_robots",
    "check_volume",
    "classify_status",
    "reporter_from_env",
    "retry_after_seconds",
    "robots_allows",
    "robots_url",
]
