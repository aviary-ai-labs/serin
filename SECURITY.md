# Security Policy

Serin is a self-hosted, single-user portfolio tracker. This document explains
what Serin protects, what it deliberately does not, and how to report issues.

## Reporting a vulnerability

Open a **GitHub security advisory** (Security → Report a vulnerability) or
email the maintainer privately. Please do not file public issues for
exploitable bugs. You can expect an acknowledgement within a week; fixes ship
as patch releases with credit unless you prefer otherwise.

## Threat model

**In scope — what Serin defends against:**

- **Database file leakage** (backups, copied volumes, stolen disks): connector
  secrets (broker keys, AI API keys) are AES-256-GCM encrypted at rest
  (`enc:v1:` envelope, `backend/secrets_store.py`). The data key comes from
  `SERIN_SECRET_KEY` or an auto-generated `data/.serin-key` (mode 0600) — keep
  the key out of backups to make leaked backups useless.
- **Exposed instance**: set `SERIN_AUTH_PASSWORD` to require a passphrase for
  the web UI and a bearer token for the API/mobile app. Unset (the default),
  Serin binds for localhost/self-host use and is open — do not port-forward an
  unlocked instance.
- **Trades**: brokerage access is read-only by construction (SnapTrade
  `connection_type: "read"`); Serin has no order-placement code path.
- **Telemetry**: none. Serin never phones home. Outbound traffic is limited to
  the data providers you configure (Yahoo/FMP/CoinGecko/SnapTrade), the FX
  rates endpoint, and — only when a briefing runs — your chosen AI provider.

**Out of scope — deploy-time responsibilities:**

- Full-disk encryption, TLS termination, and network isolation belong to your
  host (use a reverse proxy for HTTPS; Tailscale/WireGuard are a good fit).
- An attacker with code execution on the host, or with both the DB **and**
  the key file, can read secrets.
- Multi-user isolation: Serin is single-user by design today. Do not share an
  instance; the multi-user + Postgres milestone tracks this.

## Data flows (privacy)

| Data | Where it goes | When |
|---|---|---|
| Positions, transactions | Local SQLite only | always |
| Symbols (not quantities) | Market-data provider | price refresh / history |
| Portfolio snapshot + headlines | Your configured AI provider | only when a briefing or Smart Import runs |
| Uploaded statements/screenshots | Your configured AI provider | only via Smart Import, after an explicit user action |

## Supported versions

The latest minor release receives security fixes. Older versions: please
upgrade — migrations run automatically on startup.
