"""Tests of the route ``src/api/cabinet/routes_tender_ai.py`` (the view of AI artifacts).

DB/session/service are mocked (ADR-013). The template renders for real — we check the session gate, the mandatory profile_id, a 404 on a foreign/non-existent one, and the server render of each level + the empty state. The service logic is not duplicated (see test_tender_ai_service.py).
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet import customer_analytics_service as cust
from src.api.cabinet import routes_tender_ai
from src.api.cabinet.tender_ai_service import TenderAnalyses
from src.api.main import app
from tests.api.conftest import FakeSession

SESS = {"uid": "u-1", "tid": "t-1"}
_TENDER = "22222222-2222-2222-2222-222222222222"
_PROFILE = "33333333-3333-3333-3333-333333333333"


def _data(outputs):
    return TenderAnalyses(
        tender_id=_TENDER,
        profile_id=_PROFILE,
        profile_name="ИТ набавке",
        title="Набавка рачунара",
        customer_name="Општина Нови Сад",
        deadline_at=datetime(2026, 3, 15, 12, 0, tzinfo=timezone.utc),
        source_url="https://portal.rs/t/1",
        outputs=outputs,
    )


def _client(*, sess=None):
    def factory():
        return FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: sess
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _async(return_value):
    async def _stub(*args, **kwargs):
        return return_value

    return _stub


# --------------------------------------------------------------------------- #


def test_anonymous_redirected_to_login():
    resp = _client(sess=None).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 303
    # the “📂 Open in the cabinet” link from Telegram: after login return to the card
    assert resp.headers["location"] == (
        "/login?next=%2Fapp%2Ftenders%2F22222222-2222-2222-2222-222222222222"
        "%3Fprofile_id%3D33333333-3333-3333-3333-333333333333"
    )


def test_login_form_carries_tender_return_path_and_drops_foreign_next():
    """The login form puts the return path into a hidden field; a foreign host — is discarded."""
    client = _client(sess=None)
    back = f"/app/tenders/{_TENDER}?profile_id={_PROFILE}"
    ok = client.get("/login", params={"next": back})
    assert ok.status_code == 200
    assert f'name="next" value="{back}"' in ok.text
    evil = client.get("/login", params={"next": "https://evil.example/app"})
    assert 'name="next"' not in evil.text


def test_missing_profile_id_404():
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}")
    assert resp.status_code == 404
    assert "не существует" in resp.text or "не найден" in resp.text


def test_foreign_or_missing_returns_404(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(None))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 404


def test_renders_bid_verdict(monkeypatch):
    data = _data(
        {
            "bid": {
                "verdict": "go",
                "score": 80,
                "pros": ["профильно"],
                "cons": ["короткий срок"],
            }
        }
    )
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "Набавка рачунара" in resp.text
    assert "Рекомендую" in resp.text
    # score chrome: number + “/100” + a visual bar-scale whose width = the score
    assert ">80</span>" in resp.text
    assert "/100" in resp.text
    assert "verdict--go" in resp.text
    assert "score-meter__fill--go" in resp.text
    assert "width: 80%" in resp.text
    assert "профильно" in resp.text
    assert "короткий срок" in resp.text


def test_renders_checklist(monkeypatch):
    data = _data(
        {
            "checklist": {
                "documents": [
                    {"title": "Извод из АПР", "category": "юридич.", "mandatory": True}
                ],
                "deadlines": [{"label": "Подача", "value": "до 12:00 15.03.2026"}],
                "disqualification_risks": ["нет гаранције"],
                "summary": "Готово",
            }
        }
    )
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "Извод из АПР" in resp.text
    assert "до 12:00 15.03.2026" in resp.text
    assert "нет гаранције" in resp.text


def test_renders_draft(monkeypatch):
    data = _data(
        {
            "draft": {
                "sections": [{"heading": "Увод", "body": "Prvi red\nDrugi red"}],
                "criteria_mapping": [{"criterion": "cena", "response": "najniža"}],
                "disclaimer": "Аутоматски нацрт, проверити пре предаје.",
            }
        }
    )
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "Увод" in resp.text
    assert "Prvi red" in resp.text
    assert "Drugi red" in resp.text  # the multi-line body is saved line by line
    assert "najniža" in resp.text
    # one visible caveat — static; the model’s text is not shown (audit 08.10, E-F8)
    assert "проверити пре предаје" not in resp.text
    assert (
        resp.text.count("AI может ошибаться — сверяйте с документацией закупки.") == 1
    )
    # the toolbar copy/download + the hidden text source + the connected JS
    assert "data-copy" in resp.text
    assert 'data-download="chernovik-zayavki.txt"' in resp.text
    assert "data-artifact-src" in resp.text
    assert "/static/js/ai_artifacts.js?v=" in resp.text


def test_renders_deep_analysis(monkeypatch):
    data = _data(
        {
            "standard": {
                "summary": "Резиме анализе",
                "match_score": 4,
                "red_flags": ["кратак рок"],
                "required_qualifications": ["ИСО 9001"],
            }
        }
    )
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "Резиме анализе" in resp.text
    assert "4/5" in resp.text
    assert "кратак рок" in resp.text
    assert "ИСО 9001" in resp.text
    # a deep analysis without a caveat — previously the client saw the model’s output raw (audit 08.10)
    assert (
        resp.text.count("AI может ошибаться — сверяйте с документацией закупки.") == 1
    )


def test_deep_button_on_card_until_deep_exists(monkeypatch):
    post = f'hx-post="/app/tenders/{_TENDER}/generate/deep?profile_id={_PROFILE}"'
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert 'id="ai-slot-deep"' in resp.text
    assert post in resp.text
    assert "Глубокий разбор" in resp.text
    assert "deep генерируется авто-пайплайном" not in resp.text
    # A ready deep is shown by the analysis, there is no second button; one caveat only.
    data = _data({"deep": {"summary": "Итог deep", "match_score": 5}})
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(data))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert "Итог deep" in resp.text
    assert post not in resp.text


def test_renders_empty_states_when_no_artifacts(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    # Sections are present, but each in the empty state.
    assert "Вердикт по участию ещё не сгенерирован" in resp.text
    assert "Чек-лист документов ещё не сгенерирован" in resp.text
    assert "Черновик заявки ещё не сгенерирован" in resp.text


# --------------------------------------------------------------------------- #
#  Customer analytics (web-ai-4): competitor and re-procurement sections
# --------------------------------------------------------------------------- #


def _bundle(*, customer, competitor, reprocurement, incumbent=None):
    return cust.NarucilacAnalyticsBundle(
        customer=customer,
        competitor=competitor,
        reprocurement=reprocurement,
        incumbent=incumbent or cust.IncumbentView(status="no_data"),
    )


def test_renders_competitor_and_reprocurement_success(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _bundle(
        customer=cust.CustomerAnalyticsView(status="no_data"),
        competitor=cust.CompetitorAnalyticsView(
            status="success",
            winner_name="ДОО Алфа",
            winner_pib="111",
            total_wins=12,
            distinct_customers=4,
            with_amount=9,
            total_rsd_fmt="90 000 000",
            total_eur_fmt="760 000",
            top_customers=[
                cust.RivalView(
                    rank=1,
                    name="Општина Нови Сад",
                    wins=6,
                    sum_rsd_fmt="50 000 000 RSD",
                )
            ],
        ),
        reprocurement=cust.ReprocurementForecastView(
            status="success",
            customer_name="Општина Нови Сад",
            customer_pib="100236236",
            horizon_months=12,
            items=[
                cust.ReprocurementItemView(
                    rank=1,
                    lot_title="Канцеларијски материјал",
                    winner_name="ДОО Бета",
                    expiry_fmt="15.09.2026",
                    months_until_expiry=3,
                    sum_rsd_fmt="4 500 000 RSD",
                )
            ],
        ),
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    # Competitor.
    assert "Профиль конкурента" in resp.text
    assert "ДОО Алфа" in resp.text
    assert "90 000 000" in resp.text
    assert "50 000 000 RSD" in resp.text
    # Re-procurements.
    assert "Ожидаемые перезакупки" in resp.text
    assert "Канцеларијски материјал" in resp.text
    assert "15.09.2026" in resp.text
    assert "4 500 000 RSD" in resp.text


def test_renders_competitor_and_reprocurement_gate_pro(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _bundle(
        customer=cust.CustomerAnalyticsView(status="gate_pro"),
        competitor=cust.CompetitorAnalyticsView(status="gate_pro"),
        reprocurement=cust.ReprocurementForecastView(status="gate_pro"),
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    # Both sections show a Pro CTA (the same pattern as web-ai-3).
    assert resp.text.count("Смотреть тарифы") >= 2
    assert "/plans" in resp.text


def test_renders_competitor_and_reprocurement_no_data(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _bundle(
        customer=cust.CustomerAnalyticsView(status="no_data"),
        competitor=cust.CompetitorAnalyticsView(status="no_data"),
        reprocurement=cust.ReprocurementForecastView(status="no_data"),
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert "пока нет профиля конкурента" in resp.text
    assert "пока нет данных о сроках контрактов" in resp.text


def test_customer_success_renders_export_button(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _bundle(
        customer=cust.CustomerAnalyticsView(
            status="success",
            customer_name="Општина Нови Сад",
            customer_pib="100236236",
            total_awards=10,
            distinct_winners=3,
            with_amount=8,
            total_rsd_fmt="12 345 678",
            total_eur_fmt="105 000",
            top_winners=[
                cust.WinnerView(
                    rank=1, name="ДОО Алфа", wins=6, sum_rsd_fmt="9 000 000 RSD"
                )
            ],
        ),
        competitor=cust.CompetitorAnalyticsView(status="no_data"),
        reprocurement=cust.ReprocurementForecastView(status="no_data"),
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    assert (
        "Экспорт в CSV" in resp.text
    )  # t.export_csv (the single key of the CSV export)
    # The button leads to the CSV route of the customer with the profile of the current tender.
    assert f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}" in resp.text
    assert "kind=customer" in resp.text


def test_customer_concentration_renders_visual_bar(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _bundle(
        customer=cust.CustomerAnalyticsView(
            status="success",
            customer_name="Општина Нови Сад",
            customer_pib="100236236",
            total_awards=10,
            distinct_winners=3,
            with_amount=8,
            total_rsd_fmt="12 345 678",
            total_eur_fmt="105 000",
            concentration_pct=72,
            cr3_pct=95,
            is_monopolized=True,
            top_winners=[
                cust.WinnerView(
                    rank=1, name="ДОО Алфа", wins=6, sum_rsd_fmt="9 000 000 RSD"
                )
            ],
        ),
        competitor=cust.CompetitorAnalyticsView(status="no_data"),
        reprocurement=cust.ReprocurementForecastView(status="no_data"),
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")
    assert resp.status_code == 200
    # Metric tiles and a visual concentration bar (dominance → “hot”).
    assert 'class="metric"' in resp.text
    assert "Доля топ-1 победителя" in resp.text
    assert "bar-fill--hot" in resp.text
    assert "width: 72%" in resp.text


# --------------------------------------------------------------------------- #
#  CSV export (web-ai-5)
# --------------------------------------------------------------------------- #


def test_analytics_csv_anonymous_redirected_to_login():
    resp = _client(sess=None).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=customer"
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_analytics_csv_missing_profile_404():
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}/analytics.csv?kind=customer")
    assert resp.status_code == 404


def test_analytics_csv_bad_kind_404():
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=bogus"
    )
    assert resp.status_code == 404


def test_analytics_csv_foreign_tender_404(monkeypatch):
    # A foreign/non-existent tender: get_tender_analyses → None → 404.
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(None))
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=customer"
    )
    assert resp.status_code == 404


def test_analytics_csv_customer_success_returns_csv(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    export = cust.AnalyticsExportView(
        status="success",
        filename="awards_customer_100236236_all.csv",
        content="﻿Наручилац;Општина\r\n".encode("utf-8"),
    )
    monkeypatch.setattr(cust, "get_customer_export", _async(export))
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=customer"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert "awards_customer_100236236_all.csv" in resp.headers["content-disposition"]
    # UTF-8 BOM kept (Excel locale).
    assert resp.content.startswith(b"\xef\xbb\xbf")
    assert "Општина" in resp.content.decode("utf-8")


def test_analytics_csv_competitor_success_returns_csv(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    export = cust.AnalyticsExportView(
        status="success",
        filename="awards_competitor_111_all.csv",
        content=b"\xef\xbb\xbfPobednik;X\r\n",
    )
    monkeypatch.setattr(cust, "get_competitor_export", _async(export))
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=competitor"
    )
    assert resp.status_code == 200
    assert "awards_competitor_111_all.csv" in resp.headers["content-disposition"]


def test_analytics_csv_gate_pro_redirects_to_plans(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    monkeypatch.setattr(
        cust, "get_customer_export", _async(cust.AnalyticsExportView(status="gate_pro"))
    )
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=customer"
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/plans"


def test_analytics_csv_no_data_404(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    monkeypatch.setattr(
        cust, "get_customer_export", _async(cust.AnalyticsExportView(status="no_data"))
    )
    resp = _client(sess=SESS).get(
        f"/app/tenders/{_TENDER}/analytics.csv?profile_id={_PROFILE}&kind=customer"
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Incumbent: “who won against this customer before” (A4)
# --------------------------------------------------------------------------- #


def _incumbent_bundle(incumbent):
    return _bundle(
        customer=cust.CustomerAnalyticsView(status="no_data"),
        competitor=cust.CompetitorAnalyticsView(status="no_data"),
        reprocurement=cust.ReprocurementForecastView(status="no_data"),
        incumbent=incumbent,
    )


def test_renders_incumbent_success(monkeypatch):
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _incumbent_bundle(
        cust.IncumbentView(
            status="success",
            customer_name="Општина Нови Сад",
            items=[
                cust.IncumbentItemView(
                    winner_name="ЕНЕРГОТОК ДОО БЕОГРАД",
                    pib="100172936",
                    date_fmt="03.07.2026",
                    sum_rsd_fmt="7 891 300 RSD",
                    lots=1,
                    is_joint=False,
                )
            ],
        )
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert "Кто выигрывал у этого заказчика раньше" in resp.text
    assert "ЕНЕРГОТОК ДОО БЕОГРАД" in resp.text
    assert "7 891 300 RSD" in resp.text
    assert "03.07.2026" in resp.text
    assert "100172936" in resp.text


def test_incumbent_multilot_and_joint_are_explained(monkeypatch):
    """A multi-lot decision and a consortium are marked, and the glued PIB is not shown."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _incumbent_bundle(
        cust.IncumbentView(
            status="success",
            customer_name="Општина Нови Сад",
            items=[
                cust.IncumbentItemView(
                    winner_name="АМИЦУС СРБ д.о.о.",
                    pib="108585471",
                    date_fmt="03.07.2026",
                    sum_rsd_fmt="8 285 300 RSD",
                    lots=12,
                    is_joint=False,
                ),
                cust.IncumbentItemView(
                    winner_name="МАРИНКО ДОО, РУКИ ЦООП ДОО",
                    pib="",  # the consortium's glued PIBs do not go outside
                    date_fmt="",
                    sum_rsd_fmt="",
                    lots=1,
                    is_joint=True,
                ),
            ],
        )
    )
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert "12 лот(ов) одним решением" in resp.text
    assert "консорциум" in resp.text
    assert "101470969, 103229839" not in resp.text
    # Empty values are explained in words, not as “0 RSD” or an empty cell.
    assert "сумма не опубликована" in resp.text
    assert "дата неизвестна" in resp.text


