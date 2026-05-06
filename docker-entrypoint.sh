#!/bin/sh
set -eu

if [ "${1:-}" = "init-config" ]; then
  exec python /app/bridge.py "$@"
fi

python /app/bootstrap.py run

if [ "${WEB_ENABLED:-true}" != "false" ]; then
  python -u /app/portal.py &
fi

run_bridge() {
  if [ -n "${LOG_FILE:-}" ]; then
    mkdir -p "$(dirname "$LOG_FILE")"
    exec python -u /app/bridge.py "$@" 2>&1 | tee -a "$LOG_FILE"
  fi
  exec python -u /app/bridge.py "$@"
}

run_bridge "$@"
