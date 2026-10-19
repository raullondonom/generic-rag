# Kubernetes deployment

The application deployed as a small, self-contained stack: nginx in front, two stateless
API pods, one scheduler (indexing + administration), Postgres, Qdrant, one embeddings
service, one local Ollama and no extra moving parts.

```
                       ┌──────────────┐
  user ──────────────► │   nginx ×2   │  (rate limits, SSE, no buffering)
                       └──────┬───────┘
                 ┌────────────┴─────────────┐
          ┌──────▼──────┐            ┌──────▼─────────┐
          │   api ×2..6 │            │  scheduler ×1  │
          │ ROLE=api    │            │ ROLE=scheduler │
          └──┬───┬───┬──┘            └───────┬────────┘
             │   │   │                       │
   ┌─────────▼┐ ┌▼──────────┐ ┌─────────────▼┐
   │ Postgres │ │  Qdrant   │ │  embeddings  │  (TEI, local)
   └──────────┘ └───────────┘ └──────────────┘
                       │
                ┌──────▼──────┐
                │   Ollama    │  (local model, GPU optional)
                └─────────────┘
```

## What is deployed (and what is not)

| Component | Why |
|---|---|
| `nginx` (2 replicas) | Entry point: per-IP rate limits, SSE without buffering, friendly 429/503, admin routed to the scheduler. |
| `api` (2..6, HPA) | `ROLE=api`: answers questions. Stateless; reads Postgres, Qdrant, embeddings and Ollama. |
| `scheduler` (1) | `ROLE=scheduler`: folder indexing, source review, retention, `/admin` and `/api/reindex`. Exactly one instance (`Recreate`). |
| `postgres` | Conversations, feedback, spend, source registry, settings. |
| `qdrant` | Vector index (one node; replication is not needed at this size). |
| `embeddings` | Hugging Face TEI. `RAYON_NUM_THREADS` follows the CPU limit. |
| `ollama` + `ollama-pull` Job | Local model. The Job downloads the model into a PVC. |
| `metrics-server` (add-on) | Required by the HorizontalPodAutoscalers. |

**Intentionally not deployed:** the Redis queue, the Redis cache and the `worker` role.
They add durability and resumable SSE, but also two more services and a second app tier.
The code enables them only when `REDIS_QUEUE_URL` / `REDIS_CACHE_URL` are set, so the
switch to the HA topology of `docker-compose.ha.yml` remains available without code changes.

The only images required are `genric-rag-app`, `postgres`, `qdrant`, `text-embeddings-inference`,
`ollama` and `nginx`. Init containers reuse the application image (no `busybox`).

## Requirements

