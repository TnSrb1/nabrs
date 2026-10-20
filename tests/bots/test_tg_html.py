"""The helper that splits the bot’s HTML answer under the Telegram limit (#634): literals, not an echo of the code."""

from __future__ import annotations

import re

from src.bots.tg_html import TG_MAX_CHARS, split_tg_html


def _balanced(chunk: str, name: str) -> bool:
    return len(re.findall(rf"<{name}>", chunk)) == len(re.findall(rf"</{name}>", chunk))


def test_short_text_is_returned_unchanged():
    assert split_tg_html("<b>OK</b>") == ["<b>OK</b>"]


def test_limit_literal_is_4096():
    assert TG_MAX_CHARS == 4096


def test_9000_chars_split_into_parts_under_limit_with_valid_markup():
    block = "<b>Раздел</b>\n" + "а" * 900
    text = "\n\n".join([block] * 10)  # ~9 000 characters
    assert len(text) > 9000
    parts = split_tg_html(text)
    assert len(parts) >= 3
    for part in parts:
        assert len(part) <= 4096
        assert _balanced(part, "b")


def test_paragraph_boundaries_are_kept_whole():
    blocks = [
        "<b>A</b>\n" + "x" * 3000,
        "<b>B</b>\n" + "y" * 3000,
        "<b>C</b>\n" + "z" * 3000,
    ]
    parts = split_tg_html("\n\n".join(blocks))
    assert parts == blocks  # each paragraph whole, in its own message


def test_open_tag_across_hard_cut_is_closed_and_reopened():
    # one paragraph without line breaks, wrapped in <b> — a hard cut
    text = "<b>" + "ф" * 6000 + "</b>"
    parts = split_tg_html(text)
    assert len(parts) == 2
    assert parts[0].startswith("<b>") and parts[0].endswith("</b>")
    assert parts[1].startswith("<b>") and parts[1].endswith("</b>")
    for part in parts:
        assert len(part) <= 4096


def test_footer_goes_to_last_part_when_it_fits():
    text = "\n\n".join(["<b>A</b>\n" + "x" * 3000, "<b>B</b>\n" + "y" * 3000])
    parts = split_tg_html(
        text, footer='… <a href="https://app.nab.rs/app/tenders/t">полный разбор</a>'
    )
    assert len(parts) == 2
    assert parts[-1].endswith("полный разбор</a>")
    assert "полный разбор" not in parts[0]


def test_footer_is_its_own_message_when_last_part_is_full():
    # the second part 3830 characters; the footer of 300 does not fit into its tail up to 4096
    text = "\n\n".join(["<b>A</b>\n" + "x" * 3800, "y" * 3830])
    footer = "F" * 300
    parts = split_tg_html(text, footer=footer)
    assert len(parts) == 3
    assert parts[-1] == footer
    assert all(len(p) <= 4096 for p in parts)


def test_no_footer_when_text_fits():
    assert split_tg_html("short", footer="FOOTER") == ["short"]


def _unclosed_markup(part: str) -> bool:
    """The piece ends inside a tag (`<b`) or an entity (`&amp`) — a broken markup."""
    return bool(re.search(r"<[^>]*$", part) or re.search(r"&[a-zA-Z]*$", part))


def test_hard_cut_never_splits_tag_or_entity():
    # without spaces: a hard cut at 3840. The boundary 1 falls inside <b>,
    # the boundary 2 — inside &amp;
    text = "a" * 3838 + "<b>" + "x" * 3835 + "&amp;" + "y" * 1315 + "</b>"
    assert len(text) == 9000
    parts = split_tg_html(text)
    assert len(parts) >= 3
    for part in parts:
        assert len(part) <= 4096
        assert not _unclosed_markup(part)
        assert _balanced(part, "b")
    # nothing is lost and nothing is duplicated
    assert sum(p.count("x") for p in parts) == 3835
    assert sum(p.count("y") for p in parts) == 1315


def test_exactly_4096_plain_chars_is_one_message():
    assert split_tg_html("a" * 4096) == ["a" * 4096]


def test_4097_plain_chars_is_two_messages():
    parts = split_tg_html("a" * 4097)
    assert len(parts) == 2
    assert all(len(p) <= 4096 for p in parts)
    assert sum(len(p) for p in parts) == 4097


def test_nested_tags_on_boundary_are_closed_and_reopened_in_order():
    # <b><i> are opened across the boundary 3840: the first piece closes </i></b>,
    # the second reopens <b><i> in the same order
    text = "<b><i>" + "a" * 4200 + "</i></b>"
    parts = split_tg_html(text)
    assert len(parts) == 2
    assert parts[0].endswith("</i></b>")
    assert parts[1].startswith("<b><i>")
    assert parts[1].endswith("</i></b>")
