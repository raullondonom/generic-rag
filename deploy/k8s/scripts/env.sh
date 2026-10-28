# Settings shared by the Kubernetes scripts (sourced, not run). Each one comes from the environment, then from
# the repository's .env, then from a neutral default, so one .env drives Docker Compose and Kubernetes alike.
#
#   K8S_DISTRO     local cluster: kind | k3s (k3s in one Docker container, lighter) (default kind)
#   K8S_CLUSTER    local cluster name                    (default rag)
#   K8S_NAMESPACE  namespace of the deployment           (default rag)
#   K8S_OVERLAY    local | cloud | deepseek-local | deepseek-cloud (default local)
#   K8S_HOST_PORT  local cluster: entry point on localhost (default 8081)
#   K8S_API_PORT   k3s: API server on 127.0.0.1           (default 6550)
#   K8S_IMAGE      application image                     (default genric-rag-app:local)
#   DOCS_HOST_PATH documents mounted into a local cluster (default ./documents, as in Docker Compose)
#   GPU            nvidia | amd | none (auto means none: the GPU of a cluster is not detected from here)
#   K8S_NODE_CPUS / K8S_NODE_MEMORY  optional Docker limits of the local node, e.g. 2 / 2g (no swap): a
#                  small server on a bigger machine
#   K8S_EMBEDDINGS_URL  embeddings served outside the cluster (embeddings-host.sh), e.g.
#                  http://10.0.0.5:8090; empty runs them inside the cluster

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"

# Value of KEY in .env (last definition wins), without surrounding quotes or a Windows line ending
env_file_get() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^${1}=//p" "$ENV_FILE" | tail -1 | sed -e 's/\r$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# setting VAR KEY DEFAULT: $VAR or $KEY from the environment, else KEY from .env, else DEFAULT
setting() {
  local value="${!1:-}"
  [ -n "$value" ] || value="${!2:-}"
  [ -n "$value" ] || value="$(env_file_get "$2")"
  printf '%s' "${value:-$3}"
}

# Path the Docker daemon understands (on Windows, Git Bash's /c/... becomes C:/...)
host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

CLUSTER="$(setting CLUSTER K8S_CLUSTER rag)"
NAMESPACE="$(setting NAMESPACE K8S_NAMESPACE rag)"
OVERLAY="$(setting OVERLAY K8S_OVERLAY local)"
HOST_PORT="$(setting HOST_PORT K8S_HOST_PORT 8081)"
IMAGE="$(setting IMAGE K8S_IMAGE genric-rag-app:local)"
BASE_URL="${BASE_URL:-http://localhost:$HOST_PORT}"

DOCS_DIR="$(setting DOCS_DIR DOCS_HOST_PATH ./documents)"
case "$DOCS_DIR" in
  /*|[A-Za-z]:*) ;;
  *) DOCS_DIR="$REPO_ROOT/${DOCS_DIR#./}" ;;
esac

GPU="$(setting GPU GPU none)"
[ "$GPU" = auto ] && GPU=none

DISTRO="$(setting K8S_DISTRO K8S_DISTRO kind)"
case "$DISTRO" in
  kind|k3s) ;;
  *) echo "K8S_DISTRO must be kind or k3s (got '$DISTRO')" >&2; exit 1 ;;
esac
API_PORT="$(setting K8S_API_PORT K8S_API_PORT 6550)"
NODE_CPUS="$(setting NODE_CPUS K8S_NODE_CPUS "")"
NODE_MEMORY="$(setting NODE_MEMORY K8S_NODE_MEMORY "")"
EMBEDDINGS_URL="$(setting EMBEDDINGS_URL K8S_EMBEDDINGS_URL "")"
KUBE_CONTEXT="$DISTRO-$CLUSTER"

is_local_overlay() {
  case "$OVERLAY" in local|deepseek-local) return 0 ;; *) return 1 ;; esac
}

# Docker containers of the local cluster's nodes (none when it does not exist)
local_nodes() {
  if [ "$DISTRO" = k3s ]; then
    docker ps -a --filter "name=^k3s-$CLUSTER\$" --format '{{.Names}}'
  elif command -v kind >/dev/null 2>&1; then
    kind get nodes --name "$CLUSTER" 2>/dev/null
  fi
}

local_cluster_exists() { [ -n "$(local_nodes)" ]; }

# K8S_NODE_CPUS / K8S_NODE_MEMORY on the node containers (memory without swap, like a server without it).
# Applied on every `up`; to remove a limit, recreate the cluster.
limit_local_nodes() {
  [ -n "$NODE_CPUS$NODE_MEMORY" ] || return 0
  local args=() node
  [ -n "$NODE_CPUS" ] && args+=(--cpus "$NODE_CPUS")
  [ -n "$NODE_MEMORY" ] && args+=(--memory "$NODE_MEMORY" --memory-swap "$NODE_MEMORY")
  for node in $(local_nodes); do
    docker update "${args[@]}" "$node" >/dev/null
  done
  echo "Node limits: CPU ${NODE_CPUS:-unlimited}, memory ${NODE_MEMORY:-unlimited}"
}