- A Kubernetes cluster (local: kind; cloud: any managed cluster).
- `kubectl` and `kind` for the local option (`scripts/install-tools.sh` installs both).
- Docker for building/loading the application image.
- A node with **at least 4 CPUs and 8 GB of RAM** for the base manifests (16 GB recommended
  if Ollama runs on CPU). See [Sizing](#sizing).

## Local cluster (kind)

```bash
./deploy/k8s/scripts/install-tools.sh   # kubectl + kind in ~/.local/bin
./deploy/k8s/scripts/kind-up.sh         # cluster + metrics-server, UI on :8081
./deploy/k8s/scripts/deploy.sh          # build, load images, apply manifests
./deploy/k8s/scripts/smoke.sh           # health + one question
```

The local overview mounts `examples/documents` into the node (read-only) and uses
`qwen3:0.6b` so the CPU test is usable. The first `deploy.sh` downloads the model and the
embedding weights into PVCs; it can take several minutes.

To use the full corpus instead of the examples, recreate the cluster with
`DOCS_DIR="$PWD/documents" ./deploy/k8s/scripts/kind-up.sh` (the first index is slower on CPU).

Open <http://localhost:8081> and <http://localhost:8081/admin>. The admin token is printed
by `scripts/secrets.sh` and stored in `deploy/k8s/.local-secrets.env` (gitignored).

Useful commands:

```bash
./deploy/k8s/scripts/status.sh          # pods, services, HPAs, PVCs, CPU/memory
kubectl -n transit-chat logs -f deployment/scheduler
kubectl -n transit-chat get hpa
./deploy/k8s/scripts/kind-down.sh       # delete the whole local cluster
```

## Cloud cluster

1. Push the image to a registry and update `deploy/k8s/overlays/cloud/kustomization.yaml`
   (`images.newName` / `newTag`).
2. Choose a storage class for the `documents` PVC. It must be readable by every `api`,
   `scheduler` pod: use `ReadWriteMany` (EFS, Azure Files, CephFS, NFS...) or a single-node
   cluster with `ReadWriteOnce`. Edit `overlays/cloud/documents-pvc.yaml`.
3. Apply:

   ```bash
   kubectl apply -f deploy/k8s/base/namespace.yaml
   ./deploy/k8s/scripts/secrets.sh          # reads .env or the current environment
   kubectl apply -k deploy/k8s/overlays/cloud
   ```

   `secrets.sh` creates `rag-secrets` with `POSTGRES_PASSWORD`, `ADMIN_TOKEN`,
   `ANTHROPIC_API_KEY` (empty for a local-only model), `CONFLUENCE_TOKEN` and `HF_TOKEN`.
   In the cloud, export these variables (or create the Secret with your secret manager)
   instead of keeping `.local-secrets.env`.

4. Point the `nginx` Service (LoadBalancer) at your DNS and terminate TLS at the cloud
   load balancer or with an Ingress in front of `nginx:80`.
5. Copy the corpus into the `documents` PVC (the app only reads it). For example:

   ```bash
   kubectl -n transit-chat run docs-loader --image=alpine --restart=Never \
     --overrides='{"spec":{"containers":[{"name":"docs-loader","image":"alpine","command":["sleep","3600"],
       "volumeMounts":[{"name":"docs","mountPath":"/docs"}]}],
       "volumes":[{"name":"docs","persistentVolumeClaim":{"claimName":"documents"}}]}}'
   kubectl -n transit-chat cp ./documents/. docs-loader:/docs
   kubectl -n transit-chat delete pod docs-loader
   ```

   For very large corpora, populate the volume with `rclone`/`aws s3 sync` from a Job, or mount
   an NFS export directly.

6. Check `kubectl -n transit-chat get hpa` and `kubectl top pods`. HPA needs
   `metrics-server` (present in most managed clusters; install it if not).

## Configuration and secrets

- **`base/configmap.yaml`** — non-secret settings (model, retrieval, chunking, indexing,
  language). Changing it does not restart the pods: after editing, run
  `RESTART=1 ./deploy/k8s/scripts/deploy.sh` (avoid it while the scheduler is indexing) or
  `kubectl -n transit-chat rollout restart deployment/api deployment/scheduler`.
- **`rag-secrets`** — `POSTGRES_PASSWORD`, `ADMIN_TOKEN`, `ANTHROPIC_API_KEY`,
  `CONFLUENCE_TOKEN`, `HF_TOKEN`. `ADMIN_TOKEN` is required: without it the app only accepts
  admin requests from loopback, which no longer matches a request through nginx.
- **Local model / Claude** — with `LLM_PROVIDER=auto` (default), setting `ANTHROPIC_API_KEY`
  switches answers to Claude and keeps Ollama available only if `OLLAMA_WITH_CLAUDE=true`.
  No manifest change is needed to go back and forth.

## Scaling

- `api` and `nginx` have HPAs on CPU (2..6 and 2..4). `metrics-server` is required.
- The **bottleneck is the model**, not the API: a single Ollama instance serves one GPU. More
  `api` pods do not add tokens/s. To add capacity, add GPU nodes/replicas of `ollama`, or use
  Claude (then the API tier scales horizontally).
- The scheduler must stay at **1 replica**. It uses `Recreate` so a rolling update never runs
  two indexers at the same time.
- If you need the durable queue (burst protection, resumable SSE, `429` shedding), the app
  already supports it: add Redis and a `ROLE=worker` Deployment (see `docker-compose.ha.yml`
  for the environment), and scale workers by queue depth.

## Sizing

Base requests: ~5 CPU and ~8 GiB (TEI 2 CPU/2 GiB + Ollama 1 CPU/4 GiB + the rest).
On a 4-core / 8 GiB node it fits with little headroom; 16 GiB is recommended when Ollama
runs on CPU. If the node is small, lower the TEI/Ollama requests in a Kustomize patch or use
Claude for answers.

The embeddings container uses `RAYON_NUM_THREADS` from its CPU **limit** (Downward API).
Do not lower the CPU limit without also lowering the model size: at 1 vCPU a query can take
10+ seconds; at 4 vCPU it is ~1 second.

## GPU (optional)

The base deployment runs Ollama on CPU. On an NVIDIA node:

```bash
kubectl -n transit-chat patch deployment ollama --type=strategic \
  --patch-file deploy/k8s/optional/ollama-gpu-nvidia.patch.yaml
```

This requires the NVIDIA device plugin/runtime class. For AMD (ROCm), install the AMD GPU
device plugin and change `nvidia.com/gpu` to `amd.com/gpu` (Ollama's ROCm image is selected
automatically by the same manifest).

Embeddings on GPU (NVIDIA only; TEI has no ROCm image for consumer Radeon):

```bash
kubectl -n transit-chat patch deployment embeddings --type=strategic \
  --patch-file deploy/k8s/optional/embeddings-gpu-nvidia.patch.yaml
```

Choose the TEI tag that matches the GPU (89, 86, turing, hopper) in the patch.

## DeepSeek instead of a local model

DeepSeek is called through its OpenAI-compatible API; the embeddings stay local. The
`deepseek-local` and `deepseek-cloud` overlays remove the Ollama Deployment, Service, PVC and
model-pull Job (the `components/no-ollama` component); `deploy.sh` also deletes the existing
Ollama resources, because `kubectl apply` does not prune what is no longer in the manifests:

```bash
# Put the key in .env (DEEPSEEK_API_KEY=sk-...) or export it, then:
./deploy/k8s/scripts/secrets.sh
OVERLAY=deepseek-cloud ./deploy/k8s/scripts/deploy.sh   # managed cluster
# OVERLAY=deepseek-local ./deploy/k8s/scripts/deploy.sh  # kind
```

The overlay sets `LLM_PROVIDER=deepseek` and `OLLAMA_URL=""`; the settings in
`base/configmap.yaml` select the model:

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_URL` | `https://api.deepseek.com` | OpenAI-compatible base URL |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Answers (`deepseek-v4-pro` is slower and more expensive) |
| `DEEPSEEK_FAST_MODEL` | `deepseek-flash` | Search preparation/reranking; it must support JSON mode |
| `DEEPSEEK_MAX_TOKENS` | `8000` | Maximum answer length |
| `DEEPSEEK_THINKING` | `false` | Thinking mode: slower and more output tokens |

- The spend of every call is recorded and capped by `MONTHLY_BUDGET_USD`; update the DeepSeek
  prices in `app/rag/prices.yaml` if they change.
- `/health` checks the `/models` endpoint with the key, and `/admin` offers DeepSeek as the
  provider.
- **Privacy:** the question and the retrieved excerpts are sent to DeepSeek. Secrets are
  redacted before sending (and the corpus was redacted at indexing time), but this is no
  longer a fully local setup.
- To go back to the local model: `OVERLAY=local` (or `cloud`) and redeploy; the model-pull Job
  downloads the model again. The base deployment keeps Ollama; only the `deepseek-*` overlays
  remove it.
- `deepseek-local` pins `FOLDER_SCAN_INTERVAL=0` so a restored index is not reconciled
  automatically. Remove it from `overlays/deepseek-local/patch-config.yaml` (or call
  `POST /api/reindex`) when you add documents to a fresh local cluster.

## Operations

### Indexing status

```bash
# Running, finished or failed, with the last message ("N procesados, ...").
kubectl -n transit-chat exec postgres-0 -- psql -U rag -d rag -c \
  "select source, status, started_at, finished_at, message from sync_runs;"

# Documents by status and chunks written.
kubectl -n transit-chat exec postgres-0 -- psql -U rag -d rag -c \
  "select status, count(*), sum(chunks) from documents group by status;"

# Same view as the administration panel, plus the vector count.
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" http://<entry>/api/admin/status \
  | jq '{chunks_in_index, sources, folder}'

# Progress while it runs.
kubectl -n transit-chat logs -f deployment/scheduler | grep -E "Indexed|Folder:"
```

`sync_runs.status` is `running` while the sync is in progress, `ok` when it finished and
`error` when it failed. `folder.running` in `/api/admin/status` answers the same question.
Errors appear per document in `/api/documents?status=error` (with the `X-Admin-Token` header).

```bash
# Update the application image
kubectl -n transit-chat set image deployment/api app=ghcr.io/.../transit-supporter-chat:1.3.0
kubectl -n transit-chat set image deployment/scheduler app=ghcr.io/.../transit-supporter-chat:1.3.0

# Reindex everything (admin token required)
curl -X POST http://<entry>/api/reindex -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"source":"all","full":true}'

# Logs
kubectl -n transit-chat logs deployment/api
kubectl -n transit-chat logs deployment/scheduler

# Backup (outside the cluster): pg_dump + Qdrant snapshot
kubectl -n transit-chat exec postgres-0 -- pg_dump -U rag -d rag --no-owner | gzip > postgres.sql.gz
```

The index can always be rebuilt from `documents/`; Postgres holds conversations, feedback
and the source registry. Copy `scripts/backup-ha.sh` into a CronJob if you want the same
backup as the compose deployment.

### Restoring a Postgres dump and a Qdrant snapshot

The dump and the snapshot must come from the same backup, and the `documents` volume must
contain the same corpus; otherwise the scheduler reconciles the index and removes what it
does not see.

```bash
# 1. Stop indexing while the data is replaced.
kubectl -n transit-chat scale deployment/scheduler --replicas=0

# 2. Postgres (custom-format dump).
kubectl -n transit-chat cp postgres.dump postgres-0:/tmp/db.dump
kubectl -n transit-chat exec postgres-0 -- \
  pg_restore -U rag -d rag --clean --if-exists --no-owner /tmp/db.dump

# 3. Qdrant. <collection> is settings.index_collection in Postgres
#    (for example documents__qwen-qwen3-embedding-0-6b__bm25).
POD=$(kubectl -n transit-chat get pod -l app=api -o jsonpath='{.items[0].metadata.name}')
kubectl -n transit-chat exec "$POD" -c app -- python -c \
  "import httpx; print(httpx.delete('http://qdrant:6333/collections/<collection>').status_code)"
kubectl -n transit-chat cp qdrant.snapshot "$POD":/tmp/qdrant.snapshot
kubectl -n transit-chat exec -i "$POD" -c app -- python - <<'PY'
import httpx
with open("/tmp/qdrant.snapshot", "rb") as f:
    r = httpx.post("http://qdrant:6333/collections/<collection>/snapshots/upload?priority=snapshot",
                   files={"snapshot": ("snapshot", f, "application/octet-stream")}, timeout=900)
print(r.status_code, r.text)
PY

# 4. Verify and start again.
kubectl -n transit-chat exec postgres-0 -- psql -U rag -d rag -c \
  "select value from settings where key='index_collection';"
kubectl -n transit-chat exec "$POD" -c app -- python -c \
  "import httpx; print(httpx.get('http://qdrant:6333/collections/<collection>').json()['result']['points_count'])"
kubectl -n transit-chat scale deployment/scheduler --replicas=1
```

If the corpus changed since the backup, the scheduler reindexes the changed files on its
next scan. To keep the restored index untouched, set `FOLDER_SCAN_INTERVAL=0` in the
ConfigMap and restart the scheduler; `POST /api/reindex` still works when you want to
reconcile it. Applying an overlay again resets that value to the one in the manifests (the
`deepseek-local` overlay already sets `0`), so re-apply it or add it to a local patch after
every deploy.

## Troubleshooting

| Symptom | Check |
|---|---|
| `api` not ready | `kubectl logs deployment/api`; `/health/ready` checks Postgres (and Redis if configured). |
| Embeddings slow / timeouts | `kubectl logs deployment/embeddings` (`inference_time`); verify the CPU limit and `RAYON_NUM_THREADS`. |
| Scheduler does not index | `kubectl logs deployment/scheduler`; it waits for Qdrant and retries embeddings every minute. |
| Model answers "not found" | `kubectl logs job/ollama-pull`; check `kubectl -n transit-chat exec deployment/ollama -- ollama list`. |
| 403 in `/admin` | `ADMIN_TOKEN` is missing from `rag-secrets`. |
| HPA shows `<unknown>` | `metrics-server` is not ready. |
| PVC stuck Pending | The cloud overlay uses `nfs-client`; replace it with an existing `ReadWriteMany` class. |
