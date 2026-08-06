# Serin Privacy Policy

*Effective 2026-07-02 · applies to the Serin web app and the Serin mobile app*

## The short version

Serin is open-source software you run yourself. **When you self-host, we run
no servers and collect nothing** — your portfolio lives in a database on
hardware you control, and the mobile app is a client for *your* server only.
That is the default and it is free forever.

**Serin Cloud is different, and this policy says so plainly.** If you buy the
hosted plan, we operate the server and your data sits in a database we
administer, under an account you sign in to. Everything below distinguishes
the two.

## What the apps store, and where

| Data | Location | Notes |
|---|---|---|
| Positions, transactions, accounts, briefings | SQLite on your server (self-host) or our managed Postgres (Cloud) | self-host: never leaves your infrastructure by default |
| Provider API keys, broker credentials | Your server, AES-256-GCM encrypted at rest | see SECURITY.md |
| Mobile: server URL + access token | Device Keychain/Keystore (SecureStore) | removable in Settings |
| Mobile: last portfolio snapshot | Device local storage | for offline display; cleared by reinstall |

## Network connections your server makes (all optional, all user-configured)

- Market-data providers (Yahoo Finance, Financial Modeling Prep, CoinGecko):
  **symbols only** — never quantities or values.
- FX rates (open.er-api.com): currency codes only.
- SnapTrade (if you connect a brokerage): read-only holdings sync under
  SnapTrade's own terms; Serin cannot place trades.
- AI provider (**DeepSeek** by default, or Anthropic — if you enable briefings
  or Smart Import): your portfolio snapshot / uploaded statement image is sent
  to the provider *you* configured with *your* key, when you run an import or a
  briefing. If you schedule briefings, that happens on your schedule without
  further prompting. Self-hosting, you choose the provider; change it under
  Connectors → AI daily briefing.

  DeepSeek is operated from China and its terms and jurisdiction are its own,
  not ours. If you would rather your holdings were not processed there,
  configure an Anthropic key instead — Serin uses whichever you set, and the
  briefing screen names the provider it is about to use before every run.
- Expo push relay (if you enable notifications): a device push token and the
  words "Serin briefing ready" plus a one-line summary transit Expo's
  delivery service.

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
- Revoke mobile access: change `SERIN_AUTH_PASSWORD` (self-host) or change
  your password (Cloud) — either rotates every outstanding token.
- Close a Cloud account and have its data deleted: email us and we will action
  it.
- Questions / issues: open a GitHub issue on the Serin repository.

When you self-host, **you are the data controller** for everything in your
instance and we are not involved. On Serin Cloud we are the data controller
for your account and the portfolio data you store with us.
