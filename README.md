# RAG genérico (carpeta local + Confluence)

Asistente de preguntas y respuestas que contesta **únicamente** con la información de:

- los archivos de una carpeta local (PDF, Word, Excel, PowerPoint, CSV, HTML, Markdown, texto, correos, OpenDocument, RTF, EPUB…), y
- las páginas y adjuntos de los espacios de **Confluence Cloud** configurados.

La IA puede ser **local** (Ollama, en GPU o CPU) o **remota** (Claude de Anthropic), y se elige desde la interfaz o por configuración.

## Reglas del asistente

- Solo responde con los documentos indexados y cita las fuentes `[n]`.
- No tiene acceso a internet: la IA no recibe ninguna herramienta, así que no puede buscar fuera.
- Si la información no está o la pregunta es ambigua, lo dice y **pide al usuario** los datos que faltan.
- Si ningún fragmento supera la similitud mínima (`MIN_SCORE`), ni siquiera se llama a la IA.
- Los embeddings se calculan siempre en local (`bge-m3`): los documentos no salen de la máquina al indexar. Solo los fragmentos relevantes para cada pregunta se envían a Claude cuando se usa ese proveedor.

## Arquitectura

```
documentos/ ─┐                       ┌─> Qdrant (base vectorial)
             ├─> rag-app (FastAPI) ──┤
Confluence ──┘   · ingesta           └─> Ollama (bge-m3 embeddings + qwen3:8b)
                 · API REST + UI web ───> Claude API (opcional)
```

| Servicio | Función | Puerto (solo localhost) |
|---|---|---|
| `rag-app` | API, interfaz web e ingesta | `8000` |
| `qdrant` | Base vectorial | `6333` (dashboard en `/dashboard`) |
| `ollama` | Modelos locales (GPU o CPU) | `11434` |
| `ollama-init` | Descarga los modelos y termina | — |
| `ngrok` | Túnel público protegido (opcional, perfil `ngrok`) | `4040` (panel) |

## Requisitos de hardware

| Escenario | CPU | RAM | GPU | Disco | Tiempo de respuesta |
|---|---|---|---|---|---|
| **Mínimo**: respuestas con Claude (API); embeddings locales en CPU | 4 núcleos x86-64 con AVX2 | 8 GB (6 GB para Docker) | No necesaria | 15 GB | 5–15 s |
| **Local sin GPU**: Ollama `qwen3:8b` en CPU | 8+ núcleos con AVX2 (Intel 10ª gen. / Ryzen 3000 o superior) | 16 GB (12 GB para Docker) | No | 20 GB | 30–90 s |
| **Recomendado**: Ollama en GPU | 8+ núcleos con AVX2 | 32 GB (16 GB para Docker) | NVIDIA con 8 GB de VRAM o más (RTX 3060/4060 o superior) y driver ≥ 550 | 20 GB SSD | 10–15 s |

Notas:

- Funciona en Windows 10/11 (Docker Desktop con WSL2), Linux y macOS. En Mac con Apple Silicon, Ollama dentro de Docker no usa la GPU, así que se comporta como el escenario "Local sin GPU".
- En equipos con poca RAM, usa un modelo local más pequeño (`OLLAMA_CHAT_MODEL=qwen3:4b`) o el proveedor `anthropic`.
- **Windows**: limita la memoria de WSL2 en `%USERPROFILE%\.wslconfig` (por ejemplo, `memory=16GB`) y deja un archivo de paginación de al menos 16 GB. En WSL2, la GPU también consume memoria de compromiso de Windows; si se agota, aparece el error `cudaMalloc failed: out of memory` aunque la GPU esté libre.
- Para comprobar la GPU en Docker: `docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi`.

## Prueba rápida (5 minutos + descarga de modelos)

```bash
git clone https://github.com/raullondonom/generic-rag.git
cd generic-rag
cp .env.example .env            # en Windows (PowerShell): copy .env.example .env
docker compose up -d --build
docker compose logs -f ollama-init   # la primera vez descarga ~6,5 GB de modelos; espera a que termine
```

1. Abre <http://localhost:8000>.
2. Pregunta sobre el documento de ejemplo incluido (`documentos/ejemplo-guia-de-uso.md`), por ejemplo: *"¿Cómo agrego documentos nuevos?"*. Debe responder citando la fuente `[1]`.
3. Comprueba las reglas con una pregunta ajena a los documentos, como *"¿Quién ganó el mundial 2022?"*. Debe responder que no tiene esa información y pedir más datos.
4. Copia tus propios archivos a `documentos/`: se indexan solos en menos de un minuto.

Opcional:

- **GPU NVIDIA**: descomenta `COMPOSE_PATH_SEPARATOR` y `COMPOSE_FILE` en `.env` y ejecuta `docker compose up -d`.
- **Claude**: define la API key (ver "Secretos") y elige "Claude (Anthropic)" en la interfaz.
- **Confluence**: configura las variables `CONFLUENCE_*` en `.env` (ver más abajo).

## Secretos

No se guardan en el repositorio. Docker Compose los lee de las variables de entorno del sistema o de `.env`, que está en `.gitignore`.

