"""The textual guard of the SQL cut-off SRC-0a (works in the CI without a DB).

‼️ This is NOT a proof of the predicate: the execution is checked by ``tests/db/test_src0a_routing_cutoff.py`` on a live PostgreSQL (a skip without ``TEST_RLS_DATABASE_URL``) and by the measurement on production (TESTING_src0a_registry.md). Here only that the texts of the queries carry the needed conditions and do not carry extra ones — so that an edit that drops the cut-off turns red also in the CI, where there is no PG.
"""

from __future__ import annotations

from src.workers.matching import PREVIEW_WHERE_A, STAGE_A_SQL

STAGE_A = " ".join(str(STAGE_A_SQL).split())
PREVIEW = " ".join(PREVIEW_WHERE_A.split())


def test_stage_a_text_has_quiet_first_pass_cutoff():
    assert "JOIN sources s ON s.source_key = CAST(:source_key AS text)" in STAGE_A
    assert "s.baseline_at IS NOT NULL" in STAGE_A
    assert "CAST(:first_seen AS timestamptz) > s.baseline_at" in STAGE_A


def test_stage_a_text_has_subscription_moment_cutoff():
    assert (
        "CAST(:first_seen AS timestamptz) >= COALESCE( CAST(p.source_opt_in ->> "
        "CAST(:source_key AS text) AS timestamptz), p.created_at)"
    ) in STAGE_A


def test_stage_a_text_has_no_profile_country_gate():
    assert "p.country_code" not in STAGE_A


def test_region_rule_is_mirrored_in_preview():
    assert "WHERE left(r, 2) = CAST(:tender_country_code AS text)" in STAGE_A
    assert "WHERE left(r, 2) = CAST(tn.country_code AS text)" in PREVIEW


def test_preview_text_has_no_time_cutoff():
    """The preview shows the history — the cut-offs by the subscription must not be there."""
    assert "baseline_at" not in PREVIEW
    assert "source_opt_in" not in PREVIEW
