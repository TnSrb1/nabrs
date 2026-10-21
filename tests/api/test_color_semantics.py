"""One meaning — one color: blue `.badge-new` only for time and fit score.

Canon of color roles (the project rules, “Frontend”): gray = “expired/off” · orange `--status-urgent` = only time urgency · blue `--status-new` = “time still left” AND fit score · amber `--color-accent-*` = action and upsell · `.badge-muted` = neutral metadata (translation, stage, label).

The defect this guard closes: blue spread to a THIRD meaning — the funnel stage “new”, the “Novo” label on the public card, the “AI” label on the landing page, the plan in the feature table and in the subscription, admin labels (plan, activity type, audit action). All of them are metadata; their place is `.badge-muted`.
"""

from __future__ import annotations

import pathlib
import re

_TPL = pathlib.Path("src/api/templates")

# Files where blue IS legitimate, and the role it serves there.
_ALLOWED = {
    "_feed_card.html": "дней до дедлайна",
    "_cockpit_feed_teaser.html": "дней до дедлайна",
    "_my_tender_row.html": "дней до дедлайна",
    "_agenda_item.html": "дней до дедлайна",
    "tender.html": "оценка соответствия N/5",
    "_ai_artifact.html": "оценка соответствия N/5",
}

_BADGE_NEW_RE = re.compile(r"badge-new")


def _files_using_blue() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(_TPL.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        # Jinja comments are not counted: they explain WHY blue was not taken
        text = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
        hits = len(_BADGE_NEW_RE.findall(text))
        if hits:
            out[path.name] = hits
    return out


def test_blue_badge_only_for_time_and_match_score():
    """Blue must not appear outside the two pinned roles.

    Mutation: return `badge-new` to the funnel status in `_tender_actions.html`
    (or to the “Novo” label in `tender.html`) → the test goes red and names the file.
    """
    used = _files_using_blue()
    unexpected = {name: n for name, n in used.items() if name not in _ALLOWED}
    assert not unexpected, (
        "blue .badge-new outside the “time”/“fit score” roles: "
        f"{unexpected}. Metadata (stage, label, plan, activity type) — "
        "it is .badge-muted, see the color-role canon in the project rules"
    )


def test_time_scale_keeps_blue():
    """The deadline scale must not accidentally lose blue during refactoring."""
    for name in ("_feed_card.html", "_my_tender_row.html", "_agenda_item.html"):
        text = (_TPL / name).read_text(encoding="utf-8")
        assert "badge-new" in text, f"{name}: the time scale lost blue"


def test_match_score_keeps_blue():
    """Fit score is the second legitimate role of blue (canon)."""
    for name in ("tender.html", "_ai_artifact.html"):
        text = (_TPL / name).read_text(encoding="utf-8")
        assert re.search(r"badge-new[^>]*>\s*\{\{[^}]*match_score", text), name


def test_funnel_stage_is_muted_not_blue():
    """Funnel stage is metadata: a neutral badge, not “time still left”."""
    text = (_TPL / "_tender_actions.html").read_text(encoding="utf-8")
    text_wo_comments = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    assert "badge-muted" in text_wo_comments
    assert "badge-new" not in text_wo_comments


def test_accent_badge_is_defined_and_contrast_checked():
    """Amber badge — the “action and upsell” role; contrast is computed, not eyeballed.

    Mutation: return the fill #fdf6e8 → text contrast 4.47 < 4.5, the test goes red.
    """
    css = pathlib.Path("src/api/static/css/app.css").read_text(encoding="utf-8")
    assert ".badge-accent" in css, "amber badge component is not declared"
    assert "--color-accent-50: #fffbf3" in css, "fill chosen by contrast"

    def _lum(hexc: str) -> float:
        c = hexc.lstrip("#")
        parts = [int(c[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        conv = [
            x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in parts
        ]
        return 0.2126 * conv[0] + 0.7152 * conv[1] + 0.0722 * conv[2]

    def _ratio(a: str, b: str) -> float:
        la, lb = _lum(a), _lum(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    # Badge text is small (12–14px): threshold 4.5. Border — non-text: 3.0.
    assert _ratio("#9c6714", "#fffbf3") >= 4.5
    assert _ratio("#c07f1a", "#fffbf3") >= 3.0
    assert _ratio("#c07f1a", "#ffffff") >= 3.0


def test_landing_ai_label_uses_accent_not_blue():
    """The “AI” label on the landing page is amber (accent), not blue (time/score)."""
    html = (_TPL / "landing.html").read_text(encoding="utf-8")
    assert 'class="badge badge-accent">AI<' in html
    assert "badge-new" not in re.sub(r"\{#.*?#\}", "", html, flags=re.S)
