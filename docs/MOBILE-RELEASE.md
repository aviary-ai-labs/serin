# Serin Mobile — Release Guide (iOS + Android)

The Expo app in `/mobile` is store-ready code-wise. What remains is
account-holder work: Apple/Google enrollment, EAS credentials, and the store
listings. This guide is the press-the-button checklist.

## What's already in place

- Feature parity: dashboard (offline-capable), position detail + charts,
  add/edit, camera Smart Import, briefings reader, connectors status.
- Security: QR pairing (URL + bearer token), biometric app lock, SecureStore
  for credentials.
- Push: Expo push registration (`/api/v1/push/register`) and backend sends on
  scheduled-briefing completion.
- Config: `app.json` (bundle ids `money.serin.app`, permission strings, version
  0.8.0), `eas.json` (development / preview / production profiles).
- CI: `npx tsc --noEmit` runs on every PR.

## One-time setup (accounts you must own)

1. **Apple Developer Program** — $99/yr, developer.apple.com. Enroll the
   Apple ID that will own the app.
2. **Google Play Console** — $25 one-time, play.google.com/console.
3. **Expo account + EAS** — free tier is fine to start: `npm i -g eas-cli`,
   `eas login`, then in `/mobile`: `eas init` (writes the EAS `projectId`
   into `app.json` → `extra.eas.projectId`; push tokens need it in
   standalone builds).

## Build & submit

```bash
cd mobile

# Internal test build (installable via QR, no stores):
eas build --profile preview --platform all

# Store builds:
eas build --profile production --platform ios
eas build --profile production --platform android

# Submission (wizards handle certs/keys on first run):
eas submit --platform ios        # → App Store Connect / TestFlight
eas submit --platform android    # → Play Console internal track
```

Push credentials: `eas credentials` → enable Push Notifications (APNs key is
generated for you; FCM is configured from the same wizard for Android).

## Beta rollout

- **iOS**: the production build lands in TestFlight automatically after
  `eas submit`; add internal testers (up to 100, instant) then external
  testers (Apple review, ~1 day).
- **Android**: Play Console → Internal testing track → add tester emails →
  promote to Closed/Open testing when ready.

## Store listing checklist

| Item | Value / where |
|---|---|
| App name | Serin — Portfolio Tracker |
| Category | Finance |
| Privacy policy URL | host `docs/PRIVACY-POLICY.md` (repo Pages works) |
| Apple privacy labels | **Data not collected** — the app talks only to the user's own server (see PRIVACY-POLICY.md rationale) |
| Play Data safety | Same: no collection, no sharing; data stays on user infrastructure |
| Encryption compliance | `ITSAppUsesNonExemptEncryption=false` already set (HTTPS only) |
| Review notes | "This app is a client for the user's own self-hosted server (open source). Reviewer setup: run `docker compose up` from the repo, or use the demo video. No account we can provide — the user *is* the server." |

The "bring your own server" model is accepted on both stores (cf. Home
Assistant, Nextcloud, Plex) — the review notes above preempt the usual
"how do we log in?" rejection.

## Version bumps

`app.json` `version` tracks the repo release (0.8.0). `eas build --profile
production` auto-increments build numbers (`autoIncrement: true`).
