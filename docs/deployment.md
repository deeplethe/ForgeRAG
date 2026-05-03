# Deployment Guide

This guide covers deploying ForgeRAG with Docker, production configuration, and common deployment patterns.

## Docker (Recommended)

The fastest way to deploy ForgeRAG in production.

### Quick Start

```bash
# 1. Clone
git clone https://github.com/deeplethe/ForgeRAG.git
cd ForgeRAG

# 2. Run the setup wizard
python scripts/docker_setup.py

# 3. Start
docker compose up -d
```

The setup wizard generates `docker/config.yaml` and `.env`, then optionally starts the stack.

### What's Included

The default `docker-compose.yml` provides:

| Service | Image | Purpose |
|---------|-------|---------|
| **forgerag** | Built from `Dockerfile` | ForgeRAG backend + frontend |
| **postgres** | `pgvector/pgvector:pg16` | PostgreSQL 16 with pgvector extension |
| **neo4j** (optional) | `neo4j:5` | Knowledge graph store |

### Setup Wizard

The interactive wizard configures everything:

```bash
python scripts/docker_setup.py              # Interactive mode
python scripts/docker_setup.py --quick      # Accept all defaults (OpenAI, no Neo4j)
```

**What it asks:**

1. **LLM Provider** — OpenAI, DeepSeek, Ollama, or custom
2. **API Key** — checks environment first, prompts if missing
3. **Database Passwords** — auto-generates secure passwords
4. **Neo4j** — optional knowledge graph container
5. **Start now?** — optionally runs `docker compose up -d`

**Presets:**

| Provider | Chat Model | Embed Model | Dimension |
|----------|-----------|-------------|-----------|
| OpenAI | `openai/gpt-4o-mini` | `openai/text-embedding-3-small` | 1536 |
| DeepSeek | `deepseek/deepseek-v4-flash` | (use OpenAI / SiliconFlow / Ollama for embeddings — DeepSeek does not host an embedding model) | — |
| SiliconFlow | `openai/deepseek-ai/DeepSeek-V4-Pro` (api_base = `https://api.siliconflow.cn/v1`) | `openai/BAAI/bge-m3` (same api_base) | 1024 |
| Ollama | `ollama/qwen2.5` | `ollama/bge-m3` | 1024 |

### Manual Configuration

If you prefer manual setup:

**1. Create `.env`:**

```bash
OPENAI_API_KEY=sk-your-key-here
POSTGRES_PASSWORD=your-secure-password
# NEO4J_PASSWORD=your-neo4j-password  # Only if using Neo4j
```

**2. Edit `docker/config.yaml`:**

The default config uses PostgreSQL + pgvector. See [`docker/config.yaml`](../docker/config.yaml) for the template.

**3. Start:**

```bash
# Without Neo4j (default)
docker compose up -d

# With Neo4j
docker compose --profile neo4j up -d
```

### Docker Compose Services

#### ForgeRAG

```yaml
forgerag:
  build: .
  ports:
    - "8000:8000"
  environment:
    - FORGERAG_CONFIG=/app/config.yaml
    - OPENAI_API_KEY=${OPENAI_API_KEY}
    - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-forgerag}
  volumes:
    - storage:/app/storage
    - ./docker/config.yaml:/app/config.yaml:ro
  depends_on:
    postgres:
      condition: service_healthy
```

#### PostgreSQL with pgvector

```yaml
postgres:
  image: pgvector/pgvector:pg16
  environment:
    POSTGRES_USER: forgerag
    POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-forgerag}
    POSTGRES_DB: forgerag
  volumes:
    - pgdata:/var/lib/postgresql/data
  ports:
    - "5432:5432"
  healthcheck:
    test: ["CMD-SHELL", "pg_isready -U forgerag"]
    interval: 5s
    timeout: 3s
    retries: 5
```

#### Neo4j (Optional)

Started only with the `neo4j` profile:

```bash
docker compose --profile neo4j up -d
```

```yaml
neo4j:
  image: neo4j:5
  profiles: [neo4j]
  environment:
    NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-forgerag}
    NEO4J_PLUGINS: '["apoc"]'
  volumes:
    - neo4jdata:/data
  ports:
    - "7474:7474"   # HTTP browser
    - "7687:7687"   # Bolt protocol
```

### Dockerfile

Multi-stage build:

1. **Stage 1 (Node 20 Alpine):** Builds Vue 3 frontend → `web/dist/`
2. **Stage 2 (Python 3.11-slim):** Installs Python dependencies, copies built frontend, runs `main.py`

### Volumes

| Volume | Mount | Purpose |
|--------|-------|---------|
| `storage` | `/app/storage` | BM25 index, embedding cache |
| `pgdata` | `/var/lib/postgresql/data` | PostgreSQL data |
| `neo4jdata` | `/data` | Neo4j data |

### Common Operations

```bash
# View logs
docker compose logs -f forgerag

# Restart after config change
docker compose restart forgerag

# Stop everything
docker compose down

# Stop and remove volumes (⚠️ destroys data)
docker compose down -v

# Rebuild after code change
docker compose build && docker compose up -d
```

---

## Production Checklist

### Security

- [ ] Set strong passwords for PostgreSQL and Neo4j (use `secrets.token_urlsafe(32)`)
- [ ] Restrict CORS origins to your domain in `forgerag.yaml`
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

Run ForgeRAG with a local Ollama instance (no API key needed):

**1. Install and start Ollama:**

```bash
# Pull required models
ollama pull qwen2.5
ollama pull bge-m3
```

**2. Configure ForgeRAG:**

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

**Docker note:** Use `http://host.docker.internal:11434` as the API base when ForgeRAG runs in Docker and Ollama runs on the host.

---

## Database Migrations

ForgeRAG uses Alembic for database schema migrations:

```bash
# Check current migration status
alembic current

# Apply all pending migrations
alembic upgrade head

# Generate a new migration after model changes
alembic revision --autogenerate -m "description"
```

Migrations are automatically applied on startup for SQLite. For PostgreSQL, run them manually before upgrading ForgeRAG.

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
