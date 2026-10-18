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
  for img in \
    "$IMAGE" \
    postgres:18.4-alpine \
    qdrant/qdrant:v1.19.2 \
    ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 \
    ollama/ollama:0.40.5 \
    nginx:1.28-alpine; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      kind load docker-image "$img" --name "$CLUSTER"
    else
      echo "  $img is not local: the cluster will pull it"
    fi
  done
fi

kubectl apply -f "$REPO_ROOT/deploy/k8s/base/namespace.yaml"
"$REPO_ROOT/deploy/k8s/scripts/secrets.sh"

# The Job is immutable: recreate it so a changed model is pulled again.
kubectl -n "$NAMESPACE" delete job ollama-pull --ignore-not-found >/dev/null

echo "Applying overlay '$OVERLAY'..."
kubectl apply -k "$REPO_ROOT/deploy/k8s/overlays/$OVERLAY"

# ConfigMap values are read at pod start.
kubectl -n "$NAMESPACE" rollout restart \
  deployment/api deployment/scheduler deployment/embeddings deployment/nginx >/dev/null

for target in \
  statefulset/postgres \
  statefulset/qdrant \
  deployment/embeddings \
  deployment/ollama \
  deployment/api \
  deployment/scheduler \
  deployment/nginx; do
  echo "Waiting for $target..."
  kubectl -n "$NAMESPACE" rollout status "$target" --timeout=900s
done

echo "Waiting for the Ollama model (first run downloads it)..."
if kubectl -n "$NAMESPACE" wait --for=condition=complete job/ollama-pull --timeout=1800s; then
  echo "Model ready."
else
  echo "The model is still downloading. Check: kubectl -n $NAMESPACE logs job/ollama-pull" >&2
fi

echo
kubectl -n "$NAMESPACE" get pods
if [ "$OVERLAY" = local ]; then
  echo
  echo "Entry point: http://localhost:${HOST_PORT:-8081}"
fi
