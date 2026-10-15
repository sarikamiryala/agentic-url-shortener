# covers: rate_limit
from shortener.ratelimit import RateLimiter


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_allows_up_to_limit_then_blocks():
    clock = FakeClock()
    rl = RateLimiter(3, 60.0, clock)
    assert [rl.check("a")[0] for _ in range(3)] == [True, True, True]
    allowed, retry = rl.check("a")
    assert allowed is False and retry >= 1


def test_window_slides():
    clock = FakeClock()
    rl = RateLimiter(2, 60.0, clock)
    rl.check("a")
    clock.now = 30
    rl.check("a")
    assert rl.check("a")[0] is False
    clock.now = 61  # first hit expired
    assert rl.check("a")[0] is True


def test_keys_are_independent():
    rl = RateLimiter(1, 60.0, FakeClock())
    assert rl.check("a")[0] is True
    assert rl.check("b")[0] is True
    assert rl.check("a")[0] is False


def test_retry_after_reflects_oldest_hit():
    clock = FakeClock()
    rl = RateLimiter(1, 60.0, clock)
    rl.check("a")
    clock.now = 20
    allowed, retry = rl.check("a")
    assert not allowed and retry == 40
