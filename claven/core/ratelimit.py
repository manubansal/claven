"""In-memory, per-key sliding-window rate limiting.

Each Cloud Run instance keeps its own windows, so the effective limit across
the service is the per-instance limit times the instance count. That is enough
to blunt a client hammering an endpoint; it is not a quota.
"""

import logging
import threading
import time
import weakref
from collections import deque

logger = logging.getLogger(__name__)

_limiters: "weakref.WeakSet[RateLimiter]" = weakref.WeakSet()


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()
        _limiters.add(self)

    def hit(self, key: str) -> float | None:
        """Record a request for key. Returns None if allowed, else seconds until one is."""
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            self._prune(cutoff)
            hits = self._hits.setdefault(key, deque())
            if len(hits) >= self.limit:
                retry_after = hits[0] + self.window - now
                logger.debug("RateLimiter.hit: refused %s (%d in window, retry in %.0fs)", key, len(hits), retry_after)
                return retry_after
            logger.debug("RateLimiter.hit: allowed %s (%d in window)", key, len(hits) + 1)
            hits.append(now)
            return None

    def _prune(self, cutoff: float) -> None:
        """Drop hits older than the window, and keys left with none."""
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                del self._hits[key]

    def tracked_keys(self) -> int:
        return len(self._hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def reset_all() -> None:
    for limiter in list(_limiters):
        limiter.reset()
