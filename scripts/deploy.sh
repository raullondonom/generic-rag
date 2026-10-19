#!/usr/bin/env bash
# Starts, stops or shows the deployment chosen in .env with DEPLOY_MODE:
#   mini  docker-compose.yml: one of everything (also plain `docker compose up -d`)
#   ha    docker-compose.yml + docker-compose.ha.yml: nginx, 2 APIs, queue, workers, 3 Qdrant, 2-3 embeddings
#   k8s   deploy/k8s: a local kind cluster or a managed one (K8S_OVERLAY)
# GPU (auto | nvidia | amd | none) applies to the three.
#
#   ./scripts/deploy.sh up               # start (build, create the kind cluster if needed)
#   ./scripts/deploy.sh down             # stop; the data is kept
#   ./scripts/deploy.sh status
#   ./scripts/deploy.sh logs [service]
#   ./scripts/deploy.sh restart [service]
#   DEPLOY_MODE=ha ./scripts/deploy.sh up   # the environment wins over .env
set -euo pipefail
cd "$(dirname "$0")/.."

env_get() {
  if [ -f .env ]; then
    sed -n "s/^$1=//p" .env | tail -1 | sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
  fi
}

MODE="${DEPLOY_MODE:-$(env_get DEPLOY_MODE)}"
MODE="${MODE:-mini}"
CMD="${1:-}"
[ $# -gt 0 ] && shift

usage() {
  sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}
[ -n "$CMD" ] || usage 1

# ---------------------------------------------------------------- Docker Compose (mini, ha)
MINI_ONLY=(rag-app qdrant embeddings)
HA_ONLY=(redis-queue redis-cache qdrant-1 qdrant-2 qdrant-3 embeddings-1 embeddings-2 embeddings-3
         nginx-internal api-1 api-2 worker scheduler edge)

compose() { DEPLOY_MODE="$MODE" ./scripts/compose.sh "$@"; }

# Postgres, Ollama and the model volume are shared by both modes; the services of the other mode are
# removed (their volumes stay), so the two never answer at the same time.
leave_other_mode() {
  local others
  if [ "$MODE" = ha ]; then
    others="$(docker compose -f docker-compose.yml ps -a -q "${MINI_ONLY[@]}" 2>/dev/null || true)"
    if [ -n "$others" ]; then
      echo "Removing the mini services (rag-app, qdrant, embeddings); their data stays in the volumes."
      echo "The 3 Qdrant nodes of HA start empty: see \"Switching from basic to HA\" in the README."
      docker compose -f docker-compose.yml rm -sf "${MINI_ONLY[@]}" >/dev/null
    fi
  else
    others="$(docker compose -f docker-compose.yml -f docker-compose.ha.yml ps -a -q "${HA_ONLY[@]}" 2>/dev/null || true)"
    if [ -n "$others" ]; then
      echo "Removing the HA services; their data stays in the volumes."
      docker compose -f docker-compose.yml -f docker-compose.ha.yml rm -sf "${HA_ONLY[@]}" >/dev/null
    fi
  fi
}

entry_point() {
  if [ "$MODE" = ha ]; then
    echo "http://localhost:$(env_get HA_PORT | grep . || echo 8080)"
  else
    echo "http://localhost:$(env_get APP_PORT | grep . || echo 8000)"
  fi
}

compose_mode() {
  case "$CMD" in
    up)
      leave_other_mode
      compose up -d --build "$@"
      echo
      echo "Entry point: $(entry_point)   (first start: the models are downloaded; ./scripts/deploy.sh logs)"
      ;;
    down) compose down "$@" ;;
    status) compose ps "$@" ;;
    logs) compose logs -f --tail=100 "$@" ;;
    restart) compose restart "$@" ;;
    *) usage 1 ;;
  esac
}

# ---------------------------------------------------------------- Kubernetes (k8s)
k8s_mode() {
  # shellcheck source=../deploy/k8s/scripts/env.sh
  . deploy/k8s/scripts/env.sh
  local scripts=deploy/k8s/scripts
  case "$CMD" in
    up)
      if is_kind_overlay; then
        if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
          "$scripts/kind-up.sh"
        else
          # A cluster stopped by `down`: start its node containers again
          local node stopped=0
          for node in $(kind get nodes --name "$CLUSTER"); do
            if [ "$(docker inspect -f '{{.State.Running}}' "$node")" != true ]; then docker start "$node" >/dev/null; stopped=1; fi
          done
          if [ "$stopped" = 1 ]; then
            echo "Starting kind cluster '$CLUSTER' again..."
            kubectl --context "kind-$CLUSTER" wait --for=condition=Ready node --all --timeout=180s >/dev/null
          fi
        fi
        kubectl config use-context "kind-$CLUSTER" >/dev/null
      fi
      "$scripts/deploy.sh" "$@"
      ;;
    down)
      if is_kind_overlay; then
        echo "Stopping kind cluster '$CLUSTER' (its data stays; ./deploy/k8s/scripts/kind-down.sh deletes it)."
        docker stop $(kind get nodes --name "$CLUSTER") >/dev/null
      else
        echo "Scaling namespace '$NAMESPACE' to zero (the volumes stay; ./scripts/deploy.sh up starts it again)."
        kubectl -n "$NAMESPACE" scale deployment --all --replicas=0
        kubectl -n "$NAMESPACE" scale statefulset --all --replicas=0
      fi
      ;;
    status) "$scripts/status.sh" ;;
    logs)
      local svc="${1:-scheduler}"
      case "$svc" in postgres|qdrant) svc="statefulset/$svc" ;; *) svc="deployment/$svc" ;; esac
      kubectl -n "$NAMESPACE" logs -f --tail=100 "$svc"
      ;;
    restart)
      if [ $# -gt 0 ]; then kubectl -n "$NAMESPACE" rollout restart "deployment/$1"
      else kubectl -n "$NAMESPACE" rollout restart deployment
      fi
      ;;
    *) usage 1 ;;
  esac
}

case "$MODE" in
  mini|ha) compose_mode "$@" ;;
  k8s) k8s_mode "$@" ;;
  *) echo "DEPLOY_MODE must be mini, ha or k8s (got '$MODE')" >&2; exit 1 ;;
esac
