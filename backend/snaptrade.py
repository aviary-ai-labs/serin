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
from datetime import UTC, date, datetime, timedelta
from typing import Any

from backend import db, scope, secrets_store
from backend.config import settings
from backend.models import PositionIn, canonical_action

logger = logging.getLogger(__name__)

USER_SETTING_KEY = "snaptrade_user"
# SnapTrade's error code for "registerUser is not available for personal keys".
PERSONAL_KEY_CODE = "1012"
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


#: The paid add-on that unlocks brokerage sync on a hosted plan. Self-host is
#: unaffected — someone running their own instance brings their own SnapTrade
#: credentials and owes us nothing for using them.
BROKER_SYNC_FEATURE = "broker_sync"


def broker_sync_entitled() -> bool:
    """Whether this account may use brokerage sync.

    Only gated where Serin is the one paying SnapTrade. An open-source or
    self-hosted instance has no verifier installed, so this is True and the
    feature behaves exactly as it always has — the add-on exists to cover a
    per-user cost that only a hosted deployment incurs.
    """
    from backend import entitlements

    if entitlements.summary()["plan"] == "opensource":
        return True
    return entitlements.has(BROKER_SYNC_FEATURE)


def snaptrade_available() -> bool:
    client_id, consumer_key = resolved_credentials()
    return bool(client_id and consumer_key)


def error_message(exc: Exception) -> str:
    """A SnapTrade failure in one line someone can act on.

    The SDK's ApiException stringifies to the entire HTTP exchange — status,
    every response header, then the body. Surfaced in the UI that fills the
    screen with rate-limit headers and a request id, burying the one sentence
    that says what to do. Keep the sentence, drop the transcript.
    """
    status = getattr(exc, "status", None)
    detail = ""
    code = ""
    body = getattr(exc, "body", None)
    if body is not None:
        if isinstance(body, bytes | bytearray):
            try:
                body = body.decode()
            except Exception:
                body = ""
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                detail = body.strip()[:200]
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or "").strip()
            code = str(body.get("code") or "").strip()

    if code == PERSONAL_KEY_CODE or (status == 400 and "personal" in detail.lower()):
        # SnapTrade's free tier hands out one pre-provisioned user and refuses
        # registerUser. Nothing is wrong with the keys — Serin just has to be
        # told which user they came with, which lives in the same dashboard.
        return (
            "This is a personal SnapTrade key, which comes with one user "
            "already created. Copy that user's ID and secret from your "
            "SnapTrade dashboard into SNAPTRADE_USER_ID and "
            "SNAPTRADE_USER_SECRET, then try again."
        )
    if status in (401, 403):
        # Both credentials are sent on every call, so "not provided" upstream
        # means "not accepted" — say the actionable thing, not the literal one.
        return (
            "SnapTrade rejected Serin's credentials. Check the Client ID and "
            "Consumer Key against your SnapTrade dashboard — a key for the "
            "wrong environment, or a revoked one, fails exactly like this."
        )
    if status == 429:
        return "SnapTrade is rate-limiting requests. Wait a moment and try again."
    if status and 500 <= int(status) < 600:
        return f"SnapTrade is having trouble (HTTP {status}). Usually transient — try again."
    if status:
        return f"SnapTrade returned HTTP {status}{f': {detail}' if detail else '.'}"
    if isinstance(exc, SnapTradeError):
        return str(exc)
    return (str(exc).splitlines() or [""])[0][:200] or "SnapTrade request failed."


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

        # SDK 13 made the Personal/Commercial split explicit at construction
        # and stopped accepting bare consumer_key/client_id — passing them
        # raises TypeError, which is what broke this integration silently when
        # an unpinned rebuild pulled 13.x.
        #
        # Commercial is the right mode: Serin owns the SnapTrade account and
        # registers one end-user per Serin account. Personal is for an
        # individual connecting their own brokerages under their own key, and
        # takes a different flow entirely (no user registration, no
        # userSecret) — see docs/CONNECTORS.md.
        try:
            from snaptrade_client.auth import SnapTradeAuth

            _client = SnapTrade(
                auth=SnapTradeAuth.commercial_api_key(
                    consumer_key=consumer_key, client_id=client_id
                )
            )
        except ImportError:
            # SDK 11/12, where auth modes did not exist yet.
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


