"""Tests of VG-06 E3a: analytics_export.py + the run_*_export service.

Without a network/DB/AI. Coverage:
- build_customer_csv / build_competitor_csv: BOM, separator, escaping,
  the number of rows, file names.
- run_customer_export / run_competitor_export: all statuses no_account/gate_pro/
  frozen/no_tender/no_customer/no_data/success.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.analytics.awards_analytics import (
    CompetitorStats,
    CustomerRow,
    CustomerStats,
    WinnerRow,
)
from src.ai.usage import _TIER_LIMITS

# --------------------------------------------------------------------------- #
#  Fixtures
# --------------------------------------------------------------------------- #

_TENDER_ID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_TENANT_ID = "dddd4444-dddd-dddd-dddd-dddddddddddd"
_CHAT_ID = 123456789

_NOW = datetime.now(timezone.utc)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=365)

_CUSTOMER_STATS = CustomerStats(
    customer_pib="100200300",
    total_awards=20,
    distinct_winners=4,
    with_amount=12,
    total_rsd=Decimal("55000000"),
    total_eur=Decimal("460000"),
    top_winners=[
        WinnerRow(name="Победитель А", pib="111", wins=10, sum_rsd=Decimal("30000000")),
        WinnerRow(name="Победитель Б", pib="222", wins=7, sum_rsd=Decimal("15000000")),
        WinnerRow(
            name="Компания; с точкой", pib="333", wins=2, sum_rsd=Decimal("8000000")
        ),
        WinnerRow(name='С "кавычкой"', pib="444", wins=1, sum_rsd=Decimal("2000000")),
    ],
    months=12,
)

_COMPETITOR_STATS = CompetitorStats(
    winner_pib="999",
    total_wins=30,
    distinct_customers=6,
    with_amount=15,
    total_rsd=Decimal("100000000"),
    total_eur=Decimal("830000"),
    top_customers=[
        CustomerRow(name="Заказчик А", pib="aaa", wins=12, sum_rsd=Decimal("50000000")),
        CustomerRow(name="Заказчик Б", pib="bbb", wins=8, sum_rsd=Decimal("30000000")),
        CustomerRow(name="Заказчик В", pib="ccc", wins=5, sum_rsd=Decimal("12000000")),
    ],
    months=None,
)

_EMPTY_CUSTOMER = CustomerStats(
    customer_pib="100200300",
    total_awards=0,
    distinct_winners=0,
    with_amount=0,
    total_rsd=Decimal("0"),
    total_eur=Decimal("0"),
    top_winners=[],
    months=None,
)


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


# --------------------------------------------------------------------------- #
#  build_customer_csv
# --------------------------------------------------------------------------- #


def test_customer_csv_has_bom():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="ЈКП Водовод")
    assert data[:3] == b"\xef\xbb\xbf"


def test_customer_csv_semicolon_delimiter():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="ЈКП Водовод")
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    # Summary: 8 key-value pairs + an empty row + a header + 4 data rows
    assert len(rows) == 8 + 1 + 1 + len(_CUSTOMER_STATS.top_winners)


def test_customer_csv_row_count():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="ЈКП")
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    # 8 summary rows + 1 empty + 1 header + N top
    expected = 8 + 1 + 1 + len(_CUSTOMER_STATS.top_winners)
    assert len(rows) == expected


def test_customer_csv_escapes_semicolon_in_name():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="ЈКП")
    text = data.decode("utf-8-sig")
    # “Company; with a dot” must be enclosed in quotes per RFC 4180
    assert '"Компания; с точкой"' in text


def test_customer_csv_escapes_quotes():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="ЈКП")
    text = data.decode("utf-8-sig")
    # “With “quote”” → RFC 4180 double quotes
    assert '""' in text


def test_customer_csv_contains_pib_and_period():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="X")
    text = data.decode("utf-8-sig")
    assert "100200300" in text
    assert "12 мес." in text


def test_customer_csv_crlf_line_endings():
    from src.bots.analytics_export import build_customer_csv

    data = build_customer_csv(_CUSTOMER_STATS, customer_name="X")
    # After the BOM all the rows must end with CRLF
    raw = data[3:]  # strip BOM bytes
    assert b"\r\n" in raw


# --------------------------------------------------------------------------- #
#  customer_csv_filename
# --------------------------------------------------------------------------- #


def test_customer_filename_has_pib():
    from src.bots.analytics_export import customer_csv_filename

    name = customer_csv_filename(_CUSTOMER_STATS)
    assert "100200300" in name
    assert name.endswith(".csv")


def test_customer_filename_period_suffix_months():
    from src.bots.analytics_export import customer_csv_filename

    name = customer_csv_filename(_CUSTOMER_STATS)
    assert "12m" in name


def test_customer_filename_period_suffix_all():
    from src.bots.analytics_export import customer_csv_filename

    stats = CustomerStats(
        customer_pib="123",
        total_awards=1,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[],
        months=None,
    )
    name = customer_csv_filename(stats)
    assert "all" in name


def test_customer_filename_safe_ascii_only():
    from src.bots.analytics_export import customer_csv_filename

    stats = CustomerStats(
        customer_pib="",
        total_awards=1,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal("0"),
        total_eur=Decimal("0"),
        top_winners=[],
        months=None,
    )
    name = customer_csv_filename(stats, customer_name="ЈКП Водовод")
    # Cyrillic must not get into the file name (_safe_ascii is used)
    assert name.isascii()
    assert name.endswith(".csv")


# --------------------------------------------------------------------------- #
#  build_competitor_csv
# --------------------------------------------------------------------------- #


def test_competitor_csv_has_bom():
    from src.bots.analytics_export import build_competitor_csv

    data = build_competitor_csv(_COMPETITOR_STATS, winner_name="ОАО Победитель")
    assert data[:3] == b"\xef\xbb\xbf"


def test_competitor_csv_row_count():
    from src.bots.analytics_export import build_competitor_csv

    data = build_competitor_csv(_COMPETITOR_STATS, winner_name="X")
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(reader)
    # 8 summary rows + 1 empty + 1 header + N top
    expected = 8 + 1 + 1 + len(_COMPETITOR_STATS.top_customers)
    assert len(rows) == expected


def test_competitor_csv_all_time_period():
    from src.bots.analytics_export import build_competitor_csv

    data = build_competitor_csv(_COMPETITOR_STATS, winner_name="X")
    text = data.decode("utf-8-sig")
    assert "всё время" in text


def test_competitor_filename_has_pib():
    from src.bots.analytics_export import competitor_csv_filename

    name = competitor_csv_filename(_COMPETITOR_STATS)
    assert "999" in name
    assert name.endswith(".csv")


def test_competitor_filename_all_suffix():
    from src.bots.analytics_export import competitor_csv_filename

    name = competitor_csv_filename(_COMPETITOR_STATS)
    assert "all" in name


# --------------------------------------------------------------------------- #
#  run_customer_export — statuses
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_export_customer_no_account():
    from src.bots.awards_analytics_service import run_customer_export

    session = MagicMock()
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=None),
    ):
        result = await run_customer_export(session, tender_id=_TENDER_ID, chat_id=1)
    assert result.status == "no_account"
    assert result.content == b""


@pytest.mark.asyncio
async def test_export_customer_frozen():
    from src.bots.awards_analytics_service import run_customer_export

    session = _session_seq([SimpleNamespace(tier="pro", access_until=_PAST)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_customer_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "frozen"


@pytest.mark.asyncio
async def test_export_customer_gate_pro():
    from src.bots.awards_analytics_service import run_customer_export

    session = _session_seq([SimpleNamespace(tier="starter", access_until=_FUTURE)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_customer_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID, tier_limits=_TIER_LIMITS
        )
    assert result.status == "gate_pro"


@pytest.mark.asyncio
async def test_export_customer_no_tender():
    from src.bots.awards_analytics_service import run_customer_export

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
        result = await run_customer_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_tender"


@pytest.mark.asyncio
async def test_export_customer_no_customer():
    from src.bots.awards_analytics_service import run_customer_export

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
        result = await run_customer_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_customer"


@pytest.mark.asyncio
async def test_export_customer_no_data():
    from src.bots.awards_analytics_service import run_customer_export

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
            new=AsyncMock(return_value=_EMPTY_CUSTOMER),
        ),
    ):
        result = await run_customer_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_export_customer_success():
    from src.bots.awards_analytics_service import run_customer_export

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
            new=AsyncMock(return_value=_CUSTOMER_STATS),
        ),
    ):
        result = await run_customer_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "success"
    assert result.filename.endswith(".csv")
    assert result.content[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    assert b"100200300" in result.content


@pytest.mark.asyncio
async def test_export_customer_uses_export_top_n():
    """The export must call customer_award_stats with top_n=EXPORT_TOP_N."""
    from src.bots.analytics_export import EXPORT_TOP_N
    from src.bots.awards_analytics_service import run_customer_export

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
            new=AsyncMock(return_value=_CUSTOMER_STATS),
        ) as mock_stats,
    ):
        await run_customer_export(session, tender_id=_TENDER_ID, chat_id=_CHAT_ID)
    _, kwargs = mock_stats.call_args
    assert kwargs.get("top_n") == EXPORT_TOP_N


# --------------------------------------------------------------------------- #
#  run_competitor_export — statuses
# --------------------------------------------------------------------------- #

_POPULATED_CUSTOMER = CustomerStats(
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

_EMPTY_COMP = CompetitorStats(
    winner_pib="000",
    total_wins=0,
    distinct_customers=0,
    with_amount=0,
    total_rsd=Decimal("0"),
    total_eur=Decimal("0"),
    top_customers=[],
    months=None,
)


@pytest.mark.asyncio
async def test_export_competitor_gate_pro():
    from src.bots.awards_analytics_service import run_competitor_export

    session = _session_seq([SimpleNamespace(tier="free", access_until=_FUTURE)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID, tier_limits=_TIER_LIMITS
        )
    assert result.status == "gate_pro"


@pytest.mark.asyncio
async def test_export_competitor_frozen():
    from src.bots.awards_analytics_service import run_competitor_export

    session = _session_seq([SimpleNamespace(tier="pro", access_until=_PAST)])
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "frozen"


@pytest.mark.asyncio
async def test_export_competitor_no_tender():
    from src.bots.awards_analytics_service import run_competitor_export

    session = _session_seq(
        [
            SimpleNamespace(tier="pro", access_until=_FUTURE),
            None,
        ]
    )
    with patch(
        "src.bots.awards_analytics_service.resolve_tenant_id",
        new=AsyncMock(return_value=_TENANT_ID),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_tender"


@pytest.mark.asyncio
async def test_export_competitor_no_customer():
    from src.bots.awards_analytics_service import run_competitor_export

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
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_customer"


@pytest.mark.asyncio
async def test_export_competitor_no_data_empty_winners():
    from src.bots.awards_analytics_service import run_competitor_export

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
            new=AsyncMock(return_value=_EMPTY_CUSTOMER),
        ),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_export_competitor_no_data_winner_pib_empty():
    from src.bots.awards_analytics_service import run_competitor_export

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
            new=AsyncMock(return_value=stats_no_pib),
        ),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_export_competitor_no_data_empty_comp():
    from src.bots.awards_analytics_service import run_competitor_export

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
            new=AsyncMock(return_value=_POPULATED_CUSTOMER),
        ),
        patch(
            "src.bots.awards_analytics_service.competitor_stats",
            new=AsyncMock(return_value=_EMPTY_COMP),
        ),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "no_data"


@pytest.mark.asyncio
async def test_export_competitor_success():
    from src.bots.awards_analytics_service import run_competitor_export

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
            new=AsyncMock(return_value=_POPULATED_CUSTOMER),
        ),
        patch(
            "src.bots.awards_analytics_service.competitor_stats",
            new=AsyncMock(return_value=_COMPETITOR_STATS),
        ),
    ):
        result = await run_competitor_export(
            session, tender_id=_TENDER_ID, chat_id=_CHAT_ID
        )
    assert result.status == "success"
    assert result.filename.endswith(".csv")
    assert result.content[:3] == b"\xef\xbb\xbf"
    assert b"999" in result.content


# --------------------------------------------------------------------------- #
#  The audit of the callback ≤ 64 bytes (an:exc / an:exw)
# --------------------------------------------------------------------------- #

_UUID = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.parametrize("months_str", ["0", "12", "36"])
@pytest.mark.parametrize("prefix", ["an:exc:", "an:exw:"])
def test_export_callbacks_within_64_bytes(prefix, months_str):
    """the callback an:exc/an:exw ≤ 64 bytes for any period."""
    cb = f"{prefix}{_UUID}:{months_str}"
    length = len(cb.encode())
    assert length <= 64, f"callback '{cb}' = {length} > 64"


@pytest.mark.asyncio
async def test_export_competitor_uses_export_top_n():
    """The export of the competitor must call competitor_stats with top_n=EXPORT_TOP_N."""
    from src.bots.analytics_export import EXPORT_TOP_N
    from src.bots.awards_analytics_service import run_competitor_export

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
            new=AsyncMock(return_value=_POPULATED_CUSTOMER),
        ),
        patch(
            "src.bots.awards_analytics_service.competitor_stats",
            new=AsyncMock(return_value=_COMPETITOR_STATS),
        ) as mock_comp,
    ):
        await run_competitor_export(session, tender_id=_TENDER_ID, chat_id=_CHAT_ID)
    _, kwargs = mock_comp.call_args
    assert kwargs.get("top_n") == EXPORT_TOP_N
