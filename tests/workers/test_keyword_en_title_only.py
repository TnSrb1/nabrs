"""An English tender matches ONLY by title (owner 18.10, option A).

Expectations are literals from the measurement (WB: "project" 12/12, "development" 8/12 — only
from the REOI/SPN template description). The Serbian path — title + description, as before.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.domain.text_norm import fold
from src.workers.adapters.base import match_reason_of
from src.workers.matching import Candidate, TenderView, stage_b_filter
from tests.workers.test_why_matched_keyword_dict import _job

WB_TITLE = (
    "Communication and training of accounting and audit staff on new requirements"
)
WB_DESC = (
    "Consultant services. The scope covers project management, the project team "
    "and development of a training program for auditors."
)


def _cand(words) -> Candidate:
    return Candidate(
        profile_id="p",
        tenant_id="t",
        keywords_include=[{"all_of": words}],
        keywords_exclude=None,
        ai_analysis_level=None,
        delivery_channels=None,
        keyword_dict=True,
    )


def _view(source_key, language, title, description) -> TenderView:
    return TenderView(
        id="1",
        country_code="RS",
        source_key=source_key,
        language=language,
        title_normalized=fold(title),
        description_normalized=fold(description),
    )


def test_wb_description_only_words_do_not_match_english_tender():
    wb = _view("worldbank", "en", WB_TITLE, WB_DESC)
    assert stage_b_filter([_cand(["разработка"])], wb) == []
    assert stage_b_filter([_cand(["проект"])], wb) == []


def test_english_title_word_matches_english_tender():
    tender = _view("worldbank", "en", "Procurement of transport vehicles", "")
    cand = _cand(["Транспорт"])
    assert stage_b_filter([cand], tender) == [cand]


def test_serbian_tender_still_matches_on_description():
    sr = _view("jnportal", "sr", "Nabavka opreme", "Građevinski radovi na objektu")
    cand = _cand(["građevin"])
    assert stage_b_filter([cand], sr) == [cand]


def test_delivery_reason_ignores_description_for_english_job():
    # The delivery row carries the title (DeliveryJob.tender_haystack = title).
    job = _job(
        source_key="worldbank",
        language="en",
        tender_haystack=fold(WB_TITLE),
        profile_keywords_include=[{"all_of": ["проект"]}],
        profile_keyword_dict=True,
    )
    assert match_reason_of(job).matched_keywords == []


def test_load_jobs_builds_english_haystack_from_title_only():
    """Delivery row mapping: en-tender → haystack without description (same predicate)."""
    from src.workers.delivery import DeliveryWorker

    row = SimpleNamespace(
        outbox_id=1,
        tender_id="00000000-0000-0000-0000-000000000001",
        channel="telegram",
        requires_ai=False,
        user_channel_id=None,
        target=None,
        channel_status=None,
        title=WB_TITLE,
        source_url=None,
        amount_eur=None,
        deadline_at=None,
        profile_id=None,
        language="en",
        source_key="worldbank",
        tender_title=fold(WB_TITLE),
        tender_description=fold(WB_DESC),
        profile_keywords_include=[{"all_of": ["проект"]}],
        profile_keyword_dict=True,
    )

    class _Session:
        async def execute(self, *_a, **_kw):
            return [row]

    import asyncio

    worker = DeliveryWorker.__new__(DeliveryWorker)
    (job,) = asyncio.run(worker._load_jobs(_Session(), [1]))
    assert job.tender_haystack == fold(WB_TITLE)
    assert match_reason_of(job).matched_keywords == []


def test_preview_predicate_matches_routing_for_english_tender():
    from src.api.cabinet.preview_service import _keyword_filter

    passes = _keyword_filter(
        {
            "keywords_include": [{"all_of": ["проект"]}],
            "keywords_exclude": [],
            "keyword_dict": True,
        }
    )
    row = SimpleNamespace(
        title_normalized=fold(WB_TITLE),
        description_normalized=fold(WB_DESC),
        language="en",
        source_key="worldbank",
        customer_followed=False,
    )
    assert passes(row) is False
    row.title_normalized = fold("Project management consultancy services")
    assert passes(row) is True
