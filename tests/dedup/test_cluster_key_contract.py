"""The contract of compute_cluster_key (T6) — what the relative tests do not catch.

It complements ``test_cluster_key.py`` / ``test_cluster_key_extra.py``: there the key is checked AGAINST ITSELF (equal/not equal), therefore a silent mutation of the formula (swapped components, a changed separator, ``str(float)`` instead of ``int``) leaves the whole set green. Here the formula is pinned by a LITERAL sha1 and the main goal of the module — the cross-platform equivalence — is checked.

The behavior is taken from the code, not invented: ``country_code`` is canonicalised (upper+strip), ``system_code``/``code`` — ONLY strip, no upper.

The formula — ``docs/DB_SCHEMA.md §7``. Pure, without a network/DB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.dedup.cluster_key import compute_cluster_key
from src.parsers.base import NormalizedTender


def _tender(**kwargs: object) -> NormalizedTender:
    base: dict[str, object] = {"source_key": "jnportal", "source_uid": "1"}
    base.update(kwargs)
    return NormalizedTender(**base)  # type: ignore[arg-type]


def _full(**overrides: object) -> NormalizedTender:
    base: dict[str, object] = {
        "country_code": "RS",
        "customer_tax_id": "100000000",
        "classifications": [{"system_code": "eu_cpv", "code": "32200000"}],
        "amount_eur": 10000.0,
        "deadline_at": datetime(2026, 5, 20, 12, 0, 0),
    }
    base.update(overrides)
    return _tender(**base)


# --------------------------------------------------------------------------- #
#  The formula — a literal snapshot (against the “silent mutation”)
# --------------------------------------------------------------------------- #
def test_key_matches_literal_sha1_of_documented_formula():
    # sha1("RS|100000000|eu_cpv|32200000|10000|2026-05-20") — DB_SCHEMA §7.
    # A literal, not a recomputation of hashlib in the test: a recomputation would repeat the error
    # of the formula and would stay green. The order of the components, the separator "|",
    # amount as an integer without ".0" and deadline as an ISO date are pinned at once.
    assert compute_cluster_key(_full()) == "03272459ec33a88710153a97ad92b18a29870e81"


def test_amount_rendered_as_int_not_float_repr():
    # int(round(x, -3)) → "10000". If the rendering were str(round(x, -3)) →
    # "10000.0", the key would move for ALL tenders at once (all the relative
    # comparisons would stay green at the same time).
    assert compute_cluster_key(_full(amount_eur=10000.0)) == compute_cluster_key(
        _full(amount_eur=10000.4)
    )
    assert compute_cluster_key(_full()) != compute_cluster_key(
        _tender(
            country_code="RS",
            customer_tax_id="100000000",
            classifications=[{"system_code": "eu_cpv", "code": "32200000"}],
            amount_eur=11000.0,
            deadline_at=datetime(2026, 5, 20, 12, 0, 0),
        )
    )


# --------------------------------------------------------------------------- #
#  The goal of the module: one tender from DIFFERENT platforms → one cluster
# --------------------------------------------------------------------------- #
def test_same_tender_from_different_sources_shares_key():
    # The key is built ONLY from the six components of §7. The identifiers of the source,
    # the title, the description and the URL do not enter it — otherwise the cross-platform
    # deduplication would never work (that is the meaning of the module).
    jnportal = _full(
        source_key="jnportal",
        source_uid="2026/RS/00123",
        source_url="https://jnportal.rs/tender/123",
        title="Набавка рачунарске опреме",
        description="Опис поступка на порталу ЈН.",
    )
    mirror = _full(
        source_key="ted_eu",
        source_uid="TED-556677",
        source_url="https://src-ted.example.invalid/notice/556677",
        title="Purchase of computer equipment",
        description="Wholly different wording of the same notice.",
    )
    key = compute_cluster_key(jnportal)
    assert key is not None
    assert key == compute_cluster_key(mirror)


def test_different_customers_do_not_share_key():
    # The reverse side: different customers (tax_id) with everything else equal —
    # DIFFERENT clusters, otherwise the dedup would glue foreign tenders together and the subscriber
    # would not get one of them at all.
    assert compute_cluster_key(_full(customer_tax_id="100000000")) != (
        compute_cluster_key(_full(customer_tax_id="200000001"))
    )


# --------------------------------------------------------------------------- #
#  Case/spaces: the country is canonicalised, system_code/code — NOT
# --------------------------------------------------------------------------- #
def test_classification_fields_are_stripped():
    assert compute_cluster_key(
        _full(classifications=[{"system_code": "  eu_cpv ", "code": " 32200000  "}])
    ) == compute_cluster_key(_full())


def test_classification_case_is_significant_unlike_country():
    # The asymmetry is taken from the code: _normalize_country does upper, while
    # _primary_classification does only strip. Both halves of the contrast
    # are checked here, otherwise the test asserts «unlike country» without
    # showing that the country behaves differently.
    assert compute_cluster_key(_full(country_code="rs")) == compute_cluster_key(_full())
    # And "EU_CPV" and "eu_cpv" give DIFFERENT clusters. We pin it as it is:
    # if a second source with a different case of system_code appears, this test is
    # the place where the decision on normalization will be taken consciously, not by
    # the fact of spam for the subscriber.
    assert compute_cluster_key(
        _full(classifications=[{"system_code": "EU_CPV", "code": "32200000"}])
    ) != compute_cluster_key(_full())


def test_country_none_empty_and_blank_are_equivalent():
    # None / "" / "   " → all give the component "" → one key.
    keys = {
        compute_cluster_key(_full(country_code=value)) for value in (None, "", "   ")
    }
    assert len(keys) == 1
    assert None not in keys


# --------------------------------------------------------------------------- #
#  Resistance to an incomplete classifications dictionary
# --------------------------------------------------------------------------- #
def test_classification_dict_without_keys_is_treated_as_empty():
    # primary.get(...) or "" — an empty dict does not break the calculation and equals
    # the absence of the classification at all.
    assert compute_cluster_key(_full(classifications=[{}])) == compute_cluster_key(
        _full(classifications=[])
    )


def test_classification_none_values_are_treated_as_empty():
    assert compute_cluster_key(
        _full(classifications=[{"system_code": None, "code": None}])  # type: ignore[dict-item]
    ) == compute_cluster_key(_full(classifications=[]))


# --------------------------------------------------------------------------- #
#  deadline: date() is taken as is, WITHOUT the conversion to UTC
# --------------------------------------------------------------------------- #
def test_aware_deadline_uses_wall_clock_date_without_utc_shift():
    # datetime.date() ignores tzinfo. Midnight-minus-30-minutes in Belgrade
    # (UTC+2) — it is 21:30 UTC of the PREVIOUS day, but the key is built by the date
    # the 21st, not the 20th. Pinned as a fact: the change to .astimezone(utc)
    # would scatter the duplicates of one tender into two clusters.
    belgrade = timezone(timedelta(hours=2))
    aware = _full(deadline_at=datetime(2026, 5, 21, 0, 30, tzinfo=belgrade))
    naive_same_day = _full(deadline_at=datetime(2026, 5, 21, 12, 0))
    assert compute_cluster_key(aware) == compute_cluster_key(naive_same_day)
