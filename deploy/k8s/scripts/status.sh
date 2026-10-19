#!/usr/bin/env bash
# Overview of the deployment: pods, services, HPAs, volumes and CPU/memory.
set -euo pipefail
# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

kubectl -n "$NAMESPACE" get pods -o wide
echo
kubectl -n "$NAMESPACE" get svc,hpa,pvc
echo
kubectl -n "$NAMESPACE" top pods 2>/dev/null || echo "(metrics-server is not ready yet)"
