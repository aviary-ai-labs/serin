# Changelog

All notable changes to Serin. Format: [Keep a Changelog](https://keepachangelog.com);
versioning: [SemVer](https://semver.org) (pre-1.0: minor bumps may break).

## [Unreleased]

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
