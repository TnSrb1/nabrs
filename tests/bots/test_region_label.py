"""The region labels in the bot are taken from the common directory (SITE-14).

The bot wizard offers only “All regions”, but the district can be set from
the cabinet — and then the bot must SHOW it by its human name, not by the code «RS-05».
"""

from __future__ import annotations

from src.bots.telegram import _ALL_REGIONS, _REGION_OPTIONS, _region_label
from src.domain.regions import OKRUG_CHOICES, OKRUG_LABELS


def test_all_regions_option_is_the_only_wizard_choice() -> None:
    """The bot wizard does not let one choose a district yet — a keyboard of 30 buttons is a separate task."""
    assert _REGION_OPTIONS == [(_ALL_REGIONS, "Все регионы")]


def test_synthetic_all_regions_label() -> None:
    assert _region_label(_ALL_REGIONS) == "Все регионы"


def test_labels_come_from_shared_catalog() -> None:
    """A profile with a district from the cabinet is read in the bot by name, not by code."""
    for code, label in OKRUG_CHOICES:
        assert _region_label(code) == label
    assert _region_label("RS-05") == OKRUG_LABELS["RS-05"]
    assert _region_label("RS-05") != "RS-05"


def test_unknown_code_falls_back_to_itself() -> None:
    assert _region_label("RS-99") == "RS-99"
