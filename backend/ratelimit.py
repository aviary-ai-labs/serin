"""A small fixed-window rate limiter for the endpoints worth guessing at.

Login and password reset are the two places where an attacker gets unlimited
free attempts against a secret, so they get a ceiling. Everything else in
Serin is already behind authentication.

Deliberately in-process and in-memory: Serin runs as one app, self-hosted or
shared, and a Redis dependency to slow down password guessing would cost more
than it buys. If the shared deployment ever runs several instances behind a
load balancer, each keeps its own counters — the effective limit multiplies by
the instance count, which is still a ceiling, just a looser one. Swap the
backend here when that day comes; nothing else needs to change.

Counters are keyed by whatever the caller passes (an IP, an email, or both),
so a single attacker can't burn through one victim's budget *and* a shared
one at the same time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Window:
    count: int = 0
    reset_at: float = 0.0


@dataclass
class RateLimiter:
    """Allow ``limit`` hits per ``window_seconds`` per key."""

    limit: int
    window_seconds: float
    _hits: dict[str, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key: str) -> bool:
        """True if this hit is allowed; False if the key is over its limit."""
        now = time.monotonic()
        with self._lock:
            window = self._hits.get(key)
            if window is None or now >= window.reset_at:
                self._hits[key] = _Window(count=1, reset_at=now + self.window_seconds)
                self._prune(now)
                return True
            if window.count >= self.limit:
                return False
            window.count += 1
            return True

    def retry_after(self, key: str) -> int:
        """Whole seconds until ``key`` gets its budget back."""
        with self._lock:
            window = self._hits.get(key)
            if not window:
                return 0
            return max(0, int(window.reset_at - time.monotonic()) + 1)

    def reset(self, key: str) -> None:
        """Clear a key — called after a *successful* login, so a legitimate
        user who fat-fingered their password a few times isn't left throttled."""
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        """Forget every key. For tests, and for an operator who needs to lift a
        throttle without restarting the app."""
        with self._lock:
            self._hits.clear()

    def _prune(self, now: float) -> None:
        """Drop expired windows so a long uptime under attack can't grow the
        dict without bound. Cheap, and only on window creation."""
        if len(self._hits) < 1024:
            return
        for key in [k for k, w in self._hits.items() if now >= w.reset_at]:
            self._hits.pop(key, None)


def client_ip(headers, fallback: str = "unknown") -> str:
    """Best-effort client address.

    Behind a proxy the socket address is the proxy, so prefer the first hop in
    ``x-forwarded-for``. That header is caller-controlled and trivially
    spoofed — which is fine here, because the limit is a speed bump against
    bulk guessing, not an authorization decision. Limits are always paired
    with a non-spoofable key (the account being targeted) so forging the
    header can't unlock anyone.
    """
    if headers is None:
        return fallback
    forwarded = headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip() or fallback
    return headers.get("x-real-ip") or fallback
