#!/usr/bin/env bash
# One command to test Serin on iOS: backend (with the Intelligence pack when
# present) + Expo with simulator auto-launch. Ctrl-C tears everything down.
#
#   npm run dev:ios              # backend + Metro + iOS simulator
#   npm run dev:ios -- --backend-only   # just the backend (health-checked)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
mkdir -p logs

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export SERIN_BACKEND_HOST="${SERIN_BACKEND_HOST:-0.0.0.0}"  # simulator + physical phone
export SERIN_BACKEND_PORT="${SERIN_BACKEND_PORT:-8890}"

# The commercial pack is a sibling checkout; load it when it's there so the
# X-ray tab is testable. Absent → plain open-source mode, which is also valid.
if [ -z "${SERIN_PLUGINS_DIR:-}" ] && [ -d "$ROOT/../serin-pro/serin_pro" ]; then
  export SERIN_PLUGINS_DIR="$(cd "$ROOT/../serin-pro" && pwd)"
fi

python_bin="python3"
if [ -x ".venv/bin/python" ]; then
  python_bin=".venv/bin/python"
fi

existing="$(lsof -ti "tcp:$SERIN_BACKEND_PORT" || true)"
if [ -n "$existing" ]; then
  echo "› replacing process on :$SERIN_BACKEND_PORT"
  echo "$existing" | xargs kill
  sleep 1
fi

nohup "$python_bin" -m uvicorn backend.main:app \
  --host "$SERIN_BACKEND_HOST" \
  --port "$SERIN_BACKEND_PORT" \
  > logs/backend.log 2>&1 &
backend_pid="$!"
echo "$backend_pid" > logs/backend.pid

cleanup() {
  if kill -0 "$backend_pid" 2>/dev/null; then
    kill "$backend_pid" 2>/dev/null || true
    echo "› backend stopped"
  fi
}
trap cleanup EXIT INT TERM

# Fail fast if the backend never comes up — with the log tail, not a mystery.
for _ in $(seq 1 40); do
  if curl -sf -m 2 "http://127.0.0.1:$SERIN_BACKEND_PORT/api/v1/version" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    echo "✗ backend exited during startup — last log lines:" >&2
    tail -n 15 logs/backend.log >&2
    exit 1
  fi
  sleep 0.5
done
if ! curl -sf -m 2 "http://127.0.0.1:$SERIN_BACKEND_PORT/api/v1/version" >/dev/null 2>&1; then
  echo "✗ backend not healthy after 20s — last log lines:" >&2
  tail -n 15 logs/backend.log >&2
  exit 1
fi

lan_ip="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
plan="$(curl -sf -m 2 "http://127.0.0.1:$SERIN_BACKEND_PORT/api/entitlements" 2>/dev/null || echo '{}')"
echo "✓ backend up · plan: $plan"
echo "  simulator URL:  http://127.0.0.1:$SERIN_BACKEND_PORT"
[ -n "$lan_ip" ] && echo "  phone URL:      http://$lan_ip:$SERIN_BACKEND_PORT"
if [ ! -f data/.serin-license ] && [ -z "${SERIN_LICENSE_KEY:-}" ]; then
  echo "  note: no license found — X-ray runs in upsell mode (that's the free tier)"
fi

if [ "${1:-}" = "--backend-only" ]; then
  # Detach: hand the backend over instead of killing it on exit.
  trap - EXIT INT TERM
  echo "› backend-only mode — stop later with: kill \$(cat logs/backend.pid)"
  exit 0
fi

# Simulator preflight — Metro still works without one (physical phone via QR),
# so a missing runtime warns instead of failing.
ios_flag="--ios"
if ! xcrun simctl list devices available 2>/dev/null | grep -q "iPhone"; then
  ios_flag=""
  echo "⚠ no iOS simulator available (Xcode runtime not installed/mounted)."
  echo "  Starting Metro anyway — scan the QR with Expo Go, or press i after fixing."
fi

cd mobile
if [ ! -d node_modules ]; then
  echo "› installing mobile dependencies (first run)…"
  npm install
fi

echo "› starting Expo (Ctrl-C stops Expo and the backend)…"
exec npx expo start $ios_flag
