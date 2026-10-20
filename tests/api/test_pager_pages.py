"""The window of page numbers of the pager: expectations — literals, not via the same algorithm."""

from src.api.pager import pager_pages


def test_single_page_has_no_pager():
    assert pager_pages(1, 1) == []


def test_zero_total_has_no_pager():
    assert pager_pages(1, 0) == []


def test_small_total_lists_every_page_without_gaps():
    assert pager_pages(1, 5) == [1, 2, 3, 4, 5]


def test_first_page_of_ten_skips_the_middle_with_ellipsis():
    assert pager_pages(1, 10) == [1, 2, 3, None, 10]


def test_middle_page_of_120_has_both_ellipses():
    assert pager_pages(6, 120) == [1, None, 4, 5, 6, 7, 8, None, 120]


def test_deep_middle_page_has_ellipsis_on_both_sides():
    assert pager_pages(60, 120) == [1, None, 58, 59, 60, 61, 62, None, 120]


def test_last_page_of_120_mirrors_first():
    assert pager_pages(120, 120) == [1, None, 118, 119, 120]


def test_single_hidden_page_is_shown_not_elided():
    # page=2, total=6: the window 1..4 + the last 6; only 5 is skipped → a digit, not “…”.
    assert pager_pages(2, 6) == [1, 2, 3, 4, 5, 6]


def test_out_of_range_page_is_clamped():
    assert pager_pages(9999, 120) == pager_pages(120, 120)
    assert pager_pages(0, 10) == pager_pages(1, 10)


from src.api.pager import PAGE_MAX, parse_page  # noqa: E402


def test_parse_page_plain_number():
    assert parse_page("7") == 7
    assert parse_page(" 7 ") == 7


def test_parse_page_junk_zero_negative_is_first_page():
    for raw in ["", "abc", "0", "-5", "1.5", "1e3", "2²", "١"]:
        assert parse_page(raw) == 1, raw


def test_parse_page_huge_is_capped_not_rejected():
    assert parse_page("9999") == 9999
    assert parse_page("999999999999999999999") == PAGE_MAX == 10**6


from src.api.pager import pager_tokens  # noqa: E402


def _visible_narrow(tokens):
    """What the narrow screen sees: distant numbers are hidden, gap_m — an ellipsis."""
    return [
        "…" if kind in ("gap", "gap_m") else n for kind, n in tokens if kind != "far"
    ]


def test_tokens_middle_page_wide_has_far_numbers_and_gaps():
    assert pager_tokens(6, 120) == [
        ("num", 1), ("gap", None), ("far", 4), ("num", 5), ("num", 6), ("num", 7),
        ("far", 8), ("gap", None), ("num", 120),
    ]  # fmt: skip


def test_tokens_narrow_view_of_middle_page_is_one_row_with_ellipses():
    assert _visible_narrow(pager_tokens(6, 120)) == [1, "…", 5, 6, 7, "…", 120]


def test_tokens_run_of_hidden_numbers_between_visible_gets_one_marker():
    # 7 pages, page 3: 5 and 6 are hidden on the narrow one — ONE ellipsis, not two and not zero.
    assert pager_tokens(3, 7) == [
        ("num", 1), ("num", 2), ("num", 3), ("num", 4), ("gap_m", None),
        ("far", 5), ("far", 6), ("num", 7),
    ]  # fmt: skip
    assert _visible_narrow(pager_tokens(3, 7)) == [1, 2, 3, 4, "…", 7]


def test_tokens_hidden_run_next_to_real_gap_adds_no_second_ellipsis():
    assert _visible_narrow(pager_tokens(4, 10)) == [1, 2, 3, 4, 5, "…", 10]
    assert pager_tokens(4, 10)[-2] == ("gap", None)