def test_incumbent_no_data_renders_nothing_not_empty_frame(monkeypatch):
    """A customer without history (TZ A4): the block simply is not shown — NOT a 500 and NOT
    an empty frame with a heading."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _incumbent_bundle(cust.IncumbentView(status="no_data"))
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert "Кто выигрывал у этого наручиоца раньше" not in resp.text
    assert "customer-incumbent" not in resp.text


def test_incumbent_gate_pro_does_not_repeat_paywall(monkeypatch):
    """The gate is already explained by the analytics section above — we do not draw a second banner in a row."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(_data({})))
    bundle = _incumbent_bundle(cust.IncumbentView(status="gate_pro"))
    monkeypatch.setattr(cust, "get_narucilac_analytics", _async(bundle))
    resp = _client(sess=SESS).get(f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")

    assert resp.status_code == 200
    assert "Кто выигрывал у этого наручиоца раньше" not in resp.text


_TENDER_URN = "urn:uuid:22222222-2222-2222-2222-222222222222"


def _uuid_strict_analyses(monkeypatch):
    """As CAST(:tender_id AS uuid) in production: a non-canonical id → DataError (500)."""
    from sqlalchemy.exc import DataError

    async def _stub(db, *, tid, tender_id, profile_id):
        if len(tender_id) != 36:
            raise DataError("SELECT tender", {}, Exception("invalid UUID"))
        return None

    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _stub)


def test_urn_tender_id_redirects_to_canonical_not_500(monkeypatch):
    """08.10 ``/app/tenders/urn:uuid:…`` → 500 (CAST in the DB). Canonical → 301 to the same address."""
    _uuid_strict_analyses(monkeypatch)
    client = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    app.dependency_overrides[deps.get_optional_session] = lambda: SESS
    app.dependency_overrides[deps.get_session_factory] = lambda: (lambda: FakeSession())
    resp = client.get(f"/app/tenders/{_TENDER_URN}?profile_id={_PROFILE}")
    assert resp.status_code == 301
    assert resp.headers["location"] == (f"/app/tenders/{_TENDER}?profile_id={_PROFILE}")


def test_non_uuid_tender_id_is_404_not_500(monkeypatch):
    """Junk in the id of the cabinet card → a 404 page, without a trip to the DB."""
    monkeypatch.setattr(routes_tender_ai.svc, "get_tender_analyses", _async(None))
    resp = _client(sess=SESS).get(f"/app/tenders/not-a-uuid?profile_id={_PROFILE}")
    assert resp.status_code == 404
