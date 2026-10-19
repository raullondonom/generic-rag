#!/usr/bin/env bash
# docker compose for the mini or the HA deployment, with the GPU auto-detected. Use it instead of
# "docker compose" (./scripts/deploy.sh calls it for DEPLOY_MODE=mini and ha):
#   ./scripts/compose.sh up -d
#   ./scripts/compose.sh --detect          # print the choice only
# DEPLOY_MODE (mini | ha) and GPU (auto | nvidia | amd | none) come from the environment or .env.
#   mini: docker-compose.yml
#   ha:   docker-compose.yml + docker-compose.ha.yml
#   GPU nvidia adds docker-compose.gpu.yml (and docker-compose.ha.gpu.yml in HA), amd docker-compose.amd.yml.
set -euo pipefail
cd "$(dirname "$0")/.."

env_get() {
  if [ -f .env ]; then
    sed -n "s/^$1=//p" .env | tail -1 | sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
  fi
}

MODE="${DEPLOY_MODE:-$(env_get DEPLOY_MODE)}"
MODE="${MODE:-mini}"
GPU="${GPU:-$(env_get GPU)}"

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

files=(-f docker-compose.yml)
case "$MODE" in
  mini) ;;
  ha) files+=(-f docker-compose.ha.yml) ;;
  *) echo "compose.sh: DEPLOY_MODE must be mini or ha (Kubernetes: ./scripts/deploy.sh with DEPLOY_MODE=k8s)" >&2
     exit 1 ;;
esac

gpu="$(detect)"
case "$gpu" in
  nvidia)
    files+=(-f docker-compose.gpu.yml)
    [ "$MODE" = ha ] && files+=(-f docker-compose.ha.gpu.yml)
    ;;
  amd)
    files+=(-f docker-compose.amd.yml)
    if [ -z "${RENDER_GID:-}" ] && command -v getent >/dev/null 2>&1; then
      RENDER_GID="$(getent group render | cut -d: -f3)"
      export RENDER_GID
    fi
    ;;
esac

if [ "${1:-}" = "--detect" ]; then echo "Mode: $MODE · GPU: $gpu · ${files[*]}"; exit 0; fi
echo "[compose.sh] mode: $MODE · GPU: $gpu" >&2
exec docker compose "${files[@]}" "$@"
