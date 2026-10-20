"""Honest copy of the showcase and the plural forms of counters (TASK honest-copy, audit 07.10).

Expectations — LITERALS from the target text, not via ``t()``/``plural()``:
a test computed from the same catalog as the code is a tautology.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from src.api import deps
from src.api.cabinet.i18n import plural
from src.api.main import app
from src.api.public import geo_cache
from tests.api.conftest import FakeResult, FakeSession


def _client(session=None):
    def factory():
        return session or FakeSession()

    app.dependency_overrides[deps.get_session_factory] = lambda: factory
    app.dependency_overrides[deps.get_optional_session] = lambda: None
    return TestClient(app, follow_redirects=False)


def teardown_function():
    app.dependency_overrides.clear()


def _get(path: str, lang: str) -> str:
    resp = _client().get(path, cookies={"lang": lang})
    assert resp.status_code == 200
    return resp.text


# --------------------------------------------------------------------------- #
# 1. The promises of the home page: without “svaki tender” / “svakog dana”; the volume of AI — by plan
# --------------------------------------------------------------------------- #


def test_home_sr_keeps_check_interval_and_drops_every_tender_promise():
    # Frequency — a FACT from the sources table (sources.polling_interval_sec of the enabled ones):
    # production today — one source, jnportal 1800 s = 30 min, therefore "na svakih
    # 30 min“ without a range. ”Svakog dana" removed: it is not an exact wording.
    html = _get("/", "sr")
    assert "Proveravamo izvore javnih nabavki" in html
    assert "na svakih 30 min i šaljemo vam" in html
    assert "Sistem proverava izvore javnih nabavki (" in html
    assert ") na svakih 30 min." in html
    assert "obim AI analize zavisi od tarifa" in html
    assert "svaki tender" not in html
    assert "Svako poklapanje" not in html
    assert "Redovno proveravamo" not in html
    assert "Za poklapanja možete zatražiti gotov AI-odgovor" in html
    assert "Kod TED kartica specifikacija često nije u objavi" in html


def test_home_ru_keeps_check_interval_and_drops_every_tender_promise():
    html = _get("/", "ru")
    assert "Проверяем источники госзакупок (" in html
    assert ") каждые 30 минут и присылаем тендеры" in html
    assert "Система проверяет источники госзакупок (" in html
    assert ") каждые 30 минут." in html
    assert "каждому тендеру" not in html
    assert "Регулярно проверяем" not in html
    assert "У карточек TED техзадание часто не публикуется" in html


def test_home_en_keeps_check_interval_and_drops_every_tender_promise():
    html = _get("/", "en")
    assert "We check public procurement sources (" in html
    assert ") every 30 min and send you tenders" in html
    assert "The system checks public procurement sources (" in html
    assert ") every 30 min." in html
    assert "every 30 min." in html
    assert "For each tender, AI tells you" not in html
    assert "Every match comes with" not in html
    assert "For matches you can request a finished AI review" in html
    assert "ready answer" not in html
    assert "publish the specification, so the verdict may be limited" in html


# --------------------------------------------------------------------------- #
# 2–4. /plans, /help, /signup — badge, term, bot, hint
# --------------------------------------------------------------------------- #


def test_plans_sr_no_popular_badge_and_no_team_claim():
    html = _get("/plans", "sr")
    assert "Najpopularniji" not in html
    assert "Za timove" not in html
    assert "Za firme sa više delatnosti." in html
    assert "Nacrt prijave" not in html
    assert "Nacrt ponude" in html


def test_help_sr_single_bot_handle_and_human_form_name():
    html = _get("/help", "sr")
    assert "@Nabrs_bot" not in html
    assert "Nabrs_bot" not in html
    assert "@nabrs_bot" in html
    assert "Nacrt prijave" not in html
    assert "Nacrt ponude" in html
    assert "F02 / F03" not in html
    assert "(F03)" not in html
    assert "Obaveštenja o nabavci i dodeli ugovora" in html


def test_signup_sr_hint_passwordless():
    html = _get("/signup", "sr")
    assert "Lozinka nije potrebna — prijava ide preko linka na email." in html


def test_signup_ru_hint_passwordless():
    html = _get("/signup", "ru")
    assert "Пароль не нужен — вход по ссылке на почту." in html


# --------------------------------------------------------------------------- #
# 6. Plural forms of counters — literals: 1 / 2 / 5 / 11 / 21
# --------------------------------------------------------------------------- #


def test_plural_sr_open_tenders_literals():
    assert plural("sr", "open_tenders", 1) == "1 otvoren"
    assert plural("sr", "open_tenders", 2) == "2 otvorena"
    assert plural("sr", "open_tenders", 5) == "5 otvorenih"
    assert plural("sr", "open_tenders", 11) == "11 otvorenih"
    assert plural("sr", "open_tenders", 21) == "21 otvoren"


def test_plural_sr_deadline_days_literals():
    assert plural("sr", "deadline_days_left", 1) == "Još 1 dan"
    assert plural("sr", "deadline_days_left", 2) == "Još 2 dana"
    assert plural("sr", "deadline_days_left", 5) == "Još 5 dana"
    assert plural("sr", "deadline_days_left", 11) == "Još 11 dana"
    assert plural("sr", "deadline_days_left", 21) == "Još 21 dan"


def test_plural_ru_literals():
    assert plural("ru", "open_tenders", 1) == "1 открытый"
    assert plural("ru", "open_tenders", 2) == "2 открытых"
    assert plural("ru", "open_tenders", 5) == "5 открытых"
    assert plural("ru", "deadline_days_left", 1) == "Осталось 1 день"
    assert plural("ru", "deadline_days_left", 2) == "Осталось 2 дня"
    assert plural("ru", "deadline_days_left", 11) == "Осталось 11 дней"
    assert plural("ru", "deadline_days_left", 21) == "Осталось 21 день"


def test_plural_en_literals():
    assert plural("en", "deadline_days_left", 1) == "1 day left"
    assert plural("en", "deadline_days_left", 5) == "5 days left"


def _hub_with_open(open_n: int, lang: str) -> str:
    geo_cache._cpv_cache = (
        time.monotonic() + 3600,
        {"45": {"total": 10, "open": open_n, "open_sum_eur": None, "label": "Radovi"}},
    )
    session = FakeSession(results=[FakeResult(items=[])] * 4)
    resp = _client(session).get("/tenderi", cookies={"lang": lang})
    assert resp.status_code == 200
    return resp.text


def test_hub_sr_open_count_is_declined():
    assert "1 otvoren<" in _hub_with_open(1, "sr")
    assert "5 otvorenih<" in _hub_with_open(5, "sr")
    assert "1 otvorenih" not in _hub_with_open(1, "sr")


def test_hub_sr_open_count_11_and_2_forms():
    assert "11 otvorenih<" in _hub_with_open(11, "sr")
    assert "2 otvorena<" in _hub_with_open(2, "sr")


# --------------------------------------------------------------------------- #
# 7. Guard: terms removed from the showcase and the cabinet/bot do not come back into src/
# --------------------------------------------------------------------------- #


def test_banned_draft_and_claim_terms_absent_from_src():
    # #624: the guard was case-sensitive and looked only at *.py — a lowercase
    # “nacrt prijave” in the cabinet catalog (cabinet/i18n.py:932) slipped past.
    # Now: case-insensitive and over ALL text files under src/
    # (catalogs, templates, js, json). The exception — one file by an explicit list:
    # legal texts are decided by the owner separately (#624); we do not touch their wording.
    import pathlib

    banned = ("nacrt prijave", "application draft", "ready answer", "(f03)")
    excluded = {"api/public/legal_texts.py"}  # #624: legal texts, the owner’s decision
    root = pathlib.Path(__file__).resolve().parents[2] / "src"
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix == ".pyc" or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        if rel in excluded:
            continue
        try:
            text = p.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:  # png/svg/… — not text, a term cannot be there
            continue
        hits.extend(f"{rel}: {term}" for term in banned if term in text)
    assert hits == []


# --------------------------------------------------------------------------- #
# Legal texts = a fact of the code (owner 10.10.2026, legal-texts-sync)
# --------------------------------------------------------------------------- #


def test_terms_sr_does_not_promise_old_pause_and_states_open_cabinet():
    html = _get("/legal/terms", "sr")
    assert "pauzira dok ne pošaljete zahtev za pretplatu" not in html
    assert "prijava u kabinet ostaje" in html
    assert "najviše 30 dana" in html


def test_privacy_sr_states_retained_hash_and_no_blanket_deletion_promise():
    html = _get("/legal/privacy", "sr")
    assert "Na zahtev za brisanje uklanjamo nalog i povezane podatke" not in html
    assert "Posle brisanja zadržavamo: heš e-adrese" in html
    assert "Samostalna ponovna registracija sa istom e-adresom nije moguća" in html
    assert "trenutno nije moguća" not in html
    assert "čuvamo samo" not in html
