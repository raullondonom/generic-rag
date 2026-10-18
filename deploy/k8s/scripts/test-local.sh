#!/usr/bin/env bash
# Local acceptance tests for the Kubernetes deployment.
#
#   ./deploy/k8s/scripts/test-local.sh
#   SCALE_TEST=1 ./deploy/k8s/scripts/test-local.sh     # also exercises the HPA
#
# The indexing test writes a temporary file into DOCS_HOST_DIR (examples/documents by
# default) and removes it at the end. Set DOCS_HOST_DIR to the folder mounted into the
# cluster if the cluster was created with a different DOCS_DIR.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
BASE_URL="${BASE_URL:-http://localhost:8081}"
NAMESPACE="${NAMESPACE:-transit-chat}"
DOCS_HOST_DIR="${DOCS_HOST_DIR:-$REPO_ROOT/examples/documents}"
SAMPLE_FILE="${SAMPLE_FILE:-example-user-guide.md}"
QUESTION="${QUESTION:-How do I add new documents?}"
TOKEN_FILE="$REPO_ROOT/deploy/k8s/.local-secrets.env"
TOKEN="${ADMIN_TOKEN:-$(sed -n 's/^ADMIN_TOKEN=//p' "$TOKEN_FILE" 2>/dev/null || true)}"

PASS=0
FAIL=0
SKIP=0
TEST_DOC=""

cleanup() {
  [ -n "$TEST_DOC" ] && rm -f "$TEST_DOC"
}
trap cleanup EXIT

