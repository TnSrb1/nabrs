"""Block "Requirements by document" for Telegram (stepwise): ≤10 main ones + a reference to the site.

The journal is written by the code of step-wise mode (:mod:`src.ai.stepwise`); here it is only displayed.
The full list with quotes lives in the cabinet (``_ai_artifact.html``); in the chat —
a short one, so as not to bloat an analysis that is already long.
"""

from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

from src.ai.schemas import REQUIREMENT_KINDS
from src.bots.i18n import bot_t

#: How many requirements we show in the chat.
BOT_REQUIREMENTS_LIMIT = 10

# Priority of "main": deadlines and security/eligibility matter more than the rest.
_PRIORITY = {kind: i for i, kind in enumerate(REQUIREMENT_KINDS)}


def render_requirements(output: Mapping[str, Any], locale: str | None) -> str | None:
    """HTML section of requirements, or ``None`` if there is no journal (regular analysis)."""
    reqs: Sequence[Mapping[str, Any]] = output.get("requirements") or []
    if not reqs:
        return None
    ranked = sorted(
        enumerate(reqs),
        key=lambda ir: (_PRIORITY.get(str(ir[1].get("kind")), 99), ir[0]),
    )
    lines: list[str] = []
    for _, r in ranked[:BOT_REQUIREMENTS_LIMIT]:
        kind = str(r.get("kind") or "other")
        label = bot_t(locale, f"da_k_{kind if kind in _PRIORITY else 'other'}")
        doc = str(r.get("doc") or "")
        tail = f" — <i>{html.escape(doc)}</i>" if doc else ""
        lines.append(
            f"  • <b>{html.escape(label)}:</b> {html.escape(str(r.get('text') or ''))}{tail}"
        )
    head = f"📑 <b>{bot_t(locale, 'da_req_title')}</b>\n" + "\n".join(lines)
    if len(reqs) > BOT_REQUIREMENTS_LIMIT:
        head += "\n" + html.escape(bot_t(locale, "da_req_more").format(n=len(reqs)))
    meta = output.get("stepwise") or {}
    done, total = int(meta.get("chunks_done") or 0), int(meta.get("chunks_total") or 0)
    if total and done < total:
        head += (
            "\n⚠️ <i>"
            + html.escape(
                bot_t(locale, "da_req_partial").format(done=done, total=total)
            )
            + "</i>"
        )
    return head
