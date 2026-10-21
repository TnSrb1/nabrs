"""Unit tests of the two-stage matching (Sprint-16).

Stage B and the helpers — pure Python, without a DB. Stage A and the write to the outbox — through a mocked ``AsyncSession`` (the network/DB are not touched): we check the parameters of the SQL and the form of the result, not PostgreSQL itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.workers import matching
from src.workers.matching import (
    STAGE_A_SQL,
    Candidate,
    TenderView,
    keywords_match,
    primary_classification,
    stage_b_filter,
)

# --------------------------------------------------------------------------- #
#  Mocks of the DB
# --------------------------------------------------------------------------- #


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Records (statement, params) of each execute; gives the preparations."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return self._results.pop(0) if self._results else _FakeResult([])


def _tender(**over) -> TenderView:
    base = dict(
        id="t-1",
        country_code="RS",
        source_key="jnportal",
        regions=["RS-00"],
        classifications=[{"system_code": "eu_cpv", "code": "32200000"}],
        procedure_type="open",
        language="sr-Cyrl",
        amount_eur=10000.0,
        deadline_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        title_normalized="nabavka video opreme",
        description_normalized="montaza sistema video nadzora",
    )
    base.update(over)
    return TenderView(**base)


# --------------------------------------------------------------------------- #
#  Stage B — keywords_match (pure Python)
# --------------------------------------------------------------------------- #


def test_empty_include_matches_everything():
    assert keywords_match(None, None, "что угодно") is True
    assert keywords_match([], [], "что угодно") is True


def test_single_group_all_terms_present():
    incl = [{"all_of": ["video", "montaza"]}]
    assert keywords_match(incl, None, "nabavka video opreme montaza") is True


def test_single_group_missing_term_fails():
    incl = [{"all_of": ["video", "skud"]}]
    assert keywords_match(incl, None, "nabavka video opreme") is False


def test_or_between_groups():
    incl = [{"all_of": ["skud", "kontrola"]}, {"all_of": ["video"]}]
    # the first group does not match, the second — does → OR
    assert keywords_match(incl, None, "nabavka video opreme") is True


def test_exclude_blocks_even_if_include_matches():
    incl = [{"all_of": ["video"]}]
    assert keywords_match(incl, ["nadzor"], "video nadzor sistem") is False


def test_match_is_case_insensitive():
    incl = [{"all_of": ["VIDEO"]}]
    assert keywords_match(incl, None, "nabavka video opreme") is True


def test_exclude_only_no_include():
    # an empty include + a non-triggered exclude → a match
    assert keywords_match([], ["skud"], "video nadzor") is True
    assert keywords_match([], ["video"], "video nadzor") is False


# --------------------------------------------------------------------------- #
#  primary_classification
# --------------------------------------------------------------------------- #


def test_primary_classification_empty():
    assert primary_classification(None) == (None, None)
    assert primary_classification([]) == (None, None)


def test_primary_classification_takes_first():
    cls = [
        {"system_code": "eu_cpv", "code": "32200000"},
        {"system_code": "eu_cpv", "code": "45000000"},
    ]
    assert primary_classification(cls) == ("eu_cpv", "32200000")


def test_primary_classification_reads_live_ingest_shape():
    """‼️ The fixtures above wrote the classification with the PROFILE key — as nobody writes it in the DB. The jnportal adapter puts ``{"scheme": "CPV"}``, and stage A got (None, None) → a profile with a CPV filter matched nothing."""
    live = [
        {"code": "32200000", "label": "Опрема за емитовање", "scheme": "CPV"},
        {"year": 2026, "scheme": "PLAN", "quarter": "2. квартал"},
    ]
    assert primary_classification(live) == ("eu_cpv", "32200000")


@pytest.mark.asyncio
async def test_stage_a_params_canonical_for_live_shaped_tender():
    """The same tender, but the classification in the ingest form → the same SQL parameters."""
    session = _FakeSession([_FakeResult([])])
    await matching.stage_a_candidates(
        session,
        _tender(classifications=[{"code": "32200000", "scheme": "CPV"}]),
    )
    _, params = session.calls[0]
    assert params["tender_system_code"] == "eu_cpv"
    assert params["tender_classification_code"] == "32200000"


def test_stage_a_sql_canonicalises_profile_side_of_comparison():
    """The side of the profile in the SQL must be canonicalised, otherwise `CPV` ≠ `eu_cpv`."""
    sql = str(matching.STAGE_A_SQL).lower()
    assert sql.count("'^cpv$', 'eu_cpv'") == 2  # include + exclude
    assert "inc->>'system_code'" in sql and "exc->>'system_code'" in sql


# --------------------------------------------------------------------------- #
#  stage_b_filter
# --------------------------------------------------------------------------- #


def _cand(**over) -> Candidate:
    base = dict(
        profile_id="p-1",
        tenant_id="te-1",
        keywords_include=[{"all_of": ["video"]}],
        keywords_exclude=None,
        ai_analysis_level="standard",
        delivery_channels=["telegram"],
    )
    base.update(over)
    return Candidate(**base)


def test_stage_b_filter_keeps_matching_drops_rest():
    keep = _cand(profile_id="keep", keywords_include=[{"all_of": ["video"]}])
    drop = _cand(profile_id="drop", keywords_include=[{"all_of": ["skud"]}])
    out = stage_b_filter([keep, drop], _tender())
    assert [c.profile_id for c in out] == ["keep"]


def test_stage_b_keeps_match_all_for_null_tender():
    """A match-all profile (an empty include) passes stage B even on a jnportal tender with NULL procedure/amount/deadline and empty regions."""
    cand = _cand(profile_id="p-all", keywords_include=[], keywords_exclude=None)
    tender = _tender(
        deadline_at=None,
        procedure_type=None,
        amount_eur=None,
        language=None,
        regions=[],
        classifications=[],
    )
    out = stage_b_filter([cand], tender)
    assert [c.profile_id for c in out] == ["p-all"]


# --------------------------------------------------------------------------- #
#  Stage A — stage_a_candidates (a mock session)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stage_a_builds_params_and_maps_rows():
    rows = [
        SimpleNamespace(
            id="p-1",
            tenant_id="te-1",
            keywords_include=[{"all_of": ["video"]}],
            keywords_exclude=["test"],
            ai_analysis_level="standard",
            delivery_channels=["telegram", "email"],
            keyword_dict=False,
        )
    ]
    session = _FakeSession([_FakeResult(rows)])
    cands = await matching.stage_a_candidates(session, _tender())

    # the SQL parameters: country + the primary classification are passed through
    _, params = session.calls[0]
    assert params["tender_country_code"] == "RS"
    assert params["tender_system_code"] == "eu_cpv"
    assert params["tender_classification_code"] == "32200000"
    assert params["source_key"] == "jnportal"
    assert params["tender_regions"] == ["RS-00"]

    # the mapping of the row into Candidate
    assert len(cands) == 1
    assert cands[0].profile_id == "p-1"
    assert cands[0].delivery_channels == ["telegram", "email"]
    assert cands[0].keyword_dict is False


# --------------------------------------------------------------------------- #
#  Stage A — the NULL semantics of the jnportal tender (the fix “routing matches 0”, Chat 28)
# --------------------------------------------------------------------------- #


def _sql_norm() -> str:
    """STAGE_A_SQL as one line in lower case (robust to indentation)."""
    return re.sub(r"\s+", " ", str(STAGE_A_SQL)).strip().lower()


def test_stage_a_sql_guards_null_deadline_keeps_timestamptz_cast():
    """deadline IS NULL does not cut the profile; the CAST timestamptz is kept (asyncpg)."""
    sql = _sql_norm()
    assert "cast(:deadline as timestamptz) is null" in sql
    # the deadline filter remains when the deadline is set
    assert "min_days_until_deadline" in sql


def test_stage_a_sql_guards_null_procedure_and_language():
    sql = _sql_norm()
    # The NULL guards now sit on top of the casts (closing of the AmbiguousParameterError)
    assert "cast(:proc_type as text) is null" in sql
    assert "cast(:lang as text) is null" in sql
    # the branches “the filter is not set” (an empty array) are also in place — now under
    # COALESCE (a NULL array of the profile is treated as empty, does not cut)
    assert "cardinality(coalesce(p.procedure_types, '{}')) = 0" in sql
    assert "cardinality(coalesce(p.languages, '{}')) = 0" in sql


def test_stage_a_sql_guards_null_amount_when_profile_has_no_bounds():
    sql = _sql_norm()
    assert "cast(:amount_eur as numeric) is null" in sql
    # the path “the profile has no amount bounds” is added
    assert "p.min_amount_eur is null and p.max_amount_eur is null" in sql
    # include_no_amount is kept (deterministically via coalesce)
    assert "coalesce(p.include_no_amount, false) = true" in sql


def test_stage_a_sql_casts_classification_params_as_text():
    """The classification parameters with NULL do not give asyncpg the type → an explicit CAST(... AS text) in both places: the include-EXISTS and the exclude-NOT EXISTS."""
    sql = _sql_norm()
    assert "cast(:tender_system_code as text)" in sql
    assert "cast(:tender_classification_code as text)" in sql
    # 2 occurrences of each (include + exclude)
    assert sql.count("cast(:tender_system_code as text)") == 2
    assert sql.count("cast(:tender_classification_code as text)") == 2
    # the existing casts are not lost
    assert "cast(:tender_regions as text[])" in sql
    assert "cast(:amount_eur as numeric)" in sql
    assert "cast(:deadline as timestamptz)" in sql


def test_stage_a_sql_casts_all_bind_params_no_bare_param():
    """Closing of the WHOLE class AmbiguousParameterError: EVERY bind parameter of STAGE_A_SQL is typed by an explicit CAST. Not a single :param remains bare in `IS NULL` / `= ANY(...)` / a comparison with a column / LIKE / BETWEEN — asyncpg with a NULL otherwise does not infer the type (the root — `$6 = :proc_type` NULL, the legacy of #99)."""
    sql = _sql_norm()

    # 1) all the previously non-cast parameters are now under CAST(... AS text)
    assert "cast(:tender_country_code as text)" in sql
    assert "cast(:source_key as text)" in sql
    assert "cast(:proc_type as text)" in sql
    assert "cast(:lang as text)" in sql

    # 2) proc_type / lang are cast in BOTH places (IS NULL and = ANY)
    assert sql.count("cast(:proc_type as text)") == 2
    assert sql.count("cast(:lang as text)") == 2

    # 3) NOT A SINGLE bare parameter in the dangerous contexts is left
    bare_patterns = (
        ":tender_country_code",
        ":source_key",
        ":proc_type",
        ":lang",
        ":tender_regions",
        ":tender_system_code",
        ":tender_classification_code",
        ":amount_eur",
        ":deadline",
    )
    for name in bare_patterns:
        # each occurrence of a parameter name must come right after "cast(".
        for m in re.finditer(re.escape(name), sql):
            prefix = sql[max(0, m.start() - 5) : m.start()]
            assert prefix.endswith(
                "cast("
            ), f"the bare parameter {name} outside CAST(...): ...{sql[max(0, m.start() - 15): m.start() + 5]}..."


def test_stage_a_sql_coalesces_null_array_columns():
    """The NULL arrays of the profile (enabled_source_keys/regions/procedure_types/languages) must NOT falsely cut the profile (the onboarding fix): on a NULL array ``cardinality(NULL)`` gives NULL (not 0), ``:p = ANY(NULL)`` gives NULL — neither branch is TRUE → the profile was cut by three-valued logic. COALESCE(<col>, '{}') brings NULL to an empty array (deterministically), the semantics of the non-empty values does not change. source_key — WITHOUT the indulgence “no filter” (the source is mandatory), but protected from NULL the same way (NULL sources → the profile does not match)."""
    sql = _sql_norm()
    # the source is mandatory, but protected from NULL via coalesce(..., '{}')
    assert "= any(coalesce(p.enabled_source_keys, '{}'))" in sql
    # regions: both branches under coalesce
    assert "cardinality(coalesce(p.regions, '{}')) = 0" in sql
    assert "coalesce(p.regions, '{}') && cast(:tender_regions as text[])" in sql
    # procedure_types / languages: the branch of the empty filter AND the branch = any under coalesce
    assert "cardinality(coalesce(p.procedure_types, '{}')) = 0" in sql
    assert "= any(coalesce(p.procedure_types, '{}'))" in sql
    assert "cardinality(coalesce(p.languages, '{}')) = 0" in sql
    assert "= any(coalesce(p.languages, '{}'))" in sql


# --------------------------------------------------------------------------- #
#  The activity filter of the tender (the fix of the stale tender): the completed statuses
#  (closed/awarded/cancelled) are not sent; with a NULL status — a fuse
#  by the deadline (not in the past). There is no live PostgreSQL in the CI → we fix it by the text
#  the SQL + the pass-through of the parameter :tender_status.
# --------------------------------------------------------------------------- #
def test_stage_a_sql_filters_completed_tender_status():
    """STAGE_A_SQL contains the activity filter by :tender_status (CAST as text) and the fuse by the deadline with a NULL status."""
    sql = _sql_norm()
    # branch 1: the status is known and completed → do not send
    assert "cast(:tender_status as text) is null" in sql
    assert (
        "cast(:tender_status as text) not in ('closed', 'awarded', 'cancelled')" in sql
    )
    # branch 2: the status NULL → send only if the deadline is not in the past
    assert "cast(:tender_status as text) is not null" in sql
    assert "cast(:deadline as timestamptz) > now()" in sql


@pytest.mark.asyncio
async def test_stage_a_passes_tender_status_param_active():
    """An active tender: :tender_status is passed through; the profile matches (1 candidate)."""
    row = SimpleNamespace(
        id="p-act",
        tenant_id="te-1",
        keywords_include=[],
        keywords_exclude=None,
        ai_analysis_level="none",
        delivery_channels=["telegram"],
        keyword_dict=False,
    )
    session = _FakeSession([_FakeResult([row])])
    cands = await matching.stage_a_candidates(session, _tender(status="active"))

    _, params = session.calls[0]
    assert params["tender_status"] == "active"
    assert len(cands) == 1
    assert cands[0].profile_id == "p-act"


@pytest.mark.asyncio
async def test_stage_a_passes_tender_status_param_completed():
    """A completed tender: :tender_status='closed'/'awarded'/'cancelled' is passed through. The real cut-off is provided by the WHERE (pinned in ``test_stage_a_sql_filters_completed_tender_status``); the session is mocked."""
    for completed in ("closed", "awarded", "cancelled"):
        session = _FakeSession([_FakeResult([])])
        await matching.stage_a_candidates(session, _tender(status=completed))
        _, params = session.calls[0]
        assert params["tender_status"] == completed


@pytest.mark.asyncio
async def test_stage_a_passes_null_status_with_deadline():
    """status=NULL → :tender_status is passed through as None; :deadline is in place for the fuse “the deadline is not in the past”."""
    session = _FakeSession([_FakeResult([])])
    await matching.stage_a_candidates(session, _tender(status=None))
    _, params = session.calls[0]
    assert params["tender_status"] is None
    assert "deadline" in params


@pytest.mark.asyncio
async def test_stage_a_maps_profile_row_with_null_array_fields():
    """A profile with NULL arrays (as the former onboarding created it) maps correctly into a Candidate — the code of stage A does not fall on the NULL fields of the row. A tender with the set language/procedure/region/source; a NULL include → match-all in stage B. The actual NON-cut of such a profile is provided by the COALESCE guard in the SQL (pinned in ``test_stage_a_sql_coalesces_null_array_columns``); there is no live PostgreSQL in the CI — the session is mocked."""
    null_row = SimpleNamespace(
        id="p-null",
        tenant_id="te-1",
        keywords_include=None,
        keywords_exclude=None,
        ai_analysis_level=None,
        delivery_channels=None,
        keyword_dict=False,
    )
    session = _FakeSession([_FakeResult([null_row])])
    cands = await matching.stage_a_candidates(session, _tender())

    _, params = session.calls[0]
    assert params["source_key"] == "jnportal"
    assert params["lang"] == "sr-Cyrl"
    assert len(cands) == 1
    assert cands[0].profile_id == "p-null"
    # A NULL include gives match-all in stage B (there is no filter by words)
    assert cands[0].keywords_include is None
    assert keywords_match(cands[0].keywords_include, cands[0].keywords_exclude, "x")


@pytest.mark.asyncio
async def test_stage_a_no_classification_does_not_drop_match_all():
    """A tender without classification (system_code=None, code=None): stage_a does not fall, the classification parameters=None are passed through, a match-all profile (an empty include/exclude) maps into a Candidate. The branch jsonb_array_length(...)=0 passes the include; CAST(... AS text) removes the NULL in the comparison/LIKE.

    There is no live PostgreSQL in the CI — the session is mocked; the real predicate is pinned by the assertion of the SQL text in ``test_stage_a_sql_casts_classification_params_as_text``.
    """
    match_all_row = SimpleNamespace(
        id="p-all",
        tenant_id="te-1",
        keywords_include=[],
        keywords_exclude=None,
        ai_analysis_level="none",
        delivery_channels=["telegram"],
        keyword_dict=False,
    )
    session = _FakeSession([_FakeResult([match_all_row])])
    tender = _tender(classifications=[])

    cands = await matching.stage_a_candidates(session, tender)

    _, params = session.calls[0]
    assert params["tender_system_code"] is None
    assert params["tender_classification_code"] is None

    assert len(cands) == 1
    assert cands[0].profile_id == "p-all"


# --------------------------------------------------------------------------- #
#  write_outbox (§6.4) — the idempotency and the parameters
# --------------------------------------------------------------------------- #
# write_outbox now makes TWO executes: the SELECT of the live channels (live_channels),
# then the INSERT. Hence `_live([...])` as the first result and `calls[1]` — the INSERT.


def _channel_row(channel="telegram", status="active", external_id="123456"):
    """A row of user_channels in the form of the output of LIVE_CHANNELS_SQL."""
    return SimpleNamespace(channel=channel, status=status, external_id=external_id)


def _live(rows):
    """The result of the SELECT of the live channels — the first execute inside write_outbox."""
    return _FakeResult(rows)


def _insert_call(session):
    """(stmt, params) of the INSERT into the outbox; None — if there was no INSERT."""
    inserts = [c for c in session.calls if "delivery_outbox" in c[0]]
    return inserts[0] if inserts else None


def _is_web_only(call):
    """INSERT of a cabinet row (third channel): literal 'web'/'web_only', without a channels array."""
    stmt, params = call
    return (
        "'web_only'" in stmt
        and "'web'" in stmt
        and "channels" not in params
        and params["profile_id"] == "p-1"
        and params["tender_id"] == "t-1"
    )


@pytest.mark.asyncio
async def test_write_outbox_skips_when_no_channels():
    session = _FakeSession([])
    await matching.write_outbox(
        session, candidate=_cand(delivery_channels=[]), tender=_tender()
    )
    # no channels → no SELECT of live channels, only the web_only row for the cabinet feed
    assert len(session.calls) == 1
    assert _is_web_only(_insert_call(session))


@pytest.mark.asyncio
async def test_write_outbox_logs_web_only_when_no_channels(caplog):
    """Empty delivery_channels — not a "skip" (#820): a web_only row for the cabinet.
    The log about it is INFO with the fact of the write, without a WARNING "skipped" (that one was wrong).
    """
    session = _FakeSession([])
    with caplog.at_level("INFO", logger="src.workers.matching"):
        await matching.write_outbox(
            session,
            candidate=_cand(profile_id="p-1", tenant_id="te-1", delivery_channels=[]),
            tender=_tender(),
        )
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    msgs = [r.getMessage() for r in caplog.records if "web_only" in r.getMessage()]
    assert len(msgs) == 1
    assert "p-1" in msgs[0]
    assert "te-1" in msgs[0]
    assert "пропущен" not in msgs[0]


@pytest.mark.asyncio
async def test_write_outbox_inserts_with_on_conflict():
    session = _FakeSession(
        [_live([_channel_row("telegram"), _channel_row("email", external_id="a@b.rs")])]
    )
    await matching.write_outbox(
        session,
        candidate=_cand(delivery_channels=["telegram", "email"]),
        tender=_tender(),
    )
    stmt, params = _insert_call(session)
    assert "ON CONFLICT" in stmt and "DO NOTHING" in stmt  # idempotency
    assert params["profile_id"] == "p-1"
    assert params["tender_id"] == "t-1"
    assert params["channels"] == ["telegram", "email"]
    assert params["ai_level"] == "standard"
    assert params["match_kind"] == "new"
    assert params["match_score"] == 1.0


@pytest.mark.asyncio
async def test_write_outbox_coalesces_missing_ai_level_to_none():
    session = _FakeSession([_live([_channel_row("telegram")])])
    await matching.write_outbox(
        session,
        candidate=_cand(ai_analysis_level=None),
        tender=_tender(),
    )
    _, params = _insert_call(session)
    assert params["ai_level"] == "none"  # NULL <> 'none' would give NULL — coalesce


# --------------------------------------------------------------------------- #
#  The live addressee: do not multiply the deliveries into a dead channel
# --------------------------------------------------------------------------- #
# A production bug: a profile with a single `revoked` channel for a month and a half produced
# records that delivery honestly failed (“the channel is inactive or there is no target”).
# The key invariant of these tests: a live channel → the behavior does NOT change.


@pytest.mark.asyncio
async def test_write_outbox_inserts_for_active_channel_as_before():
    """The regression: an active telegram channel → the record is created exactly as before."""
    session = _FakeSession([_live([_channel_row("telegram", status="active")])])

    await matching.write_outbox(session, candidate=_cand(), tender=_tender())

    stmt, params = _insert_call(session)
    assert "delivery_outbox" in stmt
    assert params["channels"] == ["telegram"]


@pytest.mark.asyncio
async def test_write_outbox_skips_revoked_channel():
    """A channel in revoked → no record (otherwise the delivery will certainly fail)."""
    session = _FakeSession([_live([_channel_row("telegram", status="revoked")])])

    await matching.write_outbox(session, candidate=_cand(), tender=_tender())

    # not written to the messenger, but the match remains in the cabinet (third channel)
    assert _is_web_only(_insert_call(session))


@pytest.mark.asyncio
async def test_write_outbox_skips_when_tenant_has_no_channels_at_all():
    """The profile is set to telegram, but the tenant has no channels at all → no record."""
    session = _FakeSession([_live([])])

    await matching.write_outbox(session, candidate=_cand(), tender=_tender())

    # not written to the messenger, but the match remains in the cabinet (third channel)
    assert _is_web_only(_insert_call(session))


@pytest.mark.asyncio
async def test_write_outbox_skips_active_channel_without_target():
    """active, but an empty external_id — there is no addressee, the delivery fails “no target”."""
    session = _FakeSession([_live([_channel_row("telegram", external_id="")])])

    await matching.write_outbox(session, candidate=_cand(), tender=_tender())

    # not written to the messenger, but the match remains in the cabinet (third channel)
    assert _is_web_only(_insert_call(session))


@pytest.mark.asyncio
async def test_write_outbox_keeps_only_live_channel_of_two():
    """telegram active + email revoked → a record only for telegram."""
    session = _FakeSession(
        [
            _live(
                [
                    _channel_row("telegram", status="active"),
                    _channel_row("email", status="revoked", external_id="a@b.rs"),
                ]
            )
        ]
    )

    await matching.write_outbox(
        session,
        candidate=_cand(delivery_channels=["telegram", "email"]),
        tender=_tender(),
    )

    _, params = _insert_call(session)
    assert params["channels"] == ["telegram"]  # email is filtered out


@pytest.mark.asyncio
async def test_write_outbox_scopes_live_channels_to_candidate_tenant():
    """The liveness of the channel is looked up in the tenant of the profile — the parameter goes into the SELECT."""
    session = _FakeSession([_live([_channel_row("telegram")])])

    await matching.write_outbox(
        session, candidate=_cand(tenant_id="te-42"), tender=_tender()
    )

    stmt, params = session.calls[0]
    assert "user_channels" in stmt
    assert params == {"tenant_id": "te-42"}


@pytest.mark.asyncio
async def test_write_outbox_repeated_run_stays_idempotent():
    """A repeated run of a live channel: the same INSERT with ON CONFLICT DO NOTHING."""
    calls = []
    for _ in range(2):
        session = _FakeSession([_live([_channel_row("telegram")])])
        await matching.write_outbox(session, candidate=_cand(), tender=_tender())
        calls.append(_insert_call(session))

    assert (
        calls[0] == calls[1]
    )  # the dedup — on the UNIQUE index, not on the state of Python
    assert "ON CONFLICT" in calls[0][0] and "DO NOTHING" in calls[0][0]


def test_live_channels_sql_mirrors_delivery_row_choice():
    """The SQL takes THE SAME row as the delivery: the freshest active one per channel.

    external_id is not unique (the project rules) — if the selection diverged, the predicate would diverge from the reality of the delivery. The CAST on the parameter — asyncpg.
    """
    sql = re.sub(r"\s+", " ", str(matching.LIVE_CHANNELS_SQL))
    assert "DISTINCT ON (uc.channel)" in sql
    assert "(uc.status = 'active') DESC" in sql
    assert "uc.linked_at DESC NULLS LAST" in sql
    assert "CAST(:tenant_id AS uuid)" in sql


@pytest.mark.asyncio
async def test_live_channels_picks_freshest_active_row_per_channel():
    """Several rows per channel: the liveness is decided by the row given by the SQL first.

    DISTINCT ON gives one row per channel → one element in the set, no duplicate of channels (and of the outbox records) arises.
    """
    session = _FakeSession(
        [_live([_channel_row("telegram", status="active", external_id="fresh")])]
    )

    assert await matching.live_channels(session, "te-1") == {"telegram"}


@pytest.mark.asyncio
async def test_channel_states_keeps_channels_that_were_ever_connected():
    """The difference between “dropped off” and “never connected” — only through channel_states.

    live_channels collapses both cases into “not in the set”; the notice about a drop-off must tell them apart, otherwise it spams users who did not pass the onboarding.
    """
    session = _FakeSession(
        [_live([_channel_row("telegram", status="revoked", external_id="555")])]
    )
    states = await matching.channel_states(session, "te-1")

    assert set(states) == {"telegram"}  # the channel was set up — the key is there
    assert states["telegram"].is_live is False  # but it is dead


@pytest.mark.asyncio
async def test_channel_states_empty_when_no_rows():
    session = _FakeSession([_live([])])
    assert await matching.channel_states(session, "te-1") == {}


@pytest.mark.parametrize(
    "status,external_id,expected",
    [
        ("active", "555", True),
        ("active", "  ", False),  # spaces — not an addressee
        ("active", None, False),
        ("blocked", "555", False),
        ("revoked", "555", False),
        ("pending", "555", False),  # owner: the address is set up, but not confirmed
        (None, "555", False),
    ],
)
def test_channel_state_is_live_mirrors_delivery_fail_predicate(
    status, external_id, expected
):
    """is_live is exactly the inverse of the failure of the delivery “the channel is inactive or there is no target”."""
    state = matching.ChannelState(
        channel="telegram", status=status, external_id=external_id
    )
    assert state.is_live is expected


@pytest.mark.asyncio
async def test_pending_channel_never_reaches_outbox():
    """‼️ owner: routing does not write the outbox for an unconfirmed (``pending``) email.

    It holds the contract on which the whole double opt-in stands: the workers know nothing about ``pending`` and must not know — it is enough for them that it is not ``active``. The test pins the FACT, and does not introduce a new behavior.
    """
    session = _FakeSession(
        [_live([_channel_row("email", status="pending", external_id="u@c.rs")])]
    )

    assert await matching.live_channels(session, "te-1") == set()


# --------------------------------------------------------------------------- #
#  LiveChannelsCache — one SELECT per tenant within a run
# --------------------------------------------------------------------------- #


def _live_selects(session):
    """The calls of the SELECT of the live channels — the INSERTs into the outbox do not get here."""
    return [c for c in session.calls if "user_channels" in c[0]]


@pytest.mark.asyncio
async def test_live_cache_selects_once_for_two_candidates_of_one_tenant():
    """Two candidates of one tenant → exactly one SELECT of the live channels."""
    session = _FakeSession([_live([_channel_row("telegram")])])
    cache = matching.LiveChannelsCache()

    for profile_id in ("p-1", "p-2"):
        await matching.write_outbox(
            session,
            candidate=_cand(profile_id=profile_id, tenant_id="te-1"),
            tender=_tender(),
            live_cache=cache,
        )

    assert len(_live_selects(session)) == 1
    inserts = [c for c in session.calls if "delivery_outbox" in c[0]]
    assert [c[1]["profile_id"] for c in inserts] == ["p-1", "p-2"]  # both are written


@pytest.mark.asyncio
async def test_live_cache_keeps_tenants_apart():
    """Different tenants → separate SELECTs and separate cache records."""
    # _FakeSession gives out the preparations one per EACH execute, including the INSERT:
    # SELECT(te-1) → INSERT(te-1) → SELECT(te-2).
    session = _FakeSession(
        [
            _live([_channel_row("telegram")]),
            _FakeResult([]),
            _live([_channel_row("email", external_id="a@b.rs")]),
        ]
    )
    cache = matching.LiveChannelsCache()

    await matching.write_outbox(
        session,
        candidate=_cand(tenant_id="te-1", delivery_channels=["telegram", "email"]),
        tender=_tender(),
        live_cache=cache,
    )
    await matching.write_outbox(
        session,
        candidate=_cand(tenant_id="te-2", delivery_channels=["telegram", "email"]),
        tender=_tender(),
        live_cache=cache,
    )

    assert [c[1] for c in _live_selects(session)] == [
        {"tenant_id": "te-1"},
        {"tenant_id": "te-2"},
    ]
    inserts = [c for c in session.calls if "delivery_outbox" in c[0]]
    # The cache of te-1 did not substitute the liveness of te-2: each has its own set of channels.
    assert [c[1]["channels"] for c in inserts] == [["telegram"], ["email"]]


@pytest.mark.asyncio
async def test_live_cache_is_per_run_so_unlinked_channel_is_picked_up():
    """A new run — a new cache: the unlinking of a channel is picked up, a stale one does not live.

    The cache lives exactly one run for this: a channel leaves active WITHOUT the user’s participation, while the profile stays active.
    """
    session = _FakeSession([_live([_channel_row("telegram", status="active")])])
    await matching.write_outbox(
        session,
        candidate=_cand(),
        tender=_tender(),
        live_cache=matching.LiveChannelsCache(),
    )
    assert _insert_call(session) is not None

    session = _FakeSession([_live([_channel_row("telegram", status="revoked")])])
    await matching.write_outbox(
        session,
        candidate=_cand(),
        tender=_tender(),
        live_cache=matching.LiveChannelsCache(),
    )
    assert _is_web_only(_insert_call(session))  # run 2 saw the unlinking: cabinet only


@pytest.mark.asyncio
async def test_write_outbox_without_cache_selects_each_call():
    """Without live_cache the behavior is the previous one — a SELECT on each call."""
    session = _FakeSession([_live([_channel_row("telegram")])])

    await matching.write_outbox(session, candidate=_cand(), tender=_tender())
    await matching.write_outbox(session, candidate=_cand(), tender=_tender())

    assert len(_live_selects(session)) == 2


@pytest.mark.asyncio
async def test_stage_c_is_stub_returns_empty():
    session = _FakeSession([])
    assert await matching.stage_c_soft_match(session, _tender()) == []
    assert session.calls == []  # the stub does not go to the DB


# --------------------------------------------------------------------------- #
#  keyword_dict — the per-profile flag of the dictionary expansion (EPIC 3 iteration 3)
# --------------------------------------------------------------------------- #


def test_candidate_keyword_dict_defaults_to_false():
    """Candidate without keyword_dict — backward compatibility: default False."""
    c = Candidate(
        profile_id="p-1",
        tenant_id="te-1",
        keywords_include=None,
        keywords_exclude=None,
        ai_analysis_level=None,
        delivery_channels=["telegram"],
    )
    assert c.keyword_dict is False


@pytest.mark.asyncio
async def test_stage_a_reads_keyword_dict_from_row_true():
    """keyword_dict=True in the DB row → Candidate.keyword_dict is True."""
    row = SimpleNamespace(
        id="p-kd",
        tenant_id="te-1",
        keywords_include=[{"all_of": ["строительство"]}],
        keywords_exclude=None,
        ai_analysis_level="none",
        delivery_channels=["telegram"],
        keyword_dict=True,
    )
    session = _FakeSession([_FakeResult([row])])
    cands = await matching.stage_a_candidates(session, _tender())
    assert len(cands) == 1
    assert cands[0].keyword_dict is True


@pytest.mark.asyncio
async def test_stage_a_reads_keyword_dict_from_row_false():
    """keyword_dict=False in the DB row → Candidate.keyword_dict is False."""
    row = SimpleNamespace(
        id="p-no-kd",
        tenant_id="te-1",
        keywords_include=[{"all_of": ["строительство"]}],
        keywords_exclude=None,
        ai_analysis_level="none",
        delivery_channels=["telegram"],
        keyword_dict=False,
    )
    session = _FakeSession([_FakeResult([row])])
    cands = await matching.stage_a_candidates(session, _tender())
    assert cands[0].keyword_dict is False


def test_stage_a_sql_selects_keyword_dict():
    """STAGE_A_SQL carries p.keyword_dict — otherwise the mapping falls on AttributeError."""
    sql = _sql_norm()
    assert "p.keyword_dict" in sql


def test_stage_b_filter_keyword_dict_enabled_matches_serbian_form():
    """Профиль keyword_dict=True: ru-слово «строительство» → expand → сербская
    форма «грађевинарство» → тендер с сербским текстом матчится."""
    cand = _cand(
        profile_id="kd-on",
        keywords_include=[{"all_of": ["строительство"]}],
        keyword_dict=True,
    )
    tender = _tender(
        title_normalized="грађевинарство",
        description_normalized="набавка материјала",
    )
    out = stage_b_filter([cand], tender)
    assert [c.profile_id for c in out] == ["kd-on"]


def test_stage_b_filter_keyword_dict_disabled_no_expand():
    """Профиль keyword_dict=False: «строительство» НЕ расширяется → сербский
    текст тендера без кириллического перевода не матчится."""
    cand = _cand(
        profile_id="kd-off",
        keywords_include=[{"all_of": ["строительство"]}],
        keyword_dict=False,
    )
    tender = _tender(
        title_normalized="грађевинарство",
        description_normalized="набавка материјала",
    )
    out = stage_b_filter([cand], tender)
    assert out == []


def test_stage_b_filter_two_profiles_independent_keyword_dict():
    """Two candidates with different keyword_dict=True/False in one batch are processed independently: the enabled one matches, the disabled one does not."""
    on = _cand(
        profile_id="kd-on",
        keywords_include=[{"all_of": ["строительство"]}],
        keyword_dict=True,
    )
    off = _cand(
        profile_id="kd-off",
        keywords_include=[{"all_of": ["строительство"]}],
        keyword_dict=False,
    )
    tender = _tender(
        title_normalized="грађевинарство",
        description_normalized="набавка материјала",
    )
    out = stage_b_filter([on, off], tender)
    assert [c.profile_id for c in out] == ["kd-on"]