section() { printf '\n== %s ==\n' "$1"; }
ok()   { PASS=$((PASS + 1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  FAIL %s\n' "$1"; }
skip() { SKIP=$((SKIP + 1)); printf '  skip %s\n' "$1"; }

http_status() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"
}

documents() {
  curl -s --max-time 30 -H "X-Admin-Token: $TOKEN" "$BASE_URL/api/documents"
}

section "Cluster"
if kubectl cluster-info >/dev/null 2>&1; then ok "kubectl can reach the cluster"; else bad "kubectl cannot reach the cluster"; fi
not_ready="$(kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null | awk '$2 != "1/1" && $3 != "Completed"')"
if [ -z "$not_ready" ]; then ok "all pods Ready (Completed jobs ignored)"; else bad "pods not ready: $not_ready"; fi
broken="$(kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null | awk '$3 == "CrashLoopBackOff" || $3 == "Error"')"
if [ -z "$broken" ]; then ok "no CrashLoopBackOff/Error pods"; else bad "broken pods: $broken"; fi
pending="$(kubectl -n "$NAMESPACE" get pvc --no-headers 2>/dev/null | awk '$2 != "Bound"')"
if [ -z "$pending" ]; then ok "all PVCs Bound"; else bad "PVCs not bound: $pending"; fi

section "Services"
for svc in api nginx scheduler postgres qdrant embeddings ollama; do
  addrs="$(kubectl -n "$NAMESPACE" get endpoints "$svc" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null)"
  if [ -n "$addrs" ]; then ok "Service/$svc has endpoints"; else bad "Service/$svc has no endpoints"; fi
done

section "Application"
[ "$(http_status "$BASE_URL/health/live")" = "200" ] && ok "/health/live" || bad "/health/live"
[ "$(http_status "$BASE_URL/health/ready")" = "200" ] && ok "/health/ready" || bad "/health/ready"
ready="$(curl -s --max-time 30 "$BASE_URL/api/config" | jq -r '.ready' 2>/dev/null)"
[ "$ready" = "true" ] && ok "/api/config ready=true" || bad "/api/config ready=$ready"
[ "$(http_status "$BASE_URL/admin")" = "200" ] && ok "/admin" || bad "/admin"
[ "$(http_status "$BASE_URL/api/file?path=$SAMPLE_FILE")" = "200" ] && ok "/api/file serves the corpus" || bad "/api/file"
[ "$(http_status "$BASE_URL/api/chat/jobs/abc")" = "404" ] && ok "queue endpoints disabled (expected in this deployment)" || bad "unexpected /api/chat/jobs response"

section "Administration"
if [ -z "$TOKEN" ]; then
  skip "admin token not found (deploy/k8s/.local-secrets.env)"
else
  [ "$(http_status -H "X-Admin-Token: $TOKEN" "$BASE_URL/api/admin/check")" = "200" ] && ok "admin check with token" || bad "admin check with token"
  [ "$(http_status "$BASE_URL/api/documents")" = "401" ] && ok "admin check without token is refused" || bad "admin check without token"
  if documents | jq -e --arg l "$SAMPLE_FILE" '.[] | select(.location == $l and .status == "indexed")' >/dev/null 2>&1; then
    ok "sample document is indexed ($SAMPLE_FILE)"
  else
    bad "sample document is not indexed ($SAMPLE_FILE)"
  fi
fi

section "Answer (SSE)"
tmp="$(mktemp)"
curl -sN --max-time 240 -X POST "$BASE_URL/api/chat" \
  -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg q "$QUESTION" '{question: $q}')" > "$tmp"
for event in meta sources token done; do
  if grep -q "^event: $event" "$tmp"; then ok "SSE event '$event'"; else bad "SSE event '$event' missing"; fi
done
rm -f "$tmp"

section "Persistence (Postgres PVC)"
before="$(kubectl -n "$NAMESPACE" exec postgres-0 -- psql -U rag -d rag -tAc 'select count(*) from messages' 2>/dev/null | tr -d '[:space:]')"
if [ -z "$before" ]; then
  bad "could not read the messages table"
else
  kubectl -n "$NAMESPACE" rollout restart deployment/api >/dev/null
  if kubectl -n "$NAMESPACE" rollout status deployment/api --timeout=240s >/dev/null; then
    after="$(kubectl -n "$NAMESPACE" exec postgres-0 -- psql -U rag -d rag -tAc 'select count(*) from messages' 2>/dev/null | tr -d '[:space:]')"
    [ "$before" = "$after" ] && ok "conversations survive an API restart ($after messages)" || bad "message count changed after restart ($before -> $after)"
  else
    bad "API did not become ready after the restart"
  fi
fi

section "Incremental indexing"
if [ -z "$TOKEN" ]; then
  skip "admin token not found"
elif [ ! -d "$DOCS_HOST_DIR" ] || [ ! -w "$DOCS_HOST_DIR" ]; then
  skip "DOCS_HOST_DIR is not a writable folder: $DOCS_HOST_DIR"
else
  name="k8s-local-test-$$.md"
  TEST_DOC="$DOCS_HOST_DIR/$name"
  printf '# Kubernetes local test\n\nThe local test code is ORION-%s.\n' "$$" > "$TEST_DOC"
  found=0
  for _ in $(seq 1 30); do
    if documents | jq -e --arg l "$name" '.[] | select(.location == $l and .status == "indexed")' >/dev/null 2>&1; then found=1; break; fi
    sleep 5
  done
  [ "$found" = 1 ] && ok "new document indexed ($name)" || bad "new document not indexed within 150s"
  rm -f "$TEST_DOC"; TEST_DOC=""
  removed=0
  for _ in $(seq 1 30); do
    if documents | jq -e --arg l "$name" '.[] | select(.location == $l)' >/dev/null 2>&1; then
      sleep 5
    else
      removed=1; break
    fi
  done
  [ "$removed" = 1 ] && ok "deleted document removed from the index" || bad "deleted document still in the index after 150s"
fi

section "Autoscaling"
targets="$(kubectl -n "$NAMESPACE" get hpa api --no-headers 2>/dev/null | awk '{print $3" "$4}')"
case "$targets" in
  ""|*unknown*) bad "HPA api has no metrics ($targets)";;
  *) ok "HPA api reports metrics ($targets)";;
esac

if [ "${SCALE_TEST:-0}" = "1" ]; then
  section "Scale test (HPA)"
  for i in 1 2 3; do
    kubectl -n "$NAMESPACE" run "k8s-test-load-$i" --image=genric-rag-app:local --restart=Never --command -- \
      python -c $'import time,urllib.request\nend=time.time()+45\nwhile time.time()<end:\n    urllib.request.urlopen("http://api:8000/api/config").read()' >/dev/null
  done
  sleep 45
  replicas="$(kubectl -n "$NAMESPACE" get deployment api -o jsonpath='{.status.readyReplicas}' 2>/dev/null)"
  [ "${replicas:-0}" -ge 3 ] && ok "HPA scaled api to $replicas replicas" || bad "HPA did not scale (ready replicas: ${replicas:-0})"
  kubectl -n "$NAMESPACE" delete pod k8s-test-load-1 k8s-test-load-2 k8s-test-load-3 --ignore-not-found >/dev/null
  echo "  note: the API scales down after the 5 minute stabilization window"
fi

printf '\nResults: %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
exit "$FAIL"
