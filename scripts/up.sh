#!/usr/bin/env bash
# Starts the portal in the background. First run also creates users and prints their tokens.
#   RUNNER=fake ./scripts/up.sh     try the portal with no Terraform and no cloud (fake infrastructure)
#   PORT=9000 ./scripts/up.sh       change the port (default 8080, bound to 127.0.0.1 only)
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8080}"
export ENVP_RUNNER="${RUNNER:-terraform}"
mkdir -p data

if [ "${ENVP_RUNNER}" = "terraform" ] && ! command -v "${ENVP_TF_BIN:-terraform}" >/dev/null; then
  echo "terraform not found. Run ./scripts/setup.sh first (or RUNNER=fake ./scripts/up.sh)." >&2
  exit 1
fi

if [ ! -f data/users.json ]; then
  python3 -m envp init | tee data/tokens.txt
  chmod 600 data/tokens.txt
  echo "(tokens also saved in data/tokens.txt; delete that file once you have copied them)"
fi

if [ -f data/server.pid ] && kill -0 "$(cat data/server.pid)" 2>/dev/null; then
  echo "already running (pid $(cat data/server.pid))"
else
  nohup python3 -m envp serve --bind 127.0.0.1 --port "${PORT}" >> data/server.log 2>&1 &
  echo $! > data/server.pid
fi

for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null || { echo "did not start, see data/server.log" >&2; tail -20 data/server.log >&2; exit 1; }

echo
echo "Portal running (runner: ${ENVP_RUNNER}) on http://127.0.0.1:${PORT}"
echo "From your laptop:  ssh -L ${PORT}:localhost:${PORT} <user>@<ec2-public-ip>   then open http://localhost:${PORT}"
echo "Log in with a token from data/tokens.txt: alice/bob = developers, carol = approver, admin = admin."
