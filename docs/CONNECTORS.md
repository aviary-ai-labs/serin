# Connectors

Serin's data layer is a **connector platform**. A connector is a small, in-tree
plugin that brings data into Serin or acts on it. The portal (Connectors tab)
reads each connector's manifest and renders its config form automatically — so
adding a connector never requires touching the UI.

## The three kinds

| Kind | Interface | Examples | Default |
|---|---|---|---|
| `market_data` | `refresh_prices`, `fetch_history`, `quote` (+ optional `fetch_fundamentals`) | Yahoo Finance, Financial Modeling Prep | on |
| `holdings` | `sync` / `status` (or one-shot import) | SnapTrade, Coinbase, Generic CSV | on |
| `insight` | `run` | AI daily briefing | off (opt-in) |

## Distribution model

**In-tree only.** Connectors live in this repo and are contributed via pull
request. There is no third-party package installation or remote registry — this
keeps the trust model simple (you can read every connector that ships) and the
review bar high. (A future `entry_points`-based plugin model is possible; the
SDK is shaped to allow it, but it is intentionally not enabled today.)

## Anatomy of a connector

Every connector is a class with:

1. a **`manifest`** (`ConnectorManifest`) — id, kind, human metadata, and a
   `config_schema` (list of `ConfigField`) that the portal turns into a form, and
2. an **implementation** of its kind's interface, plus an optional `test()`
   used by the portal's "Test" button.

Config and enable-state are stored per-connector in SQLite (`app_settings`) and
resolved **DB-first with a settings/env fallback** — a value set in the portal
wins, otherwise the connector falls back to the matching environment variable.

## Write a market-data connector in ~40 lines

```python
# backend/connectors/market_data/alphavantage.py
from backend.connectors.base import ConfigField, ConnectorManifest, MarketDataConnector, TestResult
from backend.connectors.registry import register


@register
class AlphaVantageConnector(MarketDataConnector):
    manifest = ConnectorManifest(
        id="alphavantage",
        name="Alpha Vantage",
        kind="market_data",
        description="Stock and FX data. Free tier: 25 requests/day.",
        icon="ti-chart-candle",
        docs_url="https://www.alphavantage.co/documentation/",
        default_enabled=False,
        config_schema=[
            ConfigField(key="api_key", label="API key", type="password",
                        required=True, secret=True),
        ],
    )

    def refresh_prices(self, positions):
        key = self.get("api_key")
        prices, errors = {}, []
        # ... call Alpha Vantage, fill prices[symbol] = (price, sector) ...
        return {"prices": prices, "errors": errors}

    def fetch_history(self, period, symbols, positions_by_symbol):
        return {"history": {}, "errors": ["history not implemented yet"]}

    def quote(self, symbol, asset_type):
        return None

    # Optional capability — omit it entirely if the provider has no
    # fundamentals; the base class returns None and the chain moves on.
    # Normalized keys (any may be None): name, kind ("stock"|"etf"|"fund"),
    # market_cap, pe_ratio, pb_ratio, beta, dividend_yield (decimal),
    # expense_ratio (decimal). Served + cached by backend/fundamentals.py.
    def fetch_fundamentals(self, symbol, asset_type="stock"):
        return None

    def test(self):
        if not self.get("api_key"):
            return TestResult(ok=False, message="Add your API key first.")
        return TestResult(ok=True, message="Key present.")
```

Then register it for discovery:

```python
# backend/connectors/__init__.py
from backend.connectors.market_data import alphavantage as _alphavantage  # noqa: F401
```

That's it. The connector now appears in the portal with a generated config
form, an enable toggle, and a Test button. No frontend changes.

## ConfigField reference

| field | meaning |
|---|---|
| `key` | stored config key |
| `label` | shown in the portal |
| `type` | `text` · `password` · `url` · `number` · `select` · `textarea` · `boolean` |
| `required` | renders a `*` and signals intent (not server-enforced yet) |
| `default` | pre-filled value |
| `help` | hint text under the field |
| `placeholder` | input placeholder |
| `options` | for `select`: `[{"value","label"}]` |
| `secret` | never returned in plaintext by the API; masked in the portal |

## Testing your connector

Add a test under `tests/` that:
- asserts your connector is registered (`registry.all_manifests()`),
- exercises `test()` with and without config,
- monkeypatches any network driver so the suite stays offline.

See `tests/test_connectors.py` and `tests/test_market_data_providers.py` for
patterns. Run `pytest -q`.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/connectors` | catalog + status + (masked) config |
| GET | `/api/connectors/{id}` | one connector |
| PUT | `/api/connectors/{id}/config` | save config (blank secrets preserved) |
| POST | `/api/connectors/{id}/enable` | `{enabled: bool}` |
| POST | `/api/connectors/{id}/test` | run the health check |

All are also served under `/api/v1/...` for the mobile/SDK client.

## Connector reference

In-app docs: each portal card's **Docs** button renders its section below
(matched by the `### <connector-id> —` heading).

### yahoo — Yahoo Finance

Free quotes and daily history for stocks, ETFs and crypto — no API key, on by
default. Requests fail over between Yahoo's `query1`/`query2` mirrors with a
short backoff when one host rate-limits.

- **Config:** none.
- **Crypto:** symbols normalize to Yahoo's `BTC-USD` convention automatically.
- **Limits:** unofficial API; occasional 429s are absorbed by the local price
  cache once a first fetch has succeeded.

