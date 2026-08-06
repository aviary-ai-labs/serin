"""Binance holdings connector — read-only spot balances.

A read-only API key + secret (never trade/withdraw) signs a GET
``/api/v3/account`` request; the balances reconcile into positions tagged
``source='binance'``. Works with Binance.com (global) and Binance.US via a
base-URL select. Ongoing prices come from the crypto market-data layer
(CoinGecko) — the balance endpoint carries no USD valuation.

Auth is HMAC-SHA256 over the request query string (Binance's signed-endpoint
scheme), sent with the ``X-MBX-APIKEY`` header.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from backend import db
from backend.connectors.base import (
    ConfigField,
    ConnectorManifest,
    HoldingsConnector,
    TestResult,
)
from backend.connectors.registry import register
from backend.models import PositionIn

logger = logging.getLogger(__name__)

BROKER = "binance"
SOURCE = "binance"
_DEFAULT_BASE = "https://api.binance.com"
_TIMEOUT = 20.0


def _qty(balance: dict) -> float:
    try:
        return float(balance.get("free") or 0) + float(balance.get("locked") or 0)
    except (TypeError, ValueError):
        return 0.0


@register
class BinanceConnector(HoldingsConnector):
    supports_sync = True
    manifest = ConnectorManifest(
        id="binance",
        name="Binance",
        kind="holdings",
        description=(
            "Read-only sync of your Binance spot balances (Binance.com or "
            "Binance.US). Uses a read-only API key — Serin can never trade or withdraw."
        ),
        icon="ti-currency-bitcoin",
        docs_url="https://www.binance.com/en/my/settings/api-management",
        config_schema=[
            ConfigField(
                key="api_key",
                owner="user",
                label="API key",
                type="text",
                help="A read-only API key — enable 'Reading' only, never trading or withdrawals.",
            ),
            ConfigField(
                key="api_secret",
                owner="user",
                label="API secret",
                type="password",
                secret=True,
                help="Stored encrypted at rest; used only to sign read-only requests.",
            ),
            ConfigField(
                key="base_url",
                owner="user",
                label="Exchange",
                type="select",
                default=_DEFAULT_BASE,
                options=[
                    {"value": "https://api.binance.com", "label": "Binance.com (global)"},
                    {"value": "https://api.binance.us", "label": "Binance.US"},
                ],
                help="Choose Binance.US if your account lives on binance.us.",
            ),
        ],
        default_enabled=True,
        connect_method="api_key",  # advanced / self-host; Binance retail has no OAuth
    )

    # --- credentials --------------------------------------------------------
    def _creds(self) -> tuple[str, str]:
        return (self.get("api_key", "") or "").strip(), (self.get("api_secret", "") or "").strip()

    def _base(self) -> str:
        return (self.get("base_url", _DEFAULT_BASE) or _DEFAULT_BASE).rstrip("/")

    def configured(self) -> bool:
        key, secret = self._creds()
        return bool(key and secret)

    def _signed_get(self, path: str, params: dict | None = None) -> dict:
        """Signed GET against Binance's REST API. Raises on auth/non-2xx."""
        key, secret = self._creds()
        query = dict(params or {})
        query["timestamp"] = str(int(time.time() * 1000))
        query["recvWindow"] = "10000"
        qs = urllib.parse.urlencode(query)
        signature = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{self._base()}{path}?{qs}&signature={signature}"
        resp = httpx.get(url, headers={"X-MBX-APIKEY": key}, timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            raise PermissionError(
                "Binance rejected the API key — check the key/secret, that 'Enable Reading' "
                "is on, and any IP allowlist."
            )
        resp.raise_for_status()
        return resp.json()

    def _fetch_balances(self) -> list[dict]:
        payload = self._signed_get("/api/v3/account")
        return payload.get("balances") or []

    def _rows(self, balances: list[dict]) -> list[PositionIn]:
        rows: list[PositionIn] = []
        for bal in balances:
            asset = (bal.get("asset") or "").upper()
            qty = _qty(bal)
            if not asset or qty <= 0:
                continue
            rows.append(
                PositionIn(
                    symbol=asset,
                    name=asset,
                    broker=BROKER,
                    asset_type="crypto",
                    quantity=qty,
                    average_cost=0.0,  # /account carries no cost basis
                    current_price=0.0,  # priced by the crypto market-data layer
                    currency="USD",
                )
            )
        return rows

    def sync(self) -> dict:
        if not self.configured():
            raise RuntimeError("Binance not configured — add a read-only API key and secret first.")
        balances = self._fetch_balances()
        rows = self._rows(balances)
        result = db.replace_synced_positions(rows, {BROKER}, source=SOURCE)
        summary = {
            "assets": len(rows),
            "positions": result["upserted"],
            "removed": result["removed"],
            "new_symbols": result["new_symbols"],
        }
        logger.info(
            "Binance sync: %d assets, %d removed", summary["positions"], summary["removed"]
        )
        return summary

    def status(self) -> dict:
        return {"configured": self.configured(), "broker": BROKER, "base_url": self._base()}

    def test(self) -> TestResult:
        if not self.configured():
            return TestResult(ok=False, message="Add your Binance read-only API key and secret above.")
        try:
            balances = self._fetch_balances()
        except PermissionError as exc:
            return TestResult(ok=False, message=str(exc))
        except httpx.HTTPError as exc:
            return TestResult(ok=False, message=f"Binance request failed: {exc}")
        held = [b for b in balances if _qty(b) > 0]
        return TestResult(ok=True, message=f"Connected — {len(held)} asset(s) with a balance.")
