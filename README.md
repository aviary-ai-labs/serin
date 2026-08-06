# Serin

**The most extensible open-source portfolio tracker — connect any broker, any
data source, any format, through a connector you can configure or write
yourself.**

Serin's data layer is a [connector platform](docs/CONNECTORS.md): market-data
feeds, broker syncs, crypto prices, file imports, and optional AI insights are
all plugins. The **Connectors portal** lets you wire up where your data comes
from without editing code — and adding a new connector is a ~40-line module
and a pull request, with the config UI generated automatically from the
connector's manifest.

Out of the box it's a full portfolio tracker (positions, transactions,
multi-currency, real time-weighted and money-weighted returns, stock charts,
tax lots, AI briefings, a companion mobile app) that works **free with no API
key** (Yahoo Finance). Serin provides context and organization, not investment
advice.

## Free forever — the open-core promise

This repo is the **complete free product**, licensed AGPLv3, self-hosted, no
telemetry, your data stays on your machine.

Serin is sustained by an open-core model: optional paid layers (a hosted cloud
and a closed-source "Intelligence" add-on pack) fund development. Two promises,
in writing in [docs/BUSINESS-MODEL.md](docs/BUSINESS-MODEL.md):

- **The one-way door:** features never move from free to paid. Anything in
  this repo stays here.
- **No strings:** the core never phones home, has no license checks, and no
  kill switches. Paid add-ons load through the same public plugin seam
  (`SERIN_PLUGINS_DIR`) that you can use for your own private plugins.

## Features

- **Connectors** — a plugin platform for your data layer. Browse the catalog,
  toggle connectors on/off, and configure them through schema-generated forms
  in the Connectors portal. Ships with Yahoo Finance (free), Financial
  Modeling Prep, CoinGecko crypto prices, SnapTrade broker sync, a no-code
  Generic CSV mapper, and an optional AI briefing. Secrets are encrypted at
  rest (AES-256-GCM). Write your own in ~40 lines — see
  [docs/CONNECTORS.md](docs/CONNECTORS.md).
- **Overview** — total value, total gain, day change, invested capital;
  holdings trend chart with range and broker filters; sortable positions table
  with per-position day change and P&L sparklines; sector and broker
  allocation; top holdings; position inspector with tax lots (long/short-term
  holding periods).
- **Real returns** — transaction-aware time-weighted return (TWR),
  Modified Dietz and XIRR money-weighted returns, per period
  (WTD/MTD/YTD/1Y/Max) — not naive price deltas.
- **Multi-currency** — positions in any currency, ECB FX conversion, display
  currency switcher.
- **Stocks / Holdings Explorer** — per-symbol drill-in with SVG
  candlestick/line chart, moving-average overlay, allocation treemap, and
  period-return metrics. Price history is cached locally so charts load
  instantly and survive provider rate limits.
- **Smart Import** — drop broker PDFs, CSVs, screenshots, or a filled
  template; AI extracts positions for mandatory human review before anything
  is committed. Works with Anthropic or DeepSeek keys configured in the
  portal.
- **Briefings** — one-click AI daily briefing: portfolio context, market
  context, watch items, risk flags, and questions to review. History includes
  the model, runtime, and estimated cost. Context and organization only —
  never trade advice.
- **Scheduled morning briefing** — daily run at a chosen time and timezone,
  with catch-up after downtime, retry with backoff, and full history
  (tagged `auto`). Optional email delivery via any SMTP relay.
- **Brokerage sync** — connect brokerages read-only through SnapTrade
  (Robinhood, E*Trade, Schwab, Fidelity, and more); holdings, cash, and
  transaction history sync and reconcile automatically. Serin can never place
  trades, and you can disconnect anytime.
- **Mobile app** — an Expo (iOS/Android) companion in [`mobile/`](mobile/)
  with portfolio, charts, briefings, camera Smart Import, offline snapshot,
  QR pairing, biometric lock, and push notifications. See
  [docs/MOBILE-RELEASE.md](docs/MOBILE-RELEASE.md).
- **Your data, portable** — one-click full backup (SQLite snapshot), positions
  CSV export, and restore, from the Connectors → Data panel.
- **App lock** — optional password gate (`SERIN_AUTH_PASSWORD`) for instances
  exposed beyond localhost, with session cookies and Bearer tokens for the
  mobile app.

## Quickstart with Docker

The fastest way to run Serin — works on macOS, Linux, and Windows (with
Docker Desktop). Free Yahoo Finance data is on by default; no API key
required to get going.

```bash
git clone https://github.com/aviary-ai-labs/serin.git
cd serin
docker compose up -d
```

Open <http://localhost:8890>. Configure data sources in the Connectors tab.

Persistent state (your SQLite database and encryption key) lives in a named
volume, so rebuilding the image never loses your portfolio. To stop:
`docker compose down`. To upgrade: `docker compose pull && docker compose up
-d --build`. For reverse-proxy/TLS/app-lock guidance see
[docs/DEPLOY.md](docs/DEPLOY.md).

