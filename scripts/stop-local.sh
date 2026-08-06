#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

for pid_file in logs/backend.pid logs/frontend.pid; do
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
    rm -f "$pid_file"
  fi
done

for port in 8890 5174; do
  existing="$(lsof -ti "tcp:$port" || true)"
  if [ -n "$existing" ]; then
    echo "$existing" | xargs kill
  fi
done
