# Serin Mobile

Expo / React Native scaffold for the Serin mobile app. **The app is a client,
not a service** — it points at whatever Serin backend you configure (your
self-hosted box, a LAN address, a hosted instance). Same posture as
Home Assistant and Bitwarden mobile.

This is the **foundation** for the iOS/Android migration laid out in the
strategy memos: it speaks the `/api/v1/` contract the web app already exposes,
stores the backend URL + bearer token in `expo-secure-store`, and runs as a
proper native app once you install dependencies.

## Quickstart

One command from the **repo root** — backend (Intelligence pack auto-loaded
when a sibling `serin-pro/` checkout exists) plus Expo with iOS-simulator
auto-launch. Ctrl-C tears both down:

```bash
npm run dev:ios                     # backend + Metro + iOS simulator
npm run dev:ios -- --backend-only   # just the health-checked backend
```

The script prints the backend URLs to paste into the app's Settings:
`http://127.0.0.1:8890` in the simulator, `http://<lan-ip>:8890` on a phone.

Or run the pieces by hand:

```bash
cd mobile
npm install
npx expo start
```

Then press `i` (iOS simulator), `a` (Android emulator), or scan the QR with
Expo Go on a physical device. On first launch the dashboard punts you into
Settings — point it at your running Serin backend (the same one
`docker compose up` boots on port 8890), then tap **Save & test**.

## What's in here (v0.8 — store-ready)

- `app/index.tsx` — dashboard: totals, day change, positions with sparklines,
  pull-to-refresh (re-quotes server-side), **offline snapshot** with a
  last-synced banner when the network drops
- `app/position/[id].tsx` — detail: SVG area chart with period toggles,
  stats grid, edit/delete
- `app/add-position.tsx` — add/edit form (modal)
- `app/smart-import.tsx` — **camera Smart Import**: snap or pick a brokerage
  screenshot → AI extraction on your backend → mandatory review → import
- `app/briefing.tsx` — AI briefing reader
- `app/connectors.tsx` — connector platform status
- `app/settings.tsx` — **QR pairing** (scan from Serin web → Connectors →
  Data), backend URL + bearer token, **Face ID/biometric app lock**,
  **briefing push notifications**
- `src/api.ts` — typed `/api/v1` client + SecureStore creds + AsyncStorage
  offline cache; `src/push.ts` — Expo push registration; `src/theme.ts` —
  dark/light tokens; `src/Sparkline.tsx` — SVG charts

## Security posture

- Credentials live in the device Keychain/Keystore (SecureStore).
- The pairing QR carries the backend URL and (when the app lock is enabled)
  the bearer token — scan it privately.
- Optional biometric gate re-arms on every cold start.
- The app talks only to *your* backend; see `docs/PRIVACY-POLICY.md`.

## Shipping to the stores

Code and config are release-ready (`app.json`, `eas.json`, permission
strings, privacy policy). The remaining steps need your Apple/Google/Expo
accounts — follow `docs/MOBILE-RELEASE.md`.

## Quality gate

```bash
npx tsc --noEmit   # enforced in CI
```
