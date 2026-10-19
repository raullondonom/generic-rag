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

# Windows (Git Bash): the .exe builds
os=linux ext=""
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) os=windows ext=".exe" ;;
  Darwin) os=darwin ;;
esac

mkdir -p "$BIN_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if command -v kubectl >/dev/null 2>&1; then
  echo "kubectl already installed: $(kubectl version --client 2>/dev/null | head -1)"
else
  echo "Downloading kubectl $KUBECTL_VERSION..."
  curl -fsSLo "$tmp/kubectl$ext" "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl$ext"
  install -m 0755 "$tmp/kubectl$ext" "$BIN_DIR/kubectl$ext"
  echo "kubectl installed in $BIN_DIR/kubectl$ext"
fi

if command -v kind >/dev/null 2>&1; then
  echo "kind already installed: $(kind version)"
else
  echo "Downloading kind $KIND_VERSION..."
  curl -fsSLo "$tmp/kind$ext" "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-${os}-${arch}"
  install -m 0755 "$tmp/kind$ext" "$BIN_DIR/kind$ext"
  echo "kind installed in $BIN_DIR/kind$ext"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add this to your shell profile: export PATH=\"\$PATH:$BIN_DIR\"" ;;
esac

echo
echo "Versions:"
# The one on PATH, or the one just installed (a tool already installed elsewhere is not in BIN_DIR)
"$(command -v kubectl || echo "$BIN_DIR/kubectl$ext")" version --client
"$(command -v kind || echo "$BIN_DIR/kind$ext")" version
