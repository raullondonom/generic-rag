#!/usr/bin/env bash
# Deletes the local kind cluster (the application data lives inside it).
set -euo pipefail
# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
kind delete cluster --name "$CLUSTER"
