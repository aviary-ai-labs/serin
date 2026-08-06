# Contributing to Serin

Serin's north star: **the most extensible open-source portfolio tracker —
connect any broker, any data source, any format, through a connector you can
configure or write yourself.** The highest-impact contribution is a connector.

## Write a connector (the good first issue)

1. Copy `backend/connectors/market_data/_template.py` to
   `backend/connectors/market_data/<yourprovider>.py`.
2. Rename the class, fill in the manifest (unique `id`), implement
   `refresh_prices` / `fetch_history` / `quote` / `test`.
3. Register it with one import line in `backend/connectors/__init__.py`.
4. Copy the test pattern from `tests/test_connector_template.py` (see
   `tests/test_coingecko.py` for a worked example with mocked HTTP).
5. Add a `### <yourprovider> — <Name>` section to `docs/CONNECTORS.md` so the
   in-app Docs button works.

One module, one import line, one test file, one docs section — that's a PR.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt ruff pytest
uvicorn backend.main:app --port 8890          # backend
cd frontend && npm install && npm run dev      # web UI
cd mobile && npm install && npx expo start     # mobile (optional)
```

## Quality gates (CI enforces these)

- `ruff check backend tests` — lint, import order, bugbear
- `pytest -q` — the full suite must stay green; new behavior needs tests
- `cd frontend && npm run build` — the web bundle must build
- `cd mobile && npx tsc --noEmit` — the app must typecheck

## Ground rules

- **No telemetry, ever.** Outbound calls only to user-configured providers.
- **Read-only brokerage access.** No order-placement code paths.
- **Secrets never in plaintext at rest** — mark config fields `secret=True`
  and the registry encrypts them for you.
- One PR = one shippable change. Update `CHANGELOG.md` under *Unreleased*.

## License & CLA

The core is **AGPLv3** and everything in this repo stays that way — the
free/paid line is written down in [docs/BUSINESS-MODEL.md](docs/BUSINESS-MODEL.md)
(short version: the open-source product is complete; paid layers are hosted
operations and a separate closed-source add-on pack; features never move
from free to paid).

To keep that model legally workable, contributions require a **Contributor
License Agreement**: you keep your copyright and license your contribution
to the project maintainer with the right to relicense (this is what lets the
maintainer ship commercial builds that include the core, while every fork
remains bound by the AGPL). Using, self-hosting, or writing private plugins
for Serin never requires the CLA — only contributing code to this repo does.
A CLA bot will prompt on your first PR.

Prefer shipping a connector without the CLA? Use the out-of-tree plugin path
(`SERIN_PLUGINS_DIR`, see `backend/plugins.py`) and license your plugin
however you like.

## What we merge (and what we'll redirect)

- **Wide open — please PR these:** connectors, bug fixes, docs, tests,
  translations, mobile/web polish. This is the heart of the project.
- **Proposal first (open a Discussion before coding):** schema changes,
  analytics math, SDK surface changes — core stays deliberately small and
  maintainer-curated.
- **Commercial boundary:** features on the paid side of the line in
  [docs/BUSINESS-MODEL.md](docs/BUSINESS-MODEL.md) (hosted operations,
  managed AI, X-ray/tax/benchmark analysis, alerting) won't be merged into
  core — not because they're unwelcome, but because that line funds the
  project. If you want to build one anyway, ship it as an out-of-tree
  plugin under any license you like; we'll happily link to it. Saying this
  up front beats rejecting your finished PR later.