## Local development (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
cp .env.example .env
```

## AI providers

Serin supports three briefing/Smart-Import providers, resolved in this order
when `SERIN_AI_PROVIDER=auto`. Keys can be set in the Connectors portal
(encrypted at rest) or via env vars:

| Provider | Env var | Use for |
| --- | --- | --- |
| Anthropic API | `ANTHROPIC_API_KEY` | Production (best quality) |
| DeepSeek API | `DEEPSEEK_API_KEY` | Production (lowest cost, `deepseek-v4-flash`) |
| Claude CLI | `CLAUDE_CODE_OAUTH_TOKEN` | Local development only |

**Production note:** the Claude CLI path rides a personal Claude subscription
login. That is fine on your own machine, but a deployed/production instance
must use an API key (`ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY`) — personal
subscription tokens are not licensed for powering a service and do not scale.
The briefing is plain text generation, so any capable model works; DeepSeek
runs it at roughly 1/20th the cost of Claude Sonnet.

For local development with your Claude subscription, run `claude setup-token`
and set `CLAUDE_CODE_OAUTH_TOKEN` in `.env`.

## Scheduled briefings

Turn on the schedule in the Briefings tab (time + timezone). Implementation
notes:

- The scheduler is an in-process asyncio loop owned by the FastAPI app — no
  external cron needed. State lives in SQLite, so it survives restarts.
- Catch-up: if the app was down at the scheduled time, the run fires on next
  launch the same day.
- Failures retry up to 3 times per day with a 10-minute backoff; every attempt
  is a visible row in the briefing history, so a broken provider is never
  silent.
- Single-instance by design. If you ever run multiple backend replicas behind
  a load balancer, move the run-dedupe to a shared lock (e.g. Postgres
  advisory lock) first.

## Brokerage sync (SnapTrade)

Add your `client_id` and `consumer_key` in **Connectors → SnapTrade** (free
for one connected user at [snaptrade.com](https://snaptrade.com/)) — or set
`SNAPTRADE_CLIENT_ID` / `SNAPTRADE_CONSUMER_KEY` in `.env`. A **Brokerage
Sync** card appears in the Overview sidebar:

- **Connect a brokerage** opens SnapTrade's hosted Connection Portal in a new
  tab. Serin only ever requests **read-only** access (`connection_type=read`)
  and receives scoped tokens — never your brokerage password.
- **Sync now** pulls holdings and cash into your portfolio; transaction
  backfill imports your trade history for accurate money-weighted returns.
  Synced positions are tagged `source='snaptrade'` and marked with a ↻.
- Reconciliation is safe: a sync upserts synced rows and removes ones you've
  closed, but **never touches manual or CSV positions**, and disconnecting one
  broker leaves the others intact.

## Market data

Yahoo Finance is the free default — no key needed. Optionally enable
Financial Modeling Prep (set your `FMP_API_KEY` in the portal) for quotes,
company-profile enrichment, and EOD history; CoinGecko covers crypto. Which
provider is active is a one-click choice in the Connectors portal, and
historical prices are cached locally so a rate-limited provider degrades to
cached charts instead of errors.

Production note: confirm your FMP plan/license permits the way Serin displays
and caches data for end users before launch.

## Database

Serin is a local single-user SQLite app by design. It ships with:

- WAL mode + busy timeout for safe local concurrency.
- A **versioned migration runner** (`schema_version` table) — upgrades are
  idempotent and ordered.
- **Encrypted secrets at rest** — connector credentials are AES-256-GCM
  encrypted with a key at `data/.serin-key` (or `SERIN_SECRET_KEY`).

**Accounts are deliberately out of scope for the core.** Self-hosted Serin is
single-user: one instance, one owner, no login to create on a machine only you
can reach. The core carries a neutral scope seam (`backend/scope.py`) that a
commercial pack fills to serve many users from one deployment — that is how
the hosted tier works, and it changes nothing here. See
[docs/BUSINESS-MODEL.md](docs/BUSINESS-MODEL.md).

## Email delivery

Set `SERIN_SMTP_HOST`, `SERIN_SMTP_USERNAME`, `SERIN_SMTP_PASSWORD`, and
`SERIN_EMAIL_TO` in `.env` (see `.env.example` for a Gmail app-password
example), then restart Serin. You get:

- An **Email** button on every completed briefing in the reader.
- A toggle in the Morning Schedule card to auto-email each scheduled run.
- Plain stdlib SMTP — Gmail, Resend, SendGrid, Mailgun, or any relay works.
  Email failures on scheduled runs are logged and never block the briefing
  itself; the briefing always remains in the app.

## Run

```bash
npm run app:local
```

Open `http://127.0.0.1:5174`.

Backend runs on `http://127.0.0.1:8890`.

For a detached background server:

```bash
npm run start:local
npm run stop:local
```

## Test

```bash
pip install -r requirements-dev.txt
ruff check backend tests
npm run build
pytest
```

## Contributing & license

- **License:** [AGPLv3](LICENSE). Self-hosting is free forever, for any use.
- **Contributing:** connectors are wide open; core changes start with a
  Discussion. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution
  surface policy and [CLA.md](CLA.md) (signed by PR comment; you keep your
  copyright, and signing is never required to use Serin or ship out-of-tree
  plugins).
- **Security:** private disclosure via [SECURITY.md](SECURITY.md).
- **Privacy:** no telemetry, ever — [docs/PRIVACY-POLICY.md](docs/PRIVACY-POLICY.md).
