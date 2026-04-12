"""Shared rate limiter for all DSO API calls.

Enforces two limits:
1. Max N concurrent requests (semaphore)
2. Max M requests per second (token bucket)
"""

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_concurrent: int = 50, max_per_second: float = 50.0):
        self._semaphore = threading.Semaphore(max_concurrent)
        self._max_per_second = max_per_second
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self):
        self._semaphore.acquire()
        with self._lock:
            now = time.monotonic()
            window_start = now - 1.0
            while self._timestamps and self._timestamps[0] < window_start:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max_per_second:
                sleep_until = self._timestamps[0] + 1.0
                sleep_time = sleep_until - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.monotonic()
                    window_start = now - 1.0
                    while self._timestamps and self._timestamps[0] < window_start:
                        self._timestamps.popleft()
            self._timestamps.append(time.monotonic())

    def release(self):
        self._semaphore.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


limiter = RateLimiter(max_concurrent=50, max_per_second=50.0)
