"""Tests of the legal pages of the showcase (/legal/{privacy,terms,impressum}).

Guard: unfilled service markup ``[[owner: ...]]`` does NOT leak to a visitor —
it is replaced by a neutral localised stub + a WARNING log with the keys.
i18n parity: the structure/set of sections matches across sr/ru/en, fallback to DEFAULT.

No DB/session needed — /legal is public and static; templates render for real.
"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from src.api.main import app
from src.api.public.i18n import DEFAULT, SUPPORTED
from src.api.public.legal_texts import LEGAL, _UNFILLED_FALLBACK, get_doc

_DOCS = ("privacy", "terms", "impressum")


def _client() -> TestClient:
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------- guard


def test_rendered_legal_never_leaks_raw_placeholder():
    """Not a single legal page in any language shows the raw marker [[owner:."""
    client = _client()
    for name in _DOCS:
        for lang in SUPPORTED:
            resp = client.get(f"/legal/{name}", cookies={"lang": lang})
            assert resp.status_code == 200, (name, lang)
            assert "[[owner:" not in resp.text, (name, lang)


def test_get_doc_replaces_placeholder_with_localized_fallback(caplog):
    """A raw [[owner: X]] → a neutral language stub; the WARNING contains the key."""
    with caplog.at_level(logging.WARNING):
        doc = get_doc("privacy", "ru")
    assert doc is not None
    # The ru stub is present, there is no raw markup.
    blob = repr(doc)
    assert "[[owner:" not in blob
    assert _UNFILLED_FALLBACK["ru"] in blob
    # The WARNING lists the unfilled service markers (owner keys).
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "a WARNING about unfilled placeholders was expected"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "unfilled" in joined
    assert "precizirati rokove" not in joined  # sr key; ru key — below
    assert "уточнить сроки" in joined  # the service marker of the retention period (owner)
    assert "дата вступления в силу" not in joined  # filled 10.10.2026


def test_guard_reports_all_placeholders_per_lang(caplog):
    """The guard fires for all three documents in every language."""
    for name in _DOCS:
        for lang in SUPPORTED:
            caplog.clear()
            with caplog.at_level(logging.WARNING):
                doc = get_doc(name, lang)
            assert doc is not None
            assert "[[owner:" not in repr(doc), (name, lang)
            fb = _UNFILLED_FALLBACK[lang]
            # At least one stub of the needed language in each document.
            assert fb in repr(doc), (name, lang, "no localised stub")


# ----------------------------------------------------------------- i18n parity


def test_legal_i18n_structure_parity():
    """The set of languages and the structure of sections match across sr/ru/en for each document."""
    for name in _DOCS:
        page = LEGAL[name]
        assert set(page) == set(SUPPORTED), f"{name}: languages != {SUPPORTED}"
        ref = page[DEFAULT]
        ref_top = set(ref)
        ref_n = len(ref["sections"])  # type: ignore[arg-type]
        ref_body_lens = [len(s["body"]) for s in ref["sections"]]  # type: ignore[index]
        for lang in SUPPORTED:
            cur = page[lang]
            assert set(cur) == ref_top, f"{name}/{lang}: top-level keys != {DEFAULT}"
            secs = cur["sections"]
            assert len(secs) == ref_n, f"{name}/{lang}: number of sections != {DEFAULT}"  # type: ignore[arg-type]
            for i, s in enumerate(secs):  # type: ignore[arg-type]
                assert set(s) == {"h", "body"}, f"{name}/{lang}#{i}: section keys"
                assert (
                    len(s["body"]) == ref_body_lens[i]
                ), f"{name}/{lang}#{i}: number of paragraphs != {DEFAULT}"


def test_get_doc_falls_back_to_default_lang():
    """An unknown language → the document in DEFAULT (the fallback works), not None."""
    doc = get_doc("privacy", "de")
    assert doc is not None
    assert doc["title"] == LEGAL["privacy"][DEFAULT]["title"]  # type: ignore[index]


def test_get_doc_unknown_name_is_none():
    assert get_doc("nope", "ru") is None


# ----------------------------------------------- ToS: plans = catalog/limits
# Expectations — literals from catalog.py (Pro 29 / Business 79 / Enterprise on
# request, profiles 3/10) and usage._TIER_LIMITS (deep/bid/checklist/draft);
# there is no public Free (owner decision 30.09.2026).
_TOS_PLAN_LINES = {
    "sr": (
        "Pro — 29 EUR mesečno: 3 šablona pretrage; mesečno do 10 dubinskih AI-analiza, "
        "30 procena „učestvovati ili ne“, 30 ček-lista dokumenata i 5 nacrta prijave.",
        "Business — 79 EUR mesečno: 10 šablona pretrage; mesečno do 50 dubinskih "
        "AI-analiza, 150 procena „učestvovati ili ne“, 150 ček-lista dokumenata i "
        "30 nacrta prijave.",
        "Enterprise — po dogovoru.",
        "Probni period traje 30 dana",
    ),
    "ru": (
        "Pro — 29 EUR в месяц: 3 шаблона поиска; в месяц до 10 глубоких AI-разборов, "
        "30 вердиктов «участвовать или нет», 30 чек-листов документов и "
        "5 черновиков заявки.",
        "Business — 79 EUR в месяц: 10 шаблонов поиска; в месяц до 50 глубоких "
        "AI-разборов, 150 вердиктов «участвовать или нет», 150 чек-листов "
        "документов и 30 черновиков заявки.",
        "Enterprise — по договорённости.",
        "Пробный период длится 30 дней",
    ),
    "en": (
        "Pro — 29 EUR per month: 3 search templates; per month up to 10 deep AI "
        "analyses, 30 bid / no-bid verdicts, 30 document checklists and 5 "
        "application drafts.",
        "Business — 79 EUR per month: 10 search templates; per month up to 50 deep "
        "AI analyses, 150 bid / no-bid verdicts, 150 document checklists and 30 "
        "application drafts.",
        "Enterprise — by agreement.",
        "The trial lasts 30 days",
    ),
}


def test_terms_plans_section_matches_catalog_and_has_no_free():
    for lang, lines in _TOS_PLAN_LINES.items():
        text = "\n".join(
            p for s in get_doc("terms", lang)["sections"] for p in s["body"]  # type: ignore[index,union-attr]
        )
        for line in lines:
            assert line in text, (lang, line)
        assert "Free" not in text and "бесплатный;" not in text, lang
        assert "probni pristup besplatan" not in text
        assert "trial access is currently free" not in text.lower()


# ------------------------------------- legal texts = a fact of the code (owner 10.10.2026)
# Expectations — LITERALS from the new text, not via the ``LEGAL`` dict.
_PAGES = {
    "privacy": {
        "sr": (
            "Stupa na snagu: 10.10.2026",
            "Nalog možete sami obrisati u kabinetu (Nalog), potvrdom unosom svoje e-adrese.",
            "Posle brisanja zadržavamo: heš e-adrese",
            "evidenciju o naplati, anonimni zapis o brisanju, kao i tehničke",
            "AI-analize tendera i brojači AI-potrošnje",
            "Samostalna ponovna registracija sa istom e-adresom nije moguća",
            "novi pristup je moguć samo na zahtev upućen na",
        ),
        "ru": (
            "Вступает в силу: 10.10.2026",
            "Аккаунт можно удалить самостоятельно в кабинете (Аккаунт)",
            "После удаления мы сохраняем: хеш e-mail",
            "сведения о платежах",
            "анонимную запись об удалении",
            "Самостоятельная повторная регистрация с тем же e-mail невозможна",
        ),
        "en": (
            "Effective date: 10.10.2026",
            "You can delete the account yourself in the cabinet (Account)",
            "After deletion we keep: a hash of the email",
            "billing records",
            "an anonymous deletion record",
            "Self-service re-registration with the same email is not possible",
        ),
    },
    "terms": {
        "sr": (
            "Posle isteka pristupa slanje tendera se zaustavlja, ali prijava u kabinet ostaje",
            "Svako produženje traje najviše 30 dana, odobrava ga operater i može biti odbijeno.",
            "Probni period traje 30 dana i besplatan je.",
        ),
        "ru": (
            "После окончания доступа рассылка тендеров останавливается, но вход в кабинет сохраняется",
            "Каждое продление — не более 30 дней, его одобряет оператор, в продлении может быть отказано.",
            "Пробный период длится 30 дней и бесплатен.",
        ),
        "en": (
            "After access ends, tender delivery stops, but you can still sign in to the cabinet",
            "Each extension is at most 30 days, is approved by an operator, and may be declined.",
            "The trial lasts 30 days and is free.",
        ),
    },
}
_STALE = (
    "pauzira dok ne pošaljete zahtev za pretplatu",
    "se nastavlja na jednoj od plaćenih tarifa",
    "Na zahtev za brisanje uklanjamo nalog",
    "приостанавливается, пока вы не отправите заявку",
    "после этого работа продолжается на одном из платных тарифов",
    "По запросу на удаление удаляем аккаунт",
    "notification delivery is paused until you send",
    "afterwards you continue on one of the paid plans",
    "On a deletion request we remove the account",
    # owner 10.10: “now/trenutno/currently” and “only/samo” — an exaggeration, removed
    "trenutno nije moguća",
    "čuvamo samo",
    "сейчас невозможна",
    "currently not possible",
    "мы храним только",
    "we keep only",
)


def _flat(html: str) -> str:
    import html as _h
    import re

    return " ".join(_h.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def test_legal_pages_render_new_facts_and_drop_stale_claims():
    client = _client()
    for name, langs in _PAGES.items():
        for lang, phrases in langs.items():
            resp = client.get(f"/legal/{name}", cookies={"lang": lang})
            assert resp.status_code == 200, (name, lang)
            text = _flat(resp.text)
            for phrase in phrases:
                assert phrase in text, (name, lang, phrase)
            for stale in _STALE:
                assert stale not in text, (name, lang, stale)


def test_legal_sr_has_no_cyrillic():
    import re

    client = _client()
    for name in _DOCS:
        resp = client.get(f"/legal/{name}", cookies={"lang": "sr"})
        assert resp.status_code == 200, name
        body = _flat(resp.text)
        # Cyrillic is allowed only in the header language switcher (“Russkij”):
        # we check the legal text itself — the sections of the document.
        doc = get_doc(name, "sr")
        blob = repr(doc)
        assert re.findall("[\u0400-\u04ff]", blob) == [], name
        assert body  # the page is not empty
