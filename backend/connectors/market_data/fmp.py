"""Financial Modeling Prep market-data connector — richer data, paid key."""

from __future__ import annotations

from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    MarketDataConnector,
    TestResult,
)
from backend.connectors.registry import register
from backend.providers import fmp as driver


@register
class FMPConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="fmp",
        name="Financial Modeling Prep",
        kind="market_data",
        description="Stocks, ETFs and crypto with company fundamentals and sector data. Requires a free or paid FMP API key.",
        icon="ti-building-bank",
        docs_url="https://site.financialmodelingprep.com/developer/docs",
        config_schema=[
            ConfigField(
                key="api_key",
                label="FMP API key",
                type="password",
                required=True,
                secret=True,
                help="Get a key at financialmodelingprep.com. The free tier works for end-of-day data.",
                placeholder="your-fmp-key",
            ),
            ConfigField(
                key="base_url",
                label="Base URL",
                type="url",
                required=False,
                default="https://financialmodelingprep.com",
                help="Override only if you proxy FMP. Leave as-is otherwise.",
            ),
        ],
        default_enabled=False,
    )

    def _driver(self) -> driver.FMPProvider:
        return driver.FMPProvider(
            api_key=self.get("api_key") or None,
            base_url=self.get("base_url") or None,
        )

    def refresh_prices(self, positions) -> dict:
        return self._driver().refresh_prices(positions)

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        return self._driver().fetch_history(period, symbols, positions_by_symbol)

    def quote(self, symbol, asset_type) -> dict | None:
        return self._driver().quote(symbol, asset_type)

    def fetch_fundamentals(self, symbol, asset_type="stock") -> dict | None:
        return self._driver().fetch_fundamentals(symbol, asset_type)

    def test(self) -> TestResult:
        if not self.get("api_key"):
            return TestResult(ok=False, message="Add your FMP API key first.")
        q = self._driver().quote("AAPL", "stock")
        if q and q.get("price"):
            return TestResult(ok=True, message=f"FMP key works — AAPL at {q['price']:.2f}.")
        return TestResult(ok=False, message="FMP rejected the request — check the API key and plan.")
