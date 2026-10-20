"""The guard: not a single refusal by the limit goes to the client SILENTLY.

The requirement of the owner of 15.08.2026, verbatim: “nothing must switch off silently! the subscriber must see that he has exhausted the limit (this must work for all the limited services)”. The requirement is about the PLACE, not about the path to it, so it is closed by a predicate over ALL the channels at once: the web (the AI buttons, the translation), the bot (four buttons), the worker (the auto-analysis of the flow).

How the guard works:

1. by a predicate we look for ALL the places in ``src/`` where the code turns the decision of the limit (``exhausted``/``demo_exhausted``) into its own result — these are the “refusal points”;
2. each must be in the registry :data:`REFUSAL_POINTS` together with the PROOF of display: the file where the refusal is rendered/sent, and the substring in it;
3. the proofs are checked by reading the files, and the catalog keys — by their existence in all three languages.

‼️ A guard that scans a PART of the sources creates a blind zone — and it will certainly misfire (the real case #492: the check of the classes read only the templates, while the JS set five non-existent ones). Therefore step 1 is mandatory: a new refusal point will drop the test until a path of display is provided for it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src.api.cabinet.i18n import CABINET, SUPPORTED

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# A refusal point → by what it is proved that the client will SEE it.
#  producer — the file where the decision of the limit becomes the result;
#  proof     — (the file of the display, a substring in it);
#  keys      — the keys of the catalog of the cabinet chrome (they must be in all three languages).
REFUSAL_POINTS: dict[str, dict] = {
    "web: кнопки AI в карточке (bid/checklist/draft)": {
        "producer": "api/cabinet/tender_ai_generate.py",
        "proof": ("api/templates/_ai_slot.html", "mode == 'limit_exhausted'"),
        "keys": ("slot_limit_title", "slot_limit_hint"),
    },
    "web: перевод карточки и описания": {
        "producer": "api/cabinet/routes_tender_ai.py",
        "proof": (
            "api/templates/_tender_description.html",
            "block_reason == 'exhausted'",
        ),
        "keys": ("tr_block_exhausted", "ct_block_exhausted"),
    },
    "web: АВТО-разбор потока (деградация в доставку без разбора)": {
        "producer": "workers/ai.py",
        "proof": ("api/templates/_ai_slot.html", "mode == 'auto_skipped'"),
        "keys": (
            "slot_auto_skipped_title",
            "slot_auto_demo_hint",
            "slot_auto_limit_hint",
        ),
    },
    "бот: вердикт «стоит ли участвовать»": {
        "producer": "bots/bid_verdict_service.py",
        "proof": ("bots/telegram.py", 'result.status == "limit_exhausted"'),
        "keys": (),
    },
    "бот: чек-лист требований": {
        "producer": "bots/compliance_checklist_service.py",
        "proof": ("bots/telegram.py", 'result.status == "limit_exhausted"'),
        "keys": (),
    },
    "бот: черновик заявки": {
        "producer": "bots/draft_service.py",
        "proof": ("bots/telegram.py", 'result.status == "limit_exhausted"'),
        "keys": (),
    },
    "бот: глубокий разбор": {
        "producer": "bots/deep_analysis_service.py",
        "proof": ("bots/telegram.py", 'result.status == "limit_exhausted"'),
        "keys": (),
    },
    "бот: ранний гейт (alert ДО callback.answer, все четыре кнопки)": {
        "producer": "bots/ai_gate.py",
        "proof": ("bots/telegram.py", 'gate.status == "limit_exhausted"'),
        "keys": (),
    },
}

# The files that DECIDE, rather than refuse: the source of the decision itself and its mirror in the
# UI service. They are not “refusal points” — the code from the registry uses their output.
_DECISION_SOURCES = {
    "ai/usage.py",  # limit_decision / auto_limit_decision — produce the rows
    "api/cabinet/ai_limits_service.py",  # reads the snapshot of the spend for the display
    "api/cabinet/i18n.py",  # the texts
}

# A refusal point = a place that CALLS the gate of the limit. The predicate is by the call, not by
# the comparison with a string: the refusal is written in different ways (``== "exhausted"`` for the button levels,
# levels, ``!= “ok”`` for the auto-analysis), but ALL call the gate — «a new paid call
# = the gate BEFORE the call» (CLAUDE.md). The comparison with a string is left as a second
# signal: it catches also the wrappers that merely retell the decision.
_GATE_CALL_RE = re.compile(r"\b(auto_)?limit_decision\s*\(")
_REFUSAL_RE = re.compile(r"""==\s*["'](exhausted|demo_exhausted)["']""")


def _producers() -> set[str]:
    """All files of src/ where the decision of the limit turns into a refusal (the predicate)."""
    found = set()
    for path in _SRC.rglob("*.py"):
        rel = str(path.relative_to(_SRC))
        if rel in _DECISION_SOURCES:
            continue
        text = path.read_text(encoding="utf-8")
        if _GATE_CALL_RE.search(text) or _REFUSAL_RE.search(text):
            found.add(rel)
    return found


def test_every_refusal_point_is_registered():
    """A new refusal point must get into the registry — together with the path of the display.

    Exactly this assert closes the blind zone: until the developer shows WHERE the client will see the refusal, the test is red.
    """
    registered = {p["producer"] for p in REFUSAL_POINTS.values()}
    unregistered = _producers() - registered
    assert not unregistered, (
        "refusal points of the limit without a path of display to the client: "
        f"{sorted(unregistered)} — add to REFUSAL_POINTS together with the proof"
    )


@pytest.mark.parametrize("name", sorted(REFUSAL_POINTS))
def test_refusal_point_producer_exists(name):
    point = REFUSAL_POINTS[name]
    assert (_SRC / point["producer"]).exists(), point["producer"]


@pytest.mark.parametrize("name", sorted(REFUSAL_POINTS))
def test_refusal_point_has_visible_path(name):
    """Each refusal point has a display branch for the client (a template/a bot handler)."""
    proof_file, needle = REFUSAL_POINTS[name]["proof"]
    path = _SRC / proof_file
    assert path.exists(), proof_file
    assert needle in path.read_text(encoding="utf-8"), (
        f"{name}: in {proof_file} there is no display branch ({needle!r}) — "
        "the refusal by the limit would reach the client silently"
    )


@pytest.mark.parametrize("name", sorted(REFUSAL_POINTS))
def test_refusal_point_keys_exist_in_all_languages(name):
    """The text of the refusal — in all three languages (half of the channel in Russian is not admissible)."""
    for key in REFUSAL_POINTS[name]["keys"]:
        for lang in SUPPORTED:
            assert key in CABINET[lang], f"{key} is absent in the catalog {lang}"
            assert CABINET[lang][key].strip(), f"{key} is empty in the catalog {lang}"


def test_auto_analysis_refusal_names_the_numbers():
    """The refusal of the auto-analysis names the NUMBERS, rather than “the limit is exhausted” in general.

    “N of M” is the only thing that lets the subscriber understand whether to buy a plan; without the numbers the message is indistinguishable from a failure.
    """
    for lang in SUPPORTED:
        for key in ("slot_auto_demo_hint", "slot_auto_limit_hint"):
            text = CABINET[lang][key]
            assert "{used}" in text and "{limit}" in text, f"{key}/{lang}: no numbers"
