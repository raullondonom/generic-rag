#!/usr/bin/env bash
# Creates the local kind cluster used by the Kubernetes deployment.
# Usage: ./deploy/k8s/scripts/kind-up.sh
# The cluster name, the port and the documents folder (DOCS_HOST_PATH, as in Docker Compose) come from .env
# (see env.sh). The folder is mounted read-only when the cluster is created: to change it, recreate the cluster.
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
NODE_PORT="${NODE_PORT:-30080}"

if ! command -v kind >/dev/null 2>&1 || ! command -v kubectl >/dev/null 2>&1; then
  "$REPO_ROOT/deploy/k8s/scripts/install-tools.sh"
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "Cluster '$CLUSTER' already exists."
  kubectl cluster-info --context "kind-$CLUSTER"
  exit 0
fi

if [ ! -d "$DOCS_DIR" ]; then
  echo "Documents directory not found: $DOCS_DIR" >&2
  exit 1
fi
# The Docker daemon needs a host path it understands (C:/... on Windows, not Git Bash's /c/...)
DOCS_DIR="$(host_path "$(realpath "$DOCS_DIR")")"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: $CLUSTER
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: $NODE_PORT
        hostPort: $HOST_PORT
        protocol: TCP
    extraMounts:
      - hostPath: $DOCS_DIR
        containerPath: /mnt/documents
        readOnly: true
EOF

echo "Creating kind cluster '$CLUSTER' (documents: $DOCS_DIR)..."
kind create cluster --config "$tmp/kind.yaml"
kubectl config use-context "kind-$CLUSTER" >/dev/null
kubectl cluster-info

echo "Installing metrics-server (needed by the HPAs)..."
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml >/dev/null
kubectl -n kube-system patch deployment metrics-server --type=json -p='[
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"},
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-preferred-address-types=InternalIP"}
]' >/dev/null
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

cat <<EOF

Cluster ready.
  API entry point: $BASE_URL
  Next:            ./deploy/k8s/scripts/deploy.sh (or ./scripts/deploy.sh up with DEPLOY_MODE=k8s)
EOF
