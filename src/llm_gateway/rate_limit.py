"""Per-tenant token-bucket rate limiter.

Each tenant has a bucket of capacity ``rpm`` tokens that refills at ``rps``
per second. ``acquire`` is non-blocking: it returns True if a token is
available, False otherwise. The caller decides what to do on False
(typically: return 429).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


class RateLimitExceeded(Exception):
    """Raised by :meth:`TokenBucket.acquire_or_raise`."""


@dataclass
class TokenBucket:
    capacity: float
    refill_per_second: float
    tokens: float = 0.0
    last_refill: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be > 0")
        if self.refill_per_second <= 0:
            raise ValueError("refill_per_second must be > 0")
        self.tokens = float(self.capacity)
        self.last_refill = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last_refill)
        if elapsed:
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.refill_per_second
            )
            self.last_refill = now

    def acquire(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self._refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def acquire_or_raise(self, n: float = 1.0) -> None:
        if not self.acquire(n):
            raise RateLimitExceeded(
                f"rate limit: bucket has {self.tokens:.2f} tokens, need {n}"
            )

    def seconds_until_available(self, n: float = 1.0) -> float:
        now = time.monotonic()
        self._refill(now)
        if self.tokens >= n:
            return 0.0
        return (n - self.tokens) / self.refill_per_second
