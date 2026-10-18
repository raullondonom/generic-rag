#!/usr/bin/env bash
# Opens the entry point on http://localhost:8081 for clusters without the kind port mapping
# (for example a remote cluster reached through kubectl).
set -euo pipefail
NAMESPACE="${NAMESPACE:-transit-chat}"
HOST_PORT="${HOST_PORT:-8081}"
kubectl -n "$NAMESPACE" port-forward svc/nginx "$HOST_PORT:80"