#: Keys SnapTrade wraps a collection in, newest first. `get_account_activities`
#: answers {"data": [...], "pagination": {...}}; older endpoints use "results".
_ENVELOPE_KEYS = ("data", "results", "items")


def _response_rows(body: Any) -> list[Any]:
    """The rows inside a SnapTrade response, whatever it wrapped them in.

    The fallback used to be ``list(body)``, which on a dict yields its *keys*.
    So an activities response of {"data": [623 trades], "pagination": {...}}
    came back as the two strings "data" and "pagination", both were discarded
    as unmappable, and the import reported success having stored nothing.
    Every backfill silently did this — 720 activities across six accounts.

    An unrecognised dict now returns nothing and says so, rather than
    manufacturing rows out of key names.
    """
    if body is None:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in _ENVELOPE_KEYS:
            if key in body:
                value = body.get(key) or []
                return value if isinstance(value, list) else [value]
        logger.warning(
            "Unrecognised SnapTrade response envelope with keys %s — returning no "
            "rows rather than iterating key names", sorted(body)[:8],
        )
        return []
    try:
        return list(body)
    except TypeError:
        return [body]


# ---------------------------------------------------------------------------
# User registration (single local end-user)


def get_stored_user() -> dict | None:
    """The SnapTrade end-user for the *current Serin account*.

    app_settings is scoped by user_id, so one SnapTrade identity per Serin
    account already falls out of the storage layer — the hosted requirement in
    this module's docstring is half met by that alone.

    The userSecret is decrypted on read. It is the credential that authorises
    reading someone's brokerage holdings, and on Cloud it lives in a database
    shared by every customer; plaintext there is a much larger promise than
    plaintext on a box its owner controls.
    """
    raw = db.get_setting(USER_SETTING_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    secret = data.get("userSecret") or ""
    if secret:
        try:
            data["userSecret"] = secrets_store.decrypt(secret)
        except Exception:
            # Written before this was encrypted, or written under a key this
            # instance no longer has. Plaintext still works; a hard failure
            # here would strand a working connection.
            pass
    return data if data.get("userId") and data.get("userSecret") else None


def _store_user(user: dict) -> None:
    """Persist the end-user, encrypting the secret half."""
    payload = dict(user)
    secret = payload.get("userSecret") or ""
    if secret and not secrets_store.is_encrypted(secret):
        payload["userSecret"] = secrets_store.encrypt(secret)
    db.set_setting(USER_SETTING_KEY, json.dumps(payload))


def get_or_register_user() -> dict:
    existing = get_stored_user()
    if existing:
        return existing
    # Personal-tier keys: SnapTrade pre-provisions one user at signup and
    # blocks registerUser. Use the userId/userSecret from the dashboard.
    if settings.snaptrade_user_id and settings.snaptrade_user_secret:
        # That pre-provisioned user is one brokerage identity, and this branch
        # cannot see whose request it is serving. On a shared deployment every
        # account would be handed the same one, so the first person to connect
        # a broker would show their holdings to everybody else. Refuse rather
        # than pool them; a shared deployment needs partner credentials, which
        # register a separate SnapTrade user per account.
        if scope.provider_installed():
            raise SnapTradeError(
                "SnapTrade is configured with a personal key, which carries a "
                "single pre-provisioned user. This deployment has accounts "
                "switched on, so that one user would be shared by everybody. "
                "Broker sync here needs partner credentials from SnapTrade."
            )
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
    _store_user(user)
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
    # CONTRIBUTION and WITHDRAWAL are the two that cross the portfolio
    # boundary, so they are the only ones TWR has to neutralise. Getting
    # either wrong is the classic error: a contribution counted as growth
    # makes saving look like skill.
    "CONTRIBUTION": "deposit",
    "DEPOSIT": "deposit",
    "WITHDRAWAL": "withdrawal",
    "FEE": "fee",
    "TAX": "tax",
    "WITHHOLDING": "tax",
    "INTEREST": "interest",
    "REI": "buy",  # dividend reinvestment lands as a buy
    # A transfer between two accounts Serin already tracks nets to nothing.
    # Treating it as external would show a contribution on one side and a
    # withdrawal on the other, moving total return for no reason.
    "TRANSFER": "transfer",
    "STOCK_DIVIDEND": "adjustment",
    "SPLIT": "split",
}

