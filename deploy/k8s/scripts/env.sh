# Settings shared by the Kubernetes scripts (sourced, not run). Each one comes from the environment, then from
# the repository's .env, then from a neutral default, so one .env drives Docker Compose and Kubernetes alike.
#
#   K8S_CLUSTER    kind cluster name                     (default rag)
#   K8S_NAMESPACE  namespace of the deployment           (default rag)
#   K8S_OVERLAY    local | cloud | deepseek-local | deepseek-cloud (default local)
#   K8S_HOST_PORT  kind: entry point on localhost        (default 8081)
#   K8S_IMAGE      application image                     (default genric-rag-app:local)
#   DOCS_HOST_PATH documents mounted into a kind cluster (default ./documents, as in Docker Compose)
#   GPU            nvidia | amd | none (auto means none: the GPU of a cluster is not detected from here)

# Git Bash rewrites arguments that look like paths (/tmp/x becomes C:/Users/.../Temp/x), which breaks the
# paths inside a pod given to kubectl exec and kubectl cp. No effect outside Git Bash.
export MSYS_NO_PATHCONV=1

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

is_kind_overlay() {
  case "$OVERLAY" in local|deepseek-local) return 0 ;; *) return 1 ;; esac
}
