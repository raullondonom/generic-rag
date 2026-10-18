#!/usr/bin/env bash
# Health checks and one real question through the nginx entry point.
#   ./deploy/k8s/scripts/smoke.sh
#   BASE_URL=https://assistant.example.com ./deploy/k8s/scripts/smoke.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8081}"
QUESTION="${QUESTION:-How do I add new documents?}"

echo "== liveness =="
curl -fsS "$BASE_URL/health/live"; echo
echo "== readiness =="
curl -fsS "$BASE_URL/health/ready"; echo
echo "== UI config =="
curl -fsS "$BASE_URL/api/config" | jq '{name, ready, queue}'

echo "== asking: $QUESTION =="
timeout 300 curl -fsS -N -X POST "$BASE_URL/api/chat" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg q "$QUESTION" '{question: $q}')" | sed -n '1,80p'
