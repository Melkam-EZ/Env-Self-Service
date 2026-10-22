#!/usr/bin/env bash
# Optional: the same lifecycle against REAL AWS, using the s3-sandbox template (costs a fraction of a cent).
# Needs AWS credentials on this machine (an EC2 instance role is best) and the AWS CLI.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v aws >/dev/null || { echo "AWS CLI not found" >&2; exit 1; }
aws sts get-caller-identity >/dev/null || { echo "No AWS credentials. Attach an instance role, see STEPS.md" >&2; exit 1; }
TF="${ENVP_TF_BIN:-$(command -v terraform || command -v tofu)}"
export ENVP_TF_BIN="${TF}" ENVP_RUNNER=terraform ENVP_REAPER_INTERVAL=5 ENVP_AWS_REGION="${AWS_REGION:-eu-west-1}"
PORT="${PORT:-8090}"
DATA="$(mktemp -d)"
export ENVP_DATA="${DATA}"

python3 -m envp init > "${DATA}/tokens.txt"
python3 -m envp serve --port "${PORT}" > "${DATA}/server.log" 2>&1 &
PID=$!
trap 'kill ${PID} 2>/dev/null || true; rm -rf "${DATA}"' EXIT
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1 && break; sleep 0.5; done

as() { local who="$1"; shift; ENVP_URL="http://127.0.0.1:${PORT}" ENVP_TOKEN="$(awk -v n="${who}" '$1==n{print $2}' "${DATA}/tokens.txt")" python3 -m envp.ctl "$@"; }

ID="$(as alice request s3-sandbox awscheck --ttl 15 | awk '{print $1}' | head -1)"
as alice wait "${ID}" ACTIVE --timeout 300
BUCKET="$(as alice show "${ID}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["outputs"]["bucket"])')"
aws s3api head-bucket --bucket "${BUCKET}" && echo "ok   bucket ${BUCKET} exists in AWS"
aws s3api get-bucket-tagging --bucket "${BUCKET}" --query "TagSet[?Key=='expires_at'].Value" --output text | sed 's/^/ok   expires_at tag: /'

as alice destroy "${ID}" >/dev/null
as alice wait "${ID}" DESTROYED --timeout 300
if aws s3api head-bucket --bucket "${BUCKET}" 2>/dev/null; then echo "FAIL bucket still exists"; exit 1; fi
echo "ok   bucket is gone after destroy"

python3 -m envp orphans --aws && echo "ok   no orphaned AWS resources"
echo "AWS check passed."
