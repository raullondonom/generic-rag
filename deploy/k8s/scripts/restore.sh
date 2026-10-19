#!/usr/bin/env bash
# Restores a Postgres dump (custom format) and a Qdrant snapshot taken from the same backup into the
# Kubernetes deployment, so it starts with its index instead of embedding every document again.
#
#   ./deploy/k8s/scripts/restore.sh <postgres.dump> <qdrant.snapshot>
#
# The documents mounted into the cluster must be the same corpus, and FOLDER_SCAN_INTERVAL=0 in .env keeps
# the scheduler from reconciling the restored index by itself (POST /api/reindex still works). Before the
# scheduler starts again, the script checks that the application uses the collection of the backup:
# otherwise its first start would drop the restored collection.
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
[ $# -eq 2 ] || { echo "Usage: $0 <postgres.dump> <qdrant.snapshot>" >&2; exit 1; }
DUMP="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
SNAPSHOT="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"
[ -f "$DUMP" ] && [ -f "$SNAPSHOT" ] || { echo "Backup files not found" >&2; exit 1; }
K=(kubectl -n "$NAMESPACE")

md5() { md5sum "$1" | cut -c1-32; }
# kubectl cp from the file's folder with a relative name: on Windows "C:/..." would be read as a pod name
copy_in() {  # copy_in <file> <pod> <path> [container]
  local file="$1" target="$2:$3"
  ( cd "$(dirname "$file")" && "${K[@]}" cp "$(basename "$file")" "$target" ${4:+-c "$4"} )
  local inside
  inside="$("${K[@]}" exec "$2" ${4:+-c "$4"} -- md5sum "$3" | cut -c1-32)"
  [ "$inside" = "$(md5 "$file")" ] || { echo "The copy of $(basename "$file") in $2 is incomplete" >&2; exit 1; }
}
api_python() { "${K[@]}" exec -i "$API" -c app -- python - "$@"; }

echo "1. Stopping the scheduler (no indexing while the data is replaced)"
"${K[@]}" scale deployment/scheduler --replicas=0 >/dev/null
"${K[@]}" wait --for=delete pod -l app=scheduler --timeout=120s >/dev/null 2>&1 || true

echo "2. Postgres: $(basename "$DUMP")"
copy_in "$DUMP" postgres-0 /tmp/db.dump
"${K[@]}" exec postgres-0 -- pg_restore -U rag -d rag --clean --if-exists --no-owner /tmp/db.dump
"${K[@]}" exec postgres-0 -- rm -f /tmp/db.dump
COLLECTION="$("${K[@]}" exec postgres-0 -- psql -U rag -d rag -tAc "select value from settings where key='index_collection'" | tr -d '[:space:]')"
CHUNKS="$("${K[@]}" exec postgres-0 -- psql -U rag -d rag -tAc "select coalesce(sum(chunks),0) from documents" | tr -d '[:space:]')"
echo "   collection $COLLECTION, $CHUNKS chunks in the documents table"

API="$("${K[@]}" get pod -l app=api -o jsonpath='{.items[0].metadata.name}')"
EXPECTED="$("${K[@]}" exec "$API" -c app -- python -c 'from rag.config import Settings; print(Settings().collection_name)' | tr -d '[:space:]')"
if [ "$EXPECTED" != "$COLLECTION" ]; then
  echo "The application uses collection '$EXPECTED' but the backup holds '$COLLECTION' (EMBEDDING_MODEL," >&2
  echo "SPARSE_SEARCH or COLLECTION_PREFIX differ). The scheduler stays stopped: fix .env and deploy again." >&2
  exit 1
fi

echo "3. Qdrant: $(basename "$SNAPSHOT")"
copy_in "$SNAPSHOT" "$API" /tmp/qdrant.snapshot app
api_python "$COLLECTION" <<'PY'
import sys, httpx
url = f"http://qdrant:6333/collections/{sys.argv[1]}"
httpx.delete(url, timeout=60)
with open("/tmp/qdrant.snapshot", "rb") as f:
    r = httpx.post(f"{url}/snapshots/upload?priority=snapshot",
                   files={"snapshot": ("snapshot", f, "application/octet-stream")}, timeout=900)
r.raise_for_status()
print("   points:", httpx.get(url, timeout=30).json()["result"]["points_count"])
PY
"${K[@]}" exec "$API" -c app -- rm -f /tmp/qdrant.snapshot

echo "4. Starting the scheduler (FOLDER_SCAN_INTERVAL=$("${K[@]}" get configmap rag-config -o jsonpath='{.data.FOLDER_SCAN_INTERVAL}'))"
"${K[@]}" scale deployment/scheduler --replicas=1 >/dev/null
"${K[@]}" rollout status deployment/scheduler --timeout=300s >/dev/null
POINTS="$(api_python "$COLLECTION" <<'PY'
import sys, httpx
print(httpx.get(f"http://qdrant:6333/collections/{sys.argv[1]}", timeout=30).json()["result"]["points_count"])
PY
)"
echo "Restored: $CHUNKS chunks in Postgres, $POINTS points in Qdrant after the scheduler started."
[ "$POINTS" = "$CHUNKS" ] || echo "Warning: the counts differ; check the scheduler logs." >&2
