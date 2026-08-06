"""Generic CSV holdings connector — the no-code, fully-customizable import.

This is the showcase for "configure how you import data" without writing a
connector: the user maps their broker's CSV columns to Serin fields in the
portal, and the import endpoint applies that saved mapping. Works with *any*
broker export, not just the formats Serin ships parsers for.
"""

from __future__ import annotations

from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    HoldingsConnector,
    TestResult,
)
from backend.connectors.registry import register


@register
class GenericCsvConnector(HoldingsConnector):
    supports_sync = False  # import is a one-shot upload, not a pull
    manifest = ConnectorManifest(
        id="generic_csv",
        name="Generic CSV import",
        kind="holdings",
        description="Import positions from ANY broker's CSV by mapping its columns to Serin fields. No code, no fixed format.",
        icon="ti-file-spreadsheet",
        config_schema=[
            ConfigField(key="symbol_col", owner="user", label="Symbol column", required=True, default="symbol",
                        help="The CSV column header (or 0-based index) holding the ticker."),
            ConfigField(key="quantity_col", owner="user", label="Quantity column", required=True, default="quantity"),
            ConfigField(key="cost_col", owner="user", label="Average-cost column", default="average_cost"),
            ConfigField(key="price_col", owner="user", label="Current-price column", default="current_price"),
            ConfigField(key="name_col", owner="user", label="Name column", default="name"),
            ConfigField(
                key="asset_type",
                owner="user",
                label="Asset type",
                type="select",
                default="stock",
                options=[
                    {"value": "stock", "label": "Stock"},
                    {"value": "etf", "label": "ETF"},
                    {"value": "crypto", "label": "Crypto"},
                    {"value": "cash", "label": "Cash"},
                ],
                help="Applied to every row unless an asset-type column is present.",
            ),
            ConfigField(key="default_broker", owner="user", label="Broker label", default="csv",
                        help="Tags imported rows so you can filter by source."),
        ],
        default_enabled=True,
        connect_method="file",  # hand over a statement, no standing access
    )

    def mapping(self) -> dict:
        """The column map the import endpoint applies. Used by /api/import/csv."""
        return {
            "symbol_col": self.get("symbol_col", "symbol"),
            "quantity_col": self.get("quantity_col", "quantity"),
            "cost_col": self.get("cost_col", "average_cost"),
            "price_col": self.get("price_col", "current_price"),
            "name_col": self.get("name_col", "name"),
            "asset_type": self.get("asset_type", "stock"),
            "default_broker": self.get("default_broker", "csv"),
        }

    def test(self) -> TestResult:
        if not self.get("symbol_col") or not self.get("quantity_col"):
            return TestResult(ok=False, message="Map at least the symbol and quantity columns.")
        return TestResult(
            ok=True,
            message="Mapping saved. Upload a CSV from the Overview tab to import with this mapping.",
        )
