# Changelog

All notable changes to Serin. Format: [Keep a Changelog](https://keepachangelog.com);
versioning: [SemVer](https://semver.org) (pre-1.0: minor bumps may break).

## [0.10.0] — 2026-09-06

### Removed
- **The Expo mobile app (`mobile/`) is no longer developed in this repo.** It
  became a Serin Cloud client — it no longer connects to a self-hosted
  instance — so shipping its source next to the self-hosted server was
  misleading about what it does. Versions published here before this change
  remain AGPLv3 and forkable; they are in this repository's history and that
  grant is irrevocable. Self-hosting is unaffected: the web UI is responsive
  and a phone browser is a first-class client. `GET /api/pairing` and the
  Connectors → "Pair mobile app" QR go with it, having no remaining caller.

### Added
- **Serin is an MCP server.** A read-only agent tool layer (`backend/tools/`)
  exposed three ways: a stdio MCP server (`python -m backend.mcp_server`), a
  plain-HTTP surface under `/api/agent` that FastAPI publishes in
  `/openapi.json`, and `GET /api/agent/context.md` for agents with no tool
  support. Eight tools return *computed* answers — real TWR/XIRR, FIFO-matched
  realised gains, tax lots, data-quality gaps — rather than rows for a model to
  add up, and every tool that reads prices reports how stale they are. The
  registry refuses to register a tool that is not read-only, so an agent cannot
  mutate a portfolio. The MCP server implements the protocol directly over
  newline-delimited JSON-RPC; no new dependency.
- **Agent tokens** (`backend/agent_tokens.py`, Connectors → Agent access):
  named, individually revocable credentials, stored hashed and shown once,
  scoped to `/api/agent` and nothing else. The app lock's single deterministic
  token was the only credential before this — it carries full write access and
  revoking it signs out every device. Refused on multi-user deployments, where
  a bare token carries no identity.
- **Chat renderer** — a pack-driven chat surface on the X-ray pattern: core
  ships the renderer, the Intelligence pack ships the agent loop. Without the
  pack there is no tab and no trace.
- **Remote MCP** (`POST /api/agent/mcp`) — MCP over HTTP, one JSON-RPC message
  per request, stateless so it survives a restart, a second worker and a load
  balancer. Agent tokens now work on a shared deployment: a token names the
  account that issued it (`serin_at_<account>.<secret>`) and every request runs
  bound to that account. The owner half is not a secret and cannot be
  retargeted — swapping it looks up the other account's hashes, which will not
  match. Tokens issued before this format resolve to the single-user scope, so
  self-hosted setups keep working.
- **Briefing generator seam** (`briefings.set_generator`) — lets a commercial
  pack produce richer briefing prose. Additive and fail-safe by design: core
  keeps its own complete implementation and uses it when nothing is installed,
  when the generator *declines* (returns `None`, the ordinary path for a lapsed
  licence, not logged), or when it raises (a fault, logged). AI briefings stay
  free forever; a pack can only add depth, never take the briefing away.

- **Deterministic broker CSV import** (`backend/broker_csv.py`), starting with
  Robinhood's account activity report: parsed exactly on the server, no AI, no
  size limit. Unrecognised transaction codes are reported rather than guessed
  at. See `docs/BROKER-EXPORTS.md`, served at `/exports`.

### Fixed
- **Share splits in reconstructed history.** Price series are split-adjusted at
  source, so trades — not the running share count — needed restating into
  today's units; a pre-split buy previously left a phantom holding standing in
  every day before it.

## [0.9.0] — 2026-07-03

Open-core foundation — the free product is complete; paid layers stay
out-of-tree.

### Added
- **Plugin loader** (`SERIN_PLUGINS_DIR`): load private out-of-tree plugins —
  the same seam future paid add-ons use. Per-plugin error isolation; a broken
  plugin never takes down the core.
- **Entitlements scaffold** (`/api/entitlements`): reports the active plan
  (defaults to `opensource`); a crashing or absent verifier always fails open
  to the full free product. The Connectors portal shows a plan chip when a
  paid pack is active.
- **CLA infrastructure**: `CLA.md` (individual CLA v1.0, sign by PR comment,
  contributors keep copyright), CLA-assistant GitHub Action, PR template with
  the contribution-surface checklist.
- **Pricing page draft** at `/pricing` (noindex, unlinked until paid tiers
  ship) with the one-way-door pledge front and center.
- `docs/BUSINESS-MODEL.md`: the full open-core strategy — free/paid line,
  licensing mechanics, pricing, roadmap.

### Changed
- README aligned with open-core positioning + current feature set (real
  returns, multi-currency, Smart Import, mobile, backup, app lock).
- `pyproject.toml` version drift fixed (0.7.0 → unified with app version).

## [0.8.0] — 2026-07-02

Production + native readiness push.

### Added
- **App lock**: optional `SERIN_AUTH_PASSWORD` — passphrase login for the web
  UI, bearer token for API/mobile; unset = open self-host default.
- **Versioned DB migrations** (`schema_version` table) replacing ad-hoc ALTERs.
- **Backup & restore**: one-click JSON export/import + positions CSV export
  (Data panel on the Connectors tab).
- **Structured request logging** (`SERIN_LOG_FORMAT=json`) — method, path,
  status, duration; no request bodies, no telemetry.
- **API journey smoke test** covering import → refresh → analytics → backup.
- **Mobile feature parity** (Expo): positions list + detail with sparklines,
  add/edit position, camera/library **Smart Import**, briefings reader,
  connectors status, offline snapshot cache with last-synced banner, QR
  pairing, Face ID/biometric app lock, briefing-ready push notifications
  (Expo push), dark mode, EAS build profiles + store-readiness docs.
- **Governance**: AGPLv3 `LICENSE`, `SECURITY.md` (threat model),
  `CONTRIBUTING.md`, this changelog, GitHub Actions CI (ruff + pytest +
  frontend build + mobile typecheck).

### Changed
- Dockerfile hardened: non-root user, pinned base images, healthcheck.
- Version unified across backend/`/api/v1/version`/web/mobile.

## [0.7.0] — 2026-07-02

Finish-the-board push — all 12 open S/M items, production grade (177 tests).

### Added
- CoinGecko connector with `asset_scope="crypto"` layered routing.
- Multi-currency: per-position currency, FX cache, display-currency selector.
- Real TWR + MWR (Modified Dietz + XIRR) from the transactions log.
- Briefing cost guard (`/api/briefings/estimate`) + transaction-aware briefings.
- SnapTrade: portal credentials, transaction backfill, daily auto-sync.
- In-app connector docs + `_template.py` contributor starter.
- Secrets encrypted at rest (AES-256-GCM envelope + startup migration).
- Yahoo query1→query2 failover with backoff.

## [0.6.0] — 2026-06-30 → 07-01

- Smart Import (AI extraction from CSV/images/text, multi-file, manual form).
- Price-history cache with freshness skip + `refresh` override.
- Portal-aware AI provider/key resolution everywhere; FMP crypto symbol fix.
- Calm Dashboard UI; Stocks tab → Holdings Explorer.

## [0.5.0] — 2026-06-23

- Docker packaging; transactions table; accounts; Expo scaffold; role charters.

## [0.4.0] — 2026-06-23

- Connector platform pivot: SDK, registry, portal UI, Yahoo/FMP/SnapTrade/CSV
  connectors, AI briefing as opt-in insight connector.

## [0.3.0] — 2026-06-21

- Yahoo provider, analytics module, quote endpoints, PWA shell, stock detail.
