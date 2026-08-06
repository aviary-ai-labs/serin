"""SnapTrade read-only brokerage sync.

The current local app still has exactly one SnapTrade end-user. We register it
lazily on first connect and store {userId, userSecret} in app_settings.
Connections and accounts live on SnapTrade's side and are queried live; synced
holdings are written into the positions table tagged source='snaptrade' and
reconciled on each sync.

For a hosted multi-user Serin, this module needs to become user-scoped:
one SnapTrade user per Serin account, encrypted userSecret storage, and no
global app_settings credential.

All SDK calls are synchronous (urllib3 under the hood); callers in the API
layer wrap them in asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from backend import db
from backend.config import settings
from backend.models import PositionIn

logger = logging.getLogger(__name__)

USER_SETTING_KEY = "snaptrade_user"
LAST_SYNC_KEY = "snaptrade_last_sync"
OCC_OPTION_RE = re.compile(r"^([A-Z0-9]{1,6})\s*(\d{6})([CP])(\d{8})$")

_client = None
_client_creds: tuple[str, str] | None = None


class SnapTradeError(RuntimeError):
    pass


def resolved_credentials() -> tuple[str, str]:
    """(client_id, consumer_key) — connector portal config first, env fallback.

    Mirrors backend.ai_provider: credentials saved in the portal UI must work
    without touching .env.
    """
    try:
        from backend.connectors import registry as connector_registry

        cfg = connector_registry.get_config("snaptrade") or {}
    except Exception:
        cfg = {}
    client_id = (cfg.get("client_id") or "").strip() or settings.snaptrade_client_id.strip()
    consumer_key = (cfg.get("consumer_key") or "").strip() or settings.snaptrade_consumer_key.strip()
    return client_id, consumer_key


def snaptrade_available() -> bool:
    client_id, consumer_key = resolved_credentials()
    return bool(client_id and consumer_key)


def _get_client():
    global _client, _client_creds
    client_id, consumer_key = resolved_credentials()
    if not client_id or not consumer_key:
        raise SnapTradeError(
            "SnapTrade is not configured. Add the Client ID and Consumer Key "
            "in the SnapTrade connector (Connectors tab), or set "
            "SNAPTRADE_CLIENT_ID and SNAPTRADE_CONSUMER_KEY in .env."
        )
    creds = (client_id, consumer_key)
    if _client is not None and _client_creds is None:
        # Externally injected client (tests) — use as-is.
        return _client
    if _client is None or _client_creds != creds:
        from snaptrade_client import SnapTrade

        _client = SnapTrade(consumer_key=consumer_key, client_id=client_id)
        _client_creds = creds
    return _client


def _g(obj: Any, *keys: str, default: Any = None) -> Any:
    """Walk nested SnapTrade response objects (dict-like or attr) safely."""
    cur = obj
    for key in keys:
        if cur is None:
            return default
        try:
            cur = cur[key]
        except (KeyError, TypeError, IndexError):
            cur = getattr(cur, key, None)
    return default if cur is None else cur


def _slug_broker(name: str) -> str:
    """E*TRADE -> etrade, Robinhood -> robinhood (matches existing broker tags)."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower()) or "brokerage"


def _asset_type(code: str | None) -> str:
    code = (code or "").lower()
    if code in {"et", "etf", "oef", "cef", "mf"}:
        return "etf"
    if "crypto" in code or code in {"cc", "cr"}:
        return "crypto"
    if code in {"oe", "do", "opt", "option"}:
        return "option"
    return "stock"


def _format_symbol(symbol: str, asset_type: str) -> str:
    symbol = symbol.strip().upper()
    if asset_type != "option":
        return symbol
    match = OCC_OPTION_RE.match(symbol)
    if not match:
        return symbol
    root, expiry, call_put, strike_raw = match.groups()
    strike = int(strike_raw) / 1000
    strike_text = f"{strike:g}"
    return f"{root}-{expiry}-{call_put}{strike_text}"


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _unit_price(value: Any, asset_type: str) -> float:
    price = _float(value)
    # SnapTrade's live Robinhood option rows return price/cost per contract.
    # Serin stores option prices per share and applies the 100x multiplier.
    if asset_type == "option":
        return price / 100
    return price


def _response_rows(body: Any) -> list[Any]:
    if body is None:
        return []
    if isinstance(body, dict) and "results" in body:
        results = body.get("results") or []
        return results if isinstance(results, list) else [results]
    if isinstance(body, list):
        return body
    try:
        return list(body)
    except TypeError:
        return [body]


