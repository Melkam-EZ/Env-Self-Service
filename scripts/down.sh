#!/usr/bin/env bash
# Stops the portal. Environments still running are NOT destroyed: destroy them first (portal or envctl),
# or run with --destroy-all to destroy every live environment before stopping.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--destroy-all" ]; then
  TOKEN="$(awk '$1=="admin"{print $2}' data/tokens.txt)"
  export ENVP_URL="http://127.0.0.1:${PORT:-8080}" ENVP_TOKEN="${TOKEN}"
  for id in $(python3 -m envp.ctl list | awk '$2=="ACTIVE" || $2=="PROVISION_FAILED" || $2=="DESTROY_FAILED" {print $1}'); do
    python3 -m envp.ctl destroy "${id}" || true
  done
  echo "waiting for destroys to finish..."
  for _ in $(seq 1 120); do
    left="$(python3 -m envp.ctl list | awk '$2=="DESTROYING" || $2=="DESTROY_QUEUED" || $2=="ACTIVE" {n++} END{print n+0}')"
    [ "${left}" = "0" ] && break
    sleep 5
  done
fi

if [ -f data/server.pid ] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  kill "$(cat data/server.pid)"
  rm -f data/server.pid
  echo "portal stopped"
else
  echo "portal was not running"
fi
