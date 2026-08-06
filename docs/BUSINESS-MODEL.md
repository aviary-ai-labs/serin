# Serin Business Model — Open-Core

*Adopted 2026-07-02. Supersedes the 2026-06-23 "no commercialization" stance.*

## One paragraph

Serin is **open-core**: everything a single self-hoster needs to track their
portfolio is AGPLv3 and free forever — that is the community contract. Revenue
comes from three proprietary layers that sit *on top of* the core, never
inside it: **Serin Cloud** (we run it for you), **Serin Intelligence** (a
closed-source insight pack: managed AI, advanced analytics, alerting), and
eventually **Serin for Advisors** (multi-portfolio). The edge is structural,
not obscurity: copyright ownership + CLA, the trademark, operator API
contracts, managed-AI margin, and hosted operations are things a fork cannot
copy even with full source access.

---

## 1. The line — what's free vs. what's paid

**The pledge (one-way door):** anything already shipped in the open-source
repo stays open-source. Features never migrate from free to paid. The paid
layer is *additive* — new capabilities that are naturally services or
institutional-grade analysis.

### Free forever (AGPLv3, the current v0.8 repo)

| Area | Included |
|---|---|
| Tracking | Positions, accounts, transactions, tax lots, multi-currency, price cache |
| Connectors | Yahoo, FMP (BYO key), CoinGecko, SnapTrade (BYO operator keys), CSV, **the whole connector SDK + external plugin loading** |
| Intelligence (BYO keys) | AI briefings, Smart Import — you bring your Anthropic/DeepSeek key and pay the provider directly |
| Analytics | Real TWR / MWR (Modified Dietz + XIRR), period returns, allocation |
| Apps | Web UI, PWA, the entire Expo mobile app source |
| Ops | Docker, backup/restore, app lock, secrets-at-rest, migrations |

Rationale: this is table stakes vs. Ghostfolio and the credibility engine for
the whole model. Crippling it would kill the funnel *and* the community.

### Paid — the three revenue layers

**Layer 1 · Serin Cloud — hosted SaaS.** *"docker compose up, but it's our
problem."* Managed instance, TLS, automatic encrypted backups, upgrades,
uptime, multi-device access, **bundled broker connectivity** (our SnapTrade
operator contract — self-hosters must obtain their own) and bundled market
data (our FMP tier). Target **$8/mo or $80/yr**. This monetizes convenience
and *our* API contracts, not withheld code.

**Layer 2 · Serin Intelligence — the closed-source insight pack.** A licensed
plugin (separate private repo) that loads into core — self-host *or* Cloud —
via the OSS plugin loader + entitlement key:

- **Managed AI**: briefings + Smart Import with zero API-key setup; metered
  through our proxy with margin (COGS today: $0.001–0.04/run → gross margin
  >70% at any realistic usage).
- **Premium briefings**: multi-day memory, earnings-calendar awareness,
  position-aware news ranking; the prompt/eval pipeline stays private IP.
- **X-ray reports**: concentration, factor/sector risk, fee drag, overlap
  across accounts.
- **Tax intelligence**: loss-harvesting candidates with wash-sale windows,
  lot-level what-if on sales.
- **Benchmarks & projections**: vs. SPY/ACWI/custom, Monte Carlo drawdown and
  retirement projections.
- **Alerting**: price/dividend/risk rules → push + email.

Target **$6/mo or $60/yr** standalone; **bundled free inside Cloud** to make
Cloud the default choice.

**Layer 3 · Serin for Advisors (later).** Multi-portfolio, client read-only
share links, white-label reports, team seats. **$29+/seat/mo.** Gated on the
multi-user + Postgres milestone; do not build before Layers 1–2 have revenue.

### Explicitly never paid
Data export, backup/restore, the connector SDK, security features (app lock,
encryption). Charging for data freedom or safety would poison the model.

---

## 2. Why the edge survives full source access (moat analysis)

1. **Copyright + CLA (the legal moat).** The AGPLv3 core is single-copyright
   (sole author; CONTRIBUTING now requires a CLA assigning relicensing
   rights). That means: *we* may combine the core with proprietary modules;
   a competitor may not — their derivative work, including any proprietary
   layer that links our core, must be AGPLv3-published. AGPL is the
   open-core operator's best friend: it forces competing hosts to publish
   their secret sauce while ours stays private.
2. **Trademark.** "Serin" the name and mark are reserved (register early —
   see execution plan). Forks must rebrand, losing the funnel.
3. **Operator contracts.** SnapTrade/Plaid/FMP operator keys are commercial
   agreements. OSS users BYO keys (free tier exists); Cloud bundles ours.
   A fork must sign and pay for its own. **Trust rule:** a *hosted* Serin holds
   **no raw broker/exchange secrets** — only revocable tokens the user grants
   on the provider's own login (OAuth / Plaid / SnapTrade portal); raw API keys
   are a self-host / advanced capability only. Users judge "connect" on instinct
   in seconds, so this is a conversion lever, not just security — and it sorts
   the tiers cleanly (free self-host = BYO key/import; paid = Connect-only, the
   aggregator fee being what the subscription funds). Full model + per-connector
   classification: [CONNECTOR-TRUST.md](CONNECTOR-TRUST.md).
