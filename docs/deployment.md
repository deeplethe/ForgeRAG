# Deployment Guide

This guide covers deploying OpenCraig with Docker, production configuration, and common deployment patterns.

## Docker (Recommended)

The fastest way to deploy OpenCraig in production.

### Quick Start

```bash
# 1. Clone
git clone https://github.com/opencraig/opencraig.git
cd opencraig

# 2. Pick passwords (LLM key is collected by the in-app wizard later)
cp .env.example .env
$EDITOR .env

# 3. Start
docker compose up -d

# 4. Open http://localhost:8000 — first-boot wizard guides you through
#    LLM provider selection (SiliconFlow / OpenAI / Ollama / ...) and
#    auto-creates the first admin account.
```

That's it. No CLI wizard, no yaml editing — the web wizard at
`/setup` collects whatever's missing and writes the config overlay
on its own. Operators who prefer declarative config can skip the
wizard by setting LLM credentials in `.env` upfront; see
"Manual Configuration" below.

### What's Included

The default `docker-compose.yml` provides three services, all
default-on:

| Service | Image | Purpose |
|---------|-------|---------|
| **opencraig** | Built from `Dockerfile` | OpenCraig backend + frontend (port 8000) |
| **postgres** | `pgvector/pgvector:pg16` | PostgreSQL 16 with pgvector extension (relational + vector store) |
| **neo4j** | `neo4j:5.20-community` | Knowledge graph (ports 7474 web UI / 7687 bolt) |

Required env vars (compose fails fast if unset, no defaults to
"opencraig/opencraig" weak credentials):

* `POSTGRES_PASSWORD`
* `NEO4J_PASSWORD`
* At least one of: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `DEEPSEEK_API_KEY` (or set via the in-app wizard later)

### First-boot wizard (no yaml editing)

Once the stack is up, point a browser at `http://localhost:8000`.
The frontend probes `/api/v1/setup/status`; an unconfigured deploy
bounces to `/setup`, where you pick a model-platform preset:

| Preset | What it configures with one API key |
|---|---|
| **SiliconFlow** | DeepSeek-V3 chat + BGE-M3 embeddings + BGE reranker (CN default; cheapest unified-platform option) |
| **OpenAI** | gpt-4o-mini + text-embedding-3-small |
| **DeepSeek 官方** | DeepSeek-V3 chat (pair with another provider for embeddings) |
| **Anthropic** | Claude family chat (pair with another provider for embeddings) |
| **Ollama** | Fully self-hosted; data never leaves your network |
| **Custom** | Skip presets; configure each provider in Settings → System after registration |

The wizard tests the key with a 1-token chat completion before
applying. On apply, the config is written to a yaml overlay
(`storage/setup-overlay.yaml`) and the container restarts to load
it.

### Manual configuration (skip the wizard)

If you'd rather pin everything declaratively:

**1. Set LLM credentials in `.env`:**

```bash
POSTGRES_PASSWORD=...
NEO4J_PASSWORD=...
OPENAI_API_KEY=sk-...
```

**2. Edit `docker/config.yaml`:**

The default config wires Postgres + pgvector + Neo4j and points the
embedder + answer LLM at OpenAI. Swap models / providers there. See
[`docker/config.yaml`](../docker/config.yaml) for the template.

**3. Start:**

```bash
docker compose up -d
docker compose logs -f opencraig          # watch alembic + lifespan boot
```

Once the LLM credentials are valid, `/setup/status` returns
`configured=true` and the wizard step is skipped — the operator
proceeds directly to `/register`.

### Service shape

The actual `docker-compose.yml` is the source of truth — this
section is just a quick reference for what each volume / port /
service is for. If it conflicts with the YAML, the YAML wins.

| Volume | Mount | Purpose |
|--------|-------|---------|
| `storage` | `/app/storage` | Uploaded files, parser cache, embedding cache, setup-overlay yaml |
| `pgdata` | `/var/lib/postgresql/data` | PostgreSQL data + pgvector index |
| `neo4jdata` | `/data` | Neo4j graph + indexes |

| Port | Service | Notes |
|------|---------|-------|
| `8000` | opencraig | Backend + frontend (override via `OPENCRAIG_PORT` in `.env`) |
| `7474` | neo4j | Web UI for ops / debugging the KG (override via `NEO4J_HTTP_PORT`) |
| `7687` | neo4j | Bolt protocol — only the opencraig container talks to it (override via `NEO4J_BOLT_PORT`) |

Postgres is **not** exposed to the host by default. Add a
`ports:` block to the compose file — or use `docker-compose.dev.yml`,
which maps it to `5433` — when you need a local DB client.

