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

## Settings: one `.env` for every deployment

The scripts read the repository's `.env` (the same file Docker Compose uses); a variable in the
environment wins over it. The defaults are neutral, so nothing has to be set for a local test:

| Variable | Default | Meaning |
|---|---|---|
| `DEPLOY_MODE` | `mini` | `k8s` makes `./scripts/deploy.sh up/down/status/logs/restart` act on this deployment |
| `K8S_OVERLAY` | `local` | `local` (kind), `cloud`, `deepseek-local`, `deepseek-cloud` |
| `K8S_NAMESPACE` | `rag` | Namespace of every object |
| `K8S_CLUSTER` | `rag` | kind cluster name |
| `K8S_HOST_PORT` | `8081` | kind: entry point on localhost |
| `K8S_IMAGE` | `genric-rag-app:local` | Application image; in the cloud, the one pushed to your registry |
| `DOCS_HOST_PATH` | `./documents` | kind: folder mounted read-only into the node when the cluster is created |
| `GPU` | `none` (`auto` = `none`) | `nvidia`: Ollama and embeddings on GPU (`TEI_GPU_IMAGE` picks the TEI tag); `amd`: Ollama with ROCm |
| `K8S_CONFIG_FROM_ENV` | `true` | `false` applies the manifests' values only |
| `K8S_EMBEDDINGS_CPU` / `K8S_EMBEDDINGS_CPU_LIMIT` / `K8S_EMBEDDINGS_MEMORY` | overlay | Size of the embeddings pod for a small node (the `local` overlay asks for 4 CPU, which a 4 vCPU machine cannot give besides Kubernetes itself), e.g. `1500m` / `3` / `5Gi`; `RAYON_NUM_THREADS` follows the CPU limit |

`deploy.sh` renders the overlay through `deploy/k8s/.generated/` (gitignored, written again on every
deploy). Every key of `base/configmap.yaml` that `.env` defines replaces the manifest's value
(language, models, retrieval, `ANSWER_FORMAT`, `FOLDER_SCAN_INTERVAL`...), except the in-cluster
addresses (`QDRANT_URL`, `EMBEDDING_URL`, `OLLAMA_URL`, `DOCS_PATH`) and, in the `deepseek-*` overlays,
`LLM_PROVIDER`. `RENDER_ONLY=1 ./deploy/k8s/scripts/deploy.sh` prints the result without a cluster.

## Local cluster (kind)

```bash
# .env: DEPLOY_MODE=k8s
./scripts/deploy.sh up                  # creates the kind cluster the first time, builds, loads, applies
./deploy/k8s/scripts/smoke.sh           # health + one question
./scripts/deploy.sh down                # stops the kind node; the data stays inside it
```

The same, step by step: `install-tools.sh` (kubectl + kind in `~/.local/bin`, Windows builds under
Git Bash), `kind-up.sh` (cluster + metrics-server), `deploy.sh` (build, load images, apply).

The cluster mounts `DOCS_HOST_PATH` into the node (read-only). The `local` overlay uses `qwen3:0.6b`
so the CPU test is usable, unless `.env` sets `OLLAMA_CHAT_MODEL` (the sample `.env.example` does).
The first `deploy.sh` downloads the model and the embedding weights into PVCs; it can take several
minutes. The folder is fixed when the cluster is created: to change it, delete the cluster
(`kind-down.sh`) and create it again.

On Docker Desktop with the containerd image store, `kind load` fails for multi-platform images
("content digest ... not found"); `deploy.sh` then imports only the node's platform.

Open <http://localhost:8081> and <http://localhost:8081/admin>. The admin token is printed
by `scripts/secrets.sh` and stored in `deploy/k8s/.local-secrets.env` (gitignored).

Useful commands:

```bash
./scripts/deploy.sh status              # pods, services, HPAs, PVCs, CPU/memory (status.sh)
./scripts/deploy.sh logs scheduler      # or api, embeddings, ollama, postgres, qdrant
kubectl -n rag get hpa
./deploy/k8s/scripts/kind-down.sh       # delete the whole local cluster, data included
```

## Cloud cluster

1. Push the image to a registry and set `K8S_IMAGE` (e.g. `ghcr.io/your-org/genric-rag-app:1.0.0`)
   and `K8S_OVERLAY=cloud` in `.env`.
2. Choose a storage class for the `documents` PVC. It must be readable by every `api`,
   `scheduler` pod: use `ReadWriteMany` (EFS, Azure Files, CephFS, NFS...) or a single-node
   cluster with `ReadWriteOnce`. Edit `overlays/cloud/documents-pvc.yaml`.
