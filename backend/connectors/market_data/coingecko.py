"""CoinGecko market-data connector — crypto specialist, layered routing.

Unlike Yahoo/FMP this never becomes the *main* price source: its manifest
declares ``asset_scope="crypto"``, so when enabled, crypto positions route
here while stocks/ETFs stay on the active generalist provider.
"""

from __future__ import annotations

from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    MarketDataConnector,
    TestResult,
)
from backend.connectors.registry import register
from backend.providers import coingecko as driver


@register
class CoinGeckoConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="coingecko",
        name="CoinGecko",
        kind="market_data",
        description=(
            "Crypto prices and daily history from CoinGecko — free, no key "
            "required. When enabled, crypto positions route here while stocks "
            "stay on your main provider. Optional demo API key raises rate "
            "limits; free history is capped at 365 days."
        ),
        icon="ti-currency-bitcoin",
        docs_url="https://www.coingecko.com/en/api",
        default_enabled=False,  # opt-in layer over the main provider
        asset_scope="crypto",
        config_schema=[
            ConfigField(
                key="api_key",
                label="Demo API key (optional)",
                type="password",
                secret=True,
                help="Free demo key from coingecko.com raises the rate limit. Leave blank for anonymous access.",
            ),
        ],
    )

    def _driver(self) -> driver.CoinGeckoProvider:
        return driver.CoinGeckoProvider(api_key=str(self.get("api_key", "") or ""))

    def refresh_prices(self, positions) -> dict:
        return self._driver().refresh_prices(positions)

    def fetch_history(self, period, symbols, positions_by_symbol) -> dict:
        return self._driver().fetch_history(period, symbols, positions_by_symbol)

    def quote(self, symbol, asset_type) -> dict | None:
        return self._driver().quote(symbol, asset_type)

    def test(self) -> TestResult:
        quote = self._driver().quote("BTC", "crypto")
        if quote and quote.get("price"):
            return TestResult(ok=True, message=f"Reached CoinGecko — BTC at ${quote['price']:,.0f}.")
        return TestResult(ok=False, message="Could not reach CoinGecko (rate limit or network).")
