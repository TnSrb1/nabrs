"""Tests of the VG-07 phase-1 service — run_reprocurement_forecast + render.

Without a network/DB/AI. We cover the gate paths (no_account, frozen, gate_pro, no_tender,
no_customer, no_data, success) and render_reprocurement_forecast.
get_reprocurement_forecast is patched — the service is isolated from the engine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.usage import _TIER_LIMITS
from src.analytics.reprocurement import ReprocurementRow
from src.bots.awards_analytics_service import render_reprocurement_forecast

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)


def _session_seq(rows: list) -> MagicMock:
    idx = {"n": 0}
    session = MagicMock()

    async def _exec(sql, params=None):
        i = idx["n"]
        idx["n"] += 1
        r = MagicMock()
        r.first.return_value = rows[i] if i < len(rows) else None
        r.fetchall.return_value = []
        return r

    session.execute = _exec
    return session


_SAMPLE_ROW = ReprocurementRow(
    customer_pib="100200300",
    customer_name="ЈКП Водовод",
    lot_title="Набавка цеви ДН 200",
    winner_name="Добавр д.о.о.",
    award_amount_rsd=Decimal("5000000"),
    decision_date=date(2025, 1, 15),
    expiry_date=date.today() + timedelta(days=180),
    months_until_expiry=6,
)


# --------------------------------------------------------------------------- #
#  Gate paths
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repro_no_account():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

    session = MagicMock()
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=None),
    ):
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_account"


@pytest.mark.asyncio
async def test_repro_frozen():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

    session = _session_seq([SimpleNamespace(tier="pro", access_until=_PAST)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "frozen"


@pytest.mark.asyncio
async def test_repro_gate_pro():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

    session = _session_seq([SimpleNamespace(tier="starter", access_until=_FUTURE)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID, tier_limits=_TIER_LIMITS
        )
    assert result.status == "gate_pro"


@pytest.mark.asyncio
async def test_repro_no_tender():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            None,  # tender not found
        ]
    )
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_tender"


@pytest.mark.asyncio
async def test_repro_no_customer():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

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
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_customer"


@pytest.mark.asyncio
async def test_repro_no_data():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

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
            "src.analytics.reprocurement.get_reprocurement_forecast",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_repro_success():
    from src.bots.awards_analytics_service import run_reprocurement_forecast

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
            "src.analytics.reprocurement.get_reprocurement_forecast",
            new=AsyncMock(return_value=[_SAMPLE_ROW]),
        ),
    ):
        result = await run_reprocurement_forecast(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "success"
    assert "🔮" in result.html
    assert "Набавка цеви ДН 200" in result.html
    assert "Добавр д.о.о." in result.html


# --------------------------------------------------------------------------- #
#  render_reprocurement_forecast
# --------------------------------------------------------------------------- #


def test_render_contains_header():
    html = render_reprocurement_forecast("ЈКП Водовод", "100200300", [_SAMPLE_ROW])
    assert "🔮" in html
    assert "Ожидаемые перезакупки" in html


def test_render_contains_lot_title():
    html = render_reprocurement_forecast("ЈКП", "1", [_SAMPLE_ROW])
    assert "Набавка цеви ДН 200" in html


def test_render_contains_winner():
    html = render_reprocurement_forecast("ЈКП", "1", [_SAMPLE_ROW])
    assert "Добавр д.о.о." in html


def test_render_contains_expiry_date():
    html = render_reprocurement_forecast("ЈКП", "1", [_SAMPLE_ROW])
    assert _SAMPLE_ROW.expiry_date.strftime("%d.%m.%Y") in html


def test_render_contains_amount_when_set():
    html = render_reprocurement_forecast("ЈКП", "1", [_SAMPLE_ROW])
    assert "5 000 000 RSD" in html


def test_render_no_amount_when_none():
    row = ReprocurementRow(
        customer_pib="1",
        customer_name="X",
        lot_title="Т",
        winner_name="W",
        award_amount_rsd=None,
        decision_date=None,
        expiry_date=date.today() + timedelta(days=30),
        months_until_expiry=1,
    )
    html_text = render_reprocurement_forecast("X", "1", [row])
    assert "RSD" not in html_text


def test_render_no_data_graceful_text_included():
    """The hint about partial coverage is always present."""
    html = render_reprocurement_forecast("ЈКП", "1", [_SAMPLE_ROW])
    assert "частично заполнены" in html


def test_render_horizon_shown():
    html = render_reprocurement_forecast("ЈКП", "1", [_SAMPLE_ROW], horizon_months=6)
    assert "6 мес." in html


def test_render_empty_rows_still_renders():
    """render with an empty list does not fall (the service does not allow it, but just in case)."""
    html = render_reprocurement_forecast("ЈКП", "1", [])
    assert "🔮" in html
