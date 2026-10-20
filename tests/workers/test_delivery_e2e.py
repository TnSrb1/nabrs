"""The end-to-end regression of the delivery path: routing → outbox → delivery → adapter → attempt.

Why a separate file, if there are `test_delivery.py` / `test_delivery_digest.py` / `test_matching.py`: those tests are units, each checks ITS link on a scenario stub session (the results laid out in advance). Here the links are connected: the same set of rows goes through the WHOLE path — the tender gets into `delivery_outbox` by the routing's hand, from there the DeliveryWorker picks it up, gives it to the adapter and finalises the status. This is how the class of defects invisible to the units is caught: the links taken separately are right, but the joint between them has diverged. Exactly this class already cost the product 2683 dead deliveries in 3.5 weeks (#340): the routing honestly wrote the rows, the delivery honestly failed them, and both were “green”.

The contour of the test:

- WITHOUT a network and WITHOUT a PostgreSQL: `_FakeDb` — a mini DB in memory, it answers the same SQL constants that the modules send to the real session.
- ‼️ The honesty boundary: `_FakeDb` does NOT execute the SQL, it reproduces its meaning on python structures. So the tests here check the WIRING of the path (who calls whom, which predicate gates what, where the status goes), but NOT the semantics of the SQL strings themselves — that is proved only by a live PG (`tests/db/`, `TEST_RLS_DATABASE_URL`). Therefore the routing goes by the IDENTITY of the constant (`statement is ...`), not by a substring of the text: an edit of the SQL wording must not turn this file red falsely.

The branches (each has already broken in production or was born in the night correspondence of the path):

1. `test_live_telegram_delivers_end_to_end` — the basic production path (3 live telegram channels, all on `immediate`).
2. `test_dead_channel_never_reaches_outbox` — a channel that is not live → there is NO row in the outbox (#340 “the setting ≠ the fact”, #360 double opt-in: `pending` is not an addressee).
3. `test_email_without_transport_is_retryable_not_lost` — #349: there is no transport → `retryable`, the row waits in the queue; `ok=True` here = a silent loss of the letter.
4. `test_digest_row_waits_outside_window_and_leaves_in_window` — #357: outside the window the row WAITS (is not lost, the status is not touched), in the window it goes out in one batch.
5. `test_immediate_profile_delivers_card_per_tender` — `immediate` works the old way (#357 should not have touched the path of ALL the live profiles of production).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.workers import delivery as delivery_mod
from src.workers import matching as matching_mod
from src.workers import routing as routing_mod
from src.workers.adapters.base import DeliveryJob, DeliveryResult, FormattedMessage
from src.workers.adapters.email import EmailAdapter
from src.workers.delivery import DeliveryWorker
from src.workers.routing import RoutingWorker

BELGRADE = ZoneInfo("Europe/Belgrade")

TENANT_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"
PROFILE_ID = "33333333-3333-3333-3333-333333333333"
TENDER_ID = "44444444-4444-4444-4444-444444444444"
TENDER2_ID = "55555555-5555-5555-5555-555555555555"

# The profile matches by this word, the tenders contain it (stage B — the real one).
KEYWORD = "видеонадзор"


# --------------------------------------------------------------------------- #
#  A mini DB in memory
# --------------------------------------------------------------------------- #


class _Result:
    """The result of execute: the little from the SQLAlchemy API that the workers read."""

    def __init__(self, rows=(), scalar_value=None):
        self._rows = list(rows)
        self.rowcount = len(self._rows)
        self._scalar = scalar_value

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _FakeDb:
    """The rows of the delivery path in memory + the answers to the SQL constants of the modules.

    Both a “DB” and a session (`execute`/`commit`) at once: the session factory gives out one and the same instance, therefore routing and delivery see ONE set of rows — this is the whole point of the end-to-end test.

    An unknown statement — a loud error, not an empty result: if a new link appears on the path of the delivery (a new SELECT/UPDATE), it will have to be consciously entered here, rather than discovered in production.
    """

    def __init__(self, *, now):
        self._now = now
        self.tenants: list[dict] = []
        self.users: list[dict] = []
        self.profiles: list[dict] = []
        self.user_channels: list[dict] = []
        self.tenders: list[dict] = []
        self.outbox: list[dict] = []
        self.attempts: list[dict] = []
        self._outbox_seq = 0
        self.commits = 0

    # -- the API of the session --------------------------------------------------- #

    async def execute(self, statement, params=None):
        args = dict(params or {})
        for const, handler in self._routes():
            if statement is const:
                return handler(args)
        raise AssertionError(
            "_FakeDb: an unknown SQL on the path of the delivery — the link is not entered into the test:\n"
            f"{statement}"
        )

    async def commit(self):
        self.commits += 1

    def _routes(self):
        return (
            (routing_mod._LOAD_TENDER_SQL, self._load_tender),
            (matching_mod.STAGE_A_SQL, self._stage_a),
            (matching_mod.LIVE_CHANNELS_SQL, self._live_channels),
            (matching_mod.WRITE_OUTBOX_SQL, self._write_outbox),
            (matching_mod.WRITE_WEB_OUTBOX_SQL, self._write_web_outbox),
            (delivery_mod._DIGEST_PROFILES_SQL, self._digest_profiles),
            (delivery_mod._CLAIM_SQL, self._claim),
            (delivery_mod._LOAD_JOBS_SQL, self._load_jobs),
            (delivery_mod._PROFILE_DAILY_TENDERS_SQL, self._profile_counts),
            (delivery_mod._INSERT_ATTEMPT_SQL, self._insert_attempt),
            (delivery_mod._SET_SENT_SQL, self._set_sent),
            (delivery_mod._SET_FAILED_SQL, self._set_failed),
            (delivery_mod._REVERT_READY_SQL, self._revert_ready),
            (delivery_mod._BLOCK_CHANNEL_SQL, self._block_channel),
        )

    # -- routing ----------------------------------------------------------- #

    def _load_tender(self, args):
        rows = [t for t in self.tenders if t["id"] == args["tender_id"]]
        return _Result(SimpleNamespace(**t) for t in rows)

    def _stage_a(self, args):
        """The source of the rows for stage A.

        The predicate of STAGE_A_SQL itself (the country/regions/sums/NULL semantics) is the responsibility of `test_matching.py` and of the live PG; here stage A is needed only as a supplier of candidates, therefore only the conditions on which the END-TO-END path depends are reproduced: the profile is active and the tenant is alive.
        """
        alive = {t["id"] for t in self.tenants if t["status"] in ("trial", "active")}
        return _Result(
            SimpleNamespace(
                id=p["id"],
                tenant_id=p["tenant_id"],
                keywords_include=p["keywords_include"],
                keywords_exclude=p["keywords_exclude"],
                ai_analysis_level=p["ai_analysis_level"],
                delivery_channels=p["delivery_channels"],
                keyword_dict=p["keyword_dict"],
            )
            for p in self.profiles
            if p["is_active"] and p["tenant_id"] in alive
        )

    def _live_channels(self, args):
        """`DISTINCT ON (channel)` by the tenant: the freshest active row of the channel."""
        tenant_id = args["tenant_id"]
        user_ids = {u["id"] for u in self.users if u["tenant_id"] == tenant_id}
        rows = [uc for uc in self.user_channels if uc["user_id"] in user_ids]
        picked: dict[str, dict] = {}
        for uc in sorted(rows, key=self._channel_rank, reverse=True):
            picked.setdefault(uc["channel"], uc)
        return _Result(
            SimpleNamespace(
                channel=uc["channel"],
                status=uc["status"],
                external_id=uc["external_id"],
            )
            for uc in picked.values()
        )

    @staticmethod
    def _channel_rank(uc: dict):
        """The order of choosing the channel’s row: active first, then the freshest binding.

        The same order must be in `LIVE_CHANNELS_SQL` and `_LOAD_JOBS_SQL` — otherwise the verdict “the channel is live” would diverge from where the delivery goes (CLAUDE.md: “a predicate duplicating the selection of another module — mirror it”).
        """
        return (uc["status"] == "active", uc["linked_at"])

    def _write_outbox(self, args, status=None):
        for channel in args["channels"]:
            key = (args["profile_id"], args["tender_id"], channel)
            if any(
                (o["profile_id"], o["tender_id"], o["channel"]) == key
                for o in self.outbox
            ):
                continue  # ON CONFLICT ... DO NOTHING
            self._outbox_seq += 1
            self.outbox.append(
                {
                    "id": self._outbox_seq,
                    "profile_id": args["profile_id"],
                    "tender_id": args["tender_id"],
                    "channel": channel,
                    "status": status
                    or ("ai_pending" if args["ai_level"] != "none" else "pending"),
                    "requires_ai": args["ai_level"] != "none",
                    "created_at": self._now(),
                    "sent_at": None,
                    "failed_at": None,
                }
            )
        return _Result()

    def _write_web_outbox(self, args):
        """Cabinet row (third channel): ``web``/``web_only`` — delivery does not take it."""
        return self._write_outbox(
            {
                "channels": ["web"],
                "profile_id": args["profile_id"],
                "tender_id": args["tender_id"],
                "ai_level": "none",
            },
            status="web_only",
        )

    # -- delivery ---------------------------------------------------------- #

    def _digest_profiles(self, args):
        rows = []
        for p in self.profiles:
            if p["delivery_schedule"] not in args["schedules"]:
                continue
            tenant = next(t for t in self.tenants if t["id"] == p["tenant_id"])
            sent = [
                o["sent_at"]
                for o in self.outbox
                if o["profile_id"] == p["id"] and o["status"] == "sent" and o["sent_at"]
            ]
            rows.append(
                SimpleNamespace(
                    profile_id=p["id"],
                    delivery_schedule=p["delivery_schedule"],
                    digest_cron=p["digest_cron"],
                    tz=tenant["tz"] or args["default_tz"],
                    last_sent_at=max(sent) if sent else None,
                )
            )
        return _Result(rows)

    def _claim(self, args):
        hold = {str(pid) for pid in args["hold"]}
        ready = [
            o
            for o in sorted(self.outbox, key=lambda o: o["created_at"])
            if o["status"] in args["ready"]
            and o["channel"] in args["channels"]
            and (o["profile_id"] is None or str(o["profile_id"]) not in hold)
        ][: args["batch"]]
        for o in ready:
            o["status"] = args["claimed"]
        return _Result(SimpleNamespace(id=o["id"]) for o in ready)

    def _load_jobs(self, args):
        """`DISTINCT ON (o.id)`: outbox × profile × the user's channel × tender.

        If there is no row of `user_channels` for the channel — there is no job at all (the delivery has nothing to address), and this is NOT the same as a job with an inactive channel: the first case leads to `_fail_no_target`, the second — to the refusal of the dispatch.
        """
        rows = []
        for o in self.outbox:
            if o["id"] not in args["ids"]:
                continue
            profile = next(
                (p for p in self.profiles if p["id"] == o["profile_id"]), None
            )
            tender = next((t for t in self.tenders if t["id"] == o["tender_id"]), None)
            if profile is None or tender is None:
                continue
            user_ids = {
                u["id"] for u in self.users if u["tenant_id"] == profile["tenant_id"]
            }
            targets = [
                uc
                for uc in self.user_channels
                if uc["user_id"] in user_ids and uc["channel"] == o["channel"]
            ]
            if not targets:
                continue
            uc = max(targets, key=self._channel_rank)
            rows.append(
                SimpleNamespace(
                    outbox_id=o["id"],
                    tender_id=o["tender_id"],
                    channel=o["channel"],
                    requires_ai=o["requires_ai"],
                    profile_id=o["profile_id"],
                    user_channel_id=uc["id"],
                    target=uc["external_id"],
                    channel_status=uc["status"],
                    title=tender["title"],
                    source_url=tender["source_url"],
                    amount_eur=tender["amount_eur"],
                    deadline_at=tender["deadline_at"],
                    source_uid=tender["source_uid"],
                    source_key=tender["source_key"],
                    amount_original=None,
                    currency_original=None,
                    is_amount_estimated=False,
                    published_at=tender["first_seen_at"],
                    procedure_type=tender["procedure_type"],
                    contract_type=None,
                    regions=tender["regions"],
                    classifications=tender["classifications"],
                    profile_name=profile["name"],
                    debug_match=False,
                    profile_keywords_include=profile["keywords_include"],
                    profile_keywords_exclude=profile["keywords_exclude"],
                    profile_classifications=None,
                    tender_title=tender["title_normalized"],
                    tender_description=tender["description_normalized"],
                    language=tender.get("language"),
                )
            )
        return _Result(rows)

    def _profile_counts(self, args):
        day_start = (
            self._now()
            .astimezone(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        pairs: set[tuple[str, str]] = set()
        for o in self.outbox:
            if o["profile_id"] not in args["pids"] or o["status"] != "sent":
                continue
            if o["sent_at"] and o["sent_at"] >= day_start:
                pairs.add((o["profile_id"], o["tender_id"]))
        return _Result(
            SimpleNamespace(profile_id=pid, tender_id=tid) for pid, tid in sorted(pairs)
        )

    def _insert_attempt(self, args):
        self.attempts.append(
            {
                "outbox_id": args["outbox_id"],
                "attempted_at": self._now(),
                "response_code": args["code"],
                "error_text": args["err"],
            }
        )
        return _Result()

    def _set_sent(self, args):
        row = self._outbox_row(args["id"])
        row["status"] = "sent"
        row["sent_at"] = self._now()
        return _Result()

    def _set_failed(self, args):
        row = self._outbox_row(args["id"])
        row["status"] = "failed"
        row["failed_at"] = self._now()
        return _Result()

    def _revert_ready(self, args):
        row = self._outbox_row(args["id"])
        row["status"] = "ai_done" if row["requires_ai"] else "pending"
        return _Result()

    def _block_channel(self, args):
        for uc in self.user_channels:
            if uc["id"] == args["id"]:
                uc["status"] = "blocked"
        return _Result()

    # -- the helpers of the assertions --------------------------------------------- #

    def _outbox_row(self, outbox_id: int) -> dict:
        return next(o for o in self.outbox if o["id"] == outbox_id)

    def statuses(self) -> list[str]:
        return [o["status"] for o in self.outbox]

    def attempts_for(self, outbox_id: int) -> list[dict]:
        return [a for a in self.attempts if a["outbox_id"] == outbox_id]


def _session_factory(db: _FakeDb):
    @asynccontextmanager
    async def factory():
        yield db

    return factory


# --------------------------------------------------------------------------- #
#  An adapter fake (there is no network in any branch)
# --------------------------------------------------------------------------- #


class _Adapter:
    """A channel adapter: counts the cards and the digests separately."""

    def __init__(self, channel: str = "telegram", result: DeliveryResult | None = None):
        self.channel = channel
        self._result = result or DeliveryResult(ok=True, response_code=200)
        self.cards: list[DeliveryJob] = []
        self.digests: list[list[DeliveryJob]] = []
        self.targets: list[str | None] = []

    def supports_buttons(self) -> bool:
        return True

    def is_configured(self) -> bool:
        return True

    def format(self, job: DeliveryJob) -> FormattedMessage:
        self.cards.append(job)
        return FormattedMessage(text=f"карточка {job.tender_id}")

    def format_digest(self, jobs: list[DeliveryJob]) -> FormattedMessage:
        self.digests.append(list(jobs))
        return FormattedMessage(text=f"дайджест на {len(jobs)}")

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        self.targets.append(job.target)
        return self._result


# --------------------------------------------------------------------------- #
#  The world: a tenant + a user + a profile + the channels + the tenders
# --------------------------------------------------------------------------- #


def _world(
    *,
    now: datetime,
    channels: list[tuple[str, str | None, str]],
    delivery_channels: tuple[str, ...] = ("telegram",),
    schedule: str = "immediate",
    digest_cron: str | None = None,
    tender_ids: tuple[str, ...] = (TENDER_ID,),
) -> _FakeDb:
    """One tenant with one profile; `channels` — the rows of `user_channels`.

    `channels` — a list of `(channel, external_id, status)`: exactly the fork on which the regression #340/#360 sits (the setting of the profile exists, a live addressee may not).
    """
    db = _FakeDb(now=lambda: now)
    db.tenants.append({"id": TENANT_ID, "status": "active", "tz": "Europe/Belgrade"})
    db.users.append({"id": USER_ID, "tenant_id": TENANT_ID})
    db.profiles.append(
        {
            "id": PROFILE_ID,
            "tenant_id": TENANT_ID,
            "name": "Видеонаблюдение",
            "is_active": True,
            "keywords_include": [{"all_of": [KEYWORD]}],
            "keywords_exclude": [],
            "ai_analysis_level": "none",
            "delivery_channels": list(delivery_channels),
            "keyword_dict": False,
            "delivery_schedule": schedule,
            "digest_cron": digest_cron,
        }
    )
    for index, (channel, external_id, status) in enumerate(channels, start=1):
        db.user_channels.append(
            {
                "id": f"uc-{index}",
                "user_id": USER_ID,
                "channel": channel,
                "external_id": external_id,
                "status": status,
                "linked_at": now - timedelta(days=index),
            }
        )
    for index, tender_id in enumerate(tender_ids, start=1):
        db.tenders.append(
            {
                "id": tender_id,
                "country_code": "RS",
                "source_key": "jnportal",
                "regions": ["Београд"],
                "classifications": [],
                "procedure_type": None,
                "contract_type": None,
                "customer_name": None,
                "language": None,
                "amount_eur": 12345.0,
                "deadline_at": now + timedelta(days=30),
                "status": "active",
                "title": f"Набавка опреме {index}",
                "title_normalized": f"{KEYWORD} опрема {index}",
                "description_normalized": "",
                "source_url": f"https://jnportal.rs/t/{index}",
                "source_uid": f"JN-{index}",
                "first_seen_at": now,
            }
        )
    return db


def _routing(db: _FakeDb) -> RoutingWorker:
    return RoutingWorker(session_factory=_session_factory(db))


def _delivery(db: _FakeDb, adapters: dict, *, now: datetime) -> DeliveryWorker:
    # send_delay=0 — the pacing is checked by the unit, here it would only slow the test down.
    return DeliveryWorker(
        adapters=adapters,
        session_factory=_session_factory(db),
        send_delay=0,
        now_factory=lambda: now,
    )


async def _route(db: _FakeDb, tender_id: str = TENDER_ID) -> int:
    return await _routing(db).route_tender({"tender_id": tender_id})


# --------------------------------------------------------------------------- #
#  1. The basic path of production: a live telegram → the delivery goes out
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_live_telegram_delivers_end_to_end():
    """The tender reaches the subscriber: routing → outbox → adapter → sent + attempt.

    This is exactly the configuration of production (a live telegram channel, the profile `immediate`). A red of this test = the subscriber silently stopped receiving tenders.
    """
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    db = _world(now=now, channels=[("telegram", "555", "active")])
    adapter = _Adapter()

    matched = await _route(db)
    assert matched == 1
    # Link 1: the row got into the queue — one channel, ready to send.
    assert [(o["channel"], o["status"]) for o in db.outbox] == [("telegram", "pending")]

    processed = await _delivery(db, {"telegram": adapter}, now=now).poll_once()

    # Links 2-3: the batch is picked up, the card (not the digest) is sent to the channel’s address.
    assert processed == 1
    assert [job.tender_id for job in adapter.cards] == [TENDER_ID]
    assert adapter.digests == []
    assert adapter.targets == ["555"]
    # Link 4: the status and the attempt are written — the delivery is visible both in the outbox and in the attempts.
    assert db.statuses() == ["sent"]
    assert db.outbox[0]["sent_at"] == now
    assert [a["response_code"] for a in db.attempts_for(1)] == [200]


# --------------------------------------------------------------------------- #
#  2. The channel is not live → there is NO row in the outbox (#340, #360)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "channels",
    [
        pytest.param([], id="the channel was never linked"),
        pytest.param([("telegram", "555", "blocked")], id="the bot is blocked (#340)"),
        pytest.param(
            [("telegram", "555", "pending")], id="awaiting confirmation (#360 opt-in)"
        ),
        pytest.param([("telegram", "", "active")], id="active, but the addressee is empty"),
    ],
)
@pytest.mark.asyncio
async def test_dead_channel_never_reaches_outbox(channels):
    """No live addressee → the send queue is empty, delivery has nothing to do.

    Regression #340: `profiles.delivery_channels` is a SETTING ("where to send"), and
    `user_channels` is a FACT ("someone to send to"). Merging them gave 2683 dead deliveries
    in 3.5 weeks: routing stamped rows, delivery failed them. Regression #360:
    `pending` (double opt-in not passed) is NOT an addressee — otherwise the bot
    writes to someone who has not yet confirmed the subscription.

    We check precisely the absence of a row, not "delivery failed": a dead
    row in the queue is the defect itself, not its consequence.
    """
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    db = _world(now=now, channels=channels)
    adapter = _Adapter()

    # The profile matched the tender — it is exactly the liveness of the channel that cuts it off, not the matching.
    assert await _route(db) == 1
    # Into the send queue — nothing; into the cabinet (third channel, owner 18.10) — a row
    # web/web_only, which delivery does not take (check below: poll_once == 0).
    assert [(o["channel"], o["status"]) for o in db.outbox] == [("web", "web_only")]

    assert await _delivery(db, {"telegram": adapter}, now=now).poll_once() == 0
    assert adapter.cards == []
    assert db.attempts == []


# --------------------------------------------------------------------------- #
#  3. E-mail without a transport → retryable, NOT «ok» (#349)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_email_without_transport_is_retryable_not_lost():
    """There is no SMTP transport → the row returns to the queue and waits for the env.

    The regression #349 (fail-closed): “success” with an unconfigured transport is a silent loss of the letter. The adapter here is REAL (`EmailAdapter(None)`) — it does not touch the network precisely because there is no transport; a fake would prove this in vain.
    """
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    db = _world(
        now=now,
        channels=[("email", "buyer@example.rs", "active")],
        delivery_channels=("email",),
    )
    adapter = EmailAdapter(None)
    assert adapter.is_configured() is False

    assert await _route(db) == 1
    assert [(o["channel"], o["status"]) for o in db.outbox] == [("email", "pending")]

    assert await _delivery(db, {"email": adapter}, now=now).poll_once() == 1

    # The row is NOT sent and NOT failed: it returned to a ready status — it will go out when
    # the env appears. And the attempt is written: there must be no silence in attempts.
    assert db.statuses() == ["pending"]
    assert db.outbox[0]["sent_at"] is None
    assert len(db.attempts_for(1)) == 1
    assert db.attempts_for(1)[0]["error_text"] == "почтовый транспорт не настроен"


# --------------------------------------------------------------------------- #
#  4. The digest: outside the window the row WAITS, in the window it goes out in a batch (#357)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_digest_row_waits_outside_window_and_leaves_in_window():
    """The profile `digest_morning`: until 09:00 in the tenant’s tz the rows wait, then they go out.

    The regression #357: “waits” must mean “the status is not touched” — the profile simply does not take part in the pickup. If the row were picked up and failed, it would go into failed/attempts and get lost silently. The second run on the same day proves that the waiting does not eat the row: both go by ONE digest.
    """
    morning = datetime(2026, 7, 17, 7, 0, tzinfo=BELGRADE)  # 07:00 — the window is at 09:00
    db = _world(
        now=morning,
        channels=[("telegram", "555", "active")],
        schedule="digest_morning",
        tender_ids=(TENDER_ID, TENDER2_ID),
    )
    adapter = _Adapter()

    await _route(db, TENDER_ID)
    await _route(db, TENDER2_ID)
    assert db.statuses() == ["pending", "pending"]

    # Outside the window: the batch is not picked up, the statuses are not touched, there are no attempts.
    assert await _delivery(db, {"telegram": adapter}, now=morning).poll_once() == 0
    assert db.statuses() == ["pending", "pending"]
    assert db.attempts == []
    assert adapter.cards == [] and adapter.digests == []

    # In the window (09:30 of the same day): both rows — ONE message, both sent.
    in_window = morning.replace(hour=9, minute=30)
    assert await _delivery(db, {"telegram": adapter}, now=in_window).poll_once() == 2
    assert [len(d) for d in adapter.digests] == [2]
    assert adapter.cards == []
    assert db.statuses() == ["sent", "sent"]
    assert len(db.attempts) == 2  # an attempt for EACH row of the batch, not per message


# --------------------------------------------------------------------------- #
#  5. `immediate` — a card per tender (the path of ALL the live profiles of production)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_immediate_profile_delivers_card_per_tender():
    """`immediate` must not have turned into a digest after #357.

    Production now: all the live profiles are `immediate`. Two tenders = two cards, not one message.
    """
    now = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    db = _world(
        now=now,
        channels=[("telegram", "555", "active")],
        schedule="immediate",
        tender_ids=(TENDER_ID, TENDER2_ID),
    )
    adapter = _Adapter()

    await _route(db, TENDER_ID)
    await _route(db, TENDER2_ID)

    assert await _delivery(db, {"telegram": adapter}, now=now).poll_once() == 2
    assert [job.tender_id for job in adapter.cards] == [TENDER_ID, TENDER2_ID]
    assert adapter.digests == []
    assert db.statuses() == ["sent", "sent"]
