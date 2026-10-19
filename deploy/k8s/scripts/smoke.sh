#!/usr/bin/env bash
# Health checks and one real question through the nginx entry point (no jq needed).
#   ./deploy/k8s/scripts/smoke.sh
#   BASE_URL=https://assistant.example.com QUESTION="..." ./deploy/k8s/scripts/smoke.sh
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
QUESTION="${QUESTION:-How do I add new documents?}"

echo "== liveness =="
curl -fsS "$BASE_URL/health/live"; echo
echo "== readiness =="
curl -fsS "$BASE_URL/health/ready"; echo
echo "== UI config =="
curl -fsS "$BASE_URL/api/config" | head -c 300; echo

# JSON string of the question (backslashes and quotes escaped)
q="$(printf '%s' "$QUESTION" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
echo "== asking: $QUESTION =="
timeout 300 curl -fsS -N -X POST "$BASE_URL/api/chat" \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary "{\"question\": \"$q\"}" | sed -n '1,80p'
