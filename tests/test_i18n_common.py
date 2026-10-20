"""The guard of the NEUTRAL catalog of the common lexicon of the bot↔cabinet (:mod:`src.i18n_common`).

``COMMON`` — the SINGLE source of the funnel statuses and the plan names for the two surfaces (the cabinet and the Telegram bot). Here we check:

- the parity of the keys of all three languages (the anti-drift of the translations);
- the ru statuses = ``STATUS_LABELS`` (the source of truth of the funnel) — a divergence would name
  the same row of ``tender_user_status`` differently in the bot and in the cabinet;
- the labels of the card’s rows = the catalog of the letters (:mod:`src.i18n_email`): one tender card is drawn by two channels, and naming a field differently is not allowed;
- each code of the subject type/procedure from the delivery adapter has a label in all languages (the list is derived from the maps of the adapter, not rewritten by hand);
- the cabinet and the bot take ONE AND THE SAME object (not a copy) — otherwise they would diverge.
"""

from __future__ import annotations

import src.i18n_common as ic


def test_common_key_parity():
    """All three languages carry one set of keys, the values are non-empty."""
    assert set(ic.COMMON) == set(ic.SUPPORTED)
    ref = set(ic.COMMON[ic.DEFAULT])
    for lang in ic.SUPPORTED:
        assert set(ic.COMMON[lang]) == ref, f"the keys of {lang} diverge from {ic.DEFAULT}"
        for key, val in ic.COMMON[lang].items():
            assert isinstance(val, str) and val.strip(), f"{lang}.{key} is empty"


def test_common_status_labels_match_funnel_source():
    """the ru statuses of COMMON = STATUS_LABELS (the single source of the funnel bot↔cabinet)."""
    from src.db.models.tender_status import STATUS_LABELS, STATUSES

    for st in STATUSES:
        assert ic.COMMON["ru"][f"status_{st}"] == STATUS_LABELS[st], st
    status_keys = {k for k in ic.COMMON["ru"] if k.startswith("status_")}
    assert status_keys == {f"status_{s}" for s in STATUSES}


def test_cabinet_and_common_share_one_catalog():
    """The cabinet imports THE SAME object COMMON — not a copy (a single source)."""
    import src.api.cabinet.i18n as ci

    assert ci.COMMON is ic.COMMON


def test_card_labels_match_email_catalog():
    """The labels of the card = the catalog of the letters: one card, two channels.

    The same tender card is drawn by Telegram (COMMON) and the e-mail (:data:`src.i18n_email.EMAIL`). The keys are named identically, the strings must match in ALL three languages — otherwise the field is named differently in two channels and the subscriber sees a divergence (the same defect as #455 with the CSV).
    """
    from src.i18n_email import EMAIL

    for key in ic.CARD_LABEL_KEYS:
        for lang in ic.SUPPORTED:
            assert ic.COMMON[lang][key] == EMAIL[lang][key], f"{lang}.{key}"


def test_notify_labels_match_email_catalog():
    """The wording of the background notices = the catalog of the letters: one notice, two channels.

    The end of the subscription and the alert about a new win go to the subscriber AND to Telegram (COMMON), AND by letter (:data:`src.i18n_email.EMAIL`). The language of the channels is their own — the channel against the cabinet, this is a regular divergence; the wording itself must be one, otherwise one person reads about different things in the bot and in the mail.
    """
    from src.i18n_email import EMAIL

    for key in ic.NOTIFY_LABEL_KEYS:
        for lang in ic.SUPPORTED:
            assert ic.COMMON[lang][key] == EMAIL[lang][key], f"{lang}.{key}"


def test_notification_keys_carry_no_emoji_prefix():
    """The emoji prefixes (⏰/🔔/📊/…) are the markup of the worker, not the lexicon of the catalog.

    For ``sub_*``/``aw_*`` this is also a condition of the guard above: the letter has no emoji prefix, and with it the strings of the two channels would not match. For ``sum_*``/``rem_*`` the rule is the same for the sake of uniformity — otherwise the next line will arrive now with an emoji, now without.
    """
    prefixes = ("⏰", "🔔", "🏛", "📊", "✅", "👀", "📨", "🏆", "⏳", "🗓", "📋")
    keys = [
        k
        for k in ic.COMMON[ic.DEFAULT]
        if k.startswith(("sum_", "rem_", "sub_", "aw_"))
    ]
    assert keys, "the keys of the background notices are not found — the test checks nothing"
    for key in keys:
        for lang in ic.SUPPORTED:
            text = ic.COMMON[lang][key]
            for prefix in prefixes:
                assert prefix not in text, f"{lang}.{key}: {text!r}"


def test_days_phrase_declines_by_language():
    """“N days” is inflected: sr 1/2+, en singular/plural, ru — an uninflected abbreviation."""
    assert ic.days_phrase(1, "sr") == "1 dan"
    assert ic.days_phrase(5, "sr") == "5 dana"
    assert ic.days_phrase(1, "en") == "1 day"
    assert ic.days_phrase(7, "en") == "7 days"
    assert ic.days_phrase(1, "ru") == "1 дн."
    assert ic.days_phrase(7, None) == "7 дн."  # an explicit fallback to ru


def test_procedure_and_contract_keys_cover_delivery_maps():
    """Each code of the subject type/procedure from the adapter has a label in the catalog.

    The list of keys is derived FROM the maps of the adapter, not rewritten by hand: a new procedure code without a line in the catalog will drop the test, not the recipient’s card.
    """
    from src.workers.adapters.base import _CONTRACT_TYPE_KEYS, _PROCEDURE_TYPE_KEYS

    needed = set(_CONTRACT_TYPE_KEYS.values()) | set(_PROCEDURE_TYPE_KEYS.values())
    assert needed, "the code maps are empty — the test checks nothing"
    for lang in ic.SUPPORTED:
        missing = needed - set(ic.COMMON[lang])
        assert not missing, f"{lang}: there are no strings for {sorted(missing)}"


def test_common_t_falls_back_to_ru():
    """An unknown language → ru (the default), a known one → its own string."""
    assert ic.common_t("en", "status_new") == ic.COMMON["en"]["status_new"]
    assert ic.common_t("sr", "plan_pro") == ic.COMMON["sr"]["plan_pro"]
    assert ic.common_t(None, "status_won") == ic.COMMON["ru"]["status_won"]
    assert ic.common_t("de", "status_won") == ic.COMMON["ru"]["status_won"]
