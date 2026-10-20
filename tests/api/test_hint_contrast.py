"""Hint contrast ≥ 4.5:1 (WCAG AA, audit 07.10).

`--color-neutral-500` (#64748b) on the surface `--color-primary-50` (#eef2f9) = 4.24:1 — below AA. Raised to `--color-neutral-600` (#475569) ONLY for four selectors; tokens and the palette are not changed. Assertions — literals from the fact (hex and selector); the WCAG formula here is the only calculation.

Measurement: `.kw-chip__remove` (×8 on /profiles/{id}/edit), `.preview-live__hint` (×2),
`.field__hint` (×1) — background of `.kw-chip` / `.preview-live` = #eef2f9; `.tpl-card__desc` on
`/profiles/new` — background of `.tpl-card` = #ffffff.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_CSS = pathlib.Path("src/api/static/css/app.css").read_text(encoding="utf-8")

_NEUTRAL_600 = "#475569"
_PRIMARY_50 = "#eef2f9"
_WHITE = "#ffffff"


def _lum(hexc: str) -> float:
    vals = [int(hexc[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in vals]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted([_lum(a), _lum(b)], reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rule_body(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", _CSS)
    assert m, f"rule {selector} not found in app.css"
    return m.group(1)


@pytest.mark.parametrize(
    "selector,bg",
    [
        (".kw-chip__remove", _PRIMARY_50),
        (".preview-live__hint", _PRIMARY_50),
        (".field__hint", _WHITE),
        (".tpl-card__desc", _WHITE),
    ],
)
def test_hint_selectors_use_neutral_600(selector, bg):
    body = _rule_body(selector)
    assert "color: var(--color-neutral-600)" in body
    assert "color: var(--color-neutral-500)" not in body
    assert _ratio(_NEUTRAL_600, bg) >= 4.5


def test_neutral_600_token_is_literal_475569():
    assert re.search(r"--color-neutral-600:\s*#475569;", _CSS)
