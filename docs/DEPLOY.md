# Deploying Serin

Serin is a single container: FastAPI serves both the API and the built web
UI on one port. State is one SQLite file + one secrets key file in `/data`.

## The 60-second deploy (Docker Compose)

The only prerequisite is [Docker](https://docs.docker.com/get-docker/)
(Docker Desktop on macOS/Windows, the engine + compose plugin on Linux).

```bash
git clone https://github.com/aviary-ai-labs/serin.git && cd serin
docker compose up -d --build
```

Then open <http://localhost:8890>. That's the whole install: free Yahoo
market data works out of the box, and API keys are optional, added later in
the Connectors tab if you want them.

`docker-compose.yml` mounts a named volume at `/data` (database + secrets
key). That volume **is** your Serin — back it up.

## Environment variables that matter in production

| Var | Purpose | Recommendation |
|---|---|---|
| `SERIN_AUTH_PASSWORD` | App lock: passphrase for the web UI, bearer token for API/mobile | **Set it** for anything reachable beyond localhost |
| `SERIN_SECRET_KEY` | 32-byte base64/hex key for secrets-at-rest | Set it so the key never lives next to the DB; else `/data/.serin-key` is auto-generated |
| `SERIN_LOG_FORMAT` | `json` for structured request logs | `json` behind a log collector |
| `FMP_API_KEY`, `SNAPTRADE_*`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY` | Providers | Optional — all configurable in the portal UI instead |
| `SERIN_SMTP_*`, `SERIN_EMAIL_TO` | Briefing email delivery | Optional |

## HTTPS

Serin does not terminate TLS. Put it behind Caddy / Traefik / nginx, or reach
it over Tailscale/WireGuard (the natural fit for a personal instance):

```caddyfile
serin.example.com {
    reverse_proxy 127.0.0.1:8890
}
```

## Platform notes

- **Fly.io** — `fly launch --image` from the Dockerfile; add a 1GB volume
  mounted at `/data`; set secrets with `fly secrets set SERIN_AUTH_PASSWORD=…`.
- **Railway / Render** — deploy the Dockerfile; attach a persistent disk at
  `/data`; configure the env vars above in the dashboard.
- **Umbrel / CasaOS / TrueNAS** — generic Docker app: image from this repo,
  port 8890, volume `/data`. Pair the mobile app over your LAN or Tailscale.

## Backup & restore

- **In-app**: Connectors tab → Data panel → *Download backup* (full JSON) and
  *Positions CSV*. Restore uploads the same JSON.
- **File-level**: snapshot the `/data` volume. Keep `.serin-key` (or
  `SERIN_SECRET_KEY`) separate from DB backups if you want leaked backups to
  be useless (see SECURITY.md).

## Upgrades

```bash
git pull && docker compose up -d --build
```

Schema migrations run automatically on startup (`schema_version` table) and
are additive; downgrading is not supported — restore a backup instead.

## Health & monitoring

- `GET /api/v1/version` — liveness (used by the container healthcheck).
- Request logs: one line per request (method, path, status, ms). With
  `SERIN_LOG_FORMAT=json` each line is a JSON object for ingestion.
- No telemetry — nothing leaves the box unless you configured a provider.
