#!/usr/bin/env bash
# Installs kubectl and kind in $BIN_DIR (default ~/.local/bin) without sudo.
# Usage: ./deploy/k8s/scripts/install-tools.sh
set -euo pipefail

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
KUBECTL_VERSION="${KUBECTL_VERSION:-v1.37.1}"
KIND_VERSION="${KIND_VERSION:-v0.33.0}"

case "$(uname -m)" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$BIN_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if command -v kubectl >/dev/null 2>&1; then
  echo "kubectl already installed: $(kubectl version --client 2>/dev/null | head -1)"
else
  echo "Downloading kubectl $KUBECTL_VERSION..."
  curl -fsSLo "$tmp/kubectl" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"
  install -m 0755 "$tmp/kubectl" "$BIN_DIR/kubectl"
  echo "kubectl installed in $BIN_DIR/kubectl"
fi

if command -v kind >/dev/null 2>&1; then
  echo "kind already installed: $(kind version)"
else
  echo "Downloading kind $KIND_VERSION..."
  curl -fsSLo "$tmp/kind" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-${arch}"
  install -m 0755 "$tmp/kind" "$BIN_DIR/kind"
  echo "kind installed in $BIN_DIR/kind"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add this to your shell profile: export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac

echo
echo "Versions:"
"$BIN_DIR/kubectl" version --client
"$BIN_DIR/kind" version
