"""The ceiling on password guessing."""

from __future__ import annotations

from backend import ratelimit


def test_allows_up_to_the_limit_then_refuses():
    limiter = ratelimit.RateLimiter(limit=3, window_seconds=60)
    assert [limiter.check("k") for _ in range(5)] == [True, True, True, False, False]


def test_keys_have_separate_budgets():
    """One noisy caller must not lock everyone else out."""
    limiter = ratelimit.RateLimiter(limit=1, window_seconds=60)
    assert limiter.check("a") is True
    assert limiter.check("a") is False
    assert limiter.check("b") is True


def test_success_clears_the_key():
    """A legitimate user who mistypes twice then gets it right shouldn't be
    left throttled for the rest of the window."""
    limiter = ratelimit.RateLimiter(limit=3, window_seconds=60)
    limiter.check("k")
    limiter.check("k")
    limiter.reset("k")
    assert [limiter.check("k") for _ in range(3)] == [True, True, True]


def test_budget_returns_after_the_window():
    limiter = ratelimit.RateLimiter(limit=1, window_seconds=0.05)
    assert limiter.check("k") is True
    assert limiter.check("k") is False
    import time
    time.sleep(0.06)
    assert limiter.check("k") is True


def test_retry_after_is_reported():
    limiter = ratelimit.RateLimiter(limit=1, window_seconds=60)
    limiter.check("k")
    assert 0 < limiter.retry_after("k") <= 61


def test_client_ip_prefers_the_forwarded_hop():
    assert ratelimit.client_ip({"x-forwarded-for": "203.0.113.7, 10.0.0.1"}) == "203.0.113.7"
    assert ratelimit.client_ip({"x-real-ip": "203.0.113.9"}) == "203.0.113.9"
    assert ratelimit.client_ip({}) == "unknown"
