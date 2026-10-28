#!/usr/bin/env bash
# Starts, stops or shows the deployment chosen in .env with DEPLOY_MODE:
#   mini  docker-compose.yml: one of everything (also plain `docker compose up -d`)
#   ha    docker-compose.yml + docker-compose.ha.yml: nginx, 2 APIs, queue, workers, 3 Qdrant, 2-3 embeddings
#   k8s   deploy/k8s: a local kind or k3s cluster (K8S_DISTRO) or a managed one (K8S_OVERLAY)
# GPU (auto | nvidia | amd | none) applies to the three.
#
#   ./scripts/deploy.sh up               # start (build, create the kind cluster if needed)
#   ./scripts/deploy.sh down             # stop; the data is kept
#   ./scripts/deploy.sh status
#   ./scripts/deploy.sh logs [service]
#   ./scripts/deploy.sh restart [service]
#   ./scripts/deploy.sh tunnel up|down|url   # public ngrok URL with user/password (NGROK_* in .env)
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
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}
[ -n "$CMD" ] || usage 1

# ---------------------------------------------------------------- Docker Compose (mini, ha)
# ngrok is in both: its upstream (rag-app or edge) belongs to the mode it was started in
MINI_ONLY=(rag-app qdrant embeddings ngrok)
HA_ONLY=(redis-queue redis-cache qdrant-1 qdrant-2 qdrant-3 embeddings-1 embeddings-2 embeddings-3
         nginx-internal api-1 api-2 worker scheduler edge ngrok)

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
      docker compose -f docker-compose.yml rm -sf "${MINI_ONLY[@]}" >/dev/null 2>&1
    fi
  else
    others="$(docker compose -f docker-compose.yml -f docker-compose.ha.yml ps -a -q "${HA_ONLY[@]}" 2>/dev/null || true)"
    if [ -n "$others" ]; then
      echo "Removing the HA services; their data stays in the volumes."
      docker compose -f docker-compose.yml -f docker-compose.ha.yml rm -sf "${HA_ONLY[@]}" >/dev/null 2>&1
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

# Public URL of the tunnel: the ngrok API answers JSON with "public_url"
public_url() { grep -o '"public_url":"[^"]*"' | head -1 | cut -d'"' -f4; }
wait_url() {  # wait_url <command that prints the ngrok API answer>
  local url i
  for i in $(seq 1 30); do
    url="$("$@" 2>/dev/null | public_url || true)"
    if [ -n "$url" ]; then
      echo "Tunnel: $url   (user and password: NGROK_BASIC_AUTH; /admin also needs ADMIN_TOKEN)"
      return 0
    fi
    sleep 2
  done
  echo "No public URL yet: see the logs (./scripts/deploy.sh logs ngrok)" >&2
  return 1
}

compose_mode() {
  case "$CMD" in
    tunnel)
      local panel
      panel="$(env_get NGROK_PANEL_PORT | grep . || echo 4040)"
      case "${1:-up}" in
        up) compose --profile ngrok up -d ngrok && wait_url curl -s "http://127.0.0.1:$panel/api/tunnels" ;;
        down) compose --profile ngrok rm -sf ngrok ;;
        url) wait_url curl -s "http://127.0.0.1:$panel/api/tunnels" ;;
        *) usage 1 ;;
      esac
      ;;
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
  local K=(kubectl -n "$NAMESPACE")
  case "$CMD" in
    tunnel)
      case "${1:-up}" in
        up)
          local token auth domain
          token="$(setting NGROK_AUTHTOKEN NGROK_AUTHTOKEN "")"
          auth="$(setting NGROK_BASIC_AUTH NGROK_BASIC_AUTH "")"
          domain="$(setting NGROK_DOMAIN NGROK_DOMAIN "")"
          [ -n "$token" ] || { echo "Set NGROK_AUTHTOKEN (environment variable or .env)" >&2; exit 1; }
          case "$auth" in *:????????*) ;; *) echo "Set NGROK_BASIC_AUTH=user:password (8+ characters) in .env" >&2; exit 1 ;; esac
          "${K[@]}" create secret generic ngrok --from-literal=NGROK_AUTHTOKEN="$token" \
            --from-literal=NGROK_BASIC_AUTH="$auth" --from-literal=NGROK_DOMAIN="$domain" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
          "${K[@]}" apply -f deploy/k8s/optional/ngrok.yaml >/dev/null
          "${K[@]}" rollout restart deployment/ngrok >/dev/null   # picks up a changed Secret
          "${K[@]}" rollout status deployment/ngrok --timeout=180s >/dev/null
          wait_url "${K[@]}" exec deployment/ngrok -- wget -qO- http://127.0.0.1:4040/api/tunnels
          ;;
        down) "${K[@]}" delete deployment/ngrok secret/ngrok --ignore-not-found ;;
        url) wait_url "${K[@]}" exec deployment/ngrok -- wget -qO- http://127.0.0.1:4040/api/tunnels ;;
        *) usage 1 ;;
      esac
      ;;
    up)
      if is_local_overlay; then
        if ! local_cluster_exists; then
          "$scripts/$DISTRO-up.sh"
        else
          # A cluster stopped by `down`: start its node containers again
          local node stopped=0
          for node in $(local_nodes); do
            if [ "$(docker inspect -f '{{.State.Running}}' "$node")" != true ]; then docker start "$node" >/dev/null; stopped=1; fi
          done
          if [ "$stopped" = 1 ]; then
            echo "Starting $DISTRO cluster '$CLUSTER' again..."
            # Right after `docker start` the API server refuses connections for a few seconds, and
            # `kubectl wait` fails at once instead of waiting
            local i
            for i in $(seq 1 60); do
              kubectl --context "$KUBE_CONTEXT" get --raw /readyz >/dev/null 2>&1 && break
              sleep 2
            done
            kubectl --context "$KUBE_CONTEXT" wait --for=condition=Ready node --all --timeout=180s >/dev/null
          fi
          limit_local_nodes
        fi
        kubectl config use-context "$KUBE_CONTEXT" >/dev/null
      fi
      "$scripts/deploy.sh" "$@"
      ;;
    down)
      if is_local_overlay; then
        echo "Stopping $DISTRO cluster '$CLUSTER' (its data stays; ./deploy/k8s/scripts/$DISTRO-down.sh deletes it)."
        docker stop $(local_nodes) >/dev/null
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
