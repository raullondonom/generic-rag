#!/usr/bin/env bash
# Runs the embeddings service (TEI) with plain Docker on its own machine, for a cluster that reaches it through
# K8S_EMBEDDINGS_URL: the model is the biggest piece of the stack, and on its own server the cluster fits in
# a smaller one. Same model as the cluster (EMBEDDING_MODEL in .env); the weights stay in the Docker volume.
#
#   ./deploy/k8s/scripts/embeddings-host.sh up | down | status
#
#   EMBEDDINGS_BIND    address to listen on (default 127.0.0.1). On a server, its private network address:
#                      TEI has no authentication, so never a public one
#   EMBEDDINGS_PORT    port (default 8090); the cluster then uses K8S_EMBEDDINGS_URL=http://<bind>:<port>
#   EMBEDDINGS_CPUS / EMBEDDINGS_MEMORY_LIMIT  optional Docker limits, e.g. 2 / 3500m; the model threads
#                      (RAYON_NUM_THREADS) follow the CPU limit, or the machine's CPUs without one
set -euo pipefail

# shellcheck source=env.sh
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
export MSYS_NO_PATHCONV=1
NAME="${EMBEDDINGS_CONTAINER:-rag-embeddings}"
BIND="$(setting EMBEDDINGS_BIND EMBEDDINGS_BIND 127.0.0.1)"
PORT="$(setting EMBEDDINGS_PORT EMBEDDINGS_PORT 8090)"
CPUS="$(setting EMBEDDINGS_CPUS EMBEDDINGS_CPUS "")"
MEMORY="$(setting EMBEDDINGS_MEMORY_LIMIT EMBEDDINGS_MEMORY_LIMIT "")"
MODEL="$(setting EMBEDDING_MODEL EMBEDDING_MODEL Qwen/Qwen3-Embedding-0.6B)"
TEI_IMAGE="${TEI_IMAGE:-ghcr.io/huggingface/text-embeddings-inference:cpu-1.9}"

case "${1:-}" in
  up)
    if docker inspect "$NAME" >/dev/null 2>&1; then
      docker start "$NAME" >/dev/null
    else
      [ "$BIND" = 0.0.0.0 ] && echo "Warning: TEI has no authentication; restrict port $PORT with a firewall." >&2
      threads="$(nproc 2>/dev/null || echo 2)"
      [ -n "$CPUS" ] && threads="$(awk -v c="$CPUS" 'BEGIN { t = int(c); if (t < c) t++; print (t < 1 ? 1 : t) }')"
      limits=()
      [ -n "$CPUS" ] && limits+=(--cpus "$CPUS")
      [ -n "$MEMORY" ] && limits+=(--memory "$MEMORY" --memory-swap "$MEMORY")
      HF_TOKEN="${HF_TOKEN:-$(env_file_get HF_TOKEN)}"
      # Same arguments as the in-cluster Deployment (base/embeddings.yaml)
      docker run -d --name "$NAME" --restart unless-stopped ${limits[@]+"${limits[@]}"} \
        -p "$BIND:$PORT:80" -v "$NAME-models:/data" \
        -e RAYON_NUM_THREADS="$threads" -e HF_TOKEN="${HF_TOKEN:-}" \
        "$TEI_IMAGE" --model-id "$MODEL" --max-client-batch-size 64 --max-batch-tokens 2048 >/dev/null
    fi
    echo "Waiting for $MODEL (the first start downloads the weights)..."
    for _ in $(seq 1 180); do
      if curl -fsS "http://$( [ "$BIND" = 0.0.0.0 ] && echo 127.0.0.1 || echo "$BIND" ):$PORT/health" >/dev/null 2>&1; then
        echo "Embeddings ready: K8S_EMBEDDINGS_URL=http://$BIND:$PORT"
        exit 0
      fi
      [ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || { docker logs --tail 30 "$NAME" >&2; exit 1; }
      sleep 5
    done
    echo "Not ready after 15 minutes: docker logs $NAME" >&2
    exit 1
    ;;
  down) docker rm -f "$NAME" >/dev/null 2>&1 || true; echo "Stopped $NAME (the weights stay in the volume $NAME-models)." ;;
  status) docker ps -a --filter "name=^$NAME\$"; docker stats --no-stream "$NAME" 2>/dev/null || true ;;
  *) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
