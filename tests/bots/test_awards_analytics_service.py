"""Tests of the VG-06 service ``src.bots.awards_analytics_service``.

Without a network/DB/AI. We cover: no_account, frozen, gate_pro, no_tender,
no_customer, no_data, success, and also the pure render render_customer_analytics.
``customer_award_stats`` is patched — the service is tested in isolation from the engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.usage import _TIER_LIMITS
from src.analytics.awards_analytics import (
    CompetitorStats,
    CustomerRow,
    CustomerStats,
    WinnerRow,
)

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)


def _session_seq(rows: list) -> MagicMock:
    """Mock session: execute returns .first()=rows[i] in turn."""
    idx = {"n": 0}
    session = MagicMock()

    async def _exec(sql, params=None):
        i = idx["n"]
        idx["n"] += 1
        r = MagicMock()
        r.first.return_value = rows[i] if i < len(rows) else None
        return r

    session.execute = _exec
    return session


_POPULATED = CustomerStats(
    customer_pib="100200300",
    total_awards=12,
    distinct_winners=2,
    with_amount=5,
    total_rsd=Decimal("88000000"),
    total_eur=Decimal("733000"),
    top_winners=[
        WinnerRow(
            name="Монополист д.о.о.", pib="111", wins=9, sum_rsd=Decimal("70000000")
        ),
        WinnerRow(name="Второй", pib="222", wins=3, sum_rsd=Decimal("18000000")),
    ],
    months=None,
)

_EMPTY = CustomerStats(
    customer_pib="100200300",
    total_awards=0,
    distinct_winners=0,
    with_amount=0,
    total_rsd=Decimal("0"),
    total_eur=Decimal("0"),
    top_winners=[],
    months=None,
)


@pytest.mark.asyncio
async def test_no_account():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = MagicMock()
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=None),
    ):
        result = await run_customer_analytics(session, tender_id=_TENDER_ID, chat_id=1)
    assert result.status == "no_account"


@pytest.mark.asyncio
async def test_frozen():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq([SimpleNamespace(tier="pro", access_until=_PAST)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "frozen"


@pytest.mark.asyncio
async def test_gate_pro_starter():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq([SimpleNamespace(tier="starter", access_until=_FUTURE)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID, tier_limits=_TIER_LIMITS
        )
    assert result.status == "gate_pro"


@pytest.mark.asyncio
async def test_no_tender():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),  # tier
            None,  # tender not found
        ]
    )
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_tender"


@pytest.mark.asyncio
async def test_no_customer_when_pib_and_name_empty():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(customer_name=None, customer_tax_id=None, title="T"),
        ]
    )
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_customer"


@pytest.mark.asyncio
async def test_success_by_name_when_no_pib():
    """PIB is empty, the name is present → analytics by name, success."""
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(
                customer_name="ЈКП Топлана", customer_tax_id=None, title="T"
            ),
        ]
    )
    with (
        patch(
            "src.bots.awards_analytics_service.resolve_tenant_id",
            new=AsyncMock(return_value=_TENANT_ID),
        ),
        patch(
            "src.bots.awards_analytics_service.customer_award_stats",
            new=AsyncMock(return_value=_POPULATED),
        ) as mock_stats,
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "success"
    # the key passed by name, PIB=None
    _, kwargs = mock_stats.call_args
    assert kwargs["customer_name"] == "ЈКП Топлана"
    assert kwargs["customer_pib"] is None


@pytest.mark.asyncio
async def test_no_data_when_stats_empty():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(
                customer_name="ЈКП", customer_tax_id="100200300", title="T"
            ),
        ]
    )
    with (
        patch(
            "src.bots.awards_analytics_service.resolve_tenant_id",
            new=AsyncMock(return_value=_TENANT_ID),
        ),
        patch(
            "src.bots.awards_analytics_service.customer_award_stats",
            new=AsyncMock(return_value=_EMPTY),
        ),
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_success_renders_html():
    from src.bots.awards_analytics_service import run_customer_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(
                customer_name="ЈКП Водовод", customer_tax_id="100200300", title="T"
            ),
        ]
    )
    with (
        patch(
            "src.bots.awards_analytics_service.resolve_tenant_id",
            new=AsyncMock(return_value=_TENANT_ID),
        ),
        patch(
            "src.bots.awards_analytics_service.customer_award_stats",
            new=AsyncMock(return_value=_POPULATED),
        ),
    ):
        result = await run_customer_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "success"
    assert "🏛" in result.html
    assert "ЈКП Водовод" in result.html
    assert "Монополист" in result.html
    assert "100200300" in result.html


# --------------------------------------------------------------------------- #
#  render_customer_analytics — a pure function
# --------------------------------------------------------------------------- #


def test_render_contains_summary_and_top():
    from src.bots.awards_analytics_service import render_customer_analytics

    html = render_customer_analytics("ЈКП Водовод", "100200300", _POPULATED)
    assert "Аналитика наручиоца" in html
    assert "ЈКП Водовод" in html
    assert "12" in html  # total_awards
    assert "Монополист" in html
    # the share of top-1: 9/12 = 75%
    assert "75%" in html
    assert "RSD" in html


def test_render_no_amount_hides_money_line():
    from src.bots.awards_analytics_service import render_customer_analytics

    stats = CustomerStats(
        customer_pib="1",
        total_awards=4,
        distinct_winners=4,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[WinnerRow(name="A", pib="a", wins=1, sum_rsd=Decimal("0"))],
        months=None,
    )
    html = render_customer_analytics("X", "1", stats)
    assert "Сумма" not in html  # there are no rows with a known price → the amount block is hidden


def test_render_escapes_html():
    from src.bots.awards_analytics_service import render_customer_analytics

    stats = CustomerStats(
        customer_pib="1",
        total_awards=1,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[
            WinnerRow(name="A & <b>B</b>", pib="a", wins=1, sum_rsd=Decimal("0"))
        ],
        months=None,
    )
    html = render_customer_analytics("Заказчик & Ко <test>", "1", stats)
    assert "&amp;" in html
    assert "&lt;" in html


# --------------------------------------------------------------------------- #
#  render_customer_analytics — concentration (STAGE 2)
# --------------------------------------------------------------------------- #


def test_render_concentration_monopoly():
    from src.bots.awards_analytics_service import render_customer_analytics

    stats = CustomerStats(
        customer_pib="1",
        total_awards=10,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[
            WinnerRow(name="Монополист", pib="m", wins=10, sum_rsd=Decimal("0"))
        ],
        months=None,
    )
    out = render_customer_analytics("X", "1", stats)
    assert "Концентрация" in out
    assert "топ-1" in out
    assert "Один поставщик доминирует" in out


def test_render_concentration_cr3_shown_when_multiple_winners():
    from src.bots.awards_analytics_service import render_customer_analytics

    stats = CustomerStats(
        customer_pib="1",
        total_awards=10,
        distinct_winners=4,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[
            WinnerRow(name="A", pib="a", wins=4, sum_rsd=Decimal("0")),
            WinnerRow(name="B", pib="b", wins=3, sum_rsd=Decimal("0")),
            WinnerRow(name="C", pib="c", wins=2, sum_rsd=Decimal("0")),
        ],
        months=None,
    )
    out = render_customer_analytics("X", "1", stats)
    assert "топ-3" in out  # cr3_pct must be output


# --------------------------------------------------------------------------- #
#  render_competitor_analytics — a pure function (STAGE 2)
# --------------------------------------------------------------------------- #

_COMP_POPULATED = CompetitorStats(
    winner_pib="999",
    total_wins=15,
    distinct_customers=5,
    with_amount=8,
    total_rsd=Decimal("60000000"),
    total_eur=Decimal("500000"),
    top_customers=[
        CustomerRow(name="ЈКП Водовод", pib="aaa", wins=6, sum_rsd=Decimal("30000000")),
        CustomerRow(
            name="Болница Нови Сад", pib="bbb", wins=4, sum_rsd=Decimal("20000000")
        ),
    ],
    months=None,
)

_COMP_EMPTY = CompetitorStats(
    winner_pib="000",
    total_wins=0,
    distinct_customers=0,
    with_amount=0,
    total_rsd=Decimal("0"),
    total_eur=Decimal("0"),
    top_customers=[],
    months=12,
)


def test_render_competitor_populated():
    from src.bots.awards_analytics_service import render_competitor_analytics

    out = render_competitor_analytics("Победитель д.о.о.", "999", _COMP_POPULATED)
    assert "🥊" in out
    assert "Победитель" in out
    assert "999" in out
    assert "15" in out  # total_wins
    assert "ЈКП Водовод" in out
    assert "RSD" in out
    assert "за всё время" in out


def test_render_competitor_no_top_customers():
    from src.bots.awards_analytics_service import render_competitor_analytics

    out = render_competitor_analytics("X", "000", _COMP_EMPTY)
    assert "🥊" in out
    assert "за 12 мес." in out
    assert "Чаще всего" not in out  # there are no customers → the top block is absent


def test_render_competitor_escapes_html():
    from src.bots.awards_analytics_service import render_competitor_analytics

    stats = CompetitorStats(
        winner_pib="1",
        total_wins=1,
        distinct_customers=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_customers=[
            CustomerRow(name="A & <b>B</b>", pib="x", wins=1, sum_rsd=Decimal("0"))
        ],
        months=None,
    )
    out = render_competitor_analytics("Победитель & <X>", "1", stats)
    assert "&amp;" in out
    assert "&lt;" in out


# --------------------------------------------------------------------------- #
#  run_competitor_analytics — statuses (STAGE 2)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_competitor_gate_pro():
    from src.bots.awards_analytics_service import run_competitor_analytics

    session = _session_seq([SimpleNamespace(tier="free", access_until=_FUTURE)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_competitor_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID, tier_limits=_TIER_LIMITS
        )
    assert result.status == "gate_pro"


@pytest.mark.asyncio
async def test_competitor_frozen():
    from src.bots.awards_analytics_service import run_competitor_analytics

    session = _session_seq([SimpleNamespace(tier="pro", access_until=_PAST)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_competitor_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "frozen"


@pytest.mark.asyncio
async def test_competitor_no_data_winner_pib_empty():
    """The top winner exists, but the PIB is empty → no_data."""
    from src.bots.awards_analytics_service import run_competitor_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(
                customer_name="ЈКП", customer_tax_id="100200300", title="T"
            ),
        ]
    )
    stats_no_pib = CustomerStats(
        customer_pib="100200300",
        total_awards=5,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[WinnerRow(name="Аноним", pib=None, wins=5, sum_rsd=Decimal("0"))],
        months=None,
    )
    with (
        patch(
            "src.bots.awards_analytics_service.resolve_tenant_id",
            new=AsyncMock(return_value=_TENANT_ID),
        ),
        patch(
            "src.bots.awards_analytics_service.customer_award_stats",
            new=AsyncMock(return_value=stats_no_pib),
        ),
    ):
        result = await run_competitor_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_competitor_no_data_empty_customer_stats():
    """The customer is found, but there are no awards (top_winners is empty) → no_data."""
    from src.bots.awards_analytics_service import run_competitor_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(
                customer_name="ЈКП", customer_tax_id="100200300", title="T"
            ),
        ]
    )
    with (
        patch(
            "src.bots.awards_analytics_service.resolve_tenant_id",
            new=AsyncMock(return_value=_TENANT_ID),
        ),
        patch(
            "src.bots.awards_analytics_service.customer_award_stats",
            new=AsyncMock(return_value=_EMPTY),
        ),
    ):
        result = await run_competitor_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_competitor_success():
    from src.bots.awards_analytics_service import run_competitor_analytics

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            SimpleNamespace(
                customer_name="ЈКП Водовод", customer_tax_id="100200300", title="T"
            ),
        ]
    )
    with (
        patch(
            "src.bots.awards_analytics_service.resolve_tenant_id",
            new=AsyncMock(return_value=_TENANT_ID),
        ),
        patch(
            "src.bots.awards_analytics_service.customer_award_stats",
            new=AsyncMock(return_value=_POPULATED),
        ),
        patch(
            "src.bots.awards_analytics_service.competitor_stats",
            new=AsyncMock(return_value=_COMP_POPULATED),
        ),
    ):
        result = await run_competitor_analytics(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "success"
    assert "🥊" in result.html
    assert "Монополист" in result.html  # top_winners[0].name from _POPULATED


# --------------------------------------------------------------------------- #
#  _parse_months — parsing of the callback suffix (STAGE 2)
# --------------------------------------------------------------------------- #


def test_parse_months_empty_string():
    from src.bots.telegram import _parse_months

    assert _parse_months("") is None


def test_parse_months_zero():
    from src.bots.telegram import _parse_months

    assert _parse_months("0") is None


def test_parse_months_12():
    from src.bots.telegram import _parse_months

    assert _parse_months("12") == 12


def test_parse_months_36():
    from src.bots.telegram import _parse_months

    assert _parse_months("36") == 36


def test_parse_months_invalid():
    from src.bots.telegram import _parse_months

    assert _parse_months("abc") is None


def test_parse_months_negative():
    from src.bots.telegram import _parse_months

    assert _parse_months("-1") is None
