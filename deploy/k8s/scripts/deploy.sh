#!/usr/bin/env bash
# Builds the application image (optional), loads it into kind and applies the
# Kubernetes manifests. Works with a cloud cluster too (BUILD=0, OVERLAY=cloud).
#
#   ./deploy/k8s/scripts/deploy.sh                 # local kind cluster
#   OVERLAY=cloud BUILD=0 ./deploy/k8s/scripts/deploy.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CLUSTER="${CLUSTER:-transit-chat}"
OVERLAY="${OVERLAY:-local}"
NAMESPACE="${NAMESPACE:-transit-chat}"
BUILD="${BUILD:-1}"
IMAGE="${IMAGE:-genric-rag-app:local}"

if [ "$BUILD" = 1 ]; then
  echo "Building $IMAGE..."
  docker build -t "$IMAGE" "$REPO_ROOT/app"
fi

if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "Loading images into kind cluster '$CLUSTER'..."
  images=(
    "$IMAGE"
    postgres:18.4-alpine
    qdrant/qdrant:v1.19.2
    ghcr.io/huggingface/text-embeddings-inference:cpu-1.9
    nginx:1.28-alpine
  )
  case "$OVERLAY" in
    deepseek-*) ;;  # no local model
    *) images+=(ollama/ollama:0.40.5) ;;
  esac
  for img in "${images[@]}"; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      kind load docker-image "$img" --name "$CLUSTER"
    else
      echo "  $img is not local: the cluster will pull it"
    fi
  done
fi

kubectl apply -f "$REPO_ROOT/deploy/k8s/base/namespace.yaml"
"$REPO_ROOT/deploy/k8s/scripts/secrets.sh"

if [[ "$OVERLAY" == deepseek-* ]] && \
   [ -z "$(kubectl -n "$NAMESPACE" get secret rag-secrets -o jsonpath='{.data.DEEPSEEK_API_KEY}' 2>/dev/null)" ]; then
  echo "DEEPSEEK_API_KEY is required for overlay '$OVERLAY': put it in .env or export it." >&2
  exit 1
fi

# The Job is immutable: recreate it so a changed model is pulled again.
kubectl -n "$NAMESPACE" delete job ollama-pull --ignore-not-found >/dev/null

# `$patch: delete` only removes them from the rendered output; `kubectl apply` does not prune
# resources that already exist, so delete the local model explicitly when it is not used.
case "$OVERLAY" in
  deepseek-*)
    kubectl -n "$NAMESPACE" delete deployment/ollama service/ollama pvc/ollama-models \
      --ignore-not-found >/dev/null
    ;;
esac

echo "Applying overlay '$OVERLAY'..."
kubectl apply -k "$REPO_ROOT/deploy/k8s/overlays/$OVERLAY"

# ConfigMap values are read at pod start. Restarting is opt-in because it can interrupt an
# indexing run (use it after changing the ConfigMap, when the scheduler is idle):
#   RESTART=1 ./deploy/k8s/scripts/deploy.sh
if [ "${RESTART:-0}" = 1 ]; then
  kubectl -n "$NAMESPACE" rollout restart \
    deployment/api deployment/scheduler deployment/embeddings deployment/nginx >/dev/null
fi

for target in \
  statefulset/postgres \
  statefulset/qdrant \
  deployment/embeddings \
  deployment/api \
  deployment/scheduler \
  deployment/nginx; do
  echo "Waiting for $target..."
  kubectl -n "$NAMESPACE" rollout status "$target" --timeout=900s
done

if kubectl -n "$NAMESPACE" get deployment ollama >/dev/null 2>&1; then
  echo "Waiting for deployment/ollama..."
  kubectl -n "$NAMESPACE" rollout status deployment/ollama --timeout=300s
  echo "Waiting for the Ollama model (first run downloads it)..."
  if kubectl -n "$NAMESPACE" wait --for=condition=complete job/ollama-pull --timeout=1800s; then
    echo "Model ready."
  else
    echo "The model is still downloading. Check: kubectl -n $NAMESPACE logs job/ollama-pull" >&2
  fi
fi

echo
kubectl -n "$NAMESPACE" get pods
case "$OVERLAY" in
  local|deepseek-local)
    echo
    echo "Entry point: http://localhost:${HOST_PORT:-8081}"
    ;;
esac