4. **Managed-AI pipeline.** Prompts, evaluators, per-user memory, and cost
   routing live in the private pack. The OSS briefing is good; the paid one
   compounds with private eval data.
5. **Operations.** Backups, upgrades, on-call. Boring, real, un-forkable.
6. **Distribution.** App Store / Play listings (our accounts), the website,
   the community. Home Assistant/Bitwarden/Grafana all prove this holds.

**Threat honestly assessed:** a large fork community rejecting the CLA. Reply:
the pledge (free tier is genuinely complete), the tiny CLA scope (only
needed to *contribute*, never to use), and velocity.

---

## 3. Licensing mechanics

- **Core repo:** stays AGPLv3. No license change needed — this is the rare
  case where the strictest copyleft *is* the commercial strategy.
- **Contributions:** CLA (Apache-style ICLA) granting the maintainer the
  right to relicense contributions. Enforced via PR checkbox/bot before
  merge. Existing code: 100% maintainer-authored, no back-CLA needed.
- **`serin-pro` (private repo):** proprietary EULA, delivered as a Python
  package that registers connectors/insight modules through the public SDK
  (`SERIN_PLUGINS_DIR`). It may import core because the copyright holder is
  not bound by their own AGPL grant. Third parties writing plugins for their
  own use are fine under AGPL §13 (they're not conveying); *distributing* a
  proprietary plugin derived from core is not — which is exactly the fence
  we want.
- **Entitlements:** OSS core ships a neutral `entitlements` module (empty by
  default, feature checks return False). The pro pack installs a signed
  license-key verifier at load time. The core never phones home; license
  verification is offline (Ed25519-signed keys) with a Cloud-side issuance
  API. No kill switches: an expired key degrades to OSS behavior, nothing
  else.
- **Mobile:** the app stays OSS and free in stores; Pro features light up
  when the *server* reports entitlements (server-side unlock — the app reads
  state from your own backend, which App Store review treats like any
  self-hosted client; no IAP required for server-purchased plans, cf.
  Home Assistant Cloud/Nabu Casa precedent).

---

## 4. Pricing & unit economics

| Plan | Price | COGS/user/mo (est.) | Notes |
|---|---|---|---|
| OSS self-host | $0 | $0 | funnel + community |
| Intelligence | $6/mo · $60/yr | ~$0.60–1.50 (AI usage) | works on self-host |
| Cloud (incl. Intelligence) | $8/mo · $80/yr | ~$2.50 (compute ~$0.60, SnapTrade seat ~$1–1.5, AI, backups) | anchor plan |
| Advisors | $29/seat/mo | — | phase 3 |

Break-even math: at $8/mo Cloud with ~$2.5 COGS → ~$5.5 gross/user/mo.
1,000 Cloud users ≈ $66k ARR gross — a sustainable solo business at small
scale, credible wedge for more.

**Conversion assumptions (validate, don't trust):** OSS→paid 1–3% is the
open-core norm; the design lever is making *Cloud* the easiest onboarding
path (the landing page's `docker compose up` stays, but "Try Serin Cloud"
becomes the primary CTA once Cloud exists).

---

## 5. Execution roadmap (monetization track)

| # | Milestone | Gate |
|---|---|---|
| M1 | ✅ Plugin loader (`SERIN_PLUGINS_DIR`) + entitlements scaffold in core | shipped with this doc |
| M2 | CLA gate on PRs (cla-assistant), trademark filing, pricing page draft | before any paid launch |
| M3 | `serin-pro` private repo: managed-AI proxy + one killer feature (X-ray report) + signed license keys | first revenue (Intelligence, self-host) |
| M4 | Serin Cloud alpha: shared multi-user deployment (accounts + managed Postgres), Stripe billing, bundled SnapTrade | per-customer containers were the original plan and were built, but carried a large fixed setup cost and linear infra spend before the first subscriber; one shared deployment with database-enforced per-account isolation replaced it |
| M5 | Store apps live (free) with server-side Pro unlock | after M3 |
| M6 | Advisors tier | after Cloud retention data |

The prior production/mobile roadmaps are prerequisites and are ✅ complete.

## 6. Community positioning & the announcement

Risks: "rug pull" optics after the pure-OSS positioning. Mitigations, in the
public announcement (pin as a GitHub Discussion):

1. The pledge, verbatim: *"Everything in the repo today stays AGPL and free.
   Paid = hosted ops + a closed-source add-on pack. Features will never move
   from free to paid."*
2. The line is *services and institutional analysis*, not basics.
3. The CLA asks contributors only for relicensing rights, and the connector
   SDK + plugin loader mean anyone can ship their own plugins — including
   commercial ones for their own use.
4. Health metrics stay community-first: connectors and contributors remain
   the North-Star metric; revenue funds maintenance (state the split:
   maintainer time ≥50% on OSS core).
