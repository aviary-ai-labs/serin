"""Serin connector platform.

Importing this package registers all in-tree connectors. Two distribution
paths (the 2026-07-02 open-core decision supersedes the earlier in-tree-only
policy):

- **In-tree (preferred for shared connectors):** create a module under the
  matching ``<kind>/`` folder, decorate the class with ``@register``, import
  it here, and open a PR.
- **Out-of-tree:** drop a module using the same SDK into
  ``SERIN_PLUGINS_DIR`` (see ``backend/plugins.py``) — private connectors and
  the commercial pack load this way without forking core.
"""

from __future__ import annotations

from backend.connectors import registry
from backend.connectors.base import (  # noqa: F401  (public SDK surface)
    ConfigField,
    Connector,
    ConnectorManifest,
    HoldingsConnector,
    InsightConnector,
    MarketDataConnector,
    TestResult,
)
from backend.connectors.holdings import binance as _binance  # noqa: F401
from backend.connectors.holdings import coinbase as _coinbase  # noqa: F401
from backend.connectors.holdings import generic_csv as _generic_csv  # noqa: F401
from backend.connectors.holdings import snaptrade as _snaptrade  # noqa: F401
from backend.connectors.insight import briefing as _briefing  # noqa: F401
from backend.connectors.market_data import alphavantage as _alphavantage  # noqa: F401
from backend.connectors.market_data import cboe as _cboe  # noqa: F401
from backend.connectors.market_data import coingecko as _coingecko  # noqa: F401
from backend.connectors.market_data import fmp as _fmp  # noqa: F401
from backend.connectors.market_data import stooq as _stooq  # noqa: F401

# Import side-effect: each module calls @register on import.
from backend.connectors.market_data import yahoo as _yahoo  # noqa: F401


def active_market_data_id() -> str:
    """Which market-data connector should serve price requests.

    If the user has configured market data in the portal (any enable/config
    setting present), that wins. Otherwise fall back to settings/env resolution
    so a fresh ``.env``-only install (and the existing test suite) is unchanged.

    Specialized-scope connectors (e.g. CoinGecko, crypto-only) never compete
    for the main-provider role — they layer on top via ``active_crypto_data``.
    """
    from backend.config import settings

    md = [m for m in registry.manifests_by_kind("market_data") if m.asset_scope == "all"]
    touched = any(registry.has_setting(m.id) for m in md)
    if not touched:
        return settings.resolved_market_data_provider  # "fmp" | "yahoo" | "none"

    enabled = [m.id for m in md if registry.is_enabled(m.id)]
    # Prefer FMP (richer) when enabled and it has a usable key, else Yahoo.
    if "fmp" in enabled and (registry.get_config("fmp").get("api_key") or settings.fmp_api_key):
        return "fmp"
    if "yahoo" in enabled:
        return "yahoo"
    return enabled[0] if enabled else "none"


def active_market_data():
    """Instantiate the active market-data connector (or None)."""
    connector_id = active_market_data_id()
    if connector_id == "none":
        return None
    return registry.instantiate(connector_id)


def active_crypto_data():
    """The crypto-specialist layer, if any is enabled (else None).

    When this returns a connector, ``backend.prices`` routes crypto positions
    through it and everything else through :func:`active_market_data`.
    """
    for manifest in registry.manifests_by_kind("market_data"):
        if manifest.asset_scope == "crypto" and registry.is_enabled(manifest.id):
            return registry.instantiate(manifest.id)
    return None


def market_data_chain() -> list[tuple[str, object]]:
    """Ordered scope='all' providers to try for prices/history: the active one
    first, then any other *enabled* 'all' connectors, then Yahoo as a keyless
    last-resort backstop. Lets a paywalled/rate-limited provider (e.g. FMP 402,
    Yahoo 429) fall through to the next instead of leaving an empty chart.
    """
    ordered: list[str] = []
    active = active_market_data_id()
    if active and active != "none":
        ordered.append(active)
    for manifest in registry.manifests_by_kind("market_data"):
        if (
            manifest.asset_scope == "all"
            and registry.is_enabled(manifest.id)
            and manifest.id not in ordered
        ):
            ordered.append(manifest.id)
    # Keyless backstop — mirrors the prior hardcoded Yahoo fallback.
    if "yahoo" not in ordered and registry.has("yahoo"):
        ordered.append("yahoo")

    chain: list[tuple[str, object]] = []
    for connector_id in ordered:
        connector = registry.instantiate(connector_id)
        if connector is not None:
            chain.append((connector_id, connector))
    return chain


__all__ = [
    "registry",
    "ConfigField",
    "Connector",
    "ConnectorManifest",
    "HoldingsConnector",
    "InsightConnector",
    "MarketDataConnector",
    "TestResult",
    "active_market_data",
    "active_market_data_id",
    "active_crypto_data",
    "market_data_chain",
]
