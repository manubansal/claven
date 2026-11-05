"""Per-key sliding-window rate limiter (claven/core/ratelimit.py)."""

from claven.core.ratelimit import RateLimiter


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_allows_up_to_the_limit_then_refuses():
    limiter = RateLimiter(limit=3, window_seconds=60, clock=_Clock())
    assert [limiter.hit("a") for _ in range(3)] == [None, None, None]
    assert limiter.hit("a") is not None


def test_retry_after_is_when_the_oldest_hit_leaves_the_window():
    clock = _Clock()
    limiter = RateLimiter(limit=2, window_seconds=60, clock=clock)
    limiter.hit("a")
    clock.now += 10
    limiter.hit("a")
    assert limiter.hit("a") == 50


def test_hits_expire_after_the_window():
    clock = _Clock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.hit("a")
    clock.now += 60
    assert limiter.hit("a") is None


def test_refused_hits_do_not_extend_the_wait():
    clock = _Clock()
    limiter = RateLimiter(limit=1, window_seconds=60, clock=clock)
    limiter.hit("a")
    for _ in range(5):
        limiter.hit("a")
    clock.now += 60
    assert limiter.hit("a") is None


def test_keys_are_independent():
    limiter = RateLimiter(limit=1, window_seconds=60, clock=_Clock())
    limiter.hit("a")
    assert limiter.hit("b") is None


def test_idle_keys_are_dropped():
    """Memory stays bounded: keys with no hits left in the window are forgotten."""
    clock = _Clock()
    limiter = RateLimiter(limit=5, window_seconds=60, clock=clock)
    for i in range(100):
        limiter.hit(f"k{i}")
    clock.now += 61
    limiter.hit("fresh")
    assert limiter.tracked_keys() == 1


def test_reset_all_clears_every_limiter():
    from claven.core import ratelimit
    limiter = RateLimiter(limit=1, window_seconds=60, clock=_Clock())
    limiter.hit("a")
    ratelimit.reset_all()
    assert limiter.hit("a") is None