| Variable | Uso |
|---|---|
| `ANTHROPIC_API_KEY` o `GENRIC_RAG_ANTHROPIC_API_KEY` | API key de Claude. Es opcional: sin ella solo se ofrece Ollama |
| `CONFLUENCE_EMAIL` / `CONFLUENCE_TOKEN`, o `JIRA_EMAIL` / `JIRA_TOKEN` | Correo y API token de Atlassian (id.atlassian.com → Seguridad → Tokens de API) |
| `NGROK_AUTHTOKEN` | Solo para publicar la app con ngrok |

En Windows, después de crear una variable de entorno, abre una terminal nueva (o reinicia VS Code) para que la detecte.

## Configuración (`.env`)

Todas las opciones están documentadas en [.env.example](.env.example). Las más importantes son:

| Variable | Por defecto | Descripción |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | Proveedor por defecto (`ollama` / `anthropic`) |
| `OLLAMA_CHAT_MODEL` | `qwen3:8b` | Modelo local |
| `OLLAMA_NUM_GPU` / `OLLAMA_NUM_CTX` | `30` / `12288` | Capas en GPU y contexto, ajustados para 8 GB de VRAM |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Modelo de Claude |
| `ANTHROPIC_EFFORT` | `medium` | Profundidad de razonamiento de Claude |
| `CONFLUENCE_ENABLED` | `false` | Activa la sincronización con Confluence Cloud |
| `CONFLUENCE_URL` | — | Por ejemplo, `https://tu-empresa.atlassian.net/wiki` |
| `CONFLUENCE_SPACES` | — | Claves de los espacios a indexar (aparecen en la URL: `/wiki/spaces/<CLAVE>/`) |
| `CONFLUENCE_SYNC_MINUTES` | `60` | Frecuencia de sincronización de Confluence |
| `FOLDER_SCAN_INTERVAL` | `60` | Segundos entre revisiones de la carpeta |
| `TOP_K` / `MIN_SCORE` | `8` / `0.45` | Fragmentos recuperados y similitud mínima |
| `OCR_ENABLED` | `false` | OCR (Tesseract) para PDFs escaneados e imágenes |

Después de cambiar `.env`, aplica los cambios con `docker compose up -d`.
Si cambias `EMBED_MODEL`, `CHUNK_SIZE` o `CHUNK_OVERLAP`, reindexa todo desde cero (ver más abajo).

## Acceso externo con ngrok (pruebas)

La app no tiene login: por eso el túnel de ngrok se protege con usuario y contraseña (`NGROK_BASIC_AUTH`).

1. Crea la variable de entorno `NGROK_AUTHTOKEN` con tu authtoken (dashboard.ngrok.com → *Your Authtoken*).
2. Define `NGROK_BASIC_AUTH=usuario:contraseña` en `.env`. La contraseña debe tener al menos 8 caracteres.
3. Arranca el túnel:
   ```bash
   docker compose --profile ngrok up -d
   ```
4. Consulta la URL pública en <http://localhost:4040> o con `docker compose logs ngrok`.

Para cerrarlo, ejecuta `docker compose stop ngrok`. Ten en cuenta que quien acceda por ngrok consulta los documentos indexados y consume créditos de Claude.

## API REST

| Método | Ruta | Descripción |
|---|---|---|
| `POST` | `/api/chat` | `{question, history[], provider?}` → stream SSE con eventos `meta`, `sources`, `token`, `done` y `error` |
| `GET` | `/api/status` | Estado del índice, proveedores y última sincronización |
| `GET` | `/api/documents?source=&status=` | Documentos procesados (`indexed`, `unsupported`, `error`, `empty`) |
| `POST` | `/api/reindex` | `{source: folder\|confluence\|all, full: false}` |
| `GET` | `/api/file?path=` | Abre un archivo citado de la carpeta |
| `GET` | `/docs` | Documentación interactiva (Swagger) |

Ejemplo:

```bash
curl -N -X POST http://localhost:8000/api/chat -H "Content-Type: application/json" \
  -d '{"question": "¿Cómo se despliega a producción?", "provider": "anthropic"}'
```

## Operación

- **Ingesta incremental**: solo se procesan los archivos nuevos o modificados (por hash) y las páginas o adjuntos con una versión nueva. Lo que se borra del origen también se elimina del índice.
- **Reindexar todo desde cero**: `POST /api/reindex {"source": "all", "full": true}`, o bien `docker compose down` seguido de `docker volume rm genric-rag_qdrant_data genric-rag_rag_state`.
- **Archivos no soportados o con error**: aparecen en el panel lateral y en `GET /api/documents?status=error`.
- **Tests**: `docker compose run --rm --no-deps rag-app pytest -q`.
- **Error `cudaMalloc failed: out of memory`**: el modelo no cabe en la VRAM. Baja `OLLAMA_NUM_GPU` (capas en GPU) u `OLLAMA_NUM_CTX` en `.env` y ejecuta `docker compose up -d`.

## Licencia

Este proyecto se distribuye bajo la [PolyForm Noncommercial License 1.0.0](LICENSE).

- ✅ **Uso personal, privado, educativo, de investigación o por organizaciones sin ánimo de lucro**: permitido y gratuito, incluso modificándolo.
- ❌ **Uso comercial** (venderlo, ofrecerlo como servicio de pago o usarlo para obtener beneficios económicos): **no está permitido** sin una licencia comercial. Si te interesa, contacta con el autor en <https://github.com/raullondonom> para negociarla.

El texto legal vinculante es el del archivo [LICENSE](LICENSE), en inglés.