### fmp — Financial Modeling Prep

Richer fundamentals + history via your FMP API key. Preferred automatically
over Yahoo when a key is present.

- **Config:** `api_key` (from financialmodelingprep.com; free tier available).
- **Crypto:** bare symbols map to the USD pair (`BTC` → `BTCUSD`).
- **Limits:** the free tier 402s on some symbols; those fall back to the cache
  or Yahoo.

### coingecko — CoinGecko

Crypto specialist. When enabled, **crypto positions route here** while stocks
stay on your main provider (`asset_scope: "crypto"` routing).

- **Config:** `api_key` (optional demo key; raises rate limits).
- **Limits:** free daily history caps at 365 days, so MAX charts span at most
  a year for crypto served by CoinGecko.

### alphavantage — Alpha Vantage

Stocks & ETFs via a free Alpha Vantage key — a resilient alternative to FMP.

- **Config:** `api_key` (free at alphavantage.co/support/#api-key). The free
  tier is rate-limited (~25 req/day, 5/min), so Serin's price-history cache
  carries most of the load; enable it as a secondary/fallback provider.
- **Covers:** `GLOBAL_QUOTE` (price) + `TIME_SERIES_DAILY` (history) for
  equities/ETFs. Crypto stays on the CoinGecko layer.
- **Trust:** a market-data key fetches *public* prices — not a broker
  credential (see [CONNECTOR-TRUST.md](CONNECTOR-TRUST.md)).

### stooq — Stooq

Free, **keyless** end-of-day history for US stocks, ETFs and indices — Serin's
zero-config resilience layer when Yahoo/FMP rate-limit.

- **Config:** none. Symbols map to Stooq's format automatically (`AAPL` →
  `aapl.us`; qualified symbols like `^spx` or `vwrl.uk` pass through).
- **Covers:** daily OHLC (last close, not intraday). Crypto → CoinGecko.

### snaptrade — SnapTrade

Read-only holdings + cash sync from 20+ brokerages. Serin can never place
trades. **Positioned as the self-host aggregator** (BYO operator keys, free for
one connected user); Cloud's primary Connect is Plaid.

- **Config:** `client_id`, `consumer_key` (portal-first; falls back to
  `SNAPTRADE_CLIENT_ID` / `SNAPTRADE_CONSUMER_KEY` env vars),
  `auto_sync_daily` (sync once a day on the scheduler).
- **Import history:** the Brokerage Sync panel's *Import history* pulls broker
  activities (buys, sells, dividends, fees) into the transactions table.
  Idempotent — each activity id imports once.

### coinbase — Coinbase

Read-only sync of your Coinbase crypto balances into positions tagged
`source='coinbase'`. Serin can never trade or withdraw.

- **Config:** `api_key`, `api_secret` — create a **read-only** key at
  [coinbase.com/settings/api](https://www.coinbase.com/settings/api) with the
  `wallet:accounts:read` scope only. The secret is encrypted at rest.
- **Sync:** press **Sync now** on the connector card (generic
  `POST /api/connectors/coinbase/sync`). Balances upsert as crypto positions;
  coins you no longer hold are removed. Cost basis isn't exposed by the balance
  API, so `average_cost` starts at 0; USD value is seeded from Coinbase's
  `native_balance` and then maintained by your market-data provider.
- **Auth:** HMAC-SHA256 over `timestamp + "GET" + path` (classic v2 scheme).
  Use a legacy retail API key; CDP/JWT keys aren't supported yet.

### binance — Binance

Read-only sync of your Binance **spot** balances (Binance.com or Binance.US)
into positions tagged `source='binance'`.

- **Config:** `api_key`, `api_secret` (create a key with **Enable Reading**
  only — never trading/withdrawals; add an IP allowlist if you can), and
  `base_url` (Binance.com global vs Binance.US).
- **Sync:** **Sync now** on the card. Every asset with a non-zero balance
  (free + locked) upserts as a crypto position; assets you no longer hold are
  removed. `/account` carries no cost basis or USD value, so `average_cost`
  and price start at 0 — press **Refresh** and the crypto market-data layer
  (CoinGecko) prices them.
- **Auth:** HMAC-SHA256 over the request query string, sent as
  `X-MBX-APIKEY`.

### generic_csv — CSV import

One-shot positions import from a CSV file. For arbitrary formats (or
screenshots), prefer **Smart Import**, which AI-extracts and previews rows
before anything is written.

### ai_briefing — AI daily briefing

Optional daily plain-language briefing. Provider + API keys are configured
here and honored everywhere (briefings, Smart Import, status chips).

- **Config:** `provider` (auto / Anthropic / DeepSeek / Claude CLI),
  `anthropic_api_key`, `deepseek_api_key`, `style`.
- **Cost guard:** the Briefings tab shows the resolved model and an estimated
  $/run next to the Run button.
- **Privacy:** your portfolio snapshot and headlines are sent to the chosen
  cloud provider only when a briefing runs; nothing runs while the connector
  is off.

### Starting a new connector

Copy `backend/connectors/market_data/_template.py`, rename the class and
manifest id, implement the three methods, and register the module import in
`backend/connectors/__init__.py`. The template's comments walk through each
step; `tests/test_connector_template.py` shows the minimal test harness.