BACKFILL_REF_PREFIX = "snaptrade:"

#: Activities page size. SnapTrade caps a page at 1000 and the endpoint is
#: rate limited per account, so ask for the maximum and page as rarely as
#: possible.
_ACTIVITY_PAGE = 1000


def _day_key(symbol: str, action: str, occurred_at: str, broker: str = "") -> tuple:
    """What makes two records the same real-world trading activity.

    Not a row-level fingerprint, because the two sources do not agree on what
    a row *is*. A broker CSV lists individual executions — 97 separate TQQQ
    buys — while the API returns the order that produced them, one row of
    43,101 shares at the average fill price. No amount of rounding on quantity
    and price can reconcile those, and matching on them counted every
    overlapping trade twice: TQQQ rewound to minus 15,500 shares against a
    holding of 2,001.

    A day is the unit both sources agree on. Twenty of the twenty-one
    overlapping groups in the book that exposed this matched to the share on
    (symbol, day, action) while their row counts differed by up to 97 to 1.

    Carries the broker for the same reason the statement's span does: without
    it, buying TQQQ at Fidelity on a day a Robinhood export happens to mention
    TQQQ reads as the same trade, and the Fidelity purchase is silently
    dropped.
    """
    return ((symbol or "").upper(), action, (occurred_at or "")[:10], broker)


#: Cash events that both a statement and the API report, keyed the same way.
#: Their magnitude is money rather than shares — a dividend has no quantity —
#: so they are summed on amount. Six interest credits in the affected book
#: appeared in both sources for the identical cent.
_CASH_ACTIONS = frozenset({"dividend", "interest", "fee", "tax",
                           "deposit", "withdrawal"})


def _day_magnitude(transaction) -> float:
    """Shares for a trade, money for a cash event."""
    action = canonical_action(transaction.action)
    if action in ("buy", "sell"):
        return float(transaction.quantity or 0)
    return abs(float(transaction.amount or 0))


def _day_totals(transactions: list) -> dict[tuple, float]:
    """Magnitude per ``(symbol, day, action)``, for comparing across sources.

    Covers cash events as well as trades. Double-counted interest does not
    drive share counts negative the way a duplicated buy does, so it fails
    silently rather than loudly — which is the more dangerous of the two.
    """
    totals: dict[tuple, float] = {}
    for t in transactions:
        action = canonical_action(t.action)
        if action in ("buy", "sell"):
            if not t.symbol:
                continue
        elif action not in _CASH_ACTIONS:
            continue
        key = _day_key(t.symbol, action, t.occurred_at, t.broker)
        totals[key] = totals.get(key, 0.0) + _day_magnitude(t)
    return totals


