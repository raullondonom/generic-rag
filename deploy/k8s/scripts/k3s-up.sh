#!/usr/bin/env bash
# Creates the local k3s cluster (K8S_DISTRO=k3s): k3s in one Docker container, as kind does, but with a lighter
# control plane (one process, SQLite instead of etcd, metrics-server included), for small servers.
# Usage: ./deploy/k8s/scripts/k3s-up.sh   (./scripts/deploy.sh up calls it when the cluster does not exist)
# The name, the ports and the documents folder come from .env (see env.sh). The node is the container
# k3s-<cluster>; its state lives in the Docker volume of the same name (k3s-down.sh deletes both). The folder
# is mounted read-only when the cluster is created: to change it, recreate the cluster.
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
# Git Bash would rewrite the paths inside the container (/run, /etc/...) into Windows paths
export MSYS_NO_PATHCONV=1
NODE_PORT="${NODE_PORT:-30080}"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.36.5-k3s1}"
NODE="k3s-$CLUSTER"

if ! command -v kubectl >/dev/null 2>&1; then
  "$REPO_ROOT/deploy/k8s/scripts/install-tools.sh"
fi

if local_cluster_exists; then
  echo "Cluster '$CLUSTER' (k3s) already exists."
  kubectl cluster-info --context "$KUBE_CONTEXT"
  exit 0
fi

if [ ! -d "$DOCS_DIR" ]; then
  echo "Documents directory not found: $DOCS_DIR" >&2
  exit 1
fi
DOCS_DIR="$(host_path "$(realpath "$DOCS_DIR")")"

echo "Creating k3s cluster '$CLUSTER' ($K3S_IMAGE, documents: $DOCS_DIR)..."
# traefik and the service load balancer are not used: nginx is the entry point through its NodePort
docker run -d --name "$NODE" --hostname "$NODE" --privileged --restart unless-stopped \
  --tmpfs /run --tmpfs /var/run \
  -p "$HOST_PORT:$NODE_PORT" -p "127.0.0.1:$API_PORT:6443" \
  -v "$NODE:/var/lib/rancher/k3s" \
  -v "$DOCS_DIR:/mnt/documents:ro" \
  "$K3S_IMAGE" server --disable=traefik --disable=servicelb --tls-san=127.0.0.1 >/dev/null

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
for _ in $(seq 1 90); do
  docker exec "$NODE" test -s /etc/rancher/k3s/k3s.yaml 2>/dev/null && break
  sleep 2
done
docker exec "$NODE" cat /etc/rancher/k3s/k3s.yaml > "$tmp/k3s.yaml"

# Context k3s-<cluster> in the user's kubeconfig (merged with kubectl itself: the KUBECONFIG list separator
# differs between Windows and the rest)
field() { sed -n "s/^ *$1: //p" "$tmp/k3s.yaml" | head -1 | base64 -d > "$tmp/$2"; }
field certificate-authority-data ca.crt
field client-certificate-data client.crt
field client-key-data client.key
kubectl config set-cluster "$KUBE_CONTEXT" --server "https://127.0.0.1:$API_PORT" \
  --certificate-authority "$(host_path "$tmp/ca.crt")" --embed-certs >/dev/null
kubectl config set-credentials "$KUBE_CONTEXT" --client-certificate "$(host_path "$tmp/client.crt")" \
  --client-key "$(host_path "$tmp/client.key")" --embed-certs >/dev/null
kubectl config set-context "$KUBE_CONTEXT" --cluster "$KUBE_CONTEXT" --user "$KUBE_CONTEXT" >/dev/null
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

echo "Waiting for the node..."
for _ in $(seq 1 90); do
  [ -n "$(kubectl get nodes -o name 2>/dev/null)" ] && break
  sleep 2
done
kubectl wait --for=condition=Ready node --all --timeout=180s >/dev/null
kubectl cluster-info
limit_local_nodes

# k3s ships metrics-server (needed by the HPAs) as an add-on that appears a few seconds after the node
echo "Waiting for metrics-server..."
for _ in $(seq 1 60); do
  kubectl -n kube-system get deployment metrics-server >/dev/null 2>&1 && break
  sleep 2
done
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

cat <<EOF

Cluster ready.
  API entry point: $BASE_URL
  Next:            ./deploy/k8s/scripts/deploy.sh (or ./scripts/deploy.sh up with DEPLOY_MODE=k8s)
EOF
