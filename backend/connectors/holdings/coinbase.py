"""Coinbase holdings connector — read-only crypto balances.

Uses a read-only Coinbase API key + secret (never trade/withdraw scopes) to
pull wallet balances from the retail v2 API and reconcile them into positions
tagged ``source='coinbase'``. Ongoing prices come from the market-data layer
(CoinGecko / Yahoo); the connector only seeds each position's USD value on
import so it shows a value immediately.

Auth is HMAC-SHA256 over ``timestamp + method + requestPath`` (the classic
Coinbase v2 scheme) — see ``docs/CONNECTORS.md`` for how to create a key.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

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

API_BASE = "https://api.coinbase.com"
API_VERSION = "2024-01-01"  # CB-VERSION pin
BROKER = "coinbase"
SOURCE = "coinbase"
_TIMEOUT = 20.0


def _has_crypto_balance(acct: dict) -> bool:
    balance = acct.get("balance") or {}
    currency = acct.get("currency") or {}
    ctype = currency.get("type") if isinstance(currency, dict) else ""
    try:
        return float(balance.get("amount") or 0) > 0 and ctype != "fiat"
    except (TypeError, ValueError):
        return False


@register
class CoinbaseConnector(HoldingsConnector):
    supports_sync = True
    manifest = ConnectorManifest(
        id="coinbase",
        name="Coinbase",
        kind="holdings",
        description=(
            "Read-only sync of your Coinbase crypto balances. Uses a read-only "
            "API key — Serin can never trade or withdraw."
        ),
        icon="ti-currency-bitcoin",
        docs_url="https://www.coinbase.com/settings/api",
        config_schema=[
            ConfigField(
                key="api_key",
                owner="user",
                label="API key",
                type="text",
                help="A read-only Coinbase API key (wallet:accounts:read). Create it at Settings → API.",
            ),
            ConfigField(
                key="api_secret",
                owner="user",
                label="API secret",
                type="password",
                secret=True,
                help="Stored encrypted at rest; used only to sign read-only requests to Coinbase.",
            ),
        ],
        default_enabled=True,
        connect_method="api_key",  # advanced / self-host; hosted prefers Coinbase OAuth
    )

    # --- credentials --------------------------------------------------------
    def _creds(self) -> tuple[str, str]:
        return (self.get("api_key", "") or "").strip(), (self.get("api_secret", "") or "").strip()

    def configured(self) -> bool:
        key, secret = self._creds()
        return bool(key and secret)

    def _signed_get(self, path: str) -> dict:
        """GET a Coinbase v2 ``path`` (incl. query) with HMAC auth. Raises on non-2xx."""
        key, secret = self._creds()
        timestamp = str(int(time.time()))
        message = f"{timestamp}GET{path}"  # GET body is empty
        signature = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        headers = {
            "CB-ACCESS-KEY": key,
            "CB-ACCESS-SIGN": signature,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "CB-VERSION": API_VERSION,
            "Accept": "application/json",
        }
        resp = httpx.get(f"{API_BASE}{path}", headers=headers, timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            raise PermissionError(
                "Coinbase rejected the API key — check the key/secret and that it has "
                "read access (wallet:accounts:read)."
            )
        resp.raise_for_status()
        return resp.json()

    # --- holdings -----------------------------------------------------------
    def _fetch_accounts(self) -> list[dict]:
        """Every account, following Coinbase's cursor pagination."""
        accounts: list[dict] = []
        path = "/v2/accounts?limit=100"
        pages = 0
        while path:
            payload = self._signed_get(path)
            accounts.extend(payload.get("data") or [])
            path = (payload.get("pagination") or {}).get("next_uri") or ""
            pages += 1
            if pages > 50:  # safety valve against a pathological cursor loop
                break
        return accounts

    def _rows(self, accounts: list[dict]) -> list[PositionIn]:
        rows: list[PositionIn] = []
        for acct in accounts:
            balance = acct.get("balance") or {}
            currency = acct.get("currency") or {}
            code = (currency.get("code") if isinstance(currency, dict) else currency) or ""
            ctype = currency.get("type") if isinstance(currency, dict) else ""
            try:
                qty = float(balance.get("amount") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            # Only crypto with a live balance; fiat wallets and dust are skipped.
            if qty <= 0 or ctype == "fiat" or not code:
                continue
            name = currency.get("name") if isinstance(currency, dict) else code
            # Seed a USD price from native_balance so the position shows value
            # immediately; the market-data layer refreshes it thereafter.
            price = 0.0
            native = acct.get("native_balance") or {}
            try:
                native_amt = float(native.get("amount") or 0)
                if qty > 0 and native_amt > 0:
                    price = native_amt / qty
            except (TypeError, ValueError):
                price = 0.0
            rows.append(
                PositionIn(
                    symbol=str(code).upper(),
                    name=name or code,
                    broker=BROKER,
                    asset_type="crypto",
                    quantity=qty,
                    average_cost=0.0,  # v2 balances don't expose cost basis
                    current_price=price,
                    currency="USD",
                )
            )
        return rows

    def sync(self) -> dict:
        if not self.configured():
            raise RuntimeError("Coinbase not configured — add a read-only API key and secret first.")
        accounts = self._fetch_accounts()
        rows = self._rows(accounts)
        result = db.replace_synced_positions(rows, {BROKER}, source=SOURCE)
        summary = {
            "accounts": len(accounts),
            "positions": result["upserted"],
            "removed": result["removed"],
            "new_symbols": result["new_symbols"],
        }
        logger.info(
            "Coinbase sync: %d accounts, %d positions, %d removed",
            len(accounts), summary["positions"], summary["removed"],
        )
        return summary

    def status(self) -> dict:
        return {"configured": self.configured(), "broker": BROKER}

    def test(self) -> TestResult:
        if not self.configured():
            return TestResult(ok=False, message="Add your Coinbase read-only API key and secret above.")
        try:
            accounts = self._fetch_accounts()
        except PermissionError as exc:
            return TestResult(ok=False, message=str(exc))
        except httpx.HTTPError as exc:
            return TestResult(ok=False, message=f"Coinbase request failed: {exc}")
        held = [a for a in accounts if _has_crypto_balance(a)]
        return TestResult(ok=True, message=f"Connected — {len(held)} crypto wallet(s) with a balance.")
