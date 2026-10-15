"""In-memory sliding-window rate limiter (single process)."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Callable


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry = max(1, math.ceil(hits[0] + self.window - now))
                return False, retry
            hits.append(now)
            if len(self._hits) > 10_000:
                self._evict(cutoff)
            return True, 0

    def _evict(self, cutoff: float) -> None:
        for k in [k for k, v in self._hits.items() if not v or v[-1] <= cutoff]:
            del self._hits[k]
