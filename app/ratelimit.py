"""In-memory sliding-window rate limiter.

State lives in this process, which is correct for the single-worker, single-container deployment
this app is built for. Running several workers would give each its own window.
"""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._hits: dict[int, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: int, limit: int) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the next slot frees up."""
        if limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= limit:
                return max(0.0, hits[0] + self.window_s - now)
            hits.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
