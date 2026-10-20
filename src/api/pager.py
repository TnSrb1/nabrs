"""The window of page numbers for the pager (shared by the cabinet and the showcase).

One algorithm for all four output places: the first, the last and the window around the current one; the gaps — ``None`` (an ellipsis). One hidden page between two visible ones is shown as a digit, not an ellipsis: “… 5 … 7” saves nothing.
"""

from __future__ import annotations

from fastapi import Query

PAGER_RADIUS = 2
# The ceiling of the NUMBER from the query: longer — not an error, but “far beyond the end” (clamped by total).
# 10^6 pages × 50 = 5·10^7 rows of OFFSET — certainly more than any result set; int8 does not overflow.
PAGE_MAX = 10**6


def pager_pages(page: int, total: int, radius: int = PAGER_RADIUS) -> list[int | None]:
    """The numbers for output: ``[1, None, 4, 5, 6, 7, 8, None, 120]``; ``None`` — a gap.

    ``total <= 1`` — an empty list (the pager is not output). ``page`` is clamped to ``[1, total]``, so that a call with garbage does not draw non-existent pages.
    """
    if total <= 1:
        return []
    page = min(max(page, 1), total)
    shown = {1, total} | set(
        range(max(1, page - radius), min(total, page + radius) + 1)
    )
    # One skipped page — we show it; an ellipsis for a single number is not needed.
    for n in range(2, total):
        if n not in shown and (n - 1) in shown and (n + 1) in shown:
            shown.add(n)
    out: list[int | None] = []
    prev = 0
    for n in sorted(shown):
        if n - prev > 1:
            out.append(None)
        out.append(n)
        prev = n
    return out


def parse_page(raw: str) -> int:
    """The page number from ``?page=``: not a number, empty, ≤0 → 1; huge → ``PAGE_MAX``.

    ASCII digits only (``"²"`` and ``"١"`` are not numbers). The route further clamps to the last one.
    """
    s = raw.strip()
    if not (s.isascii() and s.isdigit()):
        return 1
    return max(1, min(int(s), PAGE_MAX))


def page_param(page: str = Query(default="1")) -> int:
    """FastAPI dependency: ``page: int = Depends(page_param)`` — garbage does not give a 422."""
    return parse_page(page)


#: Tokens for the template: (kind, number). ``num`` / ``gap`` — always visible; ``far`` (the window ±2
#: beyond ±1) — only at ≥640px; ``gap_m`` — an ellipsis at <640px, where ``far`` is hidden.
def pager_tokens(page: int, total: int) -> list[tuple[str, int | None]]:
    """The window ±2 for a wide screen and ±1 for a narrow one — one token sequence.

    A number at distance 2 from the current one (``far``) is hidden at <640px. If a series of hidden ones remains between two visible numbers, ONE ``gap_m`` is put — otherwise the narrow screen would show “5 6” right next to each other, without a sign of a skip. Next to an already output ellipsis (``gap``) ``gap_m`` is not put — otherwise “… …”.
    """
    wide = pager_pages(page, total)
    narrow = {n for n in pager_pages(page, total, 1) if n is not None}
    out: list[tuple[str, int | None]] = []
    for n in wide:
        if n is None:
            out.append(("gap", None))
        elif n in narrow:
            out.append(("num", n))
        else:
            out.append(("far", n))
    # A series of far between two num is a gap that becomes mute on a narrow screen.
    result: list[tuple[str, int | None]] = []
    i = 0
    while i < len(out):
        if out[i][0] != "far":
            result.append(out[i])
            i += 1
            continue
        j = i
        while j < len(out) and out[j][0] == "far":
            j += 1
        if result and result[-1][0] == "num" and j < len(out) and out[j][0] == "num":
            result.append(("gap_m", None))
        result.extend(out[i:j])
        i = j
    return result
