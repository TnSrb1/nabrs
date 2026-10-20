"""Tests of the in-memory rate limit (STAGE 2)."""

from __future__ import annotations

from src.api.rate_limit import FixedWindowRateLimiter


def test_allows_up_to_max_then_blocks():
    rl = FixedWindowRateLimiter(max_hits=3, window_sec=100)
    assert rl.hit("k", now=0.0) is True
    assert rl.hit("k", now=1.0) is True
    assert rl.hit("k", now=2.0) is True
    assert rl.hit("k", now=3.0) is False


def test_window_expiry_frees_slot():
    rl = FixedWindowRateLimiter(max_hits=1, window_sec=10)
    assert rl.hit("k", now=0.0) is True
    assert rl.hit("k", now=5.0) is False
    assert rl.hit("k", now=11.0) is True  # the t=0 mark left the window


def test_keys_are_independent():
    rl = FixedWindowRateLimiter(max_hits=1, window_sec=10)
    assert rl.hit("a", now=0.0) is True
    assert rl.hit("b", now=0.0) is True
    assert rl.hit("a", now=1.0) is False


def test_reset_clears_state():
    rl = FixedWindowRateLimiter(max_hits=1, window_sec=10)
    assert rl.hit("k", now=0.0) is True
    rl.reset()
    assert rl.hit("k", now=1.0) is True


def test_magic_link_limit_still_three_per_window():
    """Regression of the product limit of magic-link: 3 e-mails per 15 minutes."""
    rl = FixedWindowRateLimiter(max_hits=3, window_sec=15 * 60)
    assert [rl.hit("a@example.com", now=float(i)) for i in range(4)] == [
        True,
        True,
        True,
        False,
    ]
    # the t=0 mark left the window — exactly one slot was freed, not more
    assert rl.hit("a@example.com", now=15 * 60 + 0.5) is True
    assert rl.hit("a@example.com", now=15 * 60 + 0.6) is False


def test_expired_keys_do_not_pile_up():
    """Keys that are no longer accessed leave the dictionary (OOM vector)."""
    rl = FixedWindowRateLimiter(max_hits=3, window_sec=10)
    for i in range(1000):
        assert rl.hit(f"burst-{i}@example.com", now=0.0) is True
    assert len(rl._hits) == 1000

    # an access after the window: all 1000 marks expired, the dictionary shrinks
    assert rl.hit("later@example.com", now=100.0) is True
    assert len(rl._hits) == 1


def test_sweep_keeps_keys_with_live_stamps():
    """The cleanup does not touch the keys whose marks are still in the window."""
    rl = FixedWindowRateLimiter(max_hits=1, window_sec=10)
    assert rl.hit("old@example.com", now=0.0) is True
    assert rl.hit("live@example.com", now=9.0) is True

    # t=11: the old mark expired, live (t=9) — still in the window
    assert rl.hit("trigger@example.com", now=11.0) is True
    assert set(rl._hits) == {"live@example.com", "trigger@example.com"}
    assert rl.hit("live@example.com", now=12.0) is False


def test_flooding_other_keys_does_not_reset_active_limit():
    """A flood with foreign keys does not reset the counter of the limited key."""
    rl = FixedWindowRateLimiter(max_hits=3, window_sec=15 * 60)
    for i in range(3):
        assert rl.hit("victim@example.com", now=float(i)) is True
    assert rl.hit("victim@example.com", now=3.0) is False

    for i in range(5000):
        rl.hit(f"junk-{i}@example.com", now=4.0 + i * 0.001)

    # the victim’s key is still limited and still in the dictionary
    assert rl.hit("victim@example.com", now=60.0) is False
    assert "victim@example.com" in rl._hits
