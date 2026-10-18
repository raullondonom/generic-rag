#!/usr/bin/env bash
# Backup of the HA deployment: Postgres dump + Qdrant snapshot into ./backups (or BACKUP_DIR).
# The Qdrant copies protect against a node failing, not against a deletion or a lost server: this does.
# The index can also be rebuilt from documents/ (slow); Postgres holds conversations and ratings.
#   ./scripts/backup-ha.sh            keeps the last KEEP (default 7) backups
# Run it from cron, e.g.:  0 3 * * *  cd /path/to/repo && ./scripts/backup-ha.sh
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.ha.yml)
DIR="${BACKUP_DIR:-./backups}"
KEEP="${KEEP:-7}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DIR"

echo "[backup] Postgres -> $DIR/postgres-$STAMP.sql.gz"
"${COMPOSE[@]}" exec -T postgres pg_dump -U rag -d rag --no-owner | gzip > "$DIR/postgres-$STAMP.sql.gz"
test -s "$DIR/postgres-$STAMP.sql.gz"

echo "[backup] Qdrant snapshot -> $DIR/qdrant-$STAMP/"
mkdir -p "$DIR/qdrant-$STAMP"
"${COMPOSE[@]}" exec -T api-1 python - "$STAMP" <<'PY' | tar -x -C "$DIR/qdrant-$STAMP"
import io, sys, tarfile, httpx
base = "http://qdrant-1:6333"
names = [c["name"] for c in httpx.get(f"{base}/collections", timeout=30).json()["result"]["collections"]]
out = io.BytesIO()
with tarfile.open(fileobj=out, mode="w") as tar:
    for name in names:
        snap = httpx.post(f"{base}/collections/{name}/snapshots", timeout=600).json()["result"]["name"]
        data = httpx.get(f"{base}/collections/{name}/snapshots/{snap}", timeout=600).content
        info = tarfile.TarInfo(f"{name}.snapshot")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        httpx.delete(f"{base}/collections/{name}/snapshots/{snap}", timeout=60)
sys.stdout.buffer.write(out.getvalue())
PY
ls "$DIR/qdrant-$STAMP" | grep -q snapshot

echo "[backup] keeping the last $KEEP"
ls -1dt "$DIR"/postgres-*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
ls -1dt "$DIR"/qdrant-* 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -rf
echo "[backup] done"
