"""Entitlements — the neutral feature-flag seam for the open-core split.

The open-source core knows nothing about licenses: with no verifier
installed, every check returns False and the plan reads "opensource" —
which is a complete, uncrippled product (see docs/BUSINESS-MODEL.md).

A commercial pack (loaded via ``backend/plugins.py``) may install a verifier
with :func:`set_verifier`. The verifier returns the active plan and feature
set — typically from an offline-validated, signed license key.

Design constraints (deliberate):
- **No phoning home** from core. Verification strategy is the verifier's
  business; core only calls a local function.
- **Fail open-source**: a crashing or expired verifier degrades to OSS
  behavior — never an error state, never a lockout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Verifier contract: () -> {"plan": str, "features": list[str]}
_verifier: Callable[[], dict] | None = None

OPEN_SOURCE = {"plan": "opensource", "features": []}


def set_verifier(verifier: Callable[[], dict] | None) -> None:
    """Install (or clear) the entitlement verifier. Called by commercial
    packs at plugin-load time; tests may clear it with ``None``."""
    global _verifier
    _verifier = verifier


def summary() -> dict:
    """Current plan + features; always safe to call."""
    if _verifier is None:
        return dict(OPEN_SOURCE, features=[])
    try:
        result = _verifier() or {}
        plan = str(result.get("plan") or "opensource")
        features = [str(f) for f in (result.get("features") or [])]
        return {"plan": plan, "features": features}
    except Exception:
        logger.warning("Entitlement verifier failed; running as open source", exc_info=True)
        return dict(OPEN_SOURCE, features=[])


def has(feature: str) -> bool:
    return feature in summary()["features"]
