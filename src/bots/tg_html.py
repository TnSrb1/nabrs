"""The splitting of the HTML reply of the bot into Telegram messages (the limit of 4096 characters).

One helper for all AI replies of the bot (verdict / checklist / draft / L3 analysis):
- a text ≤ the limit → one message, unchanged;
- longer → cut at a paragraph boundary (``\\n\\n``), then at a line, then at a space,
  hard — only if otherwise impossible; the tags open at the boundary are closed at the
  end of the piece and reopened at the start of the next one (the markup of each piece is paired);
- the footer (for example, the link to the full analysis in the cabinet) — into the last piece,
  if it fits, otherwise as a separate message.

The reserve for reopening the tags — ``_TAG_RESERVE`` characters: the markup of the service —
``<b>``/``<i>``, nesting no deeper than two; ``<a href>`` does not get into the body of the reply
(the text of the models is escaped via ``html.escape``).
"""

from __future__ import annotations

import re

TG_MAX_CHARS = 4096
_TAG_RESERVE = 256
_SEPARATORS = ("\n\n", "\n", " ")
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def _safe_cut(text: str, cut: int) -> int:
    """Backs off from the cut point to the start of an unclosed ``<`` tag or a ``&`` entity.

    Otherwise a hard cut breaks the markup: the piece ends on ``<b`` or ``&amp``,
    and Telegram answers with an HTML parsing error.
    """
    head = text[:cut]
    lt = head.rfind("<")
    if lt != -1 and ">" not in head[lt:]:
        cut = lt
    amp = head.rfind("&")
    if amp != -1 and ";" not in head[amp:]:
        cut = min(cut, amp)
    return cut


def _units(text: str, budget: int) -> list[str]:
    """Cuts the text into pieces ≤ budget: first by paragraph, then by line, by space, hard.

    The cut does not fall inside a tag or an entity (:func:`_safe_cut`).
    """
    out: list[str] = []
    while len(text) > budget:
        for sep in _SEPARATORS:
            cut = text.rfind(sep, 0, budget + 1)
            if cut > 0:
                break
        else:
            cut, sep = budget, ""
        safe = _safe_cut(text, cut)
        if 0 < safe < cut:
            cut, sep = safe, ""
        out.append(text[:cut])
        text = text[cut + len(sep) :]
    out.append(text)
    return out


def _balance_tags(parts: list[str]) -> list[str]:
    """Closes the tags open at the end of the piece, and reopens them in the next one."""
    stack: list[tuple[str, str]] = []  # (the tag name, the opening tag as in the text)
    out: list[str] = []
    for part in parts:
        prefix = "".join(open_tag for _, open_tag in stack)
        for match in _TAG_RE.finditer(part):
            closing, name = match.group(1), match.group(2).lower()
            if closing:
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == name:
                        del stack[i:]
                        break
            else:
                stack.append((name, match.group(0)))
        suffix = "".join(f"</{name}>" for name, _ in reversed(stack))
        out.append(prefix + part + suffix)
    return out


def split_tg_html(
    text: str, *, footer: str = "", max_chars: int = TG_MAX_CHARS
) -> list[str]:
    """HTML text → a list of Telegram messages, each ≤ max_chars.

    ``footer`` is added only when splitting (the text is longer than the limit): to the end of
    the last piece, if it fits, otherwise as a separate message.
    """
    if len(text) <= max_chars:
        return [text]
    reserve = min(_TAG_RESERVE, max_chars // 4)
    budget = max_chars - reserve
    parts: list[str] = []
    buf: list[str] = []
    for unit in _units(text, budget):
        if buf and len("\n\n".join(buf + [unit])) > budget:
            parts.append("\n\n".join(buf))
            buf = []
        buf.append(unit)
    parts.append("\n\n".join(buf))
    parts = _balance_tags(parts)
    if footer:
        if len(parts[-1]) + len("\n\n") + len(footer) <= max_chars:
            parts[-1] += "\n\n" + footer
        else:
            parts.append(footer)
    return parts
