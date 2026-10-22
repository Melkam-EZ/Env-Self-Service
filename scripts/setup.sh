#!/usr/bin/env bash
# Installs what the portal needs on Ubuntu 22.04/24.04 or Amazon Linux 2023: git, curl, unzip, python3, Terraform.
# Safe to re-run. Needs sudo. Override the version with: TF_VERSION=1.9.8 ./scripts/setup.sh
set -euo pipefail

TF_VERSION="${TF_VERSION:-1.9.8}"

. /etc/os-release
case "${ID}" in
  ubuntu|debian) sudo apt-get update -y && sudo apt-get install -y git curl unzip python3 ;;
  amzn|rhel|fedora) sudo dnf install -y git unzip python3 && (command -v curl >/dev/null || sudo dnf install -y curl) ;;
  *) echo "Unsupported OS '${ID}'. Install git, curl, unzip, python3 (3.9+) and Terraform by hand." >&2; exit 1 ;;
esac

python3 - <<'EOF'
import sys
assert sys.version_info >= (3, 9), f"Python 3.9+ needed, found {sys.version.split()[0]}"
print("python", sys.version.split()[0], "ok")
EOF

if command -v terraform >/dev/null && terraform version | head -1 | grep -q "v${TF_VERSION}"; then
  echo "terraform ${TF_VERSION} already installed"
else
  case "$(uname -m)" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; *) echo "unknown CPU $(uname -m)" >&2; exit 1 ;; esac
  TMP="$(mktemp -d)"
  curl -fsSL -o "${TMP}/tf.zip" "https://releases.hashicorp.com/terraform/${TF_VERSION}/terraform_${TF_VERSION}_linux_${ARCH}.zip"
  unzip -o -q "${TMP}/tf.zip" -d "${TMP}"
  sudo install -m 0755 "${TMP}/terraform" /usr/local/bin/terraform
  rm -rf "${TMP}"
fi
terraform version | head -1

if command -v aws >/dev/null; then
  echo "aws cli: $(aws --version 2>&1 | cut -d' ' -f1)"
else
  echo "NOTE: AWS CLI not found. Only needed for the AWS templates and the AWS orphan scan."
  echo "      Install: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
fi
echo "Setup done."