# ---------------------------------------------------------------------------
# User registration (single local end-user)


def get_stored_user() -> dict | None:
    raw = db.get_setting(USER_SETTING_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if data.get("userId") and data.get("userSecret") else None


def get_or_register_user() -> dict:
    existing = get_stored_user()
    if existing:
        return existing
    # Personal-tier keys: SnapTrade pre-provisions one user at signup and
    # blocks registerUser. Use the userId/userSecret from the dashboard.
    if settings.snaptrade_user_id and settings.snaptrade_user_secret:
        user = {"userId": settings.snaptrade_user_id, "userSecret": settings.snaptrade_user_secret}
        db.set_setting(USER_SETTING_KEY, json.dumps(user))
        logger.info("Using pre-provisioned SnapTrade user %s", user["userId"])
        return user
    # Standard-tier keys: register a fresh end-user via the API.
    client = _get_client()
    user_id = f"serin-{uuid.uuid4().hex[:16]}"
    response = client.authentication.register_snap_trade_user(user_id=user_id)
    user = {"userId": _g(response.body, "userId"), "userSecret": _g(response.body, "userSecret")}
    if not user["userId"] or not user["userSecret"]:
        raise SnapTradeError("SnapTrade registration did not return a user secret.")
    db.set_setting(USER_SETTING_KEY, json.dumps(user))
    logger.info("Registered SnapTrade user %s", user["userId"])
    return user


def connection_portal_url(custom_redirect: str | None = None) -> str:
    """Return a one-time Connection Portal URL for a read-only broker link."""
    user = get_or_register_user()
    client = _get_client()
    kwargs = {
        "user_id": user["userId"],
        "user_secret": user["userSecret"],
        "connection_type": "read",  # read-only: no trading scope is ever granted
    }
    if custom_redirect:
        kwargs["custom_redirect"] = custom_redirect
    response = client.authentication.login_snap_trade_user(**kwargs)
    url = _g(response.body, "redirectURI")
    if not url:
        raise SnapTradeError("SnapTrade did not return a connection URL.")
    return url


# ---------------------------------------------------------------------------
# Reads


def list_connections() -> list[dict]:
    user = get_stored_user()
    if not user:
        return []
    client = _get_client()
    response = client.connections.list_brokerage_authorizations(
        user_id=user["userId"], user_secret=user["userSecret"]
    )
    connections = []
    for auth in response.body or []:
        connections.append(
            {
                "id": _g(auth, "id"),
                "institution": _g(auth, "brokerage", "name", default="Brokerage"),
                "disabled": bool(_g(auth, "disabled", default=False)),
                "created_at": _g(auth, "created_date"),
            }
        )
    return connections


def list_accounts() -> list[dict]:
    user = get_stored_user()
    if not user:
        return []
    client = _get_client()
    response = client.account_information.list_user_accounts(
        user_id=user["userId"], user_secret=user["userSecret"]
    )
    accounts = []
    for acc in response.body or []:
        accounts.append(
            {
                "id": _g(acc, "id"),
                "name": _g(acc, "name", default="Account"),
                "number": _g(acc, "number"),
                "institution": _g(acc, "institution_name", default="Brokerage"),
                "total_value": _float(_g(acc, "balance", "total", "amount", default=0)),
            }
        )
    return accounts


def _account_rows(account: dict) -> tuple[list[PositionIn], str]:
    """Fetch positions + cash for one account -> PositionIn rows + broker slug."""
    user = get_stored_user()
    client = _get_client()
    broker = _slug_broker(account["institution"])
    rows: list[PositionIn] = []

    positions = client.account_information.get_all_account_positions(
        user_id=user["userId"], user_secret=user["userSecret"], account_id=account["id"]
    )
    for pos in _response_rows(positions.body):
        ticker = _g(pos, "symbol", "symbol", "symbol") or _g(pos, "instrument", "symbol")
        if not ticker:
            continue
        type_code = _g(pos, "symbol", "symbol", "type", "code") or _g(pos, "instrument", "kind")
        description = (
            _g(pos, "symbol", "symbol", "description", default="")
            or _g(pos, "instrument", "description", default="")
        )
        average_cost = _g(pos, "average_purchase_price")
        if average_cost is None:
            average_cost = _g(pos, "cost_basis", default=0)
        asset_type = _asset_type(type_code)
        rows.append(
            PositionIn(
                symbol=_format_symbol(str(ticker), asset_type),
                name=str(description or ticker),
                broker=broker,
                asset_type=asset_type,
                quantity=_float(_g(pos, "units", default=_g(pos, "fractional_units", default=0))),
                average_cost=_unit_price(average_cost, asset_type),
                current_price=_unit_price(_g(pos, "price", default=0), asset_type),
            )
        )

    balances = client.account_information.get_user_account_balance(
        user_id=user["userId"], user_secret=user["userSecret"], account_id=account["id"]
    )
    cash_total = sum(_float(_g(bal, "cash", default=0)) for bal in _response_rows(balances.body))
    if cash_total:
        rows.append(
            PositionIn(
                symbol="CASH",
                name="Cash",
                broker=broker,
                asset_type="cash",
                quantity=cash_total,
                average_cost=1.0,
                current_price=1.0,
            )
        )
    return rows, broker


def _aggregate_rows(rows: list[PositionIn]) -> list[PositionIn]:
    """Combine holdings of the same symbol across accounts at one broker.

    A broker can expose several accounts (individual, IRA, …), each with its
    own cash and possibly the same ticker. They share the positions table's
    UNIQUE(symbol, broker, asset_type) key, so we sum quantities (and cash)
    and quantity-weight the average cost before writing — otherwise one
    account's value would silently overwrite another's.
    """
    merged: dict[tuple[str, str, str], PositionIn] = {}
    for row in rows:
        key = (row.symbol, row.broker, row.asset_type)
        existing = merged.get(key)
        if existing is None:
            merged[key] = row.model_copy()
            continue
        total_qty = existing.quantity + row.quantity
        if total_qty:
            existing.average_cost = (
                existing.average_cost * existing.quantity + row.average_cost * row.quantity
            ) / total_qty
        existing.quantity = total_qty
        existing.current_price = row.current_price or existing.current_price
        existing.name = existing.name or row.name
    return list(merged.values())


def sync(refresh_prices_after: bool = True) -> dict:
    """Pull holdings + cash from every connected account into positions.

    SnapTrade is the source of truth for *what you hold* (quantity, cost
    basis, cash). Prices and sectors are then refreshed via the configured
    market-data provider, so the two sources never fight: SnapTrade only seeds
    a price for brand-new holdings, market data owns it from then on.
    """
    user = get_stored_user()
    if not user:
        raise SnapTradeError("No SnapTrade connection yet. Connect a brokerage first.")

    accounts = list_accounts()
    all_rows: list[PositionIn] = []
    brokers: set[str] = set()
    for account in accounts:
        rows, broker = _account_rows(account)
        all_rows.extend(rows)
        brokers.add(broker)

    all_rows = _aggregate_rows(all_rows)
    result = db.replace_synced_positions(all_rows, brokers)

    # Only price brand-new holdings here (to fill their sector + a fresh
    # quote). Existing positions stay on their market-data price; a blanket
    # refresh on every sync would needlessly hammer the price API.
    repriced = 0
    new_symbols = set(result.get("new_symbols") or [])
    if refresh_prices_after and new_symbols:
        try:
            from backend.prices import refresh_prices

            repriced = refresh_prices(new_symbols).get("updated", 0)
        except Exception:
            logger.warning("Post-sync price refresh failed; holdings still synced", exc_info=True)

    summary = {
        "at": datetime.now(UTC).isoformat(),
        "accounts": len(accounts),
        "positions": result["upserted"],
        "removed": result["removed"],
        "repriced": repriced,
        "error": "",
    }
    db.set_setting(LAST_SYNC_KEY, json.dumps(summary))
    logger.info(
        "SnapTrade sync: %d accounts, %d positions, %d removed, %d repriced",
        summary["accounts"], summary["positions"], summary["removed"], repriced,
    )
    return summary


# SnapTrade activity type -> Serin transaction action.
_ACTIVITY_ACTIONS = {
    "BUY": "buy",
    "SELL": "sell",
    "DIVIDEND": "dividend",
    "CONTRIBUTION": "cash_in",
    "WITHDRAWAL": "cash_out",
    "FEE": "fee",
    "INTEREST": "interest",
    "REI": "buy",  # dividend reinvestment lands as a buy
    "TRANSFER": "transfer",
}

BACKFILL_REF_PREFIX = "snaptrade:"


def backfill_transactions(days: int = 365) -> dict:
    """Pull broker activity history into the transactions table.

    Idempotent: every imported row is tagged ``snaptrade:<activity_id>`` in its
    notes, and already-imported ids are skipped — re-running only picks up new
    activity. Unknown activity types are counted and reported, never guessed.
    """
    from backend.models import TransactionIn

    user = get_stored_user()
    if not user:
        raise SnapTradeError("No SnapTrade connection yet. Connect a brokerage first.")
    client = _get_client()

    end = datetime.now(UTC).date()
    start = end - timedelta(days=max(1, days))
    response = client.transactions_and_reporting.get_activities(
        user_id=user["userId"],
        user_secret=user["userSecret"],
        start_date=start.isoformat(),
        end_date=end.isoformat(),
    )
    activities = _response_rows(response.body)

    existing_refs = {
        t.notes for t in db.list_transactions(limit=100_000)
        if t.notes.startswith(BACKFILL_REF_PREFIX)
    }

    imported = 0
    skipped_existing = 0
    skipped_unknown = 0
    for activity in activities:
        activity_id = str(_g(activity, "id", default="") or "")
        ref = f"{BACKFILL_REF_PREFIX}{activity_id}"
        if not activity_id or ref in existing_refs:
            skipped_existing += 1
            continue
        raw_type = str(_g(activity, "type", default="") or "").upper()
        action = _ACTIVITY_ACTIONS.get(raw_type)
        if action is None:
            skipped_unknown += 1
            continue

        symbol = str(_g(activity, "symbol", "symbol", default="") or _g(activity, "symbol", "raw_symbol", default="") or "")
        units = abs(_float(_g(activity, "units", default=0)))
        price = _float(_g(activity, "price", default=0))
        amount = abs(_float(_g(activity, "amount", default=0)))
        fee = abs(_float(_g(activity, "fee", default=0)))
        occurred = str(_g(activity, "trade_date", default="") or _g(activity, "settlement_date", default="") or end.isoformat())[:10]
        broker = _slug_broker(str(_g(activity, "institution", default="") or "brokerage"))
        currency = str(_g(activity, "currency", "code", default="USD") or "USD")

        if action in ("dividend", "interest", "cash_in", "cash_out", "fee"):
            # Convention: the price field carries the cash amount for
            # non-share transactions (see db._derive_amount).
            price = amount or price

        try:
            db.create_transaction(
                TransactionIn(
                    symbol=symbol,
                    broker=broker,
                    action=action,
                    quantity=units,
                    price=price,
                    fee=fee,
                    currency=currency[:3] if len(currency) >= 3 else "USD",
                    occurred_at=occurred,
                    notes=ref,
                ),
                source="snaptrade",
            )
            existing_refs.add(ref)
            imported += 1
        except Exception:
            logger.warning("Skipping unmappable SnapTrade activity %s", activity_id, exc_info=True)
            skipped_unknown += 1

    return {
        "imported": imported,
        "skipped_existing": skipped_existing,
        "skipped_unknown": skipped_unknown,
        "window_days": days,
        "from": start.isoformat(),
        "to": end.isoformat(),
    }


def disconnect(authorization_id: str) -> int:
    """Remove one brokerage connection and its synced holdings."""
    user = get_stored_user()
    if not user:
        return 0
    client = _get_client()
    # Capture the broker slug before removing, so we can clean up its rows.
    brokers = {_slug_broker(c["institution"]) for c in list_connections() if c["id"] == authorization_id}
    client.connections.remove_brokerage_authorization(
        authorization_id=authorization_id, user_id=user["userId"], user_secret=user["userSecret"]
    )
    removed = db.delete_positions_for_brokers(brokers) if brokers else 0
    logger.info("Disconnected SnapTrade auth %s (%d positions removed)", authorization_id, removed)
    return removed


def get_last_sync() -> dict | None:
    raw = db.get_setting(LAST_SYNC_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def status() -> dict:
    """Lightweight status for the UI (no network calls unless registered)."""
    if not snaptrade_available():
        return {"configured": False, "registered": False, "connections": [], "last_sync": None}
    registered = get_stored_user() is not None
    connections: list[dict] = []
    if registered:
        try:
            connections = list_connections()
        except Exception as exc:  # surface as empty list; UI shows a sync error separately
            logger.warning("Could not list SnapTrade connections: %s", exc)
    return {
        "configured": True,
        "registered": registered,
        "connections": connections,
        "last_sync": get_last_sync(),
    }