def _statement_ranges(transactions: list) -> dict[tuple, tuple[str, str]]:
    """``(symbol, broker) -> (first day, last day)`` the statement covers.

    Keyed by broker as well as symbol because a statement is one account's
    activity export. Someone holding TQQQ at Robinhood and at Fidelity has a
    Robinhood export that says nothing about the Fidelity lot, and letting its
    date range suppress the other account's trades would delete real history
    to fix an unrelated duplicate.
    """
    ranges: dict[tuple, list[str]] = {}
    for t in transactions:
        if canonical_action(t.action) not in ("buy", "sell") or not t.symbol:
            continue
        key = ((t.symbol or "").upper(), t.broker)
        day = (t.occurred_at or "")[:10]
        span = ranges.setdefault(key, [day, day])
        span[0] = min(span[0], day)
        span[1] = max(span[1], day)
    return {k: (v[0], v[1]) for k, v in ranges.items()}


def _covered_by_statement(ranges: dict[tuple, tuple[str, str]],
                          symbol: str, broker: str, day: str) -> bool:
    span = ranges.get(((symbol or "").upper(), broker))
    return bool(span and span[0] <= (day or "")[:10] <= span[1])


def _net_shares(transactions: list) -> float:
    total = 0.0
    for t in transactions:
        action = canonical_action(t.action)
        if action == "buy":
            total += float(t.quantity or 0)
        elif action == "sell":
            total -= float(t.quantity or 0)
    return total


#: How far apart the two sources may date the same trade. Settlement lags
#: execution by a day or two, and a statement that reports on one convention
#: while the API reports on the other puts one sale on the 8th and the 9th.
#: Four days spans a weekend without reaching the next week's trading.
_SETTLEMENT_WINDOW_DAYS = 4


def _vwap(rows: list) -> float:
    shares = sum(abs(float(t.quantity or 0)) for t in rows)
    if shares <= 0:
        return 0.0
    return sum(abs(float(t.quantity or 0)) * float(t.price or 0)
               for t in rows) / shares


def _group_trades(rows: list) -> dict[tuple, list]:
    """``(symbol, broker, action, day) -> rows``, trades only."""
    groups: dict[tuple, list] = {}
    for t in rows:
        action = canonical_action(t.action)
        if action not in ("buy", "sell") or not t.symbol:
            continue
        key = ((t.symbol or "").upper(), t.broker, action, (t.occurred_at or "")[:10])
        groups.setdefault(key, []).append(t)
    return groups


def _settlement_duplicates(statement_rows: list, broker_rows: list) -> list:
    """Broker rows that are a statement day re-dated by a settlement lag.

    The case neither day-matching nor span precedence can see: one AFRM sale
    of 519 shares at $84.00, reported by the API as a single order on the 8th
    and by the statement as three fills on the 9th. The two are outside each
    other's day and outside the statement's one-day span, so both survived —
    and $43,596 of proceeds became $87,191 of cash the account never held.

    Demanding an exact share total *and* a matching average price makes this
    narrow on purpose. Selling the same quantity of the same holding at the
    same average price twice inside four days is possible; paying for it with
    invented cash on every reconstruction is worse.
    """
    statement_groups = _group_trades(statement_rows)
    claimed: set[tuple] = set()
    duplicates: list = []

    for key, rows in sorted(_group_trades(broker_rows).items()):
        symbol, broker, action, day = key
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        shares = sum(abs(float(t.quantity or 0)) for t in rows)
        price = _vwap(rows)

        for other, candidate in sorted(statement_groups.items()):
            if other in claimed or other[:3] != (symbol, broker, action):
                continue
            try:
                offset = abs((date.fromisoformat(other[3]) - when).days)
            except ValueError:
                continue
            if offset > _SETTLEMENT_WINDOW_DAYS:
                continue
            other_shares = sum(abs(float(t.quantity or 0)) for t in candidate)
            if abs(other_shares - shares) > 1e-6:
                continue
            other_price = _vwap(candidate)
            tolerance = max(abs(other_price), abs(price)) * 0.005
            if abs(other_price - price) > max(tolerance, 0.01):
                continue
            claimed.add(other)
            duplicates.extend(rows)
            break

    return duplicates


