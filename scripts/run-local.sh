#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export SERIN_BACKEND_HOST="${SERIN_BACKEND_HOST:-127.0.0.1}"
export SERIN_BACKEND_PORT="${SERIN_BACKEND_PORT:-8890}"
export SERIN_FRONTEND_PORT="${SERIN_FRONTEND_PORT:-5174}"
export SERIN_DB_PATH="${SERIN_DB_PATH:-data/serin.db}"

python_bin="python3"
if [ -x ".venv/bin/python" ]; then
  python_bin=".venv/bin/python"
fi

"$python_bin" -m uvicorn backend.main:app --host "$SERIN_BACKEND_HOST" --port "$SERIN_BACKEND_PORT" &
backend_pid=$!

npm run dev -- --host 127.0.0.1 --port "$SERIN_FRONTEND_PORT" &
frontend_pid=$!

cleanup() {
  kill "$backend_pid" "$frontend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

while true; do
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    wait "$backend_pid"
    exit $?
  fi
  if ! kill -0 "$frontend_pid" 2>/dev/null; then
    wait "$frontend_pid"
    exit $?
  fi
  sleep 1
done
