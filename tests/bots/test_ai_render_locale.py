"""The owner’s decision of 30.09.2026: the AI wrapper of the bot is rendered in the locale’s language, sr by default.

The assertions are literals. The data (headers, model answers) are not translated.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.bots import analytics_export as ax
from src.bots import awards_analytics_service as aa
from src.bots.bid_verdict_service import _render_bid_verdict
from src.bots.compliance_checklist_service import _render_checklist
from src.bots.deep_analysis_service import _render_l3_chunks
from src.bots.draft_service import _render_draft

pytestmark = pytest.mark.sr_default

CYR = re.compile("[А-Яа-яЁё]")

L3 = {
    "summary": "sažetak",
    "match_score": 4,
    "match_reasons": ["a"],
    "red_flags": ["b"],
    "required_qualifications": ["c"],
    "estimated_effort_days": 5,
    "competition_estimate": "low",
    "work_breakdown": ["d"],
    "participation_checklist": ["e"],
    "focus_answers": [{"question": "q", "answer": "a"}],
}


def test_deep_analysis_three_languages_and_default():
    sr = "\n".join(_render_l3_chunks("Naslov", L3))
    assert "<b>Dubinska analiza</b>" in sr and "~5 dana" in sr and "🟢 Niska" in sr
    assert not CYR.search(sr)
    ru = "\n".join(_render_l3_chunks("Naslov", L3, "ru"))
    assert "<b>Глубокий анализ</b>" in ru and "~5 дней" in ru
    en = "\n".join(_render_l3_chunks("Naslov", L3, "en"))
    assert "<b>Deep analysis</b>" in en and "~5 days" in en


@pytest.mark.parametrize(
    "locale,verdict,pros",
    [
        (None, "Preporučujem", "<b>Za:</b>"),
        ("ru", "Рекомендую", "<b>За:</b>"),
        ("en", "Recommended", "<b>For:</b>"),
    ],
)
def test_bid_verdict_locale(locale, verdict, pros):
    out = _render_bid_verdict(
        "T", {"verdict": "go", "score": 80, "pros": ["x"], "cons": []}, locale
    )
    assert verdict in out and pros in out


def test_checklist_categories_and_titles_localized():
    out = _render_checklist(
        "T",
        {
            "documents": [{"category": "финансы", "title": "Bilans"}],
            "deadlines": [{"label": "Rok", "value": "1.1."}],
            "disqualification_risks": ["r"],
        },
    )
    assert "Šta pripremiti:" in out and "<b>Finansije</b>" in out
    assert "Ključni rokovi:" in out and "Rizici diskvalifikacije:" in out
    ru = _render_checklist(
        "T", {"documents": [{"category": "финансы", "title": "x"}]}, "ru"
    )
    assert "Что подготовить:" in ru and "<b>Финансы</b>" in ru


def test_draft_locale():
    data = {"criteria_mapping": [{"criterion": "Cena", "response": "r"}]}
    assert "Nacrt ponude:" in _render_draft("T", data)
    assert "Povezivanje sa kriterijumima ocenjivanja:" in _render_draft("T", data)
    assert "Bid draft:" in _render_draft("T", data, "en")


def _cust_stats():
    return SimpleNamespace(
        months=None,
        total_awards=3,
        distinct_winners=2,
        with_amount=1,
        total_rsd=Decimal(1000),
        total_eur=Decimal(9),
        concentration_pct=60,
        cr3_pct=None,
        is_monopolized=True,
        top_winners=[SimpleNamespace(name="Firma", wins=2, sum_rsd=Decimal(500))],
    )


def test_customer_analytics_locale():
    sr = aa.render_customer_analytics("N", "123", _cust_stats())
    assert "Analitika naručioca" in sr and "Jedan dobavljač dominira" in sr
    assert "2 pobeda" in sr and "za sve vreme" in sr
    ru = aa.render_customer_analytics("N", "123", _cust_stats(), "ru")
    assert "Аналитика наручиоца" in ru and "2 побед" in ru
    en = aa.render_customer_analytics("N", "123", _cust_stats(), "en")
    assert "2 wins" in en and "all time" in en


def test_forecast_locale():
    row = SimpleNamespace(
        lot_title="Lot",
        winner_name="W",
        expiry_date=date(2027, 1, 2),
        months_until_expiry=3,
        award_amount_rsd=Decimal(10),
    )
    sr = aa.render_reprocurement_forecast("N", "1", [row], 12)
    assert "Očekivane ponovne nabavke" in sr and "Ističe: 02.01.2027 (~3 mes.)" in sr
    assert "Pobednik: W" in sr
    assert "Expires: 02.01.2027 (~3 mo.)" in aa.render_reprocurement_forecast(
        "N", "1", [row], 12, "en"
    )


def test_csv_headers_follow_locale():
    stats = SimpleNamespace(
        customer_pib="1",
        months=None,
        total_awards=1,
        distinct_winners=1,
        with_amount=0,
        total_rsd=Decimal(0),
        total_eur=Decimal(0),
        top_winners=[],
    )
    sr = ax.build_customer_csv(stats).decode("utf-8-sig")
    assert sr.startswith("Naručilac,") or sr.startswith("Naručilac;")
    assert "Ukupno dodela" in sr
    ru = ax.build_customer_csv(stats, locale="ru").decode("utf-8-sig")
    assert "Наручилац" in ru and "Всего наград" in ru
