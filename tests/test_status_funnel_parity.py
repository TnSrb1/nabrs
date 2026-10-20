"""The guards of the funnel tender_user_status: one funnel for the bot, the cabinet and the reminder.

VG-17w gave the web all 7 stages (`src/api/cabinet/workflow_service.set_status`) into the same table `tender_user_status` (0008), into which the bot writes. The parity is held not by synchronization, but by the common table and the common list of stages — so it breaks silently: the labels or the predicate of the reminder diverge, and “Participating”, marked in the cabinet, stops meaning the same thing in Telegram.

The tests — without a DB and a network: they compare the constants of the modules, therefore they go in the ordinary CI (unlike the RLS regressions, which skip without TEST_RLS_DATABASE_URL).
"""

from __future__ import annotations

from src.api.cabinet import workflow_service as svc
from src.bots.telegram import STATUS_LABELS as BOT_LABELS
from src.db.models.tender_status import STATUS_LABELS, STATUSES
from src.workers.reminders import _WATCH_STATUSES


def test_bot_labels_mirror_model_labels():
    """The bot and the web label the stages IDENTICALLY.

    Both copies describe one row of `tender_user_status`. If they diverge — the user sees “✅ Participating” in the bot, and another word about the same tender in the cabinet.
    It is fixed by the reduction to `src.db.models.tender_status.STATUS_LABELS`, not by tailoring the test.
    """
    assert BOT_LABELS == STATUS_LABELS


def test_labels_cover_exactly_the_funnel():
    """Each stage has a label and there are no extra labels (the UI will not show a KeyError)."""
    assert tuple(STATUS_LABELS) == STATUSES


def test_web_reminder_stages_mirror_worker_predicate():
    """The `REMINDER_STATUSES` of the web is the mirror of `_WATCH_STATUSES` of the worker, not a copy.

    The cabinet promises in the UI (“the reminders will come on these stages”) exactly what the worker really sends by. If they change the predicate in the worker, the promise becomes a lie, and this must fall here.
    """
    assert set(svc.REMINDER_STATUSES) == set(_WATCH_STATUSES)


def test_web_settable_status_active_is_visible_to_reminders():
    """The cabinet→TG link: `active`, set from the web, falls under the predicate of the worker.

    The key promise of VG-17w (“marked Participating in the cabinet → the reminder in TG will fire”) holds on three facts, and each is checked here: the stage is reachable from the web, the worker sends by exactly it, and it is one table.
    """
    assert "active" in STATUSES, "a stage cannot be set from the web"
    assert "active" in _WATCH_STATUSES, "the worker does not send reminders by the stage"
    assert "tender_user_status" in str(svc._SET_STATUS_SQL)


def test_web_writes_the_status_it_was_given():
    """The web writes the PASSED stage, not a hard-coded constant.

    The mutation guard: replace `EXCLUDED.status`/the bind with a literal (for example 'watching') — the test turns red. Without it the record “I'm in” would silently become “Studying”, and part of the reminders would not arrive.
    """
    sql = str(svc._SET_STATUS_SQL)
    assert ":status" in sql, "the stage must come through the bind"
    assert "SET status = EXCLUDED.status" in sql, "the update must take the new stage"
    for literal in ("'watching'", "'active'", "'new'"):
        assert literal not in sql, f"the hard-coded stage {literal} in the SQL of the stage change"


def test_my_page_predicate_is_derived_from_statuses():
    """The predicate “my tender” is derived from STATUSES, and not written as a literal.

    If a stage is added to the funnel — /app/my and the CSV export will pick it up by themselves.
    """
    for status in STATUSES:
        if status == "new":
            assert f"'{status}'" not in svc._MY_STATUS_IN, "the default new — is not “mine”"
        else:
            assert f"'{status}'" in svc._MY_STATUS_IN
