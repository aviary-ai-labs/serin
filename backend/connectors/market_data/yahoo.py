"""Yahoo Finance market-data connector — free, no API key."""

from __future__ import annotations

from backend.connectors.base import ConnectorManifest, MarketDataConnector, TestResult
from backend.connectors.registry import register
from backend.providers import yahoo as driver


@register
class YahooConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="yahoo",
        name="Yahoo Finance",
        kind="market_data",
        description="Free stock, ETF and crypto quotes and history. No API key required — the default market-data source.",
        icon="ti-chart-line",
        docs_url="https://finance.yahoo.com",
        config_schema=[],  # nothing to configure
        default_enabled=True,
    )

    def _driver(self) -> driver.YahooProvider:
        return driver.YahooProvider()

    def refresh_prices(self, positions) -> dict:
        return self._driver().refresh_prices(positions)

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        return self._driver().fetch_history(period, symbols, positions_by_symbol)

    def quote(self, symbol, asset_type) -> dict | None:
        return self._driver().quote(symbol, asset_type)

    def fetch_fundamentals(self, symbol, asset_type="stock") -> dict | None:
        return self._driver().fetch_fundamentals(symbol, asset_type)

    def test(self) -> TestResult:
        q = self._driver().quote("AAPL", "stock")
        if q and q.get("price"):
            return TestResult(ok=True, message=f"Reached Yahoo — AAPL at {q['price']:.2f}.")
        return TestResult(ok=False, message="Could not reach Yahoo Finance.")
