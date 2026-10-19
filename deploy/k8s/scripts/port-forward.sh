#!/usr/bin/env bash
# Opens the entry point on http://localhost:$K8S_HOST_PORT (8081) for clusters without the kind port mapping
# (for example a remote cluster reached through kubectl).
set -euo pipefail
# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
kubectl -n "$NAMESPACE" port-forward svc/nginx "$HOST_PORT:80"
