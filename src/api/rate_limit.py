"""In-memory rate limit for the public points of the cabinet (STAGE 2, security minimum).

No new dependencies: a fixed window in the process memory. Enough for one uvicorn worker of the ``api`` service. With several workers the limit is counted per worker — this is acceptable as a basic protection against e-mail bombing and timing enumeration on ``POST /login`` (the magic-link request), not as a strict quota.
"""

from __future__ import annotations

import threading
import time


class FixedWindowRateLimiter:
    """The counter of hits in a fixed window by key.

    No more than ``max_hits`` hits per ``window_sec`` seconds per key. The :meth:`hit` method returns ``True`` if the hit is within the limit (allowed), and ``False`` if the limit is exhausted. Stale marks of a key are dropped lazily when the key is accessed.

    The keys are set by input (the e-mail from the form), so one lazy cleanup “per access” is not enough: a key that is no longer accessed would remain in the dictionary forever. Once per ``window_sec`` the first access additionally sweeps out of the dictionary the keys whose marks have ALL left the window.

    Eviction of live keys is deliberately not done: any cap with eviction gives an attacker a way to reset their counter by flooding the limiter with other keys. Dropping a stale key is safe — it is not counted anyway.
    """

    def __init__(self, max_hits: int, window_sec: float) -> None:
        self._max = max_hits
        self._window = window_sec
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._next_sweep: float | None = None

    def _sweep_locked(self, moment: float, cutoff: float) -> None:
        """Drop the keys without live marks. Call under ``self._lock``."""
        if self._next_sweep is None:
            self._next_sweep = moment + self._window
            return
        if moment < self._next_sweep:
            return
        self._next_sweep = moment + self._window
        dead = [
            k for k, stamps in self._hits.items() if not any(t > cutoff for t in stamps)
        ]
        for k in dead:
            del self._hits[k]

    def hit(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        cutoff = moment - self._window
        with self._lock:
            self._sweep_locked(moment, cutoff)
            stamps = [t for t in self._hits.get(key, ()) if t > cutoff]
            if len(stamps) >= self._max:
                self._hits[key] = stamps
                return False
            stamps.append(moment)
            self._hits[key] = stamps
            return True

    def reset(self) -> None:
        """A full cleanup (for the tests)."""
        with self._lock:
            self._hits.clear()
            self._next_sweep = None
