#!/usr/bin/env bash
# Deletes the local k3s cluster: its node container, its volume (the application data lives inside it) and
# its kubectl context.
set -euo pipefail
# shellcheck source=env.sh
K8S_DISTRO=k3s . "$(dirname "${BASH_SOURCE[0]}")/env.sh"
docker rm -f "k3s-$CLUSTER" >/dev/null 2>&1 || true
docker volume rm "k3s-$CLUSTER" >/dev/null 2>&1 || true
kubectl config delete-context "$KUBE_CONTEXT" >/dev/null 2>&1 || true
kubectl config delete-cluster "$KUBE_CONTEXT" >/dev/null 2>&1 || true
kubectl config delete-user "$KUBE_CONTEXT" >/dev/null 2>&1 || true
echo "Deleted k3s cluster '$CLUSTER'."
