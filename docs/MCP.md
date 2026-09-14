# Connect your AI assistant to Serin

Serin speaks the **Model Context Protocol (MCP)**, so an AI assistant you
already use can read your portfolio and answer questions about it. Your data
never leaves your server, and the model runs on whatever subscription you
already pay for — Serin charges nothing for this.

## What it can and cannot do

It can read: totals and weights, real time-weighted and money-weighted returns,
FIFO-matched realised gains, individual holdings and their tax lots,
transactions, cached price history, and the data-quality gaps that would make
any of those unreliable.

It **cannot write**. There is no tool to add, edit or delete a position, and
the tool registry refuses to accept one — a hallucinated edit would corrupt
cost basis, which is the number the whole product exists to get right.

It does not give financial advice, and it is not a trading interface.

Answers come back already computed. Ask "what's my YTD return" and the model
receives the number Serin calculated, not forty rows to add up itself.

---

## Step 1 — create a token

In Serin: **Connectors → Agent access → Create token**.

Copy it immediately. Serin stores only a hash, so it cannot be shown again —
if you lose it, revoke it and make another.

A token is read-only and scoped to `/api/agent`. It cannot change anything,
cannot download a backup, and cannot create another token. Revoking one does
not sign you out anywhere.

---

## Step 2 — pick your path

Which recipe you want depends on what you have on the machine running the AI
client, not on which client it is.

| Your situation | Use |
| --- | --- |
| **Serin Cloud** | [Remote MCP](#a-remote-mcp-nothing-to-install), or [the one-file bridge](#b-the-one-file-bridge) |
| **Self-hosted with Docker** | [Docker](#c-inside-the-container), or remote MCP |
| **Self-hosted from a checkout** | [From a checkout](#d-from-a-checkout) |

Everything below produces the same eight tools. Pick one.

### A. Remote MCP (nothing to install)

The simplest path if your client supports remote MCP servers:

```
URL:    https://your-serin/api/agent/mcp
Header: Authorization: Bearer serin_at_...
```

On Serin Cloud the URL is `https://serin.money/api/agent/mcp`.

One endpoint, one JSON-RPC message per request, stateless. Client support for
remote servers with bearer auth is still uneven — if yours cannot, use the
one-file bridge below, which works with every client.

### B. The one-file bridge

For **Serin Cloud**, or any machine with no checkout and no container. The
bridge is a thin HTTP client with no Serin dependencies, so it runs on its own:

```bash
curl -O https://raw.githubusercontent.com/aviary-ai-labs/serin/main/backend/mcp_server.py
pip install httpx
```

Then point your client at it, using an **absolute path**:

```json
{
  "mcpServers": {
    "serin": {
      "command": "python",
      "args": ["/absolute/path/to/mcp_server.py"],
      "env": {
        "SERIN_URL": "https://serin.money",
        "SERIN_AGENT_TOKEN": "serin_at_..."
      }
    }
  }
}
```

Needs Python 3.10 or newer and `httpx`, nothing else. Self-hosters: set
`SERIN_URL` to wherever you reach Serin in a browser.

### C. Inside the container

If you run Serin with `docker compose up`, you have no checkout — but the image
already contains the bridge, so run it there:

```json
{
  "mcpServers": {
    "serin": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "SERIN_URL=http://127.0.0.1:8890",
        "-e", "SERIN_AGENT_TOKEN=serin_at_...",
        "serin",
        "python", "-m", "backend.mcp_server"
      ]
    }
  }
}
```

`serin` is the container name from `docker-compose.yml`, and it must be running
when the client starts. `127.0.0.1:8890` is correct here because the command
runs *inside* the container.

### D. From a checkout

```json
{
  "mcpServers": {
    "serin": {
      "command": "python",
      "args": ["-m", "backend.mcp_server"],
      "cwd": "/path/to/your/serin/checkout",
      "env": {
        "SERIN_URL": "http://127.0.0.1:8890",
        "SERIN_AGENT_TOKEN": "serin_at_..."
      }
    }
  }
}
```

---

## Where your client keeps its config

The JSON above goes in your client's MCP config file.

**Claude Desktop**

- macOS — `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows — `%APPDATA%\Claude\claude_desktop_config.json`

Create it if it isn't there, then restart the app.

**Claude Code**

```bash
claude mcp add serin \
  -e SERIN_URL=https://serin.money \
  -e SERIN_AGENT_TOKEN=serin_at_... \
  -- python /absolute/path/to/mcp_server.py
```

Flags move between releases — `claude mcp add --help` is authoritative.

**Cursor, Cline, Zed and others** use the same `mcpServers` shape but keep it in
their own file. Copy the block above and check your client's MCP documentation
for where that lives.

## What you can ask

- *How am I doing this year?*
- *What's my biggest position, and how concentrated am I?*
- *What did I realise in 2025, and how much was short-term?*
- *Are any of my prices stale?*
- *What's missing from my data that would make these numbers wrong?*

Serin reports how fresh its prices are with every answer that uses them, so
your assistant can tell a current quote from a cached one.

---

## Not using MCP?

The same tools are plain HTTP, published in `/openapi.json` — enough for
LangChain, LlamaIndex, OpenAI function calling, or your own loop:

| Endpoint | What it does |
| --- | --- |
| `GET /api/agent/tools` | Every tool with its JSON schema |
| `POST /api/agent/tools/{name}` | Run one; arguments are the JSON body |
| `GET /api/agent/context.md` | The whole portfolio as Markdown |
| `POST /api/agent/mcp` | MCP over HTTP |

`context.md` is the zero-integration option: it works with any assistant at
all, including ones with no tool support, and you can paste it into a chat
window yourself.

---

## Troubleshooting

**The client shows no tools.** It never finished connecting. Check the client's
MCP log: a bridge that cannot import `backend.mcp_server` exits immediately.
Use the Docker recipe, or set `cwd` to a Serin checkout.

**401 Unauthorized.** `SERIN_AGENT_TOKEN` is missing, mistyped, or revoked.
Tokens are shown once — make a new one rather than guessing.

**403 Forbidden.** That credential isn't an agent token. A session token or app
passphrase will not work here; agent tokens start with `serin_at_`.

**Connection refused.** `SERIN_URL` is wrong. On Cloud it is
`https://serin.money` — with the scheme, and no trailing path. From inside the
container it is `http://127.0.0.1:8890`. From your own machine against a
self-hosted box, it is wherever you reach Serin in a browser.

**`ModuleNotFoundError: No module named 'backend'`.** You used a
checkout-shaped recipe without a checkout. Use the one-file bridge (B) or the
Docker recipe (C).

**Answers cite old prices.** That is Serin being honest — the price cache
survives a failed refresh, and every tool reports its own staleness. Run a
price refresh.

---

## On Serin Cloud (and any shared deployment)

A token names the account that issued it (`serin_at_<account>.<secret>`) and
every request runs bound to that account, so it reads your portfolio and no one
else's. The account half is not a secret; swapping it for someone else's looks
up their stored hashes, which will not match, so a token cannot be pointed at
another account.
