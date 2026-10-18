#!/usr/bin/env bash
# Deletes the local kind cluster (the application data lives inside it).
set -euo pipefail
CLUSTER="${CLUSTER:-transit-chat}"
kind delete cluster --name "$CLUSTER"