3. Apply:

   ```bash
   BUILD=0 ./scripts/deploy.sh up           # namespace, secrets (from .env or the environment), manifests
   ```

   `secrets.sh` creates `rag-secrets` with `POSTGRES_PASSWORD`, `ADMIN_TOKEN`,
   `ANTHROPIC_API_KEY` (empty for a local-only model), `CONFLUENCE_TOKEN` and `HF_TOKEN`.
   In the cloud, export these variables (or create the Secret with your secret manager)
   instead of keeping `.local-secrets.env`.

4. Point the `nginx` Service (LoadBalancer) at your DNS and terminate TLS at the cloud
   load balancer or with an Ingress in front of `nginx:80`.
5. Copy the corpus into the `documents` PVC (the app only reads it). For example:

   ```bash
   kubectl -n rag run docs-loader --image=alpine --restart=Never \
     --overrides='{"spec":{"containers":[{"name":"docs-loader","image":"alpine","command":["sleep","3600"],
       "volumeMounts":[{"name":"docs","mountPath":"/docs"}]}],
       "volumes":[{"name":"docs","persistentVolumeClaim":{"claimName":"documents"}}]}}'
   kubectl -n rag cp ./documents/. docs-loader:/docs
   kubectl -n rag delete pod docs-loader
   ```

   For very large corpora, populate the volume with `rclone`/`aws s3 sync` from a Job, or mount
   an NFS export directly.

6. Check `kubectl -n rag get hpa` and `kubectl top pods`. HPA needs
   `metrics-server` (present in most managed clusters; install it if not).

## Configuration and secrets

