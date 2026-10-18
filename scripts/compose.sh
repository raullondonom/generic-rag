#!/usr/bin/env bash
# docker compose with the GPU auto-detected: NVIDIA (docker-compose.gpu.yml), AMD/ROCm (docker-compose.amd.yml)
# or CPU. Use it instead of "docker compose":  ./scripts/compose.sh up -d
# Force a choice with GPU=nvidia|amd|none. Print the detection only: ./scripts/compose.sh --detect
set -euo pipefail
cd "$(dirname "$0")/.."

detect() {
  case "${GPU:-auto}" in
    nvidia|amd|none) echo "$GPU"; return ;;
  esac
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 \
     && docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q nvidia; then
    echo nvidia
  elif [ -r /dev/kfd ] && [ -w /dev/kfd ] && [ -d /dev/dri ]; then
    echo amd
  else
    echo none
  fi
}

gpu="$(detect)"
files=(-f docker-compose.yml)
case "$gpu" in
  nvidia) files+=(-f docker-compose.gpu.yml) ;;
  amd)
    files+=(-f docker-compose.amd.yml)
    export RENDER_GID="${RENDER_GID:-$(getent group render | cut -d: -f3)}"
    ;;
esac

if [ "${1:-}" = "--detect" ]; then echo "GPU: $gpu"; exit 0; fi
echo "[compose.sh] GPU: $gpu" >&2
exec docker compose "${files[@]}" "$@"
