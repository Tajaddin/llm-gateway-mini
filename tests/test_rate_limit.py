"""TokenBucket unit tests."""

from __future__ import annotations

import time

import pytest

from llm_gateway import RateLimitExceeded, TokenBucket


def test_full_bucket_admits_n_immediate_acquires():
    b = TokenBucket(capacity=5, refill_per_second=1)
    for _ in range(5):
        assert b.acquire() is True


def test_empty_bucket_rejects():
    b = TokenBucket(capacity=2, refill_per_second=0.01)
    assert b.acquire(2) is True
    assert b.acquire() is False


def test_refill_after_wait():
    b = TokenBucket(capacity=1, refill_per_second=100)  # 10 ms per token
    assert b.acquire() is True
    assert b.acquire() is False
    time.sleep(0.05)  # ~5 tokens of refill, capped at 1
    assert b.acquire() is True


def test_acquire_or_raise_raises_when_empty():
    b = TokenBucket(capacity=1, refill_per_second=0.001)
    b.acquire()
    with pytest.raises(RateLimitExceeded):
        b.acquire_or_raise()


def test_seconds_until_available_returns_zero_when_full():
    b = TokenBucket(capacity=10, refill_per_second=1)
    assert b.seconds_until_available() == 0.0


def test_seconds_until_available_returns_positive_when_empty():
    b = TokenBucket(capacity=1, refill_per_second=1)
    b.acquire()
    s = b.seconds_until_available()
    assert 0.5 <= s <= 1.5


def test_rejects_bad_config():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, refill_per_second=1)
    with pytest.raises(ValueError):
        TokenBucket(capacity=1, refill_per_second=0)