- **`base/configmap.yaml`** — non-secret settings (model, retrieval, chunking, indexing,
  language); the keys that `.env` defines replace them (see [Settings](#settings-one-env-for-every-deployment)).
  Changing them does not restart the pods: after editing, run
  `RESTART=1 ./deploy/k8s/scripts/deploy.sh` (avoid it while the scheduler is indexing) or
  `kubectl -n rag rollout restart deployment/api deployment/scheduler`.
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

The base deployment runs Ollama on CPU. `GPU=nvidia` (or `amd`) in `.env` adds the patches below on
every deploy. By hand, on an NVIDIA node:

```bash
kubectl -n rag patch deployment ollama --type=strategic \
  --patch-file deploy/k8s/optional/ollama-gpu-nvidia.patch.yaml
```

This requires the NVIDIA device plugin/runtime class. For AMD (ROCm), install the AMD GPU
device plugin and change `nvidia.com/gpu` to `amd.com/gpu` (Ollama's ROCm image is selected
automatically by the same manifest).

Embeddings on GPU (NVIDIA only; TEI has no ROCm image for consumer Radeon):

```bash
kubectl -n rag patch deployment embeddings --type=strategic \
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
K8S_OVERLAY=deepseek-cloud ./deploy/k8s/scripts/deploy.sh   # managed cluster
# K8S_OVERLAY=deepseek-local ./deploy/k8s/scripts/deploy.sh  # kind
```

The overlay sets `LLM_PROVIDER=deepseek` and `OLLAMA_URL=""`; the settings in
`base/configmap.yaml` select the model:

| Variable | Default | Meaning |
|---|---|---|
| `DEEPSEEK_URL` | `https://api.deepseek.com` | OpenAI-compatible base URL |
| `DEEPSEEK_MODEL` | `deepseek-flash` | Answers; the picker shows it as DeepSeek V4.1 Flash (`deepseek-v4-pro` is slower and more expensive) |
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
- `deepseek-local` sets `FOLDER_SCAN_INTERVAL=0` so a restored index is not reconciled
  automatically. A `FOLDER_SCAN_INTERVAL` in `.env` replaces it on every deploy; on a fresh local
  cluster set it to `60` there (or call `POST /api/reindex`) to index new documents.

## Operations

### Indexing status

```bash
# Running, finished or failed, with the last message ("N procesados, ...").
kubectl -n rag exec postgres-0 -- psql -U rag -d rag -c \
  "select source, status, started_at, finished_at, message from sync_runs;"

# Documents by status and chunks written.
kubectl -n rag exec postgres-0 -- psql -U rag -d rag -c \
  "select status, count(*), sum(chunks) from documents group by status;"

# Same view as the administration panel, plus the vector count.
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" http://<entry>/api/admin/status \
  | jq '{chunks_in_index, sources, folder}'

# Progress while it runs.
kubectl -n rag logs -f deployment/scheduler | grep -E "Indexed|Folder:"
```

`sync_runs.status` is `running` while the sync is in progress, `ok` when it finished and
`error` when it failed. `folder.running` in `/api/admin/status` answers the same question.
Errors appear per document in `/api/documents?status=error` (with the `X-Admin-Token` header).

```bash
# Update the application image
kubectl -n rag set image deployment/api app=ghcr.io/your-org/genric-rag-app:1.0.0
kubectl -n rag set image deployment/scheduler app=ghcr.io/your-org/genric-rag-app:1.0.0

# Reindex everything (admin token required)
curl -X POST http://<entry>/api/reindex -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"source":"all","full":true}'

# Logs
kubectl -n rag logs deployment/api
kubectl -n rag logs deployment/scheduler

# Backup (outside the cluster): pg_dump + Qdrant snapshot
kubectl -n rag exec postgres-0 -- pg_dump -U rag -d rag --no-owner | gzip > postgres.sql.gz
```

The index can always be rebuilt from `documents/`; Postgres holds conversations, feedback
and the source registry. Copy `scripts/backup-ha.sh` into a CronJob if you want the same
backup as the compose deployment.

### Restoring a Postgres dump and a Qdrant snapshot

The dump and the snapshot must come from the same backup, and the `documents` volume must
contain the same corpus; otherwise the scheduler reconciles the index and removes what it
does not see. Set `FOLDER_SCAN_INTERVAL=0` in `.env` and deploy first, then:

```bash
./deploy/k8s/scripts/restore.sh postgres.dump qdrant.snapshot
```

It stops the scheduler, restores Postgres, checks that the application uses the backup's collection
(otherwise the scheduler's first start would drop it, so it stops there), uploads the snapshot,
starts the scheduler and compares the chunks in Postgres with the points in Qdrant. Every copy into a
pod is checked with `md5sum`. Measured: 14,145 chunks (a 2.9 MB dump and a 191 MB snapshot) in 19 s.

The same steps by hand:

```bash
# 1. Stop indexing while the data is replaced.
kubectl -n rag scale deployment/scheduler --replicas=0

# 2. Postgres (custom-format dump).
kubectl -n rag cp postgres.dump postgres-0:/tmp/db.dump
kubectl -n rag exec postgres-0 -- \
  pg_restore -U rag -d rag --clean --if-exists --no-owner /tmp/db.dump

# 3. Qdrant. <collection> is settings.index_collection in Postgres
#    (for example documents__qwen-qwen3-embedding-0-6b__bm25).
POD=$(kubectl -n rag get pod -l app=api -o jsonpath='{.items[0].metadata.name}')
kubectl -n rag exec "$POD" -c app -- python -c \
  "import httpx; print(httpx.delete('http://qdrant:6333/collections/<collection>').status_code)"
kubectl -n rag cp qdrant.snapshot "$POD":/tmp/qdrant.snapshot
kubectl -n rag exec -i "$POD" -c app -- python - <<'PY'
import httpx
with open("/tmp/qdrant.snapshot", "rb") as f:
    r = httpx.post("http://qdrant:6333/collections/<collection>/snapshots/upload?priority=snapshot",
                   files={"snapshot": ("snapshot", f, "application/octet-stream")}, timeout=900)
print(r.status_code, r.text)
PY

# 4. Verify and start again.
kubectl -n rag exec postgres-0 -- psql -U rag -d rag -c \
  "select value from settings where key='index_collection';"
kubectl -n rag exec "$POD" -c app -- python -c \
  "import httpx; print(httpx.get('http://qdrant:6333/collections/<collection>').json()['result']['points_count'])"
kubectl -n rag scale deployment/scheduler --replicas=1
```

If the corpus changed since the backup, the scheduler reindexes the changed files on its
next scan. To keep the restored index untouched, set `FOLDER_SCAN_INTERVAL=0` in `.env` before
deploying (it is applied on every deploy, so it survives a redeploy) and restart the scheduler; `POST /api/reindex` still works when you want to
reconcile it. Applied by hand (`kubectl apply -k deploy/k8s/overlays/...`, without `deploy.sh`) the
manifests' value is used instead (60; the `deepseek-local` overlay sets `0`).

On Windows (Git Bash), run `kubectl cp` from the folder of the backup with a relative file name:
`kubectl cp C:/...` takes `C` as a pod name. Piping the file through `kubectl exec -i` truncated it in a
test, so check the size (or `md5sum`) in the pod before restoring.

## Troubleshooting

| Symptom | Check |
|---|---|
| `api` not ready | `kubectl logs deployment/api`; `/health/ready` checks Postgres (and Redis if configured). |
| Embeddings slow / timeouts | `kubectl logs deployment/embeddings` (`inference_time`); verify the CPU limit and `RAYON_NUM_THREADS`. |
| Scheduler does not index | `kubectl logs deployment/scheduler`; it waits for Qdrant and retries embeddings every minute. |
| Model answers "not found" | `kubectl logs job/ollama-pull`; check `kubectl -n rag exec deployment/ollama -- ollama list`. |
| 403 in `/admin` | `ADMIN_TOKEN` is missing from `rag-secrets`. |
| HPA shows `<unknown>` | `metrics-server` is not ready. |
| PVC stuck Pending | The cloud overlay uses `nfs-client`; replace it with an existing `ReadWriteMany` class. |
