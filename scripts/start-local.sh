#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p logs

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

for port in "$SERIN_BACKEND_PORT" "$SERIN_FRONTEND_PORT"; do
  existing="$(lsof -ti "tcp:$port" || true)"
  if [ -n "$existing" ]; then
    echo "$existing" | xargs kill
  fi
done

nohup "$python_bin" -m uvicorn backend.main:app \
  --host "$SERIN_BACKEND_HOST" \
  --port "$SERIN_BACKEND_PORT" \
  > logs/backend.log 2>&1 &
echo "$!" > logs/backend.pid

nohup npm run dev -- --host 127.0.0.1 --port "$SERIN_FRONTEND_PORT" \
  > logs/frontend.log 2>&1 &
echo "$!" > logs/frontend.pid

echo "Serin backend:  http://$SERIN_BACKEND_HOST:$SERIN_BACKEND_PORT"
echo "Serin frontend: http://127.0.0.1:$SERIN_FRONTEND_PORT"