def repair_duplicate_backfill(dry_run: bool = True) -> dict[str, Any]:
    """Remove the trades a pre-fix backfill double-counted.

    The original check compared rows, and the two sources do not agree on what
    a row is: a statement lists executions, the API lists the order behind
    them. So trades the statement already held were imported a second time,
    the ledger stopped agreeing with the holdings, the rewind subtracted more
    shares than were ever owned, and reconstructed history went negative — one
    book showed securities of -$496,746.85 on the first day of the year, drawn
    as "+$1,059,137.85 (+0.00%)".

    Two passes, because one is not enough and three would be guessing:

    1. Inside the span a statement covers for a ``(symbol, broker)``, the
       statement is the record. This catches what day-matching missed, where
       the two sources dated the same trade differently — execution against
       settlement.
    2. If the symbol still does not reconcile to the position, and dropping
       the remaining broker rows makes it reconcile exactly, they go too. Self-
       validating: it acts only where it can show the result is right.

    What is left after that is reported, never guessed at.
    """
    all_rows = db.list_transactions(limit=500_000)
    statement_rows = [t for t in all_rows if t.source != "snaptrade"]
    ranges = _statement_ranges(statement_rows)

    held: dict[tuple, float] = {}
    for position in db.list_positions(include_closed=True):
        if position.asset_type in ("cash", "option"):
            continue
        key = ((position.symbol or "").upper(), position.broker)
        held[key] = held.get(key, 0.0) + float(position.quantity or 0)

    broker_trades: dict[tuple, list] = {}
    for t in all_rows:
        if t.source == "snaptrade" and t.symbol and \
                canonical_action(t.action) in ("buy", "sell"):
            broker_trades.setdefault(((t.symbol or "").upper(), t.broker), []).append(t)

    doomed: list = []

    # Pass 1b, before reconciliation: the same trade dated either side of a
    # settlement lag. Reconciliation cannot catch these on a holding that was
    # closed and removed, because there is no position left to anchor against
    # — which is exactly where the AFRM sale hid.
    already = {t.id for key, rows in broker_trades.items() for t in rows
               if _covered_by_statement(ranges, key[0], key[1], t.occurred_at)}
    doomed.extend(_settlement_duplicates(
        statement_rows,
        [t for rows in broker_trades.values() for t in rows if t.id not in already],
    ))
    settled = {t.id for t in doomed}

    for key, rows in broker_trades.items():
        symbol, broker = key
        inside = [t for t in rows
                  if _covered_by_statement(ranges, symbol, broker, t.occurred_at)]
        outside = [t for t in rows
                   if t not in inside and t.id not in settled]
        doomed.extend(inside)

        statement_net = _net_shares(
            [t for t in statement_rows
             if (t.symbol or "").upper() == symbol and t.broker == broker]
        )
        if key not in held:
            # No position to reconcile against, so there is nothing to check
            # the removal against either. Pass 2 would be guessing.
            continue
        position = held[key]
        if abs(statement_net + _net_shares(outside) - position) <= 1e-6:
            continue                       # reconciles once pass 1 is applied
        if abs(statement_net - position) <= 1e-6 and outside:
            # Dropping the rest makes it agree exactly. A duplicate the two
            # sources dated either side of the statement's own span.
            doomed.extend(outside)

    # Cash events cannot be reconciled against a share count, so they keep the
    # day match: interest credited twice on one date is the same credit.
    cash_seen = _day_totals([t for t in all_rows if t.source != "snaptrade"])
    for t in all_rows:
        action = canonical_action(t.action)
        if t.source == "snaptrade" and action in _CASH_ACTIONS and \
                _day_key(t.symbol, action, t.occurred_at, t.broker) in cash_seen:
            doomed.append(t)

    removed = 0
    if not dry_run:
        for t in doomed:
            if t.id and db.delete_transaction(t.id):
                removed += 1

    # Whatever still fails to reconcile, said plainly rather than papered over.
    kept_ids = {t.id for t in doomed}
    unresolved = []
    for key, position in sorted(held.items()):
        symbol, broker = key
        rows = [t for t in all_rows
                if (t.symbol or "").upper() == symbol and t.broker == broker
                and canonical_action(t.action) in ("buy", "sell")
                and t.id not in kept_ids]
        if not rows:
            # No ledger at all for this holding. That is the missing-history
            # case data_gaps already names, with an action attached; repeating
            # it here as a failed reconciliation buries the handful of
            # symbols where a ledger exists and genuinely does not add up.
            continue
        net = _net_shares(rows)
        if abs(net - position) > 1e-6:
            unresolved.append({"symbol": symbol, "broker": broker,
                               "ledger": round(net, 4),
                               "held": round(position, 4)})

    return {
        "dry_run": dry_run,
        "duplicate_rows": len(doomed),
        "removed": removed,
        "symbols": sorted({t.symbol for t in doomed}),
        "unresolved": unresolved,
    }


