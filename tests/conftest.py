from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_module_level_caches():
    """Reset the process-wide market-data caches between tests.

    Both are correct as global state in production — whether a provider can
    serve a symbol, and when a split happened, are facts about the market, not
    about a user. In a test suite they leak: one test marking a symbol
    unfetchable makes a later test silently skip the provider call it was
    asserting on, and the failure surfaces as an unrelated test breaking only
    when the whole suite runs.
    """
    from backend import prices

    prices.forget_unfetchable()
    prices._split_cache.clear()
    yield
    prices.forget_unfetchable()
    prices._split_cache.clear()


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """Reset the app_settings cache between tests.

    In production it is invalidated by every write and bounded by a TTL, which
    is enough because the database underneath it never changes. A test suite
    swaps databases constantly, so one test's reads — including its *misses* —
    would otherwise answer the next test's questions about a different file.
    """
    from backend import db

    db.forget_settings_cache()
    yield
    db.forget_settings_cache()


@pytest.fixture(autouse=True)
def _isolate_connector_config_cache():
    """Connector config is memoised per deployment. Between tests that would
    carry one test's provider choice into the next."""
    from backend.connectors import registry

    registry.forget_instance_cache()
    yield
    registry.forget_instance_cache()
