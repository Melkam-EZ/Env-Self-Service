#!/usr/bin/env bash
# End-to-end proof with REAL Terraform and no cloud account: starts a throwaway portal, then drives it
# through request -> approval -> provision -> destroy -> automatic expiry. Takes about 2 minutes.
set -euo pipefail
cd "$(dirname "$0")/.."

TF="${ENVP_TF_BIN:-$(command -v terraform || command -v tofu || true)}"
[ -n "${TF}" ] || { echo "terraform not found. Run ./scripts/setup.sh" >&2; exit 1; }
export ENVP_TF_BIN="${TF}" ENVP_RUNNER=terraform ENVP_REAPER_INTERVAL=2
PORT="${PORT:-8089}"
DATA="$(mktemp -d)"
export ENVP_DATA="${DATA}"
DEMO_DIR=/tmp/envportal-demo

python3 -m envp init > "${DATA}/tokens.txt"
python3 -m envp serve --port "${PORT}" > "${DATA}/server.log" 2>&1 &
PID=$!
cleanup() { kill "${PID}" 2>/dev/null || true; wait "${PID}" 2>/dev/null || true; rm -rf "${DATA}"; }
trap cleanup EXIT

for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null || { echo "portal did not start:"; cat "${DATA}/server.log"; exit 1; }

as() { local who="$1"; shift; ENVP_URL="http://127.0.0.1:${PORT}" ENVP_TOKEN="$(awk -v n="${who}" '$1==n{print $2}' "${DATA}/tokens.txt")" python3 -m envp.ctl "$@"; }
n=0
ok()   { n=$((n+1)); echo "ok   $*"; }
fail() { echo "FAIL $*"; echo "--- portal log ---"; tail -30 "${DATA}/server.log"; exit 1; }
new_id() { awk '{print $1}' | head -1; }

echo "Terraform: $("${TF}" version | head -1)"

# 1. small request: no approval needed, real terraform apply writes a file
SHORT="$(as alice request local-demo vsmall --ttl 1 | new_id)"
as alice wait "${SHORT}" ACTIVE --timeout 120 >/dev/null || fail "small environment never became ACTIVE"
[ -f "${DEMO_DIR}/${SHORT}.txt" ] || fail "terraform did not create the file for ${SHORT}"
ok "request -> provision: terraform created ${DEMO_DIR}/${SHORT}.txt"

# 2. expensive request waits for a human; a developer cannot approve their own request
BIG="$(as alice request local-demo vbig --set tier=large --ttl 240 | new_id)"
[ "$(as alice show "${BIG}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["status"])')" = "PENDING_APPROVAL" ] || fail "expensive request was not held for approval"
as alice approve "${BIG}" >/dev/null 2>&1 && fail "alice approved her own request"
ok "expensive request is held; the requester cannot approve it"

# 3. approver (carol) approves, it provisions
as carol approve "${BIG}" >/dev/null
as alice wait "${BIG}" ACTIVE --timeout 120 >/dev/null || fail "approved environment never became ACTIVE"
ok "approver approved -> provisioned"

# 4. another team cannot touch it
as bob destroy "${BIG}" >/dev/null 2>&1 && fail "bob (other team) destroyed alice's environment"
ok "other teams cannot destroy it"

# 5. owner destroys it early; the file is really gone
as alice destroy "${BIG}" >/dev/null
as alice wait "${BIG}" DESTROYED --timeout 120 >/dev/null || fail "destroy did not finish"
[ ! -f "${DEMO_DIR}/${BIG}.txt" ] || fail "file for ${BIG} still exists after destroy"
ok "destroy removed the infrastructure"

# 6. rejection
REJ="$(as alice request local-demo vrej --set tier=large --ttl 240 | new_id)"
as carol reject "${REJ}" --reason "not needed" >/dev/null
as alice wait "${REJ}" REJECTED --timeout 30 >/dev/null || fail "reject did not stick"
ok "rejected request never provisions"

# 7. the 1-minute environment expires and is destroyed by the reaper without anyone asking
echo "     waiting for ${SHORT} to expire (about 1 minute)..."
as alice wait "${SHORT}" DESTROYED --timeout 240 >/dev/null || fail "expired environment was not destroyed automatically"
[ ! -f "${DEMO_DIR}/${SHORT}.txt" ] || fail "file for ${SHORT} still exists after expiry"
ok "TTL expiry: reaper destroyed ${SHORT} automatically"

# 8. nothing left behind
python3 -m envp orphans >/dev/null || fail "orphan scan found leftovers"
ok "orphan scan is clean"

echo "All ${n} checks passed."