def backfill_transactions(days: int | None = None) -> dict:
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

    # No window by default. SnapTrade's activities endpoint returns *all*
    # historical transactions for an account; start_date and end_date are
    # optional filters. Asking for 365 days was our own limit, and it was the
    # main reason a new customer's realized gains looked wrong: anything they
    # bought more than a year ago and sold this year arrived as a sale with no
    # purchase on record, so it could not be counted as gain at all.
    end = datetime.now(UTC).date()
    start = end - timedelta(days=max(1, days)) if days else None

    # Activities are per *account*, not per user, and paginated at 1000 rows.
    # The previous call — `transactions_and_reporting.get_activities` for the
    # whole user — does not exist in SDK 13: the group was renamed and the
    # endpoint moved under the account. It raised AttributeError before any
    # request went out, so the button failed with a generic 502 that looked
    # like a SnapTrade outage. Same shape as the SDK 13 constructor change.
    activities: list = []
    for account in list_accounts():
        account_id = account.get("id")
        if not account_id:
            continue
        offset = 0
        while True:
            request = {
                "account_id": account_id,
                "user_id": user["userId"],
                "user_secret": user["userSecret"],
                "end_date": end.isoformat(),
                "offset": offset,
                "limit": _ACTIVITY_PAGE,
            }
            if start is not None:
                request["start_date"] = start.isoformat()
            response = client.account_information.get_account_activities(**request)
            page = _response_rows(response.body)
            activities.extend(page)
            # A short page is the last page. Guard the equal case too: a full
            # page with no further rows would otherwise loop once more and
            # come back empty, which is harmless but wastes a rate-limited
            # call per account.
            if len(page) < _ACTIVITY_PAGE:
                break
            offset += len(page)

    # Cross-source duplicates. The database's unique index catches a *repeat*
    # backfill, because those carry the same snaptrade: reference — but it
    # cannot see that a trade already arrived from a broker CSV, which is
    # fingerprinted differently. Importing a year of activity on top of an
    # imported statement would double every overlapping buy and sell, and a
    # doubled trade corrupts share counts, returns and coverage at once.
    # Deliberately excludes rows this backfill wrote before: a repeat run is
    # already caught by the unique index on the snaptrade: reference, and it
    # should report as "already on record" rather than as a collision with a
    # statement. Letting the content check fire first would relabel every
    # re-run as a cross-source duplicate and hide whether anything actually
    # overlapped.
    # Keyed by day, not by row. The statement lists executions and the API
    # lists the orders behind them, so no row-level fingerprint can match the
    # two: a day the statement covers as 97 separate TQQQ buys arrives here as
    # a single 43,101-share order. Comparing rows imported every overlapping
    # trade a second time and drove the reconstruction negative.
    # One read, two questions: which days the statement already accounts for,
    # and which spans it is authoritative over.
    statement_rows = [t for t in db.list_transactions(limit=100_000)
                      if t.source != "snaptrade"]
    existing = _day_totals(statement_rows)
    # Shares the broker reports on a day the statement already covers, so the
    # two can be compared after the fact. Skipping is the safe direction — a
    # missed fill can be imported later, whereas a double-counted one corrupts
    # every reconstructed figure at once — but it must not be silent.
    seen_on_covered_days: dict[tuple, float] = {}
    statement_ranges = _statement_ranges(statement_rows)

    # Deduplication is the database's, via the partial unique index on
    # (user_id, external_id). The previous approach loaded every transaction
    # this account had ever recorded and string-matched their notes — O(n) on
    # each backfill, and two syncs running at once would both pass the check
    # and both insert.
    imported = 0
    skipped_existing = 0
    skipped_unknown = 0
    skipped_duplicate = 0
    for activity in activities:
        activity_id = str(_g(activity, "id", default="") or "")
        ref = f"{BACKFILL_REF_PREFIX}{activity_id}"
        if not activity_id:
            # Without the broker's own id there is nothing stable to dedupe
            # on, and importing it would duplicate on the next sync.
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

        day_key_early = _day_key(symbol, action, occurred, broker)
        if action in ("buy", "sell") and _covered_by_statement(
                statement_ranges, symbol, broker, occurred):
            if day_key_early in existing:
                seen_on_covered_days[day_key_early] = (
                    seen_on_covered_days.get(day_key_early, 0.0) + units
                )
            # The statement is this account's complete activity export for the
            # period it spans, so inside that span it is the record — matching
            # day by day missed the trades the two sources dated differently,
            # settlement against execution, and those were still counted twice.
            skipped_duplicate += 1
            continue

        day_key = _day_key(symbol, action, occurred, broker)
        if day_key in existing:
            # The statement already covers this symbol on this day. Its rows
            # are finer-grained and were there first, so they stand.
            seen_on_covered_days[day_key] = (
                seen_on_covered_days.get(day_key, 0.0)
                + (units if action in ("buy", "sell") else abs(amount or price))
            )
            skipped_duplicate += 1
            continue

        try:
            created = db.create_transaction(
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
                external_id=ref,
            )
            if created is None:
                skipped_existing += 1
                continue
            imported += 1
        except Exception:
            logger.warning("Skipping unmappable SnapTrade activity %s", activity_id, exc_info=True)
            skipped_unknown += 1

    logger.info(
        "SnapTrade backfill: %d imported, %d already imported, %d matched an "
        "existing row from another source, %d unmappable",
        imported, skipped_existing, skipped_duplicate, skipped_unknown,
    )
    # Sweep what the row-by-row checks structurally cannot see: the same trade
    # dated either side of a settlement lag, which needs both sides in hand to
    # recognise. Idempotent and conservative, so running it here costs nothing
    # on a clean import and stops the next sync reintroducing $43,596 of
    # proceeds as $87,191 of cash.
    swept = repair_duplicate_backfill(dry_run=False) if imported else {"removed": 0}

    disagreements = [
        {"symbol": key[0], "action": key[1], "date": key[2], "broker": key[3],
         "statement": round(existing[key], 4),
         "broker_shares": round(shares, 4)}
        for key, shares in sorted(seen_on_covered_days.items())
        if abs(shares - existing[key]) > 1e-6
    ]

    return {
        "imported": imported,
        "skipped_existing": skipped_existing,
        "skipped_duplicate": skipped_duplicate,
        "skipped_unknown": skipped_unknown,
        "disagreements": disagreements,
        "swept_duplicates": swept.get("removed", 0),
        "window_days": days,
        "from": start.isoformat() if start else "",
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


def _mask_number(number: str | None) -> str:
    """Last four digits only.

    Brokers are inconsistent about what they hand back — Fidelity pre-masks to
    "*****6905" while Robinhood returns the number whole — so the masking
    happens here rather than being trusted to the upstream. Four digits is
    enough to tell two accounts apart, which is all this screen needs it for.
    """
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    # Only when the tail is actually digits. E*Trade returns an internal
    # identifier here rather than an account number, and masking it produced
    # "\u2022\u2022\u2022\u2022JlsQ" — which looks like data and is not. Showing nothing
    # is better than showing something a person cannot check against a
    # statement.
    return f"\u2022\u2022\u2022\u2022{digits[-4:]}" if len(digits) >= 4 else ""


#: raw_type/meta.type as brokers spell it, in the words a statement uses.
_ACCOUNT_TYPE_LABELS = {
    "INDIVIDUAL": "Individual",
    "ROTH_IRA": "Roth IRA",
    "TRADITIONAL_IRA": "Traditional IRA",
    "DIGITALASSET": "Crypto",
    "JOINT": "Joint",
    "MARGIN": "Margin",
    "CASH": "Cash",
}


def _synced_at(sync_status: Any, feature: str) -> str:
    block = (sync_status or {}).get(feature) if isinstance(sync_status, dict) else None
    if not isinstance(block, dict):
        return ""
    return str(block.get("last_successful_sync") or "")


def accounts() -> list[dict]:
    """Every connected account, one row each.

    Serin's own tables key on broker, so three institutions collapse six real
    accounts into three rows: an IRA, a crypto account and a taxable account at
    one broker are indistinguishable once their holdings land. This reads the
    accounts themselves so the connection screen can show what a person
    actually recognises — the account they opened, its last four digits, and
    what it is worth.
    """
    user = get_stored_user()
    if not user:
        return []
    client = _get_client()
    rows = _response_rows(
        client.account_information.list_user_accounts(
            user_id=user["userId"], user_secret=user["userSecret"]).body
    )

    out: list[dict] = []
    for row in rows:
        balance = _g(row, "balance", "total", default={}) or {}
        raw_type = str(_g(row, "meta", "type", default="")
                       or _g(row, "raw_type", default="") or "").upper()
        institution = str(_g(row, "institution_name", default="") or "")
        out.append({
            "id": str(_g(row, "id", default="") or ""),
            "institution": _slug_broker(institution),
            "institution_name": institution,
            "name": str(_g(row, "name", default="") or institution),
            "number": _mask_number(_g(row, "number", default="")),
            # An unmapped code is only worth showing if it reads as a word.
            # Fidelity sends raw_type "I", which rendered as an account type
            # of "I" beside the balance.
            "type": _ACCOUNT_TYPE_LABELS.get(
                raw_type,
                raw_type.title().replace("_", " ") if len(raw_type) > 2 else "",
            ),
            "value": _float(balance.get("amount") if isinstance(balance, dict) else 0),
            "currency": str((balance or {}).get("currency") or "USD"),
            "status": str(_g(row, "status", default="") or ""),
            "linked_at": str(_g(row, "created_date", default="") or "")[:10],
            "holdings_synced_at": _synced_at(_g(row, "sync_status", default={}), "holdings"),
            "transactions_synced_at": _synced_at(
                _g(row, "sync_status", default={}), "transactions"),
        })
    out.sort(key=lambda a: (a["institution_name"], -a["value"]))
    return out


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

    # Whether each connection has actually produced holdings yet. Connecting a
    # broker and syncing it are two separate acts — the portal hands back an
    # authorization, and nothing pulls positions until something asks. Without
    # this the UI cannot tell "connected, nothing here yet" from "connected and
    # genuinely empty", so a fresh connection looked like a silent failure.
    synced_brokers: set[str] = set()
    for position in db.list_positions(include_closed=True):
        if position.source == "snaptrade":
            synced_brokers.add(position.broker)
    for connection in connections:
        connection["synced"] = _slug_broker(
            str(connection.get("institution") or "")
        ) in synced_brokers

    return {
        "configured": True,
        "registered": registered,
        "connections": connections,
        "pending_sync": any(not c.get("synced") for c in connections),
        "last_sync": get_last_sync(),
    }
