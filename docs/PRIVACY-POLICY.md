# Serin Privacy Policy

*Effective 2026-09-08 · applies to the Serin web app and the Serin mobile app*

## The short version

Serin is open-source software you run yourself. **When you self-host, we run
no servers and collect nothing** — your portfolio lives in a database on
hardware you control, and you use it through your browser. That is the
default and it is free forever. (The mobile app is a Serin Cloud client and
does not connect to a self-hosted instance, so self-hosting and the app are
never mixed.)

**Serin Cloud is different, and this policy says so plainly.** If you buy the
hosted plan, we operate the server and your data sits in a database we
administer, under an account you sign in to. Everything below distinguishes
the two.

## What the apps store, and where

| Data | Location | Notes |
|---|---|---|
| Positions, transactions, accounts, briefings | SQLite on your server (self-host) or our managed Postgres (Cloud) | self-host: never leaves your infrastructure by default |
| Provider API keys, broker credentials | Your server, AES-256-GCM encrypted at rest | see SECURITY.md |
| Agent tokens (for connecting your own AI assistant) | Your server, stored only as a SHA-256 hash | the token itself is shown once and cannot be recovered; revoke any time |
| Chat transcripts | SQLite on your server (self-host) or our managed Postgres (Cloud) | **kept 30 days, then deleted automatically.** Your questions and Serin's replies, plus the names of the tools it consulted — never the underlying tool results. Clear them yourself at any time |
| Mobile: server URL + access token | Device Keychain/Keystore (SecureStore) | cleared by signing out or editing them in Settings. Note that the OS keychain outlives the app: deleting Serin does **not** by itself remove them, so sign out first if you are handing the device on |
| Mobile: last portfolio snapshot | Device local storage | for offline display; cleared by signing out or deleting the app |

## Network connections your server makes (all optional, all user-configured)

- Market-data providers (Yahoo Finance, Financial Modeling Prep, CoinGecko):
  **symbols only** — never quantities or values.
- FX rates (open.er-api.com): currency codes only.
- SnapTrade (if you connect a brokerage): read-only holdings sync under
  SnapTrade's own terms; Serin cannot place trades.
- AI provider (**DeepSeek** by default, or Anthropic — if you use briefings,
  Smart Import, or chat). What is sent depends on which one you use:

  - **Briefings** send a portfolio snapshot when a briefing runs — on your
    schedule, without further prompting, if you set one.
  - **Smart Import** sends the statement, screenshot or file you upload.
  - **Chat** sends your question, and whatever portfolio figures answering it
    required, once for every message you send.

  **Whose key, and what we can see.** Self-hosting with your own provider key,
  the request goes from your server to the provider you configured and never
  touches us. On Serin Cloud — and on a self-hosted instance using managed AI
  rather than its own key — the request goes to Anthropic through a proxy we
  operate, using our key. That proxy records how many tokens each account used,
  so we can meter the plan; it does not store the content of your requests, and
  we do not log them.

  DeepSeek is operated from China and its terms and jurisdiction are its own,
  not ours. If you would rather your holdings were not processed there,
  configure an Anthropic key instead — Serin uses whichever you set, and the
  briefing screen names the provider it is about to use before every run.
- Expo push relay (if you enable notifications): a device push token and the
  words "Serin briefing ready" plus a one-line summary transit Expo's
  delivery service.

## Connecting your own AI assistant (MCP)

Serin can be read by an AI assistant you already use — Claude Desktop, Claude
Code, or anything that speaks the Model Context Protocol. This is the one place
data leaves on *your* instruction rather than Serin's, so it is worth being
exact about.

You create an agent token, you paste it into a client you chose, and that
client can then read your portfolio: holdings, returns, realised gains,
transactions, price history. Whatever that client sends to *its* AI provider is
between you and them — we are not in that path and cannot see it, and its
provider's policy governs it, not ours.

Agent tokens are read-only. They cannot change a position, download a backup,
or create another token, and they reach only the `/api/agent` endpoints. Revoke
one at any time under Connectors → Agent access; revoking is immediate and does
not sign you out anywhere else.

Serin does not store your conversations with an assistant connected this way —
they happen inside your client, not inside Serin.

**Chat inside Serin is different, and is stored.** So that a conversation
survives a reload, your questions and Serin's replies are saved on the same
server as the rest of your data — yours when you self-host, ours on Cloud.
They are **kept for 30 days and then deleted automatically**, you can clear
them yourself at any time from the chat window, they are included in your
export, and they are deleted with your account. What is saved is what was on
your screen: the text of each turn and the names of the tools consulted, never
the portfolio figures those tools returned.

We treat a transcript as at least as private as the portfolio itself — it
records what you *asked*, not only what you hold — so the same rule applies:
our staff do not read it.

## What we (the Serin project) receive

**Self-host: nothing.** No telemetry, no analytics, no crash reporting, no
accounts.

**Serin Cloud:** your email address and a password we store only as a scrypt
hash — never in a form we can read. Your portfolio data is stored on our
infrastructure so we can serve it back to you. Payment is handled by Stripe;
we never see your card details.

### How Cloud accounts are separated

Cloud customers share one database rather than each getting a private machine.
Rows carry an owner, every query filters on it, and PostgreSQL row-level
security enforces the same rule underneath — so a query that forgot to filter
returns nothing rather than someone else's holdings. We think that is the
honest way to describe it: strong separation inside shared infrastructure, not
physical isolation. If you want your data on hardware nobody else touches,
self-host — that option stays free forever.

Our staff do not read customer portfolios. Access to production is limited to
what is needed to operate and support the service.

## Your controls

- Export or delete everything: Connectors → Data → backup/restore, or delete
  the database file. On Cloud the same export works, including after you
  cancel — a lapsed subscription suspends access, it never deletes your data.
- Sign out of the mobile app: Settings → Sign out, which removes the access
  token and the cached portfolio from that device.
- Revoke access everywhere at once: change `SERIN_AUTH_PASSWORD` (self-host)
  or change your password (Cloud) — either rotates every outstanding token,
  including ones on devices you no longer have.
- Revoke an AI assistant's access: Connectors → Agent access → Revoke. This
  affects that token only and nothing else.
- Clear your chat history: the Clear history button in the chat window. It is
  deleted immediately rather than at the end of the 30-day window.
- Close a Cloud account and have its data deleted: email us and we will action
  it.
- Questions / issues: open a GitHub issue on the Serin repository.

When you self-host, **you are the data controller** for everything in your
instance and we are not involved. On Serin Cloud we are the data controller
for your account and the portfolio data you store with us.
