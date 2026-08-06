# Connector trust model

> The connect moment is decided in ~2 seconds, on instinct, before anyone
> reads a line of code. "Paste your API secret into this startup's form" fails
> that test no matter how good the encryption is. So we design connectors
> around **what the user is handing over**, and make the safe paths the
> default — never lead with a key.

## The one rule

**On any hosted (paid) Serin, we hold no raw broker/exchange secrets — ever.**
Hosted connections use only *revocable, scoped tokens* obtained through a flow
where the user authenticates on the **provider's own site**. Raw API keys are a
**self-host / advanced** capability only.

Encryption answers *"can they be breached?"*. The trust question users actually
ask is *"why does this app want my secret key at all?"* — and the honest answer
is *it shouldn't need it.* This rule makes that true.

## Three connection postures

Ordered by how much the user hands over — and how safe it *feels*:

| Posture | What you hand over | Feels safe because | Hosted? |
| --- | --- | --- | --- |
| **Connect** (OAuth / aggregator) | A revocable token, granted on the **broker's own login screen** | You never gave us a credential; you can revoke it at the source anytime. Same instinct as "Sign in with Google." | ✅ default |
| **Import** (file / snapshot) | A **statement** (CSV / OFX / screenshot) — no standing access | Nothing is "connected"; there's nothing to breach or revoke. | ✅ |
| **API key** (advanced) | A **raw read-only secret** you copied out of the broker | …it doesn't, really. Fine when *you* host it on *your* machine. | 🚫 self-host only |

The portal leads with **Connect · Import**, and tucks **API key** behind an
"Advanced" disclosure most users never open.

## Per-connector classification

| Connector | Posture | Notes |
| --- | --- | --- |
| SnapTrade | **Connect** | User authorizes at the broker via SnapTrade's Connection Portal. **Positioned as the self-host aggregator** (BYO operator keys, free for one connected user). Cloud's primary Connect is Plaid; SnapTrade stays a self-host option. |
| Plaid (planned) | **Connect** | Plaid Link — bank/broker login on Plaid's screen. Cloud-bundled. |
| Coinbase | **API key** today → **Connect** on hosted (Coinbase OAuth) | Coinbase offers OAuth; prefer it on Cloud. |
| Binance | **API key** | Binance retail has no OAuth → hosted = aggregator or import only. |
| Generic CSV / OFX | **Import** | No credential at all. |
| Smart Import (screenshot/PDF) | **Import** | The most universal trust-safe path. |
| On-chain wallet (planned) | **Import-like** | User pastes a **public** address — not a secret. Trust-safe with no OAuth deal. |

## Why this also is the right business structure

- **Free self-host** → BYO key / import. You trust your own machine; costs us
  nothing per user.
- **Paid** → Connect (OAuth + aggregator) only; the user never sees a key. The
  per-connection cost (SnapTrade ~$1–1.5/user, Plaid per-item) is exactly what
  the subscription funds — already in the Cloud unit economics.

Nothing built is wasted: the API-key connectors (Coinbase, Binance) become the
self-host/advanced tier; the paid connectivity story is Connect-only.

## The hurdle (why Connect flows arrive with Cloud, not before)

The trust-safe **Connect** connectors gate on commercial work that the
BYO-key/import paths do not:

1. **Business verification + legal.** Aggregators onboard a real company
   (Aviary AI Labs): business details, a privacy policy (we have one), commercial
   ToS, and a Data Processing Agreement — you become a data processor.
2. **Application review.** Plaid in particular **reviews production apps** (use
   case, data handling, security questionnaire); Investments is a premium
   product with extra approval. Days-to-weeks, not a switch. Coinbase/most OAuth
   apps also review sensitive scopes.
3. **Per-connection cost + minimums.** Aggregators bill per connected
   account/item/user, sometimes with monthly minimums that bite pre-revenue.
   This is *why* these connectors are paid-tier only — a free user connecting via
   Plaid would cost real money with no revenue.
4. **Hosted-only by construction.** OAuth needs registered redirect URIs
   (`https://serin.money/oauth/callback`) and an app client secret we can't hand
   to self-hosters. So Connect is inherently a hosted capability; self-hosters
   who want an aggregator bring their own operator keys (already supported).
5. **Ongoing custody + maintenance.** Holding tokens = token refresh, broker
   API-change upkeep, connection-health support, and breach-notification
   obligations. Aggregators reduce but don't remove this.

**Sequencing consequence:** the free/self-host product ships **now** on
Import + BYO-key. **Intelligence** (M3) needs *no* key custody — it runs on the
portfolio snapshot — so it can be first revenue *before* the aggregator deals
land. The Connect connectivity belongs to **Cloud (M4)**, once the agreements
and reviews are in place.
