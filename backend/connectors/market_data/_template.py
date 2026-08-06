"""Starter template for a new market-data connector. Copy me!

This file is deliberately NOT registered (leading underscore + no import in
``backend/connectors/__init__.py``), so it never appears in the portal. It
compiles and is exercised by ``tests/test_connector_template.py`` so it can't
rot.

To ship a real connector:

1. Copy this file to ``backend/connectors/market_data/<yourprovider>.py``.
2. Rename the class, fill in the manifest (unique ``id``!), and implement the
   three methods below against your provider's API.
3. Register it: add ``from backend.connectors.market_data import <yourprovider>
   as _<yourprovider>  # noqa: F401`` to ``backend/connectors/__init__.py``.
4. Copy the test pattern from ``tests/test_connector_template.py`` and mock
   your HTTP calls (see ``tests/test_coingecko.py`` for a worked example).
5. Add a ``### <yourprovider> — <Name>`` section to ``docs/CONNECTORS.md`` so
   the in-app Docs button works.

That's the whole contribution: one module, one import line, one test file,
one docs section. Open a PR!
"""

from __future__ import annotations

from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    MarketDataConnector,
    TestResult,
)

# When you copy this file, add the decorator:
#
#   from backend.connectors.registry import register
#
#   @register
#   class MyProviderConnector(MarketDataConnector):
#       ...


class TemplateConnector(MarketDataConnector):
    """A fully-typed skeleton. Replace every TODO."""

    manifest = ConnectorManifest(
        id="_template",  # TODO unique lowercase id, e.g. "alphavantage"
        name="Template Provider",
        kind="market_data",
        description="TODO one sentence: what data this brings and what it costs.",
        icon="ti-plug",
        docs_url="https://example.com/api-docs",
        default_enabled=False,
        # "all" competes to be the main price source; "crypto" layers on top
        # of the main provider for crypto positions only (see CoinGecko).
        asset_scope="all",
        config_schema=[
            ConfigField(
                key="api_key",
                label="API key",
                type="password",
                secret=True,  # secret fields are never echoed back by the API
                help="TODO where to get one.",
            ),
        ],
    )

    # -- data interface ------------------------------------------------------
    # positions: list[backend.models.Position]; return shapes are exactly what
    # the orchestrator in backend/prices.py consumes.

    def refresh_prices(self, positions) -> dict:
        """Return {"prices": {symbol: (price, sector)}, "errors": [str, ...]}."""
        prices: dict[str, tuple[float, str]] = {}
        errors: list[str] = []
        for position in positions:
            # TODO call your API: price for position.symbol / position.asset_type
            errors.append(f"{position.symbol}: template connector has no data source")
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period: str, symbols, positions_by_symbol) -> dict:
        """Return {"history": {symbol: {"dates": [...], "closes": [...]}}, "errors": [...]}.

        Dates are ISO ``YYYY-MM-DD`` ascending; closes are floats. Fetched
        history is cached by Serin automatically, so a rate-limited provider
        only needs to succeed once a day.
        """
        return {"history": {}, "errors": [f"{s}: template connector has no data source" for s in symbols]}

    def quote(self, symbol: str, asset_type: str) -> dict | None:
        """Return a rich quote dict (see backend/providers/yahoo.py for all
        keys) or None when the symbol is unknown."""
        return None

    # -- health check --------------------------------------------------------

    def test(self) -> TestResult:
        """Called by the portal's Test button. Hit a cheap endpoint and report
        a human-readable result."""
        if not self.get("api_key"):
            return TestResult(ok=False, message="Add an API key first.")
        return TestResult(ok=True, message="TODO: verify a real request here.")