### Common operations

```bash
# View logs
docker compose logs -f opencraig

# Restart after config change (pick up storage/setup-overlay.yaml)
docker compose restart opencraig

# Stop everything
docker compose down

# Stop and remove volumes (⚠️ destroys data)
docker compose down -v

# Rebuild after code change
docker compose build && docker compose up -d

# Backup / restore — see docs/operations/backup.md
./scripts/backup.sh
./scripts/restore.sh ./backups/<timestamp>.tar.gz
```

### Dockerfile

Multi-stage build:

1. **Stage 1 (Node 20 Alpine):** Builds Vue 3 frontend → `web/dist/`
2. **Stage 2 (Python 3.13-slim):** Installs Python dependencies, copies built frontend, runs `main.py` — defaults to 4 uvicorn workers (override via `OPENCRAIG_WORKERS`)

---

## Production Checklist

### Security

- [ ] Set strong passwords for PostgreSQL and Neo4j (use `secrets.token_urlsafe(32)`)
- [ ] Restrict CORS origins to your domain in `opencraig.yaml`
- [ ] Use HTTPS (reverse proxy with Nginx/Caddy)
- [ ] Keep API keys in environment variables, never in config files
- [ ] Set `files.max_bytes` to limit upload size

### Performance

- [ ] Use PostgreSQL + pgvector (not SQLite) for concurrent access
- [ ] Enable reranking for better precision (`retrieval.rerank.enabled: true`)
- [ ] Tune `retrieval.vector.top_k` and `retrieval.merge.candidate_limit` based on corpus size
- [ ] Consider `text-embedding-3-large` for better retrieval quality
- [ ] Set `parser.ingest_max_workers` based on available CPU cores

### Reliability

- [ ] Mount persistent volumes for PostgreSQL data and blob storage
- [ ] Set `restart: unless-stopped` on all services (default in compose file)
- [ ] Monitor health endpoints: `GET /api/v1/health`
- [ ] Set up log aggregation for `docker compose logs`
- [x] **Crash recovery is automatic** — on restart, documents stuck in intermediate states (`processing`, `parsing`, `structuring`, etc.) are detected, reset to `pending`, and re-queued for ingestion. No manual intervention needed

### Scaling

- [ ] Use `--workers N` for multiple Uvicorn workers (CPU-bound)
- [ ] Each Uvicorn worker runs its own `IngestionQueue`; stuck-document recovery ensures no jobs are lost across worker restarts
- [ ] Separate PostgreSQL to a managed database service for high availability
- [ ] Use S3/OSS for blob storage to decouple storage from compute
- [ ] Consider a managed Neo4j instance (Aura) for large knowledge graphs

---

## Reverse Proxy (Nginx)

Example Nginx configuration:

```nginx
server {
    listen 443 ssl http2;
    server_name rag.example.com;

    ssl_certificate     /etc/ssl/certs/rag.example.com.pem;
    ssl_certificate_key /etc/ssl/private/rag.example.com.key;

    client_max_body_size 200M;  # Match files.max_bytes

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE support (streaming answers)
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

---

## Using Ollama (Local LLM)

Run OpenCraig with a local Ollama instance (no API key needed):

**1. Install and start Ollama:**

```bash
# Pull required models
ollama pull qwen2.5
ollama pull bge-m3
```

**2. Configure OpenCraig:**

```yaml
embedder:
  backend: litellm
  dimension: 1024
  litellm:
    model: ollama/bge-m3
    api_base: http://localhost:11434  # or host.docker.internal for Docker

answering:
  generator:
    backend: litellm
    model: ollama/qwen2.5
    api_base: http://localhost:11434
```

**Docker note:** Use `http://host.docker.internal:11434` as the API base when OpenCraig runs in Docker and Ollama runs on the host.

---

## Database Migrations

OpenCraig uses Alembic for database schema migrations:

```bash
# Check current migration status
alembic current

# Apply all pending migrations
alembic upgrade head

# Generate a new migration after model changes
alembic revision --autogenerate -m "description"
```

Migrations are automatically applied on startup for SQLite. For PostgreSQL, run them manually before upgrading OpenCraig.

---

## Monitoring

### Health Check

```bash
curl http://localhost:8000/api/v1/health
```

### System Info

```bash
curl http://localhost:8000/api/v1/system/info
```

Returns backend versions, document count, storage usage, and configuration summary.

### API Documentation

- Swagger UI: [http://localhost:8000/docs](http://localhost:8000/docs)
- ReDoc: [http://localhost:8000/redoc](http://localhost:8000/redoc)
