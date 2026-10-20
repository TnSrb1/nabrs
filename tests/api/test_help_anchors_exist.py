"""Every ``/help#<anchor>`` link from the code leads to an EXISTING help section.

The dashboard sent to ``/help#how`` in three places, the template form to ``/help#profile``;
such sections do not exist (the ids of the help sections — ``HELP[lang]["sections"][*]["id"]``), and
the browser silently left the client at the top of the page. The guard scans ALL templates,
``.py`` and ``.js`` under ``src/`` with one predicate — not only the dashboard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.api.public.i18n import HELP

SRC = Path(__file__).resolve().parents[2] / "src"
_LINK = re.compile(r"/help#([A-Za-z0-9_-]+)")


def _links() -> list[tuple[str, str]]:
    found = []
    for path in SRC.rglob("*"):
        if path.suffix not in {".html", ".py", ".js"}:
            continue
        for anchor in _LINK.findall(path.read_text(encoding="utf-8")):
            found.append((str(path.relative_to(SRC)), anchor))
    return found


def test_scan_finds_help_links():
    """Applicability control: the scan is not empty (otherwise the guard is always silent)."""
    assert ("api/templates/dashboard.html", "start") in _links()


@pytest.mark.parametrize("lang", sorted(HELP))
def test_every_help_anchor_exists(lang):
    ids = {s["id"] for s in HELP[lang]["sections"]}
    broken = sorted({(f, a) for f, a in _links() if a not in ids})
    assert not broken, f"/help#… without a section in HELP[{lang}]: {broken}"
